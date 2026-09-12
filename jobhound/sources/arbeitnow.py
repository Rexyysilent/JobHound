"""Arbeitnow — public JSON, no key. https://www.arbeitnow.com/api/job-board-api

Verified shape: {"data": [{slug,company_name,title,description,remote,url,tags,
job_types,location,created_at}, ...], "links": {next}, ...}. Paginated via
?page=N; we walk up to config max_pages.
"""
from __future__ import annotations

import httpx

from ..config import CONFIG
from .base import Source

_API = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowSource(Source):
    name = "arbeitnow"

    @property
    def enabled(self) -> bool:
        return CONFIG.sources.arbeitnow.enabled

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        out: list[dict] = []
        for page in range(1, CONFIG.sources.arbeitnow.max_pages + 1):
            resp = await client.get(_API, params={"page": page})
            resp.raise_for_status()
            batch = resp.json().get("data", [])
            if not batch:
                break
            out.extend(batch)
        return out
