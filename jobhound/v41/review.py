"""Isolated V5.5 review capture and deterministic offline replay."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

from ..config import CONFIG
from ..pipeline import ingest_raw_with_health
from .digest import build_digest, write_digest
from .engine import decision_fingerprint, evaluate_raw
from .replay import replay, write_snapshot
from .resolve import hydrate_result
from .provenance import sanitize_payload


def _namespace(path: Path | str) -> Path:
    target = Path(path).expanduser().resolve()
    root = Path(__file__).resolve().parents[2]
    forbidden = [root / "data", root / "state", root / "jobhound.db"]
    if any(target == item.resolve() or item.resolve() in target.parents for item in forbidden):
        raise ValueError("review output must not be inside a production data/state path")
    target.mkdir(parents=True, exist_ok=True)
    marker = target / ".jobhound-review-namespace"
    marker.touch(exist_ok=True)
    return target


@contextmanager
def _review_enabled():
    previous = CONFIG.v55.enabled
    CONFIG.v55.enabled = True
    try:
        yield
    finally:
        CONFIG.v55.enabled = previous


def _write_audit(result, target: Path) -> Path:
    path = target / "audit.json"
    payload = {
        "metadata": result.metadata.model_dump(mode="json"),
        "counts": result.counts,
        "accounting_ok": result.accounting_ok,
        "accounting_errors": result.accounting_errors,
        "source_health": sanitize_payload({"rows": result.source_health})["rows"],
        "observations": [sanitize_payload(row.model_dump(mode="json")) for row in result.observations],
        "decisions": [{
            "canonical_id": row.canonical.canonical_id,
            "job": sanitize_payload(row.job.model_dump(mode="json")),
            "assessment": sanitize_payload(row.assessment.model_dump(mode="json")),
            "decision": sanitize_payload(row.decision.model_dump(mode="json")),
        } for row in result.evaluated],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _checkpoint_raw(raw: list[dict], health: list, target: Path) -> tuple[Path, Path, datetime]:
    """Save replayable sanitized discovery before fallible evaluation."""
    path = target / "raw_discovery.json"
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = target / f"raw_discovery_{stamp}.json"
    payload = [sanitize_payload(record) for record in raw]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    metadata_path = path.with_suffix(".metadata.json")
    captured_at = datetime.now(timezone.utc)
    release_policy = sanitize_payload(CONFIG.v55.model_dump(mode="json"))
    release_policy["enabled"] = True
    metadata_path.write_text(json.dumps({
        "type": "jobhound_raw_checkpoint",
        "version": 1,
        "captured_at": captured_at.isoformat(),
        "record_count": len(payload),
        "source_health": [row.as_dict() if hasattr(row, "as_dict") else sanitize_payload(row)
                          for row in health],
        "status": "captured_pre_evaluation",
        "release_policy": release_policy,
    }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path, metadata_path, captured_at


async def capture_review(
    output_dir: Path | str, *, limit: int | None = None, source: str | None = None,
    transport=None,
) -> tuple[Path, Path]:
    """Fetch public inputs into an explicit namespace; no stores/delivery exist here."""
    target = _namespace(output_dir)
    with _review_enabled():
        raw, health = await ingest_raw_with_health(
            limit=limit, only=source, transport=transport, exclude={"email_offers"}
        )
        # Evaluation is intentionally after this checkpoint: a malformed or
        # newly unsupported record must not erase the input needed to diagnose
        # and replay a failed capture.
        checkpoint_path, checkpoint_metadata, captured_at = _checkpoint_raw(raw, health, target)
        try:
            result = evaluate_raw(raw, as_of=captured_at)
        except Exception as exc:
            metadata = json.loads(checkpoint_metadata.read_text(encoding="utf-8"))
            metadata.update({"status": "evaluation_failed", "error_type": type(exc).__name__})
            checkpoint_metadata.write_text(json.dumps(
                metadata, ensure_ascii=False, indent=2, sort_keys=True
            ), encoding="utf-8")
            raise
        metadata = json.loads(checkpoint_metadata.read_text(encoding="utf-8"))
        metadata["status"] = "evaluation_complete"
        checkpoint_metadata.write_text(json.dumps(
            metadata, ensure_ascii=False, indent=2, sort_keys=True
        ), encoding="utf-8")
        result.source_health = [row.as_dict() for row in health]
        hydration_report = await hydrate_result(result, transport=transport, review_namespace=target)
    snapshot = write_snapshot(raw, result, directory=target)
    digest = build_digest(result, include_all=True)
    digest_path = write_digest(
        digest.text,
        date_stamp=result.metadata.as_of.astimezone().strftime("%Y%m%d_%H%M%S"),
        directory=target,
    )
    audit_path = _write_audit(result, target)
    hydration_path = target / "hydration_report.json"
    hydration_path.write_text(json.dumps(
        sanitize_payload(hydration_report), ensure_ascii=False, indent=2, sort_keys=True
    ), encoding="utf-8")
    manifest = {
        "mode": "isolated_review_capture",
        "snapshot": snapshot.name,
        "digest": digest_path.name,
        "audit": audit_path.name,
        "hydration_report": hydration_path.name,
        "fingerprint": decision_fingerprint(result),
        "production_store_called": False,
        "delivery_called": False,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return snapshot, digest_path


def replay_review(snapshot: Path | str, output_dir: Path | str) -> tuple[Path, str]:
    """Pure file-to-file replay: contains no network, IMAP, LLM, store or delivery calls."""
    target = _namespace(output_dir)
    with _review_enabled():
        result = replay(snapshot)
        digest = build_digest(result, include_all=True)
    output = target / f"replay_{Path(snapshot).stem}.md"
    output.write_text(digest.text, encoding="utf-8")
    audit_path = _write_audit(result, target)
    fingerprint = decision_fingerprint(result)
    (target / "replay_manifest.json").write_text(json.dumps({
        "mode": "offline_review_replay",
        "snapshot": str(Path(snapshot).resolve()),
        "output": output.name,
        "audit": audit_path.name,
        "fingerprint": fingerprint,
        "network_allowed": False,
        "imap_allowed": False,
        "llm_allowed": False,
        "production_store_called": False,
        "delivery_called": False,
    }, indent=2, sort_keys=True), encoding="utf-8")
    return output, fingerprint
