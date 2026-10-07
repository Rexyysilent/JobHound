"""Public vetoes stay coherent through native assessment and later risk review."""
import asyncio
from datetime import timedelta

import pytest

from jobhound.config import CONFIG
from jobhound.v41 import engine
from jobhound.v41.digest import build_digest
from jobhound.v41.models import ListingObservation
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.store import V41Store
from test_v55_action_policy import observation,NOW
from test_v57_outcomes import setup,assessment_events


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    monkeypatch.setattr(CONFIG.v55,'account_states',[])


def assess(row,*others):
    result=engine.evaluate_observations([row,*others],as_of=NOW)
    assert result.accounting_ok and build_digest(result,include_all=True).accounting_ok
    return result


def blocked(row,readiness='blocked'):
    assert row.decision.action_band.value=='reject'
    assert row.assessment.action_readiness==readiness
    assert row.decision.priority_key.action_readiness==0
    assert row.decision.next_action==row.assessment.next_action=='skip'
    view=row.assessment.evidence_dimensions['readiness']
    assert view.state==readiness and view.value['next_action']=='skip'
    assert view.value['next_step']==row.decision.next_step
    assert 'Do not apply' in row.decision.next_step


@pytest.mark.parametrize('case', ['location','language','pay','non_job','scam'])
def test_native_veto_never_retains_application_readiness(case):
    row=observation()
    if case=='location':
        row.job.description+='\nApplicants must reside in the United States.\n'
    elif case=='language':
        row.job.description+='\nNative German fluency is required.\n'
    elif case=='pay':
        row.job.pay_raw='USD 1 per working hour'
    elif case=='non_job':
        row.job.title='Automation consulting services for sale'
        row.job.description='I sell automation consulting services. My fixed project service starts at USD 500.'
    else:
        row.job.description+='\nPay a registration fee and a refundable security deposit. Contact us via Telegram only.\n'
    item=assess(row).evaluated[0]
    blocked(item)
    assert item.canonical.observations[0].job.description==row.job.description


@pytest.mark.parametrize('case',['public_closure','deadline'])
def test_closed_public_record_preserves_closed_status_without_ready_label(case):
    row=observation()
    if case=='public_closure':
        row.vacancy_state='explicitly_closed'
    else:
        row.raw_payload['application_deadline']=(NOW-timedelta(hours=1)).isoformat()
    item=assess(row).evaluated[0]
    assert item.decision.lifecycle=='closed'
    blocked(item,'closed')


@pytest.mark.parametrize('state',[
    {'project_access':'blocked'},
    {'application_state':'applied'},
    {'assessment_state':'completed'},
])
def test_account_waits_remain_distinct_from_public_rejection(state):
    account=ListingObservation(observation_id='account',source='account_state',origin='account_state',
        parent_observation_ids=['one'],captured_at=NOW,
        raw_payload=state|{'observed_at':NOW.isoformat()})
    item=assess(observation(),account).evaluated[0]
    assert item.decision.action_band.value=='verify' and item.decision.lifecycle=='watch'
    assert item.assessment.action_readiness=='watch'
    assert item.decision.next_action in {'verify','await_response','await_change'}


def test_real_ready_application_and_allocated_work_keep_authority():
    assert assess(observation()).evaluated[0].decision.next_action=='apply'
    account=ListingObservation(observation_id='account',source='account_state',origin='account_state',
        parent_observation_ids=['one'],captured_at=NOW,
        raw_payload={'project_access':'accessible','task_allocation':'allocated','observed_at':NOW.isoformat()})
    item=assess(observation(),account).evaluated[0]
    assert item.assessment.action_readiness=='allocated' and item.decision.next_action=='start_allocated_task'


def test_captured_assessment_does_not_override_public_veto_or_discard_history():
    row=observation()
    row.job.description+='\nApplicants must reside in the United States.\n'
    result,binding=setup(row)
    changed,report=preview_outcomes(result,assessment_events(binding),[binding],as_of=NOW)
    blocked(changed.evaluated[0])
    assert report['counts']['accepted']==3 and len(report['events'])==3
    assert changed.evaluated[0].canonical.outcome_projection.value('assessment_state')=='invited'


def test_later_mocked_risk_veto_replaces_action_and_resorts_entire_result(monkeypatch):
    result=assess(observation('a'),observation('b'))
    victim=result.evaluated[0]
    before_pay=victim.assessment.selected_pay.model_dump()
    original_ids={row.canonical.canonical_id for row in result.evaluated}
    async def reviewed(jobs):
        victim.job.scam_score=1.0
        return 1
    monkeypatch.setattr(engine,'scam_llm_pass',reviewed)
    assert asyncio.run(engine.refine_scam_with_llm(result))==1
    blocked(victim)
    assert victim.decision.terminal_reason=='reject_scam'
    assert victim.assessment.selected_pay.model_dump()==before_pay
    assert {row.canonical.canonical_id for row in result.evaluated}==original_ids
    assert result.evaluated[0].decision.action_band.value=='primary'
    assert result.evaluated[0].decision.next_action=='apply'
    assert result.evaluated[-1] is victim
    assert result.accounting_ok and build_digest(result,include_all=True).accounting_ok


def test_reassessment_restores_ready_action_without_stale_rejected_state(tmp_path):
    row=observation()
    row.job.description+='\nApplicants must reside in the United States.\n'
    rejected=assess(row)
    blocked(rejected.evaluated[0])
    restored=assess(observation())
    assert restored.evaluated[0].canonical.canonical_id==rejected.evaluated[0].canonical.canonical_id
    assert restored.evaluated[0].assessment.action_readiness=='application_ready'
    store=V41Store(tmp_path/'state.db')
    try:
        assert not store.annotate_transitions(rejected)
        store.record_run(rejected)
        assert store.annotate_transitions(restored)
        assert restored.evaluated[0].decision.next_action=='apply'
    finally:
        store.close()
