from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from dataclasses import replace

from jobhound.filters.eligibility import Profile, default_profile
from jobhound.v41.compensation import parse_compensation
from jobhound.v41.documents import classify_document, classify_offer
from jobhound.v41.models import RequirementClaim, RequirementsAssessment, Assessment, CanonicalJob, ListingObservation, SourceKind, EligibilityStatus
from jobhound.models import Job
from jobhound.v41.action_policy import apply_action_policy
from jobhound.v41.extract import extract_pay_candidates
from jobhound.config import CONFIG
from jobhound.v41.requirements import language_mismatches, requirement_outcomes, assess_requirements
from jobhound.v41.engine import evaluate_observations
from unittest.mock import patch
from jobhound.v41.roles import classify_role_semantics
from jobhound.v41.semantic_projection import project_semantic_case


def _profile(**updates):
    values = dict(
        languages={"bengali", "english"}, credential_domains={"software"},
        known_languages={"bengali", "english", "dutch"}, gated_role_patterns={},
        working_languages={"bengali", "english"}, capability_domains={"software"},
    )
    values.update(updates)
    return Profile(**values)


def test_true_all_and_any_language_logic():
    all_claim = RequirementClaim(dimension="language", value=["bengali", "dutch"], modality="required", match_mode="all")
    any_claim = all_claim.model_copy(update={"match_mode": "any"})
    assert language_mismatches(RequirementsAssessment(languages=[all_claim]), _profile()) == ["lang_mismatch:dutch"]
    assert language_mismatches(RequirementsAssessment(languages=[any_claim]), _profile()) == []


def test_profile_preference_is_not_experience_evidence():
    claim = RequirementClaim(dimension="experience", value=2, modality="required", scope="python")
    outcome = requirement_outcomes(RequirementsAssessment(claims=[claim]), _profile(max_required_years=2))
    assert outcome.blockers == ()
    assert outcome.unverified == ("experience_unverified:python:2_years",)


def test_document_classification_paired_controls():
    seller = classify_document("I will automate your CRM", "My package starts at $350", "https://market/gig")
    buyer = classify_document("Need workflow automation", "Looking to hire a freelancer to build it", "https://community/post/1")
    assert (seller.document_type, seller.intent, seller.actionable) == ("seller_service", "seller", False)
    assert (buyer.document_type, buyer.intent, buyer.actionable) == ("buyer_request", "buyer", True)
    canonical = SimpleNamespace(job=SimpleNamespace(title="Jobs", description="Browse all open jobs", url="https://x/jobs"))
    assert classify_offer(canonical).document_type == "job_index"


def test_recorded_audio_and_labor_hours_are_distinct():
    recorded = parse_compensation("INR 500 per recorded audio hour")
    working = parse_compensation("INR 500 per working hour")
    assert recorded.basis == "output_audio_hour"
    assert recorded.labor_hour_equivalent is None
    assert working.basis == "labor_hour"
    assert working.labor_hour_equivalent == 500


def test_fixed_budget_and_explicit_ratio():
    fixed = parse_compensation("Fixed-price project budget: USD 350")
    converted = parse_compensation("USD 400 per recorded audio hour", labor_hours_per_unit=2)
    assert (fixed.basis, fixed.amount_low, fixed.labor_hour_equivalent) == ("fixed_project", 350, None)
    assert (converted.labor_hour_equivalent, converted.labor_equivalent_is_estimate) == (200, True)


def test_up_to_has_no_guaranteed_floor():
    claim = parse_compensation("Up to USD 120 per working hour")
    assert claim.qualifier == "up_to"
    assert claim.amount_high == 120
    assert claim.guaranteed_minimum is None


def test_specialist_and_generalist_subject_controls():
    specialist = classify_role_semantics("History Expert - AI Training", "Evaluate historical arguments.", "", _profile())
    generalist = classify_role_semantics("AI Content Evaluator", "Label history topics using provided guidelines; no expertise needed.", "", _profile())
    assert specialist.generalist is False
    assert specialist.subject == "history"
    assert generalist.generalist is True


def test_conventional_engineer_never_becomes_generalist_from_ai_words():
    role = classify_role_semantics("Senior Software Engineer (AI Training)", "Review using provided guidelines.", "", _profile())
    assert (role.occupation, role.generalist) == ("software_engineer", False)


