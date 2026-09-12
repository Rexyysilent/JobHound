"""V5 layered-diff regressions: evidence foundation."""

import asyncio
import json
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from jobhound.config import CONFIG
from jobhound.enrich.pay import parse_title_pay
from jobhound.filters.eligibility import default_profile
from jobhound.normalize import normalize
from jobhound.sources.adzuna import AdzunaSource, _fallback_identity
from jobhound.text import html_to_text
from jobhound.v41.digest import build_digest
from jobhound.v41.engine import _decision, evaluate_raw
from jobhound.v41.roles import assess_role
from jobhound.v41.resolve import _is_ats_host
from jobhound.v41.store import V41Store
from tests.test_v41 import _adzuna, _greenhouse, _jsearch, _requirements
from jobhound.v41.extract import extract_sections
from jobhound.v41.models import (
    ActionBand,
    Assessment,
    Decision,
    EconomicsBand,
    EligibilityStatus,
    MatchStrength,
    PayState,
    SourceKind,
)


def test_html_normalization_preserves_heading_and_list_structure():
    rendered = html_to_text(
        "<h2>Requirements</h2><ul>"
        "<li>3+ years of review experience</li>"
        "<li>Fluent Bengali</li></ul>"
    )

    assert "Requirements" in rendered
    assert "• 3+ years of review experience" in rendered
    assert "• Fluent Bengali" in rendered

    sections = extract_sections(rendered)
    assert "3+ years of review experience" in sections.requirements
    assert "Fluent Bengali" in sections.requirements
    assert sections.completeness == "partial"


def test_heading_aliases_route_to_their_semantic_sections():
    sections = extract_sections(
        "Role overview: Evaluate model answers.\n"
        "Tasks: Review Bengali outputs.\n"
        "Your profile: Native Bengali.\n"
        "Minimum qualifications: 2 years of research experience."
    )

    assert "Evaluate model answers" in sections.role
    assert "Review Bengali outputs" in sections.responsibilities
    assert "Native Bengali" in sections.requirements
    assert "2 years of research experience" in sections.requirements
    assert sections.completeness == "partial"


def test_heading_parser_does_not_split_ordinary_prose():
    text = (
        "Our responsibilities include reviewing model outputs and writing notes. "
        "The team values clear communication."
    )

    sections = extract_sections(text)

    assert sections.headings_found == []
    assert sections.other == text
    assert sections.completeness == "unknown"


def test_html_normalization_is_idempotent_and_repairs_mojibake():
    rendered = html_to_text(
        "<p>AI Training Contributor â€“ Remote</p>"
        "<p>Requirements:</p><p>English &amp; Bengali</p>"
    )

    assert "AI Training Contributor – Remote" in rendered
    assert "English & Bengali" in rendered
    assert html_to_text(rendered) == rendered


def test_legacy_inline_headings_remain_supported():
    sections = extract_sections(
        "About the Role. Score model answers. "
        "What You'll Do. Review results. "
        "Requirements. Fluent English."
    )

    assert sections.role == "Score model answers"
    assert sections.responsibilities == "Review results"
    assert sections.requirements == "Fluent English"


def test_typed_evidence_models_have_backward_compatible_defaults():
    assessment = Assessment()
    decision = Decision(action_band=ActionBand.VERIFY, terminal_reason="verify")

    assert assessment.requirements_assessment.completeness == "unknown"
    assert assessment.role_assessment.ai_task_lane == "unknown"
    assert assessment.pay_assessment.state == PayState.UNKNOWN
    assert decision.priority_key.semantic_tuple == (0, 0, 0, 0, 0, 0, 0, 0, "")


def test_requirement_section_native_german_rejects_generic_title():
    result = evaluate_raw([_greenhouse(
        "AI Content Reviewer",
        "Language Evidence Labs",
        _requirements(" Native German required."),
    )])

    item = result.evaluated[0]
    assert item.decision.terminal_reason == "reject_language"
    assert item.assessment.requirements_assessment.language_required == ["english", "german"]
    assert any(
        claim.span == "Native German required"
        for claim in item.assessment.requirements_assessment.languages
    )


