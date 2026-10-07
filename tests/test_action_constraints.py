"""Exact due-time feasibility and retained actor evidence through the real engine."""
from datetime import timedelta

import pytest
from pydantic import ValidationError

from jobhound.config import CONFIG
from jobhound.delivery_transport import DigestTransport
from jobhound.v41.outcomes import ActionDeadline, OutcomeEvent, OutcomeBinding, OutcomeScope
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.outcome_review import preview_outcomes
from test_handoff_signals import engine, material, many, stage
from test_v55_action_policy import NOW, observation
from test_v57_outcomes import setup
from test_assessment_revisions import ready, scoped, OLD
from test_work_outcomes import ready as work_ready, work_event, WORK, OTHER
from test_v6_delivery_recovery import store, EMAIL


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def deadline(hours=6, **values):
    return dict(due_at=(NOW+timedelta(hours=hours)).isoformat(), **values)


def assessment_rows(binding, **changes):
    return ready(binding)+[
        scoped(binding,'due','assessment_deadline',deadline(**changes)),
        scoped(binding,'estimate','action_cost',dict(minutes=60,cash=0,currency='USD',basis='provider_estimate'))]


def work_rows(binding, **changes):
    return work_ready(binding)+[
        work_event(binding,'due','work_deadline',deadline(**changes)),
        work_event(binding,'estimate','work_cost',dict(minutes=60,cash=0,currency='USD',basis='provider_estimate'))]


@pytest.mark.parametrize('builder,action',[(assessment_rows,'complete_known_step'),(work_rows,'start_allocated_task')])
def test_ready_exact_due_time_uses_estimate_and_preserves_action(builder,action):
    changed,_,report=engine(builder)
    item=changed.evaluated[0]
    assert not report['quarantine']
    assert item.decision.next_action==action
    assert item.decision.priority_key.supported_urgency==1
    assert item.decision.priority_key.supported_deadline_order==-int((NOW+timedelta(hours=6)).timestamp())
    assert item.assessment.account_state['_reviewed_signal_order']['status']=='estimated_feasible'


@pytest.mark.parametrize('value',[0,True,'1790000000','2026-09-08T12:00:00',None])
def test_due_time_requires_aware_datetime_and_rejects_epoch_coercion(value):
    with pytest.raises(ValidationError):ActionDeadline.model_validate(dict(due_at=value))


@pytest.mark.parametrize('value',[True,'30',-1,float('inf'),float('nan'),10001])
def test_minimum_lead_is_bounded_typed_finite_minutes(value):
    with pytest.raises(ValidationError):ActionDeadline.model_validate(deadline(minimum_lead_minutes=value))


@pytest.mark.parametrize('builder,predicate',[(assessment_rows,'assessment_deadline'),(work_rows,'work_deadline')])
@pytest.mark.parametrize('failure',['missing_duration','unknown_duration','boolean_duration','stale_duration','seed','undated','stale','expired_evidence','elapsed','insufficient','minimum_lead','conflict'])
def test_unknown_or_infeasible_due_times_do_not_supply_urgency(builder,predicate,failure):
    def rows(binding):
        records=builder(binding)
        due=next(r for r in records if r['event_id']=='due')
        estimate=next(r for r in records if r['event_id']=='estimate')
        if failure=='missing_duration':records.remove(estimate)
        if failure=='unknown_duration':estimate['value']['minutes']=None
        if failure=='boolean_duration':estimate['value']['minutes']=True
        if failure=='stale_duration':estimate['event_at']=(NOW-timedelta(days=30)).isoformat()
        if failure=='seed':due['source_kind']='reviewed_seed'
        if failure=='undated':due['event_at']=None
        if failure=='stale':due['event_at']=(NOW-timedelta(days=30)).isoformat()
        if failure=='expired_evidence':due['expires_at']=(NOW-timedelta(minutes=1)).isoformat()
        if failure=='elapsed':due['value']=deadline(hours=-1)
        if failure=='insufficient':due['value']=deadline(hours=.5)
        if failure=='minimum_lead':due['value']=deadline(hours=6,minimum_lead_minutes=400)
        if failure=='conflict':records.append(dict(due,event_id='other-due',value=deadline(hours=5)))
        return records
    result,_,_=engine(rows)
    item=result.evaluated[0]
    assert item.decision.priority_key.supported_urgency==0
    assert item.decision.next_action=='verify'
    assert item.assessment.verification_tasks
    assert item.assessment.lifecycle=='watch' if failure=='elapsed' else item.assessment.lifecycle=='active'
    assert item.canonical.observations[0].vacancy_state=='verified_open'


