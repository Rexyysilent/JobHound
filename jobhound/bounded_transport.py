"""Run-owned HTTP limits, streaming bounds and restart-safe attempt reservations.

State contains run IDs, hashed host/account scope, counters and codes, never URLs
or credentials. A reservation is not refunded once dispatch might have happened.
Only the explicit review caller owns/ closes the underlying transport.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
from pathlib import Path
import sqlite3
import time
import zlib

import httpx


class RequestDeferred(httpx.RequestError):
    pass


@dataclass(frozen=True)
class RequestLimits:
    requests: int = 60
    seconds: float = 180
    request_seconds: float = 30
    wire_bytes: int = 2_000_000
    decoded_bytes: int = 2_000_000
    concurrency: int = 4

    def __post_init__(self):
        if any(type(v) is not int for v in (self.requests, self.concurrency, self.wire_bytes, self.decoded_bytes)):
            raise ValueError('count and byte limits must be integers')
        if self.requests < 0 or self.concurrency < 1:
            raise ValueError('invalid request/concurrency limit')
        if any(not math.isfinite(v) or v <= 0 for v in
               (self.seconds, self.request_seconds, self.wire_bytes, self.decoded_bytes)):
            raise ValueError('limits must be finite and positive')


class RunBudget:
    def __init__(self, context, limits, *, wall=time.time, monotonic=time.monotonic, source_plan=None):
        self.context, self.limits = context, limits
        self.wall, self.monotonic = wall, monotonic
        self.started = monotonic()
        self.gate = asyncio.Semaphore(limits.concurrency)
        self.host_gates = {}
        self.sources = None
        if source_plan is not None:
            from .source_budget import SourceBudget
            self.sources = SourceBudget(self, source_plan)
            if self.sources.plan.request_ceiling > limits.requests:
                raise ValueError('source plan cannot add to shared request ceiling')
        root = Path(context.workspace)
        if any(p.casefold() in {'data', 'state'} for p in root.parts) or (root/'run.py').exists():
            raise ValueError('request state requires a dedicated review workspace')
        root.mkdir(parents=True, exist_ok=True)
        self.path = root/'request_budget.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, fingerprint TEXT,
                    used INTEGER NOT NULL, deadline REAL NOT NULL, cap INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cooldowns(scope TEXT PRIMARY KEY, until REAL, code TEXT);
                CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, run_id TEXT,
                    scope TEXT, state TEXT, code TEXT);
            ''')
            signature = context.fingerprint() + repr(limits)
            if self.sources is not None:
                self.sources.initialize(db)
                signature += self.sources.plan_json
            db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,0,?,?)',
                       (context.run_id, signature, wall()+limits.seconds,
                        min(limits.requests,self.sources.plan.request_ceiling) if self.sources else limits.requests))
            row = db.execute('SELECT fingerprint, deadline FROM runs WHERE id=?', (context.run_id,)).fetchone()
            if row[0] != signature:
                raise ValueError('run identity reused with different inputs or limits')
            self.deadline = row[1]

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def remaining(self):
        return min(self.deadline-self.wall(), self.limits.seconds-(self.monotonic()-self.started))

    def scope(self, request, *, account=False):
        host = request.url.host.casefold()
        # Credential identity is used only in a one-way digest, never persisted.
        credentials = '|'.join(v for k, v in request.headers.multi_items()
                               if k.lower() in {'authorization', 'x-api-key', 'x-rapidapi-key'})
        credentials += '|'.join(v for k, v in request.url.params.multi_items()
                                if k.lower() in {'app_key', 'app_id', 'api_key', 'key', 'token'})
        return hashlib.sha256((host + ('|account|'+credentials if account else '|host')).encode()).hexdigest()

    def reserve(self, request):
        if self.remaining() <= 0:
            raise RequestDeferred('time_budget_exhausted', request=request)
        host, account = self.scope(request), self.scope(request, account=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM cooldowns WHERE scope IN (?,?) AND until>?',
                          (host, account, self.wall())).fetchone():
                raise RequestDeferred('host_or_account_cooldown', request=request)
            row = db.execute('SELECT used, cap FROM runs WHERE id=?', (self.context.run_id,)).fetchone()
            if row[0] >= row[1]:
                raise RequestDeferred('request_budget_exhausted', request=request)
            checked = self.sources.check_http(db,request) if self.sources else None
            db.execute('UPDATE runs SET used=used+1 WHERE id=?', (self.context.run_id,))
            attempt=db.execute('INSERT INTO attempts(run_id,scope,state) VALUES(?,?,?)',
                               (self.context.run_id, host, 'reserved_unknown')).lastrowid
            if self.sources:self.sources.sent(db,attempt,checked)
            return attempt

    def finish(self, attempt, state, code=None):
        with self.connect() as db:
            db.execute('UPDATE attempts SET state=?,code=? WHERE id=?', (state, code, attempt))

    def cooldown(self, request, response):
        status = response.status_code
        if status not in {401, 403, 429, 503}:
            return
        value = response.headers.get('retry-after')
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = when.timestamp()-self.wall()
            except (ValueError, TypeError, OverflowError):
                delay = 3600 if status in {401, 403} else 60
        if not math.isfinite(delay):
            delay = 60
        delay = min(86400, max(1, delay))
        scope = self.scope(request, account=status in {401, 403, 429})
        with self.connect() as db:
            db.execute('INSERT INTO cooldowns VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET '
                       'until=max(until,excluded.until),code=excluded.code',
                       (scope, self.wall()+delay, str(status)))

    def receipt(self):
        with self.connect() as db:
            used = db.execute('SELECT used FROM runs WHERE id=?', (self.context.run_id,)).fetchone()[0]
            states = dict(db.execute('SELECT state,count(*) FROM attempts WHERE run_id=? GROUP BY state',
                                     (self.context.run_id,)))
            source_receipt=self.sources.receipt(db,used) if self.sources else None
        receipt={'requests_reserved': used,
                 'limit': min(self.limits.requests,self.sources.plan.request_ceiling) if self.sources else self.limits.requests,
                 'attempt_states': states}
        if source_receipt is not None:receipt['source_budget']=source_receipt
        return receipt


class BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner, budget):
        self.inner, self.budget = inner, budget
        self.is_mock = isinstance(inner, httpx.MockTransport)
        if not self.is_mock and not budget.context.network_allowed:
            raise ValueError('network capability is disabled')

    async def handle_async_request(self, request):
        budget = self.budget
        host_gate = budget.host_gates.setdefault(request.url.host, asyncio.Semaphore(1))
        remaining = budget.remaining()
        if remaining <= 0:
            raise RequestDeferred('time_budget_exhausted', request=request)
        try:
            async with asyncio.timeout(remaining):
                await budget.gate.acquire()
                try:
                    await host_gate.acquire()
                except BaseException:
                    budget.gate.release()
                    raise
        except TimeoutError as exc:
            raise RequestDeferred('time_budget_exhausted', request=request) from exc
        attempt = None
        response = None
        try:
            attempt = budget.reserve(request)
            request.headers['accept-encoding'] = 'gzip, deflate'
            expires = time.monotonic()+min(budget.remaining(), budget.limits.request_seconds)
            async with asyncio.timeout_at(expires):
                response = await self.inner.handle_async_request(request)
            budget.cooldown(request, response)
            stream = LimitedStream(response.stream, request, budget, attempt, expires,
                                   response.headers.get('content-encoding', 'identity'), host_gate)
            headers = [(k, v) for k, v in response.headers.raw
                       if k.lower() not in {b'content-encoding', b'content-length'}]
            return httpx.Response(response.status_code, headers=headers, stream=stream,
                                  extensions=response.extensions)
        except BaseException as exc:
            # Bookkeeping may fail (disk full, locked DB); the gates must not leak.
            try:
                if response is not None:
                    await response.aclose()
            finally:
                try:
                    if attempt is not None:
                        budget.finish(attempt, 'interrupted', type(exc).__name__)
                finally:
                    host_gate.release()
                    budget.gate.release()
            if isinstance(exc, TimeoutError):
                raise httpx.ReadTimeout('request_deadline', request=request) from exc
            raise

    async def aclose(self):
        # Discovery and hydration clients borrow this transport. Owner closes it.
        pass

    async def close_owned(self):
        await self.inner.aclose()


class LimitedStream(httpx.AsyncByteStream):
    def __init__(self, inner, request, budget, attempt, expires, encoding, host_gate):
        self.inner, self.request, self.budget = inner, request, budget
        self.attempt, self.expires, self.encoding = attempt, expires, encoding.lower().strip()
        self.host_gate, self.closed = host_gate, False
        self.finished = False

    async def __aiter__(self):
        wire = decoded = 0
        try:
            if self.encoding not in {'identity', '', 'gzip', 'deflate'}:
                raise httpx.DecodingError('unsupported_content_encoding', request=self.request)
            decoder = zlib.decompressobj(31 if self.encoding == 'gzip' else 15) if self.encoding in {'gzip', 'deflate'} else None
            async with asyncio.timeout_at(self.expires):
                async for chunk in self.inner:
                    wire += len(chunk)
                    if wire > self.budget.limits.wire_bytes:
                        raise httpx.ReadError('wire_size_limit', request=self.request)
                    data = decoder.decompress(chunk, self.budget.limits.decoded_bytes-decoded+1) if decoder else chunk
                    decoded += len(data)
                    if decoded > self.budget.limits.decoded_bytes or (decoder and decoder.unconsumed_tail):
                        raise httpx.ReadError('decoded_size_limit', request=self.request)
                    if decoder and decoder.unused_data:
                        raise httpx.DecodingError('trailing_compressed_data', request=self.request)
                    yield data
                if decoder and not decoder.eof:
                    raise httpx.DecodingError('truncated_compressed_body', request=self.request)
            self.finished = True
            self.budget.finish(self.attempt, 'complete')
        except TimeoutError as exc:
            self.budget.finish(self.attempt, 'interrupted', 'request_deadline')
            raise httpx.ReadTimeout('request_deadline', request=self.request) from exc
        except BaseException as exc:
            self.budget.finish(self.attempt, 'interrupted', type(exc).__name__)
            if isinstance(exc, zlib.error):
                raise httpx.DecodingError('invalid_compressed_body', request=self.request) from exc
            raise
        finally:
            await self.aclose()

    async def aclose(self):
        if not self.closed:
            self.closed = True
            try:
                await self.inner.aclose()
            finally:
                try:
                    if not self.finished:
                        self.budget.finish(self.attempt, 'interrupted', 'body_not_completed')
                finally:
                    self.host_gate.release()
                    self.budget.gate.release()
