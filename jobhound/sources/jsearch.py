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
from .query_batch import fetch_queries, jsearch_rows

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
        def request_args(query):
            params = {
                "query": query,
                "num_pages": cfg.num_pages,
                "date_posted": cfg.date_posted,
            }
            if cfg.country:
                params["country"] = cfg.country
            return {"params": params}
        return await fetch_queries(
            self, client, cfg.queries, method="GET", url=_API, headers=headers,
            request_args=request_args, rows_from_payload=jsearch_rows,
            valid_row=lambda row: isinstance(row.get("job_title"), str) and bool(row["job_title"].strip()),
        )
