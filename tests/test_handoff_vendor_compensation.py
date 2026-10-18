"""Original-class compensation through native HTML adapter and final engine."""
import asyncio
import json

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.v41.digest import build_digest
from jobhound.v41.engine import evaluate_observations
from jobhound.v41 import hydration
from jobhound.v41 import evidence_dimensions
from jobhound.v41.models import SourceKind
from jobhound.v41.hydration import RetrievalBudget, RetrievalPolicy, hydrate_public_posting, child_observation
from test_v55_action_policy import observation, NOW, DESCRIPTION


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])
    # The synthetic example.org publisher represents the known original employer.
    monkeypatch.setattr(hydration, 'public_page_kind', lambda url: SourceKind.ORIGINAL_EMPLOYER)
    monkeypatch.setattr(evidence_dimensions, 'public_page_kind', lambda url: SourceKind.ORIGINAL_EMPLOYER)


def native(pay, conditions='', *, closed=False, telugu=False, parent_terms=''):
    parent = observation()
    if telugu:
        parent.job.title = 'Telugu AI Response Evaluator'
    parent.job.url = 'https://example.org/jobs/bengali-review'
    parent.job.pay_raw = None
    parent.job.description += '\n' + parent_terms
    description = DESCRIPTION + '\n' + pay + '.\n' + conditions
    if telugu:
        description += '\nTelugu fluency is mandatory.'
    payload = {'@type': 'JobPosting', 'title': parent.job.title, 'description': description,
        'hiringOrganization': {'name': 'Example'}, 'jobLocationType': 'TELECOMMUTE',
        'applicantLocationRequirements': {'@type': 'Country', 'name': 'India'}}
    body = f'<h1>{parent.job.title}</h1><script type="application/ld+json">{json.dumps(payload)}</script>'
    body += '<main>' + description + '</main><a href="/apply">Apply</a>'
    if closed:
        body += '<footer><p>This project has been completed.</p></footer>'
    async def hydrate():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))) as client:
            return await hydrate_public_posting(parent.job.url, parent.job, client=client,
                budget=RetrievalBudget(RetrievalPolicy()))
    hydrated = asyncio.run(hydrate())
    assert hydrated.state == 'complete' and hydrated.identity_state in {'exact', 'corroborated'}
    child = child_observation(parent, hydrated)
    result = evaluate_observations([parent, child], as_of=max(NOW, child.captured_at))
    assert result.accounting_ok and len(result.evaluated) == 1
    item = result.evaluated[0]
    assert item.assessment.selected_pay.observation_id == child.observation_id
    return result, item, child


R20_CONDITIONS = ('Vendor estimates 1.5 hours of work.\nCompensation is subject to approval.\n'
    'Approval may take 7 days.\nPayment is made 30 days after approval.')


def test_r20_closed_project_keeps_fixed_amount_estimate_conditions_and_terminal_closure():
    result, item, child = native('USD 18 fixed project', R20_CONDITIONS, closed=True)
    pay = item.assessment.selected_pay
    assert pay.actual_unit == 'fixed_project' and pay.amount_low == 18
    assert pay.min_hourly_usd is None and not pay.labor_hourly_supported
    assert pay.labor_time_assumption == 'Vendor estimates 1.5 hours of work.'
    claims = [e for e in item.assessment.evidence if e.dimension == 'compensation_conditions']
    assert {e.code for e in claims} == {'vendor_work_estimate', 'approval_condition', 'approval_processing', 'payment_schedule'}
    for claim in claims:
        assert claim.observation_id == child.observation_id
        assert child.job.description[claim.value['source_span_start']:claim.value['source_span_end']] == claim.span
    assert item.decision.lifecycle == 'closed' and item.decision.next_action == 'skip'
    assert item.assessment.time_to_cash_days is None
    assert item.decision.priority_key.supported_time_to_cash == 0
    digest = build_digest(result, include_all=True)
    assert not digest.displayed  # retained recruiting/pay copy cannot reopen it


def test_r20_open_control_digest_labels_vendor_duration_and_separate_payment_trigger():
    result, item, _ = native('USD 18 fixed project', R20_CONDITIONS)
    text = build_digest(result, include_all=True).text
    assert 'vendor work estimate (not a guaranteed pace)' in text
    assert 'Approval may take 7 days.' in text
    assert 'Payment is made 30 days after approval.' in text
    assert '$12/hour' not in text and '$12/hr' not in text
    assert item.assessment.time_to_cash_days is None


def test_r30_output_hour_and_vendor_throughput_are_separate_in_engine_and_digest():
    result, item, _ = native('USD 24 per 60 audio minutes',
        'Vendor estimates throughput of 5 audio minutes per working hour.')
    pay = item.assessment.selected_pay
    assert pay.actual_unit == 'output_audio_hour' and pay.amount_low == 24
    assert not pay.labor_hourly_supported and pay.min_hourly_usd is None
    assert item.assessment.pay_rate_for_ranking is None
    text = build_digest(result, include_all=True).text
    assert '$24/audio hour' in text and 'vendor work estimate (not a guaranteed pace)' in text
    assert '5 audio minutes per working hour' in text
    assert '$24/hour' not in text and '$24/hr' not in text
    assert item.assessment.time_to_cash_days is None


def test_r30_telugu_eligibility_remains_independent_of_correct_native_pay_unit():
    _, item, _ = native('USD 24 per 60 audio minutes', telugu=True)
    assert item.assessment.selected_pay.actual_unit == 'output_audio_hour'
    assert item.decision.action_band.value == 'reject'
    assert any('telugu' in b for b in item.assessment.blockers)


