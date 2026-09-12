from datetime import datetime, timezone

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.compensation import parse_compensation
from jobhound.v41.documents import classify_document
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.extract import extract_pay_candidates
from jobhound.v41.models import Assessment, CanonicalJob, EligibilityStatus, ListingObservation, SourceKind
from jobhound.v41.action_policy import apply_action_policy


def _canonical(title: str, description: str, url: str, *, location: str | None = None):
    job = Job(source="serp", title=title, company="Upwork Client", url=url,
              description=description, location=location, is_remote=True)
    observation = ListingObservation(
        observation_id="live", source="serp", job=job,
        source_kind=SourceKind.AGGREGATOR_UNRESOLVED,
    )
    return CanonicalJob(canonical_id="live", job=job, observations=[observation],
                        field_sources={"url": "live", "description": "live"})


def test_live_non_opportunity_routes_and_real_buyer_controls():
    cases = [
        ("Automate Your Upwork Job Hunt With This n8n AI Agent - YouTube", "Video tutorial", "https://www.youtube.com/watch?v=x", "article"),
        ("Best Freelance N8N Experts for Hire", "Production automation experts", "https://www.upwork.com/hire/n8n-experts/", "talent_directory"),
        ("You will get a n8n automation expert", "Starter $200", "https://www.upwork.com/services/product/development-it-an-n8n-package-123", "seller_service"),
        ("I made thousands on Upwork", "Now I have lost it completely", "https://www.reddit.com/r/n8n/comments/abc/story/", "discussion"),
    ]
    for title, text, url, expected in cases:
        result = classify_document(title, text, url)
        assert (result.document_type, result.actionable) == (expected, False)

    upwork = classify_document("Twilio + WhatsApp API freelancer", "Need a developer", "https://www.upwork.com/freelance-jobs/apply/twilio_~01/")
    community = classify_document("Looking for a freelancer", "Looking to hire a freelancer to build a paid WhatsApp workflow", "https://community.make.com/t/request/1")
    assert (upwork.document_type, upwork.actionable) == ("buyer_request", True)
    assert (community.document_type, community.actionable) == ("buyer_request", True)


def test_reddit_explicit_recruitment_survives_but_retrospective_does_not():
    recruitment = classify_document(
        "Outlier.ai is actively hiring native Hindi/Bangla speakers for audio tasks",
        "Audio tasks (Up to $15+/hr). Expected income varies with available work.",
        "https://www.reddit.com/r/beermoneyindia/comments/abc/outlier_hiring/",
    )
    narrative = classify_document(
        "I made thousands on Upwork thanks to N8N now lost completely",
        "I know automation but I am not getting traction. Why?",
        "https://www.reddit.com/r/n8n/comments/xyz/my_story/",
    )
    assert (recruitment.document_type, recruitment.actionable) == ("individual_job", True)
    assert (narrative.document_type, narrative.actionable) == ("discussion", False)


def test_non_opportunity_route_is_rejected_before_verify(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    result = evaluate_observations([_canonical(
        "Automate Your Upwork Job Hunt With This n8n AI Agent - YouTube", "Video tutorial",
        "https://www.youtube.com/watch?v=x",
    ).observations[0]], as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert result.evaluated[0].decision.terminal_reason == "reject_source_quality"


def test_publisher_hourly_equivalent_keeps_full_context_but_is_not_wage(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    raw = "up to $10 per hour equivalent depending on the project"
    claim = parse_compensation(raw)
    assert (claim.basis, claim.qualifier, claim.labor_equivalent_is_estimate) == (
        "labor_hour_equivalent", "up_to_equivalent", True,
    )
    canonical = _canonical("Record Your Daily Routine & Get Paid", f"Earn while doing chores: {raw}.", "https://board.example/jobs/1")
    _, selected, _ = extract_pay_candidates(canonical)
    assert selected is not None
    assert selected.raw == raw
    assert selected.actual_unit == "labor_hour_equivalent"
    assert selected.amount_low == selected.amount_high == 10
    assert selected.min_hourly_usd is None and selected.max_hourly_usd is None
    assert not selected.labor_hourly_supported and selected.estimated


def test_upwork_serp_hourly_and_fixed_budget_forms(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    captured = "Twilio + WhatsApp API freelancer · Less than 30 hrs/week. Hourly · < 1 month. Duration · Intermediate. Experience Level · $15.00. -. $35.00. Hourly."
    for snippet in (captured, "Less than 30 hrs/week. $15.00 .-. $35.00 .Hourly."):
        hourly = _canonical("Twilio + WhatsApp API freelancer", snippet, "https://www.upwork.com/freelance-jobs/apply/twilio_~01/")
        _, selected_hourly, _ = extract_pay_candidates(hourly)
        assert selected_hourly is not None
        assert (selected_hourly.amount_low, selected_hourly.amount_high, selected_hourly.actual_unit) == (15, 35, "labor_hour")
        assert selected_hourly.raw in snippet

    fixed = _canonical("AI Automation Developer", "Freelance Job in AI Apps & Integration - $350.00. WhatsApp API is an advantage.", "https://www.upwork.com/freelance-jobs/apply/automation_~02/")
    _, selected_fixed, _ = extract_pay_candidates(fixed)
    assert selected_fixed is not None
    assert (selected_fixed.fixed_amount_usd, selected_fixed.actual_unit) == (350, "fixed_project")


def test_geography_conflict_stays_visible_and_explicit_remote_us_rejects(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    conflict = _canonical(
        "Record Your Daily Routine & Get Paid - AI Training (Remote)", "Flexible remote project.",
        "https://www.careerbuilder.com/job-details/role-remote-orlando-fl--abc", location="Florida, IN",
    )
    result = evaluate_observations(conflict.observations, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc)).evaluated[0]
    assert "location_scope_conflict:metadata_country_vs_url" in result.assessment.unresolved
    assert any(task["missing_fact"] == "location_scope_conflict:metadata_country_vs_url" for task in result.decision.verification_tasks)
    assert result.decision.verification_tasks[0]["missing_fact"] == "location_scope_conflict:metadata_country_vs_url"
    policy_assessment = Assessment(
        eligibility=EligibilityStatus.PASSED,
        lifecycle="active",
        unresolved=["location_scope_conflict:metadata_country_vs_url"],
    )
    apply_action_policy(conflict, policy_assessment, datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert policy_assessment.verification_tasks[0]["missing_fact"] == "location_scope_conflict:metadata_country_vs_url"
    assert policy_assessment.next_step == policy_assessment.verification_tasks[0]["next_step"]

    blocked = _canonical("AI Safety Policy Evaluator, Violence & Threats Remote US", "Applicants work remotely.", "https://board.example/jobs/us")
    rejected = evaluate_observations(blocked.observations, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc)).evaluated[0]
    assert any(blocker.startswith("location:") for blocker in rejected.assessment.blockers)


def test_metadata_country_url_reconciliation_is_v55_only(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", False)
    conflict = _canonical(
        "Record Your Daily Routine & Get Paid - AI Training (Remote)", "Flexible remote project.",
        "https://www.careerbuilder.com/job-details/role-remote-orlando-fl--abc", location="Florida, IN",
    )
    result = evaluate_observations(conflict.observations, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc)).evaluated[0]
    assert "location_scope_conflict:metadata_country_vs_url" not in result.assessment.unresolved