def test_projection_scopes_advice_waiver_and_real_credentials():
    base = {"profile": {"working_languages": ["bn", "en"]}, "title": "Bengali AI Evaluator", "description_html": "Bengali and English required."}
    advice = project_semantic_case({**base, "append_text": "Consult a tax professional about your obligations."})
    required = project_semantic_case({**base, "append_text": "Applicants must be qualified tax professionals."})
    waived = project_semantic_case({**base, "append_text": "A university degree is preferred, not required."})
    assert "tax_professional" not in advice["requirements"]["credential_tags"]
    assert "tax_professional" in required["requirements"]["credential_tags"]
    assert "university_degree" not in waived["requirements"]["credential_tags"]


def test_projection_keeps_skill_specific_years_and_location_exclusion():
    result = project_semantic_case({
        "profile": {"working_languages": ["en"]}, "title": "AI Evaluator",
        "description_html": "Five years of Python work and two years of SQL work are required. We cannot accept applicants residing in India.",
    })
    assert result["requirements"]["required_years"] == {"python": 5, "sql": 2}
    assert result["location"]["excluded"] == ["india"]


def test_projection_document_and_original_pay_units():
    result = project_semantic_case({
        "profile": {"working_languages": ["en"]},
        "title": "Buy my $200 annotation package", "description_html": "I offer annotation services.",
        "pay_raw": "INR 500 per recorded audio hour",
    })
    assert result["document"].document_type == "seller_service"
    assert (result["pay"].currency, result["pay"].amount_low, result["pay"].basis) == ("INR", 500, "output_audio_hour")


def test_degree_or_experience_group_uses_documented_alternative():
    degree = RequirementClaim(dimension="degree", value="university_degree", modality="required", logic_group="degree-or-work", match_mode="any")
    work = RequirementClaim(dimension="experience", value=1, modality="minimum", scope="relevant_work", logic_group="degree-or-work", match_mode="any")
    outcome = requirement_outcomes(
        RequirementsAssessment(claims=[degree, work]),
        _profile(documented_professional_years={"relevant_work": 1}),
    )
    assert outcome.blockers == outcome.unverified == ()
    assert outcome.alternatives_satisfied == ("degree-or-work",)


def test_repeated_currency_en_dash_range_and_non_guarantee():
    claim = parse_compensation("$40–$120 per paid task-hour")
    assert (claim.amount_low, claim.amount_high, claim.basis) == (40, 120, "task_hour")
    assert claim.guaranteed_minimum is None


def test_explicit_absent_credential_blocks_but_unknown_only_verifies():
    claim = RequirementClaim(dimension="domain", value="medical_licence", modality="required")
    requirements = RequirementsAssessment(claims=[claim])
    absent = requirement_outcomes(requirements, _profile(absent_formal_credentials={"medical_licence"}))
    unknown = requirement_outcomes(requirements, _profile())
    assert absent.blockers == ("credential_mismatch:medical_licence",)
    assert absent.unverified == ()
    assert unknown.blockers == ()
    assert unknown.unverified == ("credential_unverified:medical_licence",)


def test_waived_degree_does_not_negate_later_medical_licence():
    result = project_semantic_case({
        "profile": {"working_languages": ["en"]}, "title": "Clinical Reviewer",
        "description_html": "No university degree is required. Applicants must hold a medical licence.",
        "content_state": "role_complete", "source_kind": "original_ats",
    })
    assert "medical_licence" in result["requirements"]["credential_tags"]


def test_global_team_and_customer_market_are_not_applicant_gates():
    result = project_semantic_case({
        "profile": {"working_languages": ["en"]}, "title": "English AI Evaluator",
        "description_html": "Our global team serves customers in the Dutch market. English required.",
        "content_state": "role_complete", "source_kind": "original_ats",
    })
    assert result["requirements"]["mandatory_languages"] == ["english"]
    assert result["location"]["excluded"] == []


def test_employer_hiring_language_is_not_misclassified_as_seller_service():
    classification = classify_document(
        "AI Evaluator", "We hire contractors and offer paid work to successful applicants.",
        "https://employer.example/jobs/123",
    )
    assert classification.document_type == "individual_job"
    assert classification.actionable is True


