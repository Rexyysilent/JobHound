"""Exact private work subjects, native money and action-specific prerequisites."""
from datetime import timedelta
import json

import pytest

from jobhound.config import CONFIG
from jobhound.v41.digest import build_digest
from jobhound.v41.engine import decision_fingerprint, _decision
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.outcomes import OutcomeEvent, WorkIdentity
from test_v55_action_policy import NOW, observation
from test_v57_outcomes import setup, event
from test_assessment_revisions import ready as assessment_ready, scoped

WORK = dict(project_id='project-1', allocation_id='allocation-1', revision='terms-1')
OTHER = dict(project_id='project-1', allocation_id='allocation-2', revision='terms-1')
TERMS = dict(amount='10.00', currency='USD', reference='contract-1', unit='labor_hour')
CHECKS = dict(privacy=True, schedule=True, equipment=True, cost=True, scope=True, terms=True, economics=True)

@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])

def work_event(binding, key, predicate, value, identity=WORK, **updates):
    return event(binding, key, predicate, value, schema_version=3, work=identity, **updates)

def select(binding, identity=WORK, **updates):
    return event(binding, 'work-selection', 'work_selection', identity, schema_version=3,
                 actor='user', source_kind='user_report', **updates)

def ready(binding, identity=WORK):
    return [select(binding, identity),
        *[work_event(binding, key, key, value, identity) for key, value in (
            ('allocation_state','allocated'), ('work_access','accessible'),
            ('payment_setup','verified'), ('payable_approval','approved'),
            ('work_terms',TERMS), ('work_route','https://example.org/work/project-1'))],
        work_event(binding, 'work_checks', 'work_checks', CHECKS, identity, actor='user', source_kind='user_report')]

def preview(result, binding, rows, **kwargs):
    changed, report = preview_outcomes(result, rows, [binding], as_of=NOW, **kwargs)
    assert changed.accounting_ok and build_digest(changed, include_all=True).accounting_ok
    return changed.evaluated[0], report

def test_exact_allocation_starts_through_engine_without_borrowing_old_assessment_or_cost(monkeypatch):
    result,binding=setup(); before=result.model_dump_json()
    monkeypatch.setattr(CONFIG.v55,'account_states',[dict(canonical_id=binding.canonical_id,
        assessment_state='completed', action_cost={'cash_required':999},
        time_to_cash_days=1, time_to_cash_evidence_reference='legacy', tasks_allocated=True,
        allocated_at=NOW.isoformat(), project_access='accessible', payout_setup='verified',
        paid_terms_reference='old', observed_at=NOW.isoformat())])
    row,report=preview(result,binding,ready(binding)+assessment_ready(binding)+[
        scoped(binding,'assessment-done','assessment_state','completed',actor='user',source_kind='user_report',
               event_at=(NOW-timedelta(minutes=30)).isoformat(), observed_at=NOW.isoformat(),supersedes=['invite'])])
    assert row.decision.next_action=='start_allocated_task' and row.decision.action_band.value=='primary'
    assert row.decision.priority_key.action_readiness==3
    assert row.assessment.time_to_cash_days is None
    assert row.assessment.account_state['_reviewed_work_action']['next_action_cost'] is None
    assert row.assessment.selected_pay==result.evaluated[0].assessment.selected_pay
    assert result.model_dump_json()==before and len(report['events'])==13

@pytest.mark.parametrize('predicate',['allocation_state','work_access','payment_setup','payable_approval','work_terms','work_checks','work_route'])
@pytest.mark.parametrize('foreign',['allocation','project','revision'])
def test_other_allocation_project_or_revision_cannot_supply_prerequisite(predicate,foreign):
    result,binding=setup();rows=ready(binding)
    item=next(e for e in rows if e['predicate']==predicate)
    item['work']=dict(WORK, **{dict(allocation='allocation_id',project='project_id',revision='revision')[foreign]:'other'})
    row,report=preview(result,binding,rows)
    assert row.decision.next_action=='verify' and report['counts']['quarantined']==0
    assert predicate in row.assessment.account_state['_reviewed_work_action']['prerequisites']

