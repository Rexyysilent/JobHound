"""Native localized amounts, financial ownership and separately retained benefits."""
import hashlib

import pytest

from jobhound.config import CONFIG
from jobhound.v41.compensation import parse_compensation
from jobhound.v41.engine import evaluate_raw,evaluate_observations
from jobhound.v41.digest import build_digest
from test_v6_continuity import raw,NOW


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    monkeypatch.setattr(CONFIG.v55,'account_states',[])


def run(quote):
    result=evaluate_raw([raw(quote)],as_of=NOW)
    assert result.accounting_ok
    return result,result.evaluated[0]


@pytest.mark.parametrize('quote,low,high,unit',[
    ('Le package : 38.000-47.000€ fixe brut, revalorisations bi-annuelles.',38000,47000,'unknown'),
    ('Salaire annuel : 38.000–47.000 EUR.',38000,47000,'year'),
    ('Rémunération : 38\u202f000,50–47\u202f000,75€ brut annuel.',38000.5,47000.75,'year'),
    ('Salaire : 38\u00a0000–47\u00a0000 EUR par an.',38000,47000,'year'),
    ('Salaire : 38 000–47 000 EUR par an.',38000,47000,'year'),
    ('Salaire mensuel : EUR 2.500,50.',2500.5,2500.5,'month'),
    ('Rémunération horaire : 12,50 EUR par heure.',12.5,12.5,'labor_hour'),
    ('Salaire : EUR 12.50 par heure.',12.5,12.5,'labor_hour'),
    ('Salaire : EUR 0.005 par heure.',.005,.005,'labor_hour'),
    ('Salaire : EUR 0,005 par heure.',.005,.005,'labor_hour'),
    ('Salaire : EUR 38.000,500 par an.',38000.5,38000.5,'year'),
    ('Pay EUR 12.345 per hour.',12.345,12.345,'labor_hour'),
    ('Pay EUR 12.345 per hour for reviewing French salaire translations.',12.345,12.345,'labor_hour'),
    ('Pay EUR 12.345 per hour. We review French salaire translations.',12.345,12.345,'labor_hour'),
    ('Remuneration: EUR 12.50 per hour.',12.5,12.5,'labor_hour'),
    ('USD 20 per hour, paid monthly.',20,20,'labor_hour'),
    ('Pay GBP 60,000/year, through this employer.',60000,60000,'year'),
    ('Compensation up to 20 000 INR per month.',20000,20000,'month'),
    ('Salary USD 60\u202f000.50/year.',60000.5,60000.5,'year'),
    ('Salaire : de 75 à 90k€ bruts annuels pour un temps plein, payés sur 12 mois.',75000,90000,'year'),
    ('$63,360 - $126,720 6 days ago',63360,126720,'unknown'),
])
def test_literal_amount_format_and_adjacent_unit(quote,low,high,unit):
    claim=parse_compensation(quote)
    assert (claim.amount_low,claim.amount_high,claim.basis)==(low,high,unit)
    assert claim.raw==quote
    if unit!='labor_hour':assert claim.labor_hour_equivalent is None


@pytest.mark.parametrize('quote',[
    'Pay EUR 12,50 per hour.',
    'Pay EUR 1.234,56 per hour.',
    'Salaire : EUR 38.00.000 par an.',
    'Salaire : EUR 38 00 par an.',
    'Salaire : 38 00 EUR par an.',
    'Salaire : EUR 1,234,567 par an.',
    'Pay USD 38 00 per hour.',
    'Pay 38 00 USD per hour.',
    'Pay EUR 12.345 000 per hour.',
    'Pay USD 12.50 000 per hour.',
])
def test_ambiguous_or_malformed_formats_do_not_supply_hourly_values(quote):
    claim=parse_compensation(quote)
    assert claim.amount_low is None and claim.warnings
    assert claim.labor_hour_equivalent is None


