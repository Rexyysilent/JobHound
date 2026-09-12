"""End-to-end release controls: policy, source evidence and presentation."""
from datetime import datetime, timedelta, timezone

import pytest

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.engine import evaluate_observations, decision_fingerprint
from jobhound.v41.models import ListingObservation, SourceKind
from jobhound.v41.digest import build_digest

NOW = datetime(2026, 9, 8, 9, tzinfo=timezone.utc)
DESCRIPTION = '''Work
Review Bengali and English AI responses using supplied guidelines.
Requirements
Bengali and English fluency required.
No prior AI-training experience or university degree required.
Remote applicants in India may apply.
This is a contractor role reviewing supplied examples. Follow the provided
rubric and report any unclear examples to the project coordinator.
'''


@pytest.fixture(autouse=True)
def release_policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)


def observation(key='one', **updates):
    job = Job(source='ashby', title='Bengali AI Response Evaluator', company='Example',
              url=f'https://jobs.ashbyhq.com/example/{key}', description=DESCRIPTION,
              location='India', is_remote=True, region_tags=['india'],
              posted_at=NOW-timedelta(days=1), pay_raw='USD 10 per working hour')
    row = ListingObservation(observation_id=key,source='ashby',job=job,
        normalized_url=job.url,original_url=job.url,
        source_kind=SourceKind.ORIGINAL_ATS,source_confidence=.92,
        content_state='complete',identity_state='exact',vacancy_state='verified_open',
        application_route_state='observed_public_route',verified_open_at=NOW,
        captured_at=NOW)
    return row.model_copy(update=updates, deep=True)


def evaluate(row, *more):
    return evaluate_observations([row,*more],as_of=NOW)


def test_full_qualified_original_primary_apply():
    result=evaluate(observation())
    item=result.evaluated[0]
    assert item.decision.action_band.value == 'primary', (item.assessment.blockers,item.assessment.unresolved,item.decision.verification_tasks)
    assert item.decision.next_action == 'apply'
    assert result.accounting_ok
    text=build_digest(result,include_all=True).text
    assert 'eff ~' not in text


@pytest.mark.parametrize('change', [dict(content_state='snippet_only'),dict(vacancy_state='unknown'),dict(identity_state='uncertain'),dict(application_route_state='account_unknown')])
def test_each_readiness_dimension_independent(change):
    item=evaluate(observation(**change)).evaluated[0]
    assert item.decision.action_band.value=='verify'
    assert item.decision.verification_tasks


def test_tax_advice_not_applicant_credential():
    row=observation()
    row.job.description += '\nConsult a tax professional about your tax obligations.\n'
    item=evaluate(row).evaluated[0]
    assert item.decision.action_band.value=='primary',item.assessment.blockers


def test_recorded_unit_does_not_use_hourly_floor():
    row=observation()
    row.job.pay_raw='INR 500 per recorded audio hour'
    item=evaluate(row).evaluated[0]
    assert item.job.pay_ok is None
    assert item.job.effective_hourly_usd is None
    assert item.assessment.selected_pay.min_hourly_usd is None
    assert 'working-hour equivalent unknown' in build_digest(evaluate(row),include_all=True).text


def test_old_copy_watch_original_checked_open_survives():
    copy=observation(source_kind=SourceKind.AGGREGATOR_UNRESOLVED,content_state='snippet_only',vacancy_state='unknown')
    copy.job.posted_at=NOW-timedelta(days=180)
    item=evaluate(copy).evaluated[0]
    assert item.decision.lifecycle=='watch'
    assert build_digest(evaluate(copy),include_all=True).display_counts['suppressed_watch']==1
    original=observation()
    original.job.posted_at=copy.job.posted_at
    assert evaluate(original).evaluated[0].decision.action_band.value=='primary'


def test_known_account_block_never_recommends_new_application():
    row=observation()
    account=ListingObservation(observation_id='account',source='account_state',origin='account_state',
        parent_observation_ids=['one'],captured_at=NOW,
        raw_payload={'project_access':'inaccessible','observed_at':NOW.isoformat()})
    result=evaluate(row,account)
    assert result.evaluated[0].decision.lifecycle=='watch'
    assert result.evaluated[0].decision.next_action!='apply'
    assert result.accounting_ok


def test_account_allocated_is_not_public_application():
    account=ListingObservation(observation_id='account',source='account_state',origin='account_state',
        parent_observation_ids=['one'],captured_at=NOW,
        raw_payload={'project_access':'accessible','task_allocation':'allocated','observed_at':NOW.isoformat()})
    item=evaluate(observation(),account).evaluated[0]
    assert item.decision.next_action=='start_allocated_task'
    assert item.decision.priority_key.action_readiness==3


def test_caps_and_shuffle_have_one_accounting_outcome():
    rows=[observation(str(i)) for i in range(9)]
    result=evaluate_observations(rows,as_of=NOW)
    shuffled=evaluate_observations(list(reversed(rows)),as_of=NOW)
    assert decision_fingerprint(result)==decision_fingerprint(shuffled)
    digest=build_digest(result,include_all=True)
    assert digest.accounting_ok
    assert len(digest.displayed)<=5
    assert len(result.evaluated)==9
