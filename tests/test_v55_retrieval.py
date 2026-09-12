from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import json

from jobhound.models import Job
from jobhound.v41.hydration import (
    RetrievalBudget,
    RetrievalPolicy,
    child_observation,
    hydrate_public_posting,
)
from jobhound.v41.models import ListingObservation
from jobhound.v41.engine import decision_fingerprint, evaluate_raw
from jobhound.v41.replay import replay, write_snapshot
from jobhound.v41.review import replay_review, _namespace


def _job(url: str) -> Job:
    return Job(source="aggregator", title="Bengali AI Evaluator", company="Acme", url=url)


def test_exact_greenhouse_hydration_is_new_child_with_independent_states():
    url = "https://boards.greenhouse.io/acme/jobs/12345"
    payload = {
        "id": 12345,
        "title": "Bengali AI Evaluator",
        "absolute_url": url,
        "content": "Review Bengali AI output. Requirements: fluent Bengali and English.",
        "location": {"name": "Remote"},
        "first_published": "2026-09-01T00:00:00Z",
        "_company": "Acme",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/boards/acme/jobs/12345"
        return httpx.Response(200, json=payload)

    async def run():
        policy = RetrievalPolicy(request_budget=2)
        budget = RetrievalBudget(policy)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            outcome = await hydrate_public_posting(url, _job(url), client=client, budget=budget)
        parent = ListingObservation(observation_id="discovery:1", source="adzuna", job=_job(url), original_url=url)
        return outcome, child_observation(parent, outcome), parent

    outcome, child, parent = asyncio.run(run())
    assert outcome.state == "complete"
    assert outcome.identity_state == "exact"
    assert child.observation_id != parent.observation_id
    assert child.origin == "hydration"
    assert child.parent_observation_ids == [parent.observation_id]
    assert child.content_state == "complete"
    assert child.identity_state == "exact"
    assert child.vacancy_state == "verified_open"
    assert child.application_route_state == "observed_public_route"
    assert parent.origin == "discovery"


def test_retry_after_http_date_opens_host_cooldown_without_sleeping():
    budget = RetrievalBudget(RetrievalPolicy())
    delay = budget.note_retry_after(
        "https://api.lever.co/v0/postings/acme/id",
        "Wed, 09 Sep 2026 12:00:00 GMT",
    )
    # The exact delay depends on the wall clock; parsing either form is the contract.
    assert delay >= 0
    assert "api.lever.co" in budget.cooldown_until


def test_request_budget_counts_each_actual_api_request():
    calls = 0
    url = "https://boards.greenhouse.io/acme/jobs/12345"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def run():
        budget = RetrievalBudget(RetrievalPolicy(request_budget=1, max_retries=0))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await hydrate_public_posting(url, _job(url), client=client, budget=budget)
            second = await hydrate_public_posting(url, _job(url), client=client, budget=budget)
        return first, second, budget

    first, second, budget = asyncio.run(run())
    assert first.error == "http_503"
    assert second.state == "deferred"
    assert budget.requests == calls == 1


def test_identity_conflict_is_not_accepted():
    url = "https://boards.greenhouse.io/acme/jobs/12345"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": 99999,
            "title": "Senior Surgeon",
            "absolute_url": url,
            "content": "Medical licence required",
            "location": {"name": "Remote"},
            "_company": "Other Company",
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await hydrate_public_posting(url, _job(url), client=client, budget=RetrievalBudget(RetrievalPolicy()))

    outcome = asyncio.run(run())
    assert outcome.identity_state == "conflict"


def test_captured_hydration_replays_deterministically(tmp_path):
    url = "https://boards.greenhouse.io/acme/jobs/12345"
    raw = [{"source": "greenhouse", "raw": {
        "id": 12345, "title": "Bengali AI Evaluator", "_company": "Acme",
        "absolute_url": url, "content": "short", "location": {"name": "Remote"},
    }}]
    result = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
    parent = result.evaluated[0].canonical.best_observation
    hydrated = _job(url)
    hydrated.source = "greenhouse"
    hydrated.description = "Review Bengali AI outputs. Fluent Bengali and English required."
    from jobhound.v41.hydration import HydrationResult
    child = child_observation(parent, HydrationResult(
        state="complete", job=hydrated, source="greenhouse", public_url=url,
        payload={"id": 12345, "content": hydrated.description}, identity_state="exact",
    ))
    result.evaluated[0].canonical.observations.append(child)
    result.observations.append(child)
    snapshot = write_snapshot(raw, result, directory=tmp_path)
    first = replay(snapshot)
    second = replay(snapshot)
    assert any(obs.origin == "hydration" for obs in first.observations)
    assert decision_fingerprint(first) == decision_fingerprint(second)


def test_generic_jobposting_identity_and_redirect_to_index():
    url = "https://www.alignerr.com/jobs/bengali-evaluator"
    good = {"@context": "https://schema.org", "@type": "JobPosting",
            "title": "Bengali AI Evaluator", "description": "Review Bengali AI output. Bengali required.",
            "hiringOrganization": {"name": "Alignerr"}, "jobLocation": {"address": {"addressCountry": "India"}}}
    calls = 0
    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text=f'<script type="application/ld+json">{json.dumps(good)}</script>')
        if calls == 2:
            return httpx.Response(302, headers={"location": "/jobs"})
        return httpx.Response(200, text="Jobs index")
    async def run():
        budget = RetrievalBudget(RetrievalPolicy(max_retries=0))
        original = Job(source="serp", title="Bengali AI Evaluator", company="Alignerr", url=url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            accepted = await hydrate_public_posting(url, original, client=client, budget=budget)
            missing = await hydrate_public_posting(url + "-missing", _job(url), client=client, budget=budget)
        return accepted, missing, budget
    accepted, missing, budget = asyncio.run(run())
    assert accepted.identity_state in {"exact", "corroborated"}
    assert accepted.state == "complete"
    assert missing.state == "unavailable" and missing.error == "redirect_to_index"
    assert budget.requests == 3  # page + redirect response + index response


def test_wrong_language_country_and_role_conflict():
    original = _job("https://example.com/jobs/bengali")
    original.location = "India"
    hydrated = Job(source="first_party", title="Dutch Senior Surgeon", company="Acme",
                   url=original.url, location="Netherlands", description="Dutch required")
    from jobhound.v41.hydration import _exact_identity
    assert _exact_identity(original, hydrated, "", {}) == "conflict"


def test_offline_review_fail_traps_external_calls(tmp_path, monkeypatch):
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Reviewer", "_company": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "Review AI output."}}]
    result = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
    snapshot = write_snapshot(raw, result, directory=tmp_path / "capture")
    def forbidden(*args, **kwargs):
        raise AssertionError("external side effect attempted")
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    output, _ = replay_review(snapshot, tmp_path / "replay")
    assert output.exists()


def test_review_namespace_rejects_production_data_path():
    import pytest
    with pytest.raises(ValueError):
        _namespace(__import__("pathlib").Path(__file__).resolve().parents[1] / "data" / "review")


def test_live_review_capture_is_scoped_and_excludes_imap(tmp_path, monkeypatch):
    import jobhound.v41.review as review
    from jobhound.config import CONFIG
    seen = {}
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Reviewer", "_company": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "Review AI output."}}]
    async def fake_ingest(*, limit, only, transport, exclude):
        seen["exclude"] = exclude; seen["enabled"] = CONFIG.v55.enabled
        return raw, []
    async def fake_hydrate(result, **kwargs):
        seen["namespace"] = kwargs["review_namespace"]
        return {"changed": 0}
    monkeypatch.setattr(review, "ingest_raw_with_health", fake_ingest)
    monkeypatch.setattr(review, "hydrate_result", fake_hydrate)
    previous = CONFIG.v55.enabled
    snapshot, digest = asyncio.run(review.capture_review(tmp_path / "isolated"))
    assert snapshot.exists() and digest.exists()
    assert (tmp_path / "isolated" / "audit.json").exists()
    assert seen["enabled"] is True and CONFIG.v55.enabled is previous
    assert seen["exclude"] == {"email_offers"}
    assert seen["namespace"] == (tmp_path / "isolated").resolve()


def test_cooldown_and_cache_persist_only_with_explicit_review_namespace(tmp_path):
    plain = RetrievalBudget(RetrievalPolicy())
    plain.note_retry_after("https://api.lever.co/x", "60")
    plain.persist()
    assert not (tmp_path / "retrieval_state.json").exists()
    scoped = RetrievalBudget(RetrievalPolicy(), review_namespace=tmp_path)
    scoped.note_retry_after("https://api.lever.co/x", "60")
    scoped.cache["https://api.lever.co/x"] = {"status": 200, "body": "{}"}
    scoped.persist()
    restored = RetrievalBudget(RetrievalPolicy(), review_namespace=tmp_path)
    assert "api.lever.co" in restored.cooldown_until
    assert "https://api.lever.co/x" in restored.cache


def test_url_safety_rejects_userinfo_ports_and_private_literals():
    from jobhound.v41.hydration import _safe_public_http
    assert not _safe_public_http("http://127.0.0.1/job")
    assert not _safe_public_http("https://user:pass@example.com/job")
    assert not _safe_public_http("https://example.com:8443/job")
    assert _safe_public_http("https://example.com/job")


def test_expired_review_cache_is_not_reused(tmp_path):
    state = {"cooldown_until": {}, "cache": {"https://example.com/job": {
        "status": 200, "body": "stale", "final_url": "https://example.com/job",
        "trail": ["https://example.com/job"], "captured_at": "2020-01-01T00:00:00+00:00"}}}
    (tmp_path / "retrieval_state.json").write_text(json.dumps(state), encoding="utf-8")
    calls = 0
    body = '<script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "title": "Bengali AI Evaluator",
        "description": "Bengali review", "hiringOrganization": {"name": "Acme"}}) + '</script>'
    def handler(request):
        nonlocal calls; calls += 1
        return httpx.Response(200, text=body)
    async def run():
        budget = RetrievalBudget(RetrievalPolicy(cache_ttl_hours=1), review_namespace=tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await hydrate_public_posting("https://example.com/job", _job("https://example.com/job"), client=client, budget=budget)
    asyncio.run(run())
    assert calls == 1


def test_nested_snapshot_urls_remove_private_adzuna_click_parameters():
    from jobhound.v41.provenance import sanitize_payload
    value = sanitize_payload({"outer": {"links": [
        "https://www.adzuna.in/details/1?se=private&v=secret&utm_source=x&keep=yes"
    ]}})
    url = value["outer"]["links"][0]
    assert "se=" not in url and "v=" not in url and "utm_" not in url
    assert "keep=yes" in url


def test_lever_named_lists_are_retained_in_hydrated_content():
    url = "https://jobs.eu.lever.co/acme/abc"
    payload = {"id": "abc", "text": "Bengali Evaluator", "hostedUrl": url,
               "descriptionPlain": "Review AI output.", "categories": {"location": "Remote"},
               "lists": [{"text": "Requirements", "content": "Native German required."}],
               "_company": "Acme"}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
            return await hydrate_public_posting(url, _job(url), client=client,
                                                budget=RetrievalBudget(RetrievalPolicy(max_retries=0)))
    outcome = asyncio.run(run())
    assert outcome.job is not None
    assert "Requirements" in outcome.job.description and "Native German required" in outcome.job.description


def test_leaf_discovery_and_model_claim_validation_are_evidence_bound():
    from jobhound.v41.hydration import accepted_model_claim_count, discover_public_leaves
    parent = "https://example.com/jobs/"
    html = '<a href="/jobs/bengali-01">Bengali</a><a href="https://evil.example/jobs/x">wrong host</a>'
    async def run():
        budget = RetrievalBudget(RetrievalPolicy(request_budget=1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))) as client:
            return await discover_public_leaves(client, budget, parent)
    assert asyncio.run(run()) == ["https://example.com/jobs/bengali-01"]
    assert accepted_model_claim_count("obs", "German required", {"output_id": "wrong", "claim_span": "German required"}) == 0
    assert accepted_model_claim_count("obs", "German required", {"output_id": "obs", "claim_span": "invented"}) == 0
    assert accepted_model_claim_count("obs", "German required", {"output_id": "obs", "claim_span": "German required"}) == 1


