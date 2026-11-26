"""Provider contracts: preserve role sections, workplace meaning and pay units.

Synthetic inputs mirror the documented public ATS schemas. No live requests.
"""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from jobhound.config import CONFIG
from jobhound.normalize import normalize
from jobhound.v41.compensation import parse_compensation
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.evidence_dimensions import project_observation, work_arrangement
from jobhound.v41.models import ListingObservation


NOW = datetime(2026, 11, 25, 9, tzinfo=timezone.utc)
ROLE = """Work
Review Bengali and English AI responses using the supplied guidelines.
Compare response quality, record your reasons and report unclear examples.
Requirements
Bengali and English fluency required. Applicants in India may apply.
No prior AI-training experience or university degree required.
Compensation
USD 10 per working hour.
"""
FORM = """<form><h2>Application form</h2><select>
<option>Dutch fluency required.</option><option>Japanese native speaker required.</option>
</select></form>"""


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    monkeypatch.setattr(CONFIG.v55, "account_states", [])


def lever(**updates):
    raw = dict(id="native-role", _company="Synthetic Example",
               text="Bengali AI Response Evaluator",
               hostedUrl="https://jobs.lever.co/example/native-role",
               categories={"location": "India"}, country="IN",
               workplaceType="remote", descriptionPlain=ROLE,
               createdAt=NOW.isoformat())
    raw.update(updates)
    return {"source": "lever", "raw": raw}


def ashby(**updates):
    raw = dict(id="native-role", _company="Synthetic Example",
               title="Bengali AI Response Evaluator",
               jobUrl="https://jobs.ashbyhq.com/example/native-role",
               location="India", isRemote=True, workplaceType="Remote",
               descriptionPlain=ROLE, publishedAt=NOW.isoformat())
    raw.update(updates)
    return {"source": "ashby", "raw": raw}


def result(record):
    run = evaluate_raw([record], as_of=NOW)
    assert run.accounting_ok and len(run.evaluated) == 1
    return run, run.evaluated[0]


@pytest.mark.parametrize("workplace,flag,remote,state", [
    ("Remote", True, True, "remote"),
    ("remote", False, True, "remote"),
    ("Hybrid", True, False, "hybrid"),
    ("hybrid", False, False, "hybrid"),
    ("OnSite", False, False, "onsite"),
    ("OnSite", True, False, "conflicting"),
    (None, True, True, "unknown"),
    (None, False, False, "unknown"),
    (None, "false", False, "unknown"),
])
def test_ashby_workplace_is_not_inferred_from_broad_remote_flag(workplace, flag, remote, state):
    record = ashby(workplaceType=workplace, isRemote=flag)
    job = normalize(record)
    assert job.is_remote is remote
    evidence = work_arrangement(job, record["raw"])
    assert evidence.state == state
    if flag is True:
        assert any(c.source_field == "isRemote" and c.value == "remote_or_hybrid"
                   for c in evidence.claims)


def test_ashby_ambiguous_remote_evidence_is_lossless_and_idempotent():
    record = ashby(workplaceType=None)
    job = normalize(record)
    original = ListingObservation(observation_id="native", source="ashby",
        raw_payload=deepcopy(record["raw"]), job=job)
    before = original.model_dump_json()
    updated = project_observation(original)
    assert original.model_dump_json() == before
    assert updated.raw_payload == record["raw"]
    assert updated.work_arrangement.state == "unknown"
    assert any("hybrid" in c for c in updated.work_arrangement.caveats)
    assert project_observation(updated) == updated
    assert ListingObservation.model_validate_json(updated.model_dump_json()) == updated


def test_explicit_remote_language_can_resolve_ashby_ambiguous_flag():
    record = ashby(workplaceType=None, location="Remote, India")
    job = normalize(record)
    assert work_arrangement(job, record["raw"]).state == "remote"


@pytest.mark.parametrize("wording", [
    "This role is fully remote in India.",
    "Remote applicants in India may apply.",
    "Fully remote annotation work is available.",
    "Applicants may work from home.",
    "Remote India applicants may apply.",
    "Remote in India.",
    "Remote India.",
    "Remote worldwide.",
    "Location: remote in India.",
    "Buyer needs a fixed deliverable: a form-to-spreadsheet workflow, one revision, remote worldwide.",
])
def test_role_specific_remote_description_resolves_ashby_flag(wording):
    record = ashby(workplaceType=None, descriptionPlain=ROLE + wording)
    evidence = work_arrangement(normalize(record), record["raw"])
    assert evidence.state == "remote"
    assert any(c.source_field == "description" for c in evidence.claims)


