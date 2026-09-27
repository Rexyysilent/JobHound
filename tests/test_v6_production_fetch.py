"""Production V6 fetch: scoped runs must use a bounded transport sized for real feeds.

Regression for the 2026-09-27 cutover: with both V6 gates on, the production
run entered a RunContext but called discovery without the run-owned transport
(ValueError before any fetch), and the review-sized defaults (2 MB decoded)
dropped the 6.2 MB LILT Ashby board, Arbeitnow and Scale AI.
"""
import argparse
import asyncio
import json

import httpx
import pytest

from jobhound import cli
from jobhound.bounded_transport import BoundedTransport, RequestLimits, RunBudget
from jobhound.config import CONFIG
from jobhound.run_context import run_scope


def v6_config():
    cfg = CONFIG.model_copy(deep=True)
    cfg.v55.enabled = True
    cfg.v55.production_approved = True
    return cfg


def remotive_handler(seen):
    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, json={'jobs': [{
            'id': 1, 'url': 'https://remotive.com/job/1', 'title': 'Hindi AI Trainer',
            'company_name': 'Example', 'category': 'AI', 'tags': [], 'job_type': 'contract',
            'publication_date': '2026-09-27T00:00:00', 'candidate_required_location': 'India',
            'salary': '', 'description': 'Evaluate Hindi responses.',
        }]})
    return handler


def test_scoped_production_fetch_uses_run_owned_bounded_transport(tmp_path):
    seen = []
    context = cli._production_context(v6_config(), root=tmp_path)
    args = argparse.Namespace(limit=None, source='remotive')
    with run_scope(context):
        raw, health = asyncio.run(cli._ingest_for_run(
            args, inner_transport=httpx.MockTransport(remotive_handler(seen))))
    assert seen, 'discovery never reached the transport'
    assert raw and raw[0]['raw']['title'] == 'Hindi AI Trainer'
    receipt = json.loads((tmp_path / 'runtime' / 'v6' / 'request_budget_receipt.json').read_text())
    assert receipt['limit'] == v6_config().v55.production_fetch.requests
    assert receipt['requests_reserved'] >= 1


def test_production_limits_admit_large_ats_board(tmp_path):
    body = b'{"jobs": [' + b'"' + b'x' * 7_000_000 + b'"]}'   # LILT board was 6.2 MB decoded
    handler = lambda request: httpx.Response(200, content=body)

    def fetch(limits, root):
        # A run identity is bound to its limits, so each limit set gets its own run.
        context = cli._production_context(v6_config(), root=root)

        async def get():
            transport = BoundedTransport(httpx.MockTransport(handler), RunBudget(context, limits))
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.get('https://api.ashbyhq.com/posting-api/job-board/lilt-production')
            await transport.close_owned()
            return len(response.content)

        with run_scope(context):
            return asyncio.run(get())

    with pytest.raises(httpx.ReadError, match='size_limit'):
        fetch(RequestLimits(), tmp_path / 'review')          # review-sized default drops it
    assert fetch(cli._production_limits(v6_config()), tmp_path / 'prod') == len(body)


def test_production_workspace_is_dedicated_runtime_folder(tmp_path):
    (tmp_path / 'run.py').write_text('')                   # repo root is refused by RunBudget
    context = cli._production_context(v6_config(), root=tmp_path)
    assert context.workspace != str(tmp_path.resolve())
    assert context.workspace.endswith(str((tmp_path / 'runtime' / 'v6').resolve())[-10:])
    RunBudget(context, cli._production_limits(v6_config()))  # must not raise


def test_unscoped_v42_fetch_keeps_legacy_unbounded_path(tmp_path):
    seen = []
    args = argparse.Namespace(limit=None, source='remotive')
    raw, _ = asyncio.run(cli._ingest_for_run(
        args, inner_transport=httpx.MockTransport(remotive_handler(seen))))
    assert seen and raw
    assert not (tmp_path / 'runtime').exists()
