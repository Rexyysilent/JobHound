"""Prepare bounded offline signals in a marked, disposable review database."""
from pathlib import Path
import hashlib
import json
import sqlite3

from .delivery_outbox import Destination
from .delivery_transport import DigestTransport
from .delivery_outbox import transaction
from .v41.store import V41Store

MARKER = '.jobhound-signal-review'
DESTINATIONS = (Destination.from_address('email', 'review:synthetic@example.test'),
                Destination.from_address('telegram', 'review:synthetic-chat'))


def validate_review_state(path):
    from .config import CONFIG
    source = Path(path).resolve()
    root = Path(__file__).resolve().parents[1]
    production = Path(CONFIG.v41.sidecar_db)
    production = production if production.is_absolute() else root / production
    if (source == production.resolve() or not source.is_file() or source.name != 'delivery.sqlite'
            or not (source.parent / MARKER).is_file()
            or (source.parent / MARKER).read_text(encoding='utf-8') != 'disposable-handoff-review/v1\n'
            or any(Path(str(source)+suffix).exists() for suffix in ('-wal','-shm','-journal'))):
        raise ValueError('marked_disposable_delivery_state_required')
    return source


def retire_obsolete_previews(transport, destinations, *, now):
    """Retire wholly obsolete, provably unattempted drafts in review state only.

    Mixed drafts keep their current members frozen for explicit reissue review.
    No attempted, accepted or uncertain part is ever automatically recomposed.
    Bodies, historic membership and receipt evidence remain immutable.
    """
    if not getattr(transport.box,'_handoff_review_enabled',False):
        raise ValueError('disposable handoff review required')
    ledger=[]
    with transaction(transport.conn):
        for destination in destinations:
            envelopes=transport.conn.execute('''SELECT id FROM delivery_envelopes
                WHERE workspace=? AND profile=? AND channel=? AND destination=?
                AND status IN ('pending','uncertain','leased','failed') ORDER BY created,id''',
                (transport.box.workspace,transport.box.profile,destination.channel,destination.key)).fetchall()
            for (identity,) in envelopes:
                envelope=transport._owned(identity)
                items=transport._items(identity)
                if not items or transport._current(identity):
                    continue
                # This local helper owns handoff previews only. An older legacy
                # or operational draft stays in its original delivery workflow.
                try:
                    payloads=[json.loads(item['payload']) for item in items]
                except (ValueError,TypeError):
                    payloads=[None]
                try:
                    transport.prove_unsent_draft(identity,allowed_statuses=('pending',))
                    proven=True
                except ValueError:
                    proven=False
                if any(not isinstance(p,dict) or p.get('signal_schema')!='jobhound-handoff-signal/v1' for p in payloads):
                    reason='different_preview_workflow'
                elif not proven:
                    reason='provider_attempt_or_receipt_history'
                elif any(item['revision']==transport.box.current_revision(item['subject']) for item in items):
                    reason='mixed_current_frozen_members_require_explicit_reissue'
                else:
                    transport._stale(identity,now)
                    reason='retired_obsolete_unattempted_preview'
                ledger.append(dict(envelope=identity,channel=destination.channel,reason=reason,
                    intents=[item['id'] for item in items],body_sha256=envelope['body_hash'],
                    current_intent_ids=[item['id'] for item in items if item['revision']==transport.box.current_revision(item['subject'])],
                    obsolete_intent_ids=[item['id'] for item in items if item['revision']!=transport.box.current_revision(item['subject'])]))
    return ledger


