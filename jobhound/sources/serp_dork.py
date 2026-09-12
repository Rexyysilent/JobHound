"""SERP dorking via Serper.dev (HANDOFF v2 §11) — indirect, polite coverage of
hostile boards (LinkedIn/Wellfound) through Google's index. Low volume, garnish.

POST https://google.serper.dev/search  {"q": <dork>, "num": N}
headers: X-API-KEY.  Response: {"organic": [{title, link, snippet}]}.

Verified live 2026-07-07. FREE-TIER QUERY RULES: `site:` operators and
"quoted phrases" are rejected (400 "Query pattern not allowed for free
accounts"); bare domain tokens, parens and OR all pass — dorks in config.yaml
are written to that constraint. Results are thin (title+snippet), so listing
confidence stays low by construction and the digest links out to the original.
"""
from __future__ import annotations

import logging

import httpx

from ..config import CONFIG
from ..settings import settings
from .base import Source

log = logging.getLogger("jobhound.sources")

_API = "https://google.serper.dev/search"


class SerpDorkSource(Source):
    name = "serp"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.sources.serp
        if cfg.enabled and not settings.serper_api_key:
            log.warning("serp enabled but SERPER_API_KEY missing in .env — skipping")
            return False
        return cfg.enabled

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        cfg = CONFIG.sources.serp
        headers = {"X-API-KEY": settings.serper_api_key}
        seen_links: set = set()
        out: list[dict] = []
        for dork in cfg.dorks:
            resp = await client.post(_API, headers=headers,
                                     json={"q": dork, "num": cfg.num_results})
            resp.raise_for_status()
            for hit in resp.json().get("organic", []) or []:
                link = hit.get("link")
                if not link or link in seen_links:
                    continue
                seen_links.add(link)
                out.append(hit)
        return out
