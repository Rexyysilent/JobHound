"""Adzuna — free key, real salary fields (HANDOFF §5: salary → pay gate).

GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
    ?app_id=..&app_key=..&results_per_page=..&what=<term>

Verified shape: {"results": [{id, title, description, redirect_url, created,
company: {display_name}, location: {display_name}, salary_min, salary_max,
salary_is_predicted ("0"/"1"), ...}]}. Salaries are ANNUAL in the country's
local currency (config `currency`). Description is truncated by the API —
fine for scoring, the digest links to the original.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..config import CONFIG
from ..settings import settings
from .base import Source

log = logging.getLogger("jobhound.sources")

_API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _fallback_identity(job: dict, term: str) -> str:
    """Deterministic missing-ID key without signed URLs or credentials."""
    raw_url = str(job.get("redirect_url") or "")
    try:
        parts = urlsplit(raw_url)
        public_url = urlunsplit((
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path,
            "",
            "",
        ))
    except ValueError:
        public_url = ""
    company = job.get("company") or {}
    location = job.get("location") or {}
    payload = {
        "url": public_url,
        "title": str(job.get("title") or "").strip().casefold(),
        "company": str(
            company.get("display_name") if isinstance(company, dict) else company
        ).strip().casefold(),
        "location": str(
            location.get("display_name") if isinstance(location, dict) else location
        ).strip().casefold(),
        "term": term.strip().casefold(),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return "fallback:" + hashlib.sha256(encoded).hexdigest()[:24]


class AdzunaSource(Source):
    name = "adzuna"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.sources.adzuna
        if cfg.enabled and not settings.adzuna_ready:
            log.warning("adzuna enabled but ADZUNA_APP_ID/APP_KEY missing in .env — skipping")
            return False
        return cfg.enabled

    @staticmethod
    def _retry_delay(
        response: httpx.Response | None,
        attempt: int,
        base_seconds: float,
        now: datetime | None = None,
    ) -> float:
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    return min(30.0, max(0.0, float(raw)))
                except ValueError:
                    try:
                        target = parsedate_to_datetime(raw)
                        if target.tzinfo is None:
                            target = target.replace(tzinfo=timezone.utc)
                        current = now or datetime.now(timezone.utc)
                        return min(
                            30.0,
                            max(0.0, (target - current).total_seconds()),
                        )
                    except (TypeError, ValueError, OverflowError):
                        pass
        return max(0.0, base_seconds) * (2 ** attempt)

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        *,
        term: str,
        page: int,
    ) -> tuple[list[dict] | None, str | None]:
        cfg = CONFIG.sources.adzuna
        url = _API.format(country=cfg.country, page=page)
        params = {
            "app_id": settings.adzuna_app_id,
            "app_key": settings.adzuna_app_key,
            "results_per_page": cfg.results_per_page,
            "what": term,
        }
        for attempt in range(cfg.max_retries + 1):
            response: httpx.Response | None = None
            error_type: str | None = None
            try:
                response = await client.get(
                    url,
                    params=params,
                    timeout=cfg.request_timeout_seconds,
                )
                if response.status_code in _RETRYABLE_STATUS:
                    error_type = f"HTTP{response.status_code}"
                else:
                    response.raise_for_status()
                    payload = response.json()
                    results = payload.get("results", [])
                    if not isinstance(results, list):
                        return None, "InvalidPayload"
                    self.health.successful_requests += 1
                    return results, None
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                error_type = type(exc).__name__
            except httpx.HTTPStatusError as exc:
                return None, f"HTTP{exc.response.status_code}"
            except ValueError:
                return None, "InvalidJSON"

            if attempt >= cfg.max_retries:
                return None, error_type or "RequestFailure"
            self.health.retries += 1
            await asyncio.sleep(self._retry_delay(
                response, attempt, cfg.retry_backoff_seconds
            ))
        return None, "RequestFailure"

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        cfg = CONFIG.sources.adzuna
        seen_ids: set = set()
        out: list[dict] = []
        for term in cfg.searches:
            for page in range(1, cfg.max_pages + 1):
                results, error_type = await self._fetch_page(
                    client, term=term, page=page
                )
                if results is None:
                    self.health.failed_requests += 1
                    self.health.error_type = error_type
                    self.health.status = "partial" if out else "failed"
                    # One exhausted request is enough to open the source-level
                    # circuit. Preserve earlier pages instead of turning an
                    # outage into 20 more slow calls and then erasing ``out``.
                    return out
                for job in results:
                    jid = job.get("id") or _fallback_identity(job, term)
                    if jid in seen_ids:
                        continue
                    seen_ids.add(jid)
                    out.append(job)
                if len(results) < cfg.results_per_page:
                    break  # last page for this term
        return out
