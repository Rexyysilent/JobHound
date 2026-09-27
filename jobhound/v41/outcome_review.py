"""Explicit offline outcome import and preview. No production persistence/delivery."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .outcomes import OutcomeBinding, OutcomeEvent, import_events, projection_hash


def read_jsonl(path: Path | str) -> list[dict]:
    source = Path(path)
    if source.stat().st_size > 8_000_000:
        raise ValueError('outcome_file_too_large')
    lines = [line for line in source.read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(lines) > 5000 or any(len(line.encode('utf-8')) > 16000 for line in lines):
        raise ValueError('outcome_record_limit')
    try:
        return [json.loads(line) for line in lines]
    except ValueError:
        raise ValueError('invalid_jsonl') from None


def bind_outcomes(result, bindings: list[OutcomeBinding]):
    """Only an explicitly reviewed exact observation/URL can target a canonical."""
    from .provenance import public_url
    index = {row.canonical.canonical_id: row for row in result.evaluated}
    targets = [b.canonical_id for b in bindings]
    if len(targets) != len(set(targets)):
        raise ValueError('multiple_active_attempts_for_one_canonical')
    for binding in bindings:
        row = index.get(binding.canonical_id)
        if row is None:
            raise ValueError('unknown_binding_canonical')
        matched = [o for o in row.canonical.observations if o.observation_id == binding.observation_id]
        if not matched or not matched[0].job:
            raise ValueError('unknown_binding_observation')
        expected = public_url(matched[0].job.url).rstrip('/')
        if not expected or binding.role_url.rstrip('/') != expected:
            raise ValueError('binding_url_mismatch')
    return index


def preview_outcomes(result, records, bindings, *, as_of, previous=()):
    """Returns a COPY, evaluated through existing action policy and PriorityKey."""
    from ..config import CONFIG
    from .engine import reassess_item, recount_result
    if not CONFIG.v55.enabled:
        raise ValueError('outcomes_require_review_policy')
    if as_of.tzinfo is None or as_of != result.metadata.as_of:
        raise ValueError('outcome_and_job_as_of_must_match')
    copied = result.model_copy(deep=True)
    index = bind_outcomes(copied, bindings)
    imported = import_events(records, bindings, as_of, previous)
    for binding, projection in zip(bindings, imported['projections']):
        if not projection.events:
            continue
        row = index[binding.canonical_id]
        row.canonical.outcome_projection = projection
        reassess_item(row, as_of=as_of)
    # Keep the same engine key, not another outcome-specific ordering formula.
    from .engine import _evaluation_sort_key
    copied.evaluated.sort(key=_evaluation_sort_key)
    recount_result(copied)
    bundle = sorted((b.scope.key(), b.canonical_id, projection_hash(p))
                    for b, p in zip(bindings, imported['projections']))
    fingerprint = hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()
    copied.metadata.run_id = hashlib.sha256(
        (result.metadata.run_id + ':outcome-review-v1:' + fingerprint).encode()).hexdigest()[:20]
    imported['projection_fingerprint'] = fingerprint
    return copied, imported


def run_outcome_review(snapshot, events_path, bindings_path, output_dir, *, as_of,
                       previous_events=None):
    from ..config import CONFIG
    from .engine import evaluate_raw, evaluate_observations, decision_fingerprint
    from .models import ListingObservation
    from .replay import load_snapshot
    from .digest import build_digest
    from .review import _write_audit, _review_enabled
    root = Path(__file__).resolve().parents[2]
    target = Path(output_dir).resolve()
    # New child directory only; never repurpose an existing user/production folder.
    protected = [root/'data', root/'state']
    db = Path(CONFIG.v41.sidecar_db)
    protected.append((db if db.is_absolute() else root/db).resolve().parent)
    if target.exists() or any(target == p or p in target.parents for p in protected):
        raise ValueError('output_requires_new_nonproduction_directory')
    records = read_jsonl(events_path)
    try:
        bindings = [OutcomeBinding.model_validate(row) for row in read_jsonl(bindings_path)]
        previous = [OutcomeEvent.model_validate(row) for row in read_jsonl(previous_events)] if previous_events else []
    except ValueError:
        raise ValueError('invalid_binding_or_previous_journal') from None
    header, raw = load_snapshot(snapshot)
    # The observation timestamp of the historical capture must NOT be refreshed.
    captured_at = datetime.fromisoformat(header['as_of'])
    if as_of.tzinfo is None or captured_at.tzinfo is None or as_of < captured_at:
        raise ValueError('review_time_precedes_capture_or_lacks_timezone')
    with _review_enabled():
        seed = evaluate_raw(raw, as_of=captured_at)
        extra = [ListingObservation.model_validate(row) for row in
                 [*header.get('hydration_observations', []), *header.get('account_state_observations', [])]]
        result = evaluate_observations([*seed.observations, *extra], as_of=as_of, raw_count=len(raw))
        result.metadata.snapshot_path = str(Path(snapshot).resolve())
        result.source_health = header.get('source_health', [])
        candidate, imported = preview_outcomes(result, records, bindings, as_of=as_of, previous=previous)
        digest = build_digest(candidate, include_all=True)
    if not candidate.accounting_ok or not digest.accounting_ok:
        raise ValueError('outcome_review_accounting_failed')
    counts = imported['counts']
    if counts['input'] != counts['accepted'] + counts['duplicate'] + counts['quarantined']:
        raise ValueError('outcome_import_accounting_failed')
    target.mkdir(parents=True, exist_ok=False)
    (target/'events.jsonl').write_text(''.join(e.model_dump_json()+'\n' for e in imported['events']), encoding='utf-8')
    (target/'bindings.jsonl').write_text(''.join(b.model_dump_json()+'\n' for b in bindings), encoding='utf-8')
    (target/'projections.json').write_text(json.dumps([p.model_dump(mode='json') for p in imported['projections']], indent=2), encoding='utf-8')
    (target/'quarantine.json').write_text(json.dumps(imported['quarantine'], indent=2), encoding='utf-8')
    _write_audit(candidate, target)
    statuses = []
    bound_ids = {b.canonical_id for b in bindings}
    for item in candidate.evaluated:
        if item.canonical.canonical_id in bound_ids and item.canonical.outcome_projection is not None:
            statuses.append({'canonical_id': item.canonical.canonical_id,
                'lifecycle': item.decision.lifecycle, 'next_action': item.decision.next_action,
                'next_step': item.decision.next_step,
                'event_ids': [e.event_id for e in item.canonical.outcome_projection.events]})
    (target/'outcome_actions.json').write_text(json.dumps(statuses, indent=2), encoding='utf-8')
    text = digest.text + '\nOffline outcome status preview (not delivery-deduplicated):\n'
    text += '\n'.join(f"- {s['canonical_id']}: {s['next_action']} — {s['next_step']}" for s in statuses)
    (target/'preview.md').write_text(text+'\n', encoding='utf-8')
    manifest = {'mode': 'offline_outcome_review_v1', 'as_of': as_of.isoformat(),
        'captured_at': captured_at.isoformat(),
        'snapshot_sha256': hashlib.sha256(Path(snapshot).read_bytes()).hexdigest(),
        'events_input_sha256': hashlib.sha256(Path(events_path).read_bytes()).hexdigest(),
        'bindings_input_sha256': hashlib.sha256(Path(bindings_path).read_bytes()).hexdigest(),
        'previous_events_sha256': hashlib.sha256(Path(previous_events).read_bytes()).hexdigest() if previous_events else None,
        'policy_identity': {key: getattr(candidate.metadata, key) for key in
                            ('engine_version', 'ruleset_hash', 'profile_hash', 'trust_registry_hash', 'config_hash')},
        'projection_fingerprint': imported['projection_fingerprint'],
        'decision_fingerprint': decision_fingerprint(candidate), 'import_counts': counts,
        'decision_counts': candidate.counts, 'presentation_counts': digest.display_counts,
        'production_store_called': False, 'delivery_called': False,
        'limitations': ['Reviewed facts, not authenticated mailbox verification',
                       'Status preview has no delivery deduplication; E remains separate',
                       'Current local profile/policy, not reconstruction of historical policy']}
    (target/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return target, manifest
