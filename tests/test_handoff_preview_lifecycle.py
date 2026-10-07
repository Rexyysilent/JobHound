"""Closed-copy preview retirement never consumes a provider receipt."""
from copy import deepcopy
import hashlib
import json

import pytest

from jobhound.handoff_review import write_handoff_review,retire_obsolete_previews,DESTINATIONS
from jobhound.delivery_outbox import transaction,evaluated_row
from jobhound.delivery_transport import DigestTransport
from jobhound.notify.receipt import DeliveryReceipt
from jobhound.review_audit import material_projection
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.store import V41Store
from test_handoff_signals import engine,later,stage,many,policy
from test_work_outcomes import ready,work_event
from test_v57_outcomes import setup
from test_v55_action_policy import NOW
from test_v6_transport import Sender,send
from test_v6_delivery_recovery import store,EMAIL,TELEGRAM,start,finish


def changed_work(binding):
    changed,_=preview_outcomes(setup()[0],ready(binding)+[
        work_event(binding,'done','work_state','submitted',actor='user',source_kind='user_report')],
        [binding],as_of=NOW)
    return later(changed)


def open_review(root):
    db=V41Store(root/'delivery.sqlite')
    db.enable_delivery_review(review_root=root,workspace_id='handoff-review',profile_id='review-profile')
    return db,DigestTransport(db.delivery)


def test_changed_preview_retires_wholly_obsolete_draft_and_retains_immutable_history(tmp_path):
    first=tmp_path/'first';first.mkdir()
    result,binding,_=engine(ready)
    prior=write_handoff_review(result,first)
    db,transport=open_review(first)
    historic={p['envelope']:transport.inspect(p['envelope']) for p in prior['previews']}
    db.close()
    original_hash=hashlib.sha256((first/'delivery.sqlite').read_bytes()).hexdigest()
    second=tmp_path/'second';second.mkdir()
    receipt=write_handoff_review(changed_work(binding),second,previous_state=first/'delivery.sqlite')
    assert not receipt['preparation_holds'] and len(receipt['previews'])==2
    assert all(r['reason']=='retired_obsolete_unattempted_preview' for r in receipt['preview_lifecycle'])
    assert receipt['provider_calls']==receipt['baseline_count']==0
    assert hashlib.sha256((first/'delivery.sqlite').read_bytes()).hexdigest()==original_hash
    db,transport=open_review(second)
    for identity,original in historic.items():
        retired=transport.inspect(identity)
        assert retired['status']=='cancelled'
        assert retired['body']==original['body'] and retired['intents']==original['intents']
        assert retired['parts']==original['parts']
    assert db.conn.execute("SELECT count(*) FROM delivery_attempt_events WHERE event='envelope_cancelled'").fetchone()[0]==2
    db.close()
    third=tmp_path/'third';third.mkdir()
    restarted=write_handoff_review(later(changed_work(binding)),third,previous_state=second/'delivery.sqlite')
    assert restarted['previews']==[] and restarted['preview_lifecycle']==[]
    assert restarted['baseline_count']==0


@pytest.mark.parametrize('outcome',['not_sent','uncertain','permanent_failure'])
def test_any_provider_attempt_is_retained_as_a_hold_without_recomposition(tmp_path,outcome):
    first=tmp_path/'first';first.mkdir()
    result,binding,_=engine(ready);receipt=write_handoff_review(result,first)
    db,transport=open_review(first)
    old=receipt['previews'][0]['envelope']
    send(transport,old,Sender(DESTINATIONS[0],[DeliveryReceipt(outcome,'synthetic-attempt')]),now=NOW.timestamp()+1)
    before=transport.inspect(old);db.close()
    second=tmp_path/'second';second.mkdir()
    next_receipt=write_handoff_review(changed_work(binding),second,previous_state=first/'delivery.sqlite')
    assert next_receipt['provider_calls']==next_receipt['baseline_count']==0
    assert any(r['envelope']==old and r['reason']=='provider_attempt_or_receipt_history' for r in next_receipt['preview_lifecycle'])
    db,transport=open_review(second)
    assert transport.inspect(old)==before
    assert all(r['envelope']!=old for r in next_receipt['previews'])
    db.close()


def test_retirement_is_review_only_and_rolls_back_if_event_persistence_fails(store):
    result,binding,_=engine(ready);report=stage(store,result)
    transport=DigestTransport(store.delivery)
    old=transport.prepare(EMAIL,report['destinations'][0]['intent_ids'],now=100)
    before=transport.inspect(old)
    stage(store,changed_work(binding))
    store.delivery._handoff_review_enabled=False
    with pytest.raises(ValueError,match='disposable'):retire_obsolete_previews(transport,[EMAIL],now=100)
    store.delivery._handoff_review_enabled=True
    store.conn.execute("CREATE TRIGGER retire_fail BEFORE INSERT ON delivery_attempt_events WHEN NEW.event='envelope_cancelled' BEGIN SELECT RAISE(ABORT,'synthetic-crash'); END")
    store.conn.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError,match='synthetic-crash'):retire_obsolete_previews(transport,[EMAIL],now=100)
    assert transport.inspect(old)==before


def test_mixed_draft_current_members_remain_frozen_with_explicit_reason(store):
    result,binding,_=engine(ready, many(2));report=stage(store,result)
    transport=DigestTransport(store.delivery)
    old=transport.prepare(EMAIL,report['destinations'][0]['intent_ids'],now=100)
    original=transport.inspect(old)
    changed,_=preview_outcomes(result,ready(binding)+[work_event(binding,'done','work_state','submitted',
        actor='user',source_kind='user_report')],[binding],as_of=NOW)
    stage(store,later(changed))
    receipt=retire_obsolete_previews(transport,[EMAIL],now=100)
    assert receipt[0]['reason']=='mixed_current_frozen_members_require_explicit_reissue'
    assert transport.inspect(old)==original
    assert store.conn.execute('SELECT count(*) FROM delivery_baselines').fetchone()[0]==0


@pytest.mark.parametrize('payload',[[],{'outcome_projection':{}},{'outcome_projection':None},'malformed'])
def test_invalid_legacy_payload_is_per_destination_hold_and_other_channel_still_stages(store,payload):
    result,_=setup();item=result.evaluated[0]
    with transaction(store.conn):
        revision=store.delivery.revise(item.canonical.canonical_id,material_projection(evaluated_row(item)),{},
            run_id=result.metadata.run_id,as_of=NOW.isoformat(),now=100)
        store.delivery.enqueue(revision,EMAIL,payload,now=100)
    finish(store,start(store))
    baseline=store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0]
    receipt=stage(store,later(result),targets=(EMAIL,TELEGRAM))
    assert receipt['destinations'][0]['counts']['policy_review']==1
    assert receipt['destinations'][1]['counts']['selected_cards']==1
    assert store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0]==baseline
    assert store.conn.execute('SELECT count(*) FROM delivery_material_adoptions').fetchone()[0]==0


@pytest.mark.parametrize('material',[[],None,'legacy-scalar'])
def test_invalid_legacy_material_holds_without_inventing_a_new_audience(store,material):
    result,_=setup();identity=result.evaluated[0].canonical.canonical_id
    with transaction(store.conn):
        revision=store.delivery.revise(identity,material,{},run_id=result.metadata.run_id,as_of=NOW.isoformat(),now=100)
        store.delivery.enqueue(revision,EMAIL,evaluated_row(result.evaluated[0]),now=100)
    finish(store,start(store))
    receipt=stage(store,later(result))
    assert receipt['destinations'][0]['counts']['policy_review']==1
    assert receipt['destinations'][0]['intent_ids']==[]
