"""Remotive — public JSON, no key. https://remotive.com/api/remote-jobs

Verified shape: {"jobs": [{id,url,title,company_name,category,tags,job_type,
publication_date,candidate_required_location,salary,description}, ...]}.
We run one request per configured search term and merge (deduped downstream).
"""
from __future__ import annotations

import httpx

from ..config import CONFIG
from .base import Source
from .query_batch import fetch_queries

_API = "https://remotive.com/api/remote-jobs"


class RemotiveSource(Source):
    name = "remotive"

    @property
    def enabled(self) -> bool:
        return CONFIG.sources.remotive.enabled

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        def rows(payload):
            if not isinstance(payload, dict) or 'jobs' not in payload:
                raise ValueError('missing_jobs')
            return payload['jobs']
        return await fetch_queries(
            self, client, CONFIG.sources.remotive.searches, method='GET', url=_API, headers={},
            request_args=lambda term: {'params': {'search': term}},
            rows_from_payload=rows, valid_row=lambda row: isinstance(row, dict),
        )
