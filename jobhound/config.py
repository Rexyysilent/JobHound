"""Typed loader for config.yaml — all non-secret tunables.

Retuning the agent is config-only; no code edits. Loaded once into `CONFIG`.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class RemotiveCfg(BaseModel):
    enabled: bool = True
    searches: list[str] = Field(default_factory=lambda: ["remote"])


class RemoteOkCfg(BaseModel):
    enabled: bool = True


class ArbeitnowCfg(BaseModel):
    enabled: bool = True
    max_pages: int = 3


class AdzunaCfg(BaseModel):
    enabled: bool = True
    country: str = "in"
    currency: str = "INR"
    results_per_page: int = 50
    max_pages: int = 2
    searches: list[str] = Field(default_factory=lambda: ["ai training"])
    request_timeout_seconds: float = 15.0
    max_retries: int = 1
    retry_backoff_seconds: float = 1.0


class AtsCfg(BaseModel):
    enabled: bool = True
    # slug -> company display name, per ATS provider
    greenhouse: dict[str, str] = Field(default_factory=dict)
    lever: dict[str, str] = Field(default_factory=dict)
    ashby: dict[str, str] = Field(default_factory=dict)


class JSearchCfg(BaseModel):
    enabled: bool = True
    queries: list[str] = Field(default_factory=lambda: ["ai training remote"])
    num_pages: int = 1
    date_posted: str = "week"
    country: str = "in"


class SerpCfg(BaseModel):
    enabled: bool = False
    num_results: int = 20
    dorks: list[str] = Field(default_factory=list)


class SourcesCfg(BaseModel):
    remotive: RemotiveCfg = RemotiveCfg()
    remoteok: RemoteOkCfg = RemoteOkCfg()
    arbeitnow: ArbeitnowCfg = ArbeitnowCfg()
    adzuna: AdzunaCfg = AdzunaCfg()
    jsearch: JSearchCfg = JSearchCfg()
    ats: AtsCfg = AtsCfg()
    serp: SerpCfg = SerpCfg()


class HttpCfg(BaseModel):
    user_agent: str = "jobhound/0.1 (personal)"
    timeout_seconds: float = 30.0
    per_source_concurrency: int = 1


class PayCfg(BaseModel):
    floor_usd: float = 4.5           # v3: applies to the EFFECTIVE rate (pay_gate.py)
    hard_reject_usd_per_hour: float = 1.5
    hours_per_year: float = 2080
    hours_per_month: float = 173
    fx_to_usd: dict[str, float] = Field(default_factory=lambda: {"USD": 1.0})
    piece_rate_per_hour: dict[str, float] = Field(default_factory=dict)


class ScamLlmCfg(BaseModel):
    enabled: bool = True
    model: str = "gemini-2.5-flash"
    band: tuple[float, float] = (0.3, 0.7)
    batch_size: int = 20
    max_jobs_per_run: int = 60
    min_interval_seconds: float = 9.5
    max_calls_per_day: int = 40
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    retry_jitter_seconds: float = 1.0


class ScamCfg(BaseModel):
    threshold: float = 0.6
    min_description_chars: int = 80
    llm: ScamLlmCfg = ScamLlmCfg()


class KeywordsCfg(BaseModel):
    boost_high: list[str] = Field(default_factory=list)
    boost_mid: list[str] = Field(default_factory=list)
    boost_lang: list[str] = Field(default_factory=list)
    exclude_hard: list[str] = Field(default_factory=list)
    exclude_soft: list[str] = Field(default_factory=list)


class RelevanceCfg(BaseModel):
    fuzzy_threshold: int = 88
    # v3: fit below this -> rejected_irrelevant. Must sit ABOVE the anchor-rule
    # cap (profile.no_anchor_fit_cap, 30) so category-only jobs stay buried,
    # but below 34 so two boost_mid anchors (10+12+12) still surface.
    min_fit_to_surface: float = 32
    keywords: KeywordsCfg = KeywordsCfg()


class TitlePenaltyCfg(BaseModel):
    pattern: str
    delta: int = 0
    # v3.5: hard ceiling for titles this rule matches. A -20 penalty was not
    # enough — the production "LLM Training Delivery Leader" scored 94 and would
    # still have surfaced at 74. Set below relevance.min_fit_to_surface to make
    # a matching title unreachable rather than merely down-ranked.
    fit_cap: int | None = None


class ScoringCfg(BaseModel):
    title_penalties: dict[str, TitlePenaltyCfg] = Field(default_factory=lambda: {
        "senior_track": TitlePenaltyCfg(
            pattern=(r"\b(senior|sr\.?|lead|principal|staff|head of|director|vp|"
                     r"vice president|chief|architect|manager)\b"),
            delta=-20,
        ),
        # Titles that are structurally inaccessible to the operator. "leader"
        # (not "lead") keeps "Lead Generation Specialist" out of this rule, and
        # the "head" lookahead keeps "Headphones Reviewer" out while still
        # catching "Head, AI Training" and "Head - Delivery" (dashes are folded
        # before matching, so ASCII "-" covers en and em dashes).
        "leadership_track": TitlePenaltyCfg(
            pattern=(r"\b(?:leader|director|vice\s+president|vp|chief|"
                     r"principal)\b|\bhead\s+of\b"
                     r"|\bhead\b(?=\s*(?:$|[,\-|:;()\[\]/]))"),
            delta=0,
            fit_cap=30,
        ),
    })
    easy_entry_boost: float = 15


class ListingQualityCfg(BaseModel):
    tier_confidence: dict[str, float] = Field(default_factory=lambda: {
        "ats": 0.90,
        "board": 0.70,
        "farm": 0.35,
        "unknown": 0.55,
    })
    farm_domains: list[str] = Field(default_factory=lambda: [
        "bebee.com", "up2staff.com", "mysmartpros.com", "learn4good.com",
        "whatjobs.com", "jobrapido.com", "jooble.org", "expertini.com",
        "jobgether.com", "himalayas.app", "jobleads.com", "theelitejob.com",
        "remoteanywherejob.com", "aigigjobs.com", "pbridgeco.com",
    ])
    # Path fragments that betray a farm copy on an otherwise unrelated host.
    farm_paths: list[str] = Field(default_factory=lambda: [
        "/tuition/job/", "/tuition/jobs/",
    ])
    free_host_scam_bump: float = 0.5
    absurd_language_scam_bump: float = 0.4
    free_host_confidence: float = 0.2


class RegionCfg(BaseModel):
    worldwide_terms: list[str] = Field(default_factory=list)
    india_terms: list[str] = Field(default_factory=list)
    lang_terms: list[str] = Field(default_factory=list)


class DecayCfg(BaseModel):
    rate_per_month: float = 0.01
    min_idle_days: int = 30


class TrustCfg(BaseModel):
    registry_file: str = "platform_registry.yaml"
    weights: dict[str, float] = Field(default_factory=lambda: {
        "payment_reliability": 0.30,
        "work_consistency": 0.25,
        "account_stability": 0.20,
        "support_quality": 0.15,
        "onboarding_cost": 0.10,
    })
    hard_floor: float = 0.25
    unrated_multiplier: float = 0.85
    decay: DecayCfg = DecayCfg()


class IncidentsCfg(BaseModel):
    enabled: bool = True
    every_days: int = 7
    lookback_days: int = 14
    min_sources: int = 2
    delta: float = -0.05
    keywords: dict[str, str] = Field(default_factory=dict)  # regex -> dimension


class ReportCfg(BaseModel):
    enabled: bool = True
    every_days: int = 7


class FeedbackCfg(BaseModel):
    positive_scale: float = 1.0
    diminishing_factor: float = 0.7
    events: dict[str, dict[str, float]] = Field(default_factory=dict)


class RankingCfg(BaseModel):
    freshness_half_life_days: float = 14.0
    easy_entry_half_life_days: float = 45.0
    unknown_age_freshness: float = 0.8
    min_freshness: float = 0.1


class DigestCfg(BaseModel):
    top_n_per_group: int = 15
    per_company_cap: int = 2
    channels: list[str] = ["telegram"]   # any of: telegram, email (file digest always written)
    join_nudges: int = 3             # unjoined platforms nudged per digest (§10)


class EngineCfg(BaseModel):
    """Decision-engine rollout switch.

    V3 remains callable for rollback and replay comparison.  V4.1 writes to a
    sidecar database, so changing this switch never requires restoring the V3
    jobs table.
    """
    active: str = "v4.2"
    shadow_v3: bool = False
    capture_snapshots: bool = True


class V41Cfg(BaseModel):
    """V4.1 policy and persistence tunables."""
    sidecar_db: str = "data/jobhound_v41.db"
    snapshot_dir: str = "data/snapshots"
    min_display_priority: float = 32.0
    top_n_per_band: int = 15
    automation_top_n: int = 5
    automation_per_company_cap: int = 1
    per_company_cap: int = 2
    per_platform_cap: int = 3
    platform_backfill_when_single_source: bool = True
    resolve_urls: bool = True
    max_url_resolutions_per_run: int = 40
    resolver_concurrency: int = 2
    resolver_min_interval_seconds: float = 1.25
    resolver_max_retries: int = 2
    resolver_backoff_seconds: float = 2.0
    allow_direct_unknown_primary: bool = True
    minimum_economics_credibility: float = 0.65
    unverified_high_pay_usd: float = 25.0
    thin_source_unknown_freshness: float = 0.30
    source_health_alert_cooldown_hours: float = 24.0
    source_health_drop_ratio: float = 0.50
    trust_uncertainty_penalty: float = 0.12
    low_trust_priority_penalty: float = 8.0
    high_onboarding_priority_penalty: float = 5.0
    source_trust_priors: dict[str, float] = Field(default_factory=lambda: {
        "email_offer": 0.75,
        "original_employer": 0.70,
        "original_ats": 0.70,
        "reputable_board": 0.62,
        "aggregator_unresolved": 0.55,
        "content_farm": 0.30,
        "unknown": 0.50,
    })
    source_trust_confidence: dict[str, float] = Field(default_factory=lambda: {
        "email_offer": 0.60,
        "original_employer": 0.40,
        "original_ats": 0.40,
        "reputable_board": 0.35,
        "aggregator_unresolved": 0.25,
        "content_farm": 0.20,
        "unknown": 0.15,
    })


class EmailIngestCfg(BaseModel):
    """v3 Patch #2 — IMAP sweep of the Gmail "JobHound" label."""
    enabled: bool = False
    host: str = "imap.gmail.com"
    folder: str = "JobHound"
    sender_platform_map: dict[str, str] = Field(default_factory=dict)


