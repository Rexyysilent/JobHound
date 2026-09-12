import pytest

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.evidence_dimensions import (
    describe_evidence, project_observation, public_page_kind, work_arrangement,
)
from jobhound.v41.hydration import _generic_job, child_observation, HydrationResult
from jobhound.v41.models import (
    Assessment, CanonicalJob, EligibilityStatus, ListingObservation, RequirementsAssessment,
    MatchStrength, PayCandidate, SourceKind,
)
from jobhound.v41.provenance import canonicalize_observations
from jobhound.v41.extract import extract_pay_candidates


@pytest.fixture(autouse=True)
def release_policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)


def observation(**kwargs):
    job = Job(source='first_party', title='Bengali AI evaluator', company='Example',
              url='https://in.linkedin.com/jobs/view/123', description='Review Bengali answers.',
              location='Delhi, IN')
    return ListingObservation(observation_id='child', source='first_party', origin='hydration',
        job=job, raw_payload=kwargs.pop('raw_payload', {}), original_url=job.url,
        normalized_url=job.url, source_kind=SourceKind.ORIGINAL_EMPLOYER,
        content_state='complete', identity_state='corroborated', **kwargs)


@pytest.mark.parametrize('url,expected', [
    ('https://in.linkedin.com/jobs/view/123', 'reputable_board'),
    ('https://www.upwork.com/freelance-jobs/apply/123', 'reputable_board'),
    ('https://www.alignerr.com/jobs/123', 'original_employer'),
    ('https://careers.sigma.ai/jobs/123', 'original_employer'),
    ('https://unknown.example/jobs/123', 'unknown'),
    ('https://linkedin.com.attacker.example/jobs/123', 'unknown'),
])
def test_publisher_authority_depends_on_host_not_parser(url, expected):
    assert public_page_kind(url).value == expected


def test_saved_observation_projection_is_lossless_and_idempotent():
    original = observation(raw_payload={'jobLocationType': 'TELECOMMUTE',
                                       'unrecognized_future_field': {'pay_condition': 'after QA'}})
    before = original.model_dump_json()
    updated = project_observation(original)
    assert original.model_dump_json() == before
    assert updated.raw_payload == original.raw_payload
    assert updated.job.description == original.job.description
    assert updated.source_kind == SourceKind.REPUTABLE_BOARD
    assert updated.job.is_remote
    claim = updated.work_arrangement.claims[0]
    assert (claim.source_field, claim.span, claim.observation_id) == ('jobLocationType', 'TELECOMMUTE', 'child')
    assert updated.work_arrangement.state == 'remote'
    assert project_observation(updated) == updated
    assert ListingObservation.model_validate_json(updated.model_dump_json()) == updated


@pytest.mark.parametrize('payload,title,expected', [
    ({'jobLocationType': 'TELECOMMUTE'}, 'Reviewer', 'remote'),
    ({'jobLocationType': ['TELECOMMUTE']}, 'Reviewer', 'remote'),
    ({}, 'QA Reviewer - Remote', 'remote'),
    ({}, 'QA Reviewer - not remote', 'onsite'),
    ({}, 'QA Reviewer - Hybrid', 'hybrid'),
    ({'jobLocationType': 'TELECOMMUTE'}, 'QA Reviewer - onsite', 'conflicting'),
    ({}, 'Reviewer', 'unknown'),
    ({'isRemote': False}, 'Reviewer', 'unknown'),
    ({}, 'Remote Sensing Analyst', 'unknown'),
    ({}, 'Remote Desktop Technician', 'unknown'),
])
def test_work_arrangement_does_not_infer_onsite_from_absence(payload, title, expected):
    job = observation().job.model_copy(update={'title': title})
    assert work_arrangement(job, payload).state == expected


def test_generic_jsonld_remote_does_not_invent_global_eligibility():
    payload = {'title': 'Reviewer', 'hiringOrganization': {'name': 'Example'},
               'description': 'Evaluate model answers.', 'jobLocationType': 'TELECOMMUTE',
               'jobLocation': {'address': {'addressCountry': 'US'}}}
    job = _generic_job(payload, 'https://example.com/jobs/1', '')
    assert job.is_remote and job.location == 'US'
    assert 'worldwide' not in job.region_tags


def test_unknown_pay_does_not_erase_relevance_or_invent_failed_eligibility():
    row = project_observation(observation())
    canonical = CanonicalJob(canonical_id='1', job=row.job, observations=[row])
    assessment = Assessment(match_strength=MatchStrength.STRONG, matched_concepts=['language_ai'],
        blockers=['pay_below_floor'], eligibility=EligibilityStatus.FAILED,
        requirements_assessment=RequirementsAssessment(completeness='complete'))
    # The old overall eligibility field conflates other rejection reasons.
    # Independent applicant evidence must not turn low/unknown pay into failure.
    dimensions = describe_evidence(canonical, assessment)
    assert dimensions['relevance'].state == 'strong'
    assert dimensions['economics'].state == 'not_stated'
    assert dimensions['eligibility'].state == 'passed'
    assert assessment.eligibility == EligibilityStatus.FAILED  # projection never mutates policy
    assert dimensions['publisher'].value['source_kind'] == 'reputable_board'