def test_captured_french_package_preserves_gross_amount_without_inventing_a_period():
    quote="Le package : 38.000-47.000€ fixe brut, revalorisations bi-annuelles, intéressement jusqu'à 22% des profits redistribué de façon égalitaire."
    result,item=run(quote);pay=item.assessment.selected_pay
    assert pay and (pay.amount_low,pay.amount_high,pay.currency,pay.actual_unit,pay.gross_net)==(38000,47000,'EUR','unknown','gross')
    assert pay.min_hourly_usd is None and pay.max_hourly_usd is None
    assert item.assessment.pay_rate_for_ranking is None
    text=item.canonical.observations[0].job.description
    assert text[pay.source_span_start:pay.source_span_end]==quote
    assert pay.source_text_sha256==hashlib.sha256(text.encode()).hexdigest()
    digest=build_digest(result,include_all=True)
    assert '38,000' in digest.text and '47,000' in digest.text and digest.accounting_ok


@pytest.mark.parametrize('quote',[
    'Managing a portfolio of high-value motor claims, typically valued above £250,000, through pre- and post-litigation.',
    'Insurance claims valued at USD 250,000 require careful review.',
    'Our motor claims are typically worth GBP 250,000.',
    'Manage USD 250,000 of insurance claims.',
    'Strong experience managing high-value or catastrophic motor bodily injury claims with current authority of at least GBP 250,000.',
])
def test_insurance_case_valuations_are_retained_without_becoming_compensation(quote):
    _,item=run(quote)
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    assert item.assessment.pay_candidates
    assert all(c.scope=='non_pay_context' and c.actor=='non_compensation_context' for c in item.assessment.pay_candidates)
    assert all(c.amount_low==250000 for c in item.assessment.pay_candidates)


@pytest.mark.parametrize('quote',[
    'Salary GBP 60,000/year for managing insurance claims.',
    'Pay USD 20 per hour to value motor claims.',
    'Commission USD 100 per settled insurance claim.',
    'Project budget USD 350 fixed project to automate housing bonus workflows.',
    'USD 20 per hour for the scrap metal trading industry.',
    'USD 350 fixed project to analyze industry growth.',
])
def test_real_wages_and_buyer_budgets_survive_insurance_and_benefit_subjects(quote):
    _,item=run(quote)
    assert item.assessment.selected_pay and item.assessment.selected_pay.scope=='role_claim_unverified'
    assert item.assessment.selected_pay.amount_low in {60000,20,100,350}


@pytest.mark.parametrize('quote',[
    'Base salary USD 60,000/year. USD 10K housing bonus (if you live within 0.5 miles of our office). Up to USD 15K relocation bonus.',
    'Base salary USD 60,000/year and housing bonus USD 10K (if you live near the office).',
    'Base salary USD 60,000/year plus USD 10K housing bonus (if you live near the office).',
    'Base salary USD 60,000/year + USD 10K housing bonus (if you live near the office).',
    'Base salary USD 60,000/year, USD 10K housing bonus (if you live near the office).',
    'Base salary USD 60,000/year plus USD 10K proximity bonus (if you live near the office).',
])
def test_additional_cash_claims_preserve_conditions_without_conflicting_with_base_salary(quote):
    result,item=run(quote)
    salary=item.assessment.selected_pay
    assert salary and (salary.amount_low,salary.actual_unit)==(60000,'year')
    assert not item.assessment.pay_conflict
    benefits=[c for c in item.assessment.pay_candidates if c.scope=='role_benefit_unverified']
    assert benefits and all(c.claim_credibility==0 for c in benefits)
    assert any(c.amount_low==10000 and 'if you live' in c.raw for c in benefits)
    text=item.canonical.observations[0].job.description
    for c in benefits:
        assert text[c.source_span_start:c.source_span_end]==c.raw
        assert c.source_text_sha256==hashlib.sha256(text.encode()).hexdigest()
    digest=build_digest(result,include_all=True)
    assert digest.accounting_ok and 'Additional cash benefits' in digest.text
    assert 'if you live' in digest.text and 'payout not confirmed' in digest.text