def test_make_workflow_is_automation_but_ordinary_make_is_not():
    workflow = classify_role_semantics(
        "Hiring someone to fix my Make workflow", "Named scenario and one revision.", "", _profile()
    )
    ordinary = classify_role_semantics(
        "Make a sandwich", "Write a short recipe.", "", _profile()
    )
    assert workflow.occupation == "automation_contractor"
    assert ordinary.occupation != "automation_contractor"


def test_symbolic_bare_fixed_amount_is_fixed_project():
    claim = parse_compensation("$100 fixed")
    assert (claim.currency, claim.amount_low, claim.basis) == ("USD", 100, "fixed_project")
    assert claim.labor_hour_equivalent is None


def _policy_fixture(state, now=None):
    now = now or datetime(2026, 9, 8, tzinfo=timezone.utc)
    job = Job(source="fixture", title="Bengali AI Evaluator", company="Example", url="https://example.com/jobs/1", description="Bengali required. Remote India.")
    posting = ListingObservation(observation_id="p", source="fixture", job=job, source_kind=SourceKind.ORIGINAL_ATS, content_state="complete", identity_state="exact", vacancy_state="verified_open", application_route_state="observed_public_route", verified_open_at=now, captured_at=now)
    account = ListingObservation(observation_id="a", source="account", origin="account_state", raw_payload=state, captured_at=now)
    canonical = CanonicalJob(canonical_id="c", job=job, observations=[posting, account], field_sources={"url": "p"})
    assessment = Assessment(eligibility=EligibilityStatus.PASSED, lifecycle="active", action_cost_known=True, action_cost_acceptable=True)
    apply_action_policy(canonical, assessment, now)
    return assessment


def test_captured_invitation_and_pending_assessment_actions_require_routes():
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    invitation = _policy_fixture({"observed_at": now.isoformat(), "invitation_state": "received", "invitation_route": "https://example.com/invite/1", "action_cost": {"cash_required": 0}}, now)
    unknown_route = _policy_fixture({"observed_at": now.isoformat(), "invitation_state": "received", "action_cost": {"cash_required": 0}}, now)
    assessment = _policy_fixture({"observed_at": now.isoformat(), "assessment_state": "pending", "assessment_step": "Complete the captured language check.", "assessment_route": "https://example.com/check/1", "action_cost": {"cash_required": 0}}, now)
    assert invitation.next_action == "respond_to_invitation"
    assert unknown_route.next_action != "respond_to_invitation"
    assert assessment.next_action == "complete_known_step"


def test_allocated_readiness_rejects_future_stale_and_missing_paid_terms():
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    base = {"observed_at": now.isoformat(), "tasks_allocated": True, "project_access": "verified", "payout_setup": "verified", "agreed_pay": "USD 10", "action_cost": {"cash_required": 0}}
    current = _policy_fixture(base, now)
    future = _policy_fixture({**base, "allocated_at": (now + timedelta(days=1)).isoformat()}, now)
    stale = _policy_fixture({**base, "allocated_at": (now - timedelta(days=30)).isoformat()}, now)
    unpaid = _policy_fixture({key: value for key, value in base.items() if key != "agreed_pay"}, now)
    assert current.next_action == "start_allocated_task"
    assert all(item.next_action != "start_allocated_task" for item in (future, stale, unpaid))


def test_time_to_cash_requires_reference_and_complete_prerequisites_not_cadence():
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    base = {"observed_at": now.isoformat(), "tasks_allocated": True, "project_access": "verified", "payout_setup": "verified", "agreed_pay": "USD 10", "action_cost": {"cash_required": 0}, "time_to_cash_days": 4}
    supported = _policy_fixture({**base, "time_to_cash_evidence_reference": "account:snapshot:1"}, now)
    unsupported = _policy_fixture({**base, "payout_cadence": "weekly"}, now)
    assert supported.time_to_cash_days == 4
    assert unsupported.time_to_cash_days is None


