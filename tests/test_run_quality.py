"""Whole-cohort quality diagnostics use synthetic saved evidence only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from jobhound.quality import quality_report
from jobhound.review_audit import AuditError, fingerprint, load_audit

NOW = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def row(identity, *, band="verify", lifecycle="active", source="search"):
    return {"canonical_id": identity, "job": {"source": source, "title": "Synthetic evaluator"},
            "assessment": {"eligibility": "unknown", "blockers": [], "unresolved": [],
                           "action_readiness": "verification_needed", "source_actionability": "aggregator_unresolved",
                           "verified_open_at": None,
                           "verification_tasks": [{"missing_fact": "current_open_status"}]},
            "decision": {"action_band": band, "lifecycle": lifecycle, "next_action": "verify",
                         "terminal_reason": "surface_verify"}}


def audit(rows):
    return {"metadata": {"as_of": NOW.isoformat(), "run_id": "synthetic-run",
                         "runtime_versions": {"code_sha256": "1" * 64},
                         "profile_hash": "2" * 64, "config_hash": "3" * 64},
            "counts": {"canonical_jobs": len(rows)}, "accounting_ok": True, "decisions": rows}


def ready(identity="ready"):
    item = row(identity, band="primary", source="ats")
    item["decision"]["next_action"] = "apply"
    item["assessment"].update(eligibility="passed", action_readiness="application_ready",
                              source_actionability="original_ats", verification_tasks=[],
                              verified_open_at=NOW.isoformat())
    item["evidence"] = {"observations_complete": True, "observations": [{
        "observation_id": identity, "has_job": True, "origin": "hydration", "identity_state": "exact",
        "content_state": "role_complete", "vacancy_state": "verified_open", "truncated": False,
        "application_route_state": "observed_public_route", "verified_open_at": NOW.isoformat(),
    }]}
    return item


def codes(report):
    return {warning["code"] for warning in report["warnings"]}


def test_whole_cohort_partition_keeps_rejections_status_and_ready_steps():
    rejected = row("rejected", band="reject")
    rejected["decision"]["terminal_reason"] = "reject_location"
    watch = row("watch", lifecycle="watch")
    verify = row("verify")
    verify["assessment"]["verification_tasks"] *= 2  # one missing fact, not two candidates
    step = ready("step")
    step["assessment"].update(action_readiness="captured_next_step", verified_open_at=None)
    step["decision"]["next_action"] = "respond_to_invitation"
    incomplete = row("legacy-primary", band="primary")
    missing_lifecycle = row("unknown")
    del missing_lifecycle["decision"]["lifecycle"]
    data = audit([rejected, watch, verify, ready(), step, incomplete, missing_lifecycle])
    data["source_health"] = [{"source": "search", "status": "partial", "error_type": "ReadTimeout"},
                             {"source": "ats", "status": "ok"}]
    report = quality_report(data)
    assert report["cohort"] == {"total": 7, "rejected": 1, "watch_or_status": 1, "active_verify": 1,
                                "application_ready": 1, "other_ready_action": 1,
                                "active_primary_unconfirmed": 1, "lifecycle_unknown": 1,
                                "partition_total": 7, "partition_ok": True, "upstream_accounting_ok": True}
    assert report["active_verification"]["missing_facts"]["items"] == [
        {"fact": "current_open_status", "candidates": 1}]
    assert report["coverage"]["statuses"] == {"ok": 1, "partial": 1}
    assert "coverage_degraded" in codes(report)
    assert "no_verified_actions" not in codes(report)
    assert report["source_yield"]["rows"][0]["total"] == 5
    assert report["source_yield"]["rows"][1]["application_ready_share"] == .5
    assert report["external_accuracy"]["precision"] is None
    assert report["external_accuracy"]["state"] == "not_measured"


def test_all_verify_warning_and_missing_coverage_are_actionable_without_inventing_accuracy():
    report = quality_report(audit([row("a"), row("b")]))
    assert {"no_verified_actions", "all_active_candidates_require_verification", "coverage_not_recorded"} <= codes(report)
    assert report["active_verification"]["missing_facts"]["items"][0]["candidates"] == 2
    assert report["coverage"]["expected_configured_sources"] is None
    assert report["external_accuracy"]["recall"] is None
    assert report["provenance"]["code_sha256"] == "1" * 64
    assert report["provenance"]["snapshot_sha256"] is None


def test_legacy_missing_fields_remain_unrecorded_not_failed_or_ready():
    item = {"canonical_id": "legacy", "job": {}, "assessment": {}, "decision": {"action_band": "primary"}}
    data = audit([item])
    report = quality_report(data)
    assert report["cohort"]["lifecycle_unknown"] == 1
    assert report["cohort"]["rejected"] == report["cohort"]["application_ready"] == 0
    assert report["saved_statuses"]["eligibility"] == {"not_recorded": 1}
    assert report["evidence_support"]["opening"] == {"not_recorded": 1}
    assert report["source_yield"]["rows"][0]["source"] is None


@pytest.mark.parametrize("field,support", [
    ("has_job", "matched_role_content"), ("identity_state", "matched_role_content"),
    ("content_state", "matched_role_content"), ("truncated", "matched_role_content"),
    ("vacancy_state", "opening"), ("application_route_state", "application_route"),
])
@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_null_observation_fields_stay_not_recorded(field, support, missing):
    item = ready()
    observation = item["evidence"]["observations"][0]
    if missing:
        observation.pop(field)
    else:
        observation[field] = None
    report = quality_report(audit([item]))
    assert report["cohort"]["application_ready"] == 0
    assert report["evidence_support"][support] == {"not_recorded": 1}


@pytest.mark.parametrize("change", ["no_evidence", "incomplete_references", "closed", "future_open",
                                    "unmatched", "truncated", "route_unknown", "tasks_missing", "blockers_missing"])
def test_primary_label_alone_cannot_become_application_ready(change):
    item = ready()
    observation = item["evidence"]["observations"][0]
    if change == "no_evidence":
        del item["evidence"]
    elif change == "incomplete_references":
        item["evidence"]["observations_complete"] = False
    elif change == "closed":
        observation["vacancy_state"] = "explicitly_closed"
    elif change == "future_open":
        observation["verified_open_at"] = (NOW + timedelta(days=1)).isoformat()
    elif change == "unmatched":
        observation["identity_state"] = "uncertain"
    elif change == "truncated":
        observation["truncated"] = True
    elif change == "route_unknown":
        observation["application_route_state"] = "account_unknown"
    elif change == "tasks_missing":
        del item["assessment"]["verification_tasks"]
    elif change == "blockers_missing":
        del item["assessment"]["blockers"]
    report = quality_report(audit([item]))
    assert report["cohort"]["application_ready"] == 0
    assert report["cohort"]["active_primary_unconfirmed"] == 1
    assert "primary_readiness_unconfirmed" in codes(report)


def test_full_reference_resolution_and_unknown_missing_references():
    item = ready()
    observation = item.pop("evidence")["observations"][0]
    observation["job"] = item["job"]
    observation.pop("has_job")
    item["observation_ids"] = ["ready"]
    data = audit([item])
    data["observations"] = [observation]
    assert quality_report(data)["cohort"]["application_ready"] == 1
    item["observation_ids"].append("not-in-audit")
    report = quality_report(data)
    assert report["cohort"]["application_ready"] == 0
    assert report["fields_not_recorded"]["complete_observation_references"] == 1


def test_determinism_preserves_input_and_bounds_groups_without_losing_cohort():
    data = audit([row(str(i), source=f"source-{i}") for i in range(40)])
    before = deepcopy(data)
    first = quality_report(data, top_limit=3)
    assert first == quality_report(deepcopy(data), top_limit=3)
    assert data == before
    assert first["audit_fingerprint"] == fingerprint(data)
    assert len(first["source_yield"]["rows"]) == 3
    assert first["source_yield"]["omitted_sources"] == first["source_yield"]["omitted_candidates"] == 37
    assert first["cohort"]["total"] == 40


@pytest.mark.parametrize("value", [0, 101, True, 1.5, "3"])
def test_invalid_top_bounds_rejected(value):
    with pytest.raises(AuditError, match="top_limit"):
        quality_report(audit([]), top_limit=value)


@pytest.mark.parametrize("path,value", [
    (("source_health",), "ok"), (("metadata", "as_of"), "2026-11-01"),
    (("metadata", "runtime_versions"), []), (("evidence_reference",), {"sha256": "not-a-hash"}),
])
def test_malformed_optional_diagnostics_are_rejected(path, value):
    data = audit([row("a")])
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(AuditError):
        quality_report(data)


@pytest.mark.parametrize("key,value", [("verification_tasks", ["unknown"]),
                                       ("verification_tasks", [{"missing_fact": ["open"]}]),
                                       ("source_actionability", False), ("verified_open_at", "yesterday")])
def test_malformed_decision_fields_rejected(key, value):
    item = row("a")
    item["assessment"][key] = value
    with pytest.raises(AuditError):
        quality_report(audit([item]))


@pytest.mark.parametrize("key,value", [("raw_payload", "deadline tomorrow"),
                                       ("captured_at", "yesterday"), ("source_kind", False),
                                       ("normalized_url", 123)])
def test_malformed_posting_status_fields_rejected(key, value):
    item = ready()
    item["evidence"]["observations"][0][key] = value
    with pytest.raises(AuditError):
        quality_report(audit([item]))


def test_empty_cohort_is_not_all_verify_and_failed_accounting_is_not_hidden():
    data = audit([])
    data["accounting_ok"] = False
    report = quality_report(data)
    assert report["cohort"]["partition_ok"]
    assert not report["cohort"]["upstream_accounting_ok"]
    assert "upstream_accounting_failed" in codes(report)
    assert "all_active_candidates_require_verification" not in codes(report)
    assert "no_verified_actions" not in codes(report)


def test_missing_run_clock_and_unknown_health_stay_unknown():
    data = audit([ready()])
    del data["metadata"]["as_of"]
    data["source_health"] = [{"source": "ats"}]
    report = quality_report(data)
    assert report["cohort"]["application_ready"] == 0
    assert report["evidence_support"]["opening"] == {"not_recorded": 1}
    assert "coverage_status_unknown" in codes(report)
    assert report["coverage"]["statuses"] == {"not_recorded": 1}


def test_cli_reads_only_input_without_network_database_or_operator_config(tmp_path):
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(audit([row("a")])), encoding="utf-8")
    before = path.read_bytes()
    script = """import pathlib,runpy,socket,sqlite3,sys
