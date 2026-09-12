"""Observation provenance, URL sanitation and canonical clustering."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rapidfuzz import fuzz

from ..config import CONFIG
from ..filters.listing_quality import (
    ATS_DOMAINS,
    BOARD_DOMAINS,
    EMPLOYER_JOB_DOMAINS,
    domain_tier,
)
from ..logging_utils import redact_sensitive
from ..models import Job
from ..normalize import normalize
from ..settings import settings
from ..text import normalize_match
from ..trust.registry import default_matcher
from .models import CanonicalJob, ListingObservation, SourceKind

_TRACKING_NAMES = {
    "source", "ref", "referrer", "tracking", "campaign", "campaignid",
    "mc_cid", "mc_eid", "gclid", "fbclid",
}
_SENSITIVE_NAMES = {
    "app_id", "appid", "app_key", "appkey", "api_key", "apikey", "key",
    "token", "access_token", "auth", "authorization",
}
_PRIVATE_RESOLUTION_NAMES = {"se", "v"}
_SOURCE_RANK = {
    SourceKind.ORIGINAL_EMPLOYER: 70,
    SourceKind.ORIGINAL_ATS: 65,
    SourceKind.EMAIL_OFFER: 60,
    SourceKind.REPUTABLE_BOARD: 50,
    SourceKind.AGGREGATOR_UNRESOLVED: 35,
    SourceKind.UNKNOWN: 25,
    SourceKind.CONTENT_FARM: 0,
}
_ATS_SOURCES = {"greenhouse", "lever", "ashby"}
_REPUTABLE_BOARD_SOURCES = {"remotive", "remoteok", "arbeitnow"}


def configured_secrets() -> tuple[str, ...]:
    return tuple(filter(None, (
        settings.telegram_bot_token,
        settings.email_app_password,
        settings.imap_app_password,
        settings.adzuna_app_id,
        settings.adzuna_app_key,
        settings.rapidapi_key,
        settings.serper_api_key,
        settings.gemini_api_key,
    )))


def sanitize_url(url: str | None) -> str:
    """Strip credentials/tracking while preserving harmless query semantics."""
    if not url:
        return ""
    try:
        parts = urlsplit(redact_sensitive(url, configured_secrets()))
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"}:
        return ""
    kept: list[tuple[str, str]] = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        low = name.casefold()
        if low.startswith("utm_") or low in _TRACKING_NAMES or low in _SENSITIVE_NAMES:
            continue
        kept.append((name, value))
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path or "/",
        urlencode(kept, doseq=True),
        "",
    ))


def public_url(url: str | None) -> str:
    """Return a display-safe URL without private Adzuna click parameters."""
    clean = sanitize_url(url)
    if not clean:
        return ""
    parts = urlsplit(clean)
    host = (parts.hostname or "").casefold()
    kept = list(parse_qsl(parts.query, keep_blank_values=True))
    if host in {"adzuna.in", "www.adzuna.in"}:
        kept = [
            (name, value)
            for name, value in kept
            if name.casefold() not in _PRIVATE_RESOLUTION_NAMES
        ]
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(kept, doseq=True),
        "",
    ))


def _valid_unicode(value: str) -> str:
    """Combine valid UTF-16 pairs and replace isolated surrogate units."""
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _sanitize_value(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, dict):
        return {
            _valid_unicode(redact_sensitive(str(key), secrets)): _sanitize_value(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = redact_sensitive(value, secrets)
        # Provider JSON occasionally contains UTF-16 surrogate code units
        # (including unpaired units).  Python can hold those in ``str`` but
        # UTF-8 output and hashing cannot.  Combine valid pairs and replace
        # only malformed units, deterministically, before the value reaches
        # normalization, snapshots, or identity hashes.
        redacted = _valid_unicode(redacted)
        # Snapshot payloads may nest provider click URLs several levels deep.
        # Exact URL values receive the stricter public projection as well.
        if redacted.strip().lower().startswith(("http://", "https://")):
            return public_url(redacted)
        return redacted
    return value


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_value(payload, configured_secrets())


def _domain(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def classify_source(job: Job) -> tuple[SourceKind, float]:
    """Classify provenance/actionability independently from fraud."""
    if job.source.startswith("email:"):
        return SourceKind.EMAIL_OFFER, 0.95
    if job.source in _ATS_SOURCES:
        return SourceKind.ORIGINAL_ATS, 0.92

    if CONFIG.v55.enabled and job.source in {'serp', 'jsearch', 'adzuna'}:
        tier, confidence = domain_tier(job.url, job.company_domain)
        if tier == 'farm':
            return SourceKind.CONTENT_FARM, confidence
        return SourceKind.AGGREGATOR_UNRESOLVED, 0.60

    tier, confidence = domain_tier(job.url, job.company_domain)
    host = _domain(job.url)
    if tier == "farm":
        return SourceKind.CONTENT_FARM, confidence
    if tier == "employer" or host in EMPLOYER_JOB_DOMAINS or any(
        host.endswith(f".{item}") for item in EMPLOYER_JOB_DOMAINS
    ):
        return SourceKind.ORIGINAL_EMPLOYER, max(confidence, 0.90)
    if host in ATS_DOMAINS or any(host.endswith(f".{item}") for item in ATS_DOMAINS):
        return SourceKind.ORIGINAL_ATS, max(confidence, 0.90)
    if job.company_domain:
        company_host = _domain(
            job.company_domain if "://" in job.company_domain
            else f"https://{job.company_domain}"
        )
        if company_host and (host == company_host or host.endswith(f".{company_host}")):
            return SourceKind.ORIGINAL_EMPLOYER, max(confidence, 0.90)
    if job.source in _REPUTABLE_BOARD_SOURCES:
        return SourceKind.REPUTABLE_BOARD, max(confidence, 0.70)
    if job.source == "adzuna":
        return SourceKind.AGGREGATOR_UNRESOLVED, max(confidence, 0.65)
    if host in BOARD_DOMAINS or any(host.endswith(f".{item}") for item in BOARD_DOMAINS):
        return SourceKind.REPUTABLE_BOARD, max(confidence, 0.65)
    # JSearch/SERP may carry an original link, but without a verified company
    # domain we abstain rather than label it original.
    return SourceKind.UNKNOWN, confidence


def _raw_url(raw: dict[str, Any]) -> str:
    for key in (
        "job_apply_link", "absolute_url", "hostedUrl", "applyUrl", "jobUrl",
        "redirect_url", "url", "link", "job_google_link",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def build_observations(
    raw_records: list[dict[str, Any]],
) -> tuple[list[ListingObservation], list[ListingObservation]]:
    """Normalize every provider record without losing failures."""
    observations: list[ListingObservation] = []
    failures: list[ListingObservation] = []
    secrets = configured_secrets()
    for index, record in enumerate(raw_records):
        source = str(record.get("source") or "unknown")
        raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
        safe_raw = _sanitize_value(raw, secrets)
        digest = json.dumps(
            {"source": source, "raw": safe_raw},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        observation_id = hashlib.sha256(
            f"{index}|{digest}".encode("utf-8")
        ).hexdigest()[:24]
        # Keep private-but-required resolver parameters in memory; snapshots
        # project this through ``public_url`` separately.  Unicode repair must
        # not accidentally turn the operational URL into its display form.
        original_url = _valid_unicode(redact_sensitive(_raw_url(raw), secrets))
        try:
            job = normalize({"source": source, "raw": safe_raw})
        except Exception as exc:  # normalization failures are ledger entries
            observation = ListingObservation(
                observation_id=observation_id,
                source=source,
                raw_payload=safe_raw,
                original_url=original_url,
                normalization_error=f"{type(exc).__name__}: {exc}",
            )
            observations.append(observation)
            failures.append(observation)
            continue
        if job is None:
            observation = ListingObservation(
                observation_id=observation_id,
                source=source,
                raw_payload=safe_raw,
                original_url=original_url,
                normalization_error="unusable: missing title or URL",
            )
            observations.append(observation)
            failures.append(observation)
            continue

        resolution = record.get("_v41_resolution")
        if isinstance(resolution, dict):
            resolution = _sanitize_value(resolution, secrets)
        hydrated_source: str | None = None
        resolution_attempted = False
        resolution_error: str | None = None
        if isinstance(resolution, dict):
            resolution_attempted = bool(resolution.get("attempted"))
            if resolution.get("error"):
                resolution_error = str(resolution["error"])
        if isinstance(resolution, dict) and isinstance(
            resolution.get("hydrated_job"), dict
        ):
            try:
                job = Job.model_validate(resolution["hydrated_job"])
            except (TypeError, ValueError):
                pass
            else:
                hydrated_source = str(
                    resolution.get("hydrated_source") or job.source
                )

        job.url = public_url(job.url)
        redirect_trail: list[str] = []
        if isinstance(resolution, dict) and resolution.get("normalized_url"):
            job.url = public_url(str(resolution["normalized_url"]))
            redirect_trail = [
                sanitize_url(str(item))
                for item in (resolution.get("redirect_trail") or [])
                if sanitize_url(str(item))
            ]
        # Re-derive provenance with the current rules.  Snapshots preserve the
        # resolved public URL and trail, but an old ``source_kind`` must not
        # freeze a V4.1 misclassification (for example, official Alignerr was
        # captured as unknown before its employer domain was recognized).
        source_kind, confidence = classify_source(job)
        if isinstance(resolution, dict) and redirect_trail:
            try:
                recorded_kind = SourceKind(str(resolution.get("source_kind")))
            except ValueError:
                recorded_kind = source_kind
            first_host = _domain(redirect_trail[0])
            final_host = _domain(job.url)
            # A successful cross-host redirect proves first-party actionability
            # even when the destination employer is not yet in our registry.
            if (
                not CONFIG.v55.enabled
                and source_kind == SourceKind.UNKNOWN
                and recorded_kind == SourceKind.ORIGINAL_EMPLOYER
                and first_host
                and final_host
                and first_host != final_host
            ):
                source_kind = recorded_kind
                confidence = float(resolution.get("source_confidence", confidence))
        raw_title = ""
        for key in ("title", "job_title", "position", "text"):
            if isinstance(raw.get(key), str) and raw.get(key):
                raw_title = raw[key]
                break
        repair_flags = ["text_repaired"] if raw_title and raw_title != job.title else []
        observation = ListingObservation(
            observation_id=observation_id,
            source=source,
            raw_payload=safe_raw,
            job=job,
            publisher_domain=_domain(original_url or job.url),
            apply_domain=_domain(job.url),
            original_url=original_url,
            normalized_url=job.url,
            source_kind=source_kind,
            source_confidence=confidence,
            repair_flags=repair_flags,
            redirect_trail=redirect_trail,
            hydrated_source=hydrated_source,
            resolution_attempted=resolution_attempted,
            resolution_error=resolution_error,
        )
        observations.append(observation)
    return observations, failures


def _norm(value: str | None) -> str:
    folded = normalize_match(value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", folded).strip()


_COMPANY_ALIASES = {
    "thermofisher scientific": "thermo fisher scientific",
}


def _norm_company(value: str | None) -> str:
    normalized = _norm(value)
    return _COMPANY_ALIASES.get(normalized, normalized)


def _marketplace_counterparty(job: Job, platform_key: str) -> str | None:
    """Separate marketplace trust identity from each client's listing identity."""
    if platform_key != "upwork" or not job.url:
        return None
    path = urlsplit(job.url).path.rstrip("/")
    if "/freelance-jobs/apply/" not in path.casefold():
        return None
    stable_id = re.search(r"~\d{10,}", path)
    listing_key = stable_id.group(0) if stable_id else path.casefold()
    return f"marketplace:upwork:{listing_key}"