def test_requirement_language_alternatives_and_cumulative_demands():
    alternatives = evaluate_raw([_greenhouse(
        "AI Training Contributor",
        "Alternative Language Labs",
        _requirements(" Bengali or Hindi required."),
    )]).evaluated[0]
    cumulative = evaluate_raw([_greenhouse(
        "AI Training Contributor",
        "Cumulative Language Labs",
        _requirements(" English and German required."),
    )]).evaluated[0]

    assert alternatives.decision.terminal_reason != "reject_language"
    assert alternatives.assessment.requirements_assessment.languages[1].match_mode == "any"
    assert cumulative.decision.terminal_reason == "reject_language"
    assert "language:lang_mismatch:german" in cumulative.assessment.blockers


def test_mandatory_experience_uses_maximum_demand_not_smallest_number():
    result = evaluate_raw([_greenhouse(
        "AI Data Quality Specialist",
        "Experience Labs",
        _requirements(" 1 year preferred; minimum 5 years required."),
    )])

    item = result.evaluated[0]
    assert item.decision.terminal_reason == "reject_credentials"
    assert item.assessment.requirements_assessment.experience_min_years == 5
    assert "credentials:experience_mismatch:5_years" in item.assessment.blockers


def test_preferred_experience_is_watchout_not_hard_blocker():
    result = evaluate_raw([_greenhouse(
        "AI Data Quality Specialist",
        "Preference Labs",
        _requirements(" 5 years preferred; 1 year required."),
    )])

    item = result.evaluated[0]
    assert item.decision.terminal_reason != "reject_credentials"
    assert "preferred_experience:5_years" in item.assessment.unresolved
    assert item.assessment.requirements_assessment.experience_min_years == 1


def test_experience_parser_supports_words_plus_and_with_forms():
    cases = (
        ("at least three years working in Python", 3),
        ("3+ years' experience with Python", 3),
        ("three years with Python", 3),
    )
    for index, (requirement, expected) in enumerate(cases):
        result = evaluate_raw([_greenhouse(
            "AI Data Quality Specialist",
            f"Experience Syntax Labs {index}",
            _requirements(f" {requirement}."),
        )])
        assessment = result.evaluated[0].assessment.requirements_assessment
        assert assessment.experience_min_years == expected
        assert assessment.experience[0].span == requirement


def test_long_unstructured_description_remains_eligibility_unknown():
    description = (
        "This role evaluates AI outputs for quality and writes careful feedback. "
        * 30
    )
    item = evaluate_raw([_greenhouse(
        "AI Content Reviewer",
        "Unstructured Labs",
        description,
    )]).evaluated[0]

    assert item.assessment.requirements_assessment.completeness == "unknown"
    assert item.assessment.eligibility.value == "unknown"
    assert "requirements_not_explicit" in item.assessment.unresolved


def test_occupation_and_task_lane_are_separate():
    software = evaluate_raw([_greenhouse(
        "Software Engineer (AI Training)",
        "Occupation Labs",
        _requirements(" Minimum 3 years with Python."),
    )]).evaluated[0]
    qa = evaluate_raw([_greenhouse(
        "QA Engineer for AI Platform",
        "QA Labs",
        _requirements(),
    )]).evaluated[0]
    automation = evaluate_raw([_jsearch(
        "Build a WhatsApp Appointment Bot in n8n",
        "Upwork Client",
        "https://www.upwork.com/freelance-jobs/apply/build-bot_~02abcdef",
        _requirements(" Build and deliver the workflow."),
    )]).evaluated[0]

    assert software.assessment.role_assessment.occupation_family == "software_engineer"
    assert "ai_evaluation" in software.assessment.role_assessment.task_lanes
    assert software.assessment.role_assessment.ai_task_lane == "ai_evaluation"
    assert qa.assessment.role_assessment.occupation_family == "qa"
    assert automation.assessment.role_assessment.occupation_family == "automation_contractor"
    assert "workflow_automation" in automation.assessment.role_assessment.task_lanes


