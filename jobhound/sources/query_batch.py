"""Bounded per-query collection. Query evidence is distinct from record identity.

No retries, new queries, endpoint changes or cross-run cooldown claims. Counters
are per request, and query fingerprints live in health, not provider payloads.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .ats_batch import _cooldown
from .base import Source
from ..bounded_transport import RequestDeferred


def jsearch_rows(payload):
    if not isinstance(payload, dict) or "data" not in payload:
        raise ValueError("missing_data")
    data = payload["data"]
    if isinstance(data, dict):
        if "jobs" not in data:
            raise ValueError("missing_jobs")
        return data["jobs"]
    return data  # explicit old flat-list contract; null is malformed, not empty


def serp_rows(payload):
    if not isinstance(payload, dict) or "organic" not in payload:
        raise ValueError("missing_organic")
    return payload["organic"]


async def fetch_queries(
    source: Source, client: httpx.AsyncClient, queries: list[str], *,
    method: str, url: str, headers: dict, request_args: Callable,
    rows_from_payload: Callable, valid_row: Callable,
) -> list[dict]:
    health = source.health
    out, fingerprints = [], set()
    stop = None
    valid_queries = 0
    health.transport_hosts = [urlsplit(url).hostname]
    for index, query in enumerate(queries):
        row = {"index": index, "query_hash": hashlib.sha256(query.encode()).hexdigest()[:16],
               "retained": 0, "duplicates": 0, "malformed": 0, "budget_truncated": False,
               "retained_fingerprints": [], "duplicate_fingerprints": []}
        health.query_results.append(row)
        if stop or (source.limit is not None and len(out) >= max(0, source.limit)):
            row.update(status="deferred", reason=stop or "result_budget_reached")
            health.note_stage("discovery", "deferred")
            continue
        try:
            response = await client.request(method, url, headers=headers,
                                            follow_redirects=False, **request_args(query))
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = f"http_{status}"
            health.failed_requests += 1
            health.note_stage("discovery", "failed")
            health.error_type = reason
            row.update(status="failed", reason=reason)
            if status in {401, 403, 429} or (status == 503 and exc.response.headers.get("Retry-After")):
                stop = reason + "_stop_batch"
                health.cooldown_until = _cooldown(exc.response.headers.get("Retry-After"), datetime.now(timezone.utc))
            continue
        except RequestDeferred as exc:
            stop = str(exc)
            row.update(status='deferred', reason=stop)
            health.note_stage('discovery', 'deferred')
            continue
        except httpx.RequestError as exc:
            reason = type(exc).__name__
            health.failed_requests += 1
            health.note_stage("discovery", "failed")
            health.error_type = reason
            row.update(status="failed", reason=reason)
            continue
        health.successful_requests += 1
        health.note_stage("discovery", "succeeded")
        try:
            rows = rows_from_payload(response.json())
            if not isinstance(rows, list):
                raise ValueError("items_not_list")
        except (ValueError, TypeError):
            health.note_stage("parsing", "failed")
            health.error_type = "invalid_payload"
            row.update(status="failed", reason="invalid_payload")
            continue
        valid_queries += 1
        for item in rows:
            if not isinstance(item, dict) or not valid_row(item):
                row["malformed"] += 1
                continue
            try:
                encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError):
                row["malformed"] += 1
                continue
            fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
            if fingerprint in fingerprints:
                row["duplicates"] += 1
                row["duplicate_fingerprints"].append(fingerprint)
                continue
            if source.limit is not None and len(out) >= max(0, source.limit):
                row["budget_truncated"] = True
                continue
            fingerprints.add(fingerprint)
            out.append(item)
            row["retained"] += 1
            row["retained_fingerprints"].append(fingerprint)
        health.note_stage("parsing", "failed" if row["malformed"] else "succeeded")
        if row["malformed"]:
            health.error_type = "invalid_rows"
        row["status"] = "partial" if row["malformed"] or row["budget_truncated"] else "ok" if rows else "empty"
    degraded = any(row["status"] not in {"ok", "empty"} for row in health.query_results)
    health.status = ("partial" if out or valid_queries or not health.failed_requests else "failed") if degraded else "ok" if out else "empty"
    if degraded and not out and not valid_queries and any(row["status"] == "failed" for row in health.query_results):
        health.status = "failed"
    return out
