"""R21/R26/R27 synthetic native response witnesses, not original-page captures."""
import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.hydration import hydrate_public_posting, child_observation, RetrievalBudget, RetrievalPolicy
from jobhound.v41.models import ListingObservation, SourceKind


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


GENERAL = 'Work\nReview Bengali and English AI responses using supplied guidelines.\nRequirements\nBengali and English fluency required.\nNo university degree or prior experience required.\nRemote applicants in India may apply.'


def posting(url, title, description=GENERAL, *, country='India', pay=15):
    return {'@context': 'https://schema.org', '@type': 'JobPosting',
        'url': url, 'identifier': {'value': url.rstrip('/').rsplit('/', 1)[-1]},
        'title': title, 'hiringOrganization': {'name': 'Example'},
        'description': description, 'jobLocationType': 'TELECOMMUTE',
        'applicantLocationRequirements': {'@type': 'Country', 'name': country},
        'baseSalary': {'currency': 'USD', 'value': {'value': pay, 'unitText': 'HOUR'}}}


def html(records, surrounding=''):
    return surrounding + '<script type="application/ld+json">' + json.dumps({'@graph': records}) + '</script>'


def hydrate(url, title, body, *, key='role'):
    parent = ListingObservation(observation_id=key, source='fixture_discovery',
        job=Job(source='fixture_discovery', url=url, title=title, company='Example'),
        original_url=url, normalized_url=url, source_kind=SourceKind.ORIGINAL_EMPLOYER,
        content_state='snippet_only', identity_state='uncertain')
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=body)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await hydrate_public_posting(url, parent.job, client=client,
                budget=RetrievalBudget(RetrievalPolicy(max_retries=0)))
    outcome = asyncio.run(run())
    assert seen == [url]
    child = child_observation(parent, outcome)
    result = evaluate_observations([parent, child], as_of=datetime.now(timezone.utc))
    assert result.accounting_ok
    return outcome, child, result


def test_r21_same_title_sibling_postings_keep_native_ids_and_degree_requirements_separate():
    general_url = 'https://careers.sigma.ai/jobs/bengali-general'
    translator_url = 'https://careers.sigma.ai/jobs/bengali-translator'
    title = 'Bengali Linguistic Evaluator'
    translator = GENERAL.replace('No university degree or prior experience required.',
                                 'A university degree in translation is required. Five years of professional translation experience required.')
    records = [posting(translator_url, title, translator), posting(general_url, title)]
    general, general_child, _ = hydrate(general_url, title, html(records), key='general')
    translated, translator_child, _ = hydrate(translator_url, title, html(list(reversed(records))), key='translator')
    assert general.payload['identifier']['value'] == 'bengali-general'
    assert translated.payload['identifier']['value'] == 'bengali-translator'
    assert 'Five years' not in general.job.description
    assert 'Five years' in translated.job.description
    assert 'No university degree' not in translated.job.description
    combined = evaluate_observations([general_child, translator_child], as_of=datetime.now(timezone.utc))
    assert len(combined.evaluated) == 2 and combined.accounting_ok
    indexed = {item.job.url: item for item in combined.evaluated}
    assert indexed[general_url].canonical.canonical_id != indexed[translator_url].canonical.canonical_id
    assert not any(claim.required is True for claim in indexed[general_url].assessment.requirements_assessment.degrees)
    assert any(claim.required is True for claim in indexed[translator_url].assessment.requirements_assessment.degrees)
    assert indexed[translator_url].decision.action_band.value != 'primary'


@pytest.mark.parametrize('reverse', [False, True])
def test_r26_india_filtered_index_is_ambiguous_and_cannot_establish_individual_eligibility(reverse):
    records = [posting('https://www.utest.com/projects/us-only', 'English AI Response Evaluator',
                       GENERAL.replace('India', 'United States'), country='United States'),
               posting('https://www.utest.com/projects/hindi-device', 'Hindi Device Survey',
                       'Requirements\nNative Hindi fluency required. An iPhone 16 device is required. Complete an eligibility survey; selection is pending.')]
    outcome, child, result = hydrate('https://www.utest.com/projects?country=India',
        'English AI Response Evaluator', html(list(reversed(records)) if reverse else records))
    assert outcome.state == 'partial' and outcome.job is None
    assert child.task_availability_state == 'unknown'
    assert all(item.decision.action_band.value != 'primary' for item in result.evaluated)


def test_r26_each_leaf_keeps_its_geography_language_device_and_survey_evidence():
    us_url = 'https://www.utest.com/projects/us-only'
    outcome, child, result = hydrate(us_url, 'English AI Response Evaluator',
        html([posting(us_url, 'English AI Response Evaluator', GENERAL.replace('India', 'United States'), country='United States')]))
    assert outcome.job.location == 'United States'
    assert result.evaluated[0].decision.action_band.value == 'reject'
    hindi_url = 'https://www.utest.com/projects/hindi-device'
    text = 'Work\nRecord Hindi audio for evaluation.\nRequirements\nNative Hindi fluency required.\nAn iPhone 16 device is required.\nRemote applicants in India may apply.\nComplete an eligibility survey; selection is pending.'
    outcome, child, result = hydrate(hindi_url, 'Hindi Device Survey', html([posting(hindi_url, 'Hindi Device Survey', text)]))
    assert 'Native Hindi' in outcome.job.description and 'iPhone 16' in outcome.job.description
    languages = result.evaluated[0].assessment.requirements_assessment.languages
    assert any('hindi' in claim.value for claim in languages)
    assert child.task_availability_state != 'allocated'
    assert result.evaluated[0].decision.next_action != 'start_allocated_task'
    assert result.evaluated[0].decision.action_band.value != 'primary'