@pytest.mark.parametrize("wording", [
    "Remote India applicants may apply.",
    "Remote in India.",
    "English required; remote India.",
    "Buyer needs a fixed deliverable: a form-to-spreadsheet workflow, one revision, remote worldwide.",
])
def test_short_explicit_remote_terms_preserve_native_application_readiness(wording):
    _, item = result(ashby(workplaceType=None, descriptionPlain=ROLE + wording))
    assert item.canonical.best_observation.work_arrangement.state == "remote"
    assert item.decision.next_action == "apply"
    assert "location_scope_unknown:remote_or_hybrid" not in item.assessment.unresolved


@pytest.mark.parametrize("wording", [
    "Our company has a fully remote team.",
    "This role is not fully remote.",
    "We are not recruiting for a fully remote role.",
    'Review examples such as "This role is remote" for annotation quality.',
    "No remote applicants may apply.",
    "Fully remote applicants are not accepted.",
    "Fully remote work is not offered.",
    "Our company is remote worldwide.",
    "Our team is remote in India.",
    "Our company offers many projects, remote worldwide.",
    "This role is not remote in India.",
    "Remote in India is not available.",
    "Remote India applicants cannot apply.",
    "No remote India applicants may apply.",
    "Remote worldwide is our company culture.",
    "This role was previously offered, remote worldwide.",
    "This role might become available, remote worldwide.",
    'Review examples such as "Remote India" for annotation quality.',
])
def test_generic_or_negated_remote_prose_does_not_resolve_ashby_flag(wording):
    record = ashby(workplaceType=None, descriptionPlain=ROLE + wording)
    assert work_arrangement(normalize(record), record["raw"]).state == "unknown"


def test_ashby_hybrid_never_becomes_a_fully_remote_application():
    _, item = result(ashby(workplaceType="Hybrid"))
    assert not item.job.is_remote
    assert item.canonical.best_observation.work_arrangement.state == "hybrid"
    assert not any(c.value == "remote" for c in item.canonical.best_observation.work_arrangement.claims)
    assert item.decision.next_action != "apply"


@pytest.mark.parametrize("section", ["lists", "additionalPlain", "additional"])
def test_lever_genuine_requirements_in_every_native_section_are_enforced(section):
    if section == "lists":
        value = [{"text": "Requirements", "content": "<li>Dutch fluency required.</li>"}]
    elif section == "additional":
        value = "<p>Dutch fluency required.</p>"
    else:
        value = "Dutch fluency required."
    record = lever(**{section: value})
    before = deepcopy(record)
    run, item = result(record)
    assert "Dutch fluency required." in item.job.description
    assert any(b.endswith("lang_mismatch:dutch") for b in item.assessment.blockers)
    assert item.decision.next_action != "apply"
    assert record == before
    assert run.observations[0].raw_payload[section] == value


@pytest.mark.parametrize("section", ["lists", "additional", "salaryDescription"])
def test_lever_form_options_never_become_native_requirements(section):
    value = [{"text": "Requirements", "content": FORM}] if section == "lists" else FORM
    _, baseline = result(lever())
    _, item = result(lever(**{section: value}))
    assert "Dutch" not in item.job.description and "Japanese" not in item.job.description
    assert item.assessment.blockers == baseline.assessment.blockers


def test_lever_matching_additional_plain_uses_native_html_form_boundary():
    from jobhound.text import html_to_text
    extra = "<p>Keep project material confidential.</p>" + FORM
    _, item = result(lever(additional=extra, additionalPlain=html_to_text(extra)))
    assert "Keep project material confidential." in item.job.description
    assert "Dutch" not in item.job.description


def test_lever_mismatched_html_cannot_erase_additional_plain_requirements():
    _, item = result(lever(additional="<p>General company information.</p>",
                           additionalPlain="Dutch fluency required."))
    assert any(b.endswith("lang_mismatch:dutch") for b in item.assessment.blockers)


def test_lever_uncertain_extra_form_boundary_does_not_claim_complete_role():
    record = lever(lists=[{"text": "Requirements", "content": "<form><p>Dutch fluency required."}])
    run = evaluate_raw([record], as_of=NOW)
    assert run.accounting_ok and not run.evaluated
    assert run.observations[0].normalization_error
    assert run.observations[0].raw_payload["lists"] == record["raw"]["lists"]


def test_lever_opening_body_fallback_retains_named_sections_once():
    record = lever(descriptionPlain=None, description=None,
                   openingPlain="Welcome to the team.", descriptionBodyPlain=ROLE,
                   lists=[{"text": "Benefits", "content": "<li>Flexible schedule.</li>"}],
                   additionalPlain="Contractor terms apply.")
    text = normalize(record).description
    assert text.count("Welcome to the team.") == 1
    assert text.count("Review Bengali and English") == 1
    assert "Benefits" in text and "• Flexible schedule." in text
    assert "Contractor terms apply." in text


