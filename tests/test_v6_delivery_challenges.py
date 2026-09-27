"""Second-pass contract probes, separate from the implementation's first tests."""
import json
import sqlite3
from datetime import timedelta

import pytest

from jobhound.config import CONFIG
from jobhound.delivery_outbox import Destination, DeliveryOutbox, transaction, evaluated_row
from jobhound.review_audit import material_projection
from jobhound.v41.store import V41Store
from jobhound.run_context import RunContext, run_scope
from test_v6_continuity import run, NOW
from test_v6_delivery_recovery import store, release, stage, start, finish, EMAIL, count
from test_v57_outcomes import setup, event, preview


@pytest.mark.parametrize('predicate,value',[('application_state','rejected'),('project_access','blocked')])
def test_scoped_negative_status_once_and_all_rows_retained(store,predicate,value):
    base,binding=setup()
    changed,report=preview([event(binding,'fact',predicate,value)],base,binding)
    receipt=stage(store,changed)
    assert receipt['destinations'][0]['counts']['selected_status']==1
    claim=start(store); finish(store,claim)
    repeated=changed.model_copy(deep=True)
    repeated.metadata.run_id+='-replay'
    repeated.metadata.as_of+=timedelta(minutes=1)
    receipt=stage(store,repeated)
    assert receipt['destinations'][0]['counts']['already_recorded']==1
    assert count(store,'delivery_intents')==1
    assert count(store,'run_jobs')==2


def test_status_overflow_is_not_lost_on_next_run(store):
    base,binding=setup()
    changed,_=preview([event(binding,'fact','project_access','blocked')],base,binding)
    receipt=stage(store,changed,status_cap=0)
    assert receipt['destinations'][0]['counts']['overflow_status']==1
    changed=changed.model_copy(deep=True)
    changed.metadata.run_id+='-next'
    changed.metadata.as_of+=timedelta(minutes=1)
    receipt=stage(store,changed)
    assert receipt['destinations'][0]['counts']['selected_status']==1


def test_same_scoped_fact_recaptured_at_new_time_is_not_revision():
    base,binding=setup()
    changed,_=preview([event(binding,'fact','project_access','blocked')],base,binding)
    row=evaluated_row(changed.evaluated[0])
    before=material_projection(row)
    row['outcome_projection']['events'][0]['observed_at']=(NOW+timedelta(days=1)).isoformat()
    assert before==material_projection(row)
    row['outcome_projection']['scope']['attempt']='different-attempt'
    assert before!=material_projection(row)


def test_true_scoped_restoration_creates_new_action(store):
    base,binding=setup()
    blocked=event(binding,'block','project_access','blocked',event_at=(NOW-timedelta(days=2)).isoformat(),observed_at=(NOW-timedelta(days=1)).isoformat())
    first,report=preview([blocked],base,binding)
    stage(store,first); claim=start(store); finish(store,claim)
    restored=event(binding,'restore','project_access','accessible',supersedes=['block'])
    second,_=preview([restored,blocked],base,binding,previous=report['events'])
    receipt=stage(store,second)
    assert receipt['destinations'][0]['counts']['selected_cards']==1
    assert count(store,'delivery_revisions')==2


def test_coverage_recovery_overflow_survives_unchanged_next_run(store):
    def result(minute,status):
        result=run(minute=minute)
        result.source_health=[{'source':'jsearch','status':status,'error_type':None}]
        return result
    stage(store,result(0,'failed'),card_cap=0)
    claim=start(store); finish(store,claim)
    receipt=stage(store,result(1,'ok'),card_cap=0,status_cap=0)
    assert receipt['destinations'][0]['coverage']['overflow']==1
    receipt=stage(store,result(2,'ok'),card_cap=0,status_cap=5)
    assert receipt['destinations'][0]['coverage']['selected']==1


def test_review_context_forbids_existing_connection_writes(store,tmp_path):
    context=RunContext.capture(config=CONFIG.model_copy(deep=True),as_of=NOW,workspace=tmp_path)
    with run_scope(context),pytest.raises(RuntimeError,match='forbids'):
        DeliveryOutbox(store.conn,'other','other')


def test_credible_pay_resolution_is_material_without_score_churn():
    row=evaluated_row(run('Pay USD 20 per hour.').evaluated[0])
    row['assessment']['pay_assessment']['state']='ambiguous'
    before=material_projection(row)
    row['assessment']['pay_assessment']['state']='known_hourly'
    assert before!=material_projection(row)


def test_piece_rate_fee_change_is_material():
    row=evaluated_row(run('Pay USD 6 per image.').evaluated[0])
    before=material_projection(row)
    row['assessment']['selected_pay']['fees']=['platform_fee:10_percent']
    assert before!=material_projection(row)


def test_url_query_credentials_do_not_persist_in_delivery_payload(store):
    result=run('Pay USD 20 per hour.')
    result.evaluated[0].job.url+='?app_key=FAKE_DO_NOT_SEND_THIS_KEY&query=Bengali'
    stage(store,result)
    for row in store.delivery.inspect():
        assert 'FAKE_DO_NOT_SEND_THIS_KEY' not in row['payload']


