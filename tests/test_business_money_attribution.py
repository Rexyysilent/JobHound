"""Native business amounts stay inspectable without becoming worker pay."""
import hashlib

import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_raw, evaluate_observations
from jobhound.v41.digest import build_digest
from test_v6_continuity import raw, NOW


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def run(quote):
    result = evaluate_raw([raw(quote)], as_of=NOW)
    assert result.accounting_ok and build_digest(result,include_all=True).accounting_ok
    return result.evaluated[0]


BUSINESS_QUOTES = [
    'With revenues of more than $40 billion and the largest investment in R&D, we give our people resources.',
    'Backed by over €7 million in funding, we want to prove this technology can be useful.',
    '180 days: $5M+ in active project revenue under your management.',
    'We are aiming for a valuation exceeding $1 billion by 2026.',
    'With over $400M in revenue and profitable growth, we are building our platform.',
    '$18M raised and profitable',
    "We've raised $26 million in Series B financing and are scaling fast.",
    'With €1.3 billion in revenues, the company has a record of growth.',
    "We've raised ~£140M over three funding rounds and turned profitable.",
    'Our team has raised $60M in Series C funding from investors.',
    'In March, we raised a $25M Series A led by our investors.',
    'We were founded in 2022 and have raised a total of $1 billion in funding.',
    'Today, we manage more than £2.5 billion for a broad range of investors.',
    'You have operated with an average deal size of $100k+.',
    'The team reached $60M annual revenues in 2024.',
    'We closed the fiscal year with $3.7 billion in revenue.',
    'Our platform is backed by $23M Series B funding.',
    '💰 €400 million+ raised since launch.',
    'Experience at a pre $100M revenue company is desirable.',
    'We secured US$340 million: a $100 million Series C and a $240 million growth investment.',
    'Company revenue is USD 400,000 per month.',
    'Annual turnover of 500,000 EUR supports our growth.',
    'Our profits are USD 200,000 annually.',
    'We have USD 200,000 in profits this year.',
    'With over $110B in loans funded and $1.2B raised, we operate at real scale.',
    "We've raised $781M in funding and our last valuation was $22B.",
    'Founded in 2008 and profitable since 2014, annual revenue now exceeds $100m USD and millions use our browser.',
    "With $200+ million in annual revenue and team members around the globe, we are the world's largest remote workforce.",
    'Our agents transform the physical economy, a $12 trillion global industry.',
    'We work in a $100B market structuring at high speed.',
    "We've grown annual recurring revenue from $1 million to $50 million in two years.",
    'We grew annual recurring revenue from USD 1M to USD 50M in two years.',
    'Our platform scales toward €100m+ ARR without losing speed.',
    'Mercor is a profitable Series C company valued at $10 billion.',
    'We are looking for territory owners who want to manage deal cycles from €10k to €300k, selling a complex suite of products.',
    '5+ years of experience in B2B SaaS account management, ideally working with large enterprise clients (> €50-150K ACV).',
    'Own a portfolio of enterprise accounts with ACV in the £50k–£300k+ range — and the mandate to grow them.',
]


@pytest.mark.parametrize('quote',BUSINESS_QUOTES)
def test_business_amounts_do_not_supply_pay_or_conflicts(quote):
    item = run(quote)
    assert item.assessment.selected_pay is None
    assert item.assessment.pay_rate_for_ranking is None
    assert item.assessment.pay_candidates
    assert all(c.scope=='non_pay_context' and c.actor=='non_compensation_context'
               for c in item.assessment.pay_candidates)
    assert not item.assessment.pay_conflict
    for candidate in item.assessment.pay_candidates:
        text=item.canonical.observations[0].job.description
        assert text[candidate.source_span_start:candidate.source_span_end]==candidate.raw
        assert candidate.source_text_sha256==hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize('quote,amount,unit',[
    ('Compensation: USD 20/hour for analysts experienced in valuation and investment banking.',20,'labor_hour'),
    ('Salary is USD 80k/year for revenue operations.',80000,'year'),
    ('Pay raised to USD 30/hour.',30,'labor_hour'),
    ('Project budget: USD 350 fixed project for a revenue dashboard.',350,'fixed_project'),
    ('Budget: USD 350 fixed project, funded by our Series B.',350,'fixed_project'),
    ('Project funding of USD 350 fixed project is available for the contractor.',350,'fixed_project'),
    ('Pay USD 20 per accepted image to annotate annual revenue reports.',20,'output_item'),
    ('USD 20 per hour for a fundraising campaign.',20,'labor_hour'),
    ('Commission: USD 100 per accepted sale; a share of revenue.',100,'unknown'),
    ('Commission: USD 100 revenue share for this assignment.',100,'unknown'),
    ('Compensation: USD 20/hour for annual recurring revenue dashboards.',20,'labor_hour'),
    ('Project budget: USD 350 fixed project for ARR dashboards.',350,'fixed_project'),
    ('Internship stipend: USD 1500 monthly.',1500,'month'),
    ('A project valued at USD 350 fixed project is offered to the contractor.',350,'fixed_project'),
])
def test_compensation_near_financial_subjects_is_retained(quote,amount,unit):
    pay=run(quote).assessment.selected_pay
    assert pay and pay.amount_low==amount and pay.actual_unit==unit
    assert pay.scope=='role_claim_unverified'


