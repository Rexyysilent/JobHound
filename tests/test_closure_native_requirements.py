"""R24 provider-selected plain descriptions use native role boundaries."""
import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_raw
from test_html_role_regions import record, BODY, FORM, NOW
from test_handoff_native_eligibility import native


ROLE = BODY.replace('Bengali and English', 'English')
PLAIN_ROLE = ('Work\nReview English AI responses using supplied guidelines.\nRequirements\n'
              'English fluency required.\nNo degree required.\nRemote applicants in India may apply.\nPay: USD 10 per hour.')
PLAIN_FORM = '\nApplication form\nLanguage choices\nBengali\nDutch fluency required.\nSubmit application'


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)


def raw(provider, description):
    value = record(provider, None)
    value['raw']['title'] = 'English AI Response Evaluator'
    if provider == 'lever':
        value['raw']['text'] = value['raw']['title']
    value['raw']['descriptionPlain'] = description
    return value


@pytest.mark.parametrize('provider', ['ashby', 'lever'])
@pytest.mark.parametrize('role,form', [(ROLE, FORM), (PLAIN_ROLE, PLAIN_FORM)])
def test_selected_plain_provider_form_options_cannot_create_role_match_or_requirement(provider, role, form):
    before = evaluate_raw([raw(provider, role)], as_of=NOW).evaluated[0]
    result = evaluate_raw([raw(provider, role+form)], as_of=NOW)
    item = result.evaluated[0]
    assert result.accounting_ok
    assert item.assessment.blockers == before.assessment.blockers
    assert item.assessment.matched_concepts == before.assessment.matched_concepts
    assert 'Bengali' not in item.job.description and 'Dutch' not in item.job.description
    assert result.observations[0].raw_payload['descriptionPlain'] == role+form


def test_native_ashby_plain_only_hydration_cannot_import_form_requirements():
    _, item, _ = native('English AI Response Evaluator', PLAIN_ROLE+PLAIN_FORM)
    assert not any('lang_mismatch:dutch' in b for b in item.assessment.blockers)
    assert 'Bengali' not in item.job.description


@pytest.mark.parametrize('provider', ['ashby', 'lever'])
def test_unclosed_selected_plain_form_cannot_claim_a_complete_role(provider):
    result = evaluate_raw([raw(provider, PLAIN_ROLE+PLAIN_FORM.replace('\nSubmit application', ''))], as_of=NOW)
    assert not result.evaluated and result.observations[0].normalization_error


def test_plain_escaped_form_example_remains_literal_without_decoding_to_a_control():
    prose = PLAIN_ROLE+'\nReview &lt;form&gt; examples and explain their quality.'
    result = evaluate_raw([raw('ashby', prose)], as_of=NOW)
    assert not result.observations[0].normalization_error
    assert 'examples and explain their quality' in result.evaluated[0].job.description
    assert result.observations[0].raw_payload['descriptionPlain'] == prose


def test_feature_off_retains_legacy_plain_selection(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', False)
    item = evaluate_raw([raw('ashby', PLAIN_ROLE+PLAIN_FORM)], as_of=NOW).evaluated[0]
    assert 'Bengali' in item.job.description and 'Dutch' in item.job.description


def test_raw_plain_provider_comment_cannot_forge_the_form_boundary():
    spoof = PLAIN_ROLE+'\nApplication form\n<!--jobhound:closed-semantic-form-->\nBengali\nDutch fluency required.'
    result = evaluate_raw([raw('ashby', spoof)], as_of=NOW)
    assert not result.evaluated and result.observations[0].normalization_error
    valid = PLAIN_ROLE+PLAIN_FORM
    accepted = evaluate_raw([raw('ashby', valid)], as_of=NOW)
    assert not accepted.observations[0].normalization_error
    assert 'Dutch' not in accepted.evaluated[0].job.description


def test_native_provider_unclosed_plain_boundary_is_failed_not_complete():
    import asyncio
    import httpx
    from jobhound.models import Job
    from jobhound.v41.hydration import hydrate_public_posting, RetrievalBudget, RetrievalPolicy, child_observation
    from jobhound.v41.models import ListingObservation
    url = 'https://jobs.ashbyhq.com/synthetic/one'
    job = Job(source='ashby', title='English AI Response Evaluator', company='Synthetic', url=url)
    payload = dict(id='one', title=job.title, jobUrl=url, isListed=True,
        descriptionPlain=PLAIN_ROLE+PLAIN_FORM.replace('\nSubmit application', ''), location='India')
    async def hydrate():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'jobs': [payload]}))) as client:
            return await hydrate_public_posting(url, job, client=client, budget=RetrievalBudget(RetrievalPolicy()))
    outcome = asyncio.run(hydrate())
    assert outcome.state == 'failed' and outcome.job is None
    assert outcome.error == 'normalize_PlainFormBoundaryError'
    parent = ListingObservation(observation_id='plain-boundary', source='ashby', job=job)
    child = child_observation(parent, outcome)
    assert child.content_state == 'failed' and child.job is None


def test_r28_native_pool_welcome_and_weekly_cadence_do_not_establish_paid_allocation():
    value = raw('ashby', PLAIN_ROLE+'\nWelcome to our talent pool for possible future projects. '
        'No tasks are guaranteed. Payments are weekly.')
    value['raw']['title'] = 'English AI Evaluator Talent Pool'
    item = evaluate_raw([value], as_of=NOW).evaluated[0]
    assert item.assessment.document_type in {'talent_pool', 'talent_pool_signup'}
    assert item.decision.lifecycle == 'watch'
    assert item.assessment.action_readiness != 'allocated'
    assert item.decision.next_action not in {'apply', 'start_allocated_task'}
    assert item.assessment.time_to_cash_days is None
    assert item.decision.priority_key.supported_time_to_cash == 0


