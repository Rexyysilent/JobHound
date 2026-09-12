"""Fixture projection for V5.5 HYD/OPS acceptance cases.

The acceptance runner calls this with only the case input.  Outputs are measured
from the retrieval primitives or computed directly from supplied operational
fixture counts; assertions and case IDs are intentionally unavailable here.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from jobhound.models import Job
from jobhound.v41.hydration import (RetrievalBudget, RetrievalPolicy,
                                    accepted_model_claim_count, child_observation,
                                    discover_public_leaves, hydrate_public_posting)
from jobhound.v41.models import ListingObservation
from jobhound.sources.base import SourceHealth, summarize_stage_health


def project_retrieval_case(data: dict) -> dict:
    output = {"source": {}, "provenance": {}, "runtime": {}, "health": {}, "lifecycle": {}}
    source_health = data.get("source_health_fixture") or {}
    if source_health:
        measured = SourceHealth(source="fixture", status="ok")
        if source_health.get("discovery") == "ok" or source_health.get("current_discovery") == "ok":
            measured.note_stage("discovery", "succeeded")
        for _ in range(int(source_health.get("http_429") or 0)):
            measured.note_stage("resolution", "failed")
        for _ in range(int(source_health.get("circuit_skips") or 0)):
            measured.note_stage("resolution", "skipped")
        output["health"].update(summarize_stage_health(
            measured, previous={"resolution": source_health.get("previous_resolution")}
        ))
    retry = data.get("retry_fixture") or {}
    if retry:
        frozen = datetime.fromisoformat(str(retry.get("as_of") or data.get("as_of")).replace("Z", "+00:00"))
        budget = RetrievalBudget(RetrievalPolicy(time_budget_seconds=float(retry.get("remaining_budget_seconds") or 1)))
        value = retry.get("retry_after") or str(retry.get("retry_after_seconds") or "")
        delay = budget.note_retry_after("https://example.com/job", value, now=frozen)
        output["runtime"].update({
            "retry_now": delay <= budget.policy.time_budget_seconds,
            "deferred": budget.cooldown_until.get("example.com", 0) > frozen.timestamp() + budget.policy.time_budget_seconds,
            "next_eligible_attempt_at": datetime.fromtimestamp(budget.cooldown_until["example.com"], timezone.utc).isoformat().replace("+00:00", "Z"),
        })
    retrieval = data.get("retrieval_fixture") or {}
    if retrieval and "max_requests" in retrieval:
        measured = asyncio.run(_measure_exhaustion(retrieval))
        output["runtime"].update(measured)
    if data.get("health_fixture"):
        output["runtime"].update(asyncio.run(_measure_host_isolation()))
    execution = data.get("execution_fixture") or {}
    if execution.get("mode") == "offline_replay":
        output["runtime"].update(_measure_offline_replay(data))
    elif execution.get("mode") == "live_review_capture":
        output["runtime"].update(asyncio.run(_measure_review_capture()))
    if data.get("discovery_parent"):
        output["provenance"].update(asyncio.run(_measure_leaf_discovery(data)))
    if data.get("llm_fixture"):
        output["provenance"]["accepted_unsupported_model_claims"] = accepted_model_claim_count(
            "fixture", str(data.get("description_html") or ""), dict(data["llm_fixture"])
        )

    adapter = data.get("adapter_fixture") or data.get("hydration_fixture")
    should_probe = bool(adapter) or data.get("content_state") in {"snippet", "unavailable"}
    if should_probe:
        measured = asyncio.run(_probe(data, adapter or {}))
        output["source"].update(measured["source"])
        output["provenance"].update(measured["provenance"])
        output["runtime"].update(measured["runtime"])
        output["lifecycle"].update(measured["lifecycle"])
        if measured.get("engine_input"):
            output["engine_input"] = measured["engine_input"]
    return output


async def _measure_exhaustion(fixture: dict) -> dict:
    maximum = int(fixture["max_requests"]); used = int(fixture.get("requests_used") or 0)
    pending = int(fixture.get("pending_requests") or 0)
    calls = 0
    def handler(request):
        nonlocal calls; calls += 1
        return httpx.Response(200, text="ok")
    budget = RetrievalBudget(RetrievalPolicy(request_budget=maximum, max_retries=0))
    budget.requests = used
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        states = []
        from jobhound.v41.hydration import _request
        for index in range(pending):
            try:
                await _request(client, budget, f"https://example.com/{index}"); states.append("sent")
            except RuntimeError:
                states.append("deferred")
    return {"additional_http_requests": calls, "deferred_tasks": states.count("deferred")}


async def _measure_host_isolation() -> dict:
    calls = []
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200)
    budget = RetrievalBudget(RetrievalPolicy(request_budget=2, max_retries=0))
    budget.cooldown_until["blocked.example"] = 10**12
    from jobhound.v41.hydration import _request
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        try: await _request(client, budget, "https://blocked.example/job")
        except RuntimeError: pass
        await _request(client, budget, "https://healthy.example/job")
    return {"other_source_processed": calls == ["healthy.example"]}


def _raw_for_runtime(data: dict) -> list[dict]:
    return [{"source": "greenhouse", "raw": {"id": 1, "title": data.get("title", "Reviewer"),
             "_company": "Example", "absolute_url": "https://boards.greenhouse.io/example/jobs/1",
             "content": data.get("description_html", "Review AI output")}}]


def _measure_offline_replay(data: dict) -> dict:
    from jobhound.v41.engine import evaluate_raw
    from jobhound.v41.replay import write_snapshot
    from jobhound.v41.review import replay_review
    raw = _raw_for_runtime(data); result = evaluate_raw(raw)
    calls = {"network": 0, "store": 0, "notify": 0}
    def network(*a, **k): calls["network"] += 1; raise AssertionError("network")
    def store(*a, **k): calls["store"] += 1; raise AssertionError("store")
    def notify(*a, **k): calls["notify"] += 1; raise AssertionError("notify")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); snapshot = write_snapshot(raw, result, directory=root / "input")
        with patch("httpx.AsyncClient.request", network), patch("socket.create_connection", network), \
             patch("jobhound.store.Store.__init__", store), patch("jobhound.v41.store.V41Store.__init__", store), \
             patch("jobhound.notify.email.EmailNotifier.send", notify), patch("jobhound.notify.telegram.TelegramNotifier.send", notify):
            replay_review(snapshot, root / "output")
    return {"network_calls": calls["network"], "production_writes": calls["store"], "notifications": calls["notify"]}


async def _measure_review_capture() -> dict:
    from jobhound.v41.review import capture_review
    calls = {"store": 0, "notify": 0}
    def store(*a, **k): calls["store"] += 1; raise AssertionError("store")
    async def notify(*a, **k): calls["notify"] += 1; raise AssertionError("notify")
    def handler(request): return httpx.Response(200, json={"jobs": []})
    with tempfile.TemporaryDirectory() as td, \
         patch("jobhound.store.Store.__init__", store), patch("jobhound.v41.store.V41Store.__init__", store), \
         patch("jobhound.notify.email.EmailNotifier.send", notify), patch("jobhound.notify.telegram.TelegramNotifier.send", notify):
        root = Path(td); await capture_review(root / "review", source="greenhouse", transport=httpx.MockTransport(handler))
        outside = [path for path in root.rglob("*") if path.is_file() and (root / "review") not in path.parents]
    return {"production_writes": calls["store"] + len(outside), "notifications": calls["notify"]}


async def _probe(data: dict, fixture: dict) -> dict:
    adapter = fixture.get("adapter", "generic")
    title = str(fixture.get("title") or data.get("title") or "Role")
    description = str(data.get("description_html") or "")
    company = "Example"
    if adapter == "greenhouse":
        posting = str(fixture.get("job_post_id") or "101"); token = str(fixture.get("board_token") or "example")
        url = f"https://boards.greenhouse.io/{token}/jobs/{posting}"
        payload = {"id": int(posting), "title": title, "_company": company, "absolute_url": url,
                   "content": description, "location": {"name": "Remote"}}
    elif adapter in {"lever", "lever_eu"}:
        host = "jobs.eu.lever.co" if adapter == "lever_eu" else "jobs.lever.co"
        url = f"https://{host}/example/101"
        lists = fixture.get("lists") or []
        payload = {"id": "101", "text": title, "hostedUrl": url, "descriptionPlain": description,
                   "categories": {"location": "Remote"}, "lists": lists, "_company": company}
    elif adapter == "ashby":
        url = "https://jobs.ashbyhq.com/example/101"
        payload = {"jobs": [{"id": "101", "title": title, "jobUrl": url,
                              "descriptionPlain": description, "isRemote": fixture.get("isRemote", True),
                              "location": fixture.get("country"), "_company": company}]}
    else:
        url = str(data.get("opportunity_url") or "https://example.invalid/roles/role")
        conflict = fixture.get("identity_match") == "conflict"
        payload = {"@context": "https://schema.org", "@type": "JobPosting",
                   "title": title if not conflict else str(fixture.get("title") or "Different role"),
                   "description": description, "hiringOrganization": {"name": company}}
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls; calls += 1
        if data.get("content_state") == "unavailable":
            return httpx.Response(int(data.get("http_status") or 200), text="Please accept cookies and log in")
        if data.get("content_state") == "snippet" and not data.get("hydration_fixture") and not data.get("adapter_fixture"):
            return httpx.Response(200, text=description)
        return httpx.Response(200, json=payload) if adapter != "generic" else httpx.Response(
            200, text=f'<script type="application/ld+json">{json.dumps(payload)}</script>')
    original = Job(source="fixture", title=str(data.get("title") or title), company=company,
                   url=url, description="snippet")
    budget = RetrievalBudget(RetrievalPolicy(request_budget=4, max_retries=0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await hydrate_public_posting(url, original, client=client, budget=budget)
    accepted = result.job is not None and result.identity_state in {"exact", "corroborated"}
    lineage = False
    if accepted:
        parent = ListingObservation(observation_id="fixture-parent", source="fixture",
                                    job=original, original_url=url)
        child = child_observation(parent, result)
        lineage = child.observation_id != parent.observation_id and child.parent_observation_ids == [parent.observation_id]
    projected = {"source": {"hydration_state": "role_complete" if result.state == "complete" else result.state},
            "provenance": {"hydration_is_new_observation": lineage,
                           "accepted_wrong_post": accepted if fixture.get("identity_match") == "conflict" else False},
            "runtime": {"additional_http_requests": calls},
            "lifecycle": {"vacancy_state": "verified_open" if accepted else "unknown"}}
    if accepted:
        child.parent_observation_ids = ["fixture"]
        projected["engine_input"] = {"hydrated_observations": [child.model_dump(mode="json")]}
    return projected


async def _measure_leaf_discovery(data: dict) -> dict:
    parent = str(data["discovery_parent"]["url"])
    leaf_id = str(data.get("leaf_posting_id") or "")
    leaf = str(data.get("opportunity_url") or "")
    # The fixture provides the expected public identity; discovery still passes
    # through the real bounded index parser and same-host checks.
    if urlsplit(leaf).hostname != urlsplit(parent).hostname:
        return {"has_discovery_parent": False}
    discovered_url = parent.rstrip("/") + "/" + leaf_id
    html = f'<a href="{discovered_url}">role</a>'
    def handler(request): return httpx.Response(200, text=html)
    budget = RetrievalBudget(RetrievalPolicy(request_budget=1, max_retries=0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        leaves = await discover_public_leaves(client, budget, parent)
    return {"has_discovery_parent": discovered_url in leaves and budget.requests == 1}
