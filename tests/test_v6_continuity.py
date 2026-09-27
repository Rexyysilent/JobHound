"""Raw prose and temporary-store regression gates, with no provider/delivery I/O."""
import hashlib
from datetime import timedelta
import pytest
from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.digest import build_digest
from jobhound.v41.store import V41Store
from test_v55_action_policy import DESCRIPTION, NOW


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)


def raw(quote='', *, source='ashby', identity='one', title='Bengali AI Response Evaluator', url=None):
    description = DESCRIPTION + '\nCompensation\n' + quote
    if source == 'jsearch':
        return {'source': source, 'raw': {'job_id': identity, 'job_title': title,
            'employer_name': 'Example', 'job_apply_link': url or 'https://jobs.example.org/' + identity,
            'job_description': description, 'job_country': 'IN', 'job_is_remote': True}}
    return {'source': source, 'raw': {'id': identity, 'title': title, '_company': 'Example',
        'jobUrl': url or 'https://jobs.ashbyhq.com/example/' + identity,
        'location': 'India', 'isRemote': True, 'descriptionPlain': description}}


def run(quote='', minute=0, **kwargs):
    return evaluate_raw([raw(quote, **kwargs)], as_of=NOW+timedelta(minutes=minute))


@pytest.mark.parametrize('quote,unit,qualifier,currency', [
    ('Pay is up to $50 per hour equivalent depending on task speed.', 'labor_hour_equivalent', 'up_to_equivalent', 'USD'),
    ('Pay is $20 per hour CAD.', 'labor_hour', 'unknown', 'CAD'),
    ('Pay is USD 6 per image.', 'output_item', 'unknown', 'USD'),
    ('Pay is USD 0.05 per word.', 'output_item', 'unknown', 'USD'),
    ('Pay is USD 20 per paid task-hour.', 'task_hour', 'unknown', 'USD'),
    ('Pay is INR 50,000 monthly salary.', 'month', 'unknown', 'INR'),
    ('Pay is USD 100k–120000/year.', 'year', 'unknown', 'USD'),
    ('Pay is USD 20 per hour, paid monthly.', 'labor_hour', 'unknown', 'USD'),
    ('Pay is USD 20/hr with 3 audio hours of work per week.', 'labor_hour', 'unknown', 'USD'),
    ('Pay is USD 60 per recorded audio hour.', 'output_audio_hour', 'unknown', 'USD'),
    ('Pay Rate: US$3 per hour', 'labor_hour', 'unknown', 'USD'),
    ('Pay Rate: us$3 per hour', 'labor_hour', 'unknown', 'USD'),
    ('Pay Rate: C$20 per hour', 'labor_hour', 'unknown', 'CAD'),
    ('Pay Rate: AU$20 per hour', 'labor_hour', 'unknown', 'AUD'),
])
def test_full_prose_quote_preserved(quote, unit, qualifier, currency):
    result = run(quote)
    item = result.evaluated[0]
    pay = item.assessment.selected_pay
    assert pay and pay.actual_unit == unit
    assert pay.qualifier == qualifier and pay.currency == currency
    assert pay.raw == quote
    original = item.canonical.observations[0].job.description
    assert original[pay.source_span_start:pay.source_span_end] == pay.raw
    assert pay.source_text_sha256 == hashlib.sha256(original.encode()).hexdigest()
    assert pay.actor == 'posting_author_unverified'
    if unit != 'labor_hour':
        assert pay.min_hourly_usd is None and pay.max_hourly_usd is None
    assert build_digest(result, include_all=True).accounting_ok


@pytest.mark.parametrize('quote', [
    'Pay EUR 1.234,56 per hour.',
    'Pay USD 500/month or hourly.',
    'We pay USD 5 per image or USD 60 per hour depending on the assignment.',
    'Pay USD 10–EUR 20 per hour.',
])
def test_ambiguous_prose_does_not_regain_a_supported_rate(quote):
    item = run(quote).evaluated[0]
    assert item.assessment.pay_candidates
    assert all(c.actual_unit == 'unknown' and c.parse_warnings for c in item.assessment.pay_candidates)
    assert item.assessment.pay_rate_for_ranking is None


def test_guaranteed_wage_does_not_guarantee_hours():
    item = run('Guaranteed minimum pay USD 12 per hour.').evaluated[0]
    assert item.assessment.selected_pay.guaranteed
    assert item.job.guaranteed_hours is False


def test_explicit_us_dollar_low_rate_remains_below_floor():
    item = run('Pay Rate: US$3 per hour').evaluated[0]
    assert item.decision.terminal_reason == 'reject_pay'