class V55Cfg(BaseModel):
    """Review-only release policy. Production activation requires approval."""

    enabled: bool = False
    enrichment_shortlist: int = Field(default=40, ge=0, le=200)
    request_budget: int = Field(default=60, ge=0, le=500)
    time_budget_seconds: float = Field(default=180, gt=0, le=1800)
    resolver_concurrency: int = Field(default=4, ge=1, le=4)
    resolver_per_host_concurrency: int = Field(default=1, ge=1, le=1)
    soft_reject_reserve_fraction: float = Field(default=0.10, ge=0, le=0.5)
    verification_ttl_hours: int = Field(default=48, gt=0)
    marketplace_ttl_hours: int = Field(default=24, gt=0)
    unresolved_copy_max_age_days: int = Field(default=30, gt=0)
    max_automatic_cycles: int = Field(default=2, ge=1)
    watch_recheck_days: int = Field(default=7, gt=0)
    max_primary_actions: int = Field(default=5, ge=0)
    max_verify_actions: int = Field(default=3, ge=0)
    max_per_employer: int = Field(default=2, ge=1)
    max_per_marketplace: int = Field(default=2, ge=1)
    account_specific_platforms: list[str] = Field(default_factory=lambda: ["oneforma", "telus_oneforma"])
    account_states: list[dict] = Field(default_factory=list)


