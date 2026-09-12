"""Sanitized raw snapshots and deterministic V3/V4.1 replay comparison."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import CONFIG
from ..dedupe import dedupe
from ..normalize import normalize
from ..pipeline import account_ledger, evaluate_jobs, finalize_verdicts
from .engine import decision_fingerprint, evaluate_observations, evaluate_raw
from .models import ActionBand, ListingObservation, RunResult
from .provenance import public_url, sanitize_payload

_ROOT = Path(__file__).resolve().parent.parent.parent


def snapshot_dir() -> Path:
    configured = Path(CONFIG.v41.snapshot_dir)
    return configured if configured.is_absolute() else _ROOT / configured


def write_snapshot(
    raw_records: list[dict],
    result: RunResult,
    *,
    directory: Path | None = None,
) -> Path:
    target_dir = directory or snapshot_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    # Microseconds make snapshots append-only even when a replay/test launches
    # twice inside the same second.
    stamp = result.metadata.as_of.astimezone().strftime("%Y%m%d_%H%M%S_%f")
    path = target_dir / f"jobhound_raw_{stamp}.jsonl"
    header = {
        "type": "jobhound_snapshot",
        "version": 1,
        "engine_version": result.metadata.engine_version,
        "as_of": result.metadata.as_of.isoformat(),
        "ruleset_hash": result.metadata.ruleset_hash,
        "profile_hash": result.metadata.profile_hash,
        "trust_registry_hash": result.metadata.trust_registry_hash,
        "config_hash": result.metadata.config_hash,
        "record_count": len(raw_records),
        "source_health": result.source_health,
        "hydration_observations": [
            sanitize_payload(observation.model_dump(mode="json"))
            for observation in result.observations
            if getattr(observation, "origin", "discovery") == "hydration"
        ],
    }
    if result.metadata.engine_version == "v5.0.0-rc1":
        header["release_policy"] = sanitize_payload(CONFIG.v55.model_dump(mode="json"))
        # Capture may have exited its temporary review scope before writing.
        # Freeze the policy that produced the result, not the restored default.
        header["release_policy"]["enabled"] = True
        header["account_state_observations"] = [
            sanitize_payload(observation.model_dump(mode="json"))
            for observation in result.observations
            if getattr(observation, "origin", "discovery") == "account_state"
        ]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")
        for index, record in enumerate(raw_records):
            observation = (
                result.observations[index]
                if index < len(result.observations) else None
            )
            safe = {
                "source": str(record.get("source") or "unknown"),
                "raw": sanitize_payload(
                    record.get("raw") if isinstance(record.get("raw"), dict) else {}
                ),
            }
            if observation is not None and observation.job is not None:
                resolution = {
                    "normalized_url": public_url(observation.normalized_url),
                    "source_kind": observation.source_kind.value,
                    "source_confidence": observation.source_confidence,
                    "redirect_trail": [public_url(url) for url in observation.redirect_trail],
                    "attempted": observation.resolution_attempted,
                    "error": observation.resolution_error,
                }
                if observation.hydrated_source:
                    resolution["hydrated_source"] = observation.hydrated_source
                    resolution["hydrated_job"] = sanitize_payload(
                        observation.job.model_dump(mode="json")
                    )
                safe["_v41_resolution"] = resolution
            handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    result.metadata.snapshot_path = str(path)
    return path


def load_snapshot(path: Path | str) -> tuple[dict, list[dict]]:
    snapshot_path = Path(path)
    if snapshot_path.suffix.casefold() == ".json":
        records = json.loads(snapshot_path.read_text(encoding="utf-8"))
        metadata_path = snapshot_path.with_suffix(".metadata.json")
        if not isinstance(records, list) or not metadata_path.exists():
            raise ValueError(f"not a JobHound raw checkpoint: {snapshot_path}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("type") != "jobhound_raw_checkpoint":
            raise ValueError(f"not a JobHound raw checkpoint: {snapshot_path}")
        if len(records) != int(metadata.get("record_count", len(records))):
            raise ValueError("checkpoint record count mismatch")
        header = {
            "type": "jobhound_snapshot", "version": 1,
            "as_of": metadata["captured_at"],
            "record_count": len(records),
            "source_health": metadata.get("source_health") or [],
            "release_policy": metadata.get("release_policy") or None,
        }
        return header, records
    with snapshot_path.open("r", encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    if not lines:
        raise ValueError(f"empty snapshot: {snapshot_path}")
    header = json.loads(lines[0])
    if header.get("type") != "jobhound_snapshot":
        raise ValueError(f"not a JobHound snapshot: {snapshot_path}")
    records = [json.loads(line) for line in lines[1:]]
    if len(records) != int(header.get("record_count", len(records))):
        raise ValueError("snapshot record count mismatch")
    return header, records


def latest_snapshot(directory: Path | None = None) -> Path:
    target = directory or snapshot_dir()
    candidates = sorted(target.glob("jobhound_raw_*.jsonl"))
    if not candidates:
        raise FileNotFoundError(f"no snapshots in {target}")
    return candidates[-1]


def replay(path: Path | str) -> RunResult:
    header, raw = load_snapshot(path)
    as_of = datetime.fromisoformat(header["as_of"])
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    policy = header.get("release_policy")
    previous_policy = CONFIG.v55.model_dump(mode="python")
    if policy:
        for name, value in policy.items():
            if name in type(CONFIG.v55).model_fields:
                setattr(CONFIG.v55, name, value)
    try:
        seed = evaluate_raw(raw, as_of=as_of)
        observations = list(seed.observations)
        for encoded in [*(header.get("hydration_observations") or []),
                        *(header.get("account_state_observations") or [])]:
            try:
                observations.append(ListingObservation.model_validate(encoded))
            except (TypeError, ValueError):
                continue
        result = (evaluate_observations(observations, as_of=as_of, raw_count=len(raw))
                  if len(observations) != len(seed.observations) else seed)
    finally:
        if policy:
            for name, value in previous_policy.items():
                setattr(CONFIG.v55, name, value)
    result.source_health = list(
        header.get("source_health") or []
    )
    result.metadata.snapshot_path = str(Path(path))
    return result


def _evaluate_v3(raw_records: list[dict]) -> tuple[list, dict]:
    jobs = []
    for record in raw_records:
        try:
            job = normalize(record)
        except (KeyError, TypeError, ValueError):
            continue
        if job is not None:
            jobs.append(job)
    normalized_count = len(jobs)
    jobs = dedupe(jobs)
    gates = evaluate_jobs(jobs)
    finalize_verdicts(jobs, gates)
    ledger, _ = account_ledger(jobs, len(jobs))
    return jobs, {
        "raw": len(raw_records),
        "normalized": normalized_count,
        "deduped": len(jobs),
        **ledger,
    }


def comparison_report(
    path: Path | str,
    result: RunResult | None = None,
) -> str:
    header, raw = load_snapshot(path)
    v41 = result or replay(path)
    v3_jobs, v3_counts = _evaluate_v3(raw)
    v3_surface = {job.id: job for job in v3_jobs if job.verdict == "surfaced"}
    v41_surface = {
        item.canonical.canonical_id: item
        for item in v41.evaluated
        if item.decision.action_band != ActionBand.REJECT
    }

    # IDs differ by design, so human comparison uses normalized title/company.
    def label(title: str, company: str | None) -> str:
        return f"{title.casefold().strip()} @ {(company or '').casefold().strip()}"

    v3_labels = {label(job.title, job.company): job for job in v3_surface.values()}
    v41_labels = {label(item.job.title, item.job.company): item for item in v41_surface.values()}
    added = sorted(set(v41_labels) - set(v3_labels))
    removed = sorted(set(v3_labels) - set(v41_labels))
    shared = sorted(set(v3_labels) & set(v41_labels))

    lines = [
        "# JobHound replay comparison",
        "",
        f"Snapshot: `{Path(path)}`",
        f"Frozen as-of: {header['as_of']}",
        f"V4.1 fingerprint: `{decision_fingerprint(v41)}`",
        "",
        "## Accounting",
        "",
        f"- V3: `{json.dumps(v3_counts, sort_keys=True)}`",
        f"- V4.1: `{json.dumps(v41.counts, sort_keys=True)}`",
        f"- V4.1 accounting: {'PASS' if v41.accounting_ok else 'FAIL'}",
        "",
        "## Surface changes",
        "",
        f"- Added by V4.1: {len(added)}",
        f"- Removed by V4.1: {len(removed)}",
        f"- Shared: {len(shared)}",
    ]
    if added:
        lines.extend(["", "### Added", *[f"- {item}" for item in added[:50]]])
    if removed:
        lines.extend(["", "### Removed", *[f"- {item}" for item in removed[:50]]])
    lines.extend(["", "## Shared decision details", ""])
    for item in shared[:50]:
        v3_job = v3_labels[item]
        v41_item = v41_labels[item]
        lines.append(
            f"- {v41_item.job.title} @ {v41_item.job.company or '—'}: "
            f"V3 fit {v3_job.fit_score:.0f}/ev {v3_job.ev_score:.1f} → "
            f"V4.1 {v41_item.decision.action_band.value}/"
            f"{v41_item.assessment.match_strength.value}"
        )
    return "\n".join(lines).rstrip() + "\n"