def counterparty_key(job: Job) -> str:
    """Stable identity shared by clustering, trust and digest caps."""
    platform_key = job.platform_key or default_matcher().match(job.company)
    if platform_key:
        marketplace_key = _marketplace_counterparty(job, platform_key)
        if marketplace_key:
            return marketplace_key
        return f"registry:{platform_key}"
    company = _norm_company(job.company)
    if company:
        return f"company:{company}"
    company_domain = _domain(
        job.company_domain if job.company_domain and "://" in job.company_domain
        else f"https://{job.company_domain}" if job.company_domain else ""
    )
    if company_domain:
        return f"domain:{company_domain}"
    host = _domain(job.url)
    path = urlsplit(job.url).path.strip("/").casefold() if job.url else ""
    # Unknown publishers must not all consume one global "?" company bucket.
    # A listing-local fallback is deliberately conservative: it prevents a
    # false merge until hydration supplies a real counterparty.
    return f"listing:{host}:{path}"


def _compatible(a: ListingObservation, b: ListingObservation) -> bool:
    if a.job is None or b.job is None:
        return False
    company_a, company_b = (
        _norm_company(a.job.company), _norm_company(b.job.company)
    )
    if not company_a or company_a != company_b:
        return False
    if fuzz.token_sort_ratio(_norm(a.job.title), _norm(b.job.title)) < 90:
        return False
    loc_a, loc_b = _norm(a.job.location), _norm(b.job.location)
    if loc_a and loc_b and loc_a != loc_b and not (a.job.is_remote and b.job.is_remote):
        return False
    return True


