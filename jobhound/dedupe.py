"""Stable id assignment + fuzzy near-dup collapse across sources (v2 §2/§8.2).

id = sha1(norm(title) | norm(company) | url_root) — stable across runs so the
seen-set is idempotent. Near-dupes (same company, fuzzily-equal title, e.g. the
same job mirrored on two boards) collapse to one record: an ATS original beats
any aggregator copy (canonical-link resolution), then richer data wins. Every
source that carried the job survives in `seen_on` — cross-source corroboration
feeds listing confidence.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from rapidfuzz import fuzz

from .models import Job

_TITLE_DUP_RATIO = 90  # token_sort_ratio at/above which two titles are "the same role"
_NONALNUM = re.compile(r"[^a-z0-9]+")


def _norm_text(s: str | None) -> str:
    if not s:
        return ""
    return _NONALNUM.sub(" ", s.lower()).strip()


def _url_root(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.netloc}{p.path}".rstrip("/").lower()
    except ValueError:
        return url.lower()


def stable_id(job: Job) -> str:
    key = f"{_norm_text(job.title)}|{_norm_text(job.company)}|{_url_root(job.url)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# Direct-from-employer boards — a canonical original, not an aggregator copy.
ATS_SOURCES = {"greenhouse", "lever", "ashby"}


def _richness(job: Job) -> tuple:
    """Higher = keep. ATS original first (canonical link), then pay, then
    longer description."""
    return (job.source in ATS_SOURCES, job.pay_raw is not None, len(job.description or ""))


def _keep_richer(a: Job, b: Job) -> Job:
    kept = a if _richness(a) >= _richness(b) else b
    kept.seen_on = sorted({*a.seen_on, *b.seen_on})
    return kept


def dedupe(jobs: list[Job]) -> list[Job]:
    # 1) Assign stable ids; collapse exact-id duplicates.
    by_id: dict[str, Job] = {}
    for job in jobs:
        job.id = stable_id(job)
        job.seen_on = sorted({*job.seen_on, job.source})
        if job.id in by_id:
            by_id[job.id] = _keep_richer(by_id[job.id], job)
        else:
            by_id[job.id] = job

    # 2) Fuzzy collapse within the same (normalized) company.
    survivors: list[Job] = []
    for job in by_id.values():
        ckey = _norm_text(job.company)
        merged = False
        if ckey:  # only fuzzy-merge when we actually know the company
            tnorm = _norm_text(job.title)
            for i, kept in enumerate(survivors):
                if _norm_text(kept.company) != ckey:
                    continue
                if fuzz.token_sort_ratio(tnorm, _norm_text(kept.title)) >= _TITLE_DUP_RATIO:
                    survivors[i] = _keep_richer(kept, job)
                    merged = True
                    break
        if not merged:
            survivors.append(job)
    return survivors