def test_captured_deadline_elapsed_or_infeasible_blocks():
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    elapsed = _policy_fixture({"observed_at": now.isoformat(), "application_deadline": (now - timedelta(hours=1)).isoformat(), "action_cost": {"cash_required": 0}}, now)
    infeasible = _policy_fixture({"observed_at": now.isoformat(), "deadline": (now + timedelta(hours=1)).isoformat(), "minimum_lead_time_hours": 2, "action_cost": {"cash_required": 0}}, now)
    assert "source_quality:application_deadline_elapsed" in elapsed.blockers
    assert "deadline_infeasible" in infeasible.blockers


def test_jobs_index_needs_index_url_not_individual_footer():
    index = classify_document(
        "Remote AI jobs", "Browse current remote roles in AI training and evaluation.",
        "https://www.opentrain.ai/jobs/",
    )
    individual = classify_document(
        "Bengali AI Evaluator", "Apply for this role. Browse all jobs.",
        "https://employer.example/jobs/bengali-evaluator",
    )
    assert index.document_type == "job_index"
    assert individual.document_type == "individual_job"


def test_uncatalogued_target_language_slot_blocks_generically():
    for language in ("Tibetan", "Guarani", "Aymara"):
        result = project_semantic_case({
            "profile": {"working_languages": ["en"]},
            "title": f"AI Training Contributor - {language} - Remote",
            "description_html": "Native fluency in the target language is required. Strong English preferred.",
            "content_state": "role_complete", "source_kind": "original_ats",
        })
        assert result["requirements"]["mandatory_languages"] == [language.casefold()]


def test_unknown_language_words_outside_bound_role_slot_do_not_gate():
    from jobhound.filters.eligibility import language_gate
    profile = _profile()
    assert language_gate("Remote contributor serving the Guarani market", profile) == []
    assert language_gate("AI Training Contributor - India - Remote", profile) == []


def test_domain_or_tool_delimited_slot_is_not_language_without_body_binding():
    for slot in ("Python", "Mathematics"):
        result = project_semantic_case({
            "profile": {"working_languages": ["en"]},
            "title": f"AI Training Contributor - {slot} - Remote",
            "description_html": "Review technical answers using supplied guidelines. English required.",
            "content_state": "role_complete", "source_kind": "original_ats",
        })
        assert slot.casefold() not in result["requirements"]["mandatory_languages"]


def test_lilt_tax_obligation_template_is_not_professional_requirement():
    result = project_semantic_case({
        "profile": {"working_languages": ["en"]},
        "title": "AI Training Contributor - English - Remote",
        "description_html": (
            "Native fluency in the target language required. "
            "As a contractor, you are responsible for your own tax obligations. "
            "We recommend consulting a tax professional before engaging."
        ),
        "content_state": "role_complete", "source_kind": "original_ats",
    })
    assert "domain_expert_generic" not in result["requirements"]["credential_tags"]
    assert "tax_professional" not in result["requirements"]["credential_tags"]


def _pay_canonical(title, description="", pay_raw=None):
    job = Job(source="fixture", title=title, company="Example", url="https://example.com/jobs/pay", description=description, pay_raw=pay_raw)
    observation = ListingObservation(observation_id="pay", source="fixture", job=job, source_kind=SourceKind.ORIGINAL_ATS)
    return CanonicalJob(canonical_id="pay", job=job, observations=[observation], field_sources={"title": "pay", "description": "pay", "url": "pay"})


