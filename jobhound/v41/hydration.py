"""Bounded, provenance-preserving public posting hydration.

This module is deliberately independent of production persistence.  Callers may
inject an ``httpx.AsyncBaseTransport`` for deterministic review and tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ..models import Job
from ..normalize import company_from_job_url, normalize
from .models import ListingObservation, SourceKind
from .provenance import sanitize_payload, sanitize_url
from .evidence_dimensions import public_page_kind, work_arrangement, project_observation


@dataclass(frozen=True)
class RetrievalPolicy:
    shortlist: int = 40
    request_budget: int = 60
    time_budget_seconds: float = 180.0
    concurrency: int = 4
    per_host_concurrency: int = 1
    max_redirects: int = 5
    max_retries: int = 1
    max_body_bytes: int = 2_000_000
    cache_ttl_hours: float = 48.0
    retry_after_fallback_seconds: float = 60.0
    retry_after_safety_floor_seconds: float = 1.0


@dataclass
class StageHealth:
    stage: str
    attempted: int = 0
    succeeded: int = 0
    deferred: int = 0
    failed: int = 0
    skipped: int = 0
    error_codes: dict[str, int] = field(default_factory=dict)

    def fail(self, code: str) -> None:
        self.failed += 1
        self.error_codes[code] = self.error_codes.get(code, 0) + 1


@dataclass
class HydrationResult:
    state: str
    job: Job | None = None
    source: str | None = None
    request_url: str = ""
    public_url: str = ""
    payload: dict = field(default_factory=dict)
    error: str | None = None
    identity_state: str = "uncertain"
    captured_at: datetime | None = None
    from_cache: bool = False
    vacancy_state: str = "unknown"
    application_route_state: str = "account_unknown"
    source_kind: SourceKind = SourceKind.ORIGINAL_ATS
    task_availability_state: str = "unknown"


class RetrievalBudget:
    def __init__(self, policy: RetrievalPolicy, *, review_namespace: Path | None = None) -> None:
        self.policy = policy
        self.started = time.monotonic()
        self.requests = 0
        self._lock = asyncio.Lock()
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self.cooldown_until: dict[str, float] = {}
        self.review_namespace = review_namespace.resolve() if review_namespace else None
        self.cache: dict[str, dict] = {}
        self.verification_cycles: dict[str, int] = {}
        self.verification_next_check: dict[str, float] = {}
        if self.review_namespace:
            self.review_namespace.mkdir(parents=True, exist_ok=True)
            state = self.review_namespace / "retrieval_state.json"
            if state.exists():
                try:
                    saved = json.loads(state.read_text(encoding="utf-8"))
                    self.cooldown_until = {str(k): float(v) for k, v in saved.get("cooldown_until", {}).items()}
                    self.cache = dict(saved.get("cache", {}))
                    self.verification_cycles = {str(k): int(v) for k, v in saved.get("verification_cycles", {}).items()}
                    self.verification_next_check = {str(k): float(v) for k, v in saved.get("verification_next_check", {}).items()}
                except (ValueError, TypeError, OSError):
                    pass

    def persist(self) -> None:
        """Persist only inside an explicitly supplied review namespace."""
        if self.review_namespace is None:
            return
        (self.review_namespace / "retrieval_state.json").write_text(json.dumps({
            "cooldown_until": self.cooldown_until, "cache": self.cache,
            "verification_cycles": self.verification_cycles,
            "verification_next_check": self.verification_next_check,
        }, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    async def acquire(self, url: str) -> asyncio.Semaphore:
        host = (urlsplit(url).hostname or "").casefold()
        async with self._lock:
            if self.cooldown_until.get(host, 0) > time.time():
                raise RuntimeError("host_cooldown")
            return self._hosts.setdefault(
                host, asyncio.Semaphore(max(1, self.policy.per_host_concurrency))
            )

    async def consume_at_send(self, url: str) -> float:
        """Count only after the host semaphore is held, immediately before I/O."""
        host = (urlsplit(url).hostname or "").casefold()
        async with self._lock:
            remaining = self.policy.time_budget_seconds - (time.monotonic() - self.started)
            if remaining <= 0:
                raise RuntimeError("time_budget_exhausted")
            if self.requests >= self.policy.request_budget:
                raise RuntimeError("request_budget_exhausted")
            if self.cooldown_until.get(host, 0) > time.time():
                raise RuntimeError("host_cooldown")
            self.requests += 1
            return remaining

    def note_retry_after(self, url: str, value: str | None, *, now: datetime | None = None) -> float:
        delay: float
        valid = False
        try:
            delay = max(0.0, float(value)) if value is not None and value.strip() else 0.0
            valid = value is not None and bool(value.strip())
        except (ValueError, TypeError):
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                reference = now or datetime.now(timezone.utc)
                delay = max(0.0, (when - reference).total_seconds())
                valid = True
            except (TypeError, ValueError, OverflowError):
                delay = self.policy.retry_after_fallback_seconds
        if not valid:
            delay = self.policy.retry_after_fallback_seconds
        else:
            # A valid zero or past date means immediate retry is permitted by
            # the server, but a tiny floor prevents already-queued workers from
            # stampeding the same host in this bounded run.
            delay = max(delay, self.policy.retry_after_safety_floor_seconds)
        host = (urlsplit(url).hostname or "").casefold()
        reference_ts = (now.timestamp() if now else time.time())
        self.cooldown_until[host] = max(self.cooldown_until.get(host, 0), reference_ts + delay)
        self.persist()
        return delay


async def _request(client: httpx.AsyncClient, budget: RetrievalBudget, url: str) -> httpx.Response:
    gate = await budget.acquire(url)
    async with gate:
        await _validate_destination(url, skip_dns=isinstance(client._transport, httpx.MockTransport))
        remaining = await budget.consume_at_send(url)
        response = await client.get(url, follow_redirects=False, timeout=min(remaining, 30.0))
    return response


def _safe_public_http(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        if (parts.scheme not in {"http", "https"} or not host or parts.username or parts.password
                or parts.port not in {None, 80, 443} or host == "localhost" or host.endswith(".localhost")):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not any((address.is_private, address.is_loopback, address.is_link_local,
                        address.is_reserved, address.is_multicast))
    except ValueError:
        return False


async def _validate_destination(url: str, *, skip_dns: bool = False) -> None:
    """Reject private DNS answers as well as unsafe literal destinations."""
    if not _safe_public_http(url):
        raise RuntimeError("unsafe_or_invalid_url")
    if skip_dns:
        return
    parts = urlsplit(url)
    try:
        answers = await asyncio.get_running_loop().run_in_executor(
            None, lambda: socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        )
    except socket.gaierror as exc:
        raise RuntimeError("dns_resolution_failed") from exc
    if not answers:
        raise RuntimeError("dns_resolution_failed")
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if any((address.is_private, address.is_loopback, address.is_link_local,
                address.is_reserved, address.is_multicast)):
            raise RuntimeError("dns_private_address")


async def _get_redirected(client: httpx.AsyncClient, budget: RetrievalBudget, url: str) -> tuple[httpx.Response, str, list[str]]:
    current = sanitize_url(url)
    trail = [current]
    if not _safe_public_http(current):
        raise RuntimeError("unsafe_or_invalid_url")
    for _ in range(budget.policy.max_redirects + 1):
        response = await _request(client, budget, current)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response, current, trail
        location = response.headers.get("location")
        target = sanitize_url(urljoin(current, location or ""))
        if not location or not _safe_public_http(target):
            raise RuntimeError("redirected_to_unsafe_url")
        if target in trail:
            raise RuntimeError("redirect_loop")
        trail.append(target)
        current = target
    raise RuntimeError("redirect_limit")


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag.casefold() in {"script", "style", "noscript"}: self.hidden += 1
    def handle_endtag(self, tag):
        if tag.casefold() in {"script", "style", "noscript"} and self.hidden: self.hidden -= 1
    def handle_data(self, data):
        if not self.hidden and data.strip(): self.parts.append(data.strip())


class _LeafParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.hrefs: list[str] = []
    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "a":
            for name, value in attrs:
                if name.casefold() == "href" and value: self.hrefs.append(value)


async def discover_public_leaves(client: httpx.AsyncClient, budget: RetrievalBudget,
                                 parent_url: str) -> list[str]:
    """Fetch an index and return identifiable same-host public leaf URLs."""
    response, final, _trail = await _get_redirected(client, budget, parent_url)
    if response.status_code >= 400 or len(response.content) > budget.policy.max_body_bytes:
        return []
    parser = _LeafParser(); parser.feed(response.text)
    parent_host = (urlsplit(final).hostname or "").casefold()
    leaves = []
    for href in parser.hrefs:
        candidate = sanitize_url(urljoin(final, href))
        parts = urlsplit(candidate)
        if (parts.hostname or "").casefold() != parent_host or not _safe_public_http(candidate):
            continue
        path = parts.path.rstrip("/")
        if path and path != urlsplit(final).path.rstrip("/") and path.count("/") >= 2:
            leaves.append(candidate)
    return sorted(set(leaves))


async def resolve_public_original(client: httpx.AsyncClient, budget: RetrievalBudget,
                                  url: str) -> tuple[str | None, str | None, list[str]]:
    """Resolve an aggregator click to a specific public ATS post, conservatively."""
    response, final, trail = await _get_redirected(client, budget, url)
    if response.status_code == 429:
        budget.note_retry_after(final, response.headers.get("retry-after"))
        return None, "http_429", trail
    if response.status_code >= 400:
        return None, f"http_{response.status_code}", trail
    final_host = (urlsplit(final).hostname or "").casefold()
    if final_host not in {"adzuna.in", "www.adzuna.in"}:
        return final, None, trail
    parser = _LeafParser(); parser.feed(response.text)
    known = {"jobs.ashbyhq.com", "boards.greenhouse.io", "job-boards.greenhouse.io",
             "jobs.lever.co", "jobs.eu.lever.co"}
    candidates = []
    for href in parser.hrefs:
        target = sanitize_url(urljoin(final, href)); host = (urlsplit(target).hostname or "").casefold()
        segments = [p for p in urlsplit(target).path.split("/") if p]
        exact_shape = (("greenhouse.io" in host and len(segments) >= 3 and segments[-2] == "jobs")
                       or (host == "jobs.ashbyhq.com" and len(segments) >= 2)
                       or (host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(segments) >= 2))
        if host in known and exact_shape and _safe_public_http(target):
            candidates.append(target)
    candidates = sorted(set(candidates))
    if len(candidates) != 1:
        return None, "original_target_ambiguous" if candidates else "original_target_missing", trail
    return candidates[0], None, [*trail, candidates[0]]


def accepted_model_claim_count(observation_id: str, source_text: str, output: dict) -> int:
    """Fail closed unless model output identifies this observation and cites its text."""
    if str(output.get("output_id") or "") != observation_id:
        return 0
    claims = output.get("claims") if isinstance(output.get("claims"), list) else [output]
    return sum(1 for claim in claims if isinstance(claim, dict) and
               str(claim.get("claim_span") or "").strip() and
               str(claim.get("claim_span")).strip() in source_text)


def _jobposting_payload(body: str) -> dict | None:
    for match in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', body, re.I | re.S):
        try:
            raw = json.loads(unescape(match.group(1)))
        except (ValueError, TypeError):
            continue
        nodes = raw if isinstance(raw, list) else raw.get("@graph", [raw]) if isinstance(raw, dict) else []
        for node in nodes:
            kinds = node.get("@type", []) if isinstance(node, dict) else []
            if isinstance(kinds, str): kinds = [kinds]
            if "JobPosting" in kinds:
                return node
    return None


def _generic_job(payload: dict, public_url: str, body: str) -> Job | None:
    org = payload.get("hiringOrganization") or {}
    location = payload.get("jobLocation") or payload.get("applicantLocationRequirements") or ""
    if isinstance(location, list): location = ", ".join(str(x) for x in location)
    if isinstance(location, dict):
        address = location.get("address", location)
        location = ", ".join(str(address.get(k) or "") for k in ("addressLocality", "addressRegion", "addressCountry") if address.get(k)) if isinstance(address, dict) else str(address)
    description = str(payload.get("description") or "")
    if not description:
        parser = _VisibleText(); parser.feed(body[:2_000_000]); description = " ".join(parser.parts)
    title = str(payload.get("title") or "").strip()
    company = str(org.get("name") or "").strip() if isinstance(org, dict) else str(org)
    if not title or not description:
        return None
    job = Job(source="first_party", title=title, company=company or company_from_job_url(public_url),
               url=public_url, description=re.sub(r"<[^>]+>", " ", description),
               location=str(location) or None, is_remote="remote" in (str(location) + description).casefold())
    arrangement = work_arrangement(job, payload)
    if arrangement.state == 'remote':
        job.is_remote = True
    elif arrangement.state in {'onsite', 'hybrid'}:
        job.is_remote = False
    return job


def _exact_identity(original: Job, hydrated: Job, posting_id: str, payload: dict) -> str:
    candidate_id = str(payload.get("id") or payload.get("job_id") or payload.get("postingId") or "")
    old_title = (original.title or "").casefold().strip()
    new_title = (hydrated.title or "").casefold().strip()
    old_url, new_url = urlsplit(original.url or ""), urlsplit(hydrated.url or "")
    sigma_same_leaf = (
        (old_url.hostname or "").casefold() == "careers.sigma.ai"
        and (new_url.hostname or "").casefold() == "careers.sigma.ai"
        and old_url.path.startswith("/jobs/")
        and bool(old_url.path.removeprefix("/jobs/").strip("/"))
        and old_url.path.rstrip("/") == new_url.path.rstrip("/")
        and old_title == new_title
    )

    def company_identity(value: str | None) -> str:
        normalized = re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()
        # The captured Sigma posting uses the public brands "Sigma Group" and
        # "Sigma.AI" on the same careers.sigma.ai leaf.  Keep this alias narrow;
        # arbitrary differing employers still conflict below.
        return "sigma" if sigma_same_leaf and normalized in {"sigma group", "sigma ai"} else normalized

    old_company = company_identity(original.company)
    new_company = company_identity(hydrated.company)
    if old_company and new_company and old_company != new_company:
        return "conflict"
    old_words = set(re.findall(r"[a-z0-9]+", old_title)) - {"remote", "job", "role"}
    new_words = set(re.findall(r"[a-z0-9]+", new_title)) - {"remote", "job", "role"}
    if old_words and new_words and len(old_words & new_words) / max(1, len(old_words)) < .5:
        return "conflict"
    languages = {"bengali", "bangla", "hindi", "dutch", "german", "french", "spanish", "japanese"}
    if (old_words & languages) and (new_words & languages) and not (old_words & new_words & languages):
        return "conflict"
    old_loc, new_loc = (original.location or "").casefold(), (hydrated.location or "").casefold()
    if old_loc and new_loc and "remote" not in old_loc + new_loc:
        countries = {"india", "united states", "canada", "uk", "germany", "netherlands"}
        if {c for c in countries if c in old_loc} and {c for c in countries if c in new_loc} and not any(c in old_loc and c in new_loc for c in countries):
            return "conflict"
    if posting_id and candidate_id and posting_id == candidate_id:
        return "exact"
    if old_title and new_title and old_title == new_title:
        return "corroborated"
    return "uncertain"


async def hydrate_public_posting(
    url: str, original: Job, *, client: httpx.AsyncClient, budget: RetrievalBudget
) -> HydrationResult:
    """Hydrate only exact public Ashby, Greenhouse, or Lever postings."""
    public = sanitize_url(url)
    parts = urlsplit(public)
    host = (parts.hostname or "").casefold()
    seg = [part for part in parts.path.split("/") if part]
    request_url = ""
    source = ""
    posting_id = ""
    if host == "jobs.ashbyhq.com" and len(seg) >= 2:
        source, posting_id = "ashby", seg[1]
        request_url = f"https://api.ashbyhq.com/posting-api/job-board/{seg[0]}"
    elif host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and len(seg) >= 3 and seg[-2] == "jobs":
        source, posting_id = "greenhouse", seg[-1]
        request_url = f"https://boards-api.greenhouse.io/v1/boards/{seg[0]}/jobs/{posting_id}"
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(seg) >= 2:
        source, posting_id = "lever", seg[1]
        api_host = "api.eu.lever.co" if host == "jobs.eu.lever.co" else "api.lever.co"
        request_url = f"https://{api_host}/v0/postings/{seg[0]}/{posting_id}"
    else:
        source = "first_party"
        request_url = public
    try:
        cached = budget.cache.get(request_url)
        if cached:
            try:
                cached_at = datetime.fromisoformat(str(cached["captured_at"]).replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
                if age_hours > budget.policy.cache_ttl_hours:
                    cached = None
            except (KeyError, TypeError, ValueError):
                cached = None
        if cached:
            response = httpx.Response(int(cached["status"]), content=str(cached["body"]).encode("utf-8"))
            final_url, trail = str(cached.get("final_url") or request_url), list(cached.get("trail") or [request_url])
        else:
            response, final_url, trail = await _get_redirected(client, budget, request_url)
        for _ in range(budget.policy.max_retries):
            if response.status_code not in {500, 502, 503, 504}:
                break
            response = await _request(client, budget, request_url)
    except (httpx.HTTPError, RuntimeError) as exc:
        return HydrationResult(state="deferred" if "budget" in str(exc) or "cooldown" in str(exc) else "failed", source=source, request_url=request_url, public_url=public, error=str(exc) or type(exc).__name__)
    if response.status_code == 429:
        budget.note_retry_after(request_url, response.headers.get("retry-after"))
        return HydrationResult(state="deferred", source=source, request_url=request_url, public_url=public, error="http_429")
    if response.status_code >= 400:
        return HydrationResult(state="unavailable", source=source, request_url=request_url, public_url=public, error=f"http_{response.status_code}")
    if len(response.content) > budget.policy.max_body_bytes:
        return HydrationResult(state="partial", source=source, request_url=request_url, public_url=public, error="body_too_large")
    if not cached:
        budget.cache[request_url] = {"status": response.status_code, "body": response.text,
                                     "final_url": final_url, "trail": trail,
                                     "captured_at": datetime.now(timezone.utc).isoformat()}
        budget.persist()
    if source == "first_party":
        # A redirect to a generic root/index is unavailable, never proof of closure.
        original_path, final_path = urlsplit(public).path.rstrip("/"), urlsplit(final_url).path.rstrip("/")
        if (urlsplit(public).hostname or "").casefold() != (urlsplit(final_url).hostname or "").casefold():
            return HydrationResult(state="unavailable", source=source, request_url=request_url,
                                   public_url=public, error="redirect_identity_boundary")
        if original_path and (not final_path or final_path in {"/jobs", "/careers"}):
            return HydrationResult(state="unavailable", source=source, request_url=request_url, public_url=public, error="redirect_to_index")
        payload = _jobposting_payload(response.text)
        if payload is None:
            return HydrationResult(state="partial", source=source, request_url=request_url, public_url=public, error="jobposting_not_found")
        hydrated = _generic_job(payload, public, response.text)
        if hydrated is None:
            return HydrationResult(state="partial", source=source, request_url=request_url, public_url=public, payload=sanitize_payload(payload), error="incomplete_jobposting")
        identity = _exact_identity(original, hydrated, "", payload)
        valid = payload.get("validThrough")
        explicitly_closed = payload.get("isListed") is False
        if valid:
            try: explicitly_closed = explicitly_closed or datetime.fromisoformat(str(valid).replace("Z", "+00:00")) < datetime.now(timezone.utc)
            except (TypeError, ValueError): pass
        links = _LeafParser(); links.feed(response.text)
        apply_observed = any("apply" in (href or "").casefold() and _safe_public_http(sanitize_url(urljoin(public, href))) for href in links.hrefs)
        return HydrationResult(state="complete", job=hydrated, source=source, request_url=request_url, public_url=public, payload=sanitize_payload(payload), identity_state=identity,
                               captured_at=cached_at if cached else datetime.now(timezone.utc), from_cache=bool(cached),
                               vacancy_state="explicitly_closed" if explicitly_closed else "unknown",
                               application_route_state="observed_public_route" if apply_observed and not explicitly_closed else "account_unknown",
                               source_kind=public_page_kind(public))
    try:
        payload = response.json()
    except ValueError:
        return HydrationResult(state="failed", source=source, request_url=request_url, public_url=public, error="invalid_json")
    if source == "ashby":
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        payload = next((row for row in jobs or [] if str(row.get("id") or "") == posting_id or str(row.get("jobUrl") or "").rstrip("/").endswith("/" + posting_id)), None)
        if payload is None:
            return HydrationResult(state="unavailable", source=source, request_url=request_url, public_url=public, error="posting_not_found")
        payload = dict(payload, _slug=seg[0], _company=company_from_job_url(public))
    if source == "lever" and isinstance(payload, dict):
        named = []
        for block in payload.get("lists") or []:
            if isinstance(block, dict):
                named.append(f"{block.get('text') or ''}\n{re.sub(r'<[^>]+>', ' ', str(block.get('content') or ''))}")
        if named:
            payload = dict(payload)
            payload["descriptionPlain"] = "\n".join(filter(None, [
                str(payload.get("descriptionPlain") or ""), *named
            ]))
    if not isinstance(payload, dict):
        return HydrationResult(state="failed", source=source, request_url=request_url, public_url=public, error="invalid_payload")
    try:
        hydrated = normalize({"source": source, "raw": payload})
    except (KeyError, TypeError, ValueError) as exc:
        return HydrationResult(state="failed", source=source, request_url=request_url, public_url=public, error=f"normalize_{type(exc).__name__}")
    if hydrated is None:
        return HydrationResult(state="failed", source=source, request_url=request_url, public_url=public, error="unusable_hydration")
    hydrated.url = public
    identity = _exact_identity(original, hydrated, posting_id, payload)
    return HydrationResult(state="complete", job=hydrated, source=source, request_url=request_url, public_url=public, payload=sanitize_payload(payload), identity_state=identity,
                           captured_at=cached_at if cached else datetime.now(timezone.utc), from_cache=bool(cached),
                           vacancy_state="verified_open", application_route_state="observed_public_route",
                           task_availability_state="advertised_only")


def child_observation(parent: ListingObservation, outcome: HydrationResult) -> ListingObservation:
    """Create a NEW observation; never rewrite discovery provenance."""
    raw = outcome.payload
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
    child = ListingObservation(
        observation_id=f"hydration:{parent.observation_id}:{digest[:16]}",
        source=outcome.source or "hydration",
        raw_payload=raw,
        job=outcome.job.model_copy(deep=True) if outcome.job else None,
        publisher_domain=(urlsplit(outcome.public_url).hostname or "").casefold(),
        apply_domain=(urlsplit(outcome.public_url).hostname or "").casefold(),
        original_url=outcome.public_url,
        normalized_url=outcome.public_url,
        source_kind=outcome.source_kind,
        source_confidence=0.99 if outcome.identity_state == "exact" else 0.9,
        hydrated_source=outcome.source,
        resolution_attempted=True,
        resolution_error=outcome.error,
        repair_flags=["origin:hydration", f"parent:{parent.observation_id}", f"identity:{outcome.identity_state}", f"content:{outcome.state}"],
    )
    # Additive V5 model fields are populated when the primary owner adds them.
    accepted = outcome.state == "complete" and outcome.identity_state in {"exact", "corroborated"}
    for name, value in {
        "origin": "hydration",
        "parent_observation_ids": [parent.observation_id],
        "content_hash": digest,
        "captured_at": outcome.captured_at or datetime.now(timezone.utc),
        "resolution_state": "resolved",
        "content_state": outcome.state,
        "identity_state": outcome.identity_state,
        "vacancy_state": outcome.vacancy_state if accepted else "unknown",
        "application_route_state": outcome.application_route_state if accepted else "account_unknown",
        "task_availability_state": outcome.task_availability_state if accepted else "unknown",
        "retrieval_error": outcome.error,
        "truncated": outcome.state == "partial",
        "verified_open_at": (outcome.captured_at or datetime.now(timezone.utc)) if accepted and outcome.vacancy_state == "verified_open" else None,
        "extraction_version": "v5.0.0-rc1-retrieval",
    }.items():
        if name in type(child).model_fields:
            setattr(child, name, value)
    return project_observation(child)