def test_store_preserves_hydration_diagnostics(tmp_path):
    from jobhound.v41.hydration import HydrationResult
    from jobhound.v41.store import V41Store
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Reviewer", "_company": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "short"}}]
    result = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
    parent = result.evaluated[0].canonical.best_observation
    hydrated = parent.job.model_copy(deep=True); hydrated.description = "Complete role requirements"
    child = child_observation(parent, HydrationResult(
        state="complete", job=hydrated, source="greenhouse",
        public_url=parent.normalized_url, payload={"id": 1, "content": hydrated.description},
        identity_state="exact", captured_at=datetime(2026, 9, 8, 8, tzinfo=timezone.utc),
        vacancy_state="verified_open", application_route_state="observed_public_route",
        task_availability_state="advertised_only"))
    result.observations.append(child)
    result.evaluated[0].canonical.observations.append(child)
    store = V41Store(tmp_path / "audit.db")
    store.record_run(result)
    encoded = store.conn.execute(
        "SELECT resolution_json FROM observations WHERE observation_id=?", (child.observation_id,)
    ).fetchone()[0]
    store.close()
    audit = json.loads(encoded)
    assert audit["origin"] == "hydration"
    assert audit["parent_observation_ids"] == [parent.observation_id]
    assert audit["captured_at"] == "2026-09-08T08:00:00+00:00"
    assert audit["content_state"] == "complete" and audit["identity_state"] == "exact"
    assert audit["vacancy_state"] == "verified_open"
    assert audit["application_route_state"] == "observed_public_route"
    assert audit["task_availability_state"] == "advertised_only"
    assert audit["content_hash"] and audit["extraction_version"]


