"""Ashby posting API — keyless, canonical-original postings (HANDOFF §6).

GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

Verified shape 2026-07-02: {"jobs": [{id, title, jobUrl, applyUrl, location,
secondaryLocations, isRemote, isListed, publishedAt, descriptionPlain,
descriptionHtml, compensation: {scrapeableCompensationSalarySummary, ...}}]}.
Unlisted postings are filtered here.
"""
from __future__ import annotations

import httpx

from ..config import CONFIG
from .base import Source

_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class AshbySource(Source):
    name = "ashby"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.sources.ats
        return cfg.enabled and bool(cfg.ashby)

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        out: list[dict] = []
        for slug, company in CONFIG.sources.ats.ashby.items():
            resp = await client.get(_API.format(slug=slug),
                                    params={"includeCompensation": "true"})
            resp.raise_for_status()
            for job in resp.json().get("jobs", []):
                if not job.get("isListed", True):
                    continue
                job["_slug"], job["_company"] = slug, company
                out.append(job)
        return out
