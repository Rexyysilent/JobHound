"""Bounded URL resolution and direct-ATS hydration for V4.1."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import math
import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import CONFIG
from ..models import Job
from ..normalize import company_from_job_url, normalize
from .engine import evaluate_observations, reassess_item, recount_result
from .models import ActionBand, RunResult, SourceKind
from .provenance import (
    classify_source,
    counterparty_key,
    public_url,
    sanitize_url,
)
from .hydration import (
    RetrievalBudget,
    RetrievalPolicy,
    StageHealth,
    HydrationResult,
    child_observation,
    hydrate_public_posting,
    resolve_public_original,
)

_ADZUNA_HOSTS = {"adzuna.in", "www.adzuna.in"}
_ATS_HOSTS = {
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "apply.workable.com",
}
_OUTBOUND_SKIP_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "linkedin.com",
    "www.linkedin.com",
    "twitter.com",
    "x.com",
    "google.com",
    "www.google.com",
}
_ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
_CLOSED_MARKERS = (
    "no longer accepting applications",
    "this job is no longer available",
    "job is no longer available",
    "job has expired",
    "position has been filled",
    "no positions are accepting applications right now",
    "applications are closed",
)
logger = logging.getLogger(__name__)


@dataclass
class Resolution:
    original: str
    final: str
    trail: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Hydration:
    job: Job | None = None
    source: str | None = None
    error: str | None = None


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() != "a":
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.hrefs.append(value)


def _safe_public_http(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    host = (parts.hostname or "").casefold()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    )


def _is_ats_host(host: str) -> bool:
    normalized = (host or "").casefold().rstrip(".")
    return (
        normalized in _ATS_HOSTS
        or any(normalized.endswith(f".{candidate}") for candidate in _ATS_HOSTS)
        or normalized.endswith(".greenhouse.io")
    )


def _is_adzuna_interstitial(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.hostname or "").casefold() in _ADZUNA_HOSTS and (
        parts.path.startswith("/details/")
        or parts.path.startswith("/land/ad/")
    )


def _interstitial_target(body: str, current: str) -> str | None:
    parser = _HrefParser()
    try:
        parser.feed(body[:500_000])
    except Exception:
        return None
    embedded = re.findall(r"https?://[^\\\"'<>\\s]+", body[:500_000])
    candidates: list[tuple[int, str]] = []
    current_host = (urlsplit(current).hostname or "").casefold()
    for raw in [*parser.hrefs, *embedded]:
        target = sanitize_url(urljoin(current, raw))
        if not target or target == current or not _safe_public_http(target):
            continue
        parts = urlsplit(target)
        host = (parts.hostname or "").casefold()
        if host in _OUTBOUND_SKIP_HOSTS:
            continue
        if host in _ADZUNA_HOSTS and parts.path.startswith("/land/ad/"):
            score = 100
        elif host != current_host and _is_ats_host(host):
            score = 90
        elif host != current_host:
            score = 70
        else:
            continue
        candidates.append((score, target))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _looks_closed(body: str, url: str) -> bool:
    text = re.sub(r"\s+", " ", (body or "")[:500_000]).casefold()
    if any(marker in text for marker in _CLOSED_MARKERS):
        return True
    parts = urlsplit(url)
    return "linkedin.com" in (parts.hostname or "") and "/jobs/view/" not in parts.path


async def resolve_url(
    url: str,
    *,
    client: httpx.AsyncClient,
    max_redirects: int = 5,
    max_retries: int = 0,
    retry_base_seconds: float = 0.0,
    request_gate: Callable[[str], Awaitable[None]] | None = None,
    inspect_body: bool = False,
) -> Resolution:
    current = sanitize_url(url)
    if not _safe_public_http(current):
        return Resolution(original=url, final=current, error="unsafe_or_invalid_url")
    trail = [current]

    async def request(method: str, target: str, **kwargs) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(max(0, max_retries) + 1):
            if request_gate is not None:
                await request_gate(target)
            response = await client.request(method, target, **kwargs)
            if response.status_code != 429 or attempt >= max(0, max_retries):
                return response
            raw_retry = response.headers.get("retry-after", "")
            try:
                retry_after = float(raw_retry)
            except (TypeError, ValueError):
                retry_after = retry_base_seconds * (2 ** attempt)
            await asyncio.sleep(max(0.0, min(retry_after, 30.0)))
        assert response is not None
        return response

    for _ in range(max_redirects):
        try:
            response = await request("HEAD", current, follow_redirects=False)
            if response.status_code in {403, 405} or (
                200 <= response.status_code < 300
                and (_is_adzuna_interstitial(current) or inspect_body)
            ):
                response = await request(
                    "GET",
                    current,
                    headers=(
                        {}
                        if _is_adzuna_interstitial(current) or inspect_body
                        else {"Range": "bytes=0-0"}
                    ),
                    follow_redirects=False,
                )
        except httpx.HTTPError as exc:
            return Resolution(
                original=url,
                final=current,
                trail=trail,
                error=type(exc).__name__,
            )

        if response.status_code >= 400:
            return Resolution(
                original=url,
                final=current,
                trail=trail,
                error=f"http_{response.status_code}",
            )

        if (
            inspect_body
            and 200 <= response.status_code < 300
            and _looks_closed(response.text, current)
        ):
            return Resolution(original=url, final=current, trail=trail, error="closed_listing")

        if response.status_code not in {301, 302, 303, 307, 308}:
            if (
                200 <= response.status_code < 300
                and _is_adzuna_interstitial(current)
            ):
                target = _interstitial_target(response.text, current)
                if target and target not in trail:
                    trail.append(target)
                    current = target
                    continue
            return Resolution(original=url, final=current, trail=trail)

        location = response.headers.get("location")
        if not location:
            return Resolution(original=url, final=current, trail=trail)
        target = sanitize_url(urljoin(current, location))
        if not _safe_public_http(target):
            return Resolution(
                original=url,
                final=current,
                trail=trail,
                error="redirected_to_unsafe_url",
            )
        current_parts = urlsplit(current)
        target_parts = urlsplit(target)
        if (
            "linkedin.com" in (current_parts.hostname or "")
            and "/jobs/view/" in current_parts.path
            and "/jobs/view/" not in target_parts.path
        ):
            return Resolution(original=url, final=current, trail=trail, error="closed_listing")
        if target in trail:
            return Resolution(
                original=url,
                final=current,
                trail=trail,
                error="redirect_loop",
            )
        trail.append(target)
        current = target
    return Resolution(
        original=url,
        final=current,
        trail=trail,
        error="redirect_limit",
    )


async def hydrate_ats_url(
    url: str,
    *,
    client: httpx.AsyncClient,
) -> Hydration:
    """Hydrate a thin direct Ashby URL through its public posting API."""
    parts = urlsplit(url)
    segments = [item for item in parts.path.split("/") if item]
    if (parts.hostname or "").casefold() != "jobs.ashbyhq.com" or len(segments) < 2:
        return Hydration()
    slug, posting_id = segments[0], segments[1]
    try:
        response = await client.get(
            _ASHBY_API.format(slug=slug),
            params={"includeCompensation": "true"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return Hydration(error=type(exc).__name__)

    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return Hydration(error="invalid_payload")
    selected = None
    for raw in payload["jobs"]:
        if not isinstance(raw, dict):
            continue
        candidate_url = str(raw.get("jobUrl") or raw.get("applyUrl") or "")
        candidate_id = str(raw.get("id") or "")
        if candidate_id == posting_id or urlsplit(candidate_url).path.rstrip(
            "/"
        ).endswith(f"/{posting_id}"):
            selected = dict(raw)
            break
    if selected is None:
        return Hydration(error="posting_not_found")
    selected["_slug"] = slug
    selected["_company"] = company_from_job_url(url)
    try:
        job = normalize({"source": "ashby", "raw": selected})
    except (KeyError, TypeError, ValueError) as exc:
        return Hydration(error=f"normalize_{type(exc).__name__}")
    if job is None:
        return Hydration(error="unusable_hydration")
    job.url = sanitize_url(job.url or url)
    return Hydration(job=job, source="ashby")


def _needs_ats_hydration(item) -> bool:
    observation = item.canonical.best_observation
    return (
        observation.source_kind == SourceKind.ORIGINAL_ATS
        and (
            observation.source not in {"ashby", "greenhouse", "lever"}
            or not item.job.company
            or len((item.job.description or "").strip()) < 500
        )
    )


def _collapse_exact_final_urls(result: RunResult) -> int:
    """Merge records that resolve to the same public application URL."""
    by_url: dict[str, object] = {}
    kept = []
    collapsed = 0
    for item in sorted(
        result.evaluated,
        key=lambda row: row.decision.priority_key.sort_tuple,
    ):
        key = public_url(item.job.url).rstrip("/").casefold()
        if not key or key not in by_url:
            if key:
                by_url[key] = item
            kept.append(item)
            continue
        winner = by_url[key]
        winner.canonical.observations.extend(item.canonical.observations)
        winner.canonical.observations = list({
            observation.observation_id: observation
            for observation in winner.canonical.observations
        }.values())
        winner.canonical.corroborated_by = sorted(set(
            winner.canonical.corroborated_by + item.canonical.corroborated_by
        ))
        winner.job.seen_on = sorted(set(winner.job.seen_on + item.job.seen_on))
        winner.canonical.alternate_urls = list(dict.fromkeys([
            *winner.canonical.alternate_urls,
            *item.canonical.alternate_urls,
            item.job.url,
        ]))
        winner.canonical.canonicalization_reason += "; exact final URL collapse"
        collapsed += 1
    if collapsed:
        result.evaluated = kept
    return collapsed


async def resolve_result_urls(result: RunResult) -> int:
    """Resolve/hydrate the highest-priority actionable surface candidates."""
    if not CONFIG.v41.resolve_urls or CONFIG.v41.max_url_resolutions_per_run <= 0:
        return 0
    candidates = [
        item
        for item in result.evaluated
        if item.decision.action_band != ActionBand.REJECT
        and (
            item.assessment.source_actionability
            in {SourceKind.AGGREGATOR_UNRESOLVED, SourceKind.UNKNOWN}
            or _needs_ats_hydration(item)
            or (
                item.job.posted_at is None
                and item.assessment.source_actionability in {
                    SourceKind.ORIGINAL_EMPLOYER, SourceKind.REPUTABLE_BOARD,
                }
            )
        )
        and item.job.url
    ]
    candidates.sort(key=lambda item: item.decision.priority_key.sort_tuple)
    candidates = candidates[: CONFIG.v41.max_url_resolutions_per_run]
    if not candidates:
        return 0

    timeout = min(15.0, CONFIG.http.timeout_seconds)
    semaphore = asyncio.Semaphore(max(1, CONFIG.v41.resolver_concurrency))
    adzuna_gate = asyncio.Lock()
    adzuna_last_request = 0.0

    async def pace_request(url: str) -> None:
        """Serialize Adzuna clicks and enforce a real inter-request gap."""
        nonlocal adzuna_last_request
        host = (urlsplit(url).hostname or "").casefold()
        if host not in _ADZUNA_HOSTS:
            return
        async with adzuna_gate:
            loop = asyncio.get_running_loop()
            remaining = (
                CONFIG.v41.resolver_min_interval_seconds
                - (loop.time() - adzuna_last_request)
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
            adzuna_last_request = loop.time()
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": CONFIG.http.user_agent},
        follow_redirects=False,
    ) as client:

        async def one(item):
            async with semaphore:
                observation = item.canonical.best_observation
                resolution_input = (
                    sanitize_url(observation.original_url) or item.job.url
                )
                is_adzuna = (urlsplit(resolution_input).hostname or "").casefold() in _ADZUNA_HOSTS
                resolution = await resolve_url(
                    resolution_input,
                    client=client,
                    max_retries=(0 if is_adzuna else CONFIG.v41.resolver_max_retries),
                    retry_base_seconds=CONFIG.v41.resolver_backoff_seconds,
                    request_gate=pace_request,
                    inspect_body=not is_adzuna,
                )
                hydration = (
                    await hydrate_ats_url(resolution.final, client=client)
                    if resolution.final and not resolution.error
                    else Hydration()
                )
                return item, resolution, hydration

        adzuna_items = []
        other_items = []
        for item in candidates:
            observation = item.canonical.best_observation
            resolution_input = sanitize_url(observation.original_url) or item.job.url
            host = (urlsplit(resolution_input).hostname or "").casefold()
            (adzuna_items if host in _ADZUNA_HOSTS else other_items).append(item)

        resolved = list(await asyncio.gather(*(one(item) for item in other_items)))
        circuit_open = False
        for item in adzuna_items:
            if circuit_open:
                url = sanitize_url(item.canonical.best_observation.original_url) or item.job.url
                resolved.append((
                    item,
                    Resolution(original=url, final=url, trail=[url], error="http_429_circuit_open"),
                    Hydration(),
                ))
                continue
            outcome = await one(item)
            resolved.append(outcome)
            if outcome[1].error == "http_429":
                circuit_open = True

    changed = 0
    failures: Counter[str] = Counter()
    for item, resolution, hydration in resolved:
        observation = item.canonical.best_observation
        if hydration.error == "posting_not_found" and not resolution.error:
            resolution.error = "closed_listing"
            hydration = Hydration(error=hydration.error)
        item_changed = (
            not observation.resolution_attempted
            or observation.resolution_error != resolution.error
        )
        observation.resolution_attempted = True
        observation.resolution_error = resolution.error
        if resolution.error:
            failures[resolution.error] += 1
        previous_host = urlsplit(item.job.url).hostname
        if (
            not resolution.error
            and resolution.final
            and resolution.final != item.job.url
        ):
            final_host = urlsplit(resolution.final).hostname
            display_url = public_url(resolution.final)
            observation.normalized_url = display_url
            observation.apply_domain = (final_host or "").casefold()
            observation.redirect_trail = resolution.trail
            item.job.url = display_url
            if observation.job is not None:
                observation.job.url = display_url
            kind, confidence = classify_source(item.job)
            if (
                kind in {
                    SourceKind.UNKNOWN,
                    SourceKind.AGGREGATOR_UNRESOLVED,
                }
                and previous_host
                and final_host
                and previous_host.casefold() != final_host.casefold()
            ):
                kind, confidence = SourceKind.ORIGINAL_EMPLOYER, 0.75
            observation.source_kind = kind
            observation.source_confidence = confidence
            item.canonical.canonicalization_reason += (
                f"; resolved {previous_host or '?'} -> {final_host or '?'}"
            )
            item_changed = True

        if hydration.job is not None:
            hydrated = hydration.job
            hydrated.id = item.canonical.canonical_id
            hydrated.seen_on = sorted(
                set(item.job.seen_on + ([hydration.source] if hydration.source else []))
            )
            item.canonical.job = hydrated
            observation.job = hydrated.model_copy(deep=True)
            observation.normalized_url = hydrated.url
            observation.apply_domain = (
                urlsplit(hydrated.url).hostname or ""
            ).casefold()
            observation.source_kind = SourceKind.ORIGINAL_ATS
            observation.source_confidence = 0.98
            observation.hydrated_source = hydration.source
            observation.repair_flags = list(
                dict.fromkeys(
                    [*observation.repair_flags, f"hydrated:{hydration.source}"]
                )
            )
            item.canonical.canonicalization_reason += (
                f"; hydrated from {hydration.source}"
            )
            item_changed = True

        if not item_changed:
            continue
        item.canonical.counterparty_key = counterparty_key(item.job)
        reassess_item(item, as_of=result.metadata.as_of)
        changed += 1
    if changed:
        changed += _collapse_exact_final_urls(result)
    if changed:
        recount_result(result)
    if failures:
        summary = ", ".join(
            f"{code}={count}" for code, count in sorted(failures.items())
        )
        logger.warning("URL resolution failures: %s", summary)
    return changed


async def hydrate_result(
    result: RunResult,
    *,
    policy: RetrievalPolicy | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    review_namespace=None,
) -> dict:
    """Boundedly hydrate exact public ATS posts and reassess them in this run.

    Unlike the legacy resolver this appends an immutable child observation.
    Identity-conflicting or uncertain responses are retained as diagnostics but
    are never accepted into the canonical job.
    """
    if policy is None:
        cfg = getattr(CONFIG, "v55", None)
        policy = RetrievalPolicy(
            shortlist=getattr(cfg, "enrichment_shortlist", 40),
            request_budget=getattr(cfg, "request_budget", 60),
            time_budget_seconds=getattr(cfg, "time_budget_seconds", 180.0),
            concurrency=getattr(cfg, "resolver_concurrency", 4),
            per_host_concurrency=getattr(cfg, "resolver_per_host_concurrency", 1),
            cache_ttl_hours=getattr(cfg, "verification_ttl_hours", 48),
        )
    health = StageHealth("hydration")
    budget = RetrievalBudget(policy, review_namespace=review_namespace)
    as_of_ts = result.metadata.as_of.timestamp()
    max_cycles = getattr(getattr(CONFIG, "v55", None), "max_automatic_cycles", 2)
    watch_days = getattr(getattr(CONFIG, "v55", None), "watch_recheck_days", 7)

    def state_key(item) -> str:
        return public_url(item.canonical.best_observation.original_url or item.job.url)

    for item in result.evaluated:
        key = state_key(item)
        if item.assessment.account_state.get("new_material_evidence"):
            budget.verification_cycles.pop(key, None)
            budget.verification_next_check.pop(key, None)
    eligible = [
        item for item in result.evaluated
        if item.job.url
        and not (urlsplit(item.job.url).hostname or "").casefold().endswith("upwork.com")
        and not any(getattr(obs, "origin", "") == "hydration" and getattr(obs, "content_state", "") == "complete" for obs in item.canonical.observations)
        and (
            _is_ats_host((urlsplit(item.job.url).hostname or ""))
            or item.assessment.source_actionability in {SourceKind.ORIGINAL_EMPLOYER, SourceKind.ORIGINAL_ATS}
            or item.assessment.source_actionability == SourceKind.AGGREGATOR_UNRESOLVED
        )
        and (_needs_ats_hydration(item)
             or item.assessment.source_actionability == SourceKind.AGGREGATOR_UNRESOLVED
             or len((item.job.description or "").strip()) < 1000)
        and not (budget.verification_cycles.get(state_key(item), 0) >= max_cycles
                 and budget.verification_next_check.get(state_key(item), 0) > as_of_ts)
    ]
    eligible.sort(key=lambda row: (row.decision.priority_key.sort_tuple, row.canonical.canonical_id))
    reserve = min(len(eligible), math.ceil(policy.shortlist * getattr(getattr(CONFIG, "v55", None), "soft_reject_reserve_fraction", .1)))
    rejected = [row for row in eligible
                if row.decision.action_band == ActionBand.REJECT
                and not row.assessment.blockers
                and bool(row.assessment.unresolved)][:reserve]
    primary_pool = [row for row in eligible if row.decision.action_band != ActionBand.REJECT]
    # Deterministic source round-robin prevents one provider consuming the run.
    buckets: dict[str, list] = {}
    for row in primary_pool:
        buckets.setdefault(row.canonical.best_observation.source, []).append(row)
    diverse = []
    while buckets and len(diverse) < max(0, policy.shortlist - len(rejected)):
        for source in sorted(list(buckets)):
            if buckets[source]: diverse.append(buckets[source].pop(0))
            if not buckets[source]: del buckets[source]
            if len(diverse) >= policy.shortlist - len(rejected): break
    candidates = [*diverse, *rejected]
    health.skipped = max(0, len(result.evaluated) - len(candidates))
    semaphore = asyncio.Semaphore(max(1, policy.concurrency))
    timeout = httpx.Timeout(min(30.0, policy.time_budget_seconds))
    async with httpx.AsyncClient(
        timeout=timeout,
        transport=transport,
        headers={"User-Agent": CONFIG.http.user_agent},
        follow_redirects=False,
    ) as client:
        async def one(item):
            async with semaphore:
                parent = item.canonical.best_observation
                target = item.job.url
                host = (urlsplit(target).hostname or "").casefold()
                resolution_trail = [target]
                if host in _ADZUNA_HOSTS:
                    try:
                        resolved, error, resolution_trail = await resolve_public_original(
                            client, budget, target
                        )
                    except (httpx.HTTPError, RuntimeError) as exc:
                        resolved, error = None, str(exc) or type(exc).__name__
                    if not resolved:
                        return item, parent, HydrationResult(
                            state="deferred" if error and (
                                "budget" in error or "cooldown" in error or error == "http_429"
                            ) else "unavailable",
                            source="adzuna_resolution", request_url=target,
                            public_url=target, error=error,
                        ), resolution_trail
                    target = resolved
                outcome = await hydrate_public_posting(
                    target, item.job, client=client, budget=budget
                )
                return item, parent, outcome, resolution_trail
        outcomes = await asyncio.gather(*(one(item) for item in candidates))

    changed = 0
    adapter_health: dict[tuple[str, str], dict] = {}
    cycle_state: dict[str, dict] = {}
    for item, parent, outcome, resolution_trail in outcomes:
        cycle_key = public_url(parent.original_url or item.job.url)
        health_key = (outcome.source or "unsupported", (urlsplit(outcome.request_url).hostname or "").casefold())
        adapter_row = adapter_health.setdefault(health_key, {
            "source": health_key[0], "transport_host": health_key[1],
            "stage": "hydration", "attempted": 0, "succeeded": 0,
            "deferred": 0, "failed": 0,
        })
        adapter_row["attempted"] += 1
        health.attempted += 1
        accepted = (outcome.state in {"complete", "partial"}
                    and outcome.job is not None
                    and outcome.identity_state in {"exact", "corroborated"})
        if outcome.state == "deferred":
            health.deferred += 1
            adapter_row["deferred"] += 1
        elif not accepted:
            code = ("identity_conflict" if outcome.identity_state == "conflict"
                    else "no_accepted_observation" if outcome.state == "complete"
                    else outcome.error or outcome.state)
            health.fail(code)
            adapter_row["failed"] += 1
        prior_cycles = budget.verification_cycles.get(cycle_key, 0)
        cycles = 0 if accepted else prior_cycles + 1
        budget.verification_cycles[cycle_key] = cycles
        if accepted:
            budget.verification_next_check.pop(cycle_key, None)
        elif cycles >= max_cycles:
            budget.verification_next_check[cycle_key] = (
                result.metadata.as_of + timedelta(days=watch_days)
            ).timestamp()
        cycle_state[parent.observation_id] = {
            "verification_cycles": cycles,
            "automatic_retry_allowed": cycles < max_cycles,
            "route": "watch" if cycles >= max_cycles else "verify",
        }
        if not accepted:
            continue
        child = child_observation(parent, outcome)
        item.canonical.observations.append(child)
        result.observations.append(child)
        health.succeeded += 1
        adapter_row["succeeded"] += 1
        changed += 1
    # Persist verification history as an account-state overlay, including for
    # rows skipped because their bounded recheck is not yet due.
    overlay_added = False
    for item in result.evaluated:
        parent = item.canonical.best_observation
        key = state_key(item)
        cycles = budget.verification_cycles.get(key, 0)
        if not cycles:
            continue
        next_ts = budget.verification_next_check.get(key)
        marker = hashlib.sha256(key.encode()).hexdigest()[:16]
        observation_id = f"account:verification:{marker}:{cycles}"
        if any(obs.observation_id == observation_id for obs in result.observations):
            continue
        result.observations.append(type(parent)(
            observation_id=observation_id,
            source="account_state",
            origin="account_state",
            parent_observation_ids=[parent.observation_id],
            captured_at=result.metadata.as_of,
            raw_payload={
                "automatic_verification_cycles": cycles,
                "observed_at": result.metadata.as_of.isoformat(),
                "next_check_at": (
                    datetime.fromtimestamp(next_ts, result.metadata.as_of.tzinfo).isoformat()
                    if next_ts else None
                ),
                "new_material_evidence": False,
                "project_url": key,
            },
        ))
        overlay_added = True
    if changed or overlay_added:
        old_metadata = result.metadata
        old_health = list(result.source_health)
        rebuilt = evaluate_observations(
            result.observations,
            as_of=old_metadata.as_of,
            raw_count=int(result.counts.get("raw") or len([
                obs for obs in result.observations if obs.origin == "discovery"
            ])),
        )
        result.metadata = old_metadata
        result.observations = rebuilt.observations
        result.evaluated = rebuilt.evaluated
        result.counts = rebuilt.counts
        result.normalization_failures = rebuilt.normalization_failures
        result.accounting_ok = rebuilt.accounting_ok
        result.accounting_errors = rebuilt.accounting_errors
        result.source_health = old_health
    stage = {
        "source": "retrieval",
        "transport_host": "multiple",
        "status": "partial" if health.failed or health.deferred else ("ok" if health.succeeded else "empty"),
        "stage": health.stage,
        "attempted": health.attempted,
        "succeeded": health.succeeded,
        "deferred": health.deferred,
        "failed": health.failed,
        "skipped": health.skipped,
        "request_count": budget.requests,
        "error_codes": health.error_codes,
    }
    rows = []
    for row in adapter_health.values():
        row["status"] = "partial" if row["failed"] or row["deferred"] else (
            "ok" if row["succeeded"] else "empty"
        )
        rows.append(row)
    result.source_health.extend(rows or [stage])
    budget.persist()
    return {"changed": changed, "requests": budget.requests,
            "stage_health": stage, "adapter_health": rows,
            "attempts": [{"source": outcome.source, "request_url": public_url(outcome.request_url),
                          "state": outcome.state, "error": outcome.error,
                          "identity_state": outcome.identity_state,
                          "redirect_trail": [public_url(url) for url in resolution_trail],
                          **cycle_state.get(_parent.observation_id, {})}
                         for _item, _parent, outcome, resolution_trail in outcomes]}