def test_separate_piece_and_hour_claims_remain_visible():
    item = run('Pay USD 5 per image. Other assignments pay USD 60 per hour.').evaluated[0]
    candidates = item.assessment.pay_candidates
    assert {c.actual_unit for c in candidates} == {'output_item', 'labor_hour'}
    assert any(c.scope == 'other_assignment' for c in candidates)
    assert item.assessment.selected_pay.actual_unit == 'output_item'


def test_index_pay_never_enters_ranking_or_stored_notifications(tmp_path):
    store = V41Store(tmp_path/'index.db')
    try:
        for minute, quote in enumerate(['', 'Jobs pay USD 70–100 per hour.']):
            result = run(quote, minute, title='Browse remote AI jobs', url='https://www.opentrain.ai/jobs/')
            item = result.evaluated[0]
            assert item.assessment.document_type == 'job_index'
            assert item.assessment.selected_pay is None
            assert item.assessment.pay_rate_for_ranking is None
            assert store.annotate_transitions(result) == []
            store.record_run(result)
            if quote:
                assert item.assessment.pay_candidates
    finally:
        store.close()


def test_piece_denominator_change_notifies_once_after_delivery(tmp_path):
    store = V41Store(tmp_path/'piece.db')
    try:
        first = run('Pay USD 5 per image.')
        assert store.annotate_transitions(first)
        store.record_run(first)
        store.mark_notified(first.metadata.run_id, first.evaluated, 'synthetic', True)
        changed = run('Pay USD 5 per word.', 1)
        assert store.annotate_transitions(changed)[0].decision.notification_transition == 'material_change'
        store.record_run(changed)
        store.mark_notified(changed.metadata.run_id, changed.evaluated, 'synthetic', True)
        assert store.annotate_transitions(run('Pay USD 5 per word.', 2)) == []
    finally:
        store.close()


def test_retained_changes_reach_conflicts_without_new_job_identity(tmp_path):
    rows = [raw('Pay USD 10 per hour.', source='jsearch'), raw('Pay USD 30 per hour.', source='jsearch')]
    result = evaluate_raw(rows, as_of=NOW)
    assert len(result.observations) == 2 and len(result.evaluated) == 1
    item = result.evaluated[0]
    assert item.assessment.pay_conflict
    assert {c.amount_low for c in item.assessment.pay_candidates} == {10, 30}
    assert not any(c.corroborating_observation_ids for c in item.assessment.pay_candidates)
    store = V41Store(tmp_path/'rerun.db')
    try:
        assert store.annotate_transitions(result)
        store.record_run(result)
        store.mark_notified(result.metadata.run_id, result.evaluated, 'synthetic', True)
        again = evaluate_raw(list(reversed(rows)), as_of=NOW+timedelta(minutes=1))
        assert again.evaluated[0].canonical.canonical_id == item.canonical.canonical_id
        assert store.annotate_transitions(again) == []
    finally:
        store.close()


def test_conflict_arrival_and_resolution_are_material(tmp_path):
    store = V41Store(tmp_path/'conflict.db')
    try:
        quotes = [
            ['Pay USD 10 per hour.'],
            ['Pay USD 10 per hour.', 'Pay USD 30 per hour.'],
            ['Pay USD 10 per hour.'],
            ['Pay USD 10 per hour.'],
        ]
        for minute, batch in enumerate(quotes):
            result = evaluate_raw([raw(q, source='jsearch') for q in batch], as_of=NOW+timedelta(minutes=minute))
            items = store.annotate_transitions(result)
            assert bool(items) == (minute < 3)
            if minute in (1, 2):
                assert items[0].decision.notification_transition == 'material_change'
            store.record_run(result)
            store.mark_notified(result.metadata.run_id, items, 'synthetic', True)
    finally:
        store.close()


def test_guarantee_change_is_material_but_rephrasing_is_not(tmp_path):
    store = V41Store(tmp_path/'guarantee.db')
    try:
        for minute, quote in enumerate([
            'Pay USD 12 per hour.',
            'Guaranteed minimum pay USD 12 per hour.',
            'Guaranteed minimum pay: USD 12 per hour.',
        ]):
            result = run(quote, minute)
            items = store.annotate_transitions(result)
            assert bool(items) == (minute < 2)
            store.record_run(result)
            store.mark_notified(result.metadata.run_id, items, 'synthetic', True)
    finally:
        store.close()


def test_unknown_fx_never_defaults_fixed_budget_to_usd(monkeypatch):
    monkeypatch.setattr(CONFIG.pay, 'fx_to_usd', {'USD': 1.0})
    item = run('Freelance Job in AI Apps & Integration - CAD 350.',
               url='https://www.upwork.com/freelance-jobs/apply/example_~01/').evaluated[0]
    pay = item.assessment.selected_pay
    assert pay and pay.currency == 'CAD' and pay.amount_low == 350
    assert pay.fixed_amount_usd is None
    assert 'missing_currency_conversion' in pay.parse_warnings
