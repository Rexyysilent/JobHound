"""Synthetic controls for failure shapes seen in November digest reviews.

These are regression cases, not independently labelled precision estimates.
No mailbox content, personal profile or production identifiers are embedded.
"""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from jobhound.config import CONFIG
from jobhound.filters.eligibility import default_profile
from jobhound.filters.listing_quality import junk_check, location_gate
from jobhound.v41.documents import classify_document
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.roles import classify_role_semantics


NOW = datetime(2026, 11, 25, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)


def listing(title, snippet, url="https://board.example/roles/one"):
    return {"source": "serp", "raw": {
        "title": title, "snippet": snippet, "link": url,
    }}


@pytest.mark.parametrize("title", [
    "Bengali AI Training Jobs: 28 Open Roles Paying $12-$65/hr",
    "Python Automation Jobs — 104 Available Positions",
    "Hindi AI Jobs | 1,200+ Openings",
])
def test_counted_job_directories_cannot_be_a_vacancy_or_headline(title):
    url = "https://jobs.example/ai-training-jobs/bengali"
    document = classify_document(title, "Explore current remote work.", url)
    assert document.document_type == "job_index"
    assert not document.actionable
    assert any("index" in reason or "search_page" in reason
               for reason in junk_check(title, url, "Example", True))
    item = evaluate_raw([listing(title, "Bengali AI training, USD 12-65 per hour.", url)],
                        as_of=NOW).evaluated[0]
    assert item.decision.action_band.value == "reject"
    assert item.decision.terminal_reason == "reject_source_quality"


@pytest.mark.parametrize("title", [
    "Bengali AI Evaluator — 3 openings",
    "Python Developer — Job ID 28",
    "Jobs Platform Developer",
])
def test_real_role_titles_with_counts_or_jobs_are_preserved(title):
    document = classify_document(title, "Responsibilities: Review AI examples. Apply here.",
                                 "https://jobs.example/roles/28")
    assert document.document_type == "individual_job"
    assert document.actionable


@pytest.mark.parametrize("title,location", [
    ("Junior AI-Automation Specialist (m/w/d) – vor Ort in Dortmund",
     "Dortmund, Nordrhein-Westfalen, Deutschland"),
    ("Python Automation Developer", "Deutschland (Remote)"),
    ("AI Evaluator — vor Ort", "India"),
])
def test_explicit_german_location_and_attendance_outrank_remote_flag(title, location):
    reasons = location_gate(True, ["worldwide"], location, "Use n8n and Python.", title)
    assert any(reason.startswith("rejected_location:") for reason in reasons)


@pytest.mark.parametrize("title", [
    "AI Evaluator — remote, nicht vor Ort",
    "AI Evaluator — vor Ort optional",
    "AI Evaluator — vor Ort oder remote",
    "AI Evaluator — Remote oder vor Ort",
    "AI Evaluator — keine Arbeit vor Ort",
    "AI Evaluator — remote, vor Ort nicht verpflichtend",
    "AI Evaluator — remote, vor Ort auf Wunsch",
    "AI Evaluator — Remote weltweit, keine Präsenz vor Ort erforderlich",
])
def test_negated_optional_and_alternative_local_attendance_do_not_block(title):
    assert location_gate(True, ["india"], "India", "", title) == []


def test_dortmund_discovery_is_not_a_headline_even_with_automation_fit():
    row = evaluate_raw([listing(
        "Junior AI-Automation Specialist (m/w/d) – vor Ort in Dortmund",
        "Build Python and n8n workflow automation. Location: Dortmund, Deutschland.",
    )], as_of=NOW).evaluated[0]
    assert row.decision.terminal_reason == "reject_location"


@pytest.mark.parametrize("title", [
    "Physician Reviewer (MD/DO) - Medical AI Evaluation",
    "Licensed Physician - AI Trainer",
    "Medical Doctor - AI Response Evaluator",
])
def test_explicit_physician_roles_are_outside_generalist_target_scope(title):
    item = evaluate_raw([listing(title, "Review AI responses and provide medical feedback.")],
                        as_of=NOW).evaluated[0]
    assert item.decision.action_band.value == "reject"
    assert "outside_target_scope" in item.assessment.unresolved
    # A target-lane mismatch is not a declaration that a licence is absent.
    assert not any("medical_licence" in reason for reason in item.assessment.blockers)


def test_medical_profile_retains_physician_role_without_inventing_a_licence():
    profile = replace(default_profile(), credential_domains={"medicine"},
                      capability_domains={"medicine"}, formal_credentials=set())
    role = classify_role_semantics("Physician Reviewer (MD/DO) - Medical AI Evaluation",
                                   "Review AI answers.", "", profile)
    assert role.subject == "medical"
    assert role.status == "claimed"


@pytest.mark.parametrize("title", [
    "Medical Image Annotator",
    "Python Developer for Physician Scheduling",
    "Physician Support Coordinator",
    "Physician Office Assistant - AI Training",
    "Physician Practice Data Entry - AI Training",
    "Physician Administrative Assistant - AI Training",
    "Physician Recruitment Coordinator - AI Training",
    "Lawyer Office Assistant - AI Training",
    "Attorney Recruiting Assistant - AI Training",
])
def test_discussed_medical_subject_is_not_a_physician_occupation(title):
    role = classify_role_semantics(title, "Label examples using provided guidelines.", "",
                                   default_profile())
    assert role.status != "unclaimed"