def test_rate_is_claimed_not_verified_income_and_other_claims_survive():
    row = project_observation(observation())
    canonical = CanonicalJob(canonical_id='1', job=row.job, observations=[row])
    pay = PayCandidate(raw='$9/hour', basis='hourly', source_field='description',
                       observation_id='child', confidence=.8, min_hourly_usd=9)
    assessment = Assessment(selected_pay=pay, pay_candidates=[pay], match_strength=MatchStrength.STRONG)
    dimension = describe_evidence(canonical, assessment)['economics']
    assert dimension.state == 'claimed' and dimension.claims[0].span == '$9/hour'
    assert dimension.value['min_hourly_usd'] == 9


def test_board_child_lineage_preserves_one_job_after_authority_correction():
    parent = observation().model_copy(update={'observation_id': 'parent', 'origin': 'discovery',
        'source': 'serp', 'source_kind': SourceKind.AGGREGATOR_UNRESOLVED})
    child = project_observation(observation(parent_observation_ids=['parent']))
    # Different location representations must not separate verified parentage.
    child.job.location = 'New Delhi, India'
    assert len(canonicalize_observations([parent, child])) == 1


def test_identical_pay_on_parent_and_child_is_not_independent_confirmation():
    parent = observation().model_copy(update={'observation_id': 'parent', 'origin': 'discovery',
        'source': 'serp', 'source_kind': SourceKind.AGGREGATOR_UNRESOLVED})
    parent.job.pay_raw = '$9/hour'
    child = project_observation(observation(parent_observation_ids=['parent']))
    child.job.pay_raw = '$9/hour'
    canonical = CanonicalJob(canonical_id='1', job=child.job, observations=[parent, child])
    candidates, selected, _ = extract_pay_candidates(canonical)
    assert {c.observation_id for c in candidates} == {'parent', 'child'}
    assert all(not c.corroborating_observation_ids for c in candidates)
    assert selected is not None


def test_no_source_evidence_lost_when_unknown_domain_is_parsed():
    original = observation()
    outcome = HydrationResult(state='complete', job=original.job, source='first_party',
        public_url='https://unknown.example/jobs/1', identity_state='corroborated',
        payload={'jobLocationType': 'TELECOMMUTE', 'title': original.job.title})
    child = child_observation(original, outcome)
    assert child.source_kind == SourceKind.UNKNOWN
    assert child.raw_payload == outcome.payload and child.content_state == 'complete'
    assert child.job.title == original.job.title and child.job.is_remote


def test_evidence_dimensions_do_not_become_a_second_ranking_score():
    from jobhound.v41.engine import evaluate_observations, _decision
    row = project_observation(observation(raw_payload={'jobLocationType': 'TELECOMMUTE'}))
    item = evaluate_observations([row]).evaluated[0]
    before = _decision(item.canonical, item.assessment).model_dump()
    item.assessment.evidence_dimensions.clear()
    assert _decision(item.canonical, item.assessment).model_dump() == before


def test_old_hydration_snapshot_replays_with_new_projection_without_network(tmp_path, monkeypatch):
    import httpx
    from jobhound.v41.engine import evaluate_observations
    from jobhound.v41.replay import replay, write_snapshot
    row = observation(raw_payload={'jobLocationType': 'TELECOMMUTE'})
    # Encode the old, erroneous hydration fields, exactly as pre-patch snapshots did.
    result = evaluate_observations([row], raw_count=0)
    result.observations = [row]
    path = write_snapshot([], result, directory=tmp_path)
    original_bytes = path.read_bytes()
    monkeypatch.setattr(httpx.AsyncClient, 'get', lambda *a, **k: pytest.fail('offline replay used network'))
    again = replay(path)
    child = again.observations[0]
    assert child.source_kind == SourceKind.REPUTABLE_BOARD
    assert child.work_arrangement.state == 'remote'
    assert child.job.is_remote
    assert path.read_bytes() == original_bytes


def test_missing_requirements_remain_unknown_even_with_complete_page():
    row = project_observation(observation())
    canonical = CanonicalJob(canonical_id='1', job=row.job, observations=[row])
    assessment = Assessment(match_strength=MatchStrength.STRONG)
    result = describe_evidence(canonical, assessment)
    assert result['content'].state == 'complete'
    assert result['eligibility'].state == 'unknown'
    assert result['relevance'].state == 'strong'