def _observation_quality(observation: ListingObservation) -> tuple[int, int, int, float]:
    job = observation.job
    assert job is not None
    return (
        _SOURCE_RANK[observation.source_kind],
        1 if job.pay_raw else 0,
        len(job.description or ""),
        observation.source_confidence,
        observation.observation_id,
    )


def _canonical_id(job: Job, identity_key: str) -> str:
    identity = "|".join((_norm(job.title), identity_key, _norm(job.location)))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def canonicalize_observations(
    observations: list[ListingObservation],
) -> list[CanonicalJob]:
    valid = [observation for observation in observations if observation.job is not None and observation.origin != 'account_state']
    if CONFIG.v55.enabled:
        valid.sort(key=lambda observation: observation.observation_id)
    clusters: list[list[ListingObservation]] = []
    clusters_by_company: dict[str, list[list[ListingObservation]]] = {}
    clusters_by_url: dict[str, list[ListingObservation]] = {}
    normalized: dict[str, tuple[str, str]] = {}
    for observation in valid:
        assert observation.job is not None
        identity_key = counterparty_key(observation.job)
        title = _norm(observation.job.title)
        location = _norm(observation.job.location)
        normalized[observation.observation_id] = (title, location)
        exact_url = public_url(observation.normalized_url).rstrip("/").casefold()
        if exact_url and exact_url in clusters_by_url and (
            not CONFIG.v55.enabled or (
                title == normalized[clusters_by_url[exact_url][0].observation_id][0]
                and location == normalized[clusters_by_url[exact_url][0].observation_id][1]
            )
        ):
            clusters_by_url[exact_url].append(observation)
            continue

        bucket_key = identity_key
        company_clusters = clusters_by_company.setdefault(bucket_key, [])
        for cluster in company_clusters:
            kept = cluster[0]
            assert kept.job is not None
            kept_title, kept_location = normalized[kept.observation_id]
            same_title = fuzz.token_sort_ratio(title, kept_title) >= 90
            compatible_location = (
                not location
                or not kept_location
                or location == kept_location
                or observation.job.is_remote and kept.job.is_remote
            )
            if CONFIG.v55.enabled:
                same_title = title == kept_title
                compatible_location = location == kept_location
                kept_url = public_url(kept.normalized_url).rstrip('/').casefold()
                if observation.source_kind == SourceKind.ORIGINAL_ATS and kept.source_kind == SourceKind.ORIGINAL_ATS:
                    same_title = same_title and exact_url == kept_url
            if same_title and compatible_location:
                cluster.append(observation)
                if exact_url:
                    clusters_by_url[exact_url] = cluster
                break
        else:
            cluster = [observation]
            company_clusters.append(cluster)
            if exact_url:
                clusters_by_url[exact_url] = cluster
            clusters.append(cluster)

    if CONFIG.v55.enabled:
        # A verified hydration child is the original behind its discovery row,
        # not another vacancy just because the copy and ATS URLs differ. Only
        # explicit accepted lineage can cross these strict identity boundaries.
        owners = {o.observation_id: index for index, cluster in enumerate(clusters) for o in cluster}
        roots = list(range(len(clusters)))

        def root(index: int) -> int:
            while roots[index] != index:
                roots[index] = roots[roots[index]]
                index = roots[index]
            return index

        for observation in valid:
            if (observation.origin != 'hydration'
                    or observation.identity_state not in {'exact', 'corroborated'}
                    or observation.source_kind == SourceKind.CONTENT_FARM):
                continue
            for parent_id in observation.parent_observation_ids:
                if parent_id in owners:
                    left, right = root(owners[observation.observation_id]), root(owners[parent_id])
                    roots[max(left, right)] = min(left, right)
        merged: dict[int, list[ListingObservation]] = {}
        for index, cluster in enumerate(clusters):
            merged.setdefault(root(index), []).extend(cluster)
        clusters = list(merged.values())

    canonical: list[CanonicalJob] = []
    for cluster in clusters:
        ordered = sorted(cluster, key=_observation_quality, reverse=True)
        selected = ordered[0]
        assert selected.job is not None
        job = selected.job.model_copy(deep=True)
        field_sources: dict[str, str] = {
            field: selected.observation_id
            for field in ("title", "company", "location", "url", "source")
        }

        # Description authority follows source authority. Richer low-tier copies
        # remain available as independent observations for evidence extraction;
        # length alone can never overwrite the canonical display description.
        description_source = next(
            (
                observation for observation in ordered
                if observation.source_kind != SourceKind.CONTENT_FARM
                and observation.job is not None
                and bool(observation.job.description)
            ),
            selected,
        )
        assert description_source.job is not None
        job.description = description_source.job.description
        field_sources["description"] = description_source.observation_id

        pay_source = next(
            (observation for observation in ordered
             if observation.job is not None and observation.job.pay_raw),
            None,
        )
        if pay_source is not None and pay_source.job is not None:
            job.pay_raw = pay_source.job.pay_raw
            field_sources["pay_raw"] = pay_source.observation_id

        job.seen_on = sorted({observation.source for observation in cluster})
        job.url = selected.normalized_url
        job.source = selected.job.source
        identity_key = counterparty_key(job)
        canonical_id = _canonical_id(job, identity_key)
        legacy_canonical_id = canonical_id
        if CONFIG.v55.enabled:
            # Preserve separate requisitions/contract variants even when their
            # title, company and city are identical. Expose the legacy key for
            # evidence-reviewed migration; never silently reset notifications.
            identity = canonical_id + '|' + public_url(job.url).rstrip('/')
            canonical_id = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]
        job.id = canonical_id
        canonical.append(CanonicalJob(
            canonical_id=canonical_id,
            legacy_canonical_id=legacy_canonical_id if CONFIG.v55.enabled else None,
            counterparty_key=identity_key,
            job=job,
            observations=ordered,
            field_sources=field_sources,
            alternate_urls=list(dict.fromkeys(
                observation.normalized_url for observation in ordered
                if observation.normalized_url and observation.normalized_url != job.url
            )),
            corroborated_by=job.seen_on,
            canonicalization_reason=(
                f"selected {selected.source_kind.value} observation "
                f"{selected.observation_id}; {len(ordered)} observation(s)"
            ),
        ))
    return canonical