@pytest.mark.parametrize("url", [
    "https://www.facebook.com/example/posts/123",
    "https://www.instagram.com/p/ExamplePost/",
    "https://x.com/example/status/123",
])
def test_personal_social_updates_are_not_individual_jobs(url):
    page = classify_document("My experience with AI training and annotation",
                             "I recently started reviewing AI responses on a freelance platform.",
                             url)
    assert not page.actionable
    assert page.document_type == "discussion"


def test_social_job_seeker_is_not_employer_demand():
    page = classify_document("Bengali AI evaluator open to work",
                             "I am looking for a remote job as an AI evaluator.",
                             "https://www.facebook.com/example/posts/123")
    assert not page.actionable


@pytest.mark.parametrize("body", [
    "We are hiring Bengali AI evaluators. Requirements: fluent Bengali. Apply through our exact role page.",
    "Looking for a developer to build an n8n automation. Fixed project budget USD 300.",
    "Responsibilities: Review responses. Requirements: Bengali fluency. Compensation: USD 10/hr. "
    "Apply now through https://jobs.example/roles/123.",
    "Responsibilities: Review responses. Requirements: Bengali fluency. Pay: USD 12/hr. "
    "Apply: https://jobs.ashbyhq.com/example/role-123.",
])
def test_explicit_social_recruitment_or_buyer_request_is_retained(body):
    page = classify_document("Bengali AI and workflow automation opportunity", body,
                             "https://www.facebook.com/example/posts/123")
    assert page.actionable
    assert page.document_type in {"individual_job", "buyer_request"}


def test_recruiters_addressing_job_seekers_are_not_job_seekers_themselves():
    page = classify_document("Hiring Bengali AI Evaluators",
                             "We are hiring Bengali evaluators. Are you open to work? Apply here.",
                             "https://www.facebook.com/example/posts/123")
    assert page.actionable


def test_social_recruiter_can_quote_a_job_seeker_without_becoming_one():
    page = classify_document("Hiring Bengali AI Evaluators",
                             'If you say "I am looking for a remote job", check our role. '
                             "Requirements: Bengali fluency. Apply here.",
                             "https://www.facebook.com/example/posts/123")
    assert page.actionable


def test_needed_role_title_is_explicit_social_recruitment():
    page = classify_document("Bengali annotators needed",
                             "Remote from India. Submit your CV to our role portal: "
                             "https://jobs.example/roles/123.",
                             "https://www.facebook.com/example/posts/123")
    assert page.actionable


def test_quoted_recruitment_is_not_the_social_authors_demand():
    page = classify_document("My AI work experience",
                             'Yesterday I saw "Bengali annotators needed" on a notice. '
                             "I wonder what that work is like.",
                             "https://www.facebook.com/example/posts/123")
    assert not page.actionable


def test_social_talent_pool_invitation_preserves_watch_lifecycle():
    title = "Bengali AI Evaluators - Talent Pool"
    body = ("We are hiring Bengali AI evaluators for our talent pool. "
            "Join our network for future opportunities. No projects are currently allocated.")
    url = "https://www.facebook.com/example/posts/123"
    page = classify_document(title, body, url)
    assert page.document_type == "talent_pool"
    assert not page.actionable
    item = evaluate_raw([listing(title, body, url)], as_of=NOW).evaluated[0]
    assert item.decision.lifecycle == "watch"
    assert item.decision.next_action != "apply"


@pytest.mark.parametrize("title", [
    "Legal Research Attorney - AI Training",
    "Licensed Lawyer - AI Response Evaluator",
])
def test_attorney_occupations_are_not_generalist_ai_tasks(title):
    item = evaluate_raw([listing(title, "Review AI responses and provide legal feedback.")],
                        as_of=NOW).evaluated[0]
    assert item.decision.terminal_reason == "reject_role"
    assert "outside_target_scope" in item.assessment.unresolved
    legal = replace(default_profile(), credential_domains={"law"}, capability_domains={"law"})
    assert classify_role_semantics(title, "", "", legal).status == "claimed"


@pytest.mark.parametrize("title", [
    "Mgr, Quality/Training Strategy, AI Operations Integration",
    "Program Mgr. - AI Response Evaluation",
])
def test_management_abbreviations_do_not_escape_seniority_gate(title):
    item = evaluate_raw([listing(title, "Lead AI training and annotation operations.")],
                        as_of=NOW).evaluated[0]
    assert item.decision.terminal_reason == "reject_seniority"
    assert "seniority_mismatch:management" in item.assessment.blockers


def test_language_locale_india_cannot_overrule_explicit_us_applicant_location():
    item = evaluate_raw([{"source": "greenhouse", "raw": {
        "id": 77, "title": "AI Linguistic Evaluator — Bengali (India)",
        "_company": "Example", "absolute_url": "https://boards.greenhouse.io/example/jobs/77",
        "location": {"name": "United States (Remote)"},
        "content": "Requirements: Bengali and English fluency. Review AI responses.",
    }}], as_of=NOW).evaluated[0]
    assert item.decision.terminal_reason == "reject_location"