def test_benefit_only_posting_does_not_gain_a_wage_and_periods_stay_literal():
    result,item=run('Housing allowance: USD 1000/month. Up to USD 15K relocation bonus.')
    assert item.assessment.selected_pay is None and item.assessment.pay_rate_for_ranking is None
    claims=item.assessment.pay_candidates
    assert {(c.amount_low,c.actual_unit,c.scope) for c in claims}=={
        (1000,'month','role_benefit_unverified'),(15000,'unknown','role_benefit_unverified')}
    assert any(c.up_to for c in claims)
    digest=build_digest(result,include_all=True)
    assert 'Pay: unknown' in digest.text and 'period not stated' in digest.text


def test_other_role_and_illustrative_benefits_are_not_presented_as_this_roles_benefits():
    result,item=run('Other roles receive USD 10K housing bonus. Training example only: USD 15K relocation bonus.')
    assert item.assessment.selected_pay is None
    assert all(c.scope in {'other_assignment','non_pay_context'} for c in item.assessment.pay_candidates)
    assert 'Additional cash benefits' not in build_digest(result,include_all=True).text


def test_native_buyer_benefits_keep_actor_payload_and_current_term_scope():
    from jobhound.v41.community import project_thread,thread_outcome,saved_thread_observation
    from test_community_threads import topic,post,URL
    pages=[topic(post(cooked='<p>Looking to hire a freelancer for a paid automation workflow. '
        'Project budget: USD 350 fixed project. Relocation bonus: USD 100.</p>'))]
    observation=saved_thread_observation(thread_outcome(project_thread(pages,URL,NOW),pages))
    result=evaluate_observations([observation],as_of=NOW,raw_count=0);item=result.evaluated[0]
    assert item.assessment.selected_pay.amount_low==350 and not item.assessment.pay_conflict
    benefit=next(c for c in item.assessment.pay_candidates if c.scope.startswith('community_benefit:'))
    assert benefit.actor=='forum_actor:10' and benefit.structured_path and benefit.structured_payload_sha256
    assert benefit.amount_low==100 and benefit.claim_credibility==0


def test_superseded_buyer_benefits_are_retained_without_becoming_current_benefits():
    from datetime import timedelta
    from jobhound.v41.community import project_thread,thread_outcome,saved_thread_observation
    from test_community_threads import topic,post,URL
    pages=[topic(post(cooked='<p>Looking to hire a freelancer for a paid automation workflow. '
        'Project budget: USD 350 fixed project. Relocation bonus: USD 100.</p>'),
        post(2,10,'Updated budget: USD 750 fixed project.',created_at=(NOW-timedelta(hours=1)).isoformat()))]
    observation=saved_thread_observation(thread_outcome(project_thread(pages,URL,NOW),pages))
    result=evaluate_observations([observation],as_of=NOW,raw_count=0);item=result.evaluated[0]
    assert item.assessment.selected_pay.amount_low==750
    old=next(c for c in item.assessment.pay_candidates if c.amount_low==100)
    assert old.scope=='historical_thread_terms' and old.actor=='forum_actor:10'
    assert old.structured_payload_sha256 and old.structured_path
    assert 'Additional cash benefits' not in build_digest(result,include_all=True).text


def test_right_labelled_monthly_benefit_has_its_own_unit_without_supplying_hourly_pay():
    _,item=run('USD 1000/month housing allowance.')
    assert item.assessment.selected_pay is None and item.assessment.pay_rate_for_ranking is None
    assert len(item.assessment.pay_candidates)==1
    benefit=item.assessment.pay_candidates[0]
    assert benefit.scope=='role_benefit_unverified' and benefit.actual_unit=='month'
    assert benefit.min_hourly_usd is None


def test_long_conditions_and_more_than_three_benefits_remain_in_the_uncapped_audit():
    quotes=['Housing bonus: USD 1000 '+('eligibility review ' * 20)+'only if approved in writing.',
        'Relocation bonus: USD 2000.', 'Signing bonus: USD 3000.', 'Sign-on bonus: USD 4000.']
    result,item=run('\n'.join(quotes))
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    benefits=[c for c in item.assessment.pay_candidates if c.scope=='role_benefit_unverified']
    assert len(benefits)==4 and any('only if approved in writing' in c.raw for c in benefits)
    digest=build_digest(result,include_all=True)
    assert 'full wording and conditions retained in audit' in digest.text
    assert '1 additional claim retained in audit' in digest.text


