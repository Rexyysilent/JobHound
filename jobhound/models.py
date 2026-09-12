"""Canonical data model (HANDOFF v2 §4)."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

# Verdict values used across the pipeline.
VERDICT_PENDING = "pending"
VERDICT_SURFACED = "surfaced"
VERDICT_REJECTED_SCAM = "rejected_scam"
VERDICT_REJECTED_PAY = "rejected_pay"
VERDICT_REJECTED_IRRELEVANT = "rejected_irrelevant"
VERDICT_REJECTED_TRUST = "rejected_trust"
VERDICT_REJECTED_INELIGIBLE = "rejected_ineligible"  # v3 Patch #1 eligibility gates
VERDICT_REJECTED_LOCATION = "rejected_location"
VERDICT_REJECTED_LISTING_QUALITY = "rejected_listing_quality"

# The mutually exclusive terminal verdicts. Every deduplicated job in a run must
# end on exactly one of these — that invariant is what the v3.5 ledger enforces
# (pipeline.account_ledger). "pending" is not terminal; company caps and stale
# carryover rows are presentation/DB concerns, not verdicts.
TERMINAL_VERDICTS = (
    VERDICT_SURFACED,
    VERDICT_REJECTED_SCAM,
    VERDICT_REJECTED_PAY,
    VERDICT_REJECTED_TRUST,
    VERDICT_REJECTED_INELIGIBLE,
    VERDICT_REJECTED_LOCATION,
    VERDICT_REJECTED_LISTING_QUALITY,
    VERDICT_REJECTED_IRRELEVANT,
)


class Evidence(BaseModel):
    """One dated entry in a platform's trust audit log (HANDOFF v2 §4).

    evidence_class precedence: operator > incident > aggregate_reviews >
    community; "seed" marks the initial prior.
    """
    evidence_class: str = Field(alias="class")
    note: str
    date: date
    delta: float = 0.0

    model_config = {"populate_by_name": True}


class PlatformTrust(BaseModel):
    """Counterparty trust profile (HANDOFF v2 §4/§8). Loaded from
    platform_registry.yaml; `trust` is derived from the five dims via config
    weights at load time, never stored in the registry."""
    key: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)  # matched vs job.company
    # five dimensions, each 0..1
    payment_reliability: float
    work_consistency: float
    account_stability: float
    support_quality: float
    onboarding_cost: float         # 1 = cheap to join; 0 = long UNPAID quals
    # derived
    trust: float = 0.0             # weighted composite (weights in config)
    availability_est: float        # P(work available) — effective-rate math
    unpaid_overhead_est: float     # unpaid fraction (training, EQ-watching, disputes)
    # provenance
    evidence: list[Evidence] = Field(default_factory=list)
    prior: float                   # decay target (decay itself is P3)
    last_reviewed: date
    notes: str = ""
    caveat: str = ""               # one-liner for 🟠-band digest entries


class Job(BaseModel):
    id: str = ""                     # stable hash (set in dedupe)
    source: str                      # "remotive" | "remoteok" | ... | "greenhouse" | ...
    seen_on: list[str] = Field(default_factory=list)  # every source that carried it (set in dedupe)
    title: str
    company: str | None = None
    company_domain: str | None = None
    company_confidence: float = 0.0
    url: str                         # link to the ORIGINAL posting
    original_title: str | None = None
    title_truncated: bool = False
    title_reconstructed: bool = False
    description: str = ""
    location: str | None = None
    is_remote: bool = False
    region_tags: list[str] = Field(default_factory=list)  # ["india","worldwide","bengali",...]
    posted_at: datetime | None = None

    # pay
    pay_raw: str | None = None
    pay_min_hourly_usd: float | None = None
    pay_max_hourly_usd: float | None = None
    pay_basis: str | None = None     # "hour"|"year"|"month"|"task"|"word"|"audio_min"|"image"|"unknown"
    pay_is_estimate: bool = False    # True when derived from piece-rate throughput assumptions
    guaranteed_hours: bool = False   # offer states FT/guaranteed hours → availability=1.0 in the pay gate
    # v3.5: where the rate came from — "structured" (provider field), "email"
    # (pre-parsed offer), "title" (explicit hourly expression in the title) or
    # "unknown". Payload-only; deliberately NOT a DB column in this patch.
    pay_source: str = "unknown"

    # trust join (enrich/trust.py, HANDOFF v2 §8)
    platform_key: str | None = None       # FK into platform_registry
    platform_trust: float | None = None   # 0..1 composite; None = unrated
    effective_hourly_usd: float | None = None  # nominal × availability × (1−overhead)

    # verdicts (filled by pipeline)
    scam_score: float = 0.0          # 0 clean … 1 almost-certainly scam
    scam_flags: list[str] = Field(default_factory=list)
    llm_pending: bool = False         # Gemini batch deferred; rules verdict still stands
    pay_ok: bool | None = None       # None = unknown pay (down-rank, don't reject)
    fit_score: float = 0.0           # 0..100
    fit_reasons: list[str] = Field(default_factory=list)
    easy_entry: bool = False         # Patch #3 Hermes/rater discovery lane
    listing_confidence: float = 0.5  # 0..1 data quality of THIS posting (scored in P2)
    ev_score: float = 0.0            # final ranking value (§8.2)
    verdict: str = VERDICT_PENDING

    @property
    def reasons(self) -> list[str]:
        """Alias used by the pay/eligibility gates — same list as fit_reasons."""
        return self.fit_reasons

    @property
    def pay_display(self) -> str:
        """Human-readable normalized pay for the digest."""
        if self.pay_min_hourly_usd is None and self.pay_max_hourly_usd is None:
            return "pay unknown"
        lo, hi = self.pay_min_hourly_usd, self.pay_max_hourly_usd
        def money(value: float) -> str:
            return f"{value:.2f}".rstrip("0").rstrip(".")

        if lo is not None and hi is not None and abs(lo - hi) > 0.01:
            body = f"${money(lo)}–${money(hi)}/hr"
        else:
            v = hi if hi is not None else lo
            body = f"${money(v)}/hr"
        if self.pay_is_estimate:
            body += " (est., piece-rate)"
        return body

    @property
    def pay_display_full(self) -> str:
        """Nominal → effective (§8.2: nominal $/hr is a lie on gig platforms)."""
        body = self.pay_display
        if self.effective_hourly_usd is not None:
            body += f" → eff ~${self.effective_hourly_usd:.2f}/hr"
            if self.guaranteed_hours:
                body += " (FT guaranteed)"
        return body
