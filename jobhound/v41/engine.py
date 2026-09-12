"""Pure V4.1 evaluation engine and deterministic accounting."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..config import CONFIG
from ..enrich.confidence import score_confidence
from ..enrich.pay_gate import apply_pay_gate
from ..enrich.trust import enrich_trust, freshness
from ..filters.accessibility import easy_entry
from ..filters.eligibility import (
    _title_language_requirements,
    _title_language_pairs,
    check as eligibility_check,
    credential_gate,
    default_profile,
)
from ..filters.listing_quality import junk_check, location_gate, region_lock_check
from ..filters.posting_language import check as posting_language_check
from ..filters.scam import is_scam, score_scam
from ..filters.scam_llm import second_pass as scam_llm_pass
from ..trust.registry import default_registry
from . import ENGINE_VERSION
from .extract import detect_concepts, extract_pay_candidates, extract_sections
from .models import (
    ActionBand,
    Assessment,
    CanonicalJob,
    Decision,
    EconomicsBand,
    EligibilityStatus,
    EvaluatedJob,
    Evidence,
    MatchStrength,
    PayState,
    PriorityKey,
    RunMetadata,
    RunResult,
    SourceKind,
)
from .provenance import build_observations, canonicalize_observations
from .requirements import (
    assess_requirements,
    experience_mismatches,
    language_mismatches,
    location_mismatches,
    preferred_experience_watchouts,
    requirement_outcomes,
)
from .roles import assess_role, specialist_blockers

_ROOT = Path(__file__).resolve().parent.parent.parent
_MANAGEMENT = re.compile(
    r"\b(?:manager|leader|director|chief|vice\s+president|vp|"
    r"delivery\s+lead|team\s+lead|program\s+manager|project\s+manager)\b|"
    r"\bhead\s+of\b|\bhead\b(?=\s*(?:$|[,\-|:;()\[\]/]))",
    re.IGNORECASE,
)
_SENIOR = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|architect|founding)\b|"
    r"\blead\b(?!\s+generation\b)",
    re.IGNORECASE,
)
_REQUIRED_YEARS = re.compile(
    r"\b(\d+)(?:\s*(?:-|–|—|to)\s*\d+)?\s*\+?\s+years?"
    r"(?:\s+of)?\s+(?:relevant\s+)?(?:professional\s+)?(?:commercial\s+)?"
    r"(?:experience|building|developing|working)\b",
    re.IGNORECASE,
)
_SALES_ROLE_TITLE = re.compile(
    r"\b(?:inside sales|sales (?:specialist|representative)|account executive|"
    r"business development|market research)\b",
    re.IGNORECASE,
)
_OUTBOUND_SALES_WORK = re.compile(
    r"\b(?:cold (?:email|calling)|finding contacts|lead generation|prospecting|"
    r"sales enablement|sending emails|pipeline growth)\b",
    re.IGNORECASE,
)
_SPECIALIST_TITLE = re.compile(
    r"\b(?:expert|specialist|scientist|researcher|consultant)\b",
    re.IGNORECASE,
)
_TARGET_ROLE_CONCEPTS = {
    "language_ai", "ai_evaluation", "annotation", "transcription",
    "workflow_automation",
}
_DELIVERABLE_TECH_TITLE = re.compile(
    r"\b(?:web\s+scrap(?:e|er|ing)|data\s+clean(?:er|ing)|spreadsheet|etl|"
    r"pdf\s+(?:data\s+)?extraction|data\s+extraction|api\s+integration)\b", re.I,
)
_ACTIONABLE_PRIMARY = {
    SourceKind.EMAIL_OFFER,
    SourceKind.ORIGINAL_EMPLOYER,
    SourceKind.ORIGINAL_ATS,
    SourceKind.REPUTABLE_BOARD,
}
_SOURCE_ORDER = {
    SourceKind.ORIGINAL_EMPLOYER: 6,
    SourceKind.ORIGINAL_ATS: 6,
    SourceKind.EMAIL_OFFER: 6,
    SourceKind.REPUTABLE_BOARD: 5,
    SourceKind.AGGREGATOR_UNRESOLVED: 3,
    SourceKind.UNKNOWN: 2,
    SourceKind.CONTENT_FARM: 0,
}
_MATCH_ORDER = {MatchStrength.STRONG: 2, MatchStrength.MEDIUM: 1, MatchStrength.WEAK: 0}
_ECON_ORDER = {
    EconomicsBand.EXCELLENT: 3,
    EconomicsBand.VIABLE: 2,
    EconomicsBand.BUDGET_KNOWN: 2,
    EconomicsBand.UNKNOWN: 1,
    EconomicsBand.BELOW_FLOOR: 0,
}


def _file_hash(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError:
        content = b""
    return hashlib.sha256(content).hexdigest()[:16]


def _combined_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:16]


def _metadata(as_of: datetime | None = None) -> RunMetadata:
    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    v41_dir = Path(__file__).resolve().parent
    ruleset_hash = _combined_hash(sorted(v41_dir.glob("*.py")))
    def policy_path(name: str) -> Path:
        path = _ROOT / name
        return path if path.exists() else path.with_name(path.stem + ".example.yaml")

    profile_hash = _file_hash(policy_path("profile.yaml"))
    trust_hash = _file_hash(_ROOT / "platform_registry.yaml")
    config_hash = _file_hash(policy_path("config.yaml"))
    if CONFIG.v55.enabled:
        # Runtime review overrides are part of the policy, not invisible changes
        # to a file-only hash. No environment variables or credentials are read.
        config_hash = hashlib.sha256(json.dumps(CONFIG.model_dump(mode='json'), sort_keys=True, default=str).encode('utf-8')).hexdigest()[:16]
    run_seed = f"{now.isoformat()}|{ruleset_hash}|{profile_hash}|{config_hash}"
    run_id = hashlib.sha256(run_seed.encode("utf-8")).hexdigest()[:20]
    return RunMetadata(
        engine_version="v5.0.0-rc1" if CONFIG.v55.enabled else ENGINE_VERSION,
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        as_of=now,
        ruleset_hash=ruleset_hash,
        profile_hash=profile_hash,
        trust_registry_hash=trust_hash,
        config_hash=config_hash,
    )


def _pay_to_job(canonical: CanonicalJob, assessment: Assessment) -> None:
    job = canonical.job
    selected = assessment.selected_pay
    if selected is None:
        job.pay_min_hourly_usd = None
        job.pay_max_hourly_usd = None
        job.pay_basis = "unknown"
        job.pay_source = "unknown"
        return
    if selected.basis == "fixed":
        job.pay_raw = selected.raw
        job.pay_min_hourly_usd = None
        job.pay_max_hourly_usd = None
        job.pay_basis = "fixed"
        job.pay_is_estimate = False
        job.pay_source = selected.source_field
        return
    job.pay_raw = selected.raw
    job.pay_min_hourly_usd = selected.min_hourly_usd
    job.pay_max_hourly_usd = selected.max_hourly_usd
    job.pay_basis = selected.basis
    job.pay_is_estimate = selected.estimated
    job.pay_source = selected.source_field
    job.guaranteed_hours = selected.guaranteed


def _trust(assessment: Assessment, canonical: CanonicalJob) -> None:
    job = canonical.job
    source_kind = canonical.best_observation.source_kind
    if job.platform_trust is not None:
        assessment.platform_trust_estimate = job.platform_trust
        assessment.platform_trust_confidence = 1.0
        assessment.conservative_trust = job.platform_trust
        return
    estimate = CONFIG.v41.source_trust_priors.get(source_kind.value, 0.5)
    confidence = CONFIG.v41.source_trust_confidence.get(source_kind.value, 0.15)
    conservative = max(
        0.0,
        estimate - CONFIG.v41.trust_uncertainty_penalty * (1.0 - confidence),
    )
    assessment.platform_trust_estimate = round(estimate, 3)
    assessment.platform_trust_confidence = round(confidence, 3)
    assessment.conservative_trust = round(conservative, 3)


def _compensation_credibility(
    canonical: CanonicalJob,
    assessment: Assessment,
) -> None:
    selected = assessment.selected_pay
    if selected is None:
        assessment.pay_credibility = 0.0
        assessment.pay_rate_for_ranking = None
        return

    assessment.pay_credibility = selected.claim_credibility
    if selected.basis == "fixed" or (CONFIG.v55.enabled and not selected.labor_hourly_supported):
        assessment.pay_rate_for_ranking = None
        return
    if CONFIG.v55.enabled:
        assessment.pay_rate_for_ranking = selected.min_hourly_usd or selected.max_hourly_usd
        return
    assessment.pay_rate_for_ranking = (
        canonical.job.effective_hourly_usd
        if canonical.job.effective_hourly_usd is not None
        else selected.min_hourly_usd or selected.max_hourly_usd
    )


def _sync_pay_assessment(assessment: Assessment) -> None:
    selected = assessment.selected_pay
    if selected is None:
        state = "unknown"
    elif assessment.pay_conflict:
        state = "conflicted"
    elif assessment.economics_band == EconomicsBand.BELOW_FLOOR:
        state = "below_floor"
    elif selected.basis == "fixed":
        state = "budget_known"
    elif selected.up_to:
        state = "up_to_only"
    elif selected.basis == "year":
        state = "credible_salary"
    elif assessment.pay_credibility >= CONFIG.v41.minimum_economics_credibility:
        state = "credible_hourly"
    else:
        state = "known_unverified"
    assessment.pay_assessment = assessment.pay_assessment.model_copy(update={
        "state": PayState(state),
        "candidates": assessment.pay_candidates,
        "selected": selected,
        "selected_candidate_id": (
            f"{selected.observation_id}:{selected.source_field}"
            if selected is not None else None
        ),
        "credibility": assessment.pay_credibility,
        "conflict": assessment.pay_conflict,
        "ranking_value": assessment.pay_rate_for_ranking,
        "ranking_rate": assessment.pay_rate_for_ranking,
        "conflict_reasons": (
            ["pay_basis_or_value_conflict"] if assessment.pay_conflict else []
        ),
    })


def _match(
    canonical: CanonicalJob,
    assessment: Assessment,
) -> None:
    profile = default_profile()
    sections = extract_sections(canonical.job.description)
    concepts, concept_evidence = detect_concepts(canonical.job.title, sections, profile)
    assessment.matched_concepts = concepts
    assessment.concept_evidence = concept_evidence
    assessment.role_families = list(concepts)
    assessment.role_assessment = assess_role(
        canonical, concepts, concept_evidence, profile
    )

    # V3's historical ``bengali`` region bucket also contains generic
    # "multilingual" descriptions. V4.1 uses explicit concept evidence so that
    # multilingual dataset boilerplate cannot manufacture a language edge.
    language_edge = "language_ai" in concepts
    non_language = [concept for concept in concepts if concept != "language_ai"]
    if language_edge and non_language:
        assessment.match_strength = MatchStrength.STRONG
    elif len(concepts) >= 2:
        assessment.match_strength = MatchStrength.STRONG
    elif concepts:
        assessment.match_strength = MatchStrength.MEDIUM
    else:
        assessment.match_strength = MatchStrength.WEAK

    if assessment.management_scope:
        assessment.role_families.append("management")
    if assessment.domain_requirements:
        assessment.role_families.append("specialist")
    assessment.role_families = list(dict.fromkeys(assessment.role_families))

    accessible, access_reasons = easy_entry(canonical.job.title, sections.scoped_text)
    assessment.easy_entry = accessible
    canonical.job.easy_entry = accessible
    for reason in access_reasons:
        assessment.evidence.append(Evidence(
            dimension="accessibility",
            code=reason.split(":", 1)[0],
            value=reason,
            source_field="description",
            span=reason,
            confidence=0.9,
        ))
    for concept, location in concept_evidence.items():
        assessment.evidence.append(Evidence(
            dimension="match",
            code=f"concept:{concept}",
            value=concept,
            source_field=location.split(":", 1)[0],
            span=location.split(":", 1)[-1],
            confidence=0.9 if location.startswith("title:") else 0.75,
        ))


def _assess(canonical: CanonicalJob, as_of: datetime) -> Assessment:
    job = canonical.job
    profile = default_profile()
    registry = default_registry()
    assessment = Assessment(source_actionability=canonical.best_observation.source_kind)

    candidates, selected, pay_conflict = extract_pay_candidates(canonical)
    assessment.pay_candidates = candidates
    assessment.selected_pay = selected
    assessment.pay_conflict = pay_conflict
    _pay_to_job(canonical, assessment)

    enrich_trust(job, registry, compute_effective_rate=False)
    score_confidence(job, now=as_of)
    score_scam(job)
    if CONFIG.v55.enabled:
        # The release hourly floor only applies to supported labor-hour amounts.
        # Trust, task availability and registry overhead are not wage evidence.
        job.effective_hourly_usd = None
        job.pay_ok = None
        if selected and selected.labor_hourly_supported and not selected.estimated:
            upper = selected.max_hourly_usd or selected.min_hourly_usd
            if upper is not None and not selected.up_to:
                job.pay_ok = upper >= CONFIG.pay.floor_usd
    else:
        apply_pay_gate(job, registry, CONFIG.pay, conservative=True)
    _trust(assessment, canonical)
    _compensation_credibility(canonical, assessment)

    requirements_assessment = assess_requirements(canonical, profile)
    assessment.requirements_assessment = requirements_assessment
    # Some aggregators concatenate a US state name with the ISO country code
    # ``IN`` (India), while their retained URL identifies a US city/state.  A
    # URL slug is not applicant eligibility evidence; retain the contradiction
    # as a high-priority verification fact instead of silently choosing either.
    evidence_urls = [job.url, *[
        observation.job.url for observation in canonical.observations
        if observation.job is not None
    ]]
    if CONFIG.v55.enabled and re.search(r",\s*IN\s*$", job.location or "", re.I) and any(re.search(
        r"-(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy)--",
        urlsplit(evidence_url or "").path,
        re.I,
    ) for evidence_url in evidence_urls):
        assessment.unresolved.append("location_scope_conflict:metadata_country_vs_url")
    assessment.language_requirements = requirements_assessment.language_required
    _, eligibility_reasons = eligibility_check(job.title, profile)
    credential_reasons = [
        reason for reason in eligibility_reasons
        if reason.startswith("credential_mismatch:")
    ]
    sections = extract_sections(job.description)
    requirements = sections.requirements
    credential_reasons = list(dict.fromkeys([
        *credential_reasons,
        *credential_gate(requirements, profile),
    ]))
    experience_reasons = experience_mismatches(
        requirements_assessment,
        profile.max_required_years,
    )
    if CONFIG.v55.enabled:
        outcomes = requirement_outcomes(requirements_assessment, profile)
        # Scoped claims replace the old whole-section bag-of-words gate.
        credential_reasons = [r for r in outcomes.blockers if r.startswith('credential_mismatch:')]
        experience_reasons = [r for r in outcomes.blockers if r.startswith('experience_mismatch:')]
        assessment.unresolved.extend(outcomes.unverified)
        demanded_profession = [r for r in outcomes.blockers if r.startswith('required_profession_outside_profile:')]
        if demanded_profession:
            # A demanded profession outside the requested work lanes is a role
            # mismatch. This does not assert an unknown licence is absent.
            assessment.unresolved.extend(['outside_target_scope', *demanded_profession])
    title_language_reasons = [
        reason for reason in eligibility_reasons
        if reason.startswith("lang_mismatch:")
    ]
    requirement_language_reasons = language_mismatches(
        requirements_assessment,
        profile,
    )
    assessment.domain_requirements = sorted({
        reason.split(":", 2)[1] for reason in credential_reasons
    })

    location_reasons = [
        *location_gate(
            job.is_remote,
            job.region_tags,
            job.location,
            job.description,
            title=job.title,
        ),
        *region_lock_check(job.title, f"{job.location or ''}\n{job.description}"),
        *location_mismatches(requirements_assessment),
    ]
    if CONFIG.v55.enabled:
        if 'rejected_location:unknown' in location_reasons:
            location_reasons.remove('rejected_location:unknown')
            assessment.unresolved.append('location_scope_unknown')
        if not requirements_assessment.location_scope and not job.location:
            assessment.unresolved.append('location_scope_unknown')
    if (
        job.is_remote
        and job.location
        and not requirements_assessment.location_scope
        and not re.fullmatch(
            r"\s*(?:remote|anywhere|worldwide|global|work from home)\s*",
            job.location,
            re.IGNORECASE,
        )
    ):
        assessment.unresolved.append("location_scope_unknown")
    posting_language_reasons = posting_language_check(
        job.title, job.description, profile.languages
    )
    language_reasons = list(dict.fromkeys([
        *requirement_language_reasons,
        *title_language_reasons,
        *posting_language_reasons,
    ]))
    if CONFIG.v55.enabled:
        language_reasons = [r for r in outcomes.blockers if r.startswith('lang_mismatch:')]
        # Posting language/gender markers cannot establish working-language demands.
        if posting_language_reasons:
            assessment.unresolved.append('posting_language_interpretation')

    quality_reasons = junk_check(
        job.title,
        job.url,
        job.company,
        has_rate=selected is not None,
        company_domain=job.company_domain,
    )
    if canonical.best_observation.source_kind == SourceKind.CONTENT_FARM:
        marker = f"source_kind:{SourceKind.CONTENT_FARM.value}"
        if marker not in quality_reasons:
            quality_reasons.append(marker)
    if not CONFIG.v55.enabled and any(
        observation.resolution_error in {
            "closed_listing",
            "posting_not_found",
            "http_404",
            "http_410",
        }
        for observation in canonical.observations
    ):
        marker = "closed_or_inactive"
        if marker not in quality_reasons:
            quality_reasons.append(marker)

    management_match = _MANAGEMENT.search(job.title)
    senior_match = _SENIOR.search(job.title)
    assessment.management_scope = management_match is not None
    if management_match:
        assessment.seniority = "management"
    elif senior_match:
        assessment.seniority = "senior"
    else:
        assessment.seniority = "unspecified"
    seniority_reasons = []
    scoped_marketplace_gig = (
        "upwork.com/freelance-jobs/apply/" in (job.url or "").casefold()
    )
    if assessment.seniority in {"senior", "management"}:
        if scoped_marketplace_gig:
            assessment.unresolved.append(
                f"seniority_scope_review:{assessment.seniority}"
            )
        else:
            seniority_reasons.append(f"seniority_mismatch:{assessment.seniority}")

    if location_reasons:
        assessment.work_format = "region_locked"
    elif job.is_remote or "worldwide" in job.region_tags:
        assessment.work_format = "remote"
    elif "india" in job.region_tags:
        assessment.work_format = "india"
    elif job.location:
        assessment.work_format = "onsite_or_local"
    else:
        assessment.work_format = "unknown"

    _match(canonical, assessment)
    if job.title_truncated:
        assessment.unresolved.append("title_truncated")
        if job.title_reconstructed:
            assessment.unresolved.append("title_reconstructed_low_confidence")
    if job.company and 0.0 < job.company_confidence < 0.70:
        assessment.unresolved.append("company_identity_uncertain")

    if job.pay_ok is False:
        assessment.economics_band = EconomicsBand.BELOW_FLOOR
    elif selected is None:
        assessment.economics_band = EconomicsBand.UNKNOWN
    elif selected.basis == "fixed":
        assessment.economics_band = (
            EconomicsBand.BUDGET_KNOWN
            if assessment.pay_credibility >= CONFIG.v41.minimum_economics_credibility
            and assessment.match_strength != MatchStrength.WEAK
            else EconomicsBand.UNKNOWN
        )
    elif assessment.pay_credibility < CONFIG.v41.minimum_economics_credibility:
        assessment.economics_band = EconomicsBand.UNKNOWN
    elif CONFIG.v55.enabled and not selected.labor_hourly_supported:
        assessment.economics_band = EconomicsBand.UNKNOWN
    elif selected.up_to:
        assessment.economics_band = EconomicsBand.VIABLE
    elif (assessment.pay_rate_for_ranking or 0.0) >= 15.0:
        assessment.economics_band = EconomicsBand.EXCELLENT
    else:
        assessment.economics_band = EconomicsBand.VIABLE
    if job.posted_at is None and job.source == "serp":
        assessment.freshness = CONFIG.v41.thin_source_unknown_freshness
    else:
        half_life = (
            CONFIG.ranking.easy_entry_half_life_days
            if assessment.easy_entry
            else CONFIG.ranking.freshness_half_life_days
        )
        assessment.freshness = round(
            freshness(job.posted_at, now=as_of, half_life_days=half_life),
            4,
        )
    assessment.role_assessment.seniority = assessment.seniority
    assessment.role_assessment.management_scope = assessment.management_scope
    assessment.evidence.extend(assessment.role_assessment.evidence)
    credential_reasons = list(dict.fromkeys([
        *credential_reasons,
        *specialist_blockers(assessment.role_assessment),
    ]))
    if CONFIG.v55.enabled:
        # An undeclared specialist capability is outside this target surface,
        # not a factual assertion that the operator lacks a qualification.
        specialist_reasons = specialist_blockers(assessment.role_assessment)
        credential_reasons = [r for r in credential_reasons if r not in specialist_reasons]
        if specialist_reasons:
            assessment.unresolved.append('outside_target_scope')
    if assessment.role_assessment.specialist_domain_status == "unresolved":
        assessment.unresolved.append("specialist_domain_unresolved")
    assessment.domain_requirements = sorted({
        reason.split(":", 2)[1] for reason in credential_reasons
    })

    scam_reasons = ["scam_threshold"] if is_scam(job) else []
    trust_reasons = (
        ["trust_below_floor"]
        if job.platform_trust is not None and job.platform_trust < CONFIG.trust.hard_floor
        else []
    )
    pay_reasons = ["pay_below_floor"] if job.pay_ok is False else []
    sales_leak = _SALES_ROLE_TITLE.search(job.title) is not None
    concepts = set(assessment.matched_concepts)
    generic_technical = (
        bool(concepts)
        and not concepts.intersection(_TARGET_ROLE_CONCEPTS)
        and _DELIVERABLE_TECH_TITLE.search(job.title) is None
    )
    role_reasons = (
        ["role_mismatch"]
        if (
            assessment.match_strength == MatchStrength.WEAK
            or sales_leak
            or generic_technical
        )
        else []
    )
    if CONFIG.v55.enabled and 'outside_target_scope' in assessment.unresolved:
        role_reasons = ['role_mismatch']
    low_evidence = (
        assessment.match_strength == MatchStrength.WEAK
        and len((job.description or "").strip()) < 200
        and not sales_leak
    )

    assessment.blockers = [
        *scam_reasons,
        *[f"source_quality:{reason}" for reason in quality_reasons],
        *[f"location:{reason}" for reason in location_reasons],
        *[f"language:{reason}" for reason in language_reasons],
        *[f"credentials:{reason}" for reason in credential_reasons],
        *[f"credentials:{reason}" for reason in experience_reasons],
        *seniority_reasons,
        *pay_reasons,
        *trust_reasons,
        *([] if low_evidence else role_reasons),
        *(["low_evidence"] if low_evidence else []),
    ]

    technical_only = (
        bool(assessment.matched_concepts)
        and set(assessment.matched_concepts) <= {
            "data_automation", "workflow_automation", "software"
        }
    )
    if not assessment.blockers:
        completeness = assessment.requirements_assessment.completeness
        conventional_occupation = assessment.role_assessment.occupation_family in {
            "software_engineer", "data_engineer", "qa", "analyst", "researcher",
        }
        if completeness != "complete" and _SPECIALIST_TITLE.search(job.title):
            assessment.unresolved.append("specialist_requirements_unverified")
        if assessment.role_assessment.specialist_domain_status == "unresolved":
            assessment.eligibility = EligibilityStatus.UNKNOWN
        elif (
            (technical_only or conventional_occupation)
            and completeness != "complete"
            and not assessment.easy_entry
        ):
            assessment.eligibility = EligibilityStatus.UNKNOWN
            assessment.unresolved.append("technical_requirements_review")
        elif completeness != "complete":
            assessment.eligibility = EligibilityStatus.UNKNOWN
            assessment.unresolved.append("requirements_not_explicit")
        else:
            assessment.eligibility = EligibilityStatus.PASSED
    else:
        assessment.eligibility = EligibilityStatus.FAILED
    assessment.unresolved.extend(preferred_experience_watchouts(
        requirements_assessment, profile.max_required_years
    ))
    if pay_conflict:
        assessment.unresolved.append("pay_conflict")
    if selected is not None and selected.basis == "fixed":
        assessment.unresolved.append("fixed_price_scope_review")
    if selected is None:
        assessment.unresolved.append("pay_unknown")
    elif (
        (selected.max_hourly_usd or selected.min_hourly_usd or 0.0)
        >= CONFIG.v41.unverified_high_pay_usd
        and assessment.pay_credibility < CONFIG.v41.minimum_economics_credibility
    ):
        assessment.unresolved.append("pay_unverified_high")
    if assessment.source_actionability in {
        SourceKind.AGGREGATOR_UNRESOLVED, SourceKind.UNKNOWN
    }:
        assessment.unresolved.append(f"source:{assessment.source_actionability.value}")
    resolution_errors = sorted({
        observation.resolution_error
        for observation in canonical.observations
        if observation.resolution_error
    })
    assessment.unresolved.extend(
        f"source_resolution:{error}" for error in resolution_errors
    )
    if assessment.platform_trust_confidence < 1.0:
        assessment.unresolved.append("platform_trust_uncertain")
    platform_record = registry.get(job.platform_key or "")
    if job.platform_trust is not None and job.platform_trust < 0.50:
        assessment.unresolved.append("low_platform_trust")
    if (
        platform_record is not None
        and platform_record.onboarding_cost < 0.40
    ):
        assessment.unresolved.append("high_unpaid_onboarding_risk")
    if job.posted_at is None and job.source == "serp":
        assessment.unresolved.append("posting_age_unknown")

    for claim in requirements_assessment.claims:
        assessment.evidence.append(Evidence(
            dimension=claim.dimension,
            code=f"requirement:{claim.dimension}",
            value=claim.value,
            source_field=claim.source_field,
            observation_id=claim.observation_id,
            span=claim.span,
            modality=claim.modality,
            confidence=claim.confidence,
        ))
    _sync_pay_assessment(assessment)
    if CONFIG.v55.enabled:
        from .action_policy import apply_action_policy
        if canonical.best_observation.work_arrangement.state == 'conflicting':
            assessment.unresolved.append('location_scope_conflict:work_arrangement')
        apply_action_policy(canonical, assessment, as_of)

    # Evidence spans for hard decisions remain separate from match evidence.
    for reason in assessment.blockers:
        dimension = reason.split(":", 1)[0]
        if dimension in {"credentials", "language"}:
            continue
        assessment.evidence.append(Evidence(
            dimension=dimension,
            code=reason,
            value=reason,
            source_field="title" if dimension in {"credentials", "seniority", "language"} else "job",
            span=job.title if dimension in {"credentials", "seniority", "language"} else "",
            confidence=1.0,
        ))
    if CONFIG.v55.enabled:
        from .evidence_dimensions import describe_evidence
        assessment.evidence_dimensions = describe_evidence(canonical, assessment)
    return assessment


def _role_priority(assessment: Assessment) -> int:
    priorities = default_profile().role_priorities
    occupation_role = {
        "software_engineer": "software",
        "qa": "software",
        "data_engineer": "data_automation",
        "analyst": "data_automation",
        "researcher": "data_automation",
        "automation_contractor": "workflow_automation",
        "language_specialist": "language_ai",
        "transcriber": "transcription",
        "annotator": "annotation",
    }.get(assessment.role_assessment.occupation_family)
    if occupation_role is not None:
        return priorities.get(occupation_role, 0)
    return max(
        (priorities.get(role, 0) for role in assessment.role_families),
        default=0,
    )


def _explicit_profile_language_edge(assessment: Assessment) -> int:
    """Reward an explicit operator language, never generic multilingual copy."""
    profile_languages = {
        str(language).casefold()
        for language in default_profile().languages
        if str(language).casefold() not in {"multilingual", "any language"}
    }
    for claim in assessment.requirements_assessment.languages:
        if CONFIG.v55.enabled and claim.modality not in {'mandatory', 'minimum', 'required'}:
            continue
        values = claim.value if isinstance(claim.value, list) else [claim.value]
        if any(str(value).casefold() in profile_languages for value in values):
            return 1
    return 0


def _accessibility_confidence(assessment: Assessment) -> int:
    completeness = {
        "unknown": 0,
        "partial": 1,
        "complete": 2,
    }.get(assessment.requirements_assessment.completeness, 0)
    if assessment.eligibility == EligibilityStatus.PASSED:
        completeness = max(completeness, 2)
    if assessment.easy_entry:
        completeness = min(3, completeness + 1)
    return completeness


def _evaluation_sort_key(item: EvaluatedJob) -> tuple:
    band = {
        ActionBand.PRIMARY: 0,
        ActionBand.VERIFY: 1,
        ActionBand.REJECT: 2,
    }[item.decision.action_band]
    return (band, *item.decision.priority_key.sort_tuple)


def _decision(canonical: CanonicalJob, assessment: Assessment) -> Decision:
    blockers = assessment.blockers
    precedence = [
        ("reject_scam", lambda item: item == "scam_threshold"),
        ("reject_source_quality", lambda item: item.startswith("source_quality:")),
        ("reject_source_quality", lambda item: item == 'deadline_infeasible'),
        ("reject_location", lambda item: item.startswith("location:")),
        ("reject_language", lambda item: item.startswith("language:")),
        ("reject_credentials", lambda item: item.startswith("credentials:")),
        ("reject_seniority", lambda item: item.startswith("seniority_mismatch:")),
        ("reject_pay", lambda item: item == "pay_below_floor"),
        ("reject_trust", lambda item: item == "trust_below_floor"),
        ("reject_role", lambda item: item == "role_mismatch"),
        ("reject_low_evidence", lambda item: item == "low_evidence"),
    ]
    terminal_reason = ""
    for reason, predicate in precedence:
        if any(predicate(item) for item in blockers):
            terminal_reason = reason
            break

    if terminal_reason:
        action_band = ActionBand.REJECT
    else:
        direct_unknown_primary = (
            CONFIG.v41.allow_direct_unknown_primary
            and assessment.eligibility == EligibilityStatus.UNKNOWN
            and assessment.match_strength == MatchStrength.STRONG
            and assessment.source_actionability in {
                SourceKind.ORIGINAL_EMPLOYER,
                SourceKind.ORIGINAL_ATS,
            }
            and not assessment.pay_conflict
            and set(assessment.unresolved) <= {
                "requirements_not_explicit",
                "pay_unknown",
                "platform_trust_uncertain",
                "posting_age_unknown",
            }
        )
        primary_eligible = (
            assessment.eligibility == EligibilityStatus.PASSED
            or direct_unknown_primary
        )
    if not terminal_reason and (
        primary_eligible
        and assessment.match_strength in {MatchStrength.STRONG, MatchStrength.MEDIUM}
        and assessment.source_actionability in _ACTIONABLE_PRIMARY
        and not assessment.pay_conflict
        and "title_truncated" not in assessment.unresolved
        and "location_scope_unknown" not in assessment.unresolved
    ):
        action_band = ActionBand.PRIMARY
        terminal_reason = "surface_primary"
    elif not terminal_reason:
        action_band = ActionBand.VERIFY
        terminal_reason = "surface_verify"

    if CONFIG.v55.enabled and action_band != ActionBand.REJECT:
        ready = (
            assessment.eligibility == EligibilityStatus.PASSED
            and assessment.action_readiness in {'application_ready', 'allocated', 'captured_next_step'}
            and assessment.lifecycle == 'active'
            and not assessment.verification_tasks
            and not assessment.pay_conflict
        )
        action_band = ActionBand.PRIMARY if ready else ActionBand.VERIFY
        terminal_reason = 'surface_primary' if ready else 'surface_verify'

    role_priority = _role_priority(assessment)
    language_edge = _explicit_profile_language_edge(assessment)
    accessibility_confidence = _accessibility_confidence(assessment)
    platform_record = default_registry().get(canonical.job.platform_key or "")
    risk_adjustment = 0.0
    if (
        canonical.job.platform_trust is not None
        and canonical.job.platform_trust < 0.50
    ):
        risk_adjustment -= CONFIG.v41.low_trust_priority_penalty
    if (
        platform_record is not None
        and platform_record.onboarding_cost < 0.40
    ):
        risk_adjustment -= CONFIG.v41.high_onboarding_priority_penalty
    priority_key = PriorityKey(
        policy_version='v5.0.0-rc1' if CONFIG.v55.enabled else 'v4.2',
        action_readiness={'allocated': 3, 'captured_next_step': 3, 'application_ready': 2, 'verification_needed': 1}.get(assessment.action_readiness, 0),
        # Monotonic ordering encoding only, not a probability or earnings score.
        supported_time_to_cash=1 / (1 + assessment.time_to_cash_days) if assessment.time_to_cash_days is not None else 0,
        action_cost_and_friction=int(assessment.action_cost_acceptable),
        role_priority=role_priority,
        explicit_profile_language_edge=language_edge,
        match_strength=_MATCH_ORDER[assessment.match_strength],
        accessibility_confidence=accessibility_confidence,
        economics_quality=_ECON_ORDER[assessment.economics_band],
        source_actionability=_SOURCE_ORDER[assessment.source_actionability],
        conservative_trust=round(
            assessment.conservative_trust * 100 + risk_adjustment
        ),
        freshness=round(assessment.freshness * 100),
        stable_tiebreaker=canonical.canonical_id,
    )
    # Compatibility projection only. New ordering uses priority_key.
    rank_key = (
        role_priority,
        _MATCH_ORDER[assessment.match_strength],
        language_edge,
        _SOURCE_ORDER[assessment.source_actionability],
        round(assessment.conservative_trust * 100 + risk_adjustment),
        _ECON_ORDER[assessment.economics_band],
        round(assessment.freshness * 100),
    )
    components = {
        "role_priority": float(role_priority * 5),
        "match": float({MatchStrength.STRONG: 30, MatchStrength.MEDIUM: 20, MatchStrength.WEAK: 0}[assessment.match_strength]),
        "language_edge": float(language_edge * 10),
        "economics": float({
            EconomicsBand.EXCELLENT: 15,
            EconomicsBand.VIABLE: 8,
            EconomicsBand.BUDGET_KNOWN: 8,
            EconomicsBand.UNKNOWN: 0,
            EconomicsBand.BELOW_FLOOR: 0,
        }[assessment.economics_band]),
        "source": float({
            SourceKind.ORIGINAL_EMPLOYER: 10,
            SourceKind.ORIGINAL_ATS: 10,
            SourceKind.EMAIL_OFFER: 10,
            SourceKind.REPUTABLE_BOARD: 8,
            SourceKind.AGGREGATOR_UNRESOLVED: 3,
            SourceKind.UNKNOWN: 1,
            SourceKind.CONTENT_FARM: 0,
        }[assessment.source_actionability]),
        "trust": round(assessment.conservative_trust * 10, 2),
        "freshness": round(assessment.freshness * 5, 2),
        "easy_entry": 5.0 if assessment.easy_entry else 0.0,
        "risk_adjustment": risk_adjustment,
    }
    priority = round(sum(components.values()), 2)

    evidence = [
        f"{concept} ({assessment.concept_evidence.get(concept, '')})"
        for concept in assessment.matched_concepts
    ]
    explanation = "; ".join(evidence[:3]) or "insufficient role evidence"
    watch_outs = list(assessment.unresolved)
    if assessment.freshness <= CONFIG.ranking.min_freshness + 0.001:
        watch_outs.append("stale_or_evergreen")
    return Decision(
        engine_version='v5.0.0-rc1' if CONFIG.v55.enabled else ENGINE_VERSION,
        policy_version='v5.0.0-rc1' if CONFIG.v55.enabled else 'v4.2',
        lifecycle=assessment.lifecycle,
        next_action=assessment.next_action,
        next_step=assessment.next_step,
        verification_tasks=assessment.verification_tasks,
        action_band=action_band,
        terminal_reason=terminal_reason,
        secondary_reasons=list(blockers),
        priority_key=priority_key,
        rank_key=rank_key,
        priority_score=priority,
        priority_components=components,
        explanation=explanation,
        watch_outs=list(dict.fromkeys(watch_outs)),
    )


def _counts(
    raw_count: int,
    observations_count: int,
    failures_count: int,
    canonical_count: int,
    evaluated: list[EvaluatedJob],
    hydration_count: int = 0,
    account_count: int = 0,
) -> tuple[dict[str, int], list[str]]:
    normalized = observations_count - failures_count - hydration_count - account_count
    counts: dict[str, int] = {
        "raw_observations": raw_count,
        "normalized_observations": normalized,
        "normalization_failures": failures_count,
        "canonical_jobs": canonical_count,
        "duplicate_observations_collapsed": normalized + hydration_count - canonical_count,
        "accepted_hydration_observations": hydration_count,
        "account_observations": account_count,
        "assigned_job_observations": normalized + hydration_count,
    }
    for item in evaluated:
        reason = item.decision.terminal_reason
        counts[reason] = counts.get(reason, 0) + 1
    counts.setdefault("surface_primary", 0)
    counts.setdefault("surface_verify", 0)
    for reason in (
        "reject_scam", "reject_source_quality", "reject_location",
        "reject_language", "reject_credentials", "reject_seniority",
        "reject_pay", "reject_trust", "reject_role", "reject_low_evidence",
    ):
        counts.setdefault(reason, 0)

    errors: list[str] = []
    if counts["raw_observations"] != (
        counts["normalized_observations"] + counts["normalization_failures"]
    ):
        errors.append("raw_observation_equation")
    if counts["assigned_job_observations"] != (
        counts["canonical_jobs"] + counts["duplicate_observations_collapsed"]
    ):
        errors.append("canonicalization_equation")
    terminal_total = (
        counts["surface_primary"] + counts["surface_verify"]
        + sum(counts[reason] for reason in counts if reason.startswith("reject_"))
    )
    if counts["canonical_jobs"] != terminal_total:
        errors.append("terminal_decision_equation")
    return counts, errors


def evaluate_raw(
    raw_records: list[dict],
    *,
    as_of: datetime | None = None,
) -> RunResult:
    """Evaluate one captured ingestion batch with no network or persistence."""
    metadata = _metadata(as_of)
    observations, failures = build_observations(raw_records)
    if CONFIG.v55.enabled:
        _initialize_captured_observations(observations, metadata.as_of)
    return evaluate_observations(observations, as_of=metadata.as_of, raw_count=len(raw_records))


def _initialize_captured_observations(observations, as_of):
    for observation in observations:
        observation.captured_at = observation.captured_at or as_of
        raw, job = observation.raw_payload, observation.job
        if job is None:
            continue
        observation.content_hash = hashlib.sha256(job.description.encode('utf-8')).hexdigest()
        native_full = (
            observation.source == 'ashby' and bool(raw.get('jobUrl') or raw.get('applyUrl'))
            and bool(raw.get('descriptionPlain') or raw.get('descriptionHtml'))
            or observation.source == 'greenhouse' and raw.get('id') is not None
            and bool(raw.get('absolute_url')) and bool(raw.get('content'))
            or observation.source == 'lever' and bool(raw.get('id'))
            and bool(raw.get('hostedUrl')) and bool(raw.get('descriptionPlain') or raw.get('description'))
        )
        if native_full and not job.title_truncated:
            observation.identity_state = 'exact'
            observation.content_state = 'complete'
            observation.application_route_state = 'observed_public_route'
            observation.vacancy_state = 'explicitly_closed' if raw.get('isListed') is False else 'verified_open'
            observation.verified_open_at = as_of
        else:
            observation.content_state = 'snippet_only' if observation.source in {'serp', 'adzuna', 'jsearch'} else 'partial'


def evaluate_observations(observations, *, as_of=None, raw_count=None) -> RunResult:
    """Assess captured observations, including appended enrichment, offline."""
    metadata = _metadata(as_of)
    if CONFIG.v55.enabled:
        from .evidence_dimensions import project_observation
        observations = [project_observation(o) for o in observations]
    failures = [observation for observation in observations if observation.normalization_error]
    canonical_jobs = canonicalize_observations(observations)
    if CONFIG.v55.enabled:
        for canonical in canonical_jobs:
            ids = {o.observation_id for o in canonical.observations}
            for observation in observations:
                if observation.origin == 'account_state' and (
                    ids.intersection(observation.parent_observation_ids)
                    or observation.raw_payload.get('canonical_id') == canonical.canonical_id
                ):
                    canonical.observations.append(observation)
    evaluated: list[EvaluatedJob] = []
    for canonical in canonical_jobs:
        assessment = _assess(canonical, metadata.as_of)
        decision = _decision(canonical, assessment)
        evaluated.append(EvaluatedJob(
            canonical=canonical,
            assessment=assessment,
            decision=decision,
        ))
    evaluated.sort(key=_evaluation_sort_key)
    counts, errors = _counts(
        raw_count if raw_count is not None else len([o for o in observations if o.origin == 'discovery']),
        len(observations), len(failures), len(canonical_jobs), evaluated,
        hydration_count=len([o for o in observations if o.origin == 'hydration']),
        account_count=len([o for o in observations if o.origin == 'account_state']),
    )
    return RunResult(
        metadata=metadata,
        observations=observations,
        normalization_failures=failures,
        evaluated=evaluated,
        counts=counts,
        accounting_ok=not errors,
        accounting_errors=errors,
    )


def reassess_item(item: EvaluatedJob, *, as_of: datetime) -> None:
    """Recompute one canonical job after provenance resolution."""
    assessment = _assess(item.canonical, as_of)
    item.assessment = assessment
    item.decision = _decision(item.canonical, assessment)


def recount_result(result: RunResult) -> None:
    result.counts, result.accounting_errors = _counts(
        result.counts["raw_observations"],
        len(result.observations),
        len(result.normalization_failures),
        len(result.evaluated),
        result.evaluated,
        hydration_count=len([o for o in result.observations if o.origin == 'hydration']),
        account_count=len([o for o in result.observations if o.origin == 'account_state']),
    )
    result.accounting_ok = not result.accounting_errors


async def refine_scam_with_llm(result: RunResult) -> int:
    """Optional live refinement; deterministic rules remain the lower bound."""
    checked = await scam_llm_pass([item.job for item in result.evaluated])
    if checked == 0:
        return 0
    changed = False
    for item in result.evaluated:
        if not is_scam(item.job) or item.decision.terminal_reason == "reject_scam":
            continue
        item.assessment.blockers.insert(0, "scam_threshold")
        item.decision = _decision(item.canonical, item.assessment)
        changed = True
    if changed:
        recount_result(result)
    return checked


def decision_fingerprint(result: RunResult) -> str:
    """Stable replay fingerprint excluding run timestamps and IDs."""
    payload = [
        {
            "id": item.canonical.canonical_id,
            "band": item.decision.action_band.value,
            "reason": item.decision.terminal_reason,
            "priority_key": item.decision.priority_key.model_dump(mode="json"),
            "match": item.assessment.match_strength.value,
            "eligibility": item.assessment.eligibility.value,
            **({
                'lifecycle': item.decision.lifecycle,
                'next_action': item.decision.next_action,
                'next_step': item.decision.next_step,
                'verification_tasks': item.decision.verification_tasks,
                'requirements': item.assessment.requirements_assessment.model_dump(mode='json'),
                'selected_pay': item.assessment.selected_pay.model_dump(mode='json') if item.assessment.selected_pay else None,
            } if item.decision.policy_version == 'v5.0.0-rc1' else {}),
        }
        for item in sorted(result.evaluated, key=lambda row: row.canonical.canonical_id)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