@pytest.mark.parametrize('quote',[
    'Our revenues are USD 40M, compensation: USD 20/hour.',
    'We raised USD 40M and salary is USD 80k/year.',
    'Project budget: USD 350 fixed project, our annual revenue is USD 40M.',
    'Revenue USD 40M, but hourly pay: USD 20 per working hour.',
])
def test_labelled_finance_and_compensation_clauses_do_not_cross_bind(quote):
    item=run(quote)
    pay=item.assessment.selected_pay
    assert pay and pay.amount_low in {20,350,80000}
    assert any(c.scope=='non_pay_context' for c in item.assessment.pay_candidates)
    assert not item.assessment.pay_conflict
    assert all(c.amount_low!=40000000 for c in item.assessment.pay_candidates if c.scope!='non_pay_context')
    text=item.canonical.observations[0].job.description
    for candidate in item.assessment.pay_candidates:
        assert text[candidate.source_span_start:candidate.source_span_end]==candidate.raw
        assert candidate.source_text_sha256==hashlib.sha256(text.encode()).hexdigest()


def test_unlabelled_alternatives_keep_the_existing_abstention():
    item=run('We pay USD 5 per image or USD 60 per hour depending on the assignment.')
    assert all(c.amount_low is None and c.parse_warnings for c in item.assessment.pay_candidates)
    assert item.assessment.pay_rate_for_ranking is None


def test_explicit_wage_subjects_in_one_sentence_preserve_a_real_unit_conflict():
    item=run('Pay USD 20 per hour, compensation USD 30 per recorded audio hour.')
    assert {c.actual_unit for c in item.assessment.pay_candidates}=={'labor_hour','output_audio_hour'}
    assert item.assessment.pay_conflict and item.assessment.pay_rate_for_ranking is None


def test_money_range_and_suffix_currency_are_not_separate_amounts():
    from jobhound.v41.compensation import money_expression_spans
    text='Annual revenues of USD 1M, compensation: 20–30 EUR per hour.'
    assert [text[a:b] for a,b in money_expression_spans(text)]==['USD 1M','20–30 EUR']
    pay=run(text).assessment.selected_pay
    assert pay and (pay.amount_low,pay.amount_high,pay.currency,pay.actual_unit)==(20,30,'EUR','labor_hour')


def test_native_buyer_budget_survives_own_company_funding_context():
    from jobhound.v41.community import project_thread,thread_outcome,saved_thread_observation
    from test_community_threads import topic,post,URL
    pages=[topic(post(cooked='<p>Looking to hire a freelancer for a paid automation workflow. '
        'We raised USD 2M. Project budget: USD 350 fixed project.</p>'))]
    result=evaluate_observations([saved_thread_observation(thread_outcome(project_thread(pages,URL,NOW),pages))],as_of=NOW,raw_count=0)
    item=result.evaluated[0]
    pay=item.assessment.selected_pay
    assert pay and pay.amount_low==350 and pay.actual_unit=='fixed_project'
    assert pay.actor=='forum_actor:10' and pay.structured_path
    assert not item.assessment.pay_conflict


