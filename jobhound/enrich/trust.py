"""Trust join + EV scoring (HANDOFF v2 §8.2).

enrich_trust: joins the platform registry onto a job (platform_key, composite
trust, effective hourly). effective_hourly answers "what does an hour of showing
up actually pay once empty queues and unpaid overhead are priced in" — nominal
$75/hr at 45% availability is not $75/hr.

compute_ev: the final ranking value; runs AFTER fit_score is known.
    ev = fit × trust_mult × confidence_mult × freshness
Trust is damped (0.4 + 0.6·t) so a bad platform down-ranks a great fit but
never zeroes it; unknown platforms get a neutral multiplier, not a penalty.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import CONFIG, RankingCfg, TrustCfg
from ..models import Job, PlatformTrust
from .pay_gate import clamp_effective
from ..trust.registry import default_matcher, default_registry


def effective_hourly(nominal: float, pt: PlatformTrust) -> float:
    effective = nominal * pt.availability_est * (1 - pt.unpaid_overhead_est)
    return round(clamp_effective(effective, nominal), 2)


def enrich_trust(job: Job,
                 registry: dict[str, PlatformTrust] | None = None,
                 matcher=None,
                 *,
                 compute_effective_rate: bool = True) -> Job:
    registry = registry if registry is not None else default_registry()
    matcher = matcher if matcher is not None else default_matcher()

    # Email offers arrive with platform_key preset by the sender-domain map —
    # don't clobber it with a company-name match (email company may be None).
    if job.platform_key is None:
        job.platform_key = matcher.match(job.company)
    if job.platform_key is None or job.platform_key not in registry:
        return job
    pt = registry[job.platform_key]
    job.platform_trust = pt.trust

    # Effective rate off the nominal midpoint; both numbers go in the digest.
    known = [v for v in (job.pay_min_hourly_usd, job.pay_max_hourly_usd) if v is not None]
    if known and compute_effective_rate:
        job.effective_hourly_usd = effective_hourly(sum(known) / len(known), pt)
    return job


def trust_multiplier(trust: float | None, cfg: TrustCfg = CONFIG.trust) -> float:
    if trust is None:
        return cfg.unrated_multiplier
    return 0.4 + 0.6 * trust


def confidence_multiplier(listing_confidence: float) -> float:
    return 0.7 + 0.3 * listing_confidence


def freshness(posted_at: datetime | None,
              cfg: RankingCfg = CONFIG.ranking,
              now: datetime | None = None,
              half_life_days: float | None = None) -> float:
    if posted_at is None:
        return cfg.unknown_age_freshness
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now - posted_at).total_seconds() / 86400)
    # Floored: evergreen ATS contractor pools stay open for months and are
    # still real — decay ranks them down, it must not erase them.
    half_life = half_life_days or cfg.freshness_half_life_days
    return max(cfg.min_freshness, 2 ** (-age_days / half_life))


def compute_ev(job: Job,
               trust_cfg: TrustCfg = CONFIG.trust,
               ranking_cfg: RankingCfg = CONFIG.ranking) -> Job:
    half_life = (ranking_cfg.easy_entry_half_life_days
                 if job.easy_entry else ranking_cfg.freshness_half_life_days)
    job.ev_score = round(
        job.fit_score
        * trust_multiplier(job.platform_trust, trust_cfg)
        * confidence_multiplier(job.listing_confidence)
        * freshness(job.posted_at, ranking_cfg, half_life_days=half_life),
        1,
    )
    return job
