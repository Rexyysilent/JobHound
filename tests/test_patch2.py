"""tests/test_patch2.py (v3 Patch #2) — email parsing + effective-rate floor.

Email fixtures mirror the two synthetic email-offer shapes
Floor cases use a synthetic 4.5 USD/hour threshold.
"""
from types import SimpleNamespace

from jobhound.sources.email_offers import parse_rate, parse_offer
from jobhound.enrich.pay_gate import apply_pay_gate


# ----------------------------------------------------------------- rate parser
def test_milky_way_email():
    offer = parse_offer(
        "New Project Invitation: Project Milky Way",
        "You are invited to Project Milky Way (maps evaluation). "
        "Rate: $3.50/hour. Flexible schedule.")
    assert offer["pay_amount"] == 3.50
    assert offer["pay_currency"] == "USD"
    assert offer["pay_basis"] == "hour"
    assert offer["guaranteed_hours"] is False
    assert "Milky Way" in offer["title"]


def test_hermes_email():
    offer = parse_offer(
        "Project Hermes - LLM English Review",
        "We'd like to invite you to Hermes, an LLM English review program. "
        "Compensation: USD 5 per hour. This is a full time engagement.")
    assert offer["pay_amount"] == 5.0
    assert offer["pay_basis"] == "hour"
    assert offer["guaranteed_hours"] is True


def test_rate_variants():
    assert parse_rate("5 dol/hour")[0] == 5.0
    assert parse_rate("₹300/hr") == (300.0, "INR", "hour")
    assert parse_rate("$4.75 hourly")[0] == 4.75
    assert parse_rate("paid per word: $0.02/word")[2] == "word"
    assert parse_rate("join our great team today") is None


def test_years_experience_is_not_a_rate():
    # Live bug 2026-07-18: "5 years" in the Hermes mail out-matched the real
    # hourly rate and priced the offer at 5 USD/year (~$0.002/hr → rejected).
    offer = parse_offer(
        "Project Hermes - LLM English Review",
        "Requires 5 years of writing experience. This is a full time role. "
        "Compensation: USD 5 per hour.")
    assert (offer["pay_amount"], offer["pay_basis"]) == (5.0, "hour")
    assert parse_rate("candidates need 5 years of experience") is None


def test_prose_project_is_not_a_title():
    # Live bug 2026-07-18: Outlier marketing mail "run your project smoothly"
    # produced title "project smoothly". Names must be Capitalized.
    offer = parse_offer(
        "Inside Outlier: three things",
        "Tips to run your project smoothly and get more tasks.")
    assert offer["title"] == "Inside Outlier: three things"
    lower = parse_offer("Weekly digest", "we will keep your project running")
    assert lower["title"] == "Weekly digest"


def test_parse_fail_safety_net():
    offer = parse_offer("Opportunity at OneForma", "New projects are waiting!")
    assert offer["pay_basis"] == "unknown"
    assert offer["listing_confidence"] == 0.6   # surfaced in ❓, not dropped


# ------------------------------------------------------------------ pay gate
CFG = {"pay": {"floor_usd": 4.5}}


def _job(nominal, platform=None, guaranteed=False):
    return SimpleNamespace(
        pay_min_hourly_usd=None, pay_max_hourly_usd=nominal,
        platform_key=platform, guaranteed_hours=guaranteed,
        effective_hourly_usd=None, pay_ok=None, verdict="pending", reasons=[])


class _Reg(dict):
    def get(self, k):
        return super().get(k)


REG = _Reg({
    "telus_oneforma": SimpleNamespace(availability_est=0.70, unpaid_overhead_est=0.05),
    "outlier":        SimpleNamespace(availability_est=0.45, unpaid_overhead_est=0.30),
})


def test_hermes_guaranteed_ft_passes():
    j = _job(5.0, "telus_oneforma", guaranteed=True)   # 5 x 1.0 x 0.95 = 4.75
    apply_pay_gate(j, REG, CFG)
    assert j.pay_ok is True and j.effective_hourly_usd == 4.75


def test_milky_way_rejected():
    j = _job(3.5, "telus_oneforma", guaranteed=False)  # 3.5 x 0.70 x 0.95 = 2.33
    apply_pay_gate(j, REG, CFG)
    assert j.verdict == "rejected_pay"


def test_outlier_18_scrapes_through():
    j = _job(18.0, "outlier")                          # 18 x 0.45 x 0.70 = 5.67
    apply_pay_gate(j, REG, CFG)
    assert j.pay_ok is True


def test_outlier_8_rejected_by_trust_math():
    j = _job(8.0, "outlier")                           # 8 x 0.45 x 0.70 = 2.52
    apply_pay_gate(j, REG, CFG)
    assert j.verdict == "rejected_pay"


def test_unknown_platform_backward_compatible():
    j = _job(4.6)                                      # effective == nominal
    apply_pay_gate(j, REG, CFG)
    assert j.pay_ok is True and j.effective_hourly_usd == 4.6


def test_unknown_pay_never_rejected():
    j = _job(None)
    apply_pay_gate(j, REG, CFG)
    assert j.pay_ok is None and j.verdict == "pending"