@pytest.mark.parametrize('predicate',['work_selection','allocation_state','work_access','payment_setup','payable_approval','work_checks','work_route'])
@pytest.mark.parametrize('age',['unknown','expired','stale'])
def test_volatile_prerequisites_require_their_own_known_current_time(predicate,age):
    result,binding=setup();rows=ready(binding);item=next(e for e in rows if e['predicate']==predicate)
    if age=='unknown':item['event_at']=None
    elif age=='expired':item['expires_at']=(NOW-timedelta(minutes=30)).isoformat()
    else:item.update(event_at=(NOW-timedelta(days=90)).isoformat(),observed_at=(NOW-timedelta(days=89)).isoformat())
    row,_=preview(result,binding,rows)
    assert row.decision.next_action=='verify' and row.decision.priority_key.supported_time_to_cash==0

@pytest.mark.parametrize('predicate',['allocation_state','payment_setup','payable_approval','work_terms'])
def test_review_seed_never_authorizes_new_work(predicate):
    result,binding=setup();rows=ready(binding);next(e for e in rows if e['predicate']==predicate)['source_kind']='reviewed_seed'
    row,_=preview(result,binding,rows);assert row.decision.next_action=='verify'

@pytest.mark.parametrize('check',list(CHECKS))
@pytest.mark.parametrize('approval',[None,False])
def test_missing_or_denied_user_approval_is_not_true(check,approval):
    result,binding=setup();rows=ready(binding)
    next(e for e in rows if e['predicate']=='work_checks')['value']=dict(CHECKS,**{check:approval})
    row,_=preview(result,binding,rows);assert row.decision.next_action=='verify'

def test_public_closure_stops_new_applications_but_preserves_proven_allocated_work():
    result,binding=setup(observation(vacancy_state='explicitly_closed'))
    assert result.evaluated[0].decision.next_action=='skip'
    row,_=preview(result,binding,ready(binding))
    assert row.decision.next_action=='start_allocated_task' and row.decision.action_band.value=='primary'
    assert 'source_quality:explicitly_closed' in row.assessment.blockers
    assert row.canonical.best_observation.vacancy_state=='explicitly_closed'
    rows=[e for e in ready(binding) if e['predicate']!='allocation_state']
    row,_=preview(result,binding,rows);assert row.decision.next_action=='skip'

@pytest.mark.parametrize('blocker',['location:country_not_allowed','scam_threshold','pay_below_floor','source_quality:non_opportunity:seller_service'])
def test_later_public_veto_still_removes_work_start_authority(blocker):
    result,binding=setup();row,_=preview(result,binding,ready(binding))
    row.assessment.blockers.append(blocker)
    decision=_decision(row.canonical,row.assessment)
    assert decision.next_action=='skip' and decision.action_band.value=='reject'
    assert decision.priority_key.action_readiness==0

@pytest.mark.parametrize('fact,value,actor,source',[('invoice',dict(amount='100.00',currency='USD',reference='invoice-1'),'buyer','mail'),
    ('collected_payment',dict(amount='35.00',currency='INR',reference='receipt-1'),'user','user_report'),
    ('work_state','accepted','provider','mail'),('work_state','submitted','user','user_report')])
def test_closed_application_can_review_existing_work_without_claiming_new_eligibility(fact,value,actor,source):
    result,binding=setup(observation(vacancy_state='explicitly_closed'))
    rows=[select(binding),work_event(binding,fact,fact,value,actor=actor,source_kind=source),
          event(binding,'reject','application_state','rejected')]
    row,report=preview(result,binding,rows)
    assert row.decision.next_action==('await_response' if value=='submitted' else 'reconcile_payment_status')
    assert row.decision.action_band.value=='verify' and row.decision.lifecycle=='watch'
    assert row.assessment.eligibility.value=='failed'
    assert row.assessment.time_to_cash_days is None and row.decision.priority_key.supported_time_to_cash==0
    assert report['projections'][0].value('application_state')=='rejected'

