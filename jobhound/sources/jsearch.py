"""JSearch (RapidAPI) — Google-for-Jobs index: covers LinkedIn/Indeed/Glassdoor
*legitimately* (HANDOFF §5/§11). Small free tier → few, targeted queries only;
the query list in config IS the request budget.

GET https://jsearch.p.rapidapi.com/search-v2?query=..&num_pages=..&date_posted=..&country=..

Verified live 2026-07-02 (v2 — the old /search endpoint is gone):
{"data": {"jobs": [{job_id, job_title, employer_name, job_publisher,
job_apply_link, job_google_link, job_description, job_is_remote,
job_posted_at_datetime_utc, job_city, job_state, job_country,
job_min_salary, job_max_salary, job_salary_period, job_salary_string}]}}.
No job_salary_currency in v2 — normalize falls back to job_salary_string.
"""
from __future__ import annotations

import logging

import httpx

from ..config import CONFIG
from ..settings import settings
from .base import Source

log = logging.getLogger("jobhound.sources")

_API = "https://jsearch.p.rapidapi.com/search-v2"
_HOST = "jsearch.p.rapidapi.com"


class JSearchSource(Source):
    name = "jsearch"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.sources.jsearch
        if cfg.enabled and not settings.jsearch_ready:
            log.warning("jsearch enabled but RAPIDAPI_KEY missing in .env — skipping")
            return False
        return cfg.enabled

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        cfg = CONFIG.sources.jsearch
        headers = {"X-RapidAPI-Key": settings.rapidapi_key, "X-RapidAPI-Host": _HOST}
        seen_ids: set = set()
        out: list[dict] = []
        for query in cfg.queries:
            params = {
                "query": query,
                "num_pages": cfg.num_pages,
                "date_posted": cfg.date_posted,
            }
            if cfg.country:
                params["country"] = cfg.country
            resp = await client.get(_API, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            # v2 wraps the list: {"data": {"jobs": [...]}}; tolerate the old flat list.
            jobs = data.get("jobs", []) if isinstance(data, dict) else data
            for job in jobs or []:
                jid = job.get("job_id")
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                out.append(job)
        return out