@pytest.mark.parametrize('conditions', ['',
    'Vendor estimates another project takes 1.5 hours.\nPayment for a different role follows approval.',
    'Vendor estimates corporate onboarding takes 1.5 hours.'])
def test_missing_unrelated_or_corporate_estimates_do_not_become_role_conditions(conditions):
    _, item, _ = native('USD 24 per 60 audio minutes', conditions,
        parent_terms='Vendor estimates 9 hours of work.\nPayment is made 30 days after submission.')
    assert item.assessment.selected_pay.labor_time_assumption is None
    assert not any(e.dimension == 'compensation_conditions' for e in item.assessment.evidence)


def test_conflicting_payment_delays_stay_literal_without_time_to_cash_forecast():
    result, item, _ = native('USD 18 fixed project',
        'Payment is made 30 days after approval.\nPayment is made 60 days after submission.\nPayment is monthly.')
    claims = [e for e in item.assessment.evidence if e.code == 'payment_schedule']
    assert len(claims) == 2
    assert item.assessment.time_to_cash_days is None
    text = build_digest(result, include_all=True).text
    assert '30 days after approval' in text and '60 days after submission' in text
    assert 'first-payment timing unknown' in text


def test_non_hour_audio_counts_are_not_silently_renormalized():
    _, item, _ = native('USD 24 per 30 audio minutes')
    assert item.assessment.selected_pay.actual_unit == 'unknown'
    assert item.assessment.selected_pay.min_hourly_usd is None


@pytest.mark.parametrize('condition,expected', [
    ('Vendor estimates approval takes 3 hours.', 'approval_processing'),
    ('Vendor estimates approval review takes 3 hours.', 'approval_processing'),
    ('Payment is made after approval within 30 days.', 'payment_schedule'),
])
def test_timing_subject_keeps_work_estimate_approval_review_and_payment_separate(condition, expected):
    work = 'Vendor estimates 1.5 hours of work.'
    _, item, _ = native('USD 18 fixed project', work + '\n' + condition)
    claims = [e for e in item.assessment.evidence if e.dimension == 'compensation_conditions']
    assert [(e.code, e.span) for e in claims] == [('vendor_work_estimate', work), (expected, condition)]
    assert item.assessment.selected_pay.labor_time_assumption == work
    assert item.assessment.time_to_cash_days is None


def test_conflicting_work_estimates_retain_all_claims_without_selecting_last():
    first, second = 'Vendor estimates 1.5 hours of work.', 'Vendor estimates 3 hours of work.'
    _, item, _ = native('USD 18 fixed project', first + '\n' + second)
    assert item.assessment.selected_pay.labor_time_assumption is None
    assert [e.span for e in item.assessment.evidence if e.code == 'vendor_work_estimate'] == [first, second]


def test_long_repeated_conditions_bound_digest_without_losing_audit_evidence():
    long = 'Vendor estimates 1.5 hours of work with ' + ('careful checking ' * 180) + 'required.'
    repeats = ['Payment is made 30 days after approval.'] * 20
    extras = [f'Payment is made {n} days after submission.' for n in range(31, 40)]
    result, item, _ = native('USD 18 fixed project', '\n'.join([long, *repeats, *extras]))
    claims = [e for e in item.assessment.evidence if e.dimension == 'compensation_conditions']
    assert len(claims) == 30 and claims[0].span == long
    text = build_digest(result, include_all=True).text
    assert long not in text and text.count('Payment is made 30 days after approval.') == 1
    assert 'full condition evidence retained in audit' in text
    assert len(text) < 6000


@pytest.mark.parametrize('condition', [
    'Paid sabbatical after four years of service.',
    'Paid sick days after the first 90 days.',
    'Paid parental leave after six months.',
    'Manage paid media integration within social campaigns.',
    'Provide stress sign-off approval within a configuration-controlled environment.',
    'Review PRs, debug production issues, and take meaningful work.',
    'Review and challenge work, and take testing seriously.',
    'Code review takes 30 minutes.',
    'Vendor estimates review takes 3 hours.',
])
def test_generic_benefit_or_job_duty_is_not_financial_settlement_timing(condition):
    result, item, _ = native('USD 18 fixed project', condition)
    assert not any(e.dimension == 'compensation_conditions' for e in item.assessment.evidence)
    text = build_digest(result, include_all=True).text
    assert 'payment schedule claim' not in text and 'approval/review processing claim' not in text


@pytest.mark.parametrize('condition,code', [
    ('Vendor estimates approval takes 3 hours.', 'approval_processing'),
    ('Vendor estimates approval review takes 3 hours.', 'approval_processing'),
    ('Submission review may take 2 days.', 'approval_processing'),
    ('Review of submitted work takes 3 hours.', 'approval_processing'),
    ('Approval may take 7 days.', 'approval_processing'),
    ('Payment is made after approval within 30 days.', 'payment_schedule'),
    ('You will be paid within 14 days.', 'payment_schedule'),
    ('Payment after approval.', 'payment_schedule'),
])
def test_actual_compensation_payment_trigger_and_duration_are_retained(condition, code):
    _, item, _ = native('USD 18 fixed project', condition)
    assert [(e.code, e.span) for e in item.assessment.evidence
            if e.dimension == 'compensation_conditions'] == [(code, condition)]
    assert item.assessment.time_to_cash_days is None
