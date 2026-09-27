"""Disposable SQLite and synthetic senders only. No mailbox/provider calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import sqlite3

import pytest

from jobhound.config import CONFIG
from jobhound.delivery_outbox import (Destination, DeliveryOutbox, transaction,
    evaluated_row, dispatch_fake)
from jobhound.review_audit import material_projection
from jobhound.v41.store import V41Store
from test_v6_continuity import run, raw, NOW
from jobhound.v41.engine import evaluate_raw

EMAIL = Destination.from_address('email','test-sender:alice@example.test')
TELEGRAM = Destination.from_address('telegram','test-bot:chat-123')


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)


@pytest.fixture
def store(tmp_path):
    db = V41Store(tmp_path/'review.sqlite')
    db.enable_delivery_review(review_root=tmp_path,workspace_id='fixture',profile_id='person')
    yield db
    db.close()


def stage(store, result=None, targets=(EMAIL,), **kwargs):
    return store.record_delivery_run(result or run('Pay USD 20 per hour.'),targets,now=100,**kwargs)


def start(store, destination=EMAIL, *, now=101):
    claim = store.delivery.claim_next(destination,now=now)
    assert claim is not None
    store.delivery.begin_send(claim['id'],claim['token'],now=now)
    return claim


def finish(store, claim, outcome='accepted', *, now=102):
    store.delivery.finish(claim['id'],claim['token'],outcome,now=now,evidence='synthetic-receipt')


def count(store, table):
    return store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0]


def test_record_run_revision_and_intent_are_atomic(store, monkeypatch):
    real = store.delivery.enqueue
    def interrupted(*args, **kwargs):
        real(*args,**kwargs)
        raise RuntimeError('crash_before_commit')
    monkeypatch.setattr(store.delivery,'enqueue',interrupted)
    with pytest.raises(RuntimeError):
        stage(store)
    for table in ('runs','run_jobs','observations','canonical_jobs','delivery_subjects','delivery_revisions','delivery_intents'):
        assert count(store,table)==0,table


def test_atomic_invalid_destination_after_first_intent(store):
    with pytest.raises(ValueError):
        stage(store,targets=(EMAIL,Destination('email','bad')))
    assert count(store,'runs')==count(store,'delivery_intents')==0


def test_crash_after_commit_and_reopen_preserves_pending(store):
    stage(store)
    second = V41Store(store.path)
    box = second.enable_delivery_review(review_root=store.path.parent,workspace_id='fixture',profile_id='person')
    assert len(box.inspect())==1 and box.inspect()[0]['status']=='pending'
    second.close()


def test_no_duplicate_for_unchanged_run_and_score_churn(store):
    stage(store)
    claim=start(store); finish(store,claim)
    result=run('Pay USD 20 per hour.',minute=1)
    result.evaluated[0].decision.priority_score+=27
    result.evaluated[0].decision.next_step='Cosmetic wording only'
    report=stage(store,result)
    assert count(store,'runs')==2 and count(store,'delivery_revisions')==1
    assert count(store,'delivery_intents')==1
    assert report['destinations'][0]['counts']['already_recorded']==1


def test_recipient_change_has_independent_delivery(store):
    stage(store)
    claim=start(store); finish(store,claim)
    other=Destination.from_address('email','test-sender:bob@example.test')
    stage(store,run('Pay USD 20 per hour.',minute=1),targets=(EMAIL,other))
    assert [x['status'] for x in store.delivery.inspect()]==['accepted','pending']
    assert store.delivery.claim_next(EMAIL,now=105) is None
    assert store.delivery.claim_next(other,now=105) is not None
    assert count(store,'delivery_baselines')==1


def test_successful_telegram_does_not_suppress_failed_email(store):
    stage(store,targets=(EMAIL,TELEGRAM))
    email=start(store); finish(store,email,'not_sent')
    telegram=start(store,TELEGRAM); finish(store,telegram)
    assert count(store,'delivery_baselines')==1
    assert store.delivery.claim_next(EMAIL,now=110) is None
    assert store.delivery.claim_next(TELEGRAM,now=180) is None
    assert store.delivery.claim_next(EMAIL,now=180) is not None


def test_unstarted_expired_claim_can_retry_but_stale_worker_cannot_send(store):
    stage(store)
    first=store.delivery.claim_next(EMAIL,now=101,lease_seconds=2)
    second=store.delivery.claim_next(EMAIL,now=104)
    assert first['id']==second['id'] and first['token']!=second['token']
    assert second['attempts']==0
    with pytest.raises(ValueError):
        store.delivery.begin_send(first['id'],first['token'],now=104)


@pytest.mark.parametrize('late_ack',[True,False])
def test_send_ack_crash_window_never_autoresends(store,late_ack):
    stage(store); claim=start(store)
    if late_ack:
        with pytest.raises(ValueError):
            finish(store,claim,now=500)
    assert store.delivery.claim_next(EMAIL,now=500) is None
    assert store.delivery.inspect()[0]['status']=='uncertain'
    assert count(store,'delivery_baselines')==0
    store.delivery.reconcile(claim['id'],'accepted',reviewed=True,evidence='operator-confirmed',now=501)
    assert count(store,'delivery_baselines')==1
    assert store.delivery.claim_next(EMAIL,now=1000) is None


@pytest.mark.parametrize('reviewed',[False,None,1,'true'])
def test_reconciliation_requires_literal_review(store,reviewed):
    stage(store); claim=start(store); finish(store,claim,'uncertain')
    with pytest.raises(ValueError):
        store.delivery.reconcile(claim['id'],'not_sent',reviewed=reviewed,evidence='review',now=103)
    assert store.delivery.inspect()[0]['status']=='uncertain'


def test_known_failures_are_bounded_and_attempts_append_only(store):
    stage(store)
    for now in (101,200,400):
        claim=start(store,now=now); finish(store,claim,'not_sent',now=now+1)
    assert store.delivery.inspect()[0]['status']=='failed'
    assert store.delivery.claim_next(EMAIL,now=5000) is None
    assert count(store,'delivery_attempt_events')==9
    for sql in ("UPDATE delivery_attempt_events SET evidence='changed'",'DELETE FROM delivery_attempt_events',
                "UPDATE delivery_revisions SET cause='changed'",'DELETE FROM delivery_revisions'):
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(sql)
        store.conn.rollback()


def test_two_workers_claim_only_once(store):
    stage(store)
    def worker(_):
        conn=sqlite3.connect(store.path,timeout=10)
        conn.row_factory=sqlite3.Row
        box=DeliveryOutbox(conn,'fixture','person')
        try:
            return box.claim_next(EMAIL,now=101)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(worker,range(2)))
    assert sum(c is not None for c in claims)==1


def test_a_b_a_restoration_is_three_revisions(store):
    for minute,pay in enumerate((20,30,20)):
        stage(store,run(f'Pay USD {pay} per hour.',minute=minute))
        claim=start(store); finish(store,claim)
    assert count(store,'delivery_revisions')==count(store,'delivery_intents')==3
    rows=store.conn.execute('SELECT * FROM delivery_revisions ORDER BY sequence').fetchall()
    assert rows[0]['material']==rows[2]['material'] and rows[0]['id']!=rows[2]['id']


def test_pending_stale_pay_is_superseded_and_lease_invalidated(store):
    stage(store)
    claim=store.delivery.claim_next(EMAIL,now=101)
    stage(store,run('Pay USD 30 per hour.',minute=1))
    assert [x['status'] for x in store.delivery.inspect()]==['superseded','pending']
    with pytest.raises(ValueError):
        store.delivery.begin_send(claim['id'],claim['token'],now=102)


def test_uncertain_old_revision_blocks_new_one_until_reconciled(store):
    stage(store); claim=start(store); finish(store,claim,'uncertain')
    stage(store,run('Pay USD 30 per hour.',minute=1))
    assert store.delivery.claim_next(EMAIL,now=300) is None
    store.delivery.reconcile(claim['id'],'not_sent',reviewed=True,evidence='not-sent-confirmed',now=301)
    assert store.delivery.inspect()[0]['status']=='superseded'
    assert store.delivery.claim_next(EMAIL,now=302) is not None


def test_material_change_with_a_policy_change_is_delivered_and_labelled(store):
    # Review finding 4 (2026-09-28): this used to be held forever. The policy
    # fingerprint includes the trust-registry and config hashes, which change
    # routinely, so real updates were being frozen. The revision keeps its
    # 'policy_review' cause for the audit, but the change is delivered.
    stage(store); claim=start(store); finish(store,claim)
    updated=run('Pay USD 30 per hour.',minute=1)
    updated.metadata.ruleset_hash='new-model'
    report=stage(store,updated)
    assert report['destinations'][0]['counts']['policy_review']==0     # nothing held
    assert report['destinations'][0]['policy_changed']==1              # still visible
    assert count(store,'delivery_intents')==2
    assert count(store,'delivery_revisions')==2
    assert store.conn.execute("SELECT cause FROM delivery_revisions ORDER BY sequence DESC LIMIT 1").fetchone()[0]=='policy_review'
    assert store.delivery.claim_next(EMAIL,now=300) is not None


def test_canonical_crosswalk_retains_delivered_baseline(store):
    first=run('Pay USD 20 per hour.'); stage(store,first)
    claim=start(store); finish(store,claim)
    old=first.evaluated[0].canonical.canonical_id
    store.delivery.register_alias(old,'new-canonical',evidence='reviewed-exact-identity')
    updated=run('Pay USD 20 per hour.',minute=1)
    updated.evaluated[0].canonical.canonical_id='new-canonical'
    stage(store,updated)
    assert count(store,'delivery_revisions')==1 and count(store,'delivery_intents')==1
    with pytest.raises(ValueError):
        store.delivery.register_alias('new-canonical',old,evidence='cycle')


def test_two_rows_for_one_crosswalk_abort(store):
    result=run('Pay USD 20 per hour.'); stage(store,result)
    identity=result.evaluated[0].canonical.canonical_id
    store.delivery.register_alias(identity,'alias',evidence='exact-role')
    result=run('Pay USD 20 per hour.',minute=1)
    duplicate=result.evaluated[0].model_copy(deep=True)
    duplicate.canonical.canonical_id='alias'
    result.evaluated.append(duplicate)
    with pytest.raises(ValueError):
        stage(store,result)
    assert count(store,'runs')==1


def test_legacy_channel_receipts_quarantine_not_resend(tmp_path):
    db=V41Store(tmp_path/'old-copy.sqlite')
    result=run('Pay USD 20 per hour.')
    db.record_run(result); db.mark_notified(result.metadata.run_id,result.evaluated,'email',True)
    db.enable_delivery_review(review_root=tmp_path,workspace_id='fixture',profile_id='person')
    stage(db,run('Pay USD 20 per hour.',minute=1))
    assert db.delivery.inspect()[0]['status']=='legacy_hold'
    assert db.delivery.claim_next(EMAIL,now=1000) is None
    assert count(db,'delivery_baselines')==0
    db.close()


def test_profile_isolation_and_no_cross_owner_ack(store):
    stage(store); claim=start(store)
    other=DeliveryOutbox(store.conn,'fixture','other-profile')
    assert other.inspect()==[]
    with pytest.raises(ValueError):
        other.finish(claim['id'],claim['token'],'accepted',now=102,evidence='invalid-owner')
    assert store.delivery.inspect()[0]['status']=='sending'


def test_out_of_order_snapshot_cannot_restore_stale_facts(store):
    stage(store,run('Pay USD 30 per hour.',minute=3))
    with pytest.raises(ValueError):
        stage(store,run('Pay USD 20 per hour.',minute=1))
    assert count(store,'runs')==1 and count(store,'delivery_revisions')==1


def test_caps_reconcile_and_hidden_rows_survive(store):
    result=evaluate_raw([raw('Pay USD 20 per hour.',identity=str(i)) for i in range(4)],as_of=NOW)
    report=stage(store,result,card_cap=1)
    counts=report['destinations'][0]['counts']
    assert counts['selected_cards']==1 and counts['overflow_cards']==3
    assert count(store,'run_jobs')==count(store,'delivery_revisions')==4
    assert count(store,'delivery_intents')==1


def test_coverage_failure_and_recovery_are_separate_from_jobs(store):
    result=run(); result.source_health=[{'source':'jsearch','status':'failed','error_type':'Timeout'}]
    report=stage(store,result,card_cap=0)
    assert report['destinations'][0]['coverage']['selected']==1
    claim=start(store); finish(store,claim)
    updated=run(minute=1); updated.source_health=[{'source':'jsearch','status':'ok','error_type':None}]
    stage(store,updated,card_cap=0)
    assert len(store.delivery.inspect())==2


@pytest.mark.parametrize('receipt',[False,True,None,('bad','receipt')])
def test_ambiguous_legacy_sender_results_are_uncertain(store,receipt):
    stage(store)
    async def sender(_): return receipt
    assert asyncio.run(dispatch_fake(store.delivery,EMAIL,sender,now=lambda:101))=='uncertain'
    assert store.delivery.inspect()[0]['status']=='uncertain'


def test_fake_provider_acceptance_advances_only_that_destination(store):
    stage(store,targets=(EMAIL,TELEGRAM))
    async def sender(payload):
        assert payload['canonical_id']
        return 'accepted','fake-provider-acceptance'
    assert asyncio.run(dispatch_fake(store.delivery,EMAIL,sender,now=lambda:101))=='accepted'
    assert [x['status'] for x in store.delivery.inspect()]==['accepted','pending']


def test_backup_restore_preserves_intents_and_attempt_history(store,tmp_path):
    stage(store); claim=start(store); finish(store,claim)
    backup=sqlite3.connect(tmp_path/'backup.sqlite')
    store.conn.backup(backup); backup.close()
    restored=V41Store(tmp_path/'backup.sqlite')
    restored.enable_delivery_review(review_root=tmp_path,workspace_id='fixture',profile_id='person')
    assert restored.delivery.inspect()[0]['status']=='accepted'
    assert count(restored,'delivery_attempt_events')==3
    assert restored.delivery.claim_next(EMAIL,now=1000) is None
    restored.close()


def test_nested_savepoint_does_not_commit_outer_record(store):
    store.conn.execute('BEGIN IMMEDIATE')
    stage(store)
    store.conn.rollback()
    assert count(store,'runs')==count(store,'delivery_intents')==0


def test_cosmetic_formatting_and_capture_churn_are_not_material():
    row=evaluated_row(run().evaluated[0])
    before=material_projection(row)
    row['job']['title']='  '+row['job']['title']+'  '
    row['decision']['priority_score']=999
    row['decision']['next_step']='Different formatting instruction'
    assert material_projection(row)==before


def test_all_delivery_code_requires_explicit_optin(tmp_path):
    db=V41Store(tmp_path/'not-enabled.sqlite')
    with pytest.raises(ValueError): stage(db)
    assert not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_meta'").fetchone()
    with pytest.raises(ValueError):
        db.enable_delivery_review(review_root=tmp_path/'wrong',workspace_id='fixture',profile_id='person')
    db.close()
