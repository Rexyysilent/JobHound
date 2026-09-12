"""RemoteOK — public JSON, no key. https://remoteok.com/api

First array element is metadata (a legal/notice object) — skip it. Requires a
real User-Agent (set on the shared client). Fields: position/title, company,
location, tags, url, date, salary_min, salary_max, description.
"""
from __future__ import annotations

import httpx

from ..config import CONFIG
from .base import Source

_API = "https://remoteok.com/api"


class RemoteOkSource(Source):
    name = "remoteok"

    @property
    def enabled(self) -> bool:
        return CONFIG.sources.remoteok.enabled

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        resp = await client.get(_API)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        # Drop the leading metadata/legal element; keep real postings only.
        return [item for item in data[1:] if isinstance(item, dict) and item.get("id")]