def write_handoff_review(result, target, *, previous_state=None,reissue_plan_path=None):
    """Only preview/freeze. Provider dispatch is absent from this entrypoint."""
    target = Path(target).resolve()
    database = target / 'delivery.sqlite'
    if database.exists() or (target / MARKER).exists():
        raise ValueError('new_handoff_database_required')
    source_hash = None
    reissue_plan = None
    if reissue_plan_path is not None and previous_state is None:
        raise ValueError('reissue_plan_requires_previous_disposable_state')
    if previous_state is not None:
        source = validate_review_state(previous_state)
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if reissue_plan_path is not None:
            from .handoff_reissue import read_reissue_plan
            reissue_plan = read_reissue_plan(reissue_plan_path,source_hash)
        origin = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        copy = sqlite3.connect(database)
        try:
            origin.backup(copy)
        finally:
            origin.close(); copy.close()
        if validate_review_state(source)!=source or hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
            raise ValueError('review_state_changed_while_copying')
    (target / MARKER).write_text('disposable-handoff-review/v1\n', encoding='utf-8')
    store = V41Store(database)
    try:
        box = store.enable_delivery_review(review_root=target,workspace_id='handoff-review',profile_id='review-profile')
        now = result.metadata.as_of.timestamp()
        transport = DigestTransport(box)
        previews = []
        preparation_holds = []
        frozen = []
        reissue = None
        # Run/revisions, review generations and both frozen destinations share
        # one transaction. Renderer/insert failures cannot half-abandon a draft.
        with transaction(store.conn):
            report = store.record_delivery_run(result,DESTINATIONS,now=now,handoff_signal=True,card_cap=7,status_cap=3)
            preview_lifecycle = retire_obsolete_previews(transport,DESTINATIONS,now=now)
            if reissue_plan is not None:
                from .handoff_reissue import prepare_reissue_selection
                reissue = prepare_reissue_selection(transport,result,report,reissue_plan,destinations=DESTINATIONS,now=now)
                ledgers = reissue['destinations']
            else:
                ledgers = report['destinations']
            for ledger in ledgers:
                target_destination = next(d for d in DESTINATIONS if (d.channel,d.key)==(ledger['channel'],ledger['destination']))
                # Exact current-run IDs plus only explicitly requested recovery.
                requested = ledger['intent_ids']
                selected = [i for i in requested if transport._preparable(box._owned(i),target_destination,now)]
                preparation_holds.extend(dict(channel=ledger['channel'],intent=identity,
                    reason='held_reserved_or_unavailable') for identity in requested if identity not in selected)
                preparation_holds.extend(dict(channel=ledger['channel'],**hold) for hold in ledger.get('holds',[]))
                if selected:
                    envelope = transport.prepare(target_destination,selected,now=now)
                    frozen.append(transport.inspect(envelope))
        for info in frozen:
            body = info['body'];name = 'signal-preview-' + info['channel'] + '.md'
            (target / name).write_text(body+'\n',encoding='utf-8')
            previews.append(dict(channel=info['channel'],envelope=info['id'],status=info['status'],
                body_sha256=hashlib.sha256(body.encode()).hexdigest(),body_bytes=len(body.encode()),
                replacement_characters=body.count('\ufffd'),file=name))
        integrity = store.conn.execute('PRAGMA integrity_check').fetchone()[0]
        fk = store.conn.execute('PRAGMA foreign_key_check').fetchall()
        if integrity != 'ok' or fk:
            raise ValueError('handoff_database_integrity_failed')
        receipt = dict(mode='disposable_handoff_signals_v1',runtime_versions=result.metadata.runtime_versions,
            previous_state_sha256=source_hash,selection=report,previews=previews,
            preparation_holds=preparation_holds,
            preview_lifecycle=preview_lifecycle,
            reviewed_reissue=reissue,
            database_integrity=integrity,foreign_key_errors=len(fk),provider_calls=0,
            production_store_called=False,baseline_count=store.conn.execute('SELECT count(*) FROM delivery_baselines').fetchone()[0],
            limitations=['Synthetic review destinations; no real recipient mapping',
                         'A prepared envelope is not delivery',
                         'Restore pre-handoff database together with previous code for rollback',
                         'Reissue requires a hash-bound explicit plan; attempted/uncertain drafts require receipt resolution'])
        (target/'signal-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
        return receipt
    finally:
        store.close()
