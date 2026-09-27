"""Retained challenge failures and positive controls; no live providers."""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.digest import build_digest
from jobhound.v41.compensation import parse_compensation
from jobhound.filters.listing_quality import region_lock_check

CASES = json.loads((Path(__file__).parent / "fixtures/v6_semantic_challenges.json").read_text())["cases"]
NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)


def evaluate(case):
    description = case.get("description",
        "Review Bengali AI responses using supplied guidelines. Bengali fluency required. "
        "Remote applicants in India may apply. No degree or prior experience required.\n" + case.get("quote", ""))
    result = evaluate_raw([{"source": "ashby", "raw": {
        "id": case.get("id", "control"), "_company": "Fixture Independent",
        "title": case.get("title", "Bengali AI Response Evaluator"),
        "jobUrl": case.get("url", "https://jobs.ashbyhq.com/fixture/control"),
        "location": "India", "isRemote": True, "descriptionPlain": description,
    }}], as_of=NOW)
    assert result.accounting_ok and build_digest(result, include_all=True).accounting_ok
    return result.evaluated[0]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_retained_challenge(case):
    item = evaluate(case)
    pay = item.assessment.selected_pay
    for key, value in case["expect"].items():
        actual = {
            "unit": lambda: bool(pay and pay.actual_unit == value),
            "min": lambda: bool(pay and pay.amount_low == value),
            "not_min": lambda: not pay or pay.amount_low != value,
            "no_selected_pay": lambda: pay is None,
            "not_hourly": lambda: not pay or not pay.labor_hourly_supported,
            "no_usd": lambda: not pay or pay.min_hourly_usd is None,
            "no_guaranteed_hours": lambda: not item.job.guaranteed_hours,
            "not_primary": lambda: item.decision.action_band.value != "primary",
            "not_rejected": lambda: item.decision.action_band.value != "reject",
            "rejected": lambda: item.decision.action_band.value == "reject",
        }[key]()
        assert actual, (case["id"], key, item.decision, pay)
    for claim in item.assessment.pay_candidates:
        source = item.job.title if claim.source_field == "title" else item.job.description
        assert source[claim.source_span_start:claim.source_span_end] == claim.raw
        assert hashlib.sha256(source.encode()).hexdigest() == claim.source_text_sha256


@pytest.mark.parametrize("quote,amount", [
    ("We pay applicants USD 18 per hour.", 18),
    ("You will be paid USD 18 per hour to review training examples.", 18),
    ("Applicants pay USD 45 per hour for coaching. This role pays USD 18 per hour.", 18),
    ("Supervisors receive USD 55 per hour. This evaluator role pays USD 12 per hour.", 12),
    ("A previous worker earned USD 80 per hour at another company. We pay USD 18 per hour.", 18),
])
def test_positive_pay_survives_nonpay_context(quote, amount):
    item = evaluate({"quote": quote})
    assert item.assessment.selected_pay.amount_low == amount
    assert item.assessment.selected_pay.labor_hourly_supported


def test_supervisor_pay_not_discarded_for_actual_supervisor():
    item = evaluate({"title": "Bengali AI Review Supervisor", "quote": "Supervisors receive USD 55 per hour."})
    assert item.assessment.selected_pay.amount_low == 55


@pytest.mark.parametrize("qualifier", ["preferred", "optional", "not required", "not mandatory"])
def test_optional_language_is_not_a_blocker(qualifier):
    item = evaluate({"description": "Review Bengali AI responses. Bengali fluency required. "
        f"German fluency is {qualifier}. Remote applicants in India welcome. USD 18 per hour."})
    assert not any("lang_mismatch:german" in reason for reason in item.assessment.blockers)


@pytest.mark.parametrize("quote,basis", [
    ("We pay USD 18 per hour for equivalent experience.", "labor_hour"),
    ("Fixed budget USD 350.", "fixed_project"),
    ("Fixed budget USD 350 per hour.", "unknown"),
    ("An equivalent of USD 45 per hour.", "labor_hour_equivalent"),
])
def test_rate_qualifier_controls(quote, basis):
    assert parse_compensation(quote).basis == basis


@pytest.mark.parametrize("text,blocked", [
    ("UK residents are not eligible.", False),
    ("UK residents only.", True),
    ("Not based in UK.", False),
    ("UK residents are not eligible. Canada residents only.", True),
])
def test_location_polarity_is_local(text, blocked):
    assert bool(region_lock_check("Reviewer", text, polarity_aware=True)) == blocked


def test_legacy_country_lock_contract_unchanged():
    assert region_lock_check("Reviewer", "UK residents are not eligible.") == ["region_locked:uk"]


def test_india_exclusion_still_blocks():
    item = evaluate({"description": "Remote worldwide. India residents are not eligible. "
        "Review Bengali AI responses. USD 18 per hour."})
    assert item.decision.action_band.value == "reject"
