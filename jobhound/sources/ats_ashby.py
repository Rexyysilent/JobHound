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
from .ats_batch import fetch_boards

_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class AshbySource(Source):
    name = "ashby"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.sources.ats
        return cfg.enabled and bool(cfg.ashby)

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        return await fetch_boards(
            self, client, CONFIG.sources.ats.ashby,
            api=_API, params={"includeCompensation": "true"}, items_key="jobs",
            require_listed=True,
        )
