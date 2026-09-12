"""V4.2 regressions captured from the 2026-08-06 production digest."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.v41.digest import build_digest
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.models import ActionBand, SourceKind
from jobhound.v41.resolve import resolve_result_urls, resolve_url


AS_OF = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)


def _jsearch(
    title: str,
    company: str,
    description: str,
    *,
    url: str = "https://www.linkedin.com/jobs/view/123",
    remote: bool = True,
    city: str | None = None,
    country: str | None = "India",
) -> dict:
    return {
        "source": "jsearch",
        "raw": {
            "job_title": title,
            "employer_name": company,
            "job_apply_link": url,
            "job_description": description,
            "job_is_remote": remote,
            "job_city": city,
            "job_country": country,
            "job_posted_at_datetime_utc": "2026-08-05T06:00:00Z",
        },
    }


def _serp(title: str, snippet: str, link: str, **extra) -> dict:
    return {
        "source": "serp",
        "raw": {"title": title, "snippet": snippet, "link": link, **extra},
    }


def _requirements(extra: str = "") -> str:
    return (
        "About the Role. Evaluate AI outputs and document quality issues. "
        "What You'll Do. Review responses and annotate errors. "
        "Qualifications. Excellent written English, attention to detail, and "
        f"reliable internet. {extra} Benefits. Flexible remote contract."
    )


@pytest.mark.parametrize(
    ("title", "country"),
    [
        ("AI Training Contributor - Bengali (Bangladesh) - Remote", "Bangladesh"),
        ("AI Training Contributor - English (Ireland) - Remote", "Ireland"),
        ("AI Training Contributor - Kabyle (Algeria) - Remote", "Algeria"),
        ("AI Training Contributor - Balochi (Pakistan) - Remote", "Pakistan"),
        ("AI Training Contributor - Sepedi (South Africa) - Remote", "South Africa"),
        ("AI Training Contributor - Traditional Chinese (Taiwan) - Remote", "Taiwan"),
    ],
)
def test_remote_country_lock_wins_over_provider_remote_flag(title, country):
    item = evaluate_raw([
        _jsearch(title, "LILT", _requirements(), city=country, country=country)
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.action_band == ActionBand.REJECT
    assert item.decision.terminal_reason in {"reject_location", "reject_language"}
    assert not (
        item.decision.terminal_reason == "surface_primary"
        or item.decision.terminal_reason == "surface_verify"
    )


@pytest.mark.parametrize("language", ["Kabyle", "Balochi", "Sepedi", "Maori", "Hawaiian"])
def test_new_language_requirements_do_not_leak(language):
    item = evaluate_raw([
        _jsearch(
            f"{language} AI Content Evaluator - Remote",
            "Language Vendor",
            _requirements(),
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_language"


def test_cyrillic_homoglyph_cannot_hide_latvian_requirement():
    # The "a" below is Cyrillic U+0430, matching the production title.
    item = evaluate_raw([
        _jsearch("Lаtvian AI Content Evaluator - Remote", "LILT", _requirements())
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_language"
    assert any("latvian" in reason for reason in item.assessment.blockers)


def test_explicit_onsite_description_beats_missing_provider_location():
    item = evaluate_raw([
        _serp(
            "Data Annotation Intern (AI)",
            "Location: Gurgaon, Haryana - On-site. Annotate AI training data.",
            "https://www.linkedin.com/jobs/view/data-annotation-intern-at-loopedincode-123",
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_location"
    assert any("onsite_or_hybrid" in reason for reason in item.assessment.blockers)


def test_public_health_degree_in_requirements_is_profile_gated():
    item = evaluate_raw([
        _jsearch(
            "Health Policy & Management - AI Content Specialist",
            "Alignerr",
            _requirements("Must have an MPH or MSPH in public health."),
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_credentials"
    assert any("credential_mismatch:public_health" in reason
               for reason in item.assessment.blockers)


def test_relevant_professional_experience_phrase_is_enforced():
    item = evaluate_raw([
        _jsearch(
            "AI Evaluator - Software Expert",
            "Mercor",
            _requirements("Must-Have. 5+ years of relevant professional experience."),
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_credentials"
    assert any("experience_mismatch:5_years" in reason
               for reason in item.assessment.blockers)


def test_thin_specialist_copy_cannot_claim_eligibility_passed():
    item = evaluate_raw([
        _serp(
            "AI Evaluator - Software Expert",
            "Evaluate AI-generated artifacts against quality rubrics. Remote contract.",
            "https://www.adzuna.in/details/123",
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.assessment.eligibility.value == "unknown"
    assert "specialist_requirements_unverified" in item.decision.watch_outs
    assert item.decision.action_band == ActionBand.VERIFY


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Market Research",
            "Find contacts and send emails for cold email lead generation. "
            "Use tools for data collection and analysis.",
        ),
        (
            "Inside Sales Specialists-CRM Workflow software automation App",
            "Own prospecting and pipeline growth for an outbound sales team.",
        ),
    ],
)
def test_sales_work_does_not_become_automation_or_annotation(title, description):
    item = evaluate_raw([
        _jsearch(title, "GROW10X", description)
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_role"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.upwork.com/freelance-jobs/n8n/",
        "https://www.upwork.com/freelance-jobs/zapier/",
        "https://www.linkedin.com/jobs/data-annotation-remote-jobs",
        "https://aigigjobs.com/languages/bengali",
    ],
)
def test_search_and_category_pages_are_not_jobs(url):
    item = evaluate_raw([
        _serp("N8n Freelance Jobs", "Find remote automation jobs.", url)
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_source_quality"


@pytest.mark.parametrize(
    "url",
    [
        "https://flexboard.9y.liveblog365.com/ai-writing-evaluator",
        "https://remoteanywherejob.com/jobs/ai-evaluator",
        "https://pbridgeco.com/jobs/data-annotation",
    ],
)
def test_august_free_host_and_seo_farms_do_not_surface(url):
    item = evaluate_raw([
        _jsearch("AI Writing Evaluator (Remote)", "Unknown", _requirements(), url=url)
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.action_band == ActionBand.REJECT
    assert item.decision.terminal_reason in {"reject_scam", "reject_source_quality"}


def test_concrete_upwork_automation_listing_surfaces_for_verification():
    item = evaluate_raw([
        _serp(
            "CRM monday.com Automation Specialist Needed (Meta + WhatsApp + Chatbot)",
            "Build n8n webhooks, WhatsApp API integrations, and CRM automation. $150 fixed.",
            "https://www.upwork.com/freelance-jobs/apply/CRM-Automation-Specialist_~022078891507244897998/",
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.job.company == "Upwork Client"
    assert item.job.is_remote
    assert item.decision.action_band == ActionBand.VERIFY
    assert item.decision.terminal_reason == "surface_verify"


def test_alignerr_official_job_has_company_original_provenance_and_pay():
    item = evaluate_raw([
        _serp(
            "Bengali Language Expert",
            _requirements("Native Bengali required. Compensation $40-$120 per hour. Remote."),
            "https://www.alignerr.com/jobs/05a3b35a-6931-4892-8fc7-3c9141fa2826",
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.job.company == "Alignerr"
    assert item.job.company_domain == "alignerr.com"
    assert item.assessment.source_actionability == SourceKind.ORIGINAL_EMPLOYER
    assert item.assessment.selected_pay is not None
    assert item.assessment.selected_pay.min_hourly_usd == 40
    assert item.assessment.selected_pay.claim_credibility == 0.7
    assert item.job.effective_hourly_usd == 20.4
    assert item.assessment.pay_rate_for_ranking == 20.4
    assert item.decision.action_band == ActionBand.PRIMARY


def test_old_snapshot_unknown_provenance_is_reclassified_by_current_rules():
    record = _serp(
        "Bengali Language Expert",
        _requirements("Native Bengali required. Remote."),
        "https://www.alignerr.com/jobs/example",
    )
    record["_v41_resolution"] = {
        "normalized_url": "https://www.alignerr.com/jobs/example",
        "source_kind": "unknown",
        "source_confidence": 0.55,
        "redirect_trail": [],
        "attempted": False,
        "error": None,
    }
    item = evaluate_raw([record], as_of=AS_OF).evaluated[0]
    assert item.assessment.source_actionability == SourceKind.ORIGINAL_EMPLOYER


def test_thin_serp_age_is_unknown_not_assumed_fresh():
    item = evaluate_raw([
        _serp(
            "AI Trainer (Bengali) | $50/hr Remote",
            "Bengali evaluator and annotator. Remote contract.",
            "https://www.linkedin.com/jobs/view/ai-trainer-at-crossing-hurdles-123",
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.assessment.freshness == CONFIG.v41.thin_source_unknown_freshness
    assert "posting_age_unknown" in item.decision.watch_outs


def test_resolver_retries_retry_after_then_succeeds():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, request=request, headers={"Retry-After": "0"})
        if request.url.host == "www.adzuna.in":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://jobs.ashbyhq.com/lilt-production/abc"},
            )
        return httpx.Response(200, request=request)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await resolve_url(
                "https://www.adzuna.in/land/ad/42?se=proof&v=proof",
                client=client,
                max_retries=2,
                retry_base_seconds=0,
            )

    resolution = asyncio.run(exercise())
    assert calls == 3
    assert resolution.error is None
    assert resolution.final == "https://jobs.ashbyhq.com/lilt-production/abc"


def test_digest_reports_shown_eligible_and_hidden_per_band():
    titles = [
        "Bengali AI Evaluator",
        "Hindi Speech Data Annotator",
        "English Search Quality Rater",
    ]
    records = [
        _jsearch(
            title,
            "One Company",
            _requirements("Native Bengali required."),
            url=f"https://www.linkedin.com/jobs/view/{index}",
        )
        for index, title in enumerate(titles)
    ]
    result = evaluate_raw(records, as_of=AS_OF)
    digest = build_digest(
        result,
        include_all=True,
        min_priority=0,
        top_n=15,
        per_company_cap=1,
    )
    assert "1 shown / 3 eligible; hidden: company cap 2" in digest.text
    assert digest.accounting_ok


def test_v42_is_the_active_recorded_engine():
    result = evaluate_raw([
        _jsearch("Bengali AI Evaluator", "Example", _requirements())
    ], as_of=AS_OF)
    assert CONFIG.engine.active == "v4.2"
    assert result.metadata.engine_version == "v4.2"


@pytest.mark.parametrize(
    "title",
    [
        "Sinhalese AI Content Evaluator - Remote",
        "Portugese AI Content Evaluator - Remote",
    ],
)
def test_language_alias_typos_do_not_leak(title):
    item = evaluate_raw([_jsearch(title, "Vendor", _requirements())], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_language"


@pytest.mark.parametrize(
    "title",
    [
        "Medical Subject Matter Expert - AI Trainer",
        "Mechanical Design & CAD Specialist - AI Trainer",
        "MBBS AI Training Expert",
    ],
)
def test_specialized_credentials_do_not_leak(title):
    item = evaluate_raw([_jsearch(title, "Vendor", _requirements())], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_credentials"


def test_generic_developer_is_not_boosted_by_employer_ai_boilerplate():
    item = evaluate_raw([
        _jsearch(
            "Fronetnd Developer",
            "Agency",
            "About us. We provide AI training and annotation services. "
            "About the Role. Build responsive web interfaces in JavaScript.",
        )
    ], as_of=AS_OF).evaluated[0]
    assert "ai_evaluation" not in item.assessment.matched_concepts
    assert "annotation" not in item.assessment.matched_concepts
    assert item.assessment.easy_entry is False


def test_fixed_price_automation_is_preserved_without_fake_hourly_rate():
    record = _serp(
        "CRM monday.com Automation Specialist Needed",
        "Build n8n webhooks and WhatsApp integrations. $150 fixed.",
        "https://www.upwork.com/freelance-jobs/apply/CRM-Automation_~01/",
    )
    result = evaluate_raw([record], as_of=AS_OF)
    item = result.evaluated[0]
    assert item.assessment.selected_pay is not None
    assert item.assessment.selected_pay.basis == "fixed"
    assert item.assessment.selected_pay.fixed_amount_usd == 150
    assert item.job.pay_min_hourly_usd is None
    assert item.assessment.economics_band.value == "budget_known"
    assert "fixed_price_scope_review" in item.decision.watch_outs
    digest = build_digest(result, include_all=True, min_priority=0)
    assert "$150 fixed" in digest.text


def test_scoped_upwork_seniority_is_a_review_not_a_hard_reject():
    item = evaluate_raw([
        _serp(
            "Senior WhatsApp Automation Specialist",
            "Build n8n webhooks and WhatsApp API integrations. $25-$47/hr.",
            "https://www.upwork.com/freelance-jobs/apply/WhatsApp_~03/",
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.action_band == ActionBand.VERIFY
    assert "seniority_scope_review:senior" in item.decision.watch_outs


def test_automation_has_a_reserved_digest_section():
    records = [
        _jsearch(
            f"Bengali AI Evaluator {index}",
            f"Language Vendor {index}",
            _requirements("Native Bengali required."),
            url=f"https://www.linkedin.com/jobs/view/lang-{index}",
        )
        for index in range(3)
    ]
    records.append(_serp(
        "n8n WhatsApp Workflow Automation",
        "Build n8n webhooks and WhatsApp API integrations for a fixed-price project.",
        "https://www.upwork.com/freelance-jobs/apply/n8n_~04/",
    ))
    result = evaluate_raw(records, as_of=AS_OF)
    digest = build_digest(result, include_all=True, min_priority=0, top_n=2)
    assert "Automation opportunities" in digest.text
    assert "n8n WhatsApp Workflow Automation" in digest.text
    assert digest.accounting_ok


def test_exact_public_url_collapses_even_when_company_labels_differ():
    url = "https://jobs.example.com/posting/one?utm_source=a"
    result = evaluate_raw([
        _jsearch("Bengali AI Evaluator", "Company A", _requirements(), url=url),
        _jsearch("Bengali AI Evaluator", "Company B", _requirements(), url=url),
    ], as_of=AS_OF)
    assert len(result.evaluated) == 1
    assert len(result.evaluated[0].canonical.observations) == 2
    assert result.counts["duplicate_observations_collapsed"] == 1


def test_closed_page_body_is_detected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            text="This job is no longer available. Applications are closed.",
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await resolve_url(
                "https://jobs.example.com/closed/1",
                client=client,
                inspect_body=True,
            )

    assert asyncio.run(exercise()).error == "closed_listing"


def test_adzuna_429_opens_run_circuit_breaker(monkeypatch):
    result = evaluate_raw([
        _serp(
            f"Bengali AI Content Quality Evaluator {index}",
            _requirements("Native Bengali required."),
            f"https://www.adzuna.in/details/{index}",
        )
        for index in range(2)
    ], as_of=AS_OF)
    calls = 0
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, request=request)

    monkeypatch.setattr(
        "jobhound.v41.resolve.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    asyncio.run(resolve_result_urls(result))
    errors = {
        item.canonical.best_observation.resolution_error
        for item in result.evaluated
    }
    assert calls == 1
    assert errors == {"http_429", "http_429_circuit_open"}


def test_low_trust_high_onboarding_platform_gets_explicit_penalty():
    item = evaluate_raw([
        _jsearch("Bengali AI Evaluator", "Welo Data", _requirements())
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.priority_components["risk_adjustment"] < 0
    assert "high_unpaid_onboarding_risk" in item.decision.watch_outs


def test_remoteok_ai_supermarket_catalog_is_not_a_job():
    item = evaluate_raw([{
        "source": "remoteok",
        "raw": {
            "position": "Aragon AI",
            "company": "AI Supermarket",
            "url": "https://remoteok.com/remote-jobs/remote-aragon-ai-ai-supermarket-1",
            "description": "A catalog of products that transcribes calls and summarizes meetings.",
            "location": "Worldwide",
            "date": "2026-08-11T16:50:40Z",
        },
    }], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_source_quality"


def test_wfo_marker_in_title_overrides_remote_provider_flag():
    item = evaluate_raw([
        _jsearch(
            "AI ML Trainer Mohali WFO Work from office Locals only",
            "Local Employer",
            _requirements(),
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_location"


def test_inside_sales_title_is_rejected_without_needing_body_confirmation():
    item = evaluate_raw([
        _jsearch(
            "Inside Sales Specialists-CRM Workflow software automation App",
            "The Search House",
            "Build relationships with prospective customers and support the sales team.",
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_role"


def test_generic_ai_engineer_body_does_not_create_automation_lane():
    item = evaluate_raw([
        _jsearch(
            "AI Engineer",
            "OX Consultancy",
            "Build Python LLM systems, REST APIs, integrations, RAG, and vector databases.",
        )
    ], as_of=AS_OF).evaluated[0]
    assert "workflow_automation" not in item.assessment.matched_concepts


def test_power_platform_title_is_still_an_automation_opportunity():
    item = evaluate_raw([
        _jsearch(
            "Power Platform Developer",
            "Employer",
            "Build Microsoft Power Platform applications for business workflows.",
        )
    ], as_of=AS_OF).evaluated[0]
    assert "workflow_automation" in item.assessment.matched_concepts


def test_aerodynamic_engineer_requires_unclaimed_credentials():
    item = evaluate_raw([
        _jsearch(
            "CFD & Aerodynamic Engineer (AI Training & Evaluation)",
            "Lifted",
            _requirements(),
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_credentials"


def test_telus_opportunities_index_is_not_an_individual_job():
    item = evaluate_raw([
        _serp(
            "TELUS Digital AI Jobs | Your next opportunity in AI starts here",
            "Browse AI training and annotation opportunities.",
            "https://www.telusinternational.ai/opportunities",
        )
    ], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_source_quality"


def test_resolved_404_is_rejected_as_an_inactive_listing():
    record = _serp(
        "Bengali AI Data Annotator - Remote",
        _requirements("Native Bengali required."),
        "https://jobs.example.com/listing/gone",
    )
    record["_v41_resolution"] = {
        "normalized_url": "https://jobs.example.com/listing/gone",
        "source_kind": "unknown",
        "source_confidence": 0.55,
        "redirect_trail": [],
        "attempted": True,
        "error": "http_404",
    }
    item = evaluate_raw([record], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_source_quality"


def test_low_trust_penalty_changes_rank_order_not_only_displayed_score():
    welo = evaluate_raw([_jsearch(
        "Bengali AI Evaluator",
        "Welo Data",
        _requirements(),
    )], as_of=AS_OF).evaluated[0]
    neutral = evaluate_raw([_jsearch(
        "Bengali AI Evaluator",
        "Unknown Employer",
        _requirements(),
    )], as_of=AS_OF).evaluated[0]
    assert welo.decision.rank_key[4] < neutral.decision.rank_key[4]


def test_automation_digest_uses_one_slot_per_counterparty():
    records = []
    variants = (
        ("Thermo Fisher Scientific", "Bangalore"),
        ("ThermoFisher Scientific", "Gurugram"),
    )
    for index, (company, city) in enumerate(variants, start=1):
        records.append(_jsearch(
            "Workflow Automation Developer",
            company,
            "Build workflow automation integrations and Power Platform apps.",
            url=f"https://jobs.example.com/thermo/{index}",
            remote=False,
            city=city,
        ))
    records.append(_jsearch(
        "n8n WhatsApp Workflow Automation Specialist",
        "Independent Studio",
        "Build n8n integrations, webhooks, and WhatsApp automations.",
        url="https://jobs.example.com/independent/1",
    ))
    result = evaluate_raw(records, as_of=AS_OF)
    digest = build_digest(
        result,
        include_all=True,
        min_priority=0,
        top_n=5,
        per_company_cap=2,
    )
    assert digest.text.count("Workflow Automation Developer") == 1
    assert "n8n WhatsApp Workflow Automation Specialist" in digest.text
    assert digest.display_counts["hidden_company_cap"] == 1