def test_generic_cross_host_redirect_never_acquires_original_authority():
    url = "https://example.com/jobs/bengali"
    body = '<script type="application/ld+json">{"@type":"JobPosting","title":"Bengali AI Evaluator","description":"Bengali required","hiringOrganization":{"name":"Acme"}}</script>'
    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://other.example/jobs/bengali"})
        return httpx.Response(200, text=body)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await hydrate_public_posting(url, _job(url), client=client,
                                                budget=RetrievalBudget(RetrievalPolicy(max_retries=0)))
    outcome = asyncio.run(run())
    assert outcome.state == "unavailable"
    assert outcome.error == "redirect_identity_boundary"


def test_accepted_hydration_lineage_is_one_canonical_and_drives_requirements():
    from jobhound.config import CONFIG
    from jobhound.v41.engine import evaluate_observations
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Bengali Evaluator", "_company": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "short"}}]
    initial = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
    parent = initial.evaluated[0].canonical.best_observation
    hydrated = Job(source="lever", title="Bengali Evaluator", company="Acme",
                   url="https://jobs.eu.lever.co/acme/other-id",
                   description="Review Bengali output. Native German required.")
    child = child_observation(parent, __import__("jobhound.v41.hydration", fromlist=["HydrationResult"]).HydrationResult(
        state="complete", job=hydrated, source="lever", public_url=hydrated.url,
        payload={"id": "other-id", "description": hydrated.description}, identity_state="exact"))
    old = CONFIG.v55.enabled; CONFIG.v55.enabled = True
    try:
        result = evaluate_observations([parent, child], as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
    finally:
        CONFIG.v55.enabled = old
    assert len(result.evaluated) == 1
    assert "german" in result.evaluated[0].assessment.requirements_assessment.language_required


def test_adzuna_interstitial_to_exact_ats_uses_one_budget_and_new_child(tmp_path):
    from jobhound.config import CONFIG
    from jobhound.v41.resolve import hydrate_result
    adzuna = "https://www.adzuna.in/details/1?se=private&v=secret"
    ats = "https://boards.greenhouse.io/acme/jobs/12345"
    raw = [{"source": "adzuna", "raw": {"title": "Bengali AI Evaluator",
            "company": {"display_name": "Acme"}, "redirect_url": adzuna,
            "description": "Bengali AI review", "location": {"display_name": "Remote"}}}]
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "www.adzuna.in":
            return httpx.Response(200, text=f'<a href="{ats}">original role</a>')
        return httpx.Response(200, json={"id": 12345, "title": "Bengali AI Evaluator",
            "_company": "Acme", "absolute_url": ats,
            "content": "Review Bengali AI output. Bengali and English required.",
            "location": {"name": "Remote"}})
    old = CONFIG.v55.enabled; CONFIG.v55.enabled = True
    try:
        result = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
        original = result.observations[0].model_copy(deep=True)
        report = asyncio.run(hydrate_result(result, policy=RetrievalPolicy(request_budget=2, max_retries=0),
                                            transport=httpx.MockTransport(handler)))
        snapshot = write_snapshot(raw, result, directory=tmp_path)
        replayed = replay(snapshot)
    finally:
        CONFIG.v55.enabled = old
    children = [obs for obs in result.observations if obs.origin == "hydration"]
    assert report["requests"] == len(calls) == 2
    assert len(children) == 1 and children[0].parent_observation_ids == [original.observation_id]
    assert result.observations[0].original_url == original.original_url
    assert children[0].identity_state == "exact" and children[0].vacancy_state == "verified_open"
    assert "se=" not in report["attempts"][0]["redirect_trail"][0]
    assert decision_fingerprint(replayed) == decision_fingerprint(result)
    assert replayed.evaluated[0].canonical.canonical_id == result.evaluated[0].canonical.canonical_id


def test_adzuna_index_or_multiple_originals_are_not_accepted():
    from jobhound.v41.hydration import resolve_public_original
    adzuna = "https://www.adzuna.in/details/1"
    async def run(body):
        budget = RetrievalBudget(RetrievalPolicy(request_budget=1, max_retries=0))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))) as client:
            return await resolve_public_original(client, budget, adzuna)
    resolved, error, _ = asyncio.run(run('<a href="https://boards.greenhouse.io/acme/jobs">index</a>'))
    assert resolved is None and error == "original_target_missing"
    body = ('<a href="https://boards.greenhouse.io/acme/jobs/1">one</a>'
            '<a href="https://jobs.lever.co/acme/2">different role</a>')
    resolved, error, _ = asyncio.run(run(body))
    assert resolved is None and error == "original_target_ambiguous"


