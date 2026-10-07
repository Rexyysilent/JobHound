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
    targets = [b.canonical_id for b in bindings if b.active]
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


def preview_outcomes(result, records, bindings, *, as_of, previous=(), aliases=None):
    """Returns a COPY, evaluated through existing action policy and PriorityKey."""
    from ..config import CONFIG
    from .engine import reassess_item, recount_result
    if not CONFIG.v55.enabled:
        raise ValueError('outcomes_require_review_policy')
    if as_of.tzinfo is None or as_of != result.metadata.as_of:
        raise ValueError('outcome_and_job_as_of_must_match')
    bindings = [OutcomeBinding.model_validate(b.model_dump(mode='json', warnings=False)) for b in bindings]
    copied = result.model_copy(deep=True)
    # A reused preview carries facts, not authority to keep its former selected
    # attempt active. Revalidate those facts with the supplied complete bindings.
    prior = {e.event_id: e for e in previous}
    if len(prior) != len(previous):
        raise ValueError('invalid_previous_journal')
    for row in copied.evaluated:
        if row.canonical.outcome_projection is not None:
            for event in row.canonical.outcome_projection.events:
                if event.event_id in prior and prior[event.event_id].model_dump_json() != event.model_dump_json():
                    raise ValueError('invalid_previous_journal')
                prior[event.event_id] = event
        if row.canonical.outcome_projection is not None or row.canonical.outcome_binding_hold is not None:
            row.canonical.outcome_projection = None
            row.canonical.outcome_binding_hold = None
            reassess_item(row, as_of=as_of)
    alias_report = None
    if aliases is None:
        index = bind_outcomes(copied, bindings)
        resolved_ids = [b.canonical_id for b in bindings]
    else:
        from .outcome_aliases import resolve_alias_bindings
        index, alias_report = resolve_alias_bindings(copied, bindings, aliases)
        resolved_ids = [row['resolved_canonical_id'] for row in alias_report['binding_resolutions']]
    imported = import_events(records, bindings, as_of, list(prior.values()))
    if alias_report is not None:
        for resolution in alias_report['binding_resolutions']:
            if resolution['held_target_canonical_id']:
                row = index[resolution['held_target_canonical_id']]
                row.canonical.outcome_binding_hold = 'multiple_active_attempts_for_one_canonical'
                reassess_item(row, as_of=as_of)
    for binding, projection, destination in zip(bindings, imported['projections'], resolved_ids):
        if not binding.active or not projection.events or destination is None:
            continue
        row = index[destination]
        row.canonical.outcome_projection = projection
        reassess_item(row, as_of=as_of)
    # Keep the same engine key, not another outcome-specific ordering formula.
    from .engine import _evaluation_sort_key
    copied.evaluated.sort(key=_evaluation_sort_key)
    recount_result(copied)
    bundle = sorted((b.scope.key(), b.canonical_id, b.active, projection_hash(p))
                    for b, p in zip(bindings, imported['projections']))
    if alias_report is not None:
        bundle = {'projections': bundle, 'aliases': alias_report['accepted'],
                  'bindings': sorted(alias_report['binding_resolutions'], key=lambda r: json.dumps(r, sort_keys=True))}
        imported['alias_report'] = alias_report
    fingerprint = hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()
    copied.metadata.run_id = hashlib.sha256(
        (result.metadata.run_id + ':outcome-review-v1:' + fingerprint).encode()).hexdigest()[:20]
    imported['projection_fingerprint'] = fingerprint
    imported['active_attempts'] = [b.model_dump(mode='json') for b in bindings if b.active]
    return copied, imported


