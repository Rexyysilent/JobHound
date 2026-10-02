"""Ranking and breadth fixes from the 2026-09-30 email.

The email carded two LILT Subject Matter Expert roles, an Adzuna annotator and
a Bengali QC role while three WhatsApp automation jobs sat under "Also
eligible". Replaying that run found four causes:

1. The V5.5 order compared action cost before task fit, and an Upwork job's
   bid cost is never verified up front, so every Upwork job lost to every
   non-marketplace job regardless of fit.
2. V5.5 turned "5+ years of experience in Mathematics" into a verification
   task instead of applying the profile's max_required_years, so 23 LILT SME
   roles (all 5+ years) passed.
3. Two failed automatic checks moved a job to watch whatever its age. Mercor,
   Alignerr and Adzuna pages cannot be checked automatically, so their fresh,
   paid roles vanished after two days (94 of 130 surfaced jobs).
4. Upwork jobs arrive as two-line search snippets (the posting itself is
   behind a Cloudflare challenge), so location limits and required tools were
   never seen. The card now says so.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.action_policy import apply_action_policy
from jobhound.v41.digest import _release_entry
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.hydration import RetrievalPolicy
from jobhound.v41.models import (
    Assessment, CanonicalJob, EligibilityStatus, ListingObservation, PriorityKey, SourceKind,
)

NOW = datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc)


def _v55(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)


# 1. ranking ---------------------------------------------------------------

def test_task_fit_outranks_unverified_bid_cost():
    automation = PriorityKey(policy_version="v5.0.0-rc1", action_readiness=1,
                             action_cost_and_friction=0, match_strength=2, role_priority=5,
                             stable_tiebreaker="upwork")
    sme = PriorityKey(policy_version="v5.0.0-rc1", action_readiness=1,
                      action_cost_and_friction=1, match_strength=1, role_priority=6,
                      stable_tiebreaker="lilt")
    assert automation.sort_tuple < sme.sort_tuple


def test_bid_cost_still_breaks_ties_at_equal_fit():
    cheap = PriorityKey(policy_version="v5.0.0-rc1", action_readiness=1,
                        action_cost_and_friction=1, match_strength=2, role_priority=5,
                        stable_tiebreaker="b")
    unverified = PriorityKey(policy_version="v5.0.0-rc1", action_readiness=1,
                             action_cost_and_friction=0, match_strength=2, role_priority=5,
                             stable_tiebreaker="a")
    assert cheap.sort_tuple < unverified.sort_tuple


def test_readiness_still_comes_first():
    ready = PriorityKey(policy_version="v5.0.0-rc1", action_readiness=2, match_strength=0,
                        stable_tiebreaker="b")
    fit = PriorityKey(policy_version="v5.0.0-rc1", action_readiness=1, match_strength=3,
                      role_priority=6, stable_tiebreaker="a")
    assert ready.sort_tuple < fit.sort_tuple


# 2. experience years ------------------------------------------------------

def _sme(years: str):
    return [{"source": "greenhouse", "raw": {
        "id": 1, "title": "Subject Matter Expert – Mathematics (Hindi) – Remote", "_company": "Acme",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
        "location": {"name": "India (Remote)"},
        "content": ("We are seeking professionals to review Hindi AI benchmark answers. "
                    "QUALIFICATIONS - Native in Hindi with strong professional English proficiency - "
                    f"{years} of experience in Mathematics (e.g. teaching or research)."),
    }}]


def test_required_years_above_profile_maximum_reject_in_v55(monkeypatch):
    _v55(monkeypatch)
    row = evaluate_raw(_sme("5+ years"), as_of=NOW).evaluated[0]
    assert row.decision.action_band.value == "reject"
    assert "credentials:experience_mismatch:of:5_years" in row.assessment.blockers, row.assessment.blockers
    assert not any(u.startswith("experience_unverified") for u in row.assessment.unresolved)


def test_required_years_within_profile_maximum_stay_a_check(monkeypatch):
    _v55(monkeypatch)
    row = evaluate_raw(_sme("1+ year"), as_of=NOW).evaluated[0]
    assert row.decision.action_band.value != "reject"
    assert not any("experience_mismatch" in b for b in row.assessment.blockers)


# 3. watch after failed checks ---------------------------------------------

def _watch(*, posted_days_ago, cycles, first_failed_days_ago=None, observed_days_ago=0):
    job = Job(source="adzuna", title="Hindi Transcription Audit Specialist", company="Mercor",
              url="https://www.adzuna.in/details/1", description="Hindi audio audit work.",
              is_remote=True, platform_key="mercor",
              posted_at=(NOW - timedelta(days=posted_days_ago)).isoformat() if posted_days_ago is not None else None)
    observation = ListingObservation(observation_id="o", source="adzuna", job=job,
                                     source_kind=SourceKind.AGGREGATOR_UNRESOLVED)
    payload = {"automatic_verification_cycles": cycles,
               "observed_at": (NOW - timedelta(days=observed_days_ago)).isoformat(),
               "next_check_at": (NOW + timedelta(days=7)).isoformat(), "new_material_evidence": False}
    if first_failed_days_ago is not None:
        payload["first_failed_verification_at"] = (NOW - timedelta(days=first_failed_days_ago)).isoformat()
    state = ListingObservation(observation_id="account:verification:x:2", source="account_state",
                               origin="account_state", parent_observation_ids=["o"],
                               captured_at=NOW, raw_payload=payload)
    canonical = CanonicalJob(canonical_id="c", job=job, observations=[observation, state],
                             field_sources={"url": "o", "description": "o"})
    assessment = Assessment(eligibility=EligibilityStatus.PASSED, lifecycle="active")
    apply_action_policy(canonical, assessment, NOW)
    return assessment


def test_fresh_job_stays_visible_after_failed_checks():
    assessment = _watch(posted_days_ago=10, cycles=2, first_failed_days_ago=2)
    assert assessment.lifecycle == "active", assessment.lifecycle_reason
    assert any(t["missing_fact"] == "full_matching_requirements" for t in assessment.verification_tasks)


def test_job_older_than_the_watch_age_goes_to_watch_after_failed_checks():
    assessment = _watch(posted_days_ago=20, cycles=2, first_failed_days_ago=2)
    assert (assessment.lifecycle, assessment.lifecycle_reason) == ("watch", "verification_exhausted")


def test_undated_job_stays_visible_until_checks_fail_for_the_watch_age():
    assert _watch(posted_days_ago=None, cycles=1, first_failed_days_ago=1).lifecycle == "active"
    assert _watch(posted_days_ago=None, cycles=2, first_failed_days_ago=13).lifecycle == "active"
    assert _watch(posted_days_ago=None, cycles=2, first_failed_days_ago=15).lifecycle == "watch"


def test_undated_job_without_a_first_failure_time_stays_visible():
    # History written before first-failure times existed: the clock starts now.
    assert _watch(posted_days_ago=None, cycles=2).lifecycle == "active"


def test_stale_thin_copy_still_goes_to_watch():
    assessment = _watch(posted_days_ago=40, cycles=0)
    assert (assessment.lifecycle, assessment.lifecycle_reason) == ("watch", "stale_unresolved_copy")


def test_hydration_records_when_checks_first_failed(tmp_path, monkeypatch):
    from jobhound.v41.resolve import hydrate_result
    _v55(monkeypatch)
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Bengali Evaluator", "_company": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "short"}}]

    def attempt(as_of):
        result = evaluate_raw(raw, as_of=as_of)
        asyncio.run(hydrate_result(result, policy=RetrievalPolicy(request_budget=1, max_retries=0),
                                   transport=httpx.MockTransport(lambda r: httpx.Response(503)),
                                   review_namespace=tmp_path))
        return result.evaluated[0]

    first = attempt(NOW)
    second = attempt(NOW + timedelta(days=1))
    assert first.assessment.account_state["first_failed_verification_at"] == NOW.isoformat()
    # The first failure time is kept, not moved forward by later failures.
    assert second.assessment.account_state["first_failed_verification_at"] == NOW.isoformat()
    assert second.assessment.account_state["automatic_verification_cycles"] == 2
    assert second.decision.lifecycle == "active"


# 4. Upwork snippet-only cards ---------------------------------------------

def _upwork_row(monkeypatch, *, source="serp"):
    _v55(monkeypatch)
    raw = [{"source": source, "raw": {
        "link": "https://www.upwork.com/freelance-jobs/apply/Meta-WhatsApp-Cloud-API-Setup_~022104993204839042697/",
        "title": "Meta WhatsApp Cloud API Setup",
        "snippet": "Meta WhatsApp Cloud API Setup - Freelance Job in Web Development - $50.00 Fixed Price, posted September 29, 2026 - Upwork.",
    }}]
    return evaluate_raw(raw, as_of=NOW).evaluated[0]


def test_upwork_snippet_card_says_requirements_were_not_seen(monkeypatch):
    row = _upwork_row(monkeypatch)
    card = _release_entry(row)
    assert "Only a search snippet was seen" in card
    assert "location limits" in card and "required tools" in card


def test_card_ordering_line_lists_task_fit_before_action_cost(monkeypatch):
    card = _release_entry(_upwork_row(monkeypatch))
    line = next(l for l in card.splitlines() if l.strip().startswith("Ordering:"))
    assert line.index("task fit") < line.index("action-cost evidence")


def test_full_text_cards_do_not_carry_the_snippet_warning(monkeypatch):
    _v55(monkeypatch)
    row = evaluate_raw(_sme("1+ year"), as_of=NOW).evaluated[0]
    assert "Only a search snippet was seen" not in _release_entry(row)


# Review fixes for d659c4c -------------------------------------------------

def test_email_renderer_handles_upwork_snippet_cards(monkeypatch):
    import json
    from jobhound.delivery_outbox import evaluated_row
    from jobhound.delivery_transport import render_envelope
    payload = evaluated_row(_upwork_row(monkeypatch))
    body = render_envelope([{"payload": json.dumps(payload)}])
    assert "Only a search snippet was seen" in body
    # Payloads queued before the flag existed still render.
    del payload["assessment"]["search_snippet_only"]
    assert "Meta WhatsApp Cloud API Setup" in render_envelope([{"payload": json.dumps(payload)}])


def _policy(job, **observation):
    obs = ListingObservation(observation_id="o", source="serp", job=job,
                             source_kind=SourceKind.AGGREGATOR_UNRESOLVED, **observation)
    canonical = CanonicalJob(canonical_id="c", job=job, observations=[obs],
                             field_sources={"url": "o", "description": "o"})
    assessment = Assessment(eligibility=EligibilityStatus.PASSED, lifecycle="active")
    apply_action_policy(canonical, assessment, NOW)
    return assessment


def test_snippet_flag_follows_the_action_policy_completeness_rules():
    upwork = Job(source="serp", title="n8n WhatsApp automation", url="https://www.upwork.com/freelance-jobs/apply/x_~01/",
                 description="snippet", is_remote=True, platform_key="upwork")
    assert _policy(upwork, content_state="snippet").search_snippet_only is True
    # A truncated "complete" copy is still not the full posting.
    assert _policy(upwork, content_state="complete", truncated=True, identity_state="exact").search_snippet_only is True
    assert _policy(upwork, content_state="complete", identity_state="exact").search_snippet_only is False
    lookalike = Job(source="serp", title="x", url="https://notupwork.com/jobs/1", description="snippet", is_remote=True)
    assert _policy(lookalike, content_state="snippet").search_snippet_only is False


def test_years_inside_an_or_group_stay_a_check(monkeypatch):
    _v55(monkeypatch)
    row = evaluate_raw(_sme("a Master's degree in Mathematics or 5+ years"), as_of=NOW).evaluated[0]
    assert row.decision.action_band.value != "reject", row.assessment.blockers
    assert not any("experience_mismatch" in b for b in row.assessment.blockers)


def test_documented_field_years_are_found_even_when_the_scope_parse_is_poor():
    from dataclasses import replace
    from jobhound.filters.eligibility import default_profile
    from jobhound.v41.models import RequirementClaim, RequirementsAssessment
    from jobhound.v41.requirements import requirement_outcomes
    claim = RequirementClaim(dimension="experience", value=5.0, modality="minimum", required=True,
                             span="5+ years of experience in Mathematics (e.g.", scope="of")
    requirements = RequirementsAssessment(claims=[claim], experience=[claim])
    covered = requirement_outcomes(requirements, replace(default_profile(), max_required_years=2, documented_professional_years={"mathematics": 10}))
    assert not covered.blockers and not covered.unverified
    short = requirement_outcomes(requirements, replace(default_profile(), max_required_years=2, documented_professional_years={"mathematics": 3}))
    assert any(b.startswith("experience_mismatch") for b in short.blockers)
    none = requirement_outcomes(requirements, replace(default_profile(), max_required_years=2, documented_professional_years={}))
    assert any(b.startswith("experience_mismatch") for b in none.blockers)


def test_operator_attempts_without_a_first_failure_time_use_when_they_were_recorded():
    old = _watch(posted_days_ago=None, cycles=2, observed_days_ago=20)
    assert old.lifecycle == "watch"
    assert _watch(posted_days_ago=None, cycles=2, observed_days_ago=3).lifecycle == "active"


def test_legacy_failure_history_estimates_its_first_failure_instead_of_restarting(tmp_path, monkeypatch):
    import json
    from jobhound.v41.provenance import public_url
    from jobhound.v41.resolve import hydrate_result
    _v55(monkeypatch)
    url = "https://boards.greenhouse.io/acme/jobs/1"
    last_failure = NOW - timedelta(days=20)
    (tmp_path / "retrieval_state.json").write_text(json.dumps({
        "verification_cycles": {public_url(url): 2},
        # Next check = last failure + watch_recheck_days, and still in the future
        # relative to that failure, so the job is not re-attempted here.
        "verification_next_check": {public_url(url): (NOW + timedelta(days=1)).timestamp()},
    }), encoding="utf-8")
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Bengali Evaluator", "_company": "Acme",
            "absolute_url": url, "content": "short"}}]
    result = evaluate_raw(raw, as_of=NOW)
    asyncio.run(hydrate_result(result, policy=RetrievalPolicy(request_budget=1, max_retries=0),
                               transport=httpx.MockTransport(lambda r: httpx.Response(503)),
                               review_namespace=tmp_path))
    first = datetime.fromisoformat(result.evaluated[0].assessment.account_state["first_failed_verification_at"])
    # next check (NOW+1d) - watch_recheck_days (7) - one run (1d) = NOW-7d, not NOW.
    assert first == NOW - timedelta(days=7)
    assert last_failure < first


def test_attempt_report_does_not_claim_watch_for_a_visible_job(tmp_path, monkeypatch):
    from jobhound.v41.resolve import hydrate_result
    _v55(monkeypatch)
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Bengali Evaluator", "_company": "Acme",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "content": "short"}}]
    reports = []
    for day in (0, 1):
        result = evaluate_raw(raw, as_of=NOW + timedelta(days=day))
        reports.append(asyncio.run(hydrate_result(
            result, policy=RetrievalPolicy(request_budget=1, max_retries=0),
            transport=httpx.MockTransport(lambda r: httpx.Response(503)), review_namespace=tmp_path)))
    assert reports[1]["attempts"][0]["route"] == "retry_paused"
    assert result.evaluated[0].decision.lifecycle == "active"


def test_first_failure_times_are_pruned_with_their_history(tmp_path):
    import json
    from jobhound.v41.hydration import RetrievalBudget
    budget = RetrievalBudget(RetrievalPolicy(), review_namespace=tmp_path)
    budget.verification_cycles = {"a": 2}
    budget.verification_first_failed = {"a": 1.0, "gone": 2.0}
    budget.persist()
    saved = json.loads((tmp_path / "retrieval_state.json").read_text(encoding="utf-8"))
    assert saved["verification_first_failed"] == {"a": 1.0}


# Email subject (2026-10-01) ------------------------------------------------
# Gmail silently dropped the 2026-10-01 email twice under the fixed subject
# "JobHound V6 — durable opportunity digest"; the identical body with any
# other subject was delivered. Each day's subject is now distinct.

def test_email_subject_names_the_date_and_card_count(monkeypatch):
    import json
    from jobhound.delivery_outbox import evaluated_row
    from jobhound.notify.email import build_message
    from jobhound.delivery_transport import render_envelope
    payload = evaluated_row(_upwork_row(monkeypatch))
    sent_at = datetime(2026, 10, 1, 18, 5).astimezone().timestamp()
    body = render_envelope([{"payload": json.dumps(payload)}], now=sent_at)
    subject = build_message(body, "a@example.com", "b@example.com")["Subject"]
    assert subject == "JobHound — 1 job card, Thu 1 Oct 2026"
    assert "durable opportunity digest" not in subject


def test_email_subject_without_job_cards_says_source_updates():
    import json
    from jobhound.delivery_transport import render_envelope
    coverage = {"kind": "coverage", "health": {"source": "lever", "status": "partial"}}
    sent_at = datetime(2026, 10, 2, 18, 5).astimezone().timestamp()
    body = render_envelope([{"payload": json.dumps(coverage)}], now=sent_at)
    assert body.splitlines()[0] == "JobHound — source updates only, Fri 2 Oct 2026"


# 2026-10-01 email review --------------------------------------------------

def _pay_card(monkeypatch, content, title="Hindi Transcription Audit Specialist"):
    _v55(monkeypatch)
    raw = [{"source": "greenhouse", "raw": {"id": 7, "title": title, "_company": "Mercor",
            "absolute_url": "https://boards.greenhouse.io/mercor/jobs/7",
            "location": {"name": "Remote"}, "content": content}}]
    row = evaluate_raw(raw, as_of=NOW).evaluated[0]
    return next(l for l in _release_entry(row).splitlines() if l.strip().startswith("Pay:"))


def test_pay_line_shows_the_parsed_amount_not_the_whole_sentence(monkeypatch):
    line = _pay_card(monkeypatch, (
        "About the job Mercor connects talent with AI labs. Position: Sonic Audit Specialist - Hindi "
        "Type: Contract Compensation: $18/hour Location: Remote Role Responsibilities Evaluate AI "
        "training data by auditing multilingual transcription."))
    assert line.strip().startswith("Pay: $18/hour (per working hour;"), line
    assert "Position:" not in line and "Role Responsibilities" not in line


def test_pay_line_shows_a_range(monkeypatch):
    line = _pay_card(monkeypatch, "Review Hindi AI answers. Pay: $22 - $70/hour depending on expertise.")
    assert line.strip().startswith("Pay: $22–70/hour"), line


def test_search_result_pages_are_indexes_not_jobs():
    from jobhound.v41.documents import classify_document
    zip_page = classify_document(
        "Ai Content Evaluator Jobs (NOW HIRING)",
        "Browse 1000+ AI CONTENT EVALUATOR jobs ($44k-$160k) Remote $40 - $80/hr No prior experience",
        "https://www.ziprecruiter.com/Jobs/Ai-Content-Evaluator")
    assert zip_page.document_type == "job_index" and not zip_page.actionable
    vollna = classify_document(
        "Optimize Upwork Success with API Integrations",
        "Unlock potential on Upwork with API integration. Vollna provides real-time alerts and analytics.",
        "https://www.vollna.com/freelance-api-integration-jobs")
    assert vollna.document_type == "job_index" and not vollna.actionable
    real = classify_document("Hindi Evaluator", "Review Hindi AI answers. Pay $15/hour. Apply now.",
                             "https://boards.greenhouse.io/acme/jobs/1")
    assert real.document_type == "individual_job"


def test_linkedin_hiring_title_location_is_a_region_check():
    from jobhound.filters.listing_quality import region_lock_check
    assert region_lock_check("Hire Feed hiring AI Evaluator (Remote) in European Union")
    assert region_lock_check("Crossing Hurdles hiring AI Trainer | $70/hr Remote in European Union")
    assert region_lock_check("Acme hiring Data Annotator in United States - LinkedIn")
    for ok in ("Hired hiring AI Content Evaluator (Remote) in APAC",
               "micro1 hiring Data Annotation Specialist - AI trainer in APAC - LinkedIn",
               "Outlier hiring Bengali Language - AI Training in Greater Kolkata Area",
               "Welo Data hiring Internet Search Evaluator - English (India) in India",
               "Outlier hiring Remote Bengali AI Content Contributor in ... - LinkedIn"):
        assert region_lock_check(ok) == [], ok


# 2026-10-02 email review --------------------------------------------------

def _lang_row(monkeypatch, title, content, location="Remote"):
    _v55(monkeypatch)
    raw = [{"source": "greenhouse", "raw": {"id": 9, "title": title, "_company": "LILT",
            "absolute_url": "https://boards.greenhouse.io/lilt/jobs/9",
            "location": {"name": location}, "content": content}}]
    return evaluate_raw(raw, as_of=NOW).evaluated[0]


def test_title_language_slot_outside_the_profile_rejects(monkeypatch):
    cases = [
        ("Linguist/Transcriber - Chichewa/Nyanja - Remote",
         "Are you a native Chichewa speaker? QUALIFICATIONS - Native-level proficiency in Chichewa."),
        ("Linguist - Shona - UI Technical / Marketing - Remote",
         "QUALIFICATIONS - Native-level proficiency in Shona with professional English."),
        ("Content Writer - Chichewa - Remote", "Write original content. Native Chichewa writer required."),
    ]
    for title, content in cases:
        row = _lang_row(monkeypatch, title, content)
        assert row.decision.action_band.value == "reject", (title, row.assessment.blockers)
        assert any("lang_mismatch" in b for b in row.assessment.blockers), (title, row.assessment.blockers)


def test_title_language_slot_inside_the_profile_or_not_a_language_passes(monkeypatch):
    hindi = _lang_row(monkeypatch, "Linguist/Transcriber - Hindi - Remote",
                      "Are you a native Hindi speaker? Native-level proficiency in Hindi.")
    assert not any("lang_mismatch" in b for b in hindi.assessment.blockers), hindi.assessment.blockers
    generic = _lang_row(monkeypatch, "Linguist - UI Technical - Remote",
                        "Native-level proficiency in the target language.")
    assert not any("lang_mismatch" in b for b in generic.assessment.blockers), generic.assessment.blockers


def test_any_named_foreign_country_is_a_location_lock():
    from jobhound.filters.listing_quality import location_gate
    for place in ("Zambia (Remote)", "Malawi (Remote)", "Senegal (Remote)", "Zimbabwe", "Ghana (Remote)"):
        assert location_gate(True, [], place), place
    for place in ("Bankura, West Bengal", "India (Remote)", "Remote", "Kolkata"):
        assert location_gate(True, [], place) == [], place


def test_a_place_name_is_not_the_language():
    from jobhound.v41.extract import _term_matches
    text = "Job Locations :- All over West Bengal- Bankura, Purulia"
    assert not _term_matches("language_ai", "bengali", "body", text)
    assert _term_matches("language_ai", "bengali", "body", "Fluent Bengali speakers wanted")
    assert _term_matches("language_ai", "bangla", "body", "Bangla typing work")


# Review of d659c4c..40a9bb7 -----------------------------------------------

def test_hyphenated_country_codes_still_lock():
    from jobhound.filters.listing_quality import location_gate
    for place in ("UK-based", "USA-only", "EU-based remote", "Guinea-Bissau (Remote)"):
        assert location_gate(True, [], place), place


def test_sanctions_exclusions_are_not_a_location_lock():
    from jobhound.filters.listing_quality import location_gate
    text = "Location: Remote - Worldwide (excluding Cuba, Iran, Syria, Russia, North Korea)"
    assert location_gate(True, ["worldwide"], "Remote", description=text) == []
    assert location_gate(True, [], "Remote, except Iran") == []
    assert location_gate(True, [], "Jordan Smith's team - Remote") == []


def test_topic_slots_after_writer_titles_are_not_languages(monkeypatch):
    row = _lang_row(monkeypatch, "Content Writer - SEO - Remote",
                    "Fluent English and strong SEO skills. We need SEO writers.")
    assert not any("lang_mismatch" in b for b in row.assessment.blockers), row.assessment.blockers


def test_board_footer_does_not_make_a_job_page_an_index():
    from jobhound.v41.documents import classify_document
    page = classify_document(
        "Bengali AI Response Evaluator",
        "Review Bengali AI answers. Requirements: fluent Bengali. Apply now. "
        "Browse 2,000+ remote jobs on our board.",
        "https://remotive.com/remote-jobs/ai/bengali-ai-response-evaluator-123")
    assert page.document_type == "individual_job"


def test_large_amounts_are_not_scientific_notation():
    from jobhound.v41.digest import _amount_text
    from jobhound.v41.models import PayCandidate
    pay = PayCandidate(raw="INR 12,00,000 - 25,00,000 per year", source_field="structured",
                       observation_id="x", confidence=.9, basis="year", currency="INR",
                       amount_low=1200000, amount_high=2500000)
    assert _amount_text(pay) == "₹1,200,000–2,500,000/year"


def test_and_joined_requirements_keep_the_years_ceiling(monkeypatch):
    _v55(monkeypatch)
    row = _lang_row(monkeypatch, "Backend Engineer - Remote",
                    "QUALIFICATIONS - Requires a Master's degree and 10+ years of experience in Python or Go.")
    assert any("experience_mismatch" in b for b in row.assessment.blockers), row.assessment.blockers


def test_documented_years_attach_to_the_field_after_the_years():
    from dataclasses import replace
    from jobhound.filters.eligibility import default_profile
    from jobhound.v41.models import RequirementClaim, RequirementsAssessment
    from jobhound.v41.requirements import requirement_outcomes
    claim = RequirementClaim(dimension="experience", value=10.0, modality="minimum", required=True,
                             span="10+ years of people-management experience; Python familiarity required",
                             scope="of")
    out = requirement_outcomes(RequirementsAssessment(claims=[claim], experience=[claim]),
                               replace(default_profile(), max_required_years=12,
                                       documented_professional_years={"python": 8}))
    # 10 > ceiling? no (12): unverified, not a mismatch against Python's 8 years.
    assert not out.blockers and out.unverified


def test_language_names_are_cached_within_a_run(monkeypatch):
    import jobhound.v41.extract as extract
    calls = []
    real = extract._build_language_names
    monkeypatch.setattr(extract, "_build_language_names", lambda p: calls.append(1) or real(p))
    monkeypatch.setattr(extract, "_LANGUAGE_NAMES", (None, frozenset()))
    for _ in range(50):
        extract._term_matches("language_ai", "bengali", "body", "Fluent Bengali")
    assert len(calls) == 1


def test_legacy_backfill_counts_elapsed_rechecks(tmp_path, monkeypatch):
    import json
    from jobhound.v41.provenance import public_url
    from jobhound.v41.resolve import hydrate_result
    _v55(monkeypatch)
    url = "https://boards.greenhouse.io/acme/jobs/1"
    (tmp_path / "retrieval_state.json").write_text(json.dumps({
        "verification_cycles": {public_url(url): 5},
        "verification_next_check": {public_url(url): (NOW + timedelta(days=1)).timestamp()},
    }), encoding="utf-8")
    raw = [{"source": "greenhouse", "raw": {"id": 1, "title": "Bengali Evaluator", "_company": "Acme",
            "absolute_url": url, "content": "short"}}]
    result = evaluate_raw(raw, as_of=NOW)
    asyncio.run(hydrate_result(result, policy=RetrievalPolicy(request_budget=1, max_retries=0),
                               transport=httpx.MockTransport(lambda r: httpx.Response(503)),
                               review_namespace=tmp_path))
    first = datetime.fromisoformat(result.evaluated[0].assessment.account_state["first_failed_verification_at"])
    # 3 weekly rechecks after the 2 automatic cycles: (1 + 7*3 + 7 + 1) days earlier.
    assert first == NOW + timedelta(days=1) - timedelta(days=7 + 1 + 7 * 3)


def test_subject_counts_job_status_updates(monkeypatch):
    import json
    from jobhound.delivery_outbox import evaluated_row
    from jobhound.delivery_transport import render_envelope
    status = evaluated_row(_upwork_row(monkeypatch))
    status["decision"]["lifecycle"] = "closed"
    sent_at = datetime(2026, 10, 2, 18, 5).astimezone().timestamp()
    body = render_envelope([{"payload": json.dumps(status)}], now=sent_at)
    assert body.splitlines()[0] == "JobHound — 1 job status update, Fri 2 Oct 2026"
