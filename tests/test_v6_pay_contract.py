from datetime import datetime, timezone
import pytest
from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.models import CanonicalJob, ListingObservation, SourceKind, Assessment
from jobhound.v41.extract import _candidate, extract_pay_candidates
from jobhound.v41.engine import _sync_pay_assessment, evaluate_raw
from jobhound.v41.compensation import parse_compensation
from jobhound.compact_audit import compact_audit, write_compact_audit
from jobhound.review_audit import load_audit


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)


@pytest.mark.parametrize('raw,basis', [
    ('INR 50,000 monthly salary', 'month'),
    ('USD 100k–120000/year', 'year'),
    ('USD 20/hr with 3 audio hours each week', 'labor_hour'),
    ('USD 20 per hour, paid monthly', 'labor_hour'),
    ('USD 6 per image', 'output_item'),
    ('USD 60 per recorded audio hour', 'output_audio_hour'),
    ('USD 20 per paid task-hour', 'task_hour'),
    ('USD 350 fixed project', 'fixed'),
])
def test_native_units_survive_candidate_and_assessment(raw, basis):
    candidate = _candidate(raw, 'structured', 'obs', .95, SourceKind.ORIGINAL_ATS)
    assert candidate.basis == basis
    assert candidate.min_hourly_usd is None if basis != 'labor_hour' else candidate.min_hourly_usd == 20
    assessment = Assessment(selected_pay=candidate, pay_candidates=[candidate], pay_credibility=.95)
    _sync_pay_assessment(assessment)
    assert assessment.pay_assessment.state.value != 'credible_hourly' if basis != 'labor_hour' else assessment.pay_assessment.state.value == 'credible_hourly'


@pytest.mark.parametrize('raw', ['EUR 1.234,56 per hour', 'USD 500/month or hourly', '$5 per image. Other assignments pay $60 per hour.'])
def test_ambiguous_claim_never_falls_back_to_hourly(raw):
    candidate = _candidate(raw, 'structured', 'obs', .95, SourceKind.ORIGINAL_ATS)
    assert candidate.parse_warnings
    assert candidate.min_hourly_usd is None and candidate.max_hourly_usd is None
    assert not candidate.labor_hourly_supported


def test_suffix_currency_not_replaced_by_old_dollar_default():
    candidate = _candidate('$20/hour CAD', 'structured', 'obs', .95, SourceKind.ORIGINAL_ATS)
    assert candidate.currency == 'CAD'
    assert candidate.amount_low == 20


def test_unverified_salary_not_credible_by_unit_alone():
    candidate = _candidate('USD 100000/year', 'description', 'obs', .7, SourceKind.UNKNOWN)
    assessment = Assessment(selected_pay=candidate, pay_credibility=.3)
    _sync_pay_assessment(assessment)
    assert assessment.pay_assessment.state.value == 'known_unverified'


def test_piece_denominators_preserved():
    assert parse_compensation('USD 5 per word').literal_unit == 'per_word'
    assert parse_compensation('USD 5 per Image').literal_unit == 'per_image'


def test_different_output_claims_do_not_corroborate():
    observations = []
    for i, pay in enumerate(['USD 5 per image', 'USD 10 per image']):
        job = Job(source='ashby', title='Bengali annotator', company='Example',
                  url=f'https://source{i}.example/jobs/1', pay_raw=pay)
        observations.append(ListingObservation(observation_id=str(i), source='ashby',
            source_kind=SourceKind.ORIGINAL_ATS, job=job))
    canonical = CanonicalJob(canonical_id='one', job=observations[0].job, observations=observations)
    candidates, _, conflict = extract_pay_candidates(canonical)
    assert conflict
    assert not any(c.corroborating_observation_ids for c in candidates)


def test_compact_export_preserves_all_decisions_and_bounds(tmp_path):
    raw = [{'source': 'ashby', 'raw': {'id': '1', '_company': 'Example',
        'title': 'Bengali AI Trainer', 'jobUrl': 'https://jobs.ashbyhq.com/example/1',
        'descriptionPlain': 'Bengali annotation. ' * 1000, 'isRemote': True, 'location': 'India'}}]
    result = evaluate_raw(raw, as_of=datetime(2026, 9, 19, tzinfo=timezone.utc))
    out = tmp_path/'compact.json'
    write_compact_audit(result, out, snapshot_reference='fixture.jsonl', snapshot_sha256='0'*64)
    audit = load_audit(out)
    assert len(audit['decisions']) == len(result.evaluated)
    assert audit['completeness']['all_decisions']
    assert 'description' not in audit['decisions'][0]['job']
    assert audit['decisions'][0]['evidence']['observation_ids']
    with pytest.raises(FileExistsError):
        write_compact_audit(result, out, snapshot_reference='fixture.jsonl', snapshot_sha256='0'*64)
