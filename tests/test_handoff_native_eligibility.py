"""Synthetic native ATS witnesses for original R23/R34/R35 claim classes."""
import asyncio
from dataclasses import replace

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.v41.digest import build_digest
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.hydration import RetrievalBudget, RetrievalPolicy, hydrate_public_posting, child_observation
from jobhound.v41.models import SourceKind
from jobhound.v41 import engine
from jobhound.filters.eligibility import default_profile
from test_v55_action_policy import observation, NOW


BASE = ('Work\nReview Bengali and English AI responses using supplied guidelines.\n'
        'Requirements\nBengali and English fluency required.\n'
        'No prior AI-training experience or university degree required.\n')


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def native(title='Bengali AI Response Evaluator', description=BASE, *, location='India', remote=True):
    parent = observation()
    parent.job.company = 'Synthetic'
    parent.job.title = title
    parent.job.description = 'Individual contractor opening; see the native role page for complete requirements.'
    parent.job.pay_raw = None
    parent.content_state = 'snippet_only'
    parent.job.url = parent.original_url = parent.normalized_url = 'https://jobs.ashbyhq.com/synthetic/one'
    payload = dict(id='one', title=title, jobUrl=parent.job.url,
        descriptionPlain=description, location=location, isRemote=remote,
        isListed=True, publishedAt='2026-10-17T09:00:00Z',
        compensation={'scrapeableCompensationSalarySummary': 'USD 20 per working hour'})
    requests = []
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={'jobs': [payload]})
    async def hydrate():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await hydrate_public_posting(parent.job.url, parent.job, client=client,
                budget=RetrievalBudget(RetrievalPolicy()))
    hydrated = asyncio.run(hydrate())
    assert requests == ['https://api.ashbyhq.com/posting-api/job-board/synthetic']
    assert hydrated.state == 'complete' and hydrated.identity_state == 'exact'
    child = child_observation(parent, hydrated)
    assert child.source_kind == SourceKind.ORIGINAL_ATS
    assert child.raw_payload['descriptionPlain'] == description
    assert child.parent_observation_ids == ['one']
    result = evaluate_observations([parent, child], as_of=max(NOW, child.captured_at))
    assert result.accounting_ok and len(result.evaluated) == 1
    item = result.evaluated[0]
    assert item.assessment.selected_pay.observation_id == child.observation_id
    assert item.job.effective_hourly_usd is None
    return result, item, child


def test_r23_native_bangladesh_and_advanced_linguistics_cannot_be_rescued_by_bengali_match():
    body = BASE.replace('No prior AI-training experience or university degree required.',
        'A PhD in linguistics is required.\nApplicants must reside in Bangladesh.')
    result, item, child = native('Bengali Linguistics AI Evaluator - Bangladesh', body, location='Bangladesh')
    assert item.decision.action_band.value == 'reject'
    assert any(b.startswith('location:') for b in item.assessment.blockers)
    assert 'language_ai' in item.assessment.matched_concepts
    assert any(c.dimension == 'degree' and c.modality == 'required'
               and c.observation_id == child.observation_id
               for c in item.assessment.requirements_assessment.claims)
    assert not build_digest(result, include_all=True).displayed


def test_r23_native_india_generalist_control_has_no_region_or_credential_block():
    _, item, _ = native(description=BASE + 'Remote applicants in India may apply.')
    assert not item.assessment.blockers
    assert item.assessment.eligibility.value == 'passed'
    assert item.decision.action_band.value == 'primary'


def test_r23_native_india_advanced_credential_is_unverified_not_invented_absent():
    body = BASE.replace('No prior AI-training experience or university degree required.',
        'A PhD in linguistics is required.\nRemote applicants in India may apply.')
    _, item, _ = native(description=body)
    assert not any(b.startswith('location:') for b in item.assessment.blockers)
    assert any('credential_unverified' in u for u in item.assessment.unresolved)
    assert item.decision.next_action != 'apply'


@pytest.mark.parametrize('wording', [
    'This role is not onsite. Remote applicants in India may apply.',
    'Onsite attendance is not required. Remote applicants in India may apply.',
    'Optional onsite visits are available. This role is remote in India.',
    'Our company offices are onsite in Chennai. Remote applicants in India may apply.',
    'Our clients work onsite in Chennai. Remote applicants in India may apply.',
])
def test_r34_native_negated_optional_or_unrelated_onsite_does_not_block_remote_role(wording):
    _, item, _ = native(description=BASE + wording)
    assert not any('onsite_or_hybrid' in b for b in item.assessment.blockers)
    assert item.decision.action_band.value == 'primary'


def test_r34_native_explicit_mandatory_attendance_beyond_old_location_prefix_limit():
    result, item, _ = native(description=BASE + ('Read supplied examples carefully. ' * 60)
        + '\nOnsite attendance is required in Chennai, India.', remote=True)
    assert item.decision.action_band.value == 'reject'
    assert any('onsite_or_hybrid' in b for b in item.assessment.blockers)
    assert not build_digest(result, include_all=True).displayed