def test_setup_allocation_acceptance_invoice_and_receipt_are_orthogonal():
    result,binding=setup();rows=ready(binding)
    rows += [work_event(binding,'accepted','work_state','accepted'),
        work_event(binding,'invoice','invoice',dict(amount='100',currency='USD',reference='invoice-1'),actor='buyer'),
        work_event(binding,'receipt','collected_payment',dict(amount='35',currency='INR',reference='receipt-1'),actor='user',source_kind='user_report')]
    row,report=preview(result,binding,rows)
    projection=report['projections'][0];subject=WorkIdentity(**WORK)
    assert projection.value('payment_setup',work=subject)=='verified'
    assert projection.value('payable_approval',work=subject)=='approved'
    assert projection.value('allocation_state',work=subject)=='allocated'
    assert projection.value('work_state',work=subject)=='accepted'
    assert projection.value('invoice',work=subject).amount=='100'
    assert projection.value('collected_payment',work=subject).amount=='35'
    assert projection.value('collected_payment',work=subject).currency=='INR'
    assert row.decision.next_action=='reconcile_payment_status'
    assert row.assessment.account_state['_reviewed_work_action']['next_action_cost'] is None
    assert len(report['events'])==11

def test_older_allocation_completion_receipt_and_conflict_do_not_complete_or_fund_selected_work():
    result,binding=setup()
    rows=ready(binding)+[work_event(binding,'old-done','work_state','accepted',OTHER),
        work_event(binding,'old-invoice','invoice',dict(amount='100',currency='USD',reference='old-invoice'),OTHER),
        work_event(binding,'old-pay','collected_payment',dict(amount='100',currency='USD',reference='old-receipt'),OTHER,actor='user',source_kind='user_report'),
        work_event(binding,'old-user','work_access','blocked',OTHER,actor='user',source_kind='user_report'),
        work_event(binding,'old-provider','work_access','accessible',OTHER)]
    row,report=preview(result,binding,rows)
    assert row.decision.next_action=='start_allocated_task' and report['projections'][0].conflicts
    assert not {'old-done','old-pay','old-invoice'}.intersection(row.assessment.account_state['_reviewed_work_action']['event_ids'])

def test_duplicate_out_of_order_imports_are_idempotent_and_cross_subject_correction_is_quarantined():
    result,binding=setup();rows=ready(binding)
    changed,report=preview_outcomes(result,rows,[binding],as_of=NOW)
    again,duplicate=preview_outcomes(result,list(reversed(rows)),[binding],as_of=NOW,previous=report['events'])
    assert decision_fingerprint(changed)==decision_fingerprint(again)
    assert duplicate['counts']['duplicate']==8
    bad=work_event(binding,'other','allocation_state','revoked',OTHER,event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat(),supersedes=['allocation_state'])
    row,report=preview(result,binding,rows+[bad])
    assert report['quarantine']==[dict(line=9,code='invalid_supersession')] and row.decision.next_action=='start_allocated_task'

@pytest.mark.parametrize('predicate,old,new,actor,source',[('allocation_state','revoked','allocated','provider','mail'),
    ('work_state','accepted','submitted','provider','mail'),
    ('collected_payment',dict(amount='10',currency='USD',reference='r-1'),dict(amount='20',currency='USD',reference='r-2'),'user','user_report')])
def test_later_reminders_or_receipts_do_not_erase_terminal_history(predicate,old,new,actor,source):
    result,binding=setup();rows=ready(binding)
    rows=[e for e in rows if e['predicate']!=predicate]
    rows += [work_event(binding,'old',predicate,old,actor=actor,source_kind=source),
        work_event(binding,'new',predicate,new,actor=actor,source_kind=source,event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat())]
    row,report=preview(result,binding,rows)
    assert report['projections'][0].conflicts and row.decision.next_action!='start_allocated_task'
    assert report['projections'][0].dispositions['old']=='conflict'

