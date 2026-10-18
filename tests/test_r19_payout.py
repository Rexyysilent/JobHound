"""R19: real-engine checks of synthetic listing payout terms, no providers."""
import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.models import ListingObservation, SourceKind
from test_v55_action_policy import observation, NOW


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)


def evaluate(amount='USD 18 fixed project budget', terms='Wire payout requires amount above USD 50.', *more):
    row = observation()
    row.job.pay_raw = amount
    row.job.description += '\n' + terms
    return evaluate_observations([row, *more], as_of=NOW).evaluated[0]


def test_r19_wire_logo_does_not_make_eighteen_dollars_payable():
    item = evaluate()
    view = item.assessment.payout_feasibility
    assert view['state'] == 'single_amount_infeasible'
    assert view['amount'] == '18.0'
    assert view['routes'][0]['minimum'] == '50'
    assert item.decision.next_action == 'verify'
    assert any(t['missing_fact'] == 'payout_feasibility' for t in item.decision.verification_tasks)
    assert item.canonical.observations[0].job.pay_raw == 'USD 18 fixed project budget'
    assert any(e.dimension == 'payout' and e.observation_id == 'one' and e.span
               for e in item.assessment.evidence)
    assert not item.assessment.pay_conflict
    threshold = next(c for c in item.assessment.pay_candidates if c.amount_low == 50)
    assert threshold.scope == 'non_pay_context'


def test_existing_scam_rejection_is_preserved_for_literal_wire_transfer():
    item = evaluate(terms='Wire transfer requires amount above USD 50.')
    assert item.assessment.payout_feasibility['state'] == 'single_amount_infeasible'
    assert item.decision.next_action == 'skip'


@pytest.mark.parametrize('amount,op,expected', [
    ('49.99', 'above', 'single_amount_infeasible'),
    ('50', 'above', 'single_amount_infeasible'),
    ('50.01', 'above', 'amount_compatible'),
    ('50', 'at least', 'amount_compatible'),
])
def test_exact_threshold_boundary(amount, op, expected):
    item = evaluate(f'USD {amount} fixed project budget', f'Wire payout requires amount {op} USD 50.')
    assert item.assessment.payout_feasibility['state'] == expected


@pytest.mark.parametrize('terms,reason', [
    ('Wire payout requires amount above EUR 50.', 'currency_mismatch'),
    ('Wire payout requires amount above 50.', 'currency_unknown'),
    ('Payment methods: wire.', 'threshold_unknown'),
])
def test_missing_or_incomparable_terms_are_unknown(terms, reason):
    view = evaluate(terms=terms).assessment.payout_feasibility
    assert view['state'] == 'unknown'
    assert view['routes'][0]['reason'] == reason


@pytest.mark.parametrize('pay', [None, 'USD 18 per working hour'])
def test_rate_or_missing_pay_is_not_single_project_amount(pay):
    view = evaluate(amount=pay).assessment.payout_feasibility
    assert view['state'] == 'unknown'
    assert view['amount'] is None


def test_alternate_rail_needs_its_own_explicit_terms():
    unknown = evaluate(terms='Wire payout requires amount above USD 50.\nPayment methods: PayPal.')
    assert unknown.assessment.payout_feasibility['state'] == 'unknown'
    assert unknown.decision.next_action == 'verify'
    known = evaluate(terms='Wire payout requires amount above USD 50.\nPayPal payout has no minimum.')
    assert known.assessment.payout_feasibility['state'] == 'amount_compatible'
    assert not any(t['missing_fact'] == 'payout_feasibility' for t in known.decision.verification_tasks)


def test_aggregation_is_not_assumed_from_multiple_projects_or_balance_claim():
    item = evaluate(terms='Wire payout requires amount above USD 50.\nOther projects may be available.')
    assert item.assessment.payout_feasibility['state'] == 'single_amount_infeasible'
    assert 'supported aggregation' in item.decision.next_step


def test_other_observation_cannot_supply_alternate_method_terms():
    copy = observation('copy', source_kind=SourceKind.AGGREGATOR_UNRESOLVED)
    copy.job.url = observation().job.url
    copy.normalized_url = copy.original_url = copy.job.url
    copy.job.pay_raw = None
    copy.job.description += '\nPayPal payout has no minimum.'
    item = evaluate('USD 18 fixed project budget', 'Wire payout requires amount above USD 50.', copy)
    assert item.assessment.selected_pay.observation_id == 'one'
    assert item.assessment.payout_feasibility['state'] == 'single_amount_infeasible'
    assert {r['observation_id'] for r in item.assessment.payout_feasibility['routes']} == {'one'}


def test_threshold_and_employer_revenue_cannot_become_project_compensation():
    item = evaluate(amount=None, terms='Employer revenue is USD 5000000.\nWire payout requires amount above USD 50.')
    assert item.assessment.payout_feasibility['state'] == 'unknown'
    assert item.assessment.payout_feasibility['amount'] is None


