"""Offline run isolation and bounded-transport acceptance tests."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import gzip
import json
import sqlite3

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.run_context import RunContext, run_scope, current_run
from jobhound.bounded_transport import BoundedTransport, RunBudget, RequestLimits, RequestDeferred
from jobhound.filters.eligibility import default_profile
from jobhound.trust.registry import default_registry, default_matcher
from jobhound.v41.engine import evaluate_raw, decision_fingerprint
from jobhound.v41.review import capture_review

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def context(tmp_path, **kw):
    cfg = CONFIG.model_copy(deep=True)
    cfg.v55.enabled = True
    return RunContext.capture(config=cfg, workspace=tmp_path, as_of=NOW, **kw)


def record():
    return {'source': 'ashby', 'raw': {'id': 'isolated', '_company': 'Example',
        'title': 'Bengali AI Response Evaluator', 'jobUrl': 'https://jobs.ashbyhq.com/example/isolated',
        'location': 'India', 'isRemote': True, 'descriptionPlain':
        'Review Bengali AI responses using supplied guidelines. Bengali fluency required. '
        'Remote applicants in India may apply. No degree or prior experience required. '
        'Compensation: USD 12 per hour.'}}


def test_concurrent_opposing_profiles_policy_and_registry(tmp_path):
    base = context(tmp_path)
    cfg = base.config()
    cfg.pay.floor_usd = 100
    profile = replace(base.profile(), languages={'german'}, working_languages={'german'})
    other = RunContext.capture(config=cfg, profile=profile, registry={}, as_of=NOW, workspace=tmp_path)
    baseline = CONFIG.model_dump()
    async def one(ctx):
        with run_scope(ctx):
            await asyncio.sleep(0)
            result = evaluate_raw([record()])
            await asyncio.sleep(0)
            again = evaluate_raw([record()])
            assert decision_fingerprint(result) == decision_fingerprint(again)
            assert default_profile().languages == ctx.profile().languages
            assert set(default_registry()) == set(ctx.registry())
            assert CONFIG.pay.floor_usd == ctx.config().pay.floor_usd
            return result
    async def both():
        return await asyncio.gather(one(base), one(other))
    a, b = asyncio.run(both())
    assert a.evaluated[0].decision.terminal_reason != b.evaluated[0].decision.terminal_reason
    assert a.metadata.profile_hash != b.metadata.profile_hash
    assert a.metadata.trust_registry_hash != b.metadata.trust_registry_hash
    assert CONFIG.model_dump() == baseline and current_run() is None


def test_context_snapshot_mutation_and_exception_restoration(tmp_path):
    cfg = CONFIG.model_copy(deep=True)
    ctx = RunContext.capture(config=cfg, workspace=tmp_path, as_of=NOW)
    cfg.pay.floor_usd = 999
    with pytest.raises(RuntimeError):
        with run_scope(ctx):
            assert CONFIG.pay.floor_usd != 999
            with pytest.raises(TypeError):
                CONFIG.v55.enabled = True
            CONFIG.pay.fx_to_usd.clear()
            assert CONFIG.pay.fx_to_usd
            default_profile().languages.clear()
            assert default_profile().languages
            raise RuntimeError('test')
    assert current_run() is None


def test_snapshot_preserves_rule_and_alias_precedence(tmp_path):
    profile = default_profile()
    registry = default_registry()
    ctx = context(tmp_path, profile=profile, registry=registry)
    with run_scope(ctx):
        assert list(default_profile().role_concepts) == list(profile.role_concepts)
        assert list(default_profile().gated_role_patterns) == list(profile.gated_role_patterns)
        assert list(default_registry()) == list(registry)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False
    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk
    async def aclose(self):
        self.closed = True


def test_shared_budget_concurrency_restart_and_no_refund(tmp_path):
    ctx = context(tmp_path)
    calls = []
    def handler(req):
        calls.append(req.url.host)
        return httpx.Response(200, stream=Chunks([b'ok']))
    async def run():
        limits = RequestLimits(requests=2)
        owner = RunBudget(ctx, limits)
        transport = BoundedTransport(httpx.MockTransport(handler), owner)
        async with httpx.AsyncClient(transport=transport) as client:
            results = await asyncio.gather(*(client.get(f'https://host{i}.example/') for i in range(8)), return_exceptions=True)
        assert sum(isinstance(r, httpx.Response) for r in results) == 2
        assert sum(isinstance(r, RequestDeferred) for r in results) == 6
        restarted = RunBudget(ctx, limits)
        assert restarted.receipt()['requests_reserved'] == 2
        with pytest.raises(RequestDeferred):
            restarted.reserve(httpx.Request('GET', 'https://new.example'))
    asyncio.run(run())
    assert len(calls) == 2


@pytest.mark.parametrize('status', [401, 403, 429, 503])
def test_persistent_account_and_host_cooldowns(tmp_path, status):
    ctx = context(tmp_path)
    limits = RequestLimits()
    budget = RunBudget(ctx, limits)
    req = httpx.Request('GET', 'https://provider.example/', headers={'x-api-key': 'synthetic-a'})
    budget.cooldown(req, httpx.Response(status, headers={'Retry-After': '120'}))
    restarted = RunBudget(replace(ctx, run_id='next'), limits)
    with pytest.raises(RequestDeferred):
        restarted.reserve(req)
    different = httpx.Request('GET', 'https://provider.example/', headers={'x-api-key': 'synthetic-b'})
    if status == 503:
        with pytest.raises(RequestDeferred):
            restarted.reserve(different)
    else:
        restarted.reserve(different)
    assert b'synthetic-a' not in budget.path.read_bytes()


@pytest.mark.parametrize('encoding,payload,wire,decoded,error', [
    ('identity', b'a'*101, 100, 200, 'wire_size_limit'),
    ('identity', b'a'*101, 200, 100, 'decoded_size_limit'),
    ('gzip', gzip.compress(b'a'*10000), 1000, 100, 'decoded_size_limit'),
    ('gzip', gzip.compress(b'ok')[:-5], 1000, 100, 'truncated_compressed_body'),
    ('br', b'anything', 1000, 100, 'unsupported_content_encoding'),
])
def test_stream_bounds_before_buffering(tmp_path, encoding, payload, wire, decoded, error):
    stream = Chunks([payload])
    async def run():
        owner = RunBudget(context(tmp_path), RequestLimits(wire_bytes=wire, decoded_bytes=decoded))
        transport = BoundedTransport(httpx.MockTransport(lambda r: httpx.Response(200,
            headers={'Content-Encoding': encoding}, stream=stream)), owner)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.HTTPError, match=error):
                await client.get('https://example.org')
        assert owner.receipt()['attempt_states'] == {'interrupted': 1}
    asyncio.run(run())
    assert stream.closed


def test_gzip_success_and_slow_stream_total_deadline(tmp_path):
    async def run():
        owner = RunBudget(context(tmp_path), RequestLimits(request_seconds=.04))
        slow = Chunks([b'x']*10, .02)
        def handler(req):
            return httpx.Response(200, stream=slow if req.url.path=='/slow' else Chunks([gzip.compress(b'hello')]),
                headers={} if req.url.path=='/slow' else {'Content-Encoding': 'gzip'})
        async with httpx.AsyncClient(transport=BoundedTransport(httpx.MockTransport(handler), owner)) as client:
            assert (await client.get('https://example.org/')).text == 'hello'
            with pytest.raises(httpx.ReadTimeout):
                await client.get('https://example.org/slow')
        assert slow.closed
        assert owner.receipt()['requests_reserved'] == 2
    asyncio.run(run())


def test_cancelled_stream_releases_gate_and_keeps_attempt(tmp_path):
    async def run():
        owner = RunBudget(context(tmp_path), RequestLimits(concurrency=1))
        entered = asyncio.Event()
        async def handler(req):
            entered.set()
            return httpx.Response(200, stream=Chunks([b'x'], 5 if req.url.path=='/slow' else 0))
        async with httpx.AsyncClient(transport=BoundedTransport(httpx.MockTransport(handler), owner)) as client:
            task = asyncio.create_task(client.get('https://example.org/slow'))
            await entered.wait()
            await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await asyncio.wait_for(client.get('https://example.org/ok'), 1)).status_code == 200
        assert owner.receipt()['attempt_states'] == {'complete': 1, 'interrupted': 1}
    asyncio.run(run())


def test_capture_actual_entrypoint_uses_shared_budget(tmp_path):
    cfg = CONFIG.model_copy(deep=True)
    cfg.v55.enabled = True
    cfg.sources.ats.enabled = True
    cfg.sources.ats.ashby = {'example': 'Example', 'second': 'Second'}
    ctx = RunContext.capture(config=cfg, as_of=NOW, workspace=tmp_path)
    calls = []
    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={'jobs': [{**record()['raw'], 'isListed': True}]})
    snapshot, _ = asyncio.run(capture_review(tmp_path, source='ashby', context=ctx,
        transport=httpx.MockTransport(handler), request_limits=RequestLimits(requests=1)))
    assert snapshot.exists() and len(calls) == 1
    receipt = json.loads((tmp_path/'request_budget_receipt.json').read_text())
    assert receipt['requests_reserved'] == 1
    assert not (tmp_path/'.active-run').exists()


def test_network_capability_and_exclusive_namespace(tmp_path):
    ctx = context(tmp_path)
    with pytest.raises(ValueError, match='capability'):
        BoundedTransport(httpx.AsyncHTTPTransport(), RunBudget(ctx, RequestLimits()))
    from jobhound.v41.review import _exclusive_workspace
    with _exclusive_workspace(tmp_path):
        with pytest.raises(FileExistsError):
            asyncio.run(capture_review(tmp_path, context=ctx, transport=httpx.MockTransport(lambda r: httpx.Response(200))))


def test_default_argument_config_view_is_run_local(tmp_path):
    from jobhound.filters.scam import is_scam
    from jobhound.models import Job
    cfg = CONFIG.model_copy(deep=True)
    cfg.scam.threshold = 0.1
    ctx = RunContext.capture(config=cfg, workspace=tmp_path, as_of=NOW)
    with run_scope(ctx):
        job = Job(source='fixture', title='Reviewer', url='https://example.org', scam_score=0.2)
        assert is_scam(job)
    assert not is_scam(job)


def test_crash_unknown_reservation_and_identity_guard(tmp_path):
    ctx = context(tmp_path)
    limits = RequestLimits(requests=1)
    first = RunBudget(ctx, limits)
    first.reserve(httpx.Request('GET', 'https://example.org'))
    resumed = RunBudget(ctx, limits)
    assert resumed.receipt()['attempt_states'] == {'reserved_unknown': 1}
    with pytest.raises(RequestDeferred):
        resumed.reserve(httpx.Request('GET', 'https://example.org'))
    with pytest.raises(ValueError, match='identity reused'):
        RunBudget(ctx, RequestLimits(requests=2))


def test_redirect_hops_consume_shared_allowance(tmp_path):
    calls = []
    def handler(req):
        calls.append(req.url)
        return httpx.Response(302, headers={'Location': '/next'})
    async def run():
        owner = RunBudget(context(tmp_path), RequestLimits(requests=2))
        async with httpx.AsyncClient(transport=BoundedTransport(httpx.MockTransport(handler), owner)) as client:
            with pytest.raises(RequestDeferred):
                await client.get('https://example.org/start', follow_redirects=True)
        assert owner.receipt()['requests_reserved'] == 2
    asyncio.run(run())
    assert len(calls) == 2


def test_capture_cancellation_keeps_completed_batch_and_attempts(tmp_path, monkeypatch):
    import jobhound.pipeline as pipeline
    from jobhound.sources.base import Source
    class FakeSource(Source):
        enabled = True
        def __init__(self, name):
            super().__init__()
            self.name = name
        async def _fetch(self, client):
            response = await client.get('https://' + self.name + '.example')
            return response.json()['jobs']
    monkeypatch.setattr(pipeline, 'build_sources', lambda **kw: [FakeSource('fast'), FakeSource('slow')])
    async def run():
        entered = asyncio.Event()
        async def handler(req):
            if req.url.host == 'slow.example':
                entered.set()
                await asyncio.sleep(10)
            return httpx.Response(200, json={'jobs': [record()['raw']]})
        ctx = context(tmp_path)
        task = asyncio.create_task(capture_review(tmp_path, context=ctx, transport=httpx.MockTransport(handler)))
        await entered.wait()
        await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        receipt = json.loads((tmp_path/'request_budget_receipt.json').read_text())
        assert receipt['requests_reserved'] == 2
        saved = json.loads(next(tmp_path.glob('completed_batches_*.jsonl')).read_text())
        assert len(saved['records']) == 1 and saved['records'][0]['source'] == 'fast'
        assert not (tmp_path/'.active-run').exists()
    asyncio.run(run())


def test_review_side_effects_fail_before_io(tmp_path):
    from jobhound.store import Store
    from jobhound.v41.store import V41Store
    from jobhound.notify.email import EmailNotifier
    from jobhound.notify.telegram import TelegramNotifier
    from jobhound.filters.scam_llm import second_pass
    with run_scope(context(tmp_path)):
        for cls in (Store, V41Store):
            with pytest.raises(RuntimeError, match='forbids'):
                cls(tmp_path/'should-not-exist.db')
        for coroutine in (EmailNotifier().send('test'), TelegramNotifier().send('test'), second_pass([])):
            with pytest.raises(RuntimeError, match='forbids'):
                asyncio.run(coroutine)
    assert not (tmp_path/'should-not-exist.db').exists()


def test_budget_refuses_production_shaped_path(tmp_path):
    with pytest.raises(ValueError, match='dedicated review'):
        RunBudget(context(tmp_path/'data'), RequestLimits())
    assert not (tmp_path/'data').exists()


def test_dns_phase_has_a_deadline_without_dispatch(tmp_path, monkeypatch):
    import jobhound.v41.hydration as hydration
    async def slow(*a, **kw):
        await asyncio.sleep(5)
    monkeypatch.setattr(hydration, '_validate_destination', slow)
    async def run():
        owner = RunBudget(context(tmp_path), RequestLimits(request_seconds=.01))
        async with httpx.AsyncClient(transport=BoundedTransport(httpx.MockTransport(lambda r: httpx.Response(200)), owner)) as client:
            with pytest.raises(RuntimeError, match='dns_time_budget'):
                await hydration._request(client, hydration.RetrievalBudget(hydration.RetrievalPolicy()), 'https://example.org')
        assert owner.receipt()['requests_reserved'] == 0
    asyncio.run(run())
