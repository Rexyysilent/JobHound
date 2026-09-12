"""Typed V4.1 observation, assessment and decision records."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import Job


class SourceKind(str, Enum):
    EMAIL_OFFER = "email_offer"
    ORIGINAL_EMPLOYER = "original_employer"
    ORIGINAL_ATS = "original_ats"
    REPUTABLE_BOARD = "reputable_board"
    AGGREGATOR_UNRESOLVED = "aggregator_unresolved"
    CONTENT_FARM = "content_farm"
    UNKNOWN = "unknown"


class EligibilityStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class MatchStrength(str, Enum):
    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"


class EconomicsBand(str, Enum):
    EXCELLENT = "excellent"
    VIABLE = "viable"
    BUDGET_KNOWN = "budget_known"
    UNKNOWN = "unknown"
    BELOW_FLOOR = "below_floor"


class ActionBand(str, Enum):
    PRIMARY = "primary"
    VERIFY = "verify"
    REJECT = "reject"


class Evidence(BaseModel):
    dimension: str
    code: str
    value: Any = ""
    source_field: str = ""
    observation_id: str = ""
    span: str = ""
    confidence: float = 1.0
    source_kind: SourceKind | None = None
    modality: str = "explicit"


class EvidenceDimension(BaseModel):
    """Additive explanation, not a score or a new eligibility gate."""

    state: str = "unknown"
    value: Any = None
    claims: list[Evidence] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class PayCandidate(BaseModel):
    raw: str
    min_hourly_usd: float | None = None
    fixed_amount_usd: float | None = None
    max_hourly_usd: float | None = None
    currency: str = "USD"
    basis: str = "unknown"
    source_field: str
    observation_id: str
    confidence: float
    estimated: bool = False
    guaranteed: bool = False
    up_to: bool = False
    observation_source_kind: SourceKind | None = None
    claim_credibility: float = 0.0
    corroborating_observation_ids: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    amount_low: float | None = None
    amount_high: float | None = None
    qualifier: str = "unknown"
    actual_unit: str = "unknown"
    labor_hourly_supported: bool = False
    scope: str = ""
    gross_net: str = "unknown"
    fees: list[str] = Field(default_factory=list)
    net_amount_usd: float | None = None
    labor_time_assumption: str | None = None

    @property
    def midpoint(self) -> float | None:
        known = [
            value for value in (self.min_hourly_usd, self.max_hourly_usd)
            if value is not None
        ]
        return sum(known) / len(known) if known else None


class RequirementClaim(BaseModel):
    """A requirement claim with its source and interpretation."""

    dimension: Literal[
        "language", "experience", "degree", "domain", "tool", "location"
    ]
    value: Any
    modality: Literal[
        "required", "minimum", "mandatory", "preferred", "optional",
        "explicitly_not_required", "unknown"
    ] = "unknown"
    match_mode: Literal["single", "all", "any"] = "single"
    required: bool | None = None
    source_field: str = "description"
    observation_id: str = ""
    span: str = ""
    confidence: float = 1.0

    actor: str = "applicant"
    predicate: str = ""
    polarity: Literal["positive", "negative"] = "positive"
    scope: str = "role"
    evidence_status: str = "supported"
    logic_group: str | None = None


class RequirementsAssessment(BaseModel):
    completeness: Literal["complete", "partial", "unknown"] = "unknown"
    languages: list[RequirementClaim] = Field(default_factory=list)
    experience: list[RequirementClaim] = Field(default_factory=list)
    degrees: list[RequirementClaim] = Field(default_factory=list)
    domains: list[RequirementClaim] = Field(default_factory=list)
    tools: list[RequirementClaim] = Field(default_factory=list)
    location_scope: list[RequirementClaim] = Field(default_factory=list)
    language_required: list[str] = Field(default_factory=list)
    language_preferred: list[str] = Field(default_factory=list)
    experience_min_years: float | None = None
    experience_preferred_years: float | None = None
    credentials_required: list[str] = Field(default_factory=list)
    credentials_preferred: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    claims: list[RequirementClaim] = Field(default_factory=list)
    mandatory_language_groups: list[RequirementClaim] = Field(default_factory=list)
    degree_requirement_state: str = "unknown"
    experience_evidence_state: str = "unknown"
    field_coverage: dict[str, str] = Field(default_factory=dict)


class RoleAssessment(BaseModel):
    occupation_family: str = "unknown"
    task_lanes: list[str] = Field(default_factory=list)
    matched_profile_domains: list[str] = Field(default_factory=list)
    specialist_domain: str | None = None
    specialist_domain_status: Literal[
        "claimed", "unclaimed", "unresolved", "none"
    ] = "none"
    accessibility_evidence: list[Evidence] = Field(default_factory=list)
    # Compatibility projections retained while the old engine is migrated.
    ai_task_lane: str = "unknown"
    seniority: str = "unspecified"
    management_scope: bool = False
    specialist_domains: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class PayState(str, Enum):
    CREDIBLE_HOURLY = "credible_hourly"
    CREDIBLE_SALARY = "credible_salary"
    BUDGET_KNOWN = "budget_known"
    UP_TO_ONLY = "up_to_only"
    CONFLICTED = "conflicted"
    BELOW_FLOOR = "below_floor"
    KNOWN_CREDIBLE = "known_credible"
    KNOWN_UNVERIFIED = "known_unverified"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class PayAssessment(BaseModel):
    state: PayState = PayState.UNKNOWN
    candidates: list[PayCandidate] = Field(default_factory=list)
    selected: PayCandidate | None = None
    credibility: float = 0.0
    ranking_rate: float | None = None
    selected_candidate_id: str | None = None
    conflict: bool = False
    ranking_value: float | None = None
    conflict_reasons: list[str] = Field(default_factory=list)


class PriorityKey(BaseModel):
    """Authoritative, named ordering components within an action band."""

    role_priority: int = 0
    explicit_profile_language_edge: int = 0
    match_strength: int = 0
    accessibility_confidence: int = 0
    economics_quality: int = 0
    source_actionability: int = 0
    conservative_trust: int = 0
    freshness: int = 0
    stable_tiebreaker: str = ""
    policy_version: str = "v4.2"
    action_readiness: int = 0
    supported_time_to_cash: float = 0
    action_cost_and_friction: int = 0

    @property
    def semantic_tuple(self) -> tuple[int, int, int, int, int, int, int, int, str]:
        if self.policy_version == "v5.0.0-rc1":
            return (
                self.action_readiness, self.supported_time_to_cash,
                self.action_cost_and_friction, self.match_strength,
                self.role_priority, self.economics_quality,
                self.explicit_profile_language_edge, self.source_actionability,
                self.conservative_trust, self.freshness, self.stable_tiebreaker,
            )
        return (
            self.role_priority,
            self.explicit_profile_language_edge,
            self.match_strength,
            self.accessibility_confidence,
            self.economics_quality,
            self.source_actionability,
            self.conservative_trust,
            self.freshness,
            self.stable_tiebreaker,
        )

    @property
    def sort_tuple(self) -> tuple[int, int, int, int, int, int, int, int, str]:
        """Ascending key: semantic components descend, stable ID ascends."""
        if self.policy_version == "v5.0.0-rc1":
            values = self.semantic_tuple
            return (*(-value for value in values[:-1]), values[-1])
        return (
            -self.role_priority,
            -self.explicit_profile_language_edge,
            -self.match_strength,
            -self.accessibility_confidence,
            -self.economics_quality,
            -self.source_actionability,
            -self.conservative_trust,
            -self.freshness,
            self.stable_tiebreaker,
        )


class ListingObservation(BaseModel):
    observation_id: str
    source: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    job: Job | None = None
    publisher_domain: str = ""
    apply_domain: str = ""
    original_url: str = ""
    normalized_url: str = ""
    source_kind: SourceKind = SourceKind.UNKNOWN
    source_confidence: float = 0.5
    normalization_error: str | None = None
    repair_flags: list[str] = Field(default_factory=list)
    redirect_trail: list[str] = Field(default_factory=list)
    hydrated_source: str | None = None
    resolution_attempted: bool = False
    resolution_error: str | None = None
    origin: Literal["discovery", "hydration", "account_state"] = "discovery"
    parent_observation_ids: list[str] = Field(default_factory=list)
    captured_at: datetime | None = None
    content_hash: str = ""
    resolution_state: str = "not_needed"
    content_state: str = "unknown"
    identity_state: str = "uncertain"
    application_route_state: str = "account_unknown"
    vacancy_state: str = "unknown"
    task_availability_state: str = "unknown"
    retrieval_error: str | None = None
    truncated: bool = False
    verified_open_at: datetime | None = None
    extraction_version: str = ""
    evidence_projection_version: str = ""
    work_arrangement: EvidenceDimension = Field(default_factory=EvidenceDimension)


class CanonicalJob(BaseModel):
    canonical_id: str
    legacy_canonical_id: str | None = None
    counterparty_key: str = ""
    job: Job
    observations: list[ListingObservation]
    field_sources: dict[str, str] = Field(default_factory=dict)
    alternate_urls: list[str] = Field(default_factory=list)
    corroborated_by: list[str] = Field(default_factory=list)
    canonicalization_reason: str = ""

    @property
    def best_observation(self) -> ListingObservation:
        selected_id = self.field_sources.get("url")
        if selected_id:
            for observation in self.observations:
                if observation.observation_id == selected_id:
                    return observation
        return self.observations[0]


class Assessment(BaseModel):
    evidence_dimensions: dict[str, EvidenceDimension] = Field(default_factory=dict)
    eligibility: EligibilityStatus = EligibilityStatus.UNKNOWN
    role_families: list[str] = Field(default_factory=list)
    language_requirements: list[str] = Field(default_factory=list)
    domain_requirements: list[str] = Field(default_factory=list)
    seniority: str = "unspecified"
    management_scope: bool = False
    work_format: str = "unknown"
    source_actionability: SourceKind = SourceKind.UNKNOWN
    platform_trust_estimate: float = 0.5
    platform_trust_confidence: float = 0.0
    conservative_trust: float = 0.5
    pay_candidates: list[PayCandidate] = Field(default_factory=list)
    selected_pay: PayCandidate | None = None
    pay_conflict: bool = False
    pay_credibility: float = 0.0
    pay_rate_for_ranking: float | None = None
    match_strength: MatchStrength = MatchStrength.WEAK
    matched_concepts: list[str] = Field(default_factory=list)
    concept_evidence: dict[str, str] = Field(default_factory=dict)
    economics_band: EconomicsBand = EconomicsBand.UNKNOWN
    freshness: float = 0.0
    easy_entry: bool = False
    blockers: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    requirements_assessment: RequirementsAssessment = Field(
        default_factory=RequirementsAssessment
    )
    role_assessment: RoleAssessment = Field(default_factory=RoleAssessment)
    pay_assessment: PayAssessment = Field(default_factory=PayAssessment)
    document_type: str = "unknown"
    opportunity_type: str = "unknown"
    buyer_intent: str = "unknown"
    lifecycle: str = "active"
    lifecycle_reason: str = ""
    account_state: dict[str, Any] = Field(default_factory=dict)
    verification_tasks: list[dict[str, Any]] = Field(default_factory=list)
    action_readiness: str = "unknown"
    next_action: str = "verify"
    next_step: str = ""
    verified_open_at: datetime | None = None
    action_cost_known: bool = False
    action_cost_acceptable: bool = False
    time_to_cash_days: float | None = None


class Decision(BaseModel):
    action_band: ActionBand
    terminal_reason: str
    secondary_reasons: list[str] = Field(default_factory=list)
    rank_key: tuple[int, int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0, 0)
    priority_score: float = 0.0
    priority_components: dict[str, float] = Field(default_factory=dict)
    explanation: str = ""
    watch_outs: list[str] = Field(default_factory=list)
    notification_transition: str = "none"
    engine_version: str = "v4.2"
    priority_key: PriorityKey = Field(default_factory=PriorityKey)
    lifecycle: str = "active"
    next_action: str = "verify"
    next_step: str = ""
    verification_tasks: list[dict[str, Any]] = Field(default_factory=list)
    policy_version: str = "v4.2"


class EvaluatedJob(BaseModel):
    canonical: CanonicalJob
    assessment: Assessment
    decision: Decision

    @property
    def job(self) -> Job:
        return self.canonical.job


class RunMetadata(BaseModel):
    run_id: str
    engine_version: str = "v4.2"
    started_at: datetime
    as_of: datetime
    ruleset_hash: str
    profile_hash: str
    trust_registry_hash: str
    config_hash: str
    snapshot_path: str | None = None


class RunResult(BaseModel):
    metadata: RunMetadata
    observations: list[ListingObservation]
    evaluated: list[EvaluatedJob]
    counts: dict[str, int]
    source_health: list[dict[str, Any]] = Field(default_factory=list)
    normalization_failures: list[ListingObservation] = Field(default_factory=list)
    accounting_ok: bool = True
    accounting_errors: list[str] = Field(default_factory=list)