def run_outcome_review(snapshot, events_path, bindings_path, output_dir, *, as_of,
                       previous_events=None, aliases_path=None, handoff_signal=False,
                       delivery_state=None,reissue_plan_path=None):
    from ..config import CONFIG
    from .engine import evaluate_raw, evaluate_observations, decision_fingerprint
    from .models import ListingObservation
    from .replay import load_snapshot
    from .digest import build_digest
    from .review import _write_audit, _review_enabled
    root = Path(__file__).resolve().parents[2]
    target = Path(output_dir).resolve()
    if type(handoff_signal) is not bool or delivery_state is not None and not handoff_signal:
        raise ValueError('delivery_state_requires_handoff_review')
    if delivery_state is not None:
        from ..handoff_review import validate_review_state
        source=validate_review_state(delivery_state)
        if reissue_plan_path is not None:
            from ..handoff_reissue import read_reissue_plan
            read_reissue_plan(reissue_plan_path,hashlib.sha256(source.read_bytes()).hexdigest())
    elif reissue_plan_path is not None:
        raise ValueError('reissue_plan_requires_previous_handoff_state')
    # New child directory only; never repurpose an existing user/production folder.
    protected = [root/'data', root/'state']
    db = Path(CONFIG.v41.sidecar_db)
    protected.append((db if db.is_absolute() else root/db).resolve().parent)
    if target.exists() or any(target == p or p in target.parents for p in protected):
        raise ValueError('output_requires_new_nonproduction_directory')
    records = read_jsonl(events_path)
    aliases = read_jsonl(aliases_path) if aliases_path is not None else None
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
        candidate, imported = preview_outcomes(result, records, bindings, as_of=as_of, previous=previous, aliases=aliases)
        digest = build_digest(candidate, include_all=True)
    if not candidate.accounting_ok or not digest.accounting_ok:
        raise ValueError('outcome_review_accounting_failed')
    counts = imported['counts']
    if counts['input'] != counts['accepted'] + counts['duplicate'] + counts['quarantined']:
        raise ValueError('outcome_import_accounting_failed')
    target.mkdir(parents=True, exist_ok=False)
    (target/'events.jsonl').write_text(''.join(e.model_dump_json()+'\n' for e in imported['events']), encoding='utf-8')
    (target/'bindings.jsonl').write_text(''.join(b.model_dump_json()+'\n' for b in bindings), encoding='utf-8')
    (target/'active_attempts.json').write_text(json.dumps(imported['active_attempts'], indent=2), encoding='utf-8')
    (target/'projections.json').write_text(json.dumps([p.model_dump(mode='json') for p in imported['projections']], indent=2), encoding='utf-8')
    (target/'quarantine.json').write_text(json.dumps(imported['quarantine'], indent=2), encoding='utf-8')
    if 'alias_report' in imported:
        report = imported['alias_report']
        (target/'aliases.jsonl').write_text(''.join(json.dumps(row,sort_keys=True)+'\n' for row in report['accepted']),encoding='utf-8')
        (target/'alias_quarantine.json').write_text(json.dumps(report['quarantine'],indent=2),encoding='utf-8')
        (target/'binding_resolutions.json').write_text(json.dumps(report['binding_resolutions'],indent=2),encoding='utf-8')
    _write_audit(candidate, target)
    statuses = []
    bound_ids = ({value for r in imported['alias_report']['binding_resolutions']
                  for value in (r['resolved_canonical_id'],r['held_target_canonical_id']) if value is not None}
                 if 'alias_report' in imported else {b.canonical_id for b in bindings})
    for item in candidate.evaluated:
        if item.canonical.canonical_id in bound_ids and (item.canonical.outcome_projection is not None or item.canonical.outcome_binding_hold):
            status = {'canonical_id': item.canonical.canonical_id,
                'lifecycle': item.decision.lifecycle, 'next_action': item.decision.next_action,
                'next_step': item.decision.next_step,
                'event_ids': [e.event_id for e in item.canonical.outcome_projection.events] if item.canonical.outcome_projection is not None else [],
                'binding_hold': item.canonical.outcome_binding_hold}
            projection = item.canonical.outcome_projection
            work_action = item.assessment.account_state.get('_reviewed_work_action')
            if work_action is not None:
                status['work_action'] = work_action
            if projection is not None and (projection.facts('assessment_step') or any(e.assessment is not None for e in projection.events)):
                from .outcomes import ASSESSMENT_PREDICATES, ActionCost, AssessmentIdentity
                selected = projection.value('assessment_step')
                step = selected if isinstance(selected, AssessmentIdentity) else None
                status['assessment_step'] = step.model_dump() if step is not None else None
                status['assessment_attempt_scope'] = projection.scope.model_dump()
                facts = projection.facts('assessment_step')
                if step is not None:
                    facts += [e for predicate in sorted(ASSESSMENT_PREDICATES) for e in projection.facts(predicate, assessment=step)]
                status['assessment_current_event_ids'] = sorted(e.event_id for e in facts)
                cost = projection.value('action_cost', assessment=step) if step is not None else None
                cost_facts = projection.facts('action_cost', assessment=step) if step is not None else []
                current_cost = any(e.source_kind != 'reviewed_seed' and e.event_at is not None
                    and e.event_at <= as_of
                    and (as_of - e.event_at).total_seconds() <= CONFIG.v55.verification_ttl_hours * 3600
                    and (e.expires_at is None or as_of < e.expires_at) for e in cost_facts)
                status['assessment_next_action_cost'] = (cost.model_dump()
                    if isinstance(cost, ActionCost) and current_cost and status['next_action'] == 'complete_known_step' else None)
            statuses.append(status)
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
        'aliases_input_sha256': hashlib.sha256(Path(aliases_path).read_bytes()).hexdigest() if aliases_path is not None else None,
        'alias_counts': imported['alias_report']['counts'] if 'alias_report' in imported else None,
        'held_binding_count': sum(r['state']=='held' for r in imported['alias_report']['binding_resolutions']) if 'alias_report' in imported else 0,
        'runtime_versions': candidate.metadata.runtime_versions,
        'policy_identity': {key: getattr(candidate.metadata, key) for key in
                            ('engine_version', 'ruleset_hash', 'profile_hash', 'trust_registry_hash', 'config_hash')},
        'projection_fingerprint': imported['projection_fingerprint'],
        'decision_fingerprint': decision_fingerprint(candidate), 'import_counts': counts,
        'decision_counts': candidate.counts, 'presentation_counts': digest.display_counts,
        'production_store_called': False, 'delivery_called': False,
        'limitations': ['Reviewed facts, not authenticated mailbox verification',
                       'Canonical aliases assert reviewed historical ownership; exact current retained observations are validated',
                       'Status preview has no delivery deduplication; E remains separate',
                       'Current local profile/policy, not reconstruction of historical policy']}
    (target/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    if handoff_signal:
        from ..handoff_review import write_handoff_review
        receipt = write_handoff_review(candidate, target, previous_state=delivery_state,reissue_plan_path=reissue_plan_path)
        from collections import Counter
        event_dispositions = Counter(state for projection in imported['projections']
                                     for state in projection.dispositions.values())
        receipt['event_accounting'] = {'imports':counts,'projection_dispositions':dict(event_dispositions)}
        (target/'signal-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
        (target/'preview.md').rename(target/'outcome-preview-uncapped.md')
        compact = target/'signal-preview-email.md'
        (target/'preview.md').write_text(compact.read_text(encoding='utf-8') if compact.is_file()
            else 'No newly selected handoff signals. Queued, delivered, held and capped items remain in signal-receipt.json.\n',encoding='utf-8')
        manifest['delivery_called'] = True
        manifest['provider_calls'] = 0
        manifest['handoff_signal_review'] = {'receipt':'signal-receipt.json',
            'provider_calls':receipt['provider_calls'], 'production_store_called':False,
            'previous_state_sha256':receipt['previous_state_sha256']}
        (target/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return target, manifest