def test_title_only_hourly_range_has_complete_unit_annotation(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    candidates, selected, conflict = extract_pay_candidates(_pay_canonical("AI Evaluator ₹500-₹700/hr"))
    assert not conflict and selected is not None
    assert (selected.amount_low, selected.amount_high, selected.actual_unit) == (500, 700, "labor_hour")
    assert selected.labor_hourly_supported and selected.min_hourly_usd is not None


def test_body_only_recorded_hour_never_becomes_working_hour(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    _, selected, _ = extract_pay_candidates(_pay_canonical(
        "Transcription Contributor", "Compensation is INR 500 per accepted recorded audio hour."
    ))
    assert selected is not None
    assert (selected.amount_low, selected.actual_unit) == (500, "output_audio_hour")
    assert selected.min_hourly_usd is None and not selected.labor_hourly_supported


def test_equal_number_title_and_body_with_different_units_conflict(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    candidates, _, conflict = extract_pay_candidates(_pay_canonical(
        "Transcription Contributor INR 500/hr",
        "Payment is INR 500 per accepted recorded audio hour.",
    ))
    assert {candidate.actual_unit for candidate in candidates} >= {"labor_hour", "output_audio_hour"}
    assert conflict


def test_voice_talent_requirements_stay_verify_unknowns_without_gender_inference():
    result = project_semantic_case({
        "profile": {"working_languages": ["hi", "en"]},
        "title": "Voice Talent - Hindi and English India - Female - Remote",
        "description_html": """
        Qualifications
        Must be native speaker of Hindi and English.
        Experience with voice acting, narration, podcasting, or streaming.
        Access to a professional or high-quality home recording setup.
        Able to provide demo, sample recordings, voiceover samples.
        Open to licensing your voice for AI voice cloning / TTS training.
        """,
        "content_state": "role_complete", "source_kind": "original_ats",
    })
    tags = set(result["requirements"]["credential_tags"])
    assert tags >= {"voice_performance_experience", "recording_setup", "voice_demo", "voice_licensing_consent"}
    assert all("female" not in str(claim.value).casefold() for claim in result["requirements"]["claims"])


def test_preferred_voice_equipment_does_not_become_mandatory():
    result = project_semantic_case({
        "profile": {"working_languages": ["hi", "en"]},
        "title": "Hindi Voice Contributor",
        "description_html": "Qualifications. A high-quality home recording setup is preferred. Demo samples are optional.",
        "content_state": "role_complete", "source_kind": "original_ats",
    })
    assert "recording_setup" not in result["requirements"]["credential_tags"]
    assert "voice_demo" not in result["requirements"]["credential_tags"]


def test_generic_ai_contributor_text_does_not_create_voice_readiness():
    result = project_semantic_case({
        "profile": {"working_languages": ["hi", "en"]},
        "title": "Hindi AI Contributor",
        "description_html": "Qualifications. Review AI answers using supplied guidelines. A headset is helpful for meetings.",
        "content_state": "role_complete", "source_kind": "original_ats",
    })
    assert not ({"recording_setup", "voice_demo", "voice_licensing_consent"} & set(result["requirements"]["credential_tags"]))


def test_voice_readiness_extraction_is_off_for_legacy_policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", False)
    canonical = _pay_canonical("Hindi Voice Talent", "Qualifications. Access to a professional home recording setup. Able to provide demo voice samples.")
    assessment = assess_requirements(canonical, _profile())
    assert not ({"recording_setup", "voice_demo"} & set(assessment.credentials_required))


def test_observed_voice_role_is_verify_with_named_requirement_tasks(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    text = """Qualifications
    Must be native speaker of Hindi and English.
    Experience with voice acting, narration, podcasting, or streaming.
    Access to a professional or high-quality home recording setup.
    Able to provide demo, sample recordings, voiceover samples.
    Open to licensing your voice for AI voice cloning / TTS training."""
    job = Job(source="ashby", title="Voice Talent - Hindi and English India - Female - Remote", company="LILT", url="https://jobs.ashbyhq.com/lilt-production/12345678-1234-4234-8234-123456789abc", description=text, location="India", is_remote=True, pay_raw="USD 10 per working hour")
    row = ListingObservation(observation_id="voice", source="ashby", job=job,
        normalized_url=job.url, original_url=job.url, source_confidence=.92,
        source_kind=SourceKind.ORIGINAL_ATS, content_state="complete", identity_state="exact",
        vacancy_state="verified_open", application_route_state="observed_public_route",
        verified_open_at=now, captured_at=now)
    profile = replace(
        default_profile(),
        languages={"hindi", "english"},
        working_languages={"hindi", "english"},
    )
    with patch("jobhound.v41.engine.default_profile", return_value=profile):
        item = evaluate_observations([row], as_of=now).evaluated[0]
    facts = {task["missing_fact"] for task in item.assessment.verification_tasks}
    assert item.decision.action_band.value == "verify", (item.assessment.blockers, item.assessment.unresolved)
    assert facts >= {"requirement_unverified:recording_setup", "requirement_unverified:voice_demo", "requirement_unverified:voice_licensing_consent"}
