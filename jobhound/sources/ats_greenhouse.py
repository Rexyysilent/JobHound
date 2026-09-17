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
from .ats_batch import fetch_boards

_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


class GreenhouseSource(Source):
    name = "greenhouse"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.sources.ats
        return cfg.enabled and bool(cfg.greenhouse)

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        return await fetch_boards(
            self, client, CONFIG.sources.ats.greenhouse,
            api=_API, params={"content": "true"}, items_key="jobs",
            require_listed=False,
        )
