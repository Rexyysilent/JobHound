"""End-to-end pay semantics and document routing on synthetic evidence."""
from datetime import datetime, timedelta, timezone

import pytest

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.extract import _candidate, extract_pay_candidates
from jobhound.v41.models import (ActionBand, CanonicalJob, ListingObservation,
                                SourceKind)
from jobhound.v41.store import V41Store

NOW = datetime(2026, 9, 17, 9, tzinfo=timezone.utc)
DESCRIPTION = '''Work
Review Bengali and English AI responses using supplied guidelines.
Requirements
Bengali and English fluency required.
No prior AI-training experience or university degree required.
Remote applicants in India may apply.
This contractor role reviews supplied examples using the provided rubric.
'''


def observation(key='one', pay='USD 10 per working hour', *, kind=SourceKind.ORIGINAL_ATS):
    job = Job(source='ashby', title='Bengali AI Response Evaluator',
              company='Example', url=f'https://jobs.ashbyhq.com/example/{key}',
              description=DESCRIPTION, location='India', is_remote=True,
              region_tags=['india'], posted_at=NOW-timedelta(days=1), pay_raw=pay)
    return ListingObservation(
        observation_id=key, source='ashby', job=job,
        original_url=job.url, normalized_url=job.url, source_kind=kind,
        source_confidence=.92, content_state='complete', identity_state='exact',
        vacancy_state='verified_open', application_route_state='observed_public_route',
        captured_at=NOW, verified_open_at=NOW)


@pytest.mark.parametrize('raw,basis', [
    ('USD 50 per recorded audio hour', 'output_audio_hour'),
    ('USD 50 per paid task-hour', 'task_hour'),
    ('INR 50,000 monthly salary', 'month'),
    ('USD 100 per week', 'week'),
    ('USD 10 per item', 'output_item'),
    ('USD 500 fixed project', 'fixed_project'),
])
@pytest.mark.parametrize('review', [False, True])
def test_native_units_survive_assessment_without_hourly_projection(monkeypatch, raw, basis, review):
    monkeypatch.setattr(CONFIG.v55, 'enabled', review)
    item = evaluate_observations([observation(pay=raw)], as_of=NOW).evaluated[0]
    claim = item.assessment.selected_pay
    assert claim is not None
    assert claim.actual_unit == basis
    assert claim.min_hourly_usd is None and claim.max_hourly_usd is None
    assert item.assessment.pay_assessment.state.value != 'credible_hourly'
    assert item.assessment.pay_rate_for_ranking is None
    assert item.job.effective_hourly_usd is None


@pytest.mark.parametrize('raw', [
    '$5 per image. Other assignments pay $60 per hour.',
    'USD 10 - EUR 20 per hour',
    'USD 30-10 per hour',
])
def test_abstention_cannot_fall_back_to_legacy_parser(monkeypatch, raw):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    assert _candidate(raw, 'structured', 'test', .95, SourceKind.ORIGINAL_ATS) is None


def test_suffix_currency_is_retained(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    candidate = _candidate('30-50 USD/hr', 'structured', 'test', .95, SourceKind.ORIGINAL_ATS)
    assert candidate is not None and candidate.amount_low == 30
    assert candidate.max_hourly_usd == 50 and candidate.labor_hourly_supported


@pytest.mark.parametrize('second', ['USD 90 per recorded audio hour',
                                  'EUR 50 per recorded audio hour'])
def test_native_conflicts_are_not_false_corroboration(monkeypatch, second):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    a = observation(pay='USD 50 per recorded audio hour')
    b = observation('two', pay=second, kind=SourceKind.ORIGINAL_EMPLOYER)
    b.original_url = b.job.url = 'https://careers.example.org/roles/two'
    canonical = CanonicalJob(canonical_id='same-role', job=a.job, observations=[a, b])
    candidates, selected, conflict = extract_pay_candidates(canonical)
    assert conflict and selected is not None
    assert not any(c.corroborating_observation_ids for c in candidates)


def test_low_credibility_salary_is_not_labeled_credible(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    row = observation(pay='USD 80,000 per year', kind=SourceKind.UNKNOWN)
    item = evaluate_observations([row], as_of=NOW).evaluated[0]
    assert item.assessment.pay_assessment.state.value == 'known_unverified'


@pytest.mark.parametrize('review', [False, True])
def test_index_never_becomes_an_individual_opportunity(monkeypatch, review):
    monkeypatch.setattr(CONFIG.v55, 'enabled', review)
    row = observation()
    row.job.title = 'Remote AI Jobs: Training and Data Labeling'
    row.job.description = 'Browse all remote jobs and open roles.'
    row.job.url = row.original_url = row.normalized_url = 'https://careers.example.org/jobs/'
    item = evaluate_observations([row], as_of=NOW).evaluated[0]
    assert item.decision.action_band == ActionBand.REJECT


def test_unverified_new_pay_does_not_trigger_pay_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', False)
    store = V41Store(tmp_path/'delivery.db')
    first = evaluate_observations([observation(pay='')], as_of=NOW)
    first.evaluated[0].decision.action_band = ActionBand.VERIFY
    store.record_run(first)
    store.mark_notified(first.metadata.run_id, first.evaluated, 'test', True)
    second = evaluate_observations([observation(pay='USD 90 per hour', kind=SourceKind.UNKNOWN)],
                                   as_of=NOW+timedelta(minutes=1))
    second.evaluated[0].decision.action_band = ActionBand.VERIFY
    assert store.annotate_transitions(second) == []
    store.close()