def test_lever_alternate_india_location_is_preserved_with_us_primary():
    record = lever(country="US", categories={
        "location": "New York, United States", "allLocations": ["New York, United States", "India"]})
    job = normalize(record)
    assert job.location == "New York, United States, India, US"
    assert "india" in job.region_tags
    _, item = result(record)
    assert not any("geo_mismatch" in b or "location:india_excluded" in b for b in item.assessment.blockers)


@pytest.mark.parametrize("currency,interval,expected", [
    ("USD", "per-hour-wage", "USD 8 - 14 per hour"),
    ("INR", "per-month-wage", "INR 8 - 14 per month"),
    ("EUR", "per-year-salary", "EUR 8 - 14 per year"),
    ("USD", "per-week-wage", "USD 8 - 14 per week"),
    ("USD", "per-day-wage", "USD 8 - 14 per day"),
    ("USD", None, "USD 8 - 14"),
    ("USD", "per-task", "USD 8 - 14"),
    ("USD", "biweekly", "USD 8 - 14"),
    (None, "per-hour-wage", None),
    ("", "per-hour-wage", None),
])
def test_lever_salary_only_uses_stated_currency_and_supported_interval(currency, interval, expected):
    job = normalize(lever(salaryRange={"min": 8, "max": 14, "currency": currency, "interval": interval}))
    assert job.pay_raw == expected
    if expected and interval in {None, "per-task", "biweekly"}:
        claim = parse_compensation(job.pay_raw)
        assert claim.basis == "unknown" and claim.labor_hour_equivalent is None


@pytest.mark.parametrize("bounds,expected", [
    ({"min": 8}, "at least USD 8 per hour"),
    ({"max": 14}, "up to USD 14 per hour"),
    ({"min": 0, "max": 14}, "USD 0 - 14 per hour"),
    ({"min": 15, "max": 14}, None),
    ({"min": -1, "max": 14}, None),
    ({"min": float("inf"), "max": 14}, None),
    ({"min": True, "max": 14}, None),
])
def test_lever_salary_bounds_remain_literal(bounds, expected):
    salary = {"currency": "USD", "interval": "per-hour-wage", **bounds}
    job = normalize(lever(salaryRange=salary))
    assert job.pay_raw == expected
    if expected and "max" not in bounds:
        assert parse_compensation(expected).amount_low == 8
        assert parse_compensation(expected).qualifier != "up_to"
    if expected and "min" not in bounds:
        assert parse_compensation(expected).guaranteed_minimum is None


def test_lever_salary_description_survives_missing_structured_currency():
    record = lever(salaryRange={"min": 20, "max": 30, "interval": "per-hour-wage"},
                   salaryDescriptionPlain="Pay: EUR 20 - 30 per working hour.")
    job = normalize(record)
    assert job.pay_raw is None
    assert "EUR 20 - 30 per working hour." in job.description
    assert record["raw"]["salaryRange"]["min"] == 20


@pytest.mark.parametrize("release_enabled", [True, False])
@pytest.mark.parametrize("interval", ["one-time", "per-day-wage", "per-week-wage", "biweekly", None, "per-hour-wage"])
def test_lever_pay_floor_requires_a_convertible_period(monkeypatch, release_enabled, interval):
    from jobhound.enrich.pay import enrich_pay
    from jobhound.enrich.pay_gate import apply_pay_gate

    monkeypatch.setattr(CONFIG.v55, "enabled", release_enabled)
    record = lever(descriptionPlain=ROLE.partition("Compensation")[0],
                   salaryRange={"min": 150, "currency": "USD", "interval": interval})
    legacy = enrich_pay(normalize(record))
    apply_pay_gate(legacy, {}, CONFIG.pay)
    _, item = result(record)
    expected = 150 if interval == "per-hour-wage" else None
    assert legacy.pay_max_hourly_usd == item.job.pay_max_hourly_usd == expected
    assert legacy.pay_ok is item.job.pay_ok is (True if expected else None)
    if interval == "one-time":
        assert "one-time" in normalize(record).pay_raw


@pytest.mark.parametrize("published,expected", [
    ("2026-10-01T08:00:00Z", datetime(2026, 10, 1, 8, tzinfo=timezone.utc)),
    (None, None),
    ("", None),
    ("unknown", None),
])
def test_greenhouse_edit_time_is_never_original_publication(published, expected):
    from jobhound.v41.provenance import build_observations
    raw = dict(id=123, title="Bengali AI Response Evaluator", _company="Synthetic Example",
               absolute_url="https://boards.greenhouse.io/example/jobs/123",
               content=ROLE, location={"name": "Remote, India"},
               updated_at=NOW.isoformat(), first_published=published)
    record = {"source": "greenhouse", "raw": raw}
    before = deepcopy(record)
    observations, failures = build_observations([record])
    assert not failures and len(observations) == 1
    assert observations[0].job.posted_at == expected
    assert observations[0].raw_payload["updated_at"] == NOW.isoformat()
    assert record == before
