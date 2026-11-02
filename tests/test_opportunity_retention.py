"""Synthetic source-to-decision checks for distinct posting retention.

These exercise the real V3 orchestrator and default V4.2 engine. Discovery is
stubbed at its raw-record boundary; there are no provider calls or deliveries.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest

from jobhound import pipeline
from jobhound.config import CONFIG
from jobhound.models import VERDICT_REJECTED_LOCATION, VERDICT_SURFACED
from jobhound.v41.engine import decision_fingerprint, evaluate_raw
from jobhound.v41.models import ActionBand
from jobhound.v41.replay import replay, write_snapshot
from jobhound.v41.store import V41Store


NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
POSTING = "https://jobs.ashbyhq.com/example-labs/"
ROLE = (
    "About the Role. Review Bengali and English AI responses through model "
    "evaluation and data annotation. What You Will Do: assess response quality "
    "and write careful feedback. Requirements: fluent Bengali and English, "
    "attention to detail, a reliable computer and connectivity. "
    "No degree or prior experience required. "
)


def _posting(key, *, location="India", pay=None, extra=""):
    geography = (
        "Work remotely from India."
        if location == "India"
        else "Applicants must reside in the United States. Remote within the US only."
    )
    return {
        "source": "ashby",
        "raw": {
            "id": key,
            "title": "Bengali AI Data Evaluator",
            "_company": "Example Labs",
            "jobUrl": POSTING + key,
            "descriptionPlain": ROLE + geography + extra,
            "location": location,
            "isRemote": True,
            "publishedAt": "2026-10-22T09:00:00Z",
            "compensation": {"scrapeableCompensationSalarySummary": pay},
        },
    }


def _legacy_run(raw, monkeypatch):
    async def captured_records(limit, only):
        return copy.deepcopy(raw)

    monkeypatch.setattr(pipeline, "ingest_raw", captured_records)
    return asyncio.run(pipeline.run_pipeline())


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_pipeline_keeps_india_role_beside_richer_us_role(monkeypatch, reverse):
    india = _posting("india-open")
    us = _posting("us-only", location="United States", pay="USD 40 per hour")
    raw = [india, us]
    if reverse:
        raw.reverse()
    jobs, counts = _legacy_run(raw, monkeypatch)
    by_url = {job.url: job for job in jobs}

    assert set(by_url) == {POSTING + "india-open", POSTING + "us-only"}
    assert by_url[POSTING + "india-open"].verdict == VERDICT_SURFACED
    assert by_url[POSTING + "india-open"].pay_raw is None
    assert by_url[POSTING + "us-only"].verdict == VERDICT_REJECTED_LOCATION
    assert counts["raw"] == counts["normalized"] == counts["deduped"] == 2
    assert counts[VERDICT_SURFACED] == counts[VERDICT_REJECTED_LOCATION] == 1
    assert counts["accounted"] == 2 and counts["unaccounted"] == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_default_engine_keeps_india_role_beside_richer_us_role(reverse):
    raw = [
        _posting("india-open"),
        _posting("us-only", location="United States", pay="USD 40 per hour"),
    ]
    if reverse:
        raw.reverse()
    result = evaluate_raw(raw, as_of=NOW)
    by_url = {item.job.url: item for item in result.evaluated}

    assert set(by_url) == {POSTING + "india-open", POSTING + "us-only"}
    india, us = by_url[POSTING + "india-open"], by_url[POSTING + "us-only"]
    assert india.decision.action_band == ActionBand.PRIMARY
    assert india.assessment.selected_pay is None
    assert us.decision.terminal_reason == "reject_location"
    assert result.counts["canonical_jobs"] == 2
    assert result.counts["duplicate_observations_collapsed"] == 0
    assert result.counts["surface_primary"] == result.counts["reject_location"] == 1
    assert result.accounting_ok, result.accounting_errors


@pytest.mark.parametrize("keys", [
    ("requisition-a", "requisition-b"),
    ("vacancy?jobId=one", "vacancy?jobId=two"),
    ("RoleA", "rolea"),
])
def test_legacy_pipeline_keeps_separate_same_title_requisitions(keys, monkeypatch):
    jobs, counts = _legacy_run([_posting(key) for key in keys], monkeypatch)
    assert {job.url for job in jobs} == {POSTING + key for key in keys}
    assert len({job.id for job in jobs}) == 2
    assert all(job.verdict == VERDICT_SURFACED for job in jobs)
    assert counts["accounted"] == counts["deduped"] == 2
    assert counts["unaccounted"] == 0


@pytest.mark.parametrize("keys", [
    ("requisition-a", "requisition-b"),
    ("vacancy?jobId=one", "vacancy?jobId=two"),
    ("RoleA", "rolea"),
])
def test_default_requisitions_have_distinct_persistable_rows(keys, tmp_path):
    result = evaluate_raw([_posting(key) for key in keys], as_of=NOW)
    assert {item.job.url for item in result.evaluated} == {POSTING + key for key in keys}
    assert len({item.canonical.canonical_id for item in result.evaluated}) == 2
    assert all(item.decision.action_band == ActionBand.PRIMARY for item in result.evaluated)
    assert result.accounting_ok, result.accounting_errors

    store = V41Store(tmp_path / "separate-requisitions.db")
    try:
        assert len(store.annotate_transitions(result)) == 2
        store.record_run(result)
        rows = store.conn.execute("SELECT canonical_id, canonical_url FROM run_jobs").fetchall()
        assert len(rows) == 2
        assert {row["canonical_url"] for row in rows} == {POSTING + key for key in keys}
    finally:
        store.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_requirements_and_pay_stay_with_their_own_requisition(reverse):
    raw = [
        _posting("bengali-open"),
        _posting("german-required", pay="USD 40 per hour",
                 extra=" Requirements: Native German fluency is mandatory."),
    ]
    if reverse:
        raw.reverse()
    result = evaluate_raw(raw, as_of=NOW)
    by_url = {item.job.url: item for item in result.evaluated}
    assert set(by_url) == {POSTING + "bengali-open", POSTING + "german-required"}
    open_role = by_url[POSTING + "bengali-open"]
    restricted = by_url[POSTING + "german-required"]

    assert open_role.decision.action_band == ActionBand.PRIMARY
    assert open_role.job.pay_raw is None
    assert open_role.assessment.selected_pay is None
    assert "german" not in open_role.assessment.language_requirements
    assert not open_role.assessment.pay_conflict
    assert restricted.decision.terminal_reason == "reject_language"
    assert "german" in restricted.assessment.language_requirements
    assert restricted.assessment.selected_pay is not None
    assert len(open_role.canonical.observations) == len(restricted.canonical.observations) == 1
    assert result.accounting_ok, result.accounting_errors


def _confirmed_copies():
    original = _posting("confirmed")
    copy_record = {
        "source": "remotive",
        "raw": {
            "title": original["raw"]["title"],
            "company_name": "Example Labs",
            "url": POSTING + "confirmed?utm_source=synthetic-board",
            "description": original["raw"]["descriptionPlain"],
            "candidate_required_location": "India",
            "publication_date": "2026-10-22T09:00:00Z",
        },
    }
    return [copy_record, original]


def test_legacy_true_duplicate_retains_corroboration(monkeypatch):
    jobs, counts = _legacy_run(_confirmed_copies(), monkeypatch)
    assert len(jobs) == 1
    assert jobs[0].source == "ashby"
    assert jobs[0].url == POSTING + "confirmed"
    assert jobs[0].seen_on == ["ashby", "remotive"]
    assert jobs[0].verdict == VERDICT_SURFACED
    assert counts["raw"] == 2 and counts["deduped"] == counts["accounted"] == 1
    assert counts["unaccounted"] == 0


def test_default_true_duplicate_retains_corroboration():
    result = evaluate_raw(_confirmed_copies(), as_of=NOW)
    assert len(result.evaluated) == 1
    item = result.evaluated[0]
    assert item.job.source == "ashby"
    assert item.job.url == POSTING + "confirmed"
    assert item.canonical.corroborated_by == ["ashby", "remotive"]
    assert len(item.canonical.observations) == 2
    assert item.decision.action_band == ActionBand.PRIMARY
    assert result.counts["duplicate_observations_collapsed"] == 1
    assert result.accounting_ok, result.accounting_errors


def test_saved_default_snapshot_replays_both_eligible_requisitions(tmp_path):
    raw = [_posting("vacancy?jobId=one"), _posting("vacancy?jobId=two")]
    original = evaluate_raw(raw, as_of=NOW)
    assert len(original.evaluated) == 2
    path = write_snapshot(raw, original, directory=tmp_path)
    replayed = replay(path)

    assert {item.job.url for item in replayed.evaluated} == {
        POSTING + "vacancy?jobId=one", POSTING + "vacancy?jobId=two",
    }
    assert decision_fingerprint(replayed) == decision_fingerprint(original)
    assert replayed.counts == original.counts
    assert replayed.accounting_ok, replayed.accounting_errors


@pytest.mark.parametrize("release_policy", [False, True], ids=["v42", "v55"])
@pytest.mark.parametrize("field,values", [
    ("title", ("AI Trainer C++", "AI Trainer C#")),
    ("title", ("AI Trainer বাংলা", "AI Trainer হিন্দি")),
    ("_company", ("গবেষণা এক", "গবেষণা দুই")),
])
def test_meaningful_title_and_company_characters_survive_shared_url_identity(
    monkeypatch, release_policy, field, values,
):
    monkeypatch.setattr(CONFIG.v55, "enabled", release_policy)
    raw = [_posting("shared"), _posting("shared")]
    for record, value in zip(raw, values):
        record["raw"][field] = value
    result = evaluate_raw(raw, as_of=NOW)

    assert result.counts["canonical_jobs"] == 2
    assert result.counts["duplicate_observations_collapsed"] == 0
    assert len({item.canonical.canonical_id for item in result.evaluated}) == 2
    job_field = "company" if field == "_company" else field
    assert {getattr(item.job, job_field) for item in result.evaluated} == set(values)
    assert result.accounting_ok, result.accounting_errors


@pytest.mark.parametrize("release_policy", [False, True], ids=["v42", "v55"])
def test_repeated_capture_does_not_arbitrarily_flip_equal_quality_source_content(
    monkeypatch, release_policy,
):
    monkeypatch.setattr(CONFIG.v55, "enabled", release_policy)
    raw = [
        _posting("same", extra=" Prefer cohort A."),
        _posting("same", extra=" Prefer cohort B."),
    ]
    selections = set()
    for offset in range(10):
        result = evaluate_raw(raw, as_of=NOW + timedelta(minutes=offset))
        assert len(result.evaluated) == 1
        job = result.evaluated[0].job
        selections.add((job.id, job.title, job.description, job.source))
    # Time may change freshness and readiness, but does not supply new source
    # content or justify changing the displayed observation chosen for a tie.
    assert len(selections) == 1