@pytest.mark.parametrize('quote',[
    'Annual personal development budget of €500 for conferences and courses.',
    'Annual professional development allowance: USD 1000.',
    'A £500 home office budget is provided.',
    "A £100/month Healthy Heidi's wellness stipend is available.",
    "We offer a £100/month Healthy Heidi's wellness stipend, a £500 home office budget, a £700 learning and development budget.",
    'System migrations cost $100M+ and years of pain.',
    'Infrastructure costs of USD 300M annually are part of our business.',
    '- $1.5K monthly stipend for meals',
    '• $1.5K monthly stipend for meals.',
    'Meal stipend: USD 1500 per month.',
    'Additional benefits include: 20 days "work from abroad", 600EUR/GBP Learning & Development Budget, and other local benefits.',
    '$200 monthly laundry reimbursement',
    '• $200 monthly laundry reimbursement.',
    '$200 monthly personal wellness reimbursement',
    '• $200 monthly personal wellness reimbursement.',
    "We offer comprehensive private medical and dental cover through Bupa, 24/7 mental health, coaching and wellbeing support through Sonder, a £100/month Healthy Heidi's wellness stipend, a £500 home office budget, a £700 annual learning and development budget, 26 weeks paid primary parental leave and 18 weeks paid secondary parental leave, fertility support up to £7,000, up to four weeks of work from anywhere per year,",
])
def test_replacement_perks_and_business_expenses_do_not_become_pay(quote):
    item=run(quote)
    assert item.assessment.selected_pay is None and not item.assessment.pay_conflict
    assert item.assessment.pay_candidates
    assert all(c.scope=='non_pay_context' for c in item.assessment.pay_candidates)


@pytest.mark.parametrize('quote',[
    'Pay USD 20 per hour. Annual professional development budget of USD 500.',
    'Compensation: USD 20/hour. System migrations cost USD 2M.',
    'Project budget: USD 350 fixed project for home office equipment automation.',
    'Project budget: USD 350 fixed project for a system migration.',
    'Project budget: USD 350 fixed project for laundry workflow automation.',
])
def test_real_compensation_survives_other_costs_and_perks(quote):
    item=run(quote)
    pay=item.assessment.selected_pay
    assert pay and pay.amount_low in {20,350}
    assert not item.assessment.pay_conflict


@pytest.mark.parametrize('company_quote',[quote for quote in BUSINESS_QUOTES
    if 'annual revenue now exceeds' in quote or '$200+ million in annual revenue' in quote])
def test_formerly_excluded_revenue_stays_excluded_beside_genuine_salary(company_quote):
    item=run('USD 178,500 annually and stock options. '+company_quote)
    pay=item.assessment.selected_pay
    assert pay and (pay.amount_low,pay.actual_unit)==(178500,'year')
    assert not item.assessment.pay_conflict
    assert any(c.scope=='non_pay_context' and c.raw==company_quote for c in item.assessment.pay_candidates)


@pytest.mark.parametrize('quote',[
    'Millions of domain experts on the platform are paid over $4 million per day to train frontier AI models.',
    "As Senior Product Manager, Payments, you'll own how Mercor moves over $4 million a day to hundreds of thousands of experts in nearly every country on earth.",
    'You get the scale of a platform that already creates hundreds of thousands of jobs and pays out over $4 million a day.',
])
def test_platform_pool_payments_do_not_supply_one_workers_wage(quote):
    item=run('Compensation: USD 20 per hour. '+quote)
    pay=item.assessment.selected_pay
    assert pay and pay.amount_low==20 and pay.actual_unit=='labor_hour'
    assert not item.assessment.pay_conflict
    assert any(c.raw==quote and c.scope=='other_assignment' for c in item.assessment.pay_candidates)


@pytest.mark.parametrize('quote,amount,unit',[
    ('Thousands of domain experts are paid USD 20 per hour each.',20,'labor_hour'),
    ('Thousands of domain experts are paid USD 20 per hour.',20,'labor_hour'),
    ('Thousands of domain experts on the platform are paid USD 20 per hour.',20,'labor_hour'),
    ('The platform pays out USD 20 per hour to each worker.',20,'labor_hour'),
    ('The platform pays out USD 100 per day to each contractor.',100,'day'),
])
def test_explicit_per_worker_rate_is_preserved(quote,amount,unit):
    pay=run(quote).assessment.selected_pay
    assert pay and pay.amount_low==amount and pay.actual_unit==unit