@pytest.mark.parametrize('unit',['task','deliverable'])
def test_fixed_native_units_are_not_converted_to_hourly_pay(unit):
    result,binding=setup();rows=ready(binding)
    next(e for e in rows if e['predicate']=='work_terms')['value']=dict(TERMS,amount='500',currency='INR',unit=unit)
    row,report=preview(result,binding,rows)
    assert row.decision.next_action=='start_allocated_task'
    assert row.assessment.selected_pay==result.evaluated[0].assessment.selected_pay
    assert row.assessment.account_state['_reviewed_work_action']['terms']['unit']==unit

@pytest.mark.parametrize('terms',[dict(TERMS,amount='1'),dict(TERMS,currency='XYZ')])
def test_hourly_terms_require_supported_floor_check(terms):
    result,binding=setup();rows=ready(binding);next(e for e in rows if e['predicate']=='work_terms')['value']=terms
    row,_=preview(result,binding,rows)
    assert row.decision.next_action=='verify' and 'work_economics' in row.assessment.account_state['_reviewed_work_action']['prerequisites']

@pytest.mark.parametrize('bad',[True,False,0,-1,1.5,'0','NaN','Infinity','1e3','1,000','-1','01'])
def test_invalid_money_never_becomes_received_cash(bad):
    result,binding=setup();raw=work_event(binding,'bad','collected_payment',dict(amount=bad,currency='USD',reference='r'),actor='user',source_kind='user_report')
    _,report=preview(result,binding,[raw]);assert report['counts']['quarantined']==1

@pytest.mark.parametrize('bad',[True,False,'0','10',float('inf'),float('nan')])
def test_work_cost_rejects_boolean_coercion_strings_and_nonfinite(bad):
    result,binding=setup();raw=work_event(binding,'bad','work_cost',dict(cash=bad,currency='USD',basis='user_estimate'),actor='user',source_kind='user_report')
    _,report=preview(result,binding,[raw]);assert report['counts']['quarantined']==1

@pytest.mark.parametrize('predicate,value,actor,source',[('collected_payment',dict(amount='10',currency='USD',reference='r'),'provider','mail'),
    ('work_state','accepted','user','user_report'),('work_checks',CHECKS,'provider','mail'),
    ('allocation_state','allocated','user','user_report'),('work_route','https://example.org/work?token=synthetic','provider','mail')])
def test_inapplicable_actor_or_authenticated_route_is_quarantined(predicate,value,actor,source):
    result,binding=setup();raw=work_event(binding,'bad',predicate,value,actor=actor,source_kind=source)
    _,report=preview(result,binding,[raw]);assert report['counts']['quarantined']==1

def test_reserved_plan_from_overlay_cannot_bypass_public_rejection(monkeypatch):
    result,binding=setup(observation(vacancy_state='explicitly_closed'))
    monkeypatch.setattr(CONFIG.v55,'account_states',[dict(canonical_id=binding.canonical_id,
        _reviewed_work_action=dict(authority='reviewed_work_v3',target=WORK,read_only=True,next_action='reconcile_payment_status'))])
    row,_=preview(result,binding,[event(binding,'submitted','application_state','submitted')])
    assert row.decision.next_action=='skip' and '_reviewed_work_action' not in row.assessment.account_state

def test_literal_legacy_event_shape_has_no_work_field():
    _,binding=setup();raw=event(binding,'legacy','application_state','submitted')
    parsed=OutcomeEvent.model_validate(raw)
    assert 'work' not in json.loads(parsed.model_dump_json())
    raw2=assessment_ready(binding)[1];parsed2=OutcomeEvent.model_validate(raw2)
    assert 'work' not in json.loads(parsed2.model_dump_json())

@pytest.mark.parametrize('age',['current','unknown','stale','expired'])
def test_only_current_cost_for_selected_ready_work_is_reported(age):
    result,binding=setup();rows=ready(binding)
    cost=work_event(binding,'cost','work_cost',dict(minutes=30,cash=0,currency='USD',basis='user_estimate'),actor='user',source_kind='user_report')
    if age=='unknown':cost['event_at']=None
    elif age=='stale':cost.update(event_at=(NOW-timedelta(days=90)).isoformat(),observed_at=(NOW-timedelta(days=89)).isoformat())
    elif age=='expired':cost['expires_at']=(NOW-timedelta(minutes=30)).isoformat()
    row,_=preview(result,binding,rows+[cost]);assert row.decision.next_action=='start_allocated_task'
    assert (row.assessment.account_state['_reviewed_work_action']['next_action_cost'] is not None)==(age=='current')