@pytest.mark.parametrize('quote,amount',[
    ('Company revenue of EUR 1.3 billion supports growth.',1300000000),
    ('We operate in a USD 12 trillion global industry.',12000000000000),
    ('With over USD 110B in loans funded, the company is growing.',110000000000),
    ('We raised GBP 1.5 billion in funding.',1500000000),
])
def test_explicit_business_scales_preserve_literal_values_without_creating_wages(quote,amount):
    _,item=run(quote)
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    assert item.assessment.pay_candidates and all(c.scope=='non_pay_context' for c in item.assessment.pay_candidates)
    assert all(c.amount_low==amount for c in item.assessment.pay_candidates)


@pytest.mark.parametrize('quote',[
    'An estimated USD 124 trillion of assets will be inherited by younger generations in the next two decades.',
    'We manage creative and media for brands worldwide and USD 6B in ad spend across media platforms.',
    'We are bringing the USD 610 billion scrap metal trading industry into this century.',
    'We completed 120 projects worth USD 5.8B across five countries.',
    'An industry worth over USD 400B is being transformed.',
    'A market expected to reach USD 28.5 billion by 2028 is growing.',
    'The company manages approximately USD 82.3 billion in AUM across its strategies.',
    'Unlock USD 100B in customer value by 2035.',
    'Companies use technology to move USD 19B of merchandise across countries each year.',
    'Businesses have generated over USD 7 billion in ecosystem value.',
    'Our company—privately valued at over USD 1 billion—is growing.',
    'A platform targeting the USD 180B+ iGaming and sports betting markets.',
    'Backed by over USD 1 billion in capital, we are growing.',
    'Our platform has powered over USD 14 trillion in digital asset transfers worldwide.',
    'Our total grantmaking is likely to approach or even exceed USD 1 billion this year.',
    'We help customers access more than USD 2 billion in credit.',
    'We unlock USD 2 billion in cumulative credit for everyday earners.',
    'We tackle a USD 1.7 trillion problem.',
    'We are positioned to build the next USD 10B company powering the economy.',
    'Help transform the industry while building toward a USD 10B+ outcome.',
])
def test_newly_parsed_business_values_do_not_expose_another_false_wage(quote):
    _,item=run(quote)
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    assert item.assessment.pay_candidates
    assert all(c.scope=='non_pay_context' for c in item.assessment.pay_candidates)


@pytest.mark.parametrize('quote',[
    'Corporate Benefits Program covering health, mobility, and learning for 100 EUR net per month.',
    "Des tickets restaurant d'une valeur de 11€ par jour sont remis pour les restaurants du coin.",
    'Nos plus : Carte tickets restaurant 9,50 € par jour travaillé (60% part employeur).',
    'Carte Swile créditée de 9 € par jour travaillé',
    'USD 1,500/month meals stipend.',
    'Technology stipend – Equivalent to US$100 net per month to help support your work.',
])
def test_designated_health_learning_and_meal_benefits_do_not_gain_a_wage_period(quote):
    _,item=run(quote)
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    assert all(c.scope=='non_pay_context' for c in item.assessment.pay_candidates)


def test_native_benefit_list_marker_preserves_amount_and_condition_without_becoming_negative_pay():
    result,item=run('Benefits\n- $10K housing bonus (if you live within 0.5 miles of our office)\n- Up to $15K relocation bonus')
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    benefits=[c for c in item.assessment.pay_candidates if c.scope=='role_benefit_unverified']
    assert {c.amount_low for c in benefits}=={10000,15000}
    assert any(c.up_to for c in benefits) and all(c.actual_unit=='unknown' for c in benefits)
    assert all(c.raw.startswith('- ') for c in benefits)
    assert 'if you live' in build_digest(result,include_all=True).text


@pytest.mark.parametrize('quote',[
    'Salary - USD 20 per hour.',
    '-USD 20 per hour.',
    '- USD 20 per hour repayment deduction.',
])
def test_explicit_negative_or_deduction_claim_is_not_repaired_as_a_list_bullet(quote):
    _,item=run(quote)
    assert item.assessment.pay_rate_for_ranking is None
    assert all(c.amount_low is None for c in item.assessment.pay_candidates)