def test_profile_driven_specialist_domains_cover_neighboring_failures():
    titles = (
        "Insurance Underwriting AI Training Expert",
        "Architectural Design Evaluator",
        "Aviation Maintenance AI Reviewer",
        "Geology Research Data Specialist",
        "Legal Operations AI Trainer",
        "FPGA Simulation Reviewer",
    )
    for index, title in enumerate(titles):
        item = evaluate_raw([_greenhouse(
            title,
            f"Specialist Labs {index}",
            _requirements(),
        )]).evaluated[0]
        assert item.assessment.role_assessment.specialist_domain_status == "unclaimed"
        assert item.decision.terminal_reason == "reject_credentials"

    compatible = evaluate_raw([_greenhouse(
        "SPSS Research Assistant",
        "Statistics Labs",
        _requirements(),
    )]).evaluated[0]
    general = evaluate_raw([_greenhouse(
        "AI Training Specialist",
        "General AI Labs",
        _requirements(),
    )]).evaluated[0]
    language = evaluate_raw([_greenhouse(
        "Bengali Language Specialist",
        "Language Labs",
        _requirements(),
    )]).evaluated[0]
    assert compatible.assessment.role_assessment.specialist_domain_status == "claimed"
    assert general.assessment.role_assessment.specialist_domain_status == "none"
    assert language.assessment.role_assessment.specialist_domain_status == "none"


def test_claiming_canonical_domain_changes_specialist_outcome_without_code():
    item = evaluate_raw([_greenhouse(
        "Insurance Underwriting AI Training Expert",
        "Configurable Domain Labs",
        _requirements(),
    )]).evaluated[0]
    profile = default_profile()
    claimed = replace(
        profile,
        credential_domains={*profile.credential_domains, "insurance"},
    )
    reassessed = assess_role(
        item.canonical,
        item.assessment.matched_concepts,
        item.assessment.concept_evidence,
        claimed,
    )

    assert item.assessment.role_assessment.specialist_domain_status == "unclaimed"
    assert reassessed.specialist_domain_status == "claimed"


def test_pay_credibility_uses_the_pay_observation_not_canonical_url_source():
    ats = _greenhouse(
        "Bengali AI Data Evaluator",
        "Pay Provenance Labs",
        _requirements(),
    )
    aggregator = _adzuna(
        "Bengali AI Data Evaluator",
        "Pay Provenance Labs",
        _requirements(),
    )
    aggregator["raw"]["salary_min"] = 104000
    aggregator["raw"]["salary_max"] = 130000

    item = evaluate_raw([ats, aggregator]).evaluated[0]

    assert item.canonical.best_observation.source_kind.value == "original_ats"
    assert item.assessment.selected_pay is not None
    assert item.assessment.selected_pay.observation_source_kind.value == "aggregator_unresolved"
    assert item.assessment.pay_credibility < 0.8
    assert item.assessment.pay_assessment.selected_candidate_id


def test_source_first_description_keeps_lower_tier_evidence_independent():
    ats_description = _requirements()
    board_description = _requirements(
        " Native German required. " + ("Detailed project context. " * 80)
    )
    ats = _greenhouse(
        "AI Content Reviewer",
        "Description Authority Labs",
        ats_description,
    )
    board = _jsearch(
        "AI Content Reviewer",
        "Description Authority Labs",
        "https://example.com/jobs/description-authority",
        board_description,
    )

    item = evaluate_raw([ats, board]).evaluated[0]

    assert "Native German required" not in item.canonical.job.description
    assert item.canonical.field_sources["description"] == item.canonical.best_observation.observation_id
    assert item.decision.terminal_reason == "reject_language"
    assert any(
        claim.observation_id != item.canonical.field_sources["description"]
        and "German" in claim.span
        for claim in item.assessment.requirements_assessment.languages
    )


