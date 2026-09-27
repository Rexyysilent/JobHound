"""Offline validation and material comparison of JobHound review audits.

This module never fetches data, re-ranks jobs, opens a database, or delivers mail.
It inspects saved assessments, not the truth of the underlying vacancies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import unicodedata

MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000
SCHEMA = "jobhound-audit-review/v1"


class AuditError(ValueError):
    """An input is invalid or outside the reviewer's resource bounds."""


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise AuditError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise AuditError(f"non-finite JSON number: {value}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_audit(data: Any) -> list[dict[str, str]]:
    """Validate the existing review exporter shape; return non-fatal warnings."""
    if not isinstance(data, dict):
        raise AuditError("audit root must be an object")
    for key in ("metadata", "counts"):
        if not isinstance(data.get(key), dict):
            raise AuditError(f"{key} must be an object")
    if not isinstance(data.get("accounting_ok"), bool):
        raise AuditError("accounting_ok must be a Boolean")
    rows = data.get("decisions")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise AuditError(f"decisions must be an array of at most {MAX_ROWS} rows")
    for key in ("canonical", "canonical_jobs"):
        claimed = data["counts"].get(key)
        if claimed is not None and (isinstance(claimed, bool) or
                                   not isinstance(claimed, int) or claimed != len(rows)):
            raise AuditError("canonical count does not match decision rows")
    if data.get("schema") == "jobhound-compact-audit/v1":
        completeness = data.get("completeness")
        if (not isinstance(completeness, dict) or
            completeness.get("all_decisions") is not True or
            type(completeness.get("decision_count")) is not int or
            completeness["decision_count"] != len(rows) or
            type(data["counts"].get("canonical_jobs")) is not int):
            raise AuditError("compact audit must account for every canonical decision")
    warnings = []
    if not data["accounting_ok"]:
        warnings.append({"code": "upstream_accounting_failed", "canonical_id": ""})
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise AuditError("every decision row must be an object")
        identity = row.get("canonical_id")
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 512:
            raise AuditError("canonical_id must be nonempty text of at most 512 characters")
        if identity in seen:
            raise AuditError(f"duplicate canonical_id: {identity}")
        seen.add(identity)
        for key in ("job", "assessment", "decision"):
            if not isinstance(row.get(key), dict):
                raise AuditError(f"{identity}: {key} must be an object")
        decision, assessment = row["decision"], row["assessment"]
        for field in ("blockers", "unresolved"):
            values = assessment.get(field)
            if values is not None and (not isinstance(values, list) or
                                       not all(isinstance(item, str) for item in values)):
                raise AuditError(f"{identity}: {field} must be an array of strings")
        if assessment.get("selected_pay") is not None and not isinstance(assessment["selected_pay"], dict):
            raise AuditError(f"{identity}: selected_pay must be an object or null")
        band = decision.get("action_band")
        if not isinstance(band, str) or band not in {"primary", "verify", "reject"}:
            raise AuditError(f"{identity}: unsupported action_band")
        if band == "primary" and assessment.get("eligibility") != "passed":
            warnings.append({"code": "primary_without_saved_eligibility_pass", "canonical_id": identity})
        if decision.get("notification_transition") == "pay_resolved" and assessment.get("pay_conflict"):
            warnings.append({"code": "pay_resolved_with_saved_conflict", "canonical_id": identity})
        document_type = assessment.get("document_type")
        if document_type is not None and not isinstance(document_type, str):
            raise AuditError(f"{identity}: document_type must be text or null")
        account_state = assessment.get("account_state")
        if account_state is not None and not isinstance(account_state, dict):
            raise AuditError(f"{identity}: account_state must be an object or null")
        if band == "primary" and document_type in {
            "job_index", "job_index_search", "search_page", "talent_directory", "seller_service", "discussion", "article"
        }:
            warnings.append({"code": "primary_non_opportunity_document", "canonical_id": identity})
    # Dump once to reject infinities and unsupported in-memory values as well.
    try:
        canonical_json(data)
    except (ValueError, TypeError, RecursionError) as exc:
        raise AuditError("audit contains unsupported JSON values or excessive nesting") from exc
    return warnings


def load_audit(path: str | Path, *, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """Read with a hard byte cap, duplicate-key rejection, and no network I/O."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise AuditError(f"max_bytes must be an integer in [1, {MAX_BYTES}]")
    with Path(path).open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise AuditError(f"audit exceeds {max_bytes} bytes")
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=_constant)
        validate_audit(data)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AuditError("invalid UTF-8 JSON audit") from exc
    return data


def _pay(pay: Any) -> Any:
    if not isinstance(pay, dict):
        return None
    # Preserve native and legacy representations; never silently convert them.
    keys = ("currency", "basis", "actual_unit", "amount_low", "amount_high",
            "min_hourly_usd", "max_hourly_usd", "fixed_amount_usd", "qualifier",
            "up_to", "estimated", "guaranteed", "labor_hourly_supported", "gross_net", "scope",
            "literal_unit", "parse_warnings", "guaranteed_minimum", "fees",
            "net_amount_usd")
    return {key: pay.get(key) for key in keys}


def _stable_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = unicodedata.normalize('NFC', value)
    value = value.translate(
        dict.fromkeys(map(ord, '\u2010\u2011\u2012\u2013\u2014\u2212'), '-')
    )
    return re.sub(r'\s+', ' ', value).strip()


def _outcome_facts(projection: Any) -> Any:
    if not projection:
        return None
    current = {
        identity
        for identities in projection.get('current', {}).values()
        for identity in identities
    }
    facts = [
        {key: event.get(key) for key in ('predicate', 'value', 'actor', 'expires_at')}
        for event in projection.get('events', [])
        if event.get('event_id') in current
    ]
    return {
        'scope': projection.get('scope'),
        'facts': sorted(facts, key=canonical_json),
        'conflicts': sorted(projection.get('conflicts') or []),
    }


def material_projection(row: dict[str, Any]) -> dict[str, Any]:
    """Visible facts only. Scores, capture clocks and notification labels are not facts."""
    assessment, decision, job = row["assessment"], row["decision"], row["job"]
    projection = {
        "job": {
            key: (job.get(key) if key == "url" else _stable_text(job.get(key)))
            for key in ("title", "company", "url", "location")
        },
        "action": {
            key: decision.get(key)
            for key in ("action_band", "lifecycle", "next_action")
        },
        "eligibility": assessment.get("eligibility"),
        "action_readiness": assessment.get("action_readiness"),
        "account_state": {key: (assessment.get("account_state") or {}).get(key) for key in (
            "application_state", "assessment_state", "project_access", "task_allocation",
            "payout_setup", "application_deadline", "deadline")},
        "blockers": sorted(set(assessment.get("blockers") or [])),
        "unresolved": sorted(set(assessment.get("unresolved") or [])),
        "document_type": assessment.get("document_type"),
        "pay_conflict": assessment.get("pay_conflict"),
        "pay_state": (assessment.get("pay_assessment") or {}).get("state"),
        "pay": _pay(assessment.get("selected_pay")),
        "scoped_outcomes": _outcome_facts(row.get("outcome_projection")),
    }
    return json.loads(canonical_json(projection))


def compare_audits(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare exact canonical IDs. Identity migrations require a separate crosswalk."""
    warnings_before, warnings_after = validate_audit(before), validate_audit(after)
    old = {row["canonical_id"]: row for row in before["decisions"]}
    new = {row["canonical_id"]: row for row in after["decisions"]}
    rows, counts = [], {"added": 0, "removed": 0, "material_change": 0, "unchanged": 0}
    for identity in sorted(old.keys() | new.keys()):
        left = material_projection(old[identity]) if identity in old else None
        right = material_projection(new[identity]) if identity in new else None
        status = "added" if left is None else ("removed" if right is None else
                 ("unchanged" if left == right else "material_change"))
        counts[status] += 1
        if status != "unchanged":
            rows.append({"canonical_id": identity, "status": status, "before": left, "after": right})
    return {"schema": SCHEMA, "mode": "saved_audit_comparison_not_engine_replay",
            "before_fingerprint": fingerprint(before), "after_fingerprint": fingerprint(after),
            "counts": counts, "changes": rows,
            "warnings_before": warnings_before, "warnings_after": warnings_after,
            "profile_changed": before["metadata"].get("profile_hash") != after["metadata"].get("profile_hash"),
            "policy_changed": any(before["metadata"].get(key) != after["metadata"].get(key)
                                  for key in ("config_hash", "ruleset_hash", "engine_version", "trust_registry_hash")),
            "truth_claim": "Saved fields were compared; real vacancy correctness was not adjudicated."}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate")
    check.add_argument("audit")
    compare = commands.add_parser("compare")
    compare.add_argument("before")
    compare.add_argument("after")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            data = load_audit(args.audit)
            output = {"schema": SCHEMA, "valid": True, "rows": len(data["decisions"]),
                      "fingerprint": fingerprint(data), "warnings": validate_audit(data)}
        else:
            output = compare_audits(load_audit(args.before), load_audit(args.after))
        print(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False))
    except (OSError, AuditError) as exc:
        parser.exit(2, f"audit review failed: {exc}\n")


if __name__ == "__main__":
    main()
