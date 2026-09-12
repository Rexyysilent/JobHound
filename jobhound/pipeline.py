"""Pipeline orchestration: ingest → normalize → dedupe → enrich → filter+score.

Returns scored jobs + stage counts. Storage and notification are handled by the
caller (run.py) so the pipeline stays pure and the same code path serves both a
live run and a --dry-run.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .config import CONFIG
from .dedupe import dedupe
from .enrich.confidence import score_confidence
from .enrich.pay import enrich_pay
from .enrich.pay_gate import apply_pay_gate
from .enrich.trust import compute_ev, enrich_trust
from .filters.eligibility import check as eligibility_check, default_profile
from .filters.listing_quality import junk_check, location_gate, region_lock_check
from .filters.posting_language import check as posting_language_check
from .filters.relevance import score_relevance
from .filters.scam import is_scam, score_scam
from .filters.scam_llm import second_pass as scam_llm_pass
from .models import (
    Job,
    TERMINAL_VERDICTS,
    VERDICT_REJECTED_INELIGIBLE,
    VERDICT_REJECTED_IRRELEVANT,
    VERDICT_REJECTED_LISTING_QUALITY,
    VERDICT_REJECTED_LOCATION,
    VERDICT_REJECTED_PAY,
    VERDICT_REJECTED_SCAM,
    VERDICT_REJECTED_TRUST,
    VERDICT_SURFACED,
)
from .normalize import normalize
from .sources import SourceHealth, build_sources
from .trust.registry import default_registry

log = logging.getLogger("jobhound.pipeline")


def _decide_verdict(job: Job, hard_excluded: bool, ineligible: bool,
                    location_rejected: bool = False,
                    quality_rejected: bool = False) -> str:
    # Categorical gates lead; threshold-based scoring follows.
    if is_scam(job):
        return VERDICT_REJECTED_SCAM
    if quality_rejected:
        return VERDICT_REJECTED_LISTING_QUALITY
    if location_rejected:
        return VERDICT_REJECTED_LOCATION
    if hard_excluded:
        return VERDICT_REJECTED_IRRELEVANT
    if job.pay_ok is False:
        return VERDICT_REJECTED_PAY
    if ineligible:
        return VERDICT_REJECTED_INELIGIBLE
    if job.platform_trust is not None and job.platform_trust < CONFIG.trust.hard_floor:
        return VERDICT_REJECTED_TRUST
    # v3: base fit is 10 and the anchor-rule cap is 30 — the surface cutoff
    # (default 32) is what actually buries category-only matches.
    if job.fit_score < CONFIG.relevance.min_fit_to_surface:
        return VERDICT_REJECTED_IRRELEVANT
    return VERDICT_SURFACED


def account_ledger(jobs: list[Job], deduped: int) -> tuple[dict[str, int], list[str]]:
    """Per-verdict counts plus the accounting invariant (v3.5 item 7).

    The 2026-07-22 digest looked like it had lost 415 jobs. It had not — the
    summary simply never printed rejected_location (372) or
    rejected_listing_quality (43). The ledger is therefore made explicit:
    `accounted` sums every terminal verdict and `unaccounted` must be zero.

    Returns (counts, ids of jobs holding a non-terminal verdict).
    """
    counts = {verdict: 0 for verdict in TERMINAL_VERDICTS}
    unaccounted_ids: list[str] = []
    for job in jobs:
        if job.verdict in counts:
            counts[job.verdict] += 1
        else:
            unaccounted_ids.append(job.id)
    counts["accounted"] = sum(counts.values())
    counts["unaccounted"] = deduped - counts["accounted"]
    return counts, unaccounted_ids


async def ingest_raw_with_health(
    limit: int | None, only: str | None, *,
    transport: httpx.AsyncBaseTransport | None = None,
    exclude: set[str] | None = None,
) -> tuple[list[dict], list[SourceHealth]]:
    """Fetch one shared provider batch plus non-secret source health."""
    sources = build_sources(limit=limit, only=only)
    if exclude:
        sources = [source for source in sources if source.name not in exclude]
    headers = {"User-Agent": CONFIG.http.user_agent}
    async with httpx.AsyncClient(
        headers=headers,
        timeout=CONFIG.http.timeout_seconds,
        follow_redirects=True,
        transport=transport,
    ) as client:
        batches = await asyncio.gather(*(s.fetch(client) for s in sources))
    records = [rec for batch in batches for rec in batch]
    return records, [source.health for source in sources]


async def ingest_raw(limit: int | None, only: str | None) -> list[dict]:
    """Compatibility wrapper for V3 and callers that only need records."""
    records, _health = await ingest_raw_with_health(limit, only)
    return records


async def run_pipeline(limit: int | None = None, only: str | None = None) -> tuple[list[Job], dict]:
    raw = await ingest_raw(limit, only)

    jobs: list[Job] = []
    for rec in raw:
        try:
            job = normalize(rec)
        except ValueError as e:
            log.warning("normalize error: %s", e)
            continue
        if job is not None:
            jobs.append(job)
    normalized = len(jobs)

    jobs = dedupe(jobs)

    gates = evaluate_jobs(jobs)

    # LLM second look at borderline scam scores (§7a) — no-op without a key.
    await scam_llm_pass(jobs)

    finalize_verdicts(jobs, gates)

    ledger, unaccounted_ids = account_ledger(jobs, len(jobs))
    counts = {
        "raw": len(raw),
        "normalized": normalized,
        "deduped": len(jobs),
        **ledger,
    }
    if counts["unaccounted"]:
        # IDs only — a job that fell through the verdict logic is a code bug to
        # look up in the DB, not a reason to leak titles or URLs into the log.
        log.error("ledger unaccounted=%d job_ids=%s",
                  counts["unaccounted"], ",".join(unaccounted_ids))
    log.info("pipeline counts: %s", counts)
    return jobs, counts


def evaluate_jobs(jobs: list[Job]) -> dict[str, set[str]]:
    """Enrich, gate and score every job. Pure CPU — no network, no DB.

    Split out of run_pipeline so the offline regression fixtures
    (tests/test_v35_stabilization.py) exercise the real decision path instead of
    a reimplementation of it. Returns the gate id-sets _decide_verdict needs.
    """
    registry = default_registry()
    profile = default_profile()
    hard_excluded_ids: set[str] = set()
    ineligible_ids: set[str] = set()
    location_rejected_ids: set[str] = set()
    quality_rejected_ids: set[str] = set()
    for job in jobs:
        enrich_pay(job)
        enrich_trust(job)
        score_confidence(job)  # after dedupe — needs seen_on and merged pay
        score_scam(job)

        # Patch #3 categorical gates run before relevance. A rejected listing
        # is still retained and EV-scored for reject-pile auditability.
        # job.url is the URL dedupe RETAINED, so a farm copy that lost to a
        # canonical ATS/employer link is assessed on the kept link, not rejected
        # for a farm entry in seen_on.
        quality_reasons = junk_check(
            job.title, job.url, job.company, has_rate=bool(job.pay_raw),
            company_domain=job.company_domain,
        )
        location_reasons = [
            *location_gate(
                job.is_remote,
                job.region_tags,
                job.location,
                job.description,
                title=job.title,
            ),
            *region_lock_check(job.title, job.description),
        ]
        language_reasons = posting_language_check(
            job.title, job.description, profile.languages
        )
        eligible, eligibility_reasons = eligibility_check(job.title, profile)

        if quality_reasons:
            quality_rejected_ids.add(job.id)
            job.fit_reasons.extend(quality_reasons)
        if location_reasons:
            location_rejected_ids.add(job.id)
            job.fit_reasons.extend(location_reasons)
        if language_reasons or not eligible:
            ineligible_ids.add(job.id)
            job.fit_reasons.extend(
                f"ineligible: {reason}"
                for reason in [*language_reasons, *eligibility_reasons]
            )

        if not (quality_reasons or location_reasons
                or language_reasons or not eligible):
            if score_relevance(job, profile=profile):
                hard_excluded_ids.add(job.id)
        # Effective-rate floor (v3 Patch #2) — needs the trust join, so it runs
        # here rather than inside enrich_pay.
        apply_pay_gate(job, registry, CONFIG.pay)

    return {
        "hard_excluded": hard_excluded_ids,
        "ineligible": ineligible_ids,
        "location_rejected": location_rejected_ids,
        "quality_rejected": quality_rejected_ids,
    }


def finalize_verdicts(jobs: list[Job], gates: dict[str, set[str]]) -> None:
    """Assign the terminal verdict and EV score for every job."""
    for job in jobs:
        job.verdict = _decide_verdict(
            job,
            job.id in gates["hard_excluded"],
            job.id in gates["ineligible"],
            job.id in gates["location_rejected"],
            job.id in gates["quality_rejected"],
        )
        compute_ev(job)  # ranked reject pile is threshold-tuning data too
