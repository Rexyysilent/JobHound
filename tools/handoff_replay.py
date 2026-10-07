"""Offline candidate audits and uncapped deltas against saved review baselines."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobhound.config import CONFIG
from jobhound.run_context import RunContext, run_scope
from jobhound.trust.registry import load_registry
from jobhound.v41.replay import load_snapshot, replay
from jobhound.v41.review import _write_audit
from jobhound.compact_audit import write_compact_audit
from jobhound.v41.digest import build_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--recompare', action='store_true', help='Rebuild deltas from the existing complete compact audit without rerunning evaluation')
    parser.add_argument('--delivery-copy', type=Path, help='Explicit copied database with cutover-smoke marker; synthetic destinations only')
    parser.add_argument('--resume-delivery', action='store_true', help='Resume the copied-DB preview from an already staged, verified replay')
    args = parser.parse_args()
    # Evaluation is synchronous. No network, provider, or production-store path.
    def denied(*args, **kwargs):
        raise RuntimeError('offline verification forbids network')
    socket.socket.connect = denied
    socket.create_connection = denied
    if args.resume_delivery:
        if not args.delivery_copy:
            parser.error('--resume-delivery requires --delivery-copy')
        from types import SimpleNamespace
        from jobhound.v41.models import RunMetadata
        from jobhound.versions import runtime_versions
        summary = json.loads((args.output / 'summary.json').read_text(encoding='utf-8'))
        if summary['snapshot_sha256'] != hashlib.sha256(args.snapshot.read_bytes()).hexdigest():
            raise ValueError('saved replay snapshot changed')
        if summary['metadata']['runtime_versions'] != runtime_versions():
            raise ValueError('saved replay runtime changed')
        result = SimpleNamespace(metadata=RunMetadata.model_validate(summary['metadata']))
        verify_delivery_copy(result, args.delivery_copy, args.output, stage=False)
        print('Resumed copied-database preview; no evaluation or provider calls.')
        return
    if args.recompare:
        before_data = json.loads(args.baseline.read_text(encoding='utf-8'))
        compact = json.loads((args.output / 'compact.json').read_text(encoding='utf-8'))
        after = {}
        for row in compact['decisions']:
            assessment, decision = row['assessment'], row['decision']
            job = row['job']
            after[row['canonical_id']] = {'id': row['canonical_id'], **job,
                'band': decision['action_band'], 'lifecycle': decision['lifecycle'], 'next_action': decision['next_action'],
                'blockers': assessment['blockers'], 'unresolved': assessment['unresolved'],
                'pay': pay_projection(assessment['selected_pay'], assessment['pay_conflict'])}
        # Concept evidence is unchanged by this increment and absent in the
        # compact schema; the initial complete comparison already checked it.
        compare_and_write(args.output, before_data, after, concepts=False)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    header, _ = load_snapshot(args.snapshot)
    before_data = json.loads(args.baseline.read_text(encoding='utf-8'))
    context = RunContext.capture(config=CONFIG.model_copy(deep=True),
        registry=load_registry(args.registry), as_of=datetime.fromisoformat(header['as_of']),
        workspace=args.output, run_id='handoff-' + args.snapshot.stem)
    with run_scope(context):
        result = replay(args.snapshot)
        digest = build_digest(result, include_all=True)
    if not result.accounting_ok or not digest.accounting_ok:
        raise RuntimeError('candidate accounting failed')
    _write_audit(result, args.output)
    snapshot_hash = hashlib.sha256(args.snapshot.read_bytes()).hexdigest()
    compact_hash = write_compact_audit(result, args.output / 'compact.json',
        snapshot_reference=str(args.snapshot), snapshot_sha256=snapshot_hash)
    (args.output / 'digest.txt').write_text(digest.text, encoding='utf-8')
    def project(item):
        pay = item.assessment.selected_pay
        return {'id': item.canonical.canonical_id, 'title': item.job.title, 'company': item.job.company,
            'url': item.job.url, 'location': item.job.location, 'band': item.decision.action_band.value,
            'lifecycle': item.decision.lifecycle, 'next_action': item.decision.next_action,
            'blockers': item.assessment.blockers, 'unresolved': item.assessment.unresolved,
            'concepts': item.assessment.matched_concepts,
            'pay': pay_projection(pay.model_dump(mode='json') if pay else None, item.assessment.pay_conflict)}
    before = {row['id']: row for row in before_data['decisions']}
    after = {row.canonical.canonical_id: project(row) for row in result.evaluated}
    deltas = deltas_for(before_data, after)
    summary = {'snapshot_sha256': snapshot_hash,
        'baseline_sha256': hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        'metadata': result.metadata.model_dump(mode='json'), 'counts': result.counts,
        'accounting_ok': result.accounting_ok, 'presentation_ok': digest.accounting_ok,
        'display_counts': digest.display_counts, 'compact_sha256': compact_hash,
        'changed_decisions': len(deltas),
        'baseline_registry_hash': before_data['summary'].get('current_registry_hash'),
        'candidate_registry_hash': result.metadata.trust_registry_hash,
        'scope': 'Same saved observations and frozen release policy; current code, explicit local registry and profile. Not a historical send or independent precision estimate.'}
    (args.output / 'changes.json').write_text(json.dumps(deltas, indent=2, ensure_ascii=False), encoding='utf-8')
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    if args.delivery_copy:
        verify_delivery_copy(result, args.delivery_copy, args.output)
    print(json.dumps({k: summary[k] for k in ('counts', 'accounting_ok', 'presentation_ok', 'changed_decisions', 'baseline_registry_hash', 'candidate_registry_hash')}, ensure_ascii=True), flush=True)


def pay_projection(pay, conflict):
    if pay is None:
        return None
    return {'currency': pay['currency'], 'low': pay['amount_low'], 'high': pay['amount_high'],
            'unit': pay['actual_unit'], 'source': pay.get('observation_source_kind'), 'conflict': conflict}


def deltas_for(before_data, after, *, concepts=True):
    before = {row['id']: row for row in before_data['decisions']}
    fields = ('band', 'lifecycle', 'next_action', 'blockers', 'unresolved', 'pay') + (('concepts',) if concepts else ())
    deltas = []
    for identity in sorted(set(before) | set(after)):
        old, new = before.get(identity), after.get(identity)
        changed = [f for f in fields if old is None or new is None or old.get(f) != new.get(f)]
        if changed:
            deltas.append({'id': identity, 'changed_fields': changed,
                'before': old, 'after': new, 'classification': 'needs_evidence_review'})
    return deltas


def compare_and_write(output, before_data, after, *, concepts=True):
    deltas = deltas_for(before_data, after, concepts=concepts)
    (output / 'changes.json').write_text(json.dumps(deltas, indent=2, ensure_ascii=False), encoding='utf-8')
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    summary['changed_decisions'] = len(deltas)
    (output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'changed_decisions': len(deltas), 'recomparison': 'Existing frozen compact audit; equal compensation projection schema.'}))


def verify_delivery_copy(result, database, output, *, stage=True):
    from jobhound.delivery_outbox import Destination
    from jobhound.delivery_transport import DigestTransport
    from jobhound.v41.store import V41Store
    import time
    database = database.resolve()
    if not database.is_file() or not (database.parent / '.jobhound-cutover-smoke').is_file():
        raise ValueError('explicit copied database and smoke marker required')
    production = Path(CONFIG.v41.sidecar_db)
    if not production.is_absolute():
        production = Path(__file__).resolve().parent.parent / production
    if database == production.resolve():
        raise ValueError('production database forbidden')
    store = V41Store(database)
    try:
        store.enable_delivery_production(workspace_id='handoff-replay', profile_id='synthetic-profile', approved=True)
        target = Destination.from_address('email', 'synthetic-sender:synthetic-recipient')
        if stage:
            report = store.record_delivery_run(result, [target], now=time.time(), adopt_legacy=True)
        else:
            row = store.conn.execute('SELECT report FROM delivery_runs WHERE workspace=? AND profile=? AND run_id=?',
                ('handoff-replay', 'synthetic-profile', result.metadata.run_id)).fetchone()
            if row is None:
                raise ValueError('verified replay has not been staged in the copied database')
            report = json.loads(row[0])
        transport = DigestTransport(store.delivery)
        pending = transport.deliverable_intents(target, now=time.time(), limit=100)
        previews = []
        if pending:
            envelope = transport.prepare(target, pending, now=time.time(), summary_run_id=result.metadata.run_id)
            preview = transport.inspect(envelope)
            previews.append({'status': preview['status'], 'body_hash': preview['body_hash'],
                'body_bytes': len(preview['body'].encode()), 'replacement_characters': preview['body'].count('\ufffd')})
        if not previews:
            raise RuntimeError('copied-database check produced no fresh deliverable preview')
        checks = {'database_integrity': store.conn.execute('PRAGMA integrity_check').fetchone()[0],
            'foreign_key_errors': len(store.conn.execute('PRAGMA foreign_key_check').fetchall()),
            'provider_calls': 0, 'previews': previews, 'report': report,
            'runtime_versions': result.metadata.runtime_versions}
        if checks['database_integrity'] != 'ok' or checks['foreign_key_errors']:
            raise RuntimeError('copied database integrity failed')
        (output / 'delivery-copy.json').write_text(json.dumps(checks, indent=2, ensure_ascii=False), encoding='utf-8')
    finally:
        store.close()


if __name__ == '__main__':
    main()
