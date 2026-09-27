from datetime import datetime, timezone
import pytest
from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.digest import build_digest


@pytest.mark.parametrize('quote,unit', [
    ('INR 50,000 monthly salary', 'month'),
    ('USD 100k-120000/year', 'year'),
    ('USD 20/hr with 3 audio hours each week', 'labor_hour'),
    ('USD 20 per hour, paid monthly', 'labor_hour'),
    ('USD 6 per image', 'output_item'),
    ('USD 60 per recorded audio hour', 'output_audio_hour'),
    ('USD 20 per paid task-hour', 'task_hour'),
    ('USD 350 fixed project', 'fixed_project'),
    ('EUR 1.234,56 per hour', 'unknown'),
    ('USD 500/month or hourly', 'unknown'),
])
def test_structured_provider_through_actual_decision_and_digest(monkeypatch, quote, unit):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    raw = [{'source': 'ashby', 'raw': {
        'id': 'one', '_company': 'Example Language Lab',
        'title': 'Bengali AI Trainer',
        'jobUrl': 'https://jobs.ashbyhq.com/example-language/one',
        'descriptionPlain': 'Evaluate Bengali AI responses. Remote work in India.',
        'location': 'India', 'isRemote': True,
        'compensation': {'scrapeableCompensationSalarySummary': quote},
    }}]
    result = evaluate_raw(raw, as_of=datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert len(result.evaluated) == 1 and result.accounting_ok
    assessment = result.evaluated[0].assessment
    pay = assessment.selected_pay
    assert pay is not None and pay.raw == quote
    assert pay.actual_unit == unit
    if unit != 'labor_hour':
        assert pay.min_hourly_usd is None and pay.max_hourly_usd is None
        assert assessment.pay_assessment.state.value != 'credible_hourly'
    if unit == 'unknown':
        assert pay.parse_warnings
    digest = build_digest(result, include_all=True)
    assert digest.accounting_ok
    assert result.evaluated[0].decision.notification_transition != 'pay_resolved'