@pytest.mark.parametrize('cap',[-1,101,True,1.5])
def test_invalid_caps_rollback_entire_run(store,cap):
    with pytest.raises(ValueError): stage(store,card_cap=cap)
    assert count(store,'runs')==0


def test_bad_accounting_cannot_enqueue(store):
    result=run()
    result.accounting_errors.append('synthetic ledger failure')
    with pytest.raises(ValueError): stage(store,result)
    assert count(store,'runs')==0


def test_unknown_schema_does_not_migrate_or_queue(store):
    with store.conn:
        store.conn.execute("UPDATE delivery_meta SET value='future-version' WHERE key='version'")
    with pytest.raises(ValueError): DeliveryOutbox(store.conn,'fixture','person')
    assert count(store,'delivery_revisions')==0


def test_failure_after_acceptance_update_rolls_back_baseline(store):
    stage(store); claim=start(store)
    # SQLite-injected crash between baseline update and attempt journal append.
    store.conn.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON delivery_attempt_events WHEN NEW.event='accepted' BEGIN SELECT RAISE(ABORT,'crash-before-ack-commit'); END;")
    with pytest.raises(sqlite3.IntegrityError): finish(store,claim)
    assert count(store,'delivery_baselines')==0
    assert store.delivery.inspect()[0]['status']=='sending'
    assert store.delivery.claim_next(EMAIL,now=1000) is None
    assert store.delivery.inspect()[0]['status']=='uncertain'


def test_delivery_material_matches_audit_material(store):
    result=run('Pay USD 20 per hour.'); stage(store,result)
    saved=json.loads(store.conn.execute('SELECT material FROM delivery_revisions').fetchone()[0])
    assert saved==material_projection(evaluated_row(result.evaluated[0]))


def test_same_run_retry_returns_durable_receipt_without_new_intents(store):
    result=run('Pay USD 20 per hour.')
    first=stage(store,result)
    assert stage(store,result)==first
    assert count(store,'runs')==count(store,'delivery_intents')==count(store,'delivery_runs')==1
    altered=result.model_copy(deep=True)
    altered.evaluated[0].job.title+=' Changed'
    with pytest.raises(ValueError,match='run ID reused'): stage(store,altered)


def test_restoring_same_content_after_closed_status_does_not_collapse(store):
    base,binding=setup()
    stage(store,base); claim=start(store); finish(store,claim)
    changed,report=preview([event(binding,'reject','application_state','rejected')],base,binding)
    stage(store,changed); claim=start(store); finish(store,claim)
    restored=base.model_copy(deep=True)
    restored.metadata.run_id+='-restored'
    restored.metadata.as_of+=timedelta(minutes=1)
    stage(store,restored)
    assert count(store,'delivery_revisions')==3
    assert store.delivery.inspect()[-1]['status']=='pending'


def test_a_new_rejection_supersedes_unsent_apply_card(store):
    base,binding=setup()
    stage(store,base)
    changed,_=preview([event(binding,'reject','application_state','rejected')],base,binding)
    stage(store,changed)
    assert [row['status'] for row in store.delivery.inspect()]==['superseded','pending']
    claim=store.delivery.claim_next(EMAIL,now=101)
    assert json.loads(claim['payload'])['decision']['lifecycle']=='closed'


def test_unexplained_missing_job_does_not_invent_closure(store):
    from jobhound.v41.engine import evaluate_raw
    stage(store)
    stage(store,evaluate_raw([],as_of=NOW+timedelta(minutes=1)))
    assert count(store,'delivery_revisions')==1
    assert store.delivery.inspect()[0]['status']=='pending'


def test_distinct_workspace_cannot_claim_other_workspace_intent(store):
    stage(store)
    other=DeliveryOutbox(store.conn,'other-workspace','person')
    assert other.claim_next(EMAIL,now=101) is None


def test_attempt_cancel_after_started_send_is_uncertain_on_restart(store):
    import asyncio
    from jobhound.delivery_outbox import dispatch_fake
    stage(store)
    async def sender(_): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispatch_fake(store.delivery,EMAIL,sender,now=lambda:101))
    assert store.delivery.inspect()[0]['status']=='sending'
    assert store.delivery.claim_next(EMAIL,now=200) is None
    assert store.delivery.inspect()[0]['status']=='uncertain'


def test_delivery_migration_does_not_rewrite_legacy_rows(tmp_path):
    db=V41Store(tmp_path/'old.sqlite')
    result=run(); db.record_run(result)
    tables=('runs','run_jobs','observations','canonical_jobs','notification_events')
    before={t:[tuple(r) for r in db.conn.execute('SELECT * FROM '+t)] for t in tables}
    db.enable_delivery_review(review_root=tmp_path,workspace_id='fixture',profile_id='person')
    after={t:[tuple(r) for r in db.conn.execute('SELECT * FROM '+t)] for t in tables}
    assert before==after
    db.close()


def test_standalone_audit_script_retains_material_compare_entrypoint():
    import runpy
    from pathlib import Path
    module=runpy.run_path(str(Path(__file__).parents[1]/'jobhound/review_audit.py'))
    row={'job':{'title':'  Bengali   Trainer  '},'assessment':{},'decision':{}}
    assert module['material_projection'](row)==material_projection(row)
