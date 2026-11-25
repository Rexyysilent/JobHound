"""Decision-complete review export; bulky source evidence stays in its snapshot.

This is not the full audit or a replacement snapshot. Every evaluated row stays
present, including hidden/rejected results. Evidence references are inert data.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

from .review_audit import MAX_BYTES, validate_audit
from .v41.posting_deadlines import DEADLINE_FIELDS
from .v41.provenance import public_url, sanitize_payload


def _compact_observation(observation):
    fields = ("observation_id", "origin", "identity_state", "content_state", "vacancy_state",
              "application_route_state", "truncated", "verified_open_at", "source_kind", "captured_at")
    record = observation.model_dump(mode="json", include={
        **dict.fromkeys(fields, True), "raw_payload": set(DEADLINE_FIELDS),
    })
    record["has_job"] = observation.job is not None
    record["normalized_url"] = public_url(observation.normalized_url or
                                          (observation.job.url if observation.job else ""))
    # Retain the distinction between a historical deadline and a scoped
    # closure notice without copying the notice or unrelated source payload.
    record.setdefault("raw_payload", {})["_closure_evidence"] = bool(observation.raw_payload.get("_closure_evidence"))
    return record


def compact_audit(result, *, snapshot_reference: str, snapshot_sha256: str):
    if len(snapshot_sha256) != 64 or any(c not in "0123456789abcdef" for c in snapshot_sha256):
        raise ValueError("explicit snapshot SHA-256 required")
    rows = []
    for item in result.evaluated:
        assessment = item.assessment.model_dump(mode="json")
        decision = item.decision.model_dump(mode="json")
        job = item.job.model_dump(mode="json")
        rows.append({
            "canonical_id": item.canonical.canonical_id,
            "job": {key: job.get(key) for key in ("id", "source", "title", "company", "url", "location", "posted_at")},
            "assessment": {key: assessment.get(key) for key in (
                "eligibility", "blockers", "unresolved", "document_type",
                "opportunity_type", "intent", "buyer_intent", "pay_conflict", "selected_pay", "pay_credibility",
                "match_strength", "action_readiness", "next_action", "next_step",
                "verification_tasks", "account_state", "pay_assessment",
                "source_actionability", "lifecycle", "lifecycle_reason", "verified_open_at",
            )},
            "decision": decision,
            "outcome_projection": item.canonical.outcome_projection.model_dump(mode="json") if item.canonical.outcome_projection else None,
            "evidence": {
                "observation_ids": [o.observation_id for o in item.canonical.observations],
                "observations_complete": True,
                "observations": [_compact_observation(observation) for observation in item.canonical.observations],
            },
        })
    payload = sanitize_payload({
        "schema": "jobhound-compact-audit/v1",
        "metadata": result.metadata.model_dump(mode="json"),
        "counts": result.counts, "accounting_ok": result.accounting_ok,
        "accounting_errors": result.accounting_errors,
        "source_health": result.source_health, "decisions": rows,
        "evidence_reference": {"snapshot": snapshot_reference, "sha256": snapshot_sha256,
                               "requires_pinned_code_and_context": True},
        "completeness": {"all_decisions": True, "decision_count": len(rows),
                         "full_observations_embedded": False},
    })
    validate_audit(payload)
    return payload


def write_compact_audit(result, target, *, snapshot_reference, snapshot_sha256):
    payload = compact_audit(result, snapshot_reference=snapshot_reference, snapshot_sha256=snapshot_sha256)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("compact audit exceeds reader bound; use explicit paged export")
    with Path(target).open("xb") as stream:
        stream.write(encoded)
    return hashlib.sha256(encoded).hexdigest()
