"""Bounded offline diagnostics of saved assessments, never vacancy truth.

No current configuration, credentials, database, network client or clock is
consulted. Missing evidence is reported as missing, not a negative assertion.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .review_audit import AuditError, MAX_ROWS, fingerprint, validate_audit
from .v41.posting_deadlines import DEADLINE_FIELDS, deadline_timestamp, resolve_posting_status

SCHEMA = "jobhound-run-quality/v1"
DEFAULT_TOP = 15
_MATCHED = {"exact", "corroborated"}
_READY_ACTIONS = {
    "allocated": {"start_allocated_task"},
    "captured_next_step": {"respond_to_invitation", "complete_known_step", "clarify_scope"},
}
_BUCKETS = ("rejected", "watch_or_status", "active_verify", "application_ready",
            "other_ready_action", "active_primary_unconfirmed", "lifecycle_unknown")
_KNOWN_STATES = {
    "eligibility": {"passed", "failed", "unknown"},
    "action_readiness": {"application_ready", "allocated", "captured_next_step",
                         "verification_needed", "watch", "closed", "blocked", "unknown"},
    "source_actionability": {"original_ats", "original_employer", "email_offer",
                             "reputable_board", "aggregator_unresolved", "content_farm", "unknown"},
}


def _text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise AuditError(f"{field} must be nonempty text of at most 512 characters or null")
    return value


def _array(value: Any, field: str) -> list | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise AuditError(f"{field} must be an array of at most {MAX_ROWS} entries or null")
    return value


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    value = _text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AuditError(f"{field} must include a timezone")
    return parsed


def _top(counter: Counter, limit: int, label: str) -> dict:
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    return {"items": [{label: key, "candidates": count} for key, count in ordered[:limit]],
            "omitted_distinct": max(0, len(ordered) - limit)}


def _observation_index(data: dict) -> dict[str, dict] | None:
    observations = _array(data.get("observations"), "observations")
    if observations is None:
        return None
    index = {}
    for observation in observations:
        if not isinstance(observation, dict):
            raise AuditError("observations must contain objects")
        identity = _text(observation.get("observation_id"), "observation_id")
        if identity is None or identity in index:
            raise AuditError("saved observations require unique observation_id values")
        index[identity] = observation
    return index


def _row_observations(row: dict, index: dict | None) -> tuple[list[dict], bool]:
    evidence = row.get("evidence")
    if evidence is not None and not isinstance(evidence, dict):
        raise AuditError("decision evidence must be an object or null")
    evidence = evidence or {}
    if "observations_complete" in evidence and type(evidence["observations_complete"]) is not bool:
        raise AuditError("observations_complete must be a Boolean")
    if "observations" in evidence:
        observations = _array(evidence["observations"], "evidence.observations")
        if observations is None:
            return [], False
        if not all(isinstance(item, dict) for item in observations):
            raise AuditError("evidence.observations must contain objects")
        return observations, evidence.get("observations_complete") is True
    identities = _array(row.get("observation_ids", evidence.get("observation_ids")), "observation_ids")
    if identities is not None:
        for identity in identities:
            if _text(identity, "observation_id") is None:
                raise AuditError("observation_ids must contain strings")
    if identities is None or index is None:
        return [], False
    return [index[identity] for identity in identities if identity in index], all(
        identity in index for identity in identities)


def _support(observations: list[dict], complete: bool, as_of: datetime | None) -> dict[str, str]:
    """Positive role evidence is usable; incomplete references stay unknown."""
    matched, posting_records = [], []
    fields_complete = complete
    for observation in observations:
        for key in ("identity_state", "content_state", "vacancy_state", "application_route_state", "origin"):
            _text(observation.get(key), key)
        if observation.get("has_job") is not None and type(observation["has_job"]) is not bool:
            raise AuditError("observation has_job must be a Boolean")
        if observation.get("truncated") is not None and type(observation["truncated"]) is not bool:
            raise AuditError("observation truncated must be a Boolean")
        observed_open = _timestamp(observation.get("verified_open_at"), "verified_open_at")
        if observation.get("origin") == "account_state":
            continue
        if observation.get("has_job") is None and "job" not in observation:
            fields_complete = False
        has_job = observation.get("has_job") is True or isinstance(observation.get("job"), dict)
        if not has_job:
            continue
        fields_complete = fields_complete and all(observation.get(key) is not None for key in (
            "identity_state", "content_state", "vacancy_state", "application_route_state", "truncated"))
        if observation.get("identity_state") in _MATCHED:
            matched.append((observation, observed_open))
            _text(observation.get("source_kind"), "observation.source_kind")
            _timestamp(observation.get("captured_at"), "observation.captured_at")
            raw = observation.get("raw_payload")
            if raw is not None and not isinstance(raw, dict):
                raise AuditError("observation raw_payload must be an object or null")
            job = observation.get("job")
            url = (observation.get("normalized_url") or observation.get("url")
                   or (job.get("url") if isinstance(job, dict) else None))
            if url is not None and not isinstance(url, str):
                raise AuditError("observation posting URL must be text or null")
            # Saved exports already contain public-normalized URLs. Keep this
            # adapter free of live provenance/configuration imports.
            posting_records.append({
                **{key: observation.get(key) for key in (
                    "observation_id", "source_kind", "identity_state", "content_state",
                    "truncated", "captured_at", "vacancy_state")},
                "url": url,
                "raw_payload": ({key: raw.get(key) for key in (*DEADLINE_FIELDS, "_closure_evidence")}
                                if raw is not None else None),
            })
    unavailable = "unverified" if fields_complete else "not_recorded"
    content = any(o.get("content_state") in {"complete", "role_complete"}
                  and o.get("truncated") is False for o, _ in matched)
    route = any(o.get("application_route_state") in {"observed_public_route", "account_verified"}
                for o, _ in matched)
    status = resolve_posting_status(posting_records, as_of) if as_of is not None else None
    closed = bool(status and status.closed_observation_ids)
    deadline_elapsed = bool(status and any(
        deadline is not None and deadline <= as_of
        for value in status.deadline_values if (deadline := deadline_timestamp(value)) is not None))
    opened = any(o.get("vacancy_state") == "verified_open" and time is not None
                 and as_of is not None and time <= as_of for o, time in matched)
    opening_unavailable = "not_recorded" if as_of is None or any(
        o.get("vacancy_state") == "verified_open" and time is None for o, time in matched) else unavailable
    return {"matched_role_content": "supported" if content else unavailable,
            "application_route": "supported" if route else unavailable,
            "opening": ("explicitly_closed" if closed else "deadline_elapsed" if deadline_elapsed
                        else "verified_open_recorded" if opened else opening_unavailable)}


def _tasks(assessment: dict) -> list[dict] | None:
    tasks = _array(assessment.get("verification_tasks"), "verification_tasks")
    if tasks is not None:
        for task in tasks:
            if not isinstance(task, dict):
                raise AuditError("verification_tasks must contain objects")
            _text(task.get("missing_fact"), "missing_fact")
    return tasks


def _bucket(row: dict, support: dict, tasks: list[dict] | None, as_of: datetime | None,
            evidence_complete: bool) -> str:
    assessment, decision = row["assessment"], row["decision"]
    if decision["action_band"] == "reject":
        return "rejected"
    lifecycle = _text(decision.get("lifecycle"), "decision.lifecycle")
    if lifecycle in {None, "unknown"}:
        return "lifecycle_unknown"
    if lifecycle != "active":
        return "watch_or_status"
    if decision["action_band"] == "verify":
        return "active_verify"
    next_action = _text(decision.get("next_action"), "decision.next_action")
    common = assessment.get("eligibility") == "passed" and assessment.get("blockers") == [] and tasks == []
    readiness = assessment.get("action_readiness")
    if common and readiness == "application_ready" and next_action == "apply":
        observed_open = _timestamp(assessment.get("verified_open_at"), "assessment.verified_open_at")
        if (evidence_complete and as_of is not None and observed_open is not None and observed_open <= as_of
            and support == {"matched_role_content": "supported", "application_route": "supported",
                            "opening": "verified_open_recorded"}):
            return "application_ready"
    if common and next_action in _READY_ACTIONS.get(readiness, set()):
        return "other_ready_action"
    return "active_primary_unconfirmed"


def _coverage(data: dict, source_names: set, limit: int) -> dict:
    rows = _array(data.get("source_health"), "source_health")
    if rows is None:
        rows = []
    statuses, sources, degraded = Counter(), set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise AuditError("source_health must contain objects")
        source = _text(row.get("source"), "source_health.source")
        status = _text(row.get("status"), "source_health.status")
        _text(row.get("stage"), "source_health.stage")
        _text(row.get("error_type"), "source_health.error_type")
        if source is not None:
            sources.add(source)
        statuses[status if status in {"ok", "empty", "partial", "failed", "disabled", "unknown"}
                 else "not_recorded" if status is None else "other_recorded"] += 1
        if status in {"partial", "failed"}:
            degraded.add(source)
    missing = sorted(source for source in source_names if source is not None and source not in sources)
    degraded_names = sorted(source for source in degraded if source is not None)
    return {"state": "recorded" if rows else "not_recorded", "scope_rows": len(rows),
            "statuses": dict(sorted(statuses.items())), "degraded_source_count": len(degraded),
            "degraded_sources": degraded_names[:limit],
            "sources_without_health_count": len(missing), "sources_without_health": missing[:limit],
            "expected_configured_sources": None,
            "interpretation": "Saved source-health rows only; configured-source completeness is not established."}


def quality_report(data: dict[str, Any], *, top_limit: int = DEFAULT_TOP) -> dict[str, Any]:
    """Summarize every saved decision, including hidden, rejected and status rows."""
    if type(top_limit) is not int or not 1 <= top_limit <= 100:
        raise AuditError("top_limit must be an integer in [1, 100]")
    validation = validate_audit(data)
    as_of = _timestamp(data["metadata"].get("as_of"), "metadata.as_of")
    index = _observation_index(data)
    funnel = dict.fromkeys(_BUCKETS, 0)
    sources = defaultdict(lambda: dict.fromkeys(_BUCKETS, 0))
    states = {field: Counter() for field in _KNOWN_STATES}
    supports = {field: Counter() for field in ("matched_role_content", "application_route", "opening")}
    missing, rejected, absent_fields = Counter(), Counter(), Counter()
    task_rows, task_missing_rows, tasks_without_fact = 0, 0, 0
    for row in data["decisions"]:
        assessment, decision = row["assessment"], row["decision"]
        source = _text(row["job"].get("source"), "job.source")
        for field, allowed in _KNOWN_STATES.items():
            value = _text(assessment.get(field), "assessment." + field)
            states[field]["not_recorded" if value is None else value if value in allowed else "other_recorded"] += 1
            if value is None:
                absent_fields["assessment." + field] += 1
        for field in ("blockers", "unresolved", "verification_tasks", "verified_open_at"):
            if field not in assessment or assessment[field] is None:
                absent_fields["assessment." + field] += 1
        _timestamp(assessment.get("verified_open_at"), "assessment.verified_open_at")
        _text(decision.get("lifecycle"), "decision.lifecycle")
        _text(decision.get("next_action"), "decision.next_action")
        tasks = _tasks(assessment)
        observations, complete = _row_observations(row, index)
        support = _support(observations, complete, as_of)
        if not complete:
            absent_fields["complete_observation_references"] += 1
        for field, value in support.items():
            supports[field][value] += 1
        bucket = _bucket(row, support, tasks, as_of, complete)
        funnel[bucket] += 1
        sources[source][bucket] += 1
        if bucket == "rejected":
            reason = _text(decision.get("terminal_reason"), "terminal_reason")
            rejected[reason or "not_recorded"] += 1
        if bucket == "active_verify":
            if tasks is None:
                task_missing_rows += 1
            else:
                task_rows += 1
                facts = {task["missing_fact"] for task in tasks if task.get("missing_fact") is not None}
                missing.update(facts)
                tasks_without_fact += sum(task.get("missing_fact") is None for task in tasks)
    coverage = _coverage(data, set(sources), top_limit)
    source_rows = [{"source": source, "total": sum(counts.values()), **counts,
                    "application_ready_share": round(counts["application_ready"] / sum(counts.values()), 6)}
                   for source, counts in sources.items()]
    source_rows.sort(key=lambda row: (-row["total"], row["source"] or ""))
    metadata = data["metadata"]
    versions = metadata.get("runtime_versions")
    if versions is not None and not isinstance(versions, dict):
        raise AuditError("runtime_versions must be an object or null")
    versions = versions or {}
    evidence_ref = data.get("evidence_reference")
    if evidence_ref is not None and not isinstance(evidence_ref, dict):
        raise AuditError("evidence_reference must be an object or null")
    evidence_ref = evidence_ref or {}
    provenance = {key: _text(metadata.get(key), "metadata." + key) for key in (
        "run_id", "as_of", "engine_version", "profile_hash", "config_hash", "ruleset_hash", "trust_registry_hash")}
    for key in ("code_sha256", "dependencies_sha256"):
        provenance[key] = _text(versions.get(key), "runtime_versions." + key)
    provenance["snapshot_sha256"] = _text(evidence_ref.get("sha256"), "evidence_reference.sha256")
    if provenance["snapshot_sha256"] is not None and (
        len(provenance["snapshot_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in provenance["snapshot_sha256"])):
        raise AuditError("evidence_reference.sha256 must be a lowercase SHA-256")
    warnings = []
    def warn(code: str, message: str, count: int | None = None) -> None:
        warnings.append({"code": code, "message": message, **({"count": count} if count is not None else {})})
    total = len(data["decisions"])
    ready = funnel["application_ready"] + funnel["other_ready_action"]
    if total and ready == 0:
        warn("no_verified_actions", "No application-ready or captured-next-step action is supported by the saved fields; this is not proof that no suitable work exists.")
    active = funnel["active_verify"] + funnel["application_ready"] + funnel["other_ready_action"] + funnel["active_primary_unconfirmed"]
    if active and funnel["active_verify"] == active:
        warn("all_active_candidates_require_verification", "Every active saved candidate is VERIFY; inspect missing facts and source-resolution coverage.", active)
    if funnel["active_primary_unconfirmed"]:
        warn("primary_readiness_unconfirmed", "Saved PRIMARY rows lack consistent recorded readiness or supporting role/open/route evidence.", funnel["active_primary_unconfirmed"])
    if coverage["state"] == "not_recorded":
        warn("coverage_not_recorded", "No source-health evidence was saved; this audit cannot establish whether collection coverage was complete.")
    elif coverage["sources_without_health_count"]:
        warn("sources_without_health", "Some retained job sources have no saved source-health row; collection health is unknown for them.", coverage["sources_without_health_count"])
    if coverage["degraded_source_count"]:
        warn("coverage_degraded", "Saved source scopes include partial or failed collection; these states do not establish vacancy closure.", coverage["degraded_source_count"])
    unknown_health = sum(coverage["statuses"].get(status, 0) for status in ("unknown", "not_recorded", "other_recorded"))
    if unknown_health:
        warn("coverage_status_unknown", "Some saved source scopes lack a recognized health status; their collection health remains unknown.", unknown_health)
    if absent_fields:
        warn("quality_fields_not_recorded", "Some diagnostic fields are missing; unrecorded evidence is not treated as a negative fact.")
    if provenance["code_sha256"] is None:
        warn("runtime_fingerprint_not_recorded", "The audit does not identify the code content used for this run.")
    for code, count in sorted(Counter(item["code"] for item in validation).items()):
        warn(code, "The saved audit validator reported this consistency warning.", count)
    return {
        "schema": SCHEMA, "mode": "saved_assessment_diagnostics_not_external_accuracy",
        "audit_fingerprint": fingerprint(data), "provenance": provenance,
        "cohort": {"total": total, **funnel, "partition_total": sum(funnel.values()),
                   "partition_ok": sum(funnel.values()) == total, "upstream_accounting_ok": data["accounting_ok"]},
        "saved_statuses": {key: dict(sorted(value.items())) for key, value in states.items()},
        "evidence_support": {key: dict(sorted(value.items())) for key, value in supports.items()},
        "active_verification": {"candidates": funnel["active_verify"], "task_fields_recorded": task_rows,
                                "task_fields_not_recorded": task_missing_rows, "tasks_without_missing_fact": tasks_without_fact,
                                "missing_facts": _top(missing, top_limit, "fact")},
        "rejection_reasons": _top(rejected, top_limit, "reason"),
        "fields_not_recorded": dict(sorted(absent_fields.items())),
        "source_yield": {"attribution": "Retained canonical job.source; not marginal collector contribution.",
                         "denominator": "All canonical decisions assigned to each source, including rejections and status rows.",
                         "rows": source_rows[:top_limit], "omitted_sources": max(0, len(source_rows) - top_limit),
                         "omitted_candidates": sum(row["total"] for row in source_rows[top_limit:])},
        "coverage": coverage, "warnings": warnings,
        "external_accuracy": {"state": "not_measured", "precision": None, "recall": None,
                              "reason": "No independent vacancy labels or applicant outcomes were adjudicated by this report."},
        "limits": {"top_limit": top_limit, "all_decisions_examined": True,
                   "current_vacancies_rechecked": False, "snapshot_reference_verified": False,
                   "readiness_time": "Saved metadata.as_of and saved policy decisions, not the current clock."},
    }
