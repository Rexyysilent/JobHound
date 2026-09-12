"""Remotive — public JSON, no key. https://remotive.com/api/remote-jobs

Verified shape: {"jobs": [{id,url,title,company_name,category,tags,job_type,
publication_date,candidate_required_location,salary,description}, ...]}.
We run one request per configured search term and merge (deduped downstream).
"""
from __future__ import annotations

import httpx

from ..config import CONFIG
from .base import Source

_API = "https://remotive.com/api/remote-jobs"


class RemotiveSource(Source):
    name = "remotive"

    @property
    def enabled(self) -> bool:
        return CONFIG.sources.remotive.enabled

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        seen_ids: set = set()
        out: list[dict] = []
        for term in CONFIG.sources.remotive.searches:
            resp = await client.get(_API, params={"search": term})
            resp.raise_for_status()
            for job in resp.json().get("jobs", []):
                jid = job.get("id")
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                out.append(job)
        return out
