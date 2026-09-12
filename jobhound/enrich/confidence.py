"""Listing confidence (HANDOFF v2 §8.2) — data quality of THIS posting, 0..1.

Not about the counterparty (that's trust) and not about fraud (that's scam):
how much can the numbers and the link be believed. Feeds ev_score via
confidence_multiplier = 0.7 + 0.3·confidence, and the ●●●/●●○/●○○ badge.
Domain-resolution checks are a later pass; these are the cheap signals.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..dedupe import ATS_SOURCES
from ..config import CONFIG, ListingQualityCfg
from ..filters.listing_quality import domain_tier, free_host_check
from ..models import Job

_BASE = 0.5
_FRESH_DAYS = 30


def score_confidence(job: Job, now: datetime | None = None,
                     cfg: ListingQualityCfg = CONFIG.listing_quality) -> Job:
    if free_host_check(job.url):
        job.listing_confidence = cfg.free_host_confidence
        return job
    # Email offers carry a preset confidence (0.9, or 0.6 on the parse-fail
    # safety net — v3 Patch #2): platform pre-targeting beats these heuristics.
    if job.source.startswith("email:"):
        return job

    tier, tier_confidence = domain_tier(job.url, job.company_domain, cfg)
    # SEO farms stay at their explicit low prior. Rich copy or a claimed rate
    # must not launder a farm-only listing into high confidence.
    if tier == "farm":
        job.listing_confidence = tier_confidence
        return job

    c = tier_confidence if tier != "unknown" else _BASE
    if tier == "unknown" and job.source in ATS_SOURCES:
        c += 0.20                      # canonical original, straight from the employer
    if job.pay_raw:
        c += 0.15                      # salary explicitly stated
    if len(job.seen_on) >= 2:
        c += 0.10                      # independent cross-source corroboration
    if job.posted_at is not None:
        posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        if (now - posted).days < _FRESH_DAYS:
            c += 0.10
    else:
        c -= 0.10                      # undated → likely a stale aggregator repost
    if len((job.description or "").strip()) < 200:
        c -= 0.10                      # thin/templated copy
    job.listing_confidence = round(min(1.0, max(0.0, c)), 3)
    return job
