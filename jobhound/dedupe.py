"""Conservative posting identity for the V3 pipeline and legacy replay.

Similar titles do not establish that two postings are the same opportunity.
Collapse only matching posting URLs, titles, employers and applicant scopes.
An ATS observation wins a confirmed duplicate; all its sources survive in
``seen_on``. Unknown location is not a wildcard for a known restriction.

IDs are functions of each record, independent of the other records in a run.
Unscoped records with ordinary HTTPS URLs retain compatible historical IDs.
Records distinguished by previously discarded URL or scope information need
new IDs; the V3 seen store must not silently transfer another role's history.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from urllib.parse import unquote_plus, urlsplit

from .models import Job

_SPACE = re.compile(r"\s+")
# Generic ``ref`` and ``source`` parameters can identify a role or geography.
# Only widely defined campaign/click identifiers are discarded here.
_TRACKING_NAMES = {"gclid", "fbclid", "mc_cid", "mc_eid"}


def _norm_text(s: str | None) -> str:
    if not s:
        return ""
    folded = unicodedata.normalize("NFKC", s).casefold()
    # Keep Unicode letters and combining marks, plus technical language names
    # such as C++ and C#. ASCII-only normalization erased those distinctions.
    text = "".join(
        char if char.isalnum() or unicodedata.category(char).startswith("M")
        or char in "+#" else " "
        for char in folded
    )
    return _SPACE.sub(" ", text).strip()


def _url_root(url: str) -> str:
    """Keep role-bearing URL semantics; normalize only known equivalences.

    Unknown query components keep their bytes, order, repeated keys and empty
    values. Path case, trailing path slashes and fragments may identify different
    resources (including client-side routes), so they are not discarded.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "opaque:" + url
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return "opaque:" + url
    query = []
    for component in parts.query.split("&") if parts.query else []:
        name = unquote_plus(component.partition("=")[0]).casefold()
        if not name.startswith("utm_") and name not in _TRACKING_NAMES:
            query.append(component)
    # HTTPS keeps the historical unscoped key shape. HTTP receives an explicit
    # prefix so it cannot alias a different resource served over HTTPS.
    prefix = "" if parts.scheme.casefold() == "https" else "http://"
    path = "" if parts.path in {"", "/"} else parts.path
    result = prefix + parts.netloc.casefold() + path
    if query:
        result += "?" + "&".join(query)
    if parts.fragment:
        result += "#" + parts.fragment
    return result


def _identity_key(job: Job) -> str:
    title, company, url = _norm_text(job.title), _norm_text(job.company), _url_root(job.url)
    location = _norm_text(job.location)
    regions = sorted({tag for item in job.region_tags if (tag := _norm_text(item))})
    if location or job.is_remote or regions or "|" in url or "\0" in url:
        # A separate, unambiguous namespace also avoids delimiter collisions
        # with URLs containing punctuation. Scope is never added conditionally
        # on a collision in the current batch.
        return "scoped-v1\0" + json.dumps(
            [title, company, url, location, job.is_remote, regions],
            ensure_ascii=False, separators=(",", ":"),
        )
    return f"{title}|{company}|{url}"


def stable_id(job: Job) -> str:
    return hashlib.sha1(_identity_key(job).encode("utf-8")).hexdigest()[:16]


# Direct-from-employer boards — a canonical original, not an aggregator copy.
ATS_SOURCES = {"greenhouse", "lever", "ashby"}


def _richness(job: Job) -> tuple:
    """Authority, pay and description richness, followed by a content tie-break.

    IDs and accumulated source sets are excluded because dedupe populates them.
    Their values must not make a winner depend on arrival order or prior merging.
    """
    content = json.dumps(
        job.model_dump(mode="json", exclude={"id", "seen_on"}),
        sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    )
    return (job.source in ATS_SOURCES, bool(job.pay_raw), len(job.description or ""), content)


def _keep_richer(a: Job, b: Job) -> Job:
    kept = a if _richness(a) >= _richness(b) else b
    kept.seen_on = sorted({*a.seen_on, *b.seen_on})
    return kept


def dedupe(jobs: list[Job]) -> list[Job]:
    """Return deterministic winners while retaining the legacy in-place API."""
    by_key: dict[str, Job] = {}
    for job in jobs:
        key = _identity_key(job)
        job.id = stable_id(job)
        job.seen_on = sorted({*job.seen_on, job.source})
        if key in by_key:
            by_key[key] = _keep_richer(by_key[key], job)
        else:
            by_key[key] = job
    return sorted(by_key.values(), key=lambda job: job.id)