def test_generic_jobposting_content_does_not_invent_open_or_apply_state():
    url = "https://example.com/jobs/bengali"
    payload = {"@type": "JobPosting", "title": "Bengali AI Evaluator",
               "description": "Bengali required", "hiringOrganization": {"name": "Acme"}}
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text=f'<script type="application/ld+json">{json.dumps(payload)}</script>'))) as client:
            return await hydrate_public_posting(url, _job(url), client=client,
                                                budget=RetrievalBudget(RetrievalPolicy(max_retries=0)))
    outcome = asyncio.run(run()); child = child_observation(
        ListingObservation(observation_id="p", source="serp", job=_job(url)), outcome)
    assert outcome.state == "complete"
    # A self-asserted hiringOrganization on an unknown domain is not verified
    # employer provenance merely because it is valid JSON-LD.
    assert child.source_kind.value == "unknown"
    assert child.vacancy_state == "unknown"
    assert child.application_route_state == "account_unknown"
    assert child.verified_open_at is None


def test_rc1_snapshot_freezes_policy_and_account_state_then_restores_current(tmp_path):
    from jobhound.config import CONFIG
    from jobhound.v41.engine import evaluate_observations
    from jobhound.v41.replay import load_snapshot
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Bengali Evaluator", "_company": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "content": "Review Bengali output. Bengali required.", "location": {"name": "Remote"}}}]
    old_enabled, old_cap = CONFIG.v55.enabled, CONFIG.v55.max_verify_actions
    CONFIG.v55.enabled = True; CONFIG.v55.max_verify_actions = 1
    try:
        seed = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
        parent = seed.observations[0]
        account = ListingObservation(
            observation_id="account:1", source="account_state", origin="account_state",
            parent_observation_ids=[parent.observation_id], captured_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
            raw_payload={"project_access": "blocked", "applicable_block": True, "tasks_allocated": False})
        captured = evaluate_observations([parent, account], as_of=datetime(2026, 9, 8, tzinfo=timezone.utc), raw_count=1)
        snapshot = write_snapshot(raw, captured, directory=tmp_path)
        header, _ = load_snapshot(snapshot)
        assert header["release_policy"]["max_verify_actions"] == 1
        assert len(header["account_state_observations"]) == 1
        CONFIG.v55.max_verify_actions = 7
        restored = replay(snapshot)
        assert CONFIG.v55.max_verify_actions == 7
        assert decision_fingerprint(restored) == decision_fingerprint(captured)
        assert any(obs.origin == "account_state" for obs in restored.observations)
        assert restored.evaluated[0].assessment.account_state.get("project_access") == "blocked"
    finally:
        CONFIG.v55.enabled = old_enabled; CONFIG.v55.max_verify_actions = old_cap


