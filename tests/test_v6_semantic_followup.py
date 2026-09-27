"""Follow-up probes authored after the first fix, before their first execution.

Same-agent synthetic probes, not a blind or human-labelled quality benchmark.
"""
import pytest
from test_v6_semantic_regressions import evaluate, release


@pytest.mark.parametrize("quote,amount", [
    ("Applicants receive USD 19 per hour for reviewing coaching material.", 19),
    ("We pay USD 19 per hour with no application fees.", 19),
    ("We pay USD 19 per hour; training examples are supplied.", 19),
    ("Comment by a previous worker: I earned USD 80 per hour. Our offer is USD 19 per hour.", 19),
    ("Example: USD 99 per hour. Our offer is USD 19 per hour.", 19),
    ("This is a fictional example of USD 99 per hour.", None),
    ("Candidates must pay a USD 45 per hour coaching fee.", None),
])
def test_followup_pay_attribution(quote, amount):
    item = evaluate({"quote": quote})
    selected = item.assessment.selected_pay
    assert (selected.amount_low if selected else None) == amount


@pytest.mark.parametrize("text,reject", [
    ("Bengali fluency is mandatory.", False),
    ("German proficiency is mandatory.", True),
    ("German proficiency is desirable, not mandatory.", False),
    ("Remote applicants in India welcome. United Kingdom residents are not eligible.", False),
    ("Remote worldwide. Applicants residing in India are not eligible.", True),
    ("Remote applicants in India welcome. UK residents are not eligible, but Canada residents only.", True),
])
def test_followup_eligibility(text, reject):
    item = evaluate({"description": "Review Bengali AI responses using supplied guidelines. "
        + text + " Compensation is USD 19 per hour."})
    assert (item.decision.action_band.value == "reject") == reject, item.assessment.blockers


@pytest.mark.parametrize("url", [
    "https://www.upwork.com/jobs/~021234567890123456789",
    "https://www.upwork.com/freelance-jobs/apply/whatsapp-integration_~123456789/",
])
def test_followup_fixed_buyer_routes(url):
    item = evaluate({"title": "n8n webhook integration", "url": url,
        "description": "We need a freelancer to connect WhatsApp webhooks. Fixed-price budget: USD 420. "
        "Deliver source and documentation. Contractors in India welcome."})
    assert item.assessment.selected_pay.actual_unit == "fixed_project"
    assert item.assessment.selected_pay.amount_low == 420


def test_buyer_wording_does_not_upgrade_pay_provenance():
    item = evaluate({"quote": "We are looking for a contractor. Compensation USD 19 per hour."})
    assert item.assessment.selected_pay.source_field == "description"
    assert item.assessment.selected_pay.confidence == .70


@pytest.mark.parametrize("text", [
    "Remote applicants in India welcome, but UK residents are not eligible.",
    "Remote worldwide except UK residents.",
])
def test_local_exclusion_does_not_spill_into_positive_scope(text):
    item = evaluate({"description": "Review Bengali AI responses. " + text + " USD 19 per hour."})
    assert item.decision.action_band.value != "reject", item.assessment.blockers


@pytest.mark.parametrize("quote", [
    "We pay USD 19 per hour to write fictional stories.",
    "You receive USD 19 per hour to evaluate hypothetical problems.",
])
def test_imaginary_work_material_does_not_make_real_pay_imaginary(quote):
    item = evaluate({"quote": quote})
    assert item.assessment.selected_pay.amount_low == 19


def test_regional_salary_is_not_country_eligibility():
    item = evaluate({"description": "Review Bengali AI responses. Remote applicants in India welcome. "
        "This salary range ONLY applies to candidates living in the UK for this job. USD 19 per hour."})
    assert not any("location:" in reason for reason in item.assessment.blockers)
