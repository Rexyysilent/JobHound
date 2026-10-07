"""Arbeitnow — public JSON, no key. https://www.arbeitnow.com/api/job-board-api

Verified shape: {"data": [{slug,company_name,title,description,remote,url,tags,
job_types,location,created_at}, ...], "links": {next}, ...}. Paginated via
?page=N; we walk up to config max_pages.
"""
from __future__ import annotations

import httpx
from datetime import datetime, timezone

from ..config import CONFIG
from .base import Source
from ..bounded_transport import RequestDeferred
from .ats_batch import _cooldown

_API = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowSource(Source):
    name = "arbeitnow"

    @property
    def enabled(self) -> bool:
        return CONFIG.sources.arbeitnow.enabled

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        out: list[dict] = []
        for page in range(1, CONFIG.sources.arbeitnow.max_pages + 1):
            if self.limit is not None and len(out) >= max(0, self.limit):
                self.health.note_stage('discovery', 'deferred')
                self.health.query_results.append({'page': page, 'status': 'deferred', 'reason': 'result_budget_reached'})
                self.health.status = 'partial'
                break
            try:
                resp = await client.get(_API, params={"page": page})
                resp.raise_for_status()
            except RequestDeferred as exc:
                self.health.note_stage('discovery', 'deferred')
                self.health.query_results.append({'page': page, 'status': 'deferred', 'reason': str(exc)})
                self.health.status = 'partial'
                break
            except Exception as exc:
                self.health.failed_requests += 1
                self.health.error_type = type(exc).__name__
                self.health.note_stage('discovery', 'failed')
                self.health.query_results.append({'page': page, 'status': 'failed', 'error_type': type(exc).__name__})
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {429, 503}:
                    self.health.cooldown_until = _cooldown(exc.response.headers.get('Retry-After'), datetime.now(timezone.utc))
                # Later pages depend on this page: preserve the prefix and stop.
                break
            self.health.successful_requests += 1
            self.health.note_stage('discovery', 'succeeded')
            try:
                payload = resp.json()
                if not isinstance(payload, dict) or 'data' not in payload:
                    raise ValueError('missing_data')
                batch = payload['data']
                if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                    raise ValueError('invalid_data_payload')
            except (ValueError, TypeError):
                self.health.note_stage('parsing', 'failed')
                self.health.error_type = 'invalid_payload'
                self.health.query_results.append({'page': page, 'status': 'failed', 'reason': 'invalid_payload'})
                self.health.status = 'partial' if out else 'failed'
                break
            self.health.note_stage('parsing', 'succeeded')
            self.health.query_results.append({'page': page, 'status': 'ok', 'item_count': len(batch)})
            links = payload.get('links')
            if not batch:
                if isinstance(links, dict) and links.get('next'):
                    continue
                break
            remaining = len(batch) if self.limit is None else max(0, self.limit - len(out))
            out.extend(batch[:remaining])
            if remaining < len(batch):
                self.health.note_stage('discovery', 'deferred')
                self.health.query_results.append({'page': page, 'status': 'deferred',
                    'reason': 'result_budget_reached', 'omitted_item_count': len(batch) - remaining})
                self.health.status = 'partial'
                break
            # An explicit end-of-pagination signal avoids a false degradation
            # when the provider ends exactly at the configured page ceiling.
            if isinstance(links, dict) and 'next' in links and links['next'] is None:
                break
        else:
            self.health.note_stage('discovery', 'deferred')
            self.health.query_results.append({'page': CONFIG.sources.arbeitnow.max_pages + 1,
                'status': 'deferred', 'reason': 'page_budget_reached'})
            self.health.status = 'partial'
        if self.health.failed_requests:
            self.health.status = 'partial' if self.health.successful_requests else 'failed'
        return out
