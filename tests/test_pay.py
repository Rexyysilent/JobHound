"""Pay-normalization fixtures — the sweatshop gate must be trustworthy.

v3: the floor decision moved out of enrich_pay into pay_gate.apply_pay_gate
(effective-rate basis) — gate tests here run it with an empty registry, so
effective == nominal and the old flat-floor expectations still hold.
"""
import pytest

from jobhound.config import PayCfg
from jobhound.enrich.pay import enrich_pay, parse_pay
from jobhound.enrich.pay_gate import apply_pay_gate
from jobhound.models import Job

CFG = PayCfg(
    floor_usd=6.0,
    hard_reject_usd_per_hour=1.5,
    hours_per_year=2080,
    hours_per_month=173,
    fx_to_usd={"USD": 1.0, "INR": 0.012, "EUR": 1.08, "GBP": 1.27},
    piece_rate_per_hour={"task": 20, "word": 500, "audio_min": 6, "image": 60},
)


@pytest.mark.parametrize(
    "raw, exp_basis, exp_min, exp_max, exp_estimate",
    [
        ("$70,000 - $90,000 per year", "year", 33.65, 43.27, False),
        ("$25/hr", "hour", 25.0, 25.0, False),
        ("$120k", "year", 57.69, 57.69, False),         # basis inferred from magnitude
        ("₹30,000 per month", "month", 2.08, 2.08, False),  # INR → USD → hourly
        ("Rs 800 per hour", "hour", 9.6, 9.6, False),
        ("$0.10 per word", "word", 50.0, 50.0, True),    # piece-rate estimate
        ("$2 per task", "task", 40.0, 40.0, True),
    ],
)
def test_parse_pay_values(raw, exp_basis, exp_min, exp_max, exp_estimate):
    lo, hi, basis, est = parse_pay(raw, CFG)
    assert basis == exp_basis
    assert lo == pytest.approx(exp_min, abs=0.05)
    assert hi == pytest.approx(exp_max, abs=0.05)
    assert est is exp_estimate


@pytest.mark.parametrize("raw", [None, "", "competitive salary", "DOE", "negotiable"])
def test_unknown_pay_is_not_rejected(raw):
    lo, hi, basis, est = parse_pay(raw, CFG)
    assert (lo, hi, basis) == (None, None, "unknown")


def test_gate_sets_pay_ok_above_floor():
    job = Job(source="t", title="x", url="http://x", pay_raw="$40/hr")
    enrich_pay(job, CFG)
    apply_pay_gate(job, {}, CFG)
    assert job.pay_ok is True
    assert job.pay_max_hourly_usd == pytest.approx(40.0)


def test_gate_flags_sweatshop_below_floor():
    job = Job(source="t", title="x", url="http://x", pay_raw="₹20,000 per month")
    enrich_pay(job, CFG)
    apply_pay_gate(job, {}, CFG)
    assert job.pay_ok is False  # ~1.39 $/hr < floor


def test_gate_unknown_pay_ok_is_none():
    job = Job(source="t", title="x", url="http://x", pay_raw=None)
    enrich_pay(job, CFG)
    apply_pay_gate(job, {}, CFG)
    assert job.pay_ok is None  # unknown → down-rank, not reject
    assert "unknown" in job.pay_basis