def test_mixed_wage_and_threshold_clause_retains_wage_and_ambiguous_binding():
    item = evaluate(amount=None, terms='Project budget: USD 18 and wire payout requires amount above USD 50.')
    assert any('USD 18' in c.raw and c.scope != 'non_pay_context'
               and 'multiple_money_claims_require_segmentation' in c.parse_warnings
               for c in item.assessment.pay_candidates)
    assert item.assessment.payout_feasibility['state'] != 'amount_compatible'


@pytest.mark.parametrize('terms', ['Wire payout requires amount above USD 1,000.',
                                   'Wire payout is not available for amounts above USD 10.'])
def test_unsupported_or_negated_terms_cannot_claim_compatibility(terms):
    assert evaluate(terms=terms).assessment.payout_feasibility['state'] == 'unknown'


@pytest.mark.parametrize('terms', [
    'Wire payout fees are waived for amounts above USD 50.',
    'Wire payout is available with a fee waiver for amounts above USD 50.',
    'Wire payout premium tier applies for amounts above USD 50.',
    'Wire payout amounts above USD 50 receive a bonus.',
    'Wire payout amounts above USD 50.',
])
def test_fee_waiver_tier_or_unbound_amount_condition_is_not_a_minimum(terms):
    view = evaluate(terms=terms).assessment.payout_feasibility
    assert view['state'] == 'unknown'
    assert 'minimum' not in view['routes'][0]


def test_no_terms_preserves_existing_engine_behavior():
    item = evaluate(amount='USD 10 per working hour', terms='')
    assert item.assessment.payout_feasibility == {}
    assert item.decision.next_action == 'apply'


@pytest.mark.parametrize('cadence', ['daily', 'day', 'hour', 'month', 'task', 'day-', 'weekly'])
def test_cadence_wise_payment_does_not_invent_provider(cadence):
    item = evaluate(amount='USD 10 per working hour',
                    terms=f'Work from home with {cadence} wise payment option.')
    assert item.assessment.payout_feasibility == {}
    assert not any(u.startswith('payout_feasibility:') for u in item.assessment.unresolved)
    assert item.decision.next_action == 'apply'


@pytest.mark.parametrize('terms', ['Paid via Wise.', 'Paid via wise.',
    'Payment methods: wise.', 'Wise payout has no minimum.',
    'wise payout requires amount above USD 50.'])
def test_explicit_wise_provider_context_survives_case_insensitively(terms):
    view = evaluate(terms=terms).assessment.payout_feasibility
    assert view['routes'][0]['method'] == 'wise'
    if 'no minimum' in terms:
        assert view['state'] == 'amount_compatible'
    if 'above' in terms:
        assert view['state'] == 'single_amount_infeasible'


def test_closed_listing_retains_closure_despite_historical_payout_terms():
    row = observation(vacancy_state='explicitly_closed')
    row.job.pay_raw = 'USD 18 fixed project budget'
    row.job.description += '\nWire payout requires amount above USD 50.'
    item = evaluate_observations([row], as_of=NOW).evaluated[0]
    assert item.assessment.payout_feasibility['state'] == 'single_amount_infeasible'
    assert item.decision.lifecycle == 'closed'
    assert item.decision.next_action != 'apply'


def test_account_allocation_cannot_override_amount_constraint():
    account = ListingObservation(observation_id='account', source='account_state', origin='account_state',
        parent_observation_ids=['one'], captured_at=NOW,
        raw_payload={'project_access': 'accessible', 'task_allocation': 'allocated',
                     'observed_at': NOW.isoformat()})
    item = evaluate('USD 18 fixed project budget', 'Wire payout requires amount above USD 50.', account)
    assert item.decision.next_action == 'verify'
    assert item.assessment.time_to_cash_days is None


def test_reviewed_v3_ready_work_starts_until_amount_constraint_then_plan_agrees():
    from test_v57_outcomes import setup
    from test_work_outcomes import ready, preview

    def reviewed(terms):
        row = observation()
        row.job.pay_raw = 'USD 18 fixed project budget'
        row.job.description += '\n' + terms
        result, binding = setup(row)
        events = ready(binding)
        next(e for e in events if e['predicate'] == 'work_terms')['value'] = dict(
            amount='18', currency='USD', reference='exact-project-contract', unit='task')
        return preview(result, binding, events)[0]

    unrestricted = reviewed('')
    assert unrestricted.decision.next_action == 'start_allocated_task'
    assert unrestricted.assessment.account_state['_reviewed_work_action']['prerequisites'] == []
    restricted = reviewed('Wire payout requires amount above USD 50.')
    assert restricted.assessment.payout_feasibility['state'] == 'single_amount_infeasible'
    assert restricted.decision.next_action == 'verify'
    assert restricted.decision.priority_key.action_readiness < 3
    plan = restricted.assessment.account_state['_reviewed_work_action']
    assert plan['authority'] == 'reviewed_work_v3'
    assert plan['next_action'] == 'verify'
    assert plan['prerequisites'] == ['payout_feasibility']
    assert plan['next_action_cost'] is None and plan['read_only'] is False
