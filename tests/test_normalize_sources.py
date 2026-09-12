"""Normalizer fixtures for the keyed Tier A sources (Adzuna, JSearch) — field
mapping + pay_raw construction feeding the pay gate."""
import pytest

from jobhound.config import CONFIG
from jobhound.enrich.pay import enrich_pay
from jobhound.enrich.pay_gate import apply_pay_gate
from jobhound.normalize import normalize
from jobhound.notify.digest import build_digest


def _enrich_and_gate(job):
    enrich_pay(job)
    apply_pay_gate(job, {}, CONFIG.pay)  # no platform → effective == nominal

ADZUNA_RAW = {
    "id": "5001",
    "title": "Data Annotation Specialist - <strong>AI Training</strong>",
    "description": "Work from home labelling data for AI models. Kolkata team.",
    "redirect_url": "https://www.adzuna.in/details/5001",
    "created": "2026-06-28T10:00:00Z",
    "company": {"display_name": "Acme Data Labs"},
    "location": {"display_name": "Kolkata, West Bengal"},
    "salary_min": 400000,
    "salary_max": 600000,
    "salary_is_predicted": "1",
}

JSEARCH_RAW = {
    "job_id": "abc123",
    "job_title": "Bengali AI Trainer",
    "employer_name": "TELUS Digital",
    "job_publisher": "LinkedIn",
    "job_apply_link": "https://linkedin.com/jobs/view/999",
    "job_description": "Evaluate LLM outputs as a fluent Bengali speaker. Remote.",
    "job_is_remote": True,
    "job_posted_at_datetime_utc": "2026-06-30T00:00:00Z",
    "job_city": None,
    "job_state": None,
    "job_country": "IN",
    "job_min_salary": 12,
    "job_max_salary": 20,
    "job_salary_currency": "USD",
    "job_salary_period": "HOUR",
}


def test_adzuna_mapping_and_pay():
    job = normalize({"source": "adzuna", "raw": ADZUNA_RAW})
    assert job.title == "Data Annotation Specialist - AI Training"  # html stripped
    assert job.company == "Acme Data Labs"
    assert job.url.endswith("/5001")
    assert job.is_remote          # "work from home" in description
    assert "india" in job.region_tags
    assert job.pay_raw == "INR 400000 - 600000 per year (predicted)"
    _enrich_and_gate(job)
    # 400–600k INR/yr → ~$2.2–3.3/hr → below the $4.50 floor: the sweatshop gate works
    assert job.pay_basis == "year"
    assert job.pay_ok is False


def test_adzuna_redirect_query_is_removed_before_canonical_storage():
    fake_app_id = "fake-adzuna-app-id"
    raw = {
        **ADZUNA_RAW,
        "redirect_url": (
            "https://www.adzuna.in/details/5001"
            f"?utm_medium=api&utm_source={fake_app_id}&app_key=fake-key#apply"
        ),
    }
    job = normalize({"source": "adzuna", "raw": raw})

    assert job.url == "https://www.adzuna.in/details/5001"
    assert fake_app_id not in job.url
    assert "fake-key" not in job.url
    digest = build_digest([job], top_n=1, per_company_cap=1)
    assert "https://www.adzuna.in/details/5001" in digest
    assert fake_app_id not in digest
    assert "fake-key" not in digest


def test_adzuna_without_salary_has_no_pay_raw():
    raw = {**ADZUNA_RAW, "salary_min": None, "salary_max": None}
    job = normalize({"source": "adzuna", "raw": raw})
    assert job.pay_raw is None
    _enrich_and_gate(job)
    assert job.pay_ok is None     # unknown pay → down-rank, never reject


def test_jsearch_mapping_and_pay():
    job = normalize({"source": "jsearch", "raw": JSEARCH_RAW})
    assert job.company == "TELUS Digital"
    assert job.is_remote is True
    assert job.location == "IN"
    assert "bengali" in job.region_tags   # title mention is role-defining
    assert job.pay_raw == "USD 12 - 20 per hour"
    _enrich_and_gate(job)
    assert job.pay_min_hourly_usd == pytest.approx(12.0)
    assert job.pay_max_hourly_usd == pytest.approx(20.0)
    assert job.pay_ok is True


def test_jsearch_missing_currency_uses_salary_string_not_usd():
    # v2 dropped job_salary_currency — an INR salary must NOT be read as USD.
    raw = {**JSEARCH_RAW, "job_salary_currency": None,
           "job_min_salary": 30000, "job_max_salary": 40000,
           "job_salary_period": "MONTH",
           "job_salary_string": "₹30,000 - ₹40,000 a month"}
    job = normalize({"source": "jsearch", "raw": raw})
    assert job.pay_raw == "₹30,000 - ₹40,000 a month"
    enrich_pay(job)
    assert job.pay_max_hourly_usd == pytest.approx(
        40000 * CONFIG.pay.fx_to_usd["INR"] / 173, abs=0.05)


def test_jsearch_falls_back_to_google_link():
    raw = {**JSEARCH_RAW, "job_apply_link": None,
           "job_google_link": "https://google.com/jobs/x"}
    job = normalize({"source": "jsearch", "raw": raw})
    assert job.url == "https://google.com/jobs/x"
