from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jobhound.models import Job
from jobhound.v41.hydration import (
    RetrievalBudget,
    RetrievalPolicy,
    _exact_identity,
    hydrate_public_posting,
    resolve_public_original,
)
from jobhound.v41.provenance import build_observations, sanitize_payload
from jobhound.v41.replay import load_snapshot
from jobhound.v41.replay import replay
from jobhound.v41.engine import evaluate_raw
from jobhound.config import CONFIG
from jobhound.v41 import review


def test_surrogate_raw_capture_is_repaired_hashable_and_roundtrips(tmp_path):
    raw = [{
        "source": "jsearch",
        "raw": {
            "job_title": "Unicode \ud800 reviewer",
            "employer_name": "Example \udfff",
            "job_apply_link": "https://example.com/jobs/\ud800",
            "job_description": "valid pair: \ud83d\ude00 invalid: \ud800",
            "bad\ud800key": "value",
        },
    }]

    observations, failures = build_observations(raw)
    assert not failures
    assert len(observations) == 1
    description = observations[0].raw_payload["job_description"]
    assert description == f"valid pair: {chr(0x1F600)} invalid: \ufffd"
    assert "\ud800" not in description
    encoded = json.dumps(observations[0].model_dump(mode="json"), ensure_ascii=False)
    encoded.encode("utf-8")
    assert "\ud800" not in encoded and "\udfff" not in encoded

    path = tmp_path / "raw.json"
    path.write_text(json.dumps(sanitize_payload(raw[0]), ensure_ascii=False), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["raw"]["job_description"] == description


def test_capture_checkpoints_sanitized_raw_before_evaluation_failure(tmp_path, monkeypatch):
    raw = [{"source": "jsearch", "raw": {"job_description": "bad \ud800"}}]

    async def ingest(**_kwargs):
        return raw, []

    monkeypatch.setattr(review, "ingest_raw_with_health", ingest)
    monkeypatch.setattr(review, "evaluate_raw", lambda _raw, **_kwargs: (_ for _ in ()).throw(RuntimeError("deliberate")))

    target = tmp_path / "review"
    with pytest.raises(RuntimeError, match="deliberate"):
        asyncio.run(review.capture_review(target))
    checkpoint = target / "raw_discovery.json"
    assert checkpoint.exists()
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved[0]["raw"]["job_description"] == "bad \ufffd"
    metadata = json.loads((target / "raw_discovery.metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "evaluation_failed"
    header, loaded = load_snapshot(checkpoint)
    assert header["record_count"] == 1 and loaded == saved


def test_checkpoint_replays_with_frozen_release_policy_and_as_of(tmp_path):
    raw = [{"source": "serp", "raw": {
        "title": "Bengali AI evaluator", "link": "https://example.com/jobs/1",
        "snippet": "Review Bengali AI output remotely.",
    }}]
    old = CONFIG.v55.enabled
    CONFIG.v55.enabled = True
    try:
        checkpoint, metadata_path, _captured_at = review._checkpoint_raw(raw, [], tmp_path)
    finally:
        CONFIG.v55.enabled = old
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["release_policy"]["enabled"] is True
    result = replay(checkpoint)
    assert result.metadata.engine_version == "v5.0.0-rc1"
    assert result.metadata.as_of.isoformat() == metadata["captured_at"]
    assert CONFIG.v55.enabled == old


@pytest.mark.parametrize("header", [None, "not-a-delay"])
def test_adzuna_missing_or_malformed_retry_after_uses_fallback_cooldown(tmp_path, header):
    def handler(request):
        headers = {} if header is None else {"Retry-After": header}
        return httpx.Response(429, headers=headers, request=request)

    async def exercise():
        budget = RetrievalBudget(RetrievalPolicy(retry_after_fallback_seconds=60), review_namespace=tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await resolve_public_original(client, budget, "https://www.adzuna.in/details/1")
        return budget

    budget = asyncio.run(exercise())
    assert budget.cooldown_until["www.adzuna.in"] > __import__("time").time() + 50


def test_concurrent_same_host_429_only_reaches_transport_once():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, request=request)

    original = Job(source="serp", title="Role", company="Example",
                   url="https://example.com/jobs/1", description="short")

    async def exercise():
        budget = RetrievalBudget(RetrievalPolicy(per_host_concurrency=1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await asyncio.gather(*(
                hydrate_public_posting(f"https://example.com/jobs/{i}", original,
                                       client=client, budget=budget)
                for i in range(4)
            ))

    outcomes = asyncio.run(exercise())
    assert calls == 1
    assert outcomes[0].error == "http_429"
    assert all(row.state == "deferred" for row in outcomes)


def test_adzuna_retry_after_cools_host_and_prevents_followup_request(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, request=request)

    async def exercise():
        budget = RetrievalBudget(RetrievalPolicy(), review_namespace=tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await resolve_public_original(client, budget, "https://www.adzuna.in/details/1")
            assert first[1] == "http_429"
            with pytest.raises(RuntimeError, match="host_cooldown"):
                await resolve_public_original(client, budget, "https://www.adzuna.in/details/2")
        return budget

    budget = asyncio.run(exercise())
    assert calls == 1
    assert "www.adzuna.in" in budget.cooldown_until
    state = json.loads((tmp_path / "retrieval_state.json").read_text(encoding="utf-8"))
    assert "www.adzuna.in" in state["cooldown_until"]


def test_sigma_public_brand_alias_reconciles_same_post_title():
    original = Job(
        source="serp", title="Bengali Linguistic Projects (Remote)", company="Sigma Group",
        url="https://careers.sigma.ai/jobs/4972806-bengali-linguistic-projects-remote",
        description="snippet",
    )
    hydrated = original.model_copy(update={"source": "first_party", "company": "Sigma.AI"})
    assert _exact_identity(original, hydrated, "", {}) == "corroborated"


def test_saved_sigma_response_hydrates_with_real_title_and_brand_when_available():
    state_path = (__import__("pathlib").Path(__file__).parents[1]
                  / "roadmap/v5.5/review/live_20260908_1913/retrieval_state.json")
    if not state_path.exists():
        pytest.skip("local live-review cache is not packaged")
    cache = json.loads(state_path.read_text(encoding="utf-8"))["cache"]
    entry = next((value for key, value in cache.items() if "careers.sigma.ai/jobs/4972806" in key), None)
    if entry is None:
        pytest.skip("Sigma response is absent from local live-review cache")
    url = "https://careers.sigma.ai/jobs/4972806-bengali-linguistic-projects-remote"
    from jobhound.v41.hydration import _generic_job, _jobposting_payload
    payload = _jobposting_payload(entry["body"])
    assert payload is not None
    parsed = _generic_job(payload, url, entry["body"])
    assert parsed is not None
    original = parsed.model_copy(update={"source": "serp", "company": "Sigma Group"})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=entry["body"], request=request))) as client:
            return await hydrate_public_posting(
                url, original, client=client, budget=RetrievalBudget(RetrievalPolicy(max_retries=0)))

    outcome = asyncio.run(exercise())
    assert outcome.job is not None and outcome.job.title == parsed.title
    assert outcome.identity_state == "corroborated"


def test_hydration_stage_outcomes_reconcile_including_unaccepted_and_conflict(monkeypatch):
    import jobhound.v41.resolve as resolver
    from jobhound.v41.hydration import HydrationResult
    from datetime import datetime, timezone
    raw = [{"source": "serp", "raw": {
        "title": f"Bengali evaluator {index}",
        "link": f"https://jobs.ashbyhq.com/acme/{index}",
        "snippet": "Bengali language evaluation role",
    }} for index in range(3)]
    old = CONFIG.v55.enabled
    CONFIG.v55.enabled = True
    try:
        result = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))

        async def outcome(url, original, **_kwargs):
            index = url.rsplit("/", 1)[-1]
            if index == "0":
                return HydrationResult(state="complete", job=original.model_copy(), source="ashby",
                                       public_url=url, request_url=url, identity_state="exact")
            if index == "1":
                return HydrationResult(state="complete", job=original.model_copy(), source="ashby",
                                       public_url=url, request_url=url, identity_state="uncertain")
            return HydrationResult(state="partial", source="ashby", public_url=url,
                                   request_url=url, identity_state="conflict", error="incomplete")

        monkeypatch.setattr(resolver, "hydrate_public_posting", outcome)
        report = asyncio.run(resolver.hydrate_result(
            result, policy=RetrievalPolicy(shortlist=3, request_budget=3)))
    finally:
        CONFIG.v55.enabled = old
    stage = report["stage_health"]
    assert stage["attempted"] == stage["succeeded"] + stage["failed"] + stage["deferred"]
    assert stage["succeeded"] == 1 and stage["failed"] == 2
    assert stage["error_codes"] == {"no_accepted_observation": 1, "identity_conflict": 1}


@pytest.mark.parametrize("url", [
    "https://example.com/jobs/4972806-bengali-linguistic-projects-remote",
    "https://careers.sigma.ai/jobs/a-different-requisition",
])
def test_sigma_brand_alias_does_not_cross_host_or_leaf(url):
    original = Job(source="serp", title="Bengali Linguistic Projects (Remote)", company="Sigma Group",
                   url="https://careers.sigma.ai/jobs/4972806-bengali-linguistic-projects-remote",
                   description="snippet")
    hydrated = original.model_copy(update={"company": "Sigma.AI", "url": url})
    assert _exact_identity(original, hydrated, "", {}) == "conflict"


def test_different_company_remains_identity_conflict():
    original = Job(source="serp", title="Bengali Linguist", company="Sigma Group",
                   url="https://careers.sigma.ai/jobs/1", description="snippet")
    hydrated = original.model_copy(update={"company": "Unrelated Employer"})
    assert _exact_identity(original, hydrated, "", {}) == "conflict"