@pytest.mark.parametrize('remote', [False, True])
def test_r34_native_explicit_onsite_chennai_beats_india_and_remote_metadata(remote):
    result, item, child = native(description=BASE + 'This role is onsite in Chennai, India. Office attendance is required.',
        location='Chennai, India', remote=remote)
    assert item.decision.action_band.value == 'reject'
    assert any('onsite_or_hybrid' in b for b in item.assessment.blockers)
    assert 'onsite in Chennai' in child.job.description
    assert not build_digest(result, include_all=True).displayed


def test_r34_native_remote_india_control_is_actionable():
    _, item, _ = native(description=BASE + 'Remote applicants in India may apply. No office attendance is required.')
    assert item.decision.action_band.value == 'primary'
    assert not any(b.startswith('location:') for b in item.assessment.blockers)


@pytest.mark.parametrize('wording', [
    'This role is onsite or remote in India; you may choose either.',
    'This role is onsite optionally; fully remote applicants accepted.',
    'Previous role description: "This role is onsite in Chennai." This current role is remote in India.',
])
def test_r34_native_role_local_alternative_optional_and_quoted_old_copy_are_not_mandatory(wording):
    _, item, _ = native(description=BASE + wording)
    assert not any('onsite_or_hybrid' in b for b in item.assessment.blockers)
    assert item.decision.action_band.value == 'primary'


def test_r34_native_mandatory_role_onsite_is_not_cancelled_by_later_remote_company_copy():
    _, item, _ = native(description=BASE + 'This role is onsite in Chennai. Our company has a remote team in India.', remote=True)
    assert item.decision.action_band.value == 'reject'
    assert any('onsite_or_hybrid' in b for b in item.assessment.blockers)


@pytest.mark.parametrize('title,requirements', [
    ('Data Science Expert - AI Evaluator', 'Data science expertise and 5 years of statistics experience required. Build machine-learning models and evaluate AI outputs.'),
    ('Competitive Programming Expert - AI Evaluator', 'Competitive programming expertise and 5 years of algorithms experience required. Solve advanced algorithmic problems and evaluate AI outputs.'),
])
def test_r35_native_specialist_requirements_survive_generic_evaluator_vocabulary(title, requirements):
    body = BASE.replace('No prior AI-training experience or university degree required.', requirements)
    result, item, child = native(title, body + 'Remote applicants in India may apply.')
    assert item.decision.action_band.value == 'reject'
    assert any('experience_mismatch' in b for b in item.assessment.blockers)
    assert 'ai_evaluation' in item.assessment.matched_concepts or 'annotation' in item.assessment.matched_concepts
    assert item.assessment.role_assessment.specialist_domain_status != 'none'
    assert item.assessment.role_assessment.evidence[0].observation_id == child.observation_id
    assert not build_digest(result, include_all=True).displayed


def test_r35_native_generalist_evaluation_control_has_no_specialist_block():
    _, item, _ = native(description=BASE + 'Evaluate general knowledge examples using provided guidelines. Remote applicants in India may apply.')
    assert item.assessment.role_assessment.specialist_domain_status == 'none'
    assert item.decision.action_band.value == 'primary'


@pytest.mark.parametrize('title,domain,required', [
    ('Data Science Expert - AI Evaluator', 'data_science', 'Advanced data science expertise is required.'),
    ('Competitive Programming Expert - AI Evaluator', 'competitive_programming', 'Competitive programming expertise is required.'),
])
def test_r35_native_exact_specialism_is_not_authorized_by_umbrella_data_or_programming(title, domain, required):
    body = BASE.replace('No prior AI-training experience or university degree required.', required)
    _, item, _ = native(title, body + 'Remote applicants in India may apply.')
    assert item.assessment.role_assessment.specialist_domain == domain
    assert item.assessment.role_assessment.specialist_domain_status == 'unclaimed'
    assert item.decision.action_band.value != 'primary'
    assert item.decision.next_action != 'apply'


@pytest.mark.parametrize('title,domain', [
    ('Data Science Expert - AI Evaluator', 'data_science'),
    ('Competitive Programming Expert - AI Evaluator', 'competitive_programming'),
])
def test_r35_native_exact_explicit_specialism_profile_control_is_not_blanket_rejected(monkeypatch, title, domain):
    persona = default_profile()
    monkeypatch.setattr(engine, 'default_profile', lambda: replace(persona,
        credential_domains=persona.credential_domains | {domain}))
    _, item, _ = native(title, BASE + 'Remote applicants in India may apply.')
    assert item.assessment.role_assessment.specialist_domain_status == 'claimed'
    assert item.decision.action_band.value == 'primary'


@pytest.mark.parametrize('title', ['Data Annotation AI Evaluator', 'Beginner Coding AI Evaluator'])
def test_r35_native_ordinary_annotation_and_beginner_coding_remain_available(title):
    _, item, _ = native(title, BASE + 'Remote applicants in India may apply.')
    assert item.decision.action_band.value != 'reject'
    assert item.assessment.role_assessment.specialist_domain not in {'data_science', 'competitive_programming'}
    assert 'role_mismatch' not in item.assessment.blockers