class Config(BaseModel):
    v55: V55Cfg = V55Cfg()
    engine: EngineCfg = EngineCfg()
    v41: V41Cfg = V41Cfg()
    sources: SourcesCfg = SourcesCfg()
    email_ingest: EmailIngestCfg = EmailIngestCfg()
    http: HttpCfg = HttpCfg()
    pay: PayCfg = PayCfg()
    scam: ScamCfg = ScamCfg()
    relevance: RelevanceCfg = RelevanceCfg()
    scoring: ScoringCfg = ScoringCfg()
    listing_quality: ListingQualityCfg = ListingQualityCfg()
    region: RegionCfg = RegionCfg()
    trust: TrustCfg = TrustCfg()
    feedback: FeedbackCfg = FeedbackCfg()
    incidents: IncidentsCfg = IncidentsCfg()
    report: ReportCfg = ReportCfg()
    ranking: RankingCfg = RankingCfg()
    digest: DigestCfg = DigestCfg()


def load_config(path: Path | str | None = None) -> Config:
    p = Path(path) if path else _CONFIG_PATH
    # Public distributions contain only non-personal, disabled-by-default
    # examples. An installed operator's config continues to take precedence.
    if path is None and not p.exists():
        p = p.with_name("config.example.yaml")
    if not p.exists():
        return Config()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


CONFIG = load_config()