@pytest.mark.parametrize('identity',[OLD,dict(OLD,revision='terms-2')])
def test_inactive_assessment_due_time_does_not_borrow_or_change_material(identity):
    first,_,_=engine(ready)
    changed,_,_=engine(lambda b:ready(b)+[scoped(b,'old-due','assessment_deadline',deadline(hours=-1),identity)])
    assert changed.evaluated[0].decision.next_action=='complete_known_step'
    assert material(first)==material(changed)


def test_inactive_allocation_cannot_supply_duration_for_selected_due_time():
    def rows(binding):
        records=work_rows(binding)
        next(r for r in records if r['event_id']=='estimate')['work']=OTHER
        return records
    changed,_,_=engine(rows)
    assert changed.evaluated[0].decision.next_action=='verify'
    assert any(t['missing_fact']=='action_deadline_duration' for t in changed.evaluated[0].assessment.verification_tasks)


def test_reminder_does_not_extend_due_time_but_explicit_same_scope_correction_does():
    def rows(binding,correct):
        records=assessment_rows(binding,hours=-1)
        records.append(scoped(binding,'extension','assessment_deadline',deadline(hours=6),
            event_at=(NOW-timedelta(minutes=20)).isoformat(),observed_at=NOW.isoformat(),
            **({'supersedes':['due']} if correct else {})))
        return records
    unresolved,_,_=engine(lambda b:rows(b,False))
    extended,_,report=engine(lambda b:rows(b,True))
    assert unresolved.evaluated[0].decision.next_action=='verify'
    assert extended.evaluated[0].decision.priority_key.supported_urgency==1
    assert report['projections'][0].dispositions['due']=='superseded'
    assert len(report['events'])==7


@pytest.mark.parametrize('builder,state_predicate,scope_kw,state',[
    (assessment_rows,'assessment_state',{},'completed'),
    (work_rows,'work_state',{},'submitted')])
def test_completed_subject_does_not_inherit_due_time_conflicts(builder,state_predicate,scope_kw,state):
    def rows(binding):
        records=builder(binding,hours=-1)
        if state_predicate=='assessment_state':
            records.append(scoped(binding,'done',state_predicate,state,supersedes=['invite'],
                actor='user',source_kind='user_report',event_at=(NOW-timedelta(minutes=20)).isoformat(),observed_at=NOW.isoformat()))
        else:records.append(work_event(binding,'done',state_predicate,state,actor='user',source_kind='user_report'))
        due=next(r for r in records if r['event_id']=='due')
        records.append(dict(due,event_id='conflicting-due',value=deadline(hours=1)))
        return records
    changed,_,_=engine(rows)
    assert changed.evaluated[0].decision.next_action in {'await_response','reconcile_payment_status'}
    assert changed.evaluated[0].decision.priority_key.supported_urgency==0
    assert all('deadline' not in t['missing_fact'] for t in changed.evaluated[0].assessment.verification_tasks)


def test_public_deadline_cannot_supply_private_urgency_and_hard_eligibility_wins(monkeypatch):
    _,binding=setup()
    monkeypatch.setattr(CONFIG.v55,'account_states',[dict(canonical_id=binding.canonical_id,
        deadline=(NOW+timedelta(hours=1)).isoformat(),
        _reviewed_signal_order=dict(authority='reviewed_signal_order_v1',supported_urgency=1))])
    changed,_,_=engine(work_ready)
    assert changed.evaluated[0].decision.priority_key.supported_urgency==0
    blocked=observation();blocked.job.description+=' Applicants must live in the United States.'
    changed,_,_=engine(work_rows,[blocked])
    assert changed.evaluated[0].decision.action_band.value=='reject'
    assert changed.evaluated[0].decision.priority_key.supported_urgency==0


def test_offset_equivalent_deadline_is_materially_identical_and_earlier_due_time_ranks_first():
    first,_,_=engine(assessment_rows)
    def offset(binding):
        records=assessment_rows(binding)
        due=next(r for r in records if r['event_id']=='due')
        from datetime import timezone
        due['value']['due_at']=(NOW+timedelta(hours=6)).astimezone(timezone(timedelta(hours=5,minutes=30))).isoformat()
        return records
    equivalent,_,_=engine(offset)
    earlier,_,_=engine(lambda b:assessment_rows(b,hours=3))
    assert material(first)==material(equivalent)
    assert earlier.evaluated[0].decision.priority_key.sort_tuple<first.evaluated[0].decision.priority_key.sort_tuple
    cold,_=setup()
    assert first.evaluated[0].decision.priority_key.sort_tuple<cold.evaluated[0].decision.priority_key.sort_tuple
    assert 'supported_urgency' not in cold.evaluated[0].decision.priority_key.model_dump()