def test_authoritative_priority_places_economics_before_source():
    richer = evaluate_raw([_greenhouse(
        "AI Content Evaluator",
        "Economics First Labs",
        "Evaluate model outputs and provide feedback.",
    )]).evaluated[0]
    more_direct = evaluate_raw([_greenhouse(
        "AI Content Evaluator",
        "Source First Labs",
        "Evaluate model outputs and provide feedback.",
    )]).evaluated[0]

    for item in (richer, more_direct):
        item.assessment.eligibility = EligibilityStatus.PASSED
        item.assessment.match_strength = MatchStrength.STRONG
        item.assessment.blockers = []
        item.assessment.unresolved = []
    richer.assessment.economics_band = EconomicsBand.EXCELLENT
    richer.assessment.source_actionability = SourceKind.REPUTABLE_BOARD
    more_direct.assessment.economics_band = EconomicsBand.UNKNOWN
    more_direct.assessment.source_actionability = SourceKind.ORIGINAL_ATS

    richer_decision = _decision(richer.canonical, richer.assessment)
    direct_decision = _decision(more_direct.canonical, more_direct.assessment)

    assert richer_decision.action_band == ActionBand.PRIMARY
    assert direct_decision.action_band == ActionBand.PRIMARY
    assert richer_decision.priority_key.economics_quality == 3
    assert direct_decision.priority_key.source_actionability == 6
    assert richer_decision.priority_key.sort_tuple < direct_decision.priority_key.sort_tuple


def test_language_edge_requires_an_explicit_profile_language():
    result = evaluate_raw([
        _greenhouse(
            "Multilingual AI Evaluator",
            "Generic Language Labs",
            "Evaluate model outputs and provide feedback.",
        ),
        _greenhouse(
            "Bengali AI Evaluator",
            "Bengali Language Labs",
            "Evaluate model outputs and provide feedback.",
        ),
    ])
    by_company = {item.job.company: item for item in result.evaluated}

    assert (
        by_company["Generic Language Labs"]
        .decision.priority_key.explicit_profile_language_edge
        == 0
    )
    assert (
        by_company["Bengali Language Labs"]
        .decision.priority_key.explicit_profile_language_edge
        == 1
    )


def test_conventional_occupation_does_not_inherit_ai_modifier_priority():
    item = evaluate_raw([_greenhouse(
        "Software Engineer (AI Training)",
        "Occupation Priority Labs",
        _requirements(),
    )]).evaluated[0]

    assert item.assessment.role_assessment.occupation_family == "software_engineer"
    assert "ai_evaluation" in item.assessment.role_assessment.task_lanes
    assert item.decision.priority_key.role_priority == 1


def test_easy_entry_accessibility_outranks_unknown_requirements():
    result = evaluate_raw([
        _greenhouse(
            "AI Content Evaluator - No Experience Required",
            "Accessible Labs",
            "Evaluate model outputs and provide feedback.",
        ),
        _greenhouse(
            "AI Content Evaluator",
            "Unknown Requirements Labs",
            "Evaluate model outputs and provide feedback.",
        ),
    ])
    by_company = {item.job.company: item for item in result.evaluated}

    accessible = by_company["Accessible Labs"].decision.priority_key
    unknown = by_company["Unknown Requirements Labs"].decision.priority_key
    assert accessible.accessibility_confidence > unknown.accessibility_confidence


def test_priority_order_and_digest_are_input_order_invariant():
    records = [
        _greenhouse(
            f"AI Content Evaluator {index}",
            f"Deterministic Labs {index}",
            _requirements(),
        )
        for index in range(4)
    ]
    as_of = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc)
    expected_ids = None
    expected_digest = None

    for seed in range(8):
        shuffled = list(records)
        random.Random(seed).shuffle(shuffled)
        result = evaluate_raw(shuffled, as_of=as_of)
        digest = build_digest(
            result,
            include_all=True,
            min_priority=0,
            top_n=20,
            per_company_cap=20,
            per_platform_cap=0,
        )
        ids = [item.canonical.canonical_id for item in result.evaluated]
        if expected_ids is None:
            expected_ids = ids
            expected_digest = digest.text
        assert ids == expected_ids
        assert digest.text == expected_digest


def test_digest_explains_named_key_and_only_debug_shows_tiebreaker():
    result = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator",
        "Priority Explanation Labs",
        _requirements(),
    )])
    item = result.evaluated[0]
    normal = build_digest(
        result,
        items=[item],
        min_priority=0,
        top_n=5,
        per_company_cap=5,
        per_platform_cap=0,
    )
    debug = build_digest(
        result,
        include_all=True,
        min_priority=0,
        top_n=5,
        per_company_cap=5,
        per_platform_cap=0,
    )

    assert "Priority key:" in normal.text
    assert "Rank factors:" not in normal.text
    assert "stable canonical ID (hidden)" in normal.text
    assert item.canonical.canonical_id not in normal.text
    assert f"tie {item.canonical.canonical_id}" in debug.text