def test_isolated_failed_verification_cycles_route_to_watch_once_per_run(tmp_path):
    from jobhound.config import CONFIG
    from jobhound.v41.resolve import hydrate_result
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Bengali Evaluator", "_company": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "short"}}]
    async def attempt():
        result = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
        report = await hydrate_result(result, policy=RetrievalPolicy(request_budget=1, max_retries=0),
                                      transport=httpx.MockTransport(lambda r: httpx.Response(503)),
                                      review_namespace=tmp_path)
        return result, report
    old = CONFIG.v55.enabled; CONFIG.v55.enabled = True
    try:
        first_result, first = asyncio.run(attempt()); second_result, second = asyncio.run(attempt())
        third_result, third = asyncio.run(attempt())
    finally:
        CONFIG.v55.enabled = old
    assert first["attempts"][0]["verification_cycles"] == 1
    assert first["attempts"][0]["route"] == "verify"
    assert second["attempts"][0]["verification_cycles"] == 2
    assert second["attempts"][0]["route"] == "watch"
    assert second["attempts"][0]["automatic_retry_allowed"] is False
    assert second_result.evaluated[0].decision.lifecycle == "watch"
    assert third["requests"] == 0 and third["attempts"] == []
    assert third_result.evaluated[0].assessment.account_state["automatic_verification_cycles"] == 2
    assert third_result.evaluated[0].decision.lifecycle == "watch"

    other = [{"source": "greenhouse", "raw": {"id": 2, "title": "Hindi Evaluator", "_company": "Other",
              "absolute_url": "https://boards.greenhouse.io/other/jobs/2", "content": "short"}}]
    calls = 0
    def other_handler(request):
        nonlocal calls; calls += 1
        return httpx.Response(503)
    old = CONFIG.v55.enabled; CONFIG.v55.enabled = True
    try:
        unrelated = evaluate_raw(other, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
        asyncio.run(hydrate_result(unrelated, policy=RetrievalPolicy(request_budget=1, max_retries=0),
                                   transport=httpx.MockTransport(other_handler), review_namespace=tmp_path))
    finally:
        CONFIG.v55.enabled = old
    assert calls == 1
