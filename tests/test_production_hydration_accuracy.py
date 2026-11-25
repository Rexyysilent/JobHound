"""Production-derived accuracy cases, using fictional postings and clocks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41 import hydration, resolve
from jobhound.v41.engine import decision_fingerprint, evaluate_observations, evaluate_raw
from jobhound.v41.hydration import HydrationResult, RetrievalBudget, RetrievalPolicy
from jobhound.v41.models import ListingObservation, SourceKind
from jobhound.v41.posting_deadlines import deadline_timestamp, has_elapsed_deadline
from jobhound.v41.replay import replay, write_snapshot


NOW = datetime(2026, 11, 25, 9, tzinfo=timezone.utc)
TITLE = "Bengali AI Evaluator"
CONTENT = ("Review Bengali AI model outputs. This role is fully remote in India. "
           "Requirements: fluent Bengali and English. No degree or experience required.")


@pytest.fixture(autouse=True)
def release_policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)


def freeze(monkeypatch, module, value):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is not None else value.replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", Clock)


def discovery(url, *, title=TITLE):
    return {"source": "serp", "raw": {"link": url, "title": title,
            "snippet": "Review Bengali AI outputs. Remote India opportunity."}}


def greenhouse(url, *, identifier=123, company="Acme", title=TITLE, **extra):
    return {"id": identifier, "title": title, "company_name": company,
            "absolute_url": url, "content": CONTENT,
            "location": {"name": "India (Remote)"},
            "first_published": NOW.isoformat(), **extra}


def run_hydration(raw, handler, *, policy=None, namespace=None):
    result = evaluate_raw(raw, as_of=NOW)
    report = asyncio.run(resolve.hydrate_result(
        result, policy=policy or RetrievalPolicy(max_retries=0),
        transport=httpx.MockTransport(handler), review_namespace=namespace))
    return result, report


@pytest.mark.parametrize("host", [
    "boards.greenhouse.io", "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
])
def test_exact_regional_greenhouse_becomes_original_in_same_run(monkeypatch, host):
    freeze(monkeypatch, hydration, NOW + timedelta(seconds=10))
    freeze(monkeypatch, resolve, NOW + timedelta(seconds=11))
    url = f"https://{host}/fictionalboard/jobs/123"
    raw = [discovery(url)]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.host == "boards-api.greenhouse.io"
        assert request.url.path == "/v1/boards/fictionalboard/jobs/123"
        return httpx.Response(200, json=greenhouse(url, company="Fictional Company Limited"))

    result, report = run_hydration(raw, handler)
    item = result.evaluated[0]
    assert len(calls) == report["requests"] == report["changed"] == 1
    assert item.assessment.source_actionability == SourceKind.ORIGINAL_ATS
    assert item.job.company == "Fictional Company Limited"
    assert item.assessment.verified_open_at == NOW + timedelta(seconds=10)
    assert result.metadata.as_of == NOW + timedelta(seconds=11)
    assert item.assessment.next_action == "apply", item.assessment.verification_tasks
    assert not item.assessment.blockers
    assert result.observations[0].source == "serp"
    assert result.observations[0].captured_at == NOW
    assert result.observations[1].parent_observation_ids == [result.observations[0].observation_id]


def test_same_run_clock_and_discovery_times_survive_snapshot_replay(monkeypatch, tmp_path):
    freeze(monkeypatch, hydration, NOW + timedelta(seconds=10))
    freeze(monkeypatch, resolve, NOW + timedelta(seconds=11))
    url = "https://job-boards.eu.greenhouse.io/acme/jobs/123"
    other_url = "https://job-boards.greenhouse.io/acme/jobs/124"
    raw = [discovery(url), {"source": "greenhouse", "raw": greenhouse(
        other_url, identifier=124, title="Hindi AI Evaluator",
        content=CONTENT + " " + "Review the assigned language samples carefully. " * 30)}]
    result, report = run_hydration(raw, lambda request: httpx.Response(200, json=greenhouse(url)))
    assert report["changed"] == 1
    original_run_id = result.metadata.run_id
    saved = write_snapshot(raw, result, directory=tmp_path)
    restored = replay(saved)
    assert result.metadata.run_id == original_run_id
    assert restored.metadata.as_of == result.metadata.as_of == NOW + timedelta(seconds=11)
    original_times = {o.observation_id: (o.captured_at, o.verified_open_at)
                      for o in result.observations if o.origin == "discovery"}
    assert {o.observation_id: (o.captured_at, o.verified_open_at)
            for o in restored.observations if o.origin == "discovery"} == original_times
    assert decision_fingerprint(restored) == decision_fingerprint(result)


def test_future_dated_hydration_cannot_advance_clock_or_become_apply(monkeypatch):
    freeze(monkeypatch, resolve, NOW + timedelta(seconds=11))
    url = "https://job-boards.greenhouse.io/acme/jobs/123"

    async def future(*args, **kwargs):
        return HydrationResult(state="complete", source="greenhouse", public_url=url,
            job=Job(source="greenhouse", title=TITLE, company="Acme", url=url,
                    description=CONTENT, location="India (Remote)", is_remote=True),
            payload=greenhouse(url), captured_at=NOW + timedelta(days=1),
            identity_state="exact", vacancy_state="verified_open",
            application_route_state="observed_public_route")

    monkeypatch.setattr(resolve, "hydrate_public_posting", future)
    result, _ = run_hydration([discovery(url)], lambda request: httpx.Response(500))
    assert result.metadata.as_of == NOW
    assert result.evaluated[0].assessment.verified_open_at is None
    assert result.evaluated[0].assessment.next_action != "apply"


@pytest.mark.parametrize("provider", ["serp", "jsearch"])
def test_explicit_conflicting_employer_is_not_replaced_by_matching_title(provider):
    url = "https://job-boards.eu.greenhouse.io/acme/jobs/123"
    original = Job(source=provider, title=TITLE, company="Different Employer", url=url)
    hydrated = Job(source="greenhouse", title=TITLE, company="Acme", url=url)
    assert hydration._exact_identity(original, hydrated, "123", {"id": 123}) == "conflict"


def test_different_native_requisition_is_not_corroborated_by_same_title():
    url = "https://job-boards.eu.greenhouse.io/acme/jobs/123"
    original = Job(source="serp", title=TITLE, company="Acme", url=url)
    assert hydration._exact_identity(original, original, "123", {"id": 999}) == "conflict"


@pytest.mark.parametrize("tail", ["", "/jobs", "/jobs/123/another-page"])
def test_greenhouse_indexes_and_extra_paths_are_not_exact_api_routes(tail):
    assert hydration.native_posting_route("https://job-boards.eu.greenhouse.io/acme" + tail) is None


def test_adzuna_exact_eu_link_uses_same_conservative_original_resolution():
    url = "https://job-boards.eu.greenhouse.io/acme/jobs/123"

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=f'<a href="{url}">Apply</a>'))) as client:
            return await hydration.resolve_public_original(client, RetrievalBudget(RetrievalPolicy()),
                                                            "https://www.adzuna.in/details/123")
    original, error, trail = asyncio.run(run())
    assert original == url and error is None and trail[-1] == url


@pytest.mark.parametrize("value,elapsed", [
    ("2026-11-24", True), ("2026-11-25", False), ("2026-11-26", False),
    ("2026-11-25T08:59:00", True), ("2026-11-25T09:00:00Z", True),
    ("2026-11-25T09:01:00Z", False), ("2026-11-25T10:00:00+02:00", True),
    ("not a date", False), (None, False), (False, False), ("9999-12-31", False),
])
def test_deadline_precision_and_timezone(value, elapsed):
    assert has_elapsed_deadline({"validThrough": value}, NOW) is elapsed


def test_date_only_deadline_is_valid_through_the_whole_stated_day():
    assert deadline_timestamp("2026-11-25") == datetime(2026, 11, 26, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["2026-11-24", "2026-11-25T08:00:00", "2026-11-25T08:00:00Z"])
def test_generic_expired_jobposting_is_closed_during_hydration(monkeypatch, value):
    freeze(monkeypatch, hydration, NOW)
    url = "https://careers.example.org/jobs/bengali"
    payload = {"@type": "JobPosting", "title": TITLE, "description": CONTENT,
               "hiringOrganization": {"name": "Acme"}, "validThrough": value}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, text='<script type="application/ld+json">' + json.dumps(payload) + '</script>'))) as client:
            return await hydration.hydrate_public_posting(url,
                Job(source="aggregator", title=TITLE, company="Acme", url=url),
                client=client, budget=RetrievalBudget(RetrievalPolicy()))
    outcome = asyncio.run(run())
    assert outcome.identity_state == "corroborated"
    assert outcome.vacancy_state == "explicitly_closed"


def test_frozen_jobposting_deadline_is_rechecked_without_another_fetch():
    url = "https://careers.example.org/jobs/bengali"
    observation = ListingObservation(observation_id="exact", source="first_party", origin="hydration",
        job=Job(source="first_party", title=TITLE, company="Acme", url=url, description=CONTENT,
                location="India (Remote)", is_remote=True), original_url=url,
        source_kind=SourceKind.ORIGINAL_EMPLOYER, content_state="complete", identity_state="exact",
        captured_at=NOW, verified_open_at=NOW, vacancy_state="verified_open",
        application_route_state="observed_public_route", raw_payload={"validThrough": "2026-11-25"})
    result = evaluate_observations([observation], as_of=NOW + timedelta(days=1), raw_count=0)
    assert result.evaluated[0].assessment.lifecycle == "closed"
    assert result.evaluated[0].assessment.next_action != "apply"


def test_native_greenhouse_deadline_and_open_evidence_agree():
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    result = evaluate_raw([{"source": "greenhouse", "raw": greenhouse(
        url, application_deadline="2026-11-24")}], as_of=NOW)
    assert result.observations[0].vacancy_state == "explicitly_closed"
    assert result.observations[0].verified_open_at is None
    assert result.evaluated[0].assessment.lifecycle == "closed"


def test_deadline_crossed_while_remaining_hydration_finishes_is_closed(monkeypatch):
    freeze(monkeypatch, hydration, NOW + timedelta(seconds=10))
    freeze(monkeypatch, resolve, NOW + timedelta(seconds=11))
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    result, _ = run_hydration([discovery(url)], lambda request: httpx.Response(
        200, json=greenhouse(url, application_deadline=(NOW + timedelta(seconds=10.5)).isoformat())))
    assert result.metadata.as_of == NOW + timedelta(seconds=11)
    assert result.evaluated[0].assessment.lifecycle == "closed"
    assert result.evaluated[0].assessment.next_action != "apply"


@pytest.mark.parametrize("location", ["Remote", "Work from home"])
def test_remote_label_does_not_prove_applicant_country(location):
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    content = CONTENT.replace("This role is fully remote in India.", "This role is fully remote.")
    result = evaluate_raw([{"source": "greenhouse", "raw": greenhouse(
        url, content=content, location={"name": location})}], as_of=NOW)
    assessment = result.evaluated[0].assessment
    assert assessment.next_action == "verify"
    assert not assessment.blockers
    assert "location_scope_unknown" in assessment.unresolved
    assert assessment.eligibility.value == "unknown"
    assert assessment.evidence_dimensions["eligibility"].state == "unknown"


@pytest.mark.parametrize("location", ["India (Remote)", "Worldwide", "APAC"])
def test_explicit_accessible_applicant_regions_remain_application_ready(location):
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    content = CONTENT.replace("This role is fully remote in India.", "This role is fully remote.")
    result = evaluate_raw([{"source": "greenhouse", "raw": greenhouse(
        url, content=content, location={"name": location})}], as_of=NOW)
    assessment = result.evaluated[0].assessment
    assert assessment.next_action == "apply", assessment.verification_tasks
    assert not assessment.blockers


@pytest.mark.parametrize("remote", ["TELECOMMUTE", ["TELECOMMUTE"]])
def test_remote_jobposting_uses_applicant_geography_not_company_office(remote):
    payload = {"title": TITLE, "description": CONTENT, "hiringOrganization": {"name": "Acme"},
               "jobLocationType": remote,
               "jobLocation": {"address": {"addressCountry": "United States"}},
               "applicantLocationRequirements": {"@type": "Country", "name": "India"}}
    job = hydration._generic_job(payload, "https://careers.example.org/jobs/bengali", "")
    assert job.is_remote is True
    assert job.location == "India"


def test_budget_deferrals_do_not_exhaust_unattempted_opportunities(monkeypatch, tmp_path):
    freeze(monkeypatch, hydration, NOW)
    freeze(monkeypatch, resolve, NOW)
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    raw = [discovery(url)]
    sent = []
    for _ in range(3):
        result, report = run_hydration(raw, lambda request: sent.append(request),
            policy=RetrievalPolicy(request_budget=0, max_retries=0), namespace=tmp_path)
        assert report["stage_health"]["deferred"] == 1
        assert report["attempts"][0]["verification_cycles"] == 0
        assert not result.evaluated[0].assessment.account_state.get("automatic_verification_cycles")
    assert not sent
    result, report = run_hydration(raw, lambda request: httpx.Response(200, json=greenhouse(url)),
        policy=RetrievalPolicy(request_budget=1, max_retries=0), namespace=tmp_path)
    assert report["changed"] == 1 and result.evaluated[0].assessment.next_action == "apply"


def test_exact_native_leaf_gets_first_verification_slot_within_discovery_source(monkeypatch):
    freeze(monkeypatch, hydration, NOW)
    freeze(monkeypatch, resolve, NOW)
    native = "https://job-boards.eu.greenhouse.io/acme/jobs/123"
    raw = [discovery("https://copied-jobs.example.org/jobs/bengali"), discovery(native)]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=greenhouse(native))

    result, report = run_hydration(raw, handler, policy=RetrievalPolicy(shortlist=1, request_budget=1))
    assert report["changed"] == 1 and len(calls) == 1
    assert "boards-api.greenhouse.io" in calls[0]
    assert len(result.evaluated) == 2


def test_native_priority_survives_cross_source_round_robin(monkeypatch):
    freeze(monkeypatch, hydration, NOW)
    freeze(monkeypatch, resolve, NOW)
    native = "https://job-boards.eu.greenhouse.io/acme/jobs/123"
    raw = [{"source": "adzuna", "raw": {
        "id": "copy", "title": TITLE, "description": "Remote Bengali AI evaluation work in India.",
        "company": {"display_name": "Other Co"}, "location": {"display_name": "India"},
        "redirect_url": "https://www.adzuna.in/details/321"}}, discovery(native)]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=greenhouse(native))

    result, report = run_hydration(raw, handler, policy=RetrievalPolicy(shortlist=1, request_budget=1))
    assert report["changed"] == 1 and len(calls) == 1
    assert "boards-api.greenhouse.io" in calls[0]
    assert len(result.evaluated) == 2


def dated_posting(identifier, captured_at, deadline, *, kind=SourceKind.ORIGINAL_ATS,
                  vacancy_state="verified_open", closure_notice=False):
    url = "https://jobs.lever.co/acme/bengali-123"
    payload = {"validThrough": deadline}
    if closure_notice:
        payload["_closure_evidence"] = {"text": "This job is closed."}
    return ListingObservation(observation_id=identifier, source="lever", source_kind=kind,
        job=Job(source="lever", title=TITLE, company="Acme", url=url, description=CONTENT,
                location="India (Remote)", is_remote=True),
        original_url=url, normalized_url=url, raw_payload=payload, content_state="complete",
        identity_state="exact", captured_at=captured_at, verified_open_at=captured_at,
        vacancy_state=vacancy_state, application_route_state="observed_public_route")


@pytest.mark.parametrize("old_state", ["verified_open", "explicitly_closed"])
def test_new_original_deadline_replaces_historical_expiry_for_same_post(old_state):
    old = dated_posting("old", NOW - timedelta(days=2), "2026-11-24", vacancy_state=old_state)
    new = dated_posting("new", NOW, "2026-12-10")
    result = evaluate_observations([old, new], as_of=NOW, raw_count=2)
    assert len(result.observations) == 2
    assert len(result.evaluated) == 1
    assert result.evaluated[0].assessment.next_action == "apply"
    assert result.evaluated[0].assessment.lifecycle == "active"


def test_newer_copy_cannot_extend_original_deadline():
    old = dated_posting("original", NOW - timedelta(days=2), "2026-11-24")
    copy = dated_posting("new-copy", NOW, "2026-12-10", kind=SourceKind.REPUTABLE_BOARD)
    result = evaluate_observations([old, copy], as_of=NOW, raw_count=2)
    assert result.evaluated[0].assessment.lifecycle == "closed"


def test_old_copy_deadline_does_not_close_current_original():
    old = dated_posting("old-copy", NOW - timedelta(days=2), "2026-11-24", kind=SourceKind.REPUTABLE_BOARD)
    new = dated_posting("original", NOW, "2026-12-10")
    result = evaluate_observations([old, new], as_of=NOW, raw_count=2)
    assert result.evaluated[0].assessment.next_action == "apply"


def test_extension_does_not_erase_an_explicit_scoped_closure_notice():
    old = dated_posting("closed", NOW - timedelta(days=2), "2026-11-24",
                        vacancy_state="explicitly_closed", closure_notice=True)
    new = dated_posting("later-deadline", NOW, "2026-12-10")
    result = evaluate_observations([old, new], as_of=NOW, raw_count=2)
    assert result.evaluated[0].assessment.lifecycle == "closed"
    assert "source_quality:explicitly_closed" in result.evaluated[0].assessment.blockers


def test_future_capture_cannot_replace_original_deadline():
    old = dated_posting("old", NOW - timedelta(days=2), "2026-11-24")
    future = dated_posting("future", NOW + timedelta(days=1), "2026-12-10")
    result = evaluate_observations([old, future], as_of=NOW, raw_count=2)
    assert result.evaluated[0].assessment.lifecycle == "closed"


def test_future_original_cannot_erase_a_captured_copy_deadline():
    old = dated_posting("copy", NOW - timedelta(days=2), "2026-11-24", kind=SourceKind.REPUTABLE_BOARD)
    future = dated_posting("future-original", NOW + timedelta(days=1), "2026-12-10")
    result = evaluate_observations([old, future], as_of=NOW, raw_count=2)
    assert result.evaluated[0].assessment.lifecycle == "closed"


def test_future_closure_cannot_close_a_current_original():
    current = dated_posting("current", NOW, "2026-12-10")
    future = dated_posting("future-closure", NOW + timedelta(days=1), "2026-11-24",
                           vacancy_state="explicitly_closed", closure_notice=True)
    result = evaluate_observations([current, future], as_of=NOW, raw_count=2)
    assert result.evaluated[0].assessment.next_action == "apply"
