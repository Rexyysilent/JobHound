"""Lever postings API — keyless, canonical-original postings (HANDOFF §6).

GET https://api.lever.co/v0/postings/{slug}?mode=json

Verified shape 2026-07-02: [{id, text (title), hostedUrl, applyUrl,
createdAt (MILLISECOND epoch), workplaceType ("remote"|...), country,
categories: {commitment, department, team, location, allLocations},
descriptionPlain, salaryRange?: {min, max, currency, interval}}].
Appen's board here is the AI-Trainers contractor lane — the exact target.
"""
from __future__ import annotations

import httpx

from ..config import CONFIG
from .base import Source
from .ats_batch import fetch_boards

_API = "https://api.lever.co/v0/postings/{slug}"


class LeverSource(Source):
    name = "lever"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.sources.ats
        return cfg.enabled and bool(cfg.lever)

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        return await fetch_boards(
            self, client, CONFIG.sources.ats.lever,
            api=_API, params={"mode": "json"}, items_key=None,
            require_listed=False,
        )
