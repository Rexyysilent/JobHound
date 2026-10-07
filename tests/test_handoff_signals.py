"""Engine -> disposable durable queue -> bounded preview and synthetic receipts."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

from jobhound.config import CONFIG
from jobhound.delivery_outbox import evaluated_row
from jobhound.delivery_transport import DigestTransport, render_envelope
from jobhound.handoff_signal import handoff_material_projection, selected_outcome_facts
from jobhound.review_audit import material_projection
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.store import V41Store
from test_v55_action_policy import NOW, observation
from test_v57_outcomes import setup, event
from test_assessment_revisions import ready as assessment_ready, scoped, OLD
from test_work_outcomes import ready as work_ready, work_event, OTHER, WORK
from test_v6_delivery_recovery import EMAIL, TELEGRAM, store, start, finish, count


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def engine(records=(), rows=()):
    result, binding = setup(*rows)
    changed, imported = preview_outcomes(result, records(binding) if callable(records) else records,
                                        [binding], as_of=NOW)
    return changed, binding, imported


def material(result):
    return handoff_material_projection(evaluated_row(result.evaluated[0]))


def stage(store, result, targets=(EMAIL,), **kwargs):
    return store.record_delivery_run(result, targets, now=100, handoff_signal=True, **kwargs)


def later(result):
    result = result.model_copy(deep=True)
    result.metadata.run_id += '-later'
    result.metadata.as_of += timedelta(minutes=1)
    return result


def test_inactive_step_changes_do_not_create_a_new_action_fingerprint(store):
    first, binding, _ = engine(assessment_ready)
    second, _, _ = engine(lambda b: assessment_ready(b) + [scoped(b, 'old-done', 'assessment_state', 'completed', OLD)])
    assert material(first) == material(second)
    assert material_projection(evaluated_row(first.evaluated[0])) != material_projection(evaluated_row(second.evaluated[0]))
    stage(store, first); finish(store, start(store))
    report = stage(store, later(second))
    assert report['destinations'][0]['counts']['already_recorded'] == 1
    assert count(store, 'delivery_revisions') == count(store, 'delivery_intents') == 1


def test_inactive_work_receipts_and_conflicts_do_not_create_a_new_action_fingerprint():
    first, _, _ = engine(work_ready)
    second, _, _ = engine(lambda b: work_ready(b) + [
        work_event(b, 'old-receipt', 'collected_payment', dict(amount='200',currency='USD',reference='old'), OTHER,
                   actor='user',source_kind='user_report'),
        work_event(b, 'old-block', 'work_access', 'blocked', OTHER),
        work_event(b, 'old-open', 'work_access', 'accessible', OTHER,actor='user',source_kind='user_report')])
    assert material(first) == material(second)
    assert second.evaluated[0].canonical.outcome_projection.conflicts


def test_material_cost_scope_deadline_and_prerequisite_changes_are_retained():
    first, binding, _ = engine(assessment_ready)
    costs = assessment_ready(binding) + [scoped(binding, 'cost', 'action_cost',
                dict(minutes=45,cash=0,currency='USD',basis='provider_estimate'))]
    second, _ = preview_outcomes(setup()[0], costs, [binding], as_of=NOW)
    assert material(first) != material(second)
    row = evaluated_row(first.evaluated[0]); changed = deepcopy(row)
    changed['assessment']['account_state']['deadline'] = (NOW+timedelta(hours=12)).isoformat()
    assert handoff_material_projection(row) != handoff_material_projection(changed)
    third, _, _ = engine(lambda b: [e for e in assessment_ready(b) if e['event_id'] != 'checks'])
    assert material(first) != material(third)


def test_capture_format_scores_event_ids_and_monetary_decimal_format_are_immaterial():
    first, _, _ = engine(work_ready)
    row = evaluated_row(first.evaluated[0]); changed = deepcopy(row)
    changed['job']['title'] = '  '+row['job']['title'].replace(' ', '  ')+' '
    changed['decision']['priority_score'] += 123
    changed['decision']['next_step'] = 'Cosmetic instruction wording'
    projection = changed['outcome_projection']
    for e in projection['events']:
        old = e['event_id']; e['event_id'] += '.recaptured'
        e['observed_at'] = NOW.isoformat()
        for ids in projection['current'].values():
            if old in ids: ids[ids.index(old)] = e['event_id']
        projection['dispositions'][e['event_id']] = projection['dispositions'].pop(old)
        if e['predicate'] == 'work_terms': e['value']['amount'] = '10.0000'
    changed['assessment']['account_state']['_reviewed_work_action']['event_ids'] = ['different']
    changed['assessment']['account_state']['_reviewed_work_action']['terms']['amount'] = '10.000'
    assert handoff_material_projection(row) == handoff_material_projection(changed)


def test_public_buyer_scope_revision_is_material_without_private_work_claim():
    from test_community_revisions import native, update
    _, _, first = native()
    _, _, changed = native(update('The scope now requires self-hosted n8n migration.'))
    assert handoff_material_projection(evaluated_row(first)) != handoff_material_projection(evaluated_row(changed))
    assert changed.canonical.outcome_projection is None
    assert handoff_material_projection(evaluated_row(changed))['buyer_terms']


def many(count=9):
    rows = []
    for i in range(count):
        row = observation('one' if i == 0 else 'item-'+str(i))
        row.job.company = 'Employer '+str(i)
        rows.append(row)
    return rows


def test_joint_action_caps_reserve_discovery_and_retain_exhaustive_audit(store, monkeypatch):
    rows = many()
    result = evaluate_observations(rows, as_of=NOW)
    from jobhound.v41.outcomes import OutcomeBinding, OutcomeScope
    bindings, records = [], []
    for item in result.evaluated[:7]:
        scope = OutcomeScope(provider='example',portal='contractors',role=item.canonical.canonical_id,
                             account='local-1',attempt='attempt-1')
        binding = OutcomeBinding(scope=scope,canonical_id=item.canonical.canonical_id,
            observation_id=item.canonical.observations[0].observation_id,role_url=item.job.url,reviewed=True)
        bindings.append(binding)
        for record in work_ready(binding):
            record['event_id'] = item.canonical.canonical_id[:12]+'.'+record['event_id']
            records.append(record)
    changed, _ = preview_outcomes(result, records, bindings, as_of=NOW)
    assert sum(item.decision.next_action == 'start_allocated_task' for item in changed.evaluated) == 7
    report = stage(store, changed)['destinations'][0]
    assert report['counts']['selected_cards'] == 5
    assert report['counts']['overflow_cards'] == 4
    reserved = report['reservation']['canonical_id']
    assert next(i for i in changed.evaluated if i.canonical.canonical_id == reserved).canonical.outcome_projection is None
    assert len(report['dispositions']) == 9
    assert report['action_accounting']['total'] == sum(report['action_accounting']['dispositions'].values()) == 9
    assert sum(report['counts'][k] for k in report['counts'] if k != 'total') == 9


def test_no_weak_discovery_filler_and_two_verify_cap(store):
    rows = many(5)
    for row in rows: row.vacancy_state = 'unknown'
    result = evaluate_observations(rows, as_of=NOW)
    report = stage(store, result)['destinations'][0]
    assert report['counts']['selected_cards'] == 2
    assert report['reservation']['canonical_id'] is None
    assert set(report['dispositions'].values()) == {'displayed_verify', 'verify_cap'}


def test_lower_transport_cap_and_zero_caps_are_respected(store):
    result = evaluate_observations(many(5), as_of=NOW)
    assert stage(store, result, card_cap=1)['destinations'][0]['counts']['selected_cards'] == 1
    second = later(result)
    assert stage(store, second, card_cap=0)['destinations'][0]['counts']['selected_cards'] == 0


def test_public_closure_preserves_exact_allocated_work_as_action_card(store):
    row = observation(vacancy_state='explicitly_closed')
    changed, _, _ = engine(work_ready, [row])
    item = changed.evaluated[0]
    assert item.decision.action_band.value == 'primary' and item.decision.next_action == 'start_allocated_task'
    report = stage(store, changed)['destinations'][0]
    assert report['counts']['selected_cards'] == 1 and report['counts']['selected_status'] == 0
    payload = store.delivery.inspect()[0]['payload']
    text = render_envelope([dict(payload=payload)], now=100)
    assert 'Apply / act first' in text and item.decision.next_step in text
    assert json.loads(payload)['decision']['action_band'] == item.decision.action_band.value


def test_exact_rejection_is_compact_once_per_destination_and_not_a_job_card(store):
    first, binding = setup()
    changed, _ = preview_outcomes(first, [event(binding,'reject','application_state','rejected')], [binding], as_of=NOW)
    report = stage(store, changed, targets=(EMAIL, TELEGRAM))
    assert all(d['counts']['selected_status'] == 1 and d['counts']['selected_cards'] == 0 for d in report['destinations'])
    finish(store, start(store, TELEGRAM))
    second = stage(store, later(changed), targets=(EMAIL, TELEGRAM))
    assert all(d['counts']['already_recorded'] == 1 for d in second['destinations'])
    assert count(store, 'delivery_baselines') == 1
    pending = [r for r in store.delivery.inspect() if r['channel']=='email']
    body = render_envelope(pending, now=100)
    assert body.count('Status updates') == 1 and body.count('application state: rejected') == 1
    assert 'Worth a bounded check' not in body and len(body) < 700


def test_three_status_limit_and_overflow_remain_in_full_audit(store):
    result = evaluate_observations(many(6), as_of=NOW)
    stage(store, result)
    for target in (EMAIL,):
        while store.delivery.claim_next(target,now=101) is not None:
            # Claim above is used only to stop this loop; finish via its token.
            row = next(r for r in store.delivery.inspect() if r['status']=='leased')
            store.delivery.begin_send(row['id'],row['token'],now=101)
            finish(store,row)
    closed = later(result)
    for item in closed.evaluated:
        item.decision.lifecycle='closed';item.decision.next_action='await_change'
    report = stage(store, closed)['destinations'][0]
    assert report['counts']['selected_status'] == 3 and report['counts']['overflow_status'] == 2
    assert report['counts']['ineligible'] == 1
    assert len(report['dispositions']) == 6
    assert 'status_cap' in report['dispositions'].values()


def test_schema_only_migration_uses_accepted_payload_without_faking_a_receipt(store):
    result, _ = setup()
    store.record_delivery_run(result, (EMAIL, TELEGRAM), now=100)
    finish(store, start(store, TELEGRAM))
    prior = store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0]
    report = stage(store, later(result), targets=(EMAIL, TELEGRAM))
    assert report['destinations'][1]['migrations'] == {'adopted':1}
    assert report['destinations'][1]['counts']['already_recorded'] == 1
    assert report['destinations'][0]['counts']['selected_cards'] == 1
    assert count(store, 'delivery_material_adoptions') == 1
    assert count(store, 'delivery_baselines') == 1
    assert sum(row['status']=='accepted' for row in store.delivery.inspect()) == 1
    assert store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0] != prior
    with pytest.raises(sqlite3.IntegrityError,match='append-only'):
        store.conn.execute('DELETE FROM delivery_material_adoptions')
    store.conn.rollback()


def test_missing_migration_proof_holds_baseline_without_consuming_it(store):
    result, _ = setup()
    store.record_delivery_run(result, (EMAIL,), now=100)
    finish(store, start(store))
    # Simulate an imported historical receipt without its immutable intent.
    store.conn.execute("UPDATE delivery_intents SET status='superseded'")
    store.conn.commit()
    baseline = store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0]
    report = stage(store, later(result))['destinations'][0]
    assert report['counts']['policy_review'] == 1 and report['counts']['selected_cards'] == 0
    assert report['dispositions'] == {result.evaluated[0].canonical.canonical_id:'migration_review_required'}
    assert store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0] == baseline


def test_partial_channels_preview_and_failed_send_cannot_consume_baseline(store):
    result, _ = setup(); report = stage(store, result, targets=(EMAIL,TELEGRAM))
    transport = DigestTransport(store.delivery)
    transport.prepare(EMAIL,report['destinations'][0]['intent_ids'],now=100)
    assert count(store,'delivery_baselines') == 0
    finish(store,start(store,TELEGRAM))
    # The preview owns email intents; it is deliberately never dispatched.
    assert count(store,'delivery_baselines') == 1
    assert store.conn.execute('SELECT channel FROM delivery_baselines').fetchone()[0] == 'telegram'


def test_uncertain_send_and_persistence_failure_recover_without_auto_resend(store):
    result, _ = setup();stage(store,result);claim=start(store)
    store.conn.execute("CREATE TRIGGER fail_baseline BEFORE INSERT ON delivery_baselines BEGIN SELECT RAISE(ABORT,'crash'); END")
    store.conn.commit()
    with pytest.raises(sqlite3.IntegrityError,match='crash'):finish(store,claim)
    assert count(store,'delivery_baselines') == 0
    reopened=V41Store(store.path)
    box=reopened.enable_delivery_review(review_root=store.path.parent,workspace_id='fixture',profile_id='person')
    assert box.claim_next(EMAIL,now=200) is None
    assert box.inspect()[0]['status']=='uncertain'
    reopened.close()


def test_production_mode_cannot_enter_handoff_or_adopt_channel_only_legacy(store):
    result, _ = setup()
    store.enable_delivery_production(workspace_id='fixture',profile_id='person',approved=True)
    with pytest.raises(ValueError,match='disposable'):stage(store,result)
    assert count(store,'runs') == 0
    store.enable_delivery_review(review_root=store.path.parent,workspace_id='fixture',profile_id='person')
    with pytest.raises(ValueError,match='channel-only'):stage(store,result,adopt_legacy=True)


def test_rollback_requires_original_database_and_retains_original_receipts(store,tmp_path):
    result, _ = setup();store.record_delivery_run(result,(EMAIL,),now=100);finish(store,start(store))
    backup=sqlite3.connect(tmp_path/'before.sqlite');store.conn.backup(backup);backup.close()
    stage(store,later(result))
    with pytest.raises(ValueError,match='pre-handoff'):
        store.record_delivery_run(later(later(result)),(EMAIL,),now=100)
    restored=V41Store(tmp_path/'before.sqlite')
    restored.enable_delivery_review(review_root=tmp_path,workspace_id='fixture',profile_id='person')
    report=restored.record_delivery_run(later(result),(EMAIL,),now=100)
    assert report['destinations'][0]['counts']['already_recorded']==1
    assert count(restored,'delivery_intents')==1
    restored.close()


def test_envelope_cannot_combine_multiple_runs_into_an_over_limit_digest(store):
    result=evaluate_observations(many(9),as_of=NOW)
    report=stage(store,result)['destinations'][0]
    payload=store.delivery.inspect()[0]['payload']
    with pytest.raises(ValueError,match='exceeds bounded'):
        render_envelope([dict(payload=payload)]*6,now=100)


def test_identity_alias_keeps_delivered_destination_baseline(store):
    result, _ = setup();stage(store,result);finish(store,start(store))
    identity=result.evaluated[0].canonical.canonical_id
    store.delivery.register_alias(identity,'reviewed-new-id',evidence='synthetic:exact-identity-review')
    migrated=later(result)
    migrated.evaluated[0].canonical.canonical_id='reviewed-new-id'
    report=stage(store,migrated)['destinations'][0]
    assert report['counts']['already_recorded']==1
    assert count(store,'delivery_revisions')==count(store,'delivery_intents')==1


def test_real_offline_cli_prepares_new_review_state_and_copies_previous_state(tmp_path,monkeypatch):
    import socket
    from jobhound import cli
    from jobhound.store import Store
    from jobhound.v41.replay import write_snapshot
    from jobhound.v41.engine import evaluate_raw
    from test_v6_continuity import raw
    def forbidden(*args,**kwargs):pytest.fail('network or production store called')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(Store,'__init__',forbidden)
    original=V41Store.__init__
    def reviewed_only(self,path=None):
        assert path is not None and tmp_path in __import__('pathlib').Path(path).resolve().parents
        original(self,path)
    monkeypatch.setattr(V41Store,'__init__',reviewed_only)
    inputs=[raw('Pay USD 20 per hour.')]
    result=evaluate_raw(inputs,as_of=NOW)
    snapshot=write_snapshot(inputs,result,directory=tmp_path)
    from jobhound.v41.outcomes import OutcomeBinding,OutcomeScope
    item=result.evaluated[0]
    binding=OutcomeBinding(scope=OutcomeScope(provider='example',portal='main',role='one',account='local',attempt='one'),
        canonical_id=item.canonical.canonical_id,observation_id=item.canonical.observations[0].observation_id,
        role_url=item.job.url,reviewed=True)
    bindings=tmp_path/'bindings.jsonl';bindings.write_text(binding.model_dump_json()+'\n',encoding='utf-8')
    events=tmp_path/'events.jsonl';events.write_text(''.join(json.dumps(e)+'\n' for e in work_ready(binding)),encoding='utf-8')
    def invoke(directory,*extra):
        args=cli.build_parser().parse_args(['review','outcomes',str(snapshot),'--events',str(events),
            '--bindings',str(bindings),'--as-of',NOW.isoformat(),'--output-dir',str(directory),'--handoff-signal',*extra])
        args.func(args)
        return json.loads((directory/'signal-receipt.json').read_text(encoding='utf-8'))
    first=tmp_path/'first';receipt=invoke(first)
    assert receipt['provider_calls']==0 and receipt['production_store_called'] is False
    assert len(receipt['previews'])==2 and all(p['status']=='pending' and p['replacement_characters']==0 for p in receipt['previews'])
    assert receipt['baseline_count']==0 and receipt['event_accounting']['imports']['accepted']==8
    assert 'Apply / act first' in (first/'preview.md').read_text(encoding='utf-8')
    second=tmp_path/'second';again=invoke(second,'--previous-events',str(first/'events.jsonl'),
                                       '--delivery-state',str(first/'delivery.sqlite'))
    assert again['previews']==[] and again['previous_state_sha256']
    assert again['event_accounting']['imports']['duplicate']==8
    assert all(d['counts']['already_recorded']==1 for d in again['selection']['destinations'])
    assert (first/'outcome-preview-uncapped.md').is_file()


def test_unmarked_or_production_delivery_state_cannot_be_copied(tmp_path,monkeypatch):
    from jobhound.handoff_review import validate_review_state,MARKER
    source=tmp_path/'delivery.sqlite';sqlite3.connect(source).close()
    with pytest.raises(ValueError,match='marked_disposable'):validate_review_state(source)
    (tmp_path/MARKER).write_text('disposable-handoff-review/v1\n',encoding='utf-8')
    monkeypatch.setattr(CONFIG.v41,'sidecar_db',str(source))
    with pytest.raises(ValueError,match='marked_disposable'):validate_review_state(source)


@pytest.mark.parametrize('failure',['not_sent','uncertain'])
def test_handoff_fake_transport_preserves_partial_channel_receipts(store,failure):
    from jobhound.notify.receipt import DeliveryReceipt
    from test_v6_transport import Sender,send
    result, _, _=engine(work_ready)
    report=stage(store,result,targets=(EMAIL,TELEGRAM))
    transport=DigestTransport(store.delivery)
    email=transport.prepare(EMAIL,report['destinations'][0]['intent_ids'],now=101)
    telegram=transport.prepare(TELEGRAM,report['destinations'][1]['intent_ids'],now=101)
    sender=Sender(EMAIL,[DeliveryReceipt(failure,'synthetic-failure')])
    assert send(transport,email,sender)==('pending' if failure=='not_sent' else failure)
    assert send(transport,telegram,Sender(TELEGRAM))=='accepted'
    assert count(store,'delivery_baselines')==1
    assert store.conn.execute('SELECT channel FROM delivery_baselines').fetchone()[0]=='telegram'
    second=stage(store,later(result),targets=(EMAIL,TELEGRAM))
    assert all(d['counts']['already_recorded']==1 for d in second['destinations'])
    assert len(sender.calls)==1


def test_invoice_and_receipt_status_keeps_native_claims_and_does_not_forecast_income(store):
    result, binding, _=engine(lambda b: work_ready(b)+[
        work_event(b,'invoice','invoice',dict(amount='100',currency='USD',reference='invoice'),actor='buyer'),
        work_event(b,'receipt','collected_payment',dict(amount='35',currency='INR',reference='receipt'),actor='user',source_kind='user_report')])
    report=stage(store,result)['destinations'][0]
    assert report['counts']['selected_cards']==0 and report['counts']['selected_status']==1
    body=render_envelope(store.delivery.inspect(),now=100)
    assert 'invoice claim: USD 100' in body and 'recipient-reported receipt: INR 35' in body
    assert 'Worth a bounded check' not in body and 'Apply / act first' not in body


def test_changed_cli_preview_holds_prior_uncertain_envelope_without_sending(tmp_path):
    from jobhound.handoff_review import write_handoff_review,DESTINATIONS
    from jobhound.notify.receipt import DeliveryReceipt
    from test_v6_transport import Sender,send
    result, binding, _=engine(work_ready)
    first=tmp_path/'first';first.mkdir();initial=write_handoff_review(result,first)
    db=V41Store(first/'delivery.sqlite')
    db.enable_delivery_review(review_root=first,workspace_id='handoff-review',profile_id='review-profile')
    transport=DigestTransport(db.delivery)
    envelope=initial['previews'][0]['envelope']
    assert send(transport,envelope,Sender(DESTINATIONS[0],[DeliveryReceipt('uncertain','synthetic-unknown')]),
                now=NOW.timestamp()+1)=='uncertain'
    db.close()
    changed, _=preview_outcomes(setup()[0],work_ready(binding)+[work_event(binding,'done','work_state','submitted',
        actor='user',source_kind='user_report')],[binding],as_of=NOW)
    second=tmp_path/'second';second.mkdir()
    receipt=write_handoff_review(later(changed),second,previous_state=first/'delivery.sqlite')
    assert receipt['provider_calls']==0 and receipt['preparation_holds']
    assert receipt['baseline_count']==0