def test_changed_terms_require_new_approvals_and_do_not_borrow_old_terms_cost():
    result,binding=setup();rows=ready(binding);new=dict(WORK,revision='terms-2')
    selection=select(binding,new,event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat());selection['event_id']='new-selection'
    row,report=preview(result,binding,rows+[selection,work_event(binding,'new-allocation','allocation_state','allocated',new)])
    assert row.decision.next_action=='verify'
    plan=row.assessment.account_state['_reviewed_work_action']
    assert plan['target']==new and plan['terms'] is None and plan['next_action_cost'] is None
    assert report['projections'][0].dispositions['work_checks']=='current'

def test_explicit_correction_is_subject_local_and_retains_original_receipt():
    result,binding=setup()
    original=work_event(binding,'receipt','collected_payment',dict(amount='10',currency='USD',reference='r-1'),actor='user',source_kind='user_report')
    correction=work_event(binding,'corrected','collected_payment',dict(amount='12',currency='USD',reference='r-1'),actor='user',source_kind='user_report',
        event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat(),supersedes=['receipt'])
    row,report=preview(result,binding,[select(binding),correction,original])
    projection=report['projections'][0]
    assert len(projection.events)==3 and projection.dispositions['receipt']=='superseded'
    assert projection.value('collected_payment',work=WorkIdentity(**WORK)).amount=='12'
    assert row.decision.next_action=='reconcile_payment_status' and not projection.conflicts

def test_revocation_prevents_new_work_and_provider_sent_is_not_received():
    result,binding=setup();rows=ready(binding)
    rows += [work_event(binding,'revoked','allocation_state','revoked',event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat())]
    row,report=preview(result,binding,rows)
    assert row.decision.next_action=='verify' and report['projections'][0].dispositions['allocation_state']=='superseded'
    raw=work_event(binding,'sent','collected_payment','sent')
    _,report=preview(result,binding,[raw]);assert report['counts']['quarantined']==1

@pytest.mark.parametrize('updates',[dict(schema_version=1),dict(schema_version=2),dict(schema_version='3'),
    dict(work=None),dict(assessment={'step_id':'s','revision':'r'}),dict(actor='buyer',source_kind='community')])
def test_work_fact_requires_supported_version_exact_subject_and_private_source(updates):
    result,binding=setup();raw=work_event(binding,'bad','allocation_state','allocated');raw.update(updates)
    _,report=preview(result,binding,[raw]);assert report['counts']['quarantined']==1

def test_undated_invoice_is_a_review_claim_not_confirmation_or_cash():
    result,binding=setup();raw=work_event(binding,'invoice','invoice',dict(amount='10',currency='USD',reference='i'),event_at=None)
    row,report=preview(result,binding,[select(binding),raw])
    assert row.decision.next_action=='reconcile_payment_status'
    assert 'invoice_evidence' in row.assessment.account_state['_reviewed_work_action']['prerequisites']
    assert not report['projections'][0].facts('collected_payment',work=WorkIdentity(**WORK))

def test_incomparable_selections_hold_and_keep_both_allocation_histories():
    result,binding=setup();rows=ready(binding)
    selection=select(binding,OTHER);selection['event_id']='other-selection';rows.append(selection)
    row,report=preview(result,binding,rows)
    assert 'work_selection' in report['projections'][0].conflicts
    assert row.decision.next_action=='verify' and row.assessment.account_state['_reviewed_work_action']['target'] is None

def test_account_block_does_not_disappear_into_work_access_or_payment_setup():
    result,binding=setup();rows=ready(binding)+[event(binding,'blocked','project_access','blocked')]
    row,_=preview(result,binding,rows)
    assert row.decision.next_action=='verify' and 'work_account_restriction' in row.assessment.account_state['_reviewed_work_action']['prerequisites']