@pytest.mark.parametrize('text,uid,expected',[
    ('Updated budget: USD 800 fixed.',10,1),
    ('The scope now requires self-hosted n8n migration.',10,1),
    ('Thanks for the updated scope!',10,0),
    ('Updated budget: USD 800 fixed.',20,0),
    ('<blockquote>Updated budget: USD 800 fixed.</blockquote>',10,0)])
def test_substantive_buyer_priority_uses_actor_and_original_post_time(text,uid,expected):
    from test_community_revisions import native,update,NOW as COMMUNITY_NOW
    thread,child,row=native(update(text,uid=uid))
    assert row.decision.priority_key.substantive_buyer_update==expected
    assert child.job.posted_at==COMMUNITY_NOW-timedelta(days=30)
    assert row.decision.next_action not in {'start_allocated_task','complete_known_step','bid'}
    assert row.decision.priority_key.supported_urgency==0


def urgent_universe(count=5, same_employer=False):
    observations=many(count+1)
    if same_employer:
        for raw in observations[:-1]:raw.job.company='One employer'
    result=evaluate_observations(observations,as_of=NOW)
    bindings,events=[],[]
    for item in result.evaluated:
        if item.canonical.observations[0].observation_id==observations[-1].observation_id:continue
        binding=OutcomeBinding(scope=OutcomeScope(provider='example',portal='contractors',
            role=item.canonical.canonical_id,account='local',attempt='1'),canonical_id=item.canonical.canonical_id,
            observation_id=item.canonical.observations[0].observation_id,role_url=item.job.url,reviewed=True)
        bindings.append(binding)
        for raw in work_rows(binding):
            raw['event_id']=item.canonical.canonical_id+'.'+raw['event_id'];events.append(raw)
    changed,report=preview_outcomes(result,events,bindings,as_of=NOW)
    assert not report['quarantine']
    return changed


@pytest.mark.parametrize('count,displace',[(5,True),(4,False)])
def test_discovery_displacement_requires_every_ready_slot_supported_urgent(store,count,displace):
    result=urgent_universe(count)
    report=stage(store,result)['destinations'][0]
    reservation=report['reservation']
    assert report['counts']['selected_cards']==5
    assert (reservation['reason']=='supported_urgent_existing')==displace
    assert bool(reservation['displaced_discovery'])==displace
    assert len(reservation['urgent_existing'])==(5 if displace else 0)
    if displace:
        assert all(r['evidence']['predicate']=='work_deadline' and r['evidence']['target']==WORK for r in reservation['urgent_existing'])
    else:assert reservation['canonical_id'] is not None
    transport=DigestTransport(store.delivery)
    draft=transport.prepare(EMAIL,report['intent_ids'],now=100)
    assert transport.inspect(draft)['status']=='pending'
    assert store.conn.execute('SELECT count(*) FROM delivery_baselines').fetchone()[0]==0


def test_breadth_cap_prevents_false_urgent_displacement(store):
    result=urgent_universe(same_employer=True)
    report=stage(store,result)['destinations'][0]
    assert report['reservation']['reason']=='suitable_new_discovery'
    assert report['reservation']['urgent_existing']==[]
    assert report['counts']['selected_cards']<=CONFIG.v55.max_per_employer+1


def test_priority_serializer_supports_partial_fields_without_changing_legacy_shape():
    from jobhound.v41.models import PriorityKey
    key=PriorityKey(supported_urgency=1,stable_tiebreaker='one')
    assert key.model_dump(include={'stable_tiebreaker'})=={'stable_tiebreaker':'one'}
    assert 'supported_urgency' not in key.model_dump(exclude={'supported_urgency'})
    assert PriorityKey().model_dump(include={'supported_urgency'})=={}


def test_private_assessment_deadline_does_not_reopen_closed_public_vacancy():
    result,_,_=engine(assessment_rows,[observation(vacancy_state='explicitly_closed')])
    item=result.evaluated[0]
    assert item.decision.lifecycle=='closed'
    assert item.decision.next_action=='skip'
    assert item.decision.priority_key.supported_urgency==0


@pytest.mark.parametrize('predicate,builder,field,bad',[
    ('assessment_deadline',assessment_rows,'actor','user'),
    ('work_deadline',work_rows,'actor','user'),
    ('assessment_deadline',assessment_rows,'schema_version',1),
    ('work_deadline',work_rows,'schema_version',2),
    ('assessment_deadline',assessment_rows,'assessment',None),
    ('work_deadline',work_rows,'work',None)])
def test_due_time_needs_counterparty_actor_and_exact_versioned_scope(predicate,builder,field,bad):
    _,binding=setup()
    due=next(r for r in builder(binding) if r['predicate']==predicate)
    due[field]=bad
    with pytest.raises(ValidationError):OutcomeEvent.model_validate(due)