def test_previous_pay_heading_does_not_repair_a_negative_claim_in_later_responsibilities():
    _,item=run('Benefits\nEmployer insurance.\nResponsibilities\n- USD 20 per hour.')
    assert item.assessment.pay_rate_for_ranking is None
    assert all(c.amount_low is None for c in item.assessment.pay_candidates)


@pytest.mark.parametrize('quote',[
    '(3 320 € de moyenne 2025 pour les commerciaux débutants).',
    '(4 100 € de moyenne de salaire en 2025 à partir du 4ème mois).',
    "6 600 € : c'est le salaire moyen mensuel obtenu en 2025 par les commerciaux experts.",
])
def test_dated_salary_averages_remain_benchmarks_without_becoming_current_pay(quote):
    _,item=run(quote)
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    assert all(c.scope=='other_assignment' for c in item.assessment.pay_candidates)


@pytest.mark.parametrize('quote',[
    'Salary EUR 70000/year, above the moyenne de salaire en 2025.',
    'Salaire annuel EUR 70000, supérieur à la moyenne de salaire en 2025.',
    'Rémunération annuelle EUR 70000, supérieure à la moyenne de salaire en 2025.',
])
def test_current_salary_is_preserved_when_it_cites_a_dated_benchmark(quote):
    _,item=run(quote)
    assert item.assessment.selected_pay.amount_low==70000


def test_profit_share_bonus_is_retained_separately_from_base_salary():
    result,item=run('Salaire annuel EUR 70000. Une participation annuelle aux bénéfices de Unaferm de 6 100 € sur les résultats 2025.')
    assert item.assessment.selected_pay.amount_low==70000 and not item.assessment.pay_conflict
    benefit=next(c for c in item.assessment.pay_candidates if c.scope=='role_benefit_unverified')
    assert benefit.amount_low==6100 and benefit.actual_unit=='unknown'
    assert 'résultats 2025' in build_digest(result,include_all=True).text


def test_truncated_aggregate_scale_retains_literal_amount_and_unknown_ownership():
    _,item=run('With over USD 25 billion in an…')
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    candidate=item.assessment.pay_candidates[0]
    assert candidate.amount_low==25000000000 and candidate.scope=='unattributed_amount'
    assert candidate.claim_credibility==0


def test_large_explicit_salary_retains_work_ownership():
    _,item=run('Salary USD 1 billion/year.')
    assert item.assessment.selected_pay.amount_low==1000000000
    assert item.assessment.selected_pay.scope=='role_claim_unverified'


def test_buyer_identity_does_not_turn_an_unattributed_aggregate_into_a_project_budget():
    from jobhound.v41.community import project_thread,thread_outcome,saved_thread_observation
    from test_community_threads import topic,post,URL
    pages=[topic(post(cooked='<p>Looking to hire a freelancer for a paid automation workflow. '
        'Project budget: USD 350 fixed project. With over USD 25 billion in an…</p>'))]
    observation=saved_thread_observation(thread_outcome(project_thread(pages,URL,NOW),pages))
    result=evaluate_observations([observation],as_of=NOW,raw_count=0);item=result.evaluated[0]
    assert item.assessment.selected_pay.amount_low==350 and not item.assessment.pay_conflict
    ambiguous=next(c for c in item.assessment.pay_candidates if c.amount_low==25000000000)
    assert ambiguous.scope=='unattributed_amount' and ambiguous.claim_credibility==0


def test_truncated_counter_after_range_keeps_ambiguity_instead_of_inventing_metadata():
    claim=parse_compensation('USD 30 - USD 50 3 ...')
    assert claim.amount_low is None and claim.warnings==('ambiguous_number_grouping',)


def test_training_stipend_and_budget_for_card_credit_work_keep_wage_ownership():
    for quote in ('Stipend USD 100/month for this training assignment.',
                  'Project budget USD 350 fixed project to reconcile card credit and customer loans.'):
        _,item=run(quote)
        assert item.assessment.selected_pay and item.assessment.selected_pay.scope=='role_claim_unverified'
