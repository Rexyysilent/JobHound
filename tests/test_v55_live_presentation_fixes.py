"""Keep live retrieval evidence distinct from legacy discovery counters."""
from jobhound.v41.digest import _source_health_alerts, build_digest
from jobhound.v41.engine import evaluate_raw
from jobhound.config import CONFIG
from jobhound.v41.models import PayCandidate
from jobhound.v41.digest import _pay_line
from types import SimpleNamespace


def test_stage_health_is_rendered_once_with_actual_candidate_counts(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    result = evaluate_raw([])
    result.source_health = [dict(source='first_party', transport_host='example.org',
        stage='hydration', status='partial', attempted=4, succeeded=0,
        failed=3, deferred=1, error_codes={'http_429': 3})]
    digest = build_digest(result, include_all=True)
    assert digest.text.count('hydration partial') == 1
    assert '4 candidates attempted, 0 succeeded, 3 failed, 1 deferred' in digest.text
    assert 'example.org' in digest.text
    assert 'http_429: 3' in digest.text
    assert '0 failed request' not in digest.text
    assert 'HTTP requests' not in digest.text  # absent is not a measured zero
    assert 'not evidence of job closure' in digest.text
    assert digest.accounting_ok


def test_reported_request_count_and_notification_filter_are_preserved():
    result = evaluate_raw([])
    result.source_health = [dict(source='retrieval', stage='hydration', status='partial',
        attempted=40, succeeded=0, failed=37, deferred=1, request_count=42,
        alert_notify=False)]
    assert '42 HTTP requests' in _source_health_alerts(result)[0]
    assert _source_health_alerts(result, notify_only=True) == []


def test_legacy_discovery_failure_message_is_unchanged():
    result = evaluate_raw([])
    result.source_health = [dict(source='adzuna', status='partial', item_count=30,
        failed_requests=2, retries=1, error_type='HTTP 429')]
    message = _source_health_alerts(result)[0]
    assert '30 items retained' in message
    assert '2 failed request(s) and 1 retry/retries' in message


def test_publisher_equivalent_is_not_rendered_as_actual_hourly_wage():
    pay = PayCandidate(raw='up to $10 per hour equivalent depending on the project',
        source_field='description', observation_id='example', confidence=.7,
        basis='labor_hour_equivalent', actual_unit='labor_hour_equivalent',
        amount_low=10, amount_high=10, estimated=True, labor_hourly_supported=False)
    item = SimpleNamespace(decision=SimpleNamespace(policy_version='v5.0.0-rc1'),
        assessment=SimpleNamespace(selected_pay=pay, action_readiness='unknown',
            time_to_cash_days=None, pay_conflict=False))
    text = _pay_line(item)
    assert 'publisher-stated hourly equivalent (estimate)' in text
    assert 'actual working-hour rate not established' in text
    assert 'per working hour' not in text
    assert 'depending on the project' in text