def _automation_records(count: int) -> list[dict]:
    return [
        _jsearch(
            f"Build n8n WhatsApp Workflow {index}",
            "Upwork",
            f"https://www.upwork.com/freelance-jobs/apply/workflow-{index}_~02abc{index}",
            _requirements(" Build and deliver the workflow."),
        )
        for index in range(count)
    ]


def _unique_counterparties(result):
    for index, item in enumerate(result.evaluated):
        item.canonical.counterparty_key = f"client:{index}"


def test_platform_cap_limits_flood_when_alternatives_exist_and_backfills_when_alone():
    upwork = _automation_records(4)
    direct = _greenhouse(
        "n8n Workflow Automation Contractor",
        "Direct Automation Labs",
        _requirements(" Build and deliver the workflow."),
    )
    mixed_result = evaluate_raw([*upwork, direct])
    _unique_counterparties(mixed_result)
    mixed = build_digest(
        mixed_result,
        include_all=True,
        min_priority=0,
        top_n=10,
        per_company_cap=10,
        per_platform_cap=3,
    )

    only_result = evaluate_raw(upwork)
    _unique_counterparties(only_result)
    only = build_digest(
        only_result,
        include_all=True,
        min_priority=0,
        top_n=10,
        per_company_cap=10,
        per_platform_cap=3,
    )

    assert mixed.display_counts["displayed"] == 4
    assert mixed.display_counts["hidden_platform_cap"] == 1
    assert mixed.accounting_ok
    assert only.display_counts["displayed"] == 4
    assert only.display_counts["hidden_platform_cap"] == 0
    assert only.accounting_ok


def test_store_persists_authoritative_priority_key(tmp_path):
    result = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator",
        "Priority Persistence Labs",
        _requirements(),
    )])
    store = V41Store(tmp_path / "priority.db")
    store.record_run(result)
    row = store.conn.execute(
        "SELECT priority_key_json FROM run_jobs"
    ).fetchone()
    version = store.conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()["value"]
    columns = {
        record["name"]
        for record in store.conn.execute("PRAGMA table_info(run_jobs)")
    }
    store.close()

    assert "priority_key_json" in columns
    assert version == "4"
    assert json.loads(row["priority_key_json"]) == (
        result.evaluated[0].decision.priority_key.model_dump(mode="json")
    )


def _serp_record(title: str, snippet: str, link: str) -> dict:
    return {
        "source": "serp",
        "raw": {"title": title, "snippet": snippet, "link": link},
    }


def test_compensation_parser_supports_suffix_currency_and_dash_ranges():
    cases = (
        "$120-$170/hr",
        "$120–$170/hr",
        "$120 to $170 per hour",
        "USD 120-170/hour",
        "120-170 USD/hour",
    )
    for expression in cases:
        parsed = parse_title_pay(expression, CONFIG.pay)
        assert parsed is not None, expression
        lo, hi, basis, estimated, _raw = parsed
        assert (lo, hi, basis, estimated) == (120, 170, "hour", False)


def test_marketplace_bare_budget_is_known_but_unrelated_bare_money_is_not_pay():
    marketplace = evaluate_raw([_serp_record(
        "Build n8n WhatsApp Appointment Bot",
        "Freelance Job in Scripts & Utilities - $350.00",
        "https://www.upwork.com/freelance-jobs/apply/"
        "build-n8n-whatsapp-appointment-bot_~02abc",
    )]).evaluated[0]
    unrelated = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator",
        "Bare Money Labs",
        _requirements(" The equipment example costs $350.00."),
    )]).evaluated[0]

    assert marketplace.assessment.selected_pay is not None
    assert marketplace.assessment.selected_pay.basis == "fixed"
    assert marketplace.assessment.selected_pay.fixed_amount_usd == 350
    assert marketplace.assessment.pay_assessment.state == PayState.BUDGET_KNOWN
    assert marketplace.assessment.economics_band == EconomicsBand.BUDGET_KNOWN
    assert unrelated.assessment.selected_pay is None


