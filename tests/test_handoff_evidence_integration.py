"""Parent integration checks start at native feed input, before normalization."""
from dataclasses import replace
import pytest
from jobhound.config import CONFIG
from jobhound.filters.eligibility import default_profile
from jobhound.run_context import RunContext,run_scope
from jobhound.v41.engine import evaluate_raw
from test_v55_action_policy import NOW,DESCRIPTION
from test_v57_dated_language_profile import OLD_CV,WRITTEN_AVOID,SPOKEN


def native(description, pay='USD 10 per working hour'):
    return [dict(source='ashby',raw=dict(id='native-scope',title='Bengali AI Response Evaluator',
        _company='Example',jobUrl='https://jobs.ashbyhq.com/example/native-scope',location='India',
        isRemote=True,publishedAt=NOW.isoformat(),descriptionPlain=description,
        compensation=dict(scrapeableCompensationSalarySummary=pay)))]


def test_native_feed_preserves_single_project_payout_constraint(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    result=evaluate_raw(native(DESCRIPTION+'\nWire payout requires amount above USD 50.',
                               'USD 18 fixed project budget'),as_of=NOW)
    item=result.evaluated[0]
    assert result.accounting_ok
    assert item.assessment.payout_feasibility['state']=='single_amount_infeasible'
    assert item.assessment.payout_feasibility['amount']=='18.0'
    assert item.assessment.selected_pay.observation_id==item.canonical.best_observation.observation_id
    assert any(task['missing_fact']=='payout_feasibility' for task in item.decision.verification_tasks)


@pytest.mark.parametrize('requirement,avoided',[
    ('Advanced written Hindi proficiency required.',True),
    ('Spoken Hindi fluency required.',False),
])
def test_native_feed_respects_dated_modality_preference(monkeypatch,requirement,avoided):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    profile=replace(default_profile(),language_evidence=[OLD_CV,WRITTEN_AVOID,SPOKEN])
    context=RunContext.capture(profile=profile,as_of=NOW)
    with run_scope(context):
        result=evaluate_raw(native(DESCRIPTION+'\n'+requirement),as_of=NOW)
    assert result.accounting_ok
    item=result.evaluated[0]
    assert any('preference_avoided' in reason for reason in item.assessment.blockers)==avoided
    assert any(check['language']=='hindi' for check in item.assessment.requirements_assessment.profile_language_evidence)
    assert context.profile().language_evidence==profile.language_evidence
