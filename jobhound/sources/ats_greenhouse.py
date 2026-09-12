"""Greenhouse boards API — keyless, canonical-original postings (HANDOFF §6).

GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

Verified shape 2026-07-02: {"jobs": [{id, title, company_name, absolute_url,
updated_at, first_published, location: {name}, content (entity-escaped HTML),
departments, offices}]}. Slugs live in config `sources.ats.greenhouse`; each
raw item is tagged with _slug/_company for normalize.
"""
from __future__ import annotations

import httpx

from ..config import CONFIG
from .base import Source

_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


class GreenhouseSource(Source):
    name = "greenhouse"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.sources.ats
        return cfg.enabled and bool(cfg.greenhouse)

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        out: list[dict] = []
        for slug, company in CONFIG.sources.ats.greenhouse.items():
            resp = await client.get(_API.format(slug=slug), params={"content": "true"})
            resp.raise_for_status()
            for job in resp.json().get("jobs", []):
                job["_slug"], job["_company"] = slug, company
                out.append(job)
        return out