def test_up_to_pay_never_receives_excellent_economics():
    item = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator - up to $50/hr",
        "Up To Labs",
        _requirements(),
    )]).evaluated[0]

    assert item.assessment.selected_pay is not None
    assert item.assessment.selected_pay.up_to
    assert item.assessment.economics_band != EconomicsBand.EXCELLENT
    assert item.assessment.pay_assessment.state == PayState.UP_TO_ONLY


def test_fixed_and_hourly_claims_create_a_basis_conflict():
    hourly = _greenhouse(
        "Bengali AI Content Evaluator",
        "Basis Conflict Labs",
        _requirements(" Compensation: $20/hr."),
    )
    fixed = _adzuna(
        "Bengali AI Content Evaluator",
        "Basis Conflict Labs",
        _requirements(" Project budget: $500."),
    )
    item = evaluate_raw([hourly, fixed]).evaluated[0]

    assert item.assessment.pay_conflict
    assert {candidate.basis for candidate in item.assessment.pay_candidates} >= {
        "hour", "fixed",
    }
    assert item.decision.action_band == ActionBand.VERIFY


def test_regional_greenhouse_sigma_and_remoterocketship_company_hints():
    greenhouse = normalize(_serp_record(
        "Bengali Language Specialist",
        "Review model output.",
        "https://job-boards.eu.greenhouse.io/labelbox/jobs/111",
    ))
    sigma = normalize(_serp_record(
        "Bengali Linguistic Project",
        "Review model output.",
        "https://careers.sigma.ai/jobs/111",
    ))
    rocketship = normalize(_serp_record(
        "AI Voice Trainer - Bengali",
        "Review model output.",
        "https://www.remoterocketship.com/company/wing-assistant/jobs/111",
    ))

    assert greenhouse is not None and greenhouse.company == "Labelbox"
    assert _is_ats_host("job-boards.eu.greenhouse.io")
    assert sigma is not None and sigma.company == "Sigma AI"
    assert sigma.company_domain == "sigma.ai"
    assert rocketship is not None and rocketship.company == "Wing Assistant"
    assert rocketship.company_confidence == 0.60


def test_truncated_upwork_title_is_reconstructed_but_kept_in_verify():
    item = evaluate_raw([_serp_record(
        "Build n8n WhatsApp...",
        "Freelance Job in Scripts & Utilities - $350.00",
        "https://www.upwork.com/freelance-jobs/apply/"
        "build-n8n-whatsapp-appointment-bot_~02abc",
    )]).evaluated[0]

    assert item.job.title == "Build N8N Whatsapp Appointment Bot"
    assert item.job.original_title == "Build n8n WhatsApp..."
    assert item.job.title_truncated
    assert item.job.title_reconstructed
    assert "title_truncated" in item.assessment.unresolved
    assert item.decision.action_band == ActionBand.VERIFY


def test_requirement_location_scopes_override_generic_remote_flags():
    for index, country in enumerate(("UAE", "Chile", "Israel", "Norway", "Peru")):
        item = evaluate_raw([_greenhouse(
            "Bengali AI Content Evaluator",
            f"Location Labs {index}",
            _requirements(f" Must reside in {country}."),
        )]).evaluated[0]
        assert item.decision.terminal_reason == "reject_location", country

    india = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator",
        "India APAC Labs",
        _requirements(" APAC remote; India eligible."),
    )]).evaluated[0]
    assert india.decision.terminal_reason != "reject_location"


def test_late_requirement_location_is_parsed_without_biography_false_positive():
    late = (
        "About the Role:\nReview AI output.\n"
        + ("The global team solves global challenges. " * 80)
        + "\nRequirements:\nMust reside in Israel."
    )
    item = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator",
        "Late Scope Labs",
        late,
    )]).evaluated[0]
    global_only = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator",
        "Global Phrase Labs",
        _requirements(" Work on global challenges with a global team."),
    )]).evaluated[0]

    assert item.decision.terminal_reason == "reject_location"
    assert global_only.decision.terminal_reason != "reject_location"
    assert "worldwide" not in global_only.job.region_tags


def test_city_only_remote_scope_stays_verify():
    item = evaluate_raw([_jsearch(
        "Bengali AI Content Evaluator",
        "City Scope Labs",
        "https://example.com/jobs/city-scope",
        _requirements(),
        remote=True,
        city="Quito",
        country="",
    )]).evaluated[0]

    assert "location_scope_unknown" in item.assessment.unresolved
    assert item.decision.action_band == ActionBand.VERIFY