def forbidden(*a,**kw): raise AssertionError('unexpected external operation')
socket.create_connection=socket.socket.connect=sqlite3.connect=forbidden
def audit_event(event,args):
    if event=='open' and isinstance(args[0],(str,bytes)):
        if pathlib.Path(args[0]).name in {'.env','config.yaml','profile.yaml','platform_registry.yaml'}:
            raise AssertionError('operator policy read')
sys.addaudithook(audit_event)
sys.argv=['quality','quality',sys.argv[1]]
runpy.run_module('jobhound.review_audit',run_name='__main__')
"""
    process = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=True)
    output = json.loads(process.stdout)
    assert output["cohort"]["active_verify"] == 1
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_cli_rejects_duplicate_keys_without_partial_report(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"metadata":{},"metadata":{}}', encoding="utf-8")
    process = subprocess.run([sys.executable, "-m", "jobhound.review_audit", "quality", str(path)],
                             capture_output=True, text=True)
    assert process.returncode == 2 and process.stdout == ""
    assert "duplicate JSON key" in process.stderr


def test_full_compact_and_review_export_keep_same_cohort_and_exact_snapshot(tmp_path, monkeypatch):
    from jobhound.config import CONFIG
    from jobhound.models import Job
    from jobhound.v41.engine import evaluate_observations
    from jobhound.v41.models import ListingObservation, SourceKind
    from jobhound.v41.review import _write_audit, _write_quality_exports
    from jobhound.v41.replay import write_snapshot

    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    job = Job(source="ashby", title="Bengali AI Response Evaluator", company="Example",
              url="https://jobs.ashbyhq.com/example/synthetic-one", location="India", is_remote=True,
              description="Requirements: Fluent Bengali and English. No degree or experience required. "
                          "Remote applicants in India may apply. Review AI responses using supplied guidelines.",
              pay_raw="USD 12 per working hour", posted_at=NOW - timedelta(days=1))
    observation = ListingObservation(observation_id="synthetic-one", source="ashby", job=job,
        source_kind=SourceKind.ORIGINAL_ATS, content_state="complete", identity_state="exact",
        vacancy_state="verified_open", application_route_state="observed_public_route", verified_open_at=NOW)
    result = evaluate_observations([observation], as_of=NOW)
    result.source_health = [{"source": "ashby", "status": "ok"}]
    snapshot = write_snapshot([], result, directory=tmp_path)
    snapshot_bytes = snapshot.read_bytes()
    full_path = _write_audit(result, tmp_path)
    full_bytes = full_path.read_bytes()
    receipt = _write_quality_exports(result, tmp_path, snapshot)
    full, compact = load_audit(full_path), load_audit(tmp_path / receipt["compact_audit"])
    assert "observations" in full and "observations" not in compact
    assert "description" in full["decisions"][0]["job"] and "description" not in compact["decisions"][0]["job"]
    full_report, compact_report = quality_report(full), quality_report(compact)
    assert full_report["cohort"] == compact_report["cohort"]
    assert full_report["evidence_support"] == compact_report["evidence_support"]
    assert full_report["source_yield"] == compact_report["source_yield"]
    assert full_report["cohort"]["application_ready"] == 1
    quality_path = tmp_path / receipt["quality_report"]
    saved = json.loads(quality_path.read_text(encoding="utf-8"))
    assert saved == compact_report
    assert saved["provenance"]["snapshot_sha256"] == hashlib.sha256(snapshot_bytes).hexdigest()
    assert receipt["quality_report_sha256"] == hashlib.sha256(quality_path.read_bytes()).hexdigest()
    assert receipt["compact_audit_sha256"] == hashlib.sha256((tmp_path / "audit.compact.json").read_bytes()).hexdigest()
    assert snapshot.read_bytes() == snapshot_bytes and full_path.read_bytes() == full_bytes
    assert _write_quality_exports(result, tmp_path, snapshot) == receipt
    assert not list(tmp_path.glob(".quality-*"))


@pytest.mark.parametrize("scenario,application_ready,opening", [
    ("renewed_deadline_closure", True, "verified_open_recorded"),
    ("renewed_old_open", True, "verified_open_recorded"),
    ("removed_deadline_after_closure", True, "verified_open_recorded"),
    ("removed_deadline_after_open", True, "verified_open_recorded"),
    ("explicit_closure_notice", False, "explicitly_closed"),
    ("copy_cannot_extend_original", False, "deadline_elapsed"),
    ("current_original_beats_old_copy", True, "verified_open_recorded"),
    ("current_original_beats_closed_copy", True, "verified_open_recorded"),
    ("future_original_cannot_extend", False, "deadline_elapsed"),
    ("future_closure_cannot_close", True, "verified_open_recorded"),
])
def test_quality_and_engine_share_current_posting_status_in_full_and_compact(
        tmp_path, monkeypatch, scenario, application_ready, opening):
    from jobhound.compact_audit import compact_audit
    from jobhound.config import CONFIG
    from jobhound.v41.engine import evaluate_observations
    from jobhound.v41.models import SourceKind
    from jobhound.v41.review import _write_audit
    from test_production_hydration_accuracy import dated_posting, NOW as run_clock

    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    old = dated_posting("old", run_clock - timedelta(days=2), "2026-11-24")
    new = dated_posting("new", run_clock, "2026-12-10")
    if scenario in {"removed_deadline_after_closure", "removed_deadline_after_open"}:
        old.source_kind = new.source_kind = SourceKind.ORIGINAL_EMPLOYER
        del new.raw_payload["validThrough"]
        if scenario == "removed_deadline_after_closure":
            old.captured_at = run_clock - timedelta(hours=1)
            old.vacancy_state = "explicitly_closed"
            old.verified_open_at = None
    if scenario in {"renewed_deadline_closure", "explicit_closure_notice"}:
        old.vacancy_state = "explicitly_closed"
    if scenario == "explicit_closure_notice":
        old.raw_payload["_closure_evidence"] = {"text": "This exact role is closed."}
    elif scenario == "copy_cannot_extend_original":
        new.source_kind = SourceKind.REPUTABLE_BOARD
    elif scenario in {"current_original_beats_old_copy", "current_original_beats_closed_copy"}:
        old.source_kind = SourceKind.REPUTABLE_BOARD
        old.vacancy_state = "explicitly_closed"
        if scenario == "current_original_beats_closed_copy":
            old.raw_payload["_closure_evidence"] = {"text": "No longer accepting applications on this board."}
    elif scenario == "future_original_cannot_extend":
        new.captured_at = new.verified_open_at = run_clock + timedelta(days=1)
    elif scenario == "future_closure_cannot_close":
        old = dated_posting("old", run_clock + timedelta(days=1), "2026-11-24",
                            vacancy_state="explicitly_closed", closure_notice=True)
    old.raw_payload["unrelated_source_text"] = "Bulky captured source content stays in the full audit."
    old_payload = deepcopy(old.raw_payload)
    result = evaluate_observations([old, new], as_of=run_clock, raw_count=2)
    assert len(result.evaluated) == 1
    assert (result.evaluated[0].assessment.next_action == "apply") is application_ready

    full = load_audit(_write_audit(result, tmp_path))
    compact = compact_audit(result, snapshot_reference="captured-snapshot.json", snapshot_sha256="0" * 64)
    full_report, compact_report = quality_report(full), quality_report(compact)
    assert full_report["cohort"] == compact_report["cohort"]
    assert full_report["evidence_support"] == compact_report["evidence_support"]
    assert compact_report["cohort"]["application_ready"] == int(application_ready)
    assert compact_report["evidence_support"]["opening"] == {opening: 1}
    assert ("no_verified_actions" not in codes(compact_report)) is application_ready
    assert old.raw_payload == old_payload
    projected = {o["observation_id"]: o for o in compact["decisions"][0]["evidence"]["observations"]}
    assert len(projected) == 2 and projected["old"]["vacancy_state"] == old.vacancy_state
    assert projected["old"]["captured_at"] == old.captured_at.isoformat().replace("+00:00", "Z")
    assert projected["old"]["source_kind"] == old.source_kind.value
    assert projected["old"]["normalized_url"] == old.normalized_url
    assert projected["old"]["raw_payload"] == {
        "validThrough": "2026-11-24", "_closure_evidence": bool(old_payload.get("_closure_evidence")),
    }
    assert any(o["raw_payload"].get("unrelated_source_text") for o in full["observations"])


def test_capture_and_replay_add_exports_without_changing_full_audit_contract(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from jobhound.v41 import review

    async def ingest(**kwargs):
        return [], []
    async def hydrate(*args, **kwargs):
        return {"changed": 0}
    monkeypatch.setattr(review, "ingest_raw_with_health", ingest)
    monkeypatch.setattr(review, "hydrate_result", hydrate)
    captured = tmp_path / "captured"
    snapshot, _ = asyncio.run(review.capture_review(captured,
        transport=httpx.MockTransport(lambda request: httpx.Response(500))))
    replayed = tmp_path / "replayed"
    review.replay_review(snapshot, replayed)
    for directory, name in ((captured, "manifest.json"), (replayed, "replay_manifest.json")):
        manifest = json.loads((directory / name).read_text(encoding="utf-8"))
        assert manifest["compact_audit"] == "audit.compact.json"
        assert manifest["quality_report"] == "quality.json"
        assert "observations" in json.loads((directory / "audit.json").read_text(encoding="utf-8"))
        quality = json.loads((directory / "quality.json").read_text(encoding="utf-8"))
        assert quality["cohort"]["total"] == 0
        assert quality["provenance"]["snapshot_sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()


def test_oversized_compact_is_nonfatal_keeps_full_output_and_never_prints_stale_path(tmp_path, monkeypatch, capsys):
    import asyncio
    import httpx
    from jobhound import review_audit
    from jobhound.cli import _print_quality_exports
    from jobhound.v41 import review

    async def ingest(**kwargs):
        return [], []
    async def hydrate(result, **kwargs):
        result.metadata.runtime_versions["synthetic_oversize_padding"] = "x" * 50_000
        return {"changed": 0}
    monkeypatch.setattr(review, "ingest_raw_with_health", ingest)
    monkeypatch.setattr(review, "hydrate_result", hydrate)
    monkeypatch.setattr(review_audit, "MAX_BYTES", 12_000)
    stale = tmp_path / "audit.compact.json"
    stale.write_text("earlier review export", encoding="utf-8")
    snapshot, digest = asyncio.run(review.capture_review(tmp_path,
        transport=httpx.MockTransport(lambda request: httpx.Response(500))))
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert snapshot.is_file() and digest.is_file() and (tmp_path / "audit.json").is_file()
    assert "observations" in json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert manifest["compact_audit"] is manifest["compact_audit_sha256"] is None
    status = manifest["quality_export_status"]["compact_audit"]
    assert status["status"] == "omitted_size_limit" and status["bytes"] > status["limit_bytes"] == 12_000
    assert not stale.exists()
    assert manifest["quality_report"] == "quality.json" and (tmp_path / "quality.json").is_file()
    quality = json.loads((tmp_path / "quality.json").read_text(encoding="utf-8"))
    assert quality["cohort"]["total"] == 0
    _print_quality_exports(tmp_path, "manifest.json")
    printed = capsys.readouterr().out
    assert "Compact audit: omitted_size_limit" in printed
    assert str(stale) not in printed
    assert str(tmp_path / "quality.json") in printed


@pytest.mark.parametrize("failed_export", ["compact_audit", "quality_report"])
def test_optional_export_validation_failure_preserves_capture_and_replay(tmp_path, monkeypatch, capsys, failed_export):
    import asyncio
    import httpx
    from jobhound import review_audit
    from jobhound.cli import _print_quality_exports
    from jobhound.sources.base import SourceHealth
    from jobhound.v41 import review
    from test_source_native_semantics import lever

    async def ingest(**kwargs):
        health = SourceHealth(source="lever", status="partial", error_type="x" * 513)
        return [lever()], [health]
    async def hydrate(*args, **kwargs):
        return {"changed": 0}
    monkeypatch.setattr(review, "ingest_raw_with_health", ingest)
    monkeypatch.setattr(review, "hydrate_result", hydrate)
    if failed_export == "compact_audit":
        monkeypatch.setattr(review_audit, "MAX_ROWS", 0)
    captured, replayed = tmp_path / "capture", tmp_path / "replay"
    for directory in (captured, replayed):
        directory.mkdir()
        (directory / "quality.json").write_text("stale")
        (directory / "audit.compact.json").write_text("stale")
    snapshot, digest = asyncio.run(review.capture_review(captured, transport=httpx.MockTransport(
        lambda request: pytest.fail("unexpected HTTP request"))))
    output, fingerprint = review.replay_review(snapshot, replayed)
    assert snapshot.is_file() and digest.is_file() and output.is_file() and (captured / "hydration_report.json").is_file()
    for directory, name in ((captured, "manifest.json"), (replayed, "replay_manifest.json")):
        manifest = json.loads((directory / name).read_text())
        assert manifest["fingerprint"] == fingerprint and (directory / manifest["audit"]).is_file()
        assert manifest["quality_export_status"][failed_export]["error_type"] == "AuditError"
        assert manifest["quality_report"] is None and not (directory / "quality.json").exists()
        assert (directory / "audit.compact.json").is_file() == (failed_export == "quality_report")
        _print_quality_exports(directory, name)
        assert "export_failed (AuditError)" in capsys.readouterr().out
