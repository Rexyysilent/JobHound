"""Approved production V5.5 runs hydrate like review capture does.

Gap (2026-09-27 audit): the production V5.5 path only ran the legacy URL
resolver, so the v5.5 R1 loop (fetch the matching original, append a child
observation, reassess in the same run) and the two-failed-cycles -> Watch
rule never ran in production; Verify cards promised automatic checks that
never happened. Production now hydrates through the same run-owned bounded
transport as discovery, with verification history in a stable namespace.
"""
import argparse
import asyncio
from pathlib import Path

import httpx

from jobhound import cli
from jobhound.bounded_transport import BoundedTransport
from jobhound.config import CONFIG
from jobhound.run_context import run_scope
from jobhound.v41 import engine, resolve
from test_v6_production_fetch import remotive_handler, v6_config


def args():
    return argparse.Namespace(limit=None, source='remotive', skip_llm=False)


def spy(calls, name, result=None):
    async def record(*a, **kw):
        calls.append((name, a, kw))
        return result
    return record


def test_production_v55_hydrates_with_run_transport_and_stable_namespace(tmp_path, monkeypatch):
    calls = []
    seen_transports = []
    real_ingest = cli.ingest_raw_with_health

    async def ingest(*a, transport=None, **kw):
        seen_transports.append(transport)
        return await real_ingest(*a, transport=transport, **kw)

    monkeypatch.setattr(cli, 'ingest_raw_with_health', ingest)
    monkeypatch.setattr(resolve, 'hydrate_result', spy(calls, 'hydrate', {'changed': 0}))
    monkeypatch.setattr(resolve, 'resolve_result_urls', spy(calls, 'legacy_resolve', 0))
    monkeypatch.setattr(engine, 'refine_scam_with_llm', spy(calls, 'llm', 0))
    context = cli._production_context(v6_config(), root=tmp_path)
    with run_scope(context):
        raw, result = asyncio.run(cli._fetch_and_evaluate(
            args(), inner_transport=httpx.MockTransport(remotive_handler([]))))
    assert raw and result.evaluated
    assert [name for name, _, _ in calls] == ['hydrate', 'llm']     # LLM after the rebuild
    _, hydrate_args, hydrate_kwargs = calls[0]
    assert hydrate_args[0] is result
    assert isinstance(hydrate_kwargs['transport'], BoundedTransport)
    assert hydrate_kwargs['transport'] is seen_transports[0]       # one budget owner
    assert Path(hydrate_kwargs['review_namespace']) == tmp_path / 'runtime' / 'v6' / 'retrieval'


def test_unscoped_v42_keeps_legacy_resolver_and_never_hydrates(monkeypatch):
    calls = []
    monkeypatch.setattr(resolve, 'hydrate_result', spy(calls, 'hydrate', {'changed': 0}))
    monkeypatch.setattr(resolve, 'resolve_result_urls', spy(calls, 'legacy_resolve', 0))
    monkeypatch.setattr(engine, 'refine_scam_with_llm', spy(calls, 'llm', 0))
    raw, result = asyncio.run(cli._fetch_and_evaluate(
        args(), inner_transport=httpx.MockTransport(remotive_handler([]))))
    assert [name for name, _, _ in calls] == ['llm', 'legacy_resolve']
    assert not CONFIG.v55.enabled


def test_skip_llm_is_honoured_on_the_v55_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(resolve, 'hydrate_result', spy(calls, 'hydrate', {'changed': 0}))
    monkeypatch.setattr(engine, 'refine_scam_with_llm', spy(calls, 'llm', 0))
    context = cli._production_context(v6_config(), root=tmp_path)
    skip = argparse.Namespace(limit=None, source='remotive', skip_llm=True)
    with run_scope(context):
        asyncio.run(cli._fetch_and_evaluate(
            skip, inner_transport=httpx.MockTransport(remotive_handler([]))))
    assert [name for name, _, _ in calls] == ['hydrate']