def _health_result(as_of: datetime, status: str, *, error: str | None, count: int):
    result = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator",
        f"Health Labs {as_of.hour}",
        _requirements(),
    )], as_of=as_of)
    result.source_health = [{
        "source": "adzuna",
        "status": status,
        "item_count": count,
        "successful_requests": int(status == "ok"),
        "failed_requests": int(status in {"partial", "failed"}),
        "retries": int(status in {"partial", "failed"}),
        "error_type": error,
    }]
    return result


def test_source_health_alerts_on_transition_not_repeated_failure(tmp_path):
    store = V41Store(tmp_path / "source-health.db")
    start = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)

    first = _health_result(start, "failed", error="ReadTimeout", count=0)
    store.annotate_source_health(first)
    assert first.source_health[0]["alert_notify"] is True
    store.record_run(first)

    repeated = _health_result(
        start + timedelta(hours=1), "failed", error="ReadTimeout", count=0
    )
    store.annotate_source_health(repeated)
    assert repeated.source_health[0]["alert_notify"] is False
    store.record_run(repeated)
    repeated_digest = build_digest(repeated, include_all=True, min_priority=0)
    assert "Adzuna: unavailable" in repeated_digest.text
    assert repeated_digest.operational_alerts == []

    recovered = _health_result(
        start + timedelta(hours=2), "ok", error=None, count=12
    )
    store.annotate_source_health(recovered)
    assert recovered.source_health[0]["alert_transition"] == "recovered"
    assert recovered.source_health[0]["alert_notify"] is True
    store.record_run(recovered)
    store.close()


def test_adzuna_missing_id_fallback_retains_distinct_rows_and_hides_query_secrets(monkeypatch):
    rows = [
        {
            "title": f"AI Evaluator {index}",
            "company": {"display_name": "Example"},
            "location": {"display_name": "Remote"},
            "redirect_url": (
                f"https://www.adzuna.in/details/{index}"
                "?app_key=fake-secret&utm_source=test"
            ),
        }
        for index in range(3)
    ]
    assert len({_fallback_identity(row, "ai evaluator") for row in rows}) == 3
    assert _fallback_identity(rows[0], "ai evaluator") == _fallback_identity(
        dict(rows[0]), "ai evaluator"
    )
    assert "fake-secret" not in _fallback_identity(rows[0], "ai evaluator")

    source = AdzunaSource()
    async def fake_page(_client, *, term, page):
        return [rows[0], dict(rows[0]), rows[1], rows[2]], None
    monkeypatch.setattr(source, "_fetch_page", fake_page)
    monkeypatch.setattr(CONFIG.sources.adzuna, "searches", ["ai evaluator"])
    monkeypatch.setattr(CONFIG.sources.adzuna, "max_pages", 1)
    retained = asyncio.run(source._fetch(None))
    assert len(retained) == 3


def test_adzuna_retry_after_supports_seconds_and_http_date():
    now = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)
    numeric = httpx.Response(
        429,
        headers={"Retry-After": "9"},
        request=httpx.Request("GET", "https://example.test"),
    )
    dated = httpx.Response(
        429,
        headers={"Retry-After": format_datetime(now + timedelta(seconds=12))},
        request=httpx.Request("GET", "https://example.test"),
    )

    assert AdzunaSource._retry_delay(numeric, 0, 1, now=now) == 9
    assert AdzunaSource._retry_delay(dated, 0, 1, now=now) == 12


def test_digest_exposes_access_requirements_pay_source_and_freshness():
    result = evaluate_raw([_greenhouse(
        "Bengali AI Content Evaluator - $20/hr",
        "Digest Facts Labs",
        _requirements(" Bengali required; minimum 1 year experience."),
    )])
    digest = build_digest(result, include_all=True, min_priority=0)

    assert "  Access:" in digest.text
    assert "  Requirements:" in digest.text
    assert "source title/original_ats" in digest.text
    assert "  Apply evidence:" in digest.text
    assert "  Freshness:" in digest.text
