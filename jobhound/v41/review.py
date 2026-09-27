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
from ..run_context import RunContext, run_scope, current_run
from ..bounded_transport import BoundedTransport, RunBudget, RequestLimits
import httpx
import errno
import os
import hashlib


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


def _try_lock(handle) -> bool:
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_workspace(target: Path):
    """Hold an OS lock on ``.active-run``; the OS drops it if the process dies.

    A leftover file from a crashed run is therefore reclaimed, while a live
    capture still excludes every other one.
    """
    lock = target/'.active-run'
    while True:
        handle = open(lock, 'a+b')
        if not _try_lock(handle):
            handle.close()
            raise FileExistsError(errno.EEXIST,
                                  'review workspace is in use by another capture', str(lock))
        # POSIX: a releasing owner may have unlinked the path after we opened it.
        try:
            if os.name == 'nt' or os.path.samestat(os.fstat(handle.fileno()), os.stat(lock)):
                break
        except FileNotFoundError:
            pass
        handle.close()
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        yield
    finally:
        try:
            if os.name != 'nt':
                lock.unlink(missing_ok=True)
            _unlock(handle)
        finally:
            handle.close()
        if os.name == 'nt':
            try:
                lock.unlink(missing_ok=True)
            except PermissionError:
                pass  # the next capture already opened it


@contextmanager
def _review_enabled():
    config = CONFIG.model_copy(deep=True)
    config.v55.enabled = True
    prior = current_run()
    context = RunContext.capture(config=config,
        as_of=prior.as_of if prior else None,
        workspace=prior.workspace if prior else '.',
        run_id=prior.run_id if prior else None,
        network_allowed=prior.network_allowed if prior else False)
    with run_scope(context):
        yield


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
    captured_at = current_run().as_of if current_run() else datetime.now(timezone.utc)
    from .replay import release_policy_payload
    release_policy = release_policy_payload()
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
    transport=None, context: RunContext | None = None,
    request_limits: RequestLimits | None = None,
) -> tuple[Path, Path]:
    """Exclusive workspace; explicit context capability required for real HTTP."""
    target = _namespace(output_dir)
    config = context.config() if context else CONFIG.model_copy(deep=True)
    config.v55.enabled = True
    context = context or RunContext.capture(config=config, workspace=target)
    if Path(context.workspace) != target or not context.config().v55.enabled:
        raise ValueError('capture context must select its review workspace and V5.5')
    with _exclusive_workspace(target):
        bounded = None
        try:
            limits = request_limits or RequestLimits(requests=config.v55.request_budget,
                                                     seconds=config.v55.time_budget_seconds)
            budget = RunBudget(context, limits)
            if transport is None and not context.network_allowed:
                raise ValueError('live capture needs explicit network capability')
            bounded = BoundedTransport(transport or httpx.AsyncHTTPTransport(retries=0), budget)
            with run_scope(context):
                return await _capture_review(target, limit=limit, source=source, transport=bounded)
        finally:
            if bounded:
                await bounded.close_owned()
                (target/'request_budget_receipt.json').write_text(
                    json.dumps(bounded.budget.receipt(), sort_keys=True), encoding='utf-8')


async def _capture_review(
    output_dir: Path | str, *, limit: int | None = None, source: str | None = None,
    transport=None,
) -> tuple[Path, Path]:
    """Fetch public inputs into an explicit namespace; no stores/delivery exist here."""
    target = _namespace(output_dir)
    with _review_enabled():
        progressive = target / ('completed_batches_' + current_run().run_id + '.jsonl')
        def save_batch(batch, health):
            # Completed source batches survive cancellation of another source.
            with progressive.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(sanitize_payload({'records': batch,
                    'health': health.as_dict()}), ensure_ascii=True) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
        raw, health = await ingest_raw_with_health(
            limit=limit, only=source, transport=transport, exclude={"email_offers"},
            on_batch=save_batch,
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
        "context_fingerprint": current_run().fingerprint(),
        "request_budget": transport.budget.receipt(),
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return snapshot, digest_path


def replay_review(snapshot: Path | str, output_dir: Path | str) -> tuple[Path, str]:
    """Pure file-to-file replay: contains no network, IMAP, LLM, store or delivery calls."""
    target = _namespace(output_dir)
    from .replay import load_snapshot, restore_release_policy
    header, _raw = load_snapshot(snapshot)
    config = CONFIG.model_copy(deep=True)
    config.v55.enabled = True
    restore_release_policy(config, header.get('release_policy'))
    context = RunContext.capture(config=config, workspace=target,
                                as_of=datetime.fromisoformat(header['as_of']))
    with run_scope(context):
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