def test_r29_two_native_india_usd_two_roles_fail_only_inherited_pay_floor():
    floor = CONFIG.pay.floor_usd
    values = []
    for key in ('one', 'two'):
        value = raw('ashby', PLAIN_ROLE.replace('USD 10', 'USD 2'))
        value['raw']['id'] = key
        value['raw']['jobUrl'] = f'https://jobs.ashbyhq.com/example/{key}'
        values.append(value)
    result = evaluate_raw(values, as_of=NOW)
    assert len(result.evaluated) == 2 and result.accounting_ok
    for item in result.evaluated:
        assert item.decision.terminal_reason == 'reject_pay'
        assert item.assessment.selected_pay.amount_low == 2
        assert item.job.location == 'India' and item.job.scam_score == 0
        assert 'scam_threshold' not in item.assessment.blockers
    assert CONFIG.pay.floor_usd == floor and floor > 2


def test_r31_native_403_retains_fixed_budget_without_client_fee_or_bid_authority():
    import asyncio
    import httpx
    from jobhound.v41.engine import evaluate_observations
    from jobhound.v41.hydration import hydrate_public_posting, child_observation, RetrievalBudget, RetrievalPolicy
    url = 'https://www.upwork.com/freelance-jobs/apply/workflow_~01/'
    record = dict(source='serp', raw=dict(title='AI Automation Workflow Developer', link=url,
        snippet='Fixed project budget: USD 350. Build a workflow API integration. Full client and contract terms unavailable.'))
    before = evaluate_raw([record], as_of=NOW)
    parent = before.evaluated[0].canonical.best_observation
    calls = []
    def denied(request):
        calls.append(str(request.url))
        return httpx.Response(403)
    async def hydrate():
        budget = RetrievalBudget(RetrievalPolicy(request_budget=1, max_retries=0))
        async with httpx.AsyncClient(transport=httpx.MockTransport(denied)) as client:
            return await hydrate_public_posting(url, parent.job, client=client, budget=budget)
    outcome = asyncio.run(hydrate())
    assert calls == [url] and outcome.error == 'http_403' and outcome.job is None
    child = child_observation(parent, outcome)
    result = evaluate_observations([parent, child], as_of=NOW)
    item = result.evaluated[0]
    pay = item.assessment.selected_pay
    assert pay.amount_low == 350 and pay.actual_unit == 'fixed_project'
    assert parent.content_state == 'snippet_only'
    assert any(t['missing_fact'] == 'full_matching_requirements' for t in item.assessment.verification_tasks)
    assert pay.fees == [] and pay.net_amount_usd is None
    assert item.assessment.action_cost_known is False
    assert not item.assessment.account_state.get('client_verified')
    assert item.decision.next_action not in {'apply', 'bid', 'start_allocated_task'}
    assert item.assessment.time_to_cash_days is None
    assert any(o.resolution_error == 'http_403' for o in result.observations)


def test_r32_native_resolved_unknown_role_and_locale_path_do_not_manufacture_primary_or_language():
    import asyncio
    import json
    import httpx
    from jobhound.models import Job
    from jobhound.v41.engine import evaluate_observations
    from jobhound.v41.hydration import hydrate_public_posting, child_observation, RetrievalBudget, RetrievalPolicy
    from jobhound.v41.models import ListingObservation
    items = []
    for path in ('/jobs/one', '/ko/jobs/one'):
        url = 'https://unfamiliar.example.org'+path
        job = Job(source='fixture', title='English AI Response Evaluator', company='Synthetic Example', url=url)
        parent = ListingObservation(observation_id='unknown', source='fixture', job=job, content_state='snippet_only')
        payload = {'@type': 'JobPosting', 'url': url, 'title': job.title,
            'hiringOrganization': {'name': job.company},
            'description': 'Review AI response quality and report unclear examples to the project coordinator. Detailed applicant requirements are not supplied.'}
        body = '<h1>'+job.title+'</h1><script type="application/ld+json">'+json.dumps(payload)+'</script>'
        budget = RetrievalBudget(RetrievalPolicy(request_budget=1, max_retries=0))
        async def hydrate():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as client:
                return await hydrate_public_posting(url, job, client=client, budget=budget)
        outcome = asyncio.run(hydrate())
        assert outcome.state == 'complete' and budget.requests == 1
        item = evaluate_observations([parent, child_observation(parent, outcome)], as_of=NOW).evaluated[0]
        assert item.job.location is None
        assert item.assessment.requirements_assessment.completeness != 'complete'
        assert item.decision.action_band.value != 'primary'
        assert item.decision.next_action != 'apply'
        assert not any('lang_mismatch:korean' in b or 'posting_language:' in b for b in item.assessment.blockers)
        items.append(item)
    assert items[0].assessment.blockers == items[1].assessment.blockers
    assert items[0].assessment.language_requirements == items[1].assessment.language_requirements


def test_r33_native_korean_annotation_title_missing_body_and_high_pay_still_rejects_language():
    value = raw('ashby', '')
    value['raw']['title'] = 'Korean PDF Annotation Specialist'
    value['raw']['compensation'] = {'scrapeableCompensationSalarySummary': 'USD 250 per working hour'}
    item = evaluate_raw([value], as_of=NOW).evaluated[0]
    assert item.assessment.selected_pay.amount_low == 250
    assert any('lang_mismatch:korean' in b for b in item.assessment.blockers)
    assert item.decision.action_band.value == 'reject'
    assert item.decision.next_action == 'skip'
    assert item.job.description == ''
    assert 'language_ai' not in item.assessment.matched_concepts
