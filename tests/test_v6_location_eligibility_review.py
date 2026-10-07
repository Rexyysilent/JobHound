"""Location-eligibility review of 13fe4db (2026-10-03).

Four ways a role open to applicants in India was rejected for location:
1. An employer statement ("Our company is based in the United States") was
   read as an applicant residency requirement, by both the legacy region gate
   and the scoped extractor.
2. A weaker discovery copy's location blocked a role whose verified original
   accepts applicants worldwide (weak_copy was ignored for geography).
3. The pronoun "us" ("sharing feedback with us") was read as United States.
4. Hydration kept a JobPosting's office address and dropped its
   applicantLocationRequirements.
Controls keep real applicant restrictions rejected.
"""
from __future__ import annotations

from datetime import datetime, timezone

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.models import RequirementClaim, RequirementsAssessment

NOW = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)
BASE = ("<h2>Work</h2><p>Review Bengali and English AI responses using supplied guidelines.</p>"
        "<h2>Requirements</h2><ul><li>Bengali and English fluency required.</li>"
        "<li>No prior experience or degree required.</li></ul>"
        "<p>Remote applicants worldwide may apply.</p><p>Pay: USD 10 per hour.</p>")


def _row(monkeypatch, content, location="Remote"):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    raw = [{"source": "greenhouse", "raw": {
        "id": 1, "title": "Bengali AI Response Evaluator", "_company": "Acme",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": location},
        "content": content, "updated_at": "2026-10-02T09:00:00Z"}}]
    return evaluate_raw(raw, as_of=NOW).evaluated[0]


def _location_blocked(row):
    return [b for b in row.assessment.blockers if b.startswith("location:")]


def test_baseline_worldwide_role_is_accepted(monkeypatch):
    assert not _location_blocked(_row(monkeypatch, BASE))


# 1. employer location ------------------------------------------------------

def test_employer_headquarters_is_not_an_applicant_restriction(monkeypatch):
    for prefix in ("<p>Our company is based in the United States.</p>",
                   "<p>We are a startup located in the US.</p>",
                   "<p>Acme is headquartered in Germany and based in Berlin.</p>"):
        row = _row(monkeypatch, prefix + BASE)
        assert not _location_blocked(row), (prefix, row.assessment.blockers)


def test_legacy_region_gate_ignores_employer_location():
    from jobhound.filters.listing_quality import region_lock_check
    assert region_lock_check("Evaluator", "Our company is based in the United States.") == []
    assert region_lock_check("Evaluator", "Candidates must be based in the US.")


def test_applicant_residency_is_still_a_restriction(monkeypatch):
    row = _row(monkeypatch, BASE.replace("Remote applicants worldwide may apply.",
                                         "Applicants must reside in the United States."))
    assert _location_blocked(row)


def test_role_location_and_required_headquarters_work_still_restrict(monkeypatch):
    for line in ('This role is based in the United States.',
                 'Must work at our headquarters in the United States.'):
        row = _row(monkeypatch, BASE.replace('Remote applicants worldwide may apply.', line))
        assert _location_blocked(row), line
    row = _row(monkeypatch, BASE.replace("Remote applicants worldwide may apply.",
                                         "You must be based in the United States."))
    assert _location_blocked(row)


# 2. weak copies --------------------------------------------------------------

def _claim(status, polarity="positive", value=("united_states",)):
    return RequirementClaim(dimension="location", value=list(value), modality="unknown",
                            observation_id="copy", source_field="location", span="United States",
                            actor="applicant", predicate="resides_in", polarity=polarity,
                            evidence_status=status)


def test_weak_copy_geography_does_not_block_a_verified_original():
    from jobhound.v41.requirements import location_mismatches
    assert location_mismatches(RequirementsAssessment(location_scope=[_claim("weak_copy")])) == []
    assert location_mismatches(RequirementsAssessment(
        location_scope=[_claim("weak_copy", "negative", ("india",))])) == []
    assert location_mismatches(RequirementsAssessment(location_scope=[_claim("supported")])) == [
        "location_scope_mismatch:united_states"]


# 3. the pronoun "us" -----------------------------------------------------------

def test_pronoun_us_is_not_the_united_states(monkeypatch):
    for line in ("You must be comfortable sharing feedback with us.",
                 "You must be ready to work with us on weekends.",
                 "Only shortlisted candidates will hear from us."):
        row = _row(monkeypatch, BASE + f"<p>{line}</p>")
        assert not _location_blocked(row), (line, row.assessment.blockers)


def test_us_in_geographic_context_still_restricts(monkeypatch):
    for line in ("Remote within the US only.", "Applicants in the US only.", "Must be US-based."):
        row = _row(monkeypatch, BASE.replace("Remote applicants worldwide may apply.", line))
        assert _location_blocked(row), line


# 4. JobPosting applicant location -----------------------------------------------

def _posting(**extra):
    return {"@type": "JobPosting", "title": "Bengali AI Response Evaluator",
            "description": "Review Bengali and English AI responses. Bengali fluency required.",
            "hiringOrganization": {"name": "Acme"},
            "jobLocation": {"@type": "Place", "address": {
                "addressLocality": "Austin", "addressCountry": "United States"}}, **extra}


def test_remote_posting_uses_applicant_location_requirements():
    from jobhound.filters.listing_quality import location_gate
    from jobhound.v41.hydration import _generic_job
    for requirement in ({"@type": "Country", "name": "India"},
                        [{"@type": "Country", "name": "India"}, {"@type": "Country", "name": "Nepal"}]):
        job = _generic_job(_posting(jobLocationType="TELECOMMUTE",
                                    applicantLocationRequirements=requirement),
                           "https://acme.example/jobs/1", "")
        assert "India" in job.location and "United States" not in job.location, job.location
        assert job.is_remote
        assert location_gate(job.is_remote, job.region_tags, job.location, job.description, job.title) == []


def test_onsite_posting_keeps_its_physical_location():
    from jobhound.v41.hydration import _generic_job
    job = _generic_job(_posting(applicantLocationRequirements={"@type": "Country", "name": "India"}),
                       "https://acme.example/jobs/1", "")
    assert "United States" in job.location
    plain = _generic_job(_posting(), "https://acme.example/jobs/1", "")
    assert plain.location == "Austin, United States"