def test_r27_campaign_pay_and_generalist_prose_cannot_replace_senior_role_evidence():
    url = 'https://mindrift.ai/jobs/senior-scraping'
    title = 'Senior Python Scraping Engineer'
    text = 'Work\nBuild production web scraping infrastructure in Python.\nRequirements\nSeven years of professional software engineering experience required.\nRemote applicants must reside in the United States.'
    leaf = posting(url, title, text, country='United States', pay=12)
    campaign = posting('https://mindrift.ai/campaign/python', title, GENERAL, pay=250)
    outcome, child, result = hydrate(url, title, html([campaign, leaf],
        '<header>Earn up to USD 250 per hour. Python beginners welcome worldwide.</header>'))
    assert outcome.payload['identifier']['value'] == 'senior-scraping'
    assert outcome.job.pay_raw == 'USD 12 per hour'
    assert outcome.job.location == 'United States'
    assert 'beginners' not in outcome.job.description and 'Seven years' in outcome.job.description
    assessed = result.evaluated[0]
    assert assessed.assessment.seniority == 'senior'
    assert assessed.decision.action_band.value == 'reject'
    assert child.task_availability_state != 'allocated'


@pytest.mark.parametrize('case', ['duplicate-target', 'unscoped-multiple', 'foreign-only'])
def test_ambiguous_or_foreign_jsonld_cannot_be_adopted_on_matching_title_alone(case):
    url = 'https://careers.sigma.ai/jobs/target'
    title = 'Bengali AI Response Evaluator'
    target = posting(url, title)
    if case == 'duplicate-target': records = [target, dict(target, description='Conflicting requirements')]
    elif case == 'foreign-only': records = [posting('https://careers.sigma.ai/jobs/other', title)]
    else:
        target.pop('url')
        records = [target, dict(target, description='Other role requirements')]
    outcome, child, result = hydrate(url, title, html(records))
    assert outcome.state == 'partial' and outcome.job is None
    assert child.identity_state not in {'exact', 'corroborated'}
    assert all(item.decision.action_band.value != 'primary' for item in result.evaluated)


@pytest.mark.parametrize('url_value', [None, [], {'name': 'target'}, 'https://other.example/jobs/target',
                                      '/jobs/target?utm_role_id=other'])
def test_single_explicit_malformed_foreign_or_different_query_identity_stays_partial(url_value):
    url = 'https://careers.sigma.ai/jobs/target'
    title = 'Bengali AI Response Evaluator'
    native = posting(url, title)
    native['url'] = url_value
    outcome, _, _ = hydrate(url, title, html([native]))
    assert outcome.state == 'partial' and outcome.job is None


def test_single_block_without_url_preserves_legacy_identity_check_and_relative_leaf_is_scoped():
    url = 'https://careers.sigma.ai/jobs/target'
    title = 'Bengali AI Response Evaluator'
    native = posting(url, title)
    native.pop('url')
    outcome, _, _ = hydrate(url, title, html([native]))
    assert outcome.state == 'complete' and outcome.identity_state == 'corroborated'
    wrong, child, result = hydrate(url, 'German Robotics Engineer', html([native]))
    assert wrong.identity_state == 'conflict' and child.identity_state == 'conflict'
    assert all(row.decision.action_band.value != 'primary' for row in result.evaluated)
    native['url'] = '/jobs/target'
    outcome, _, _ = hydrate(url, title, html([dict(native, url='/jobs/sibling'), native]))
    assert outcome.payload['url'] == '/jobs/target'


def test_jsonld_base_does_not_rebind_relative_sibling_identity():
    url = 'https://careers.sigma.ai/jobs/target'
    title = 'Bengali AI Response Evaluator'
    body = '<script type="application/ld+json">' + json.dumps({'@base': 'https://other.example/',
        '@graph': [posting('https://other.example/jobs/target', title), posting('/jobs/target', title)]}) + '</script>'
    outcome, _, _ = hydrate(url, title, body)
    assert outcome.payload['url'] == '/jobs/target'


def test_same_host_sibling_redirect_cannot_adopt_sibling_or_cache_its_body():
    url = 'https://careers.sigma.ai/jobs/target'
    sibling = 'https://careers.sigma.ai/jobs/sibling'
    title = 'Bengali AI Response Evaluator'
    budget = RetrievalBudget(RetrievalPolicy(max_retries=0))
    def handler(request):
        if str(request.url) == url:
            return httpx.Response(302, headers={'location': sibling})
        return httpx.Response(200, text=html([posting(sibling, title)]))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await hydrate_public_posting(url, Job(source='fixture', url=url,
                title=title, company='Example'), client=client, budget=budget)
    outcome = asyncio.run(run())
    assert outcome.state == 'partial' and outcome.job is None
    assert url not in budget.cache
