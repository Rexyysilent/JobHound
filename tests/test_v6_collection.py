import asyncio
import json
import pytest
import httpx

from jobhound.config import CONFIG
from jobhound.settings import settings
from jobhound.sources.jsearch import JSearchSource
from jobhound.sources.serp_dork import SerpDorkSource
from jobhound.sources.ats_greenhouse import GreenhouseSource
from jobhound.sources.ats_lever import LeverSource
from jobhound.sources.ats_ashby import AshbySource
from jobhound.sources.base import SourceHealth, summarize_stage_health


@pytest.fixture(params=['jsearch', 'serp'])
def query_source(request, monkeypatch):
    name = request.param
    cfg = CONFIG.sources.jsearch if name == 'jsearch' else CONFIG.sources.serp
    monkeypatch.setattr(cfg, 'enabled', True)
    monkeypatch.setattr(cfg, 'queries' if name == 'jsearch' else 'dorks', ['one', 'two', 'three'])
    monkeypatch.setattr(settings, 'rapidapi_key' if name == 'jsearch' else 'serper_api_key', 'synthetic-key')
    source = JSearchSource() if name == 'jsearch' else SerpDorkSource()
    def record(i, pay='10'):
        return {'job_id': i, 'job_title': 'Bengali trainer', 'job_description': pay} if name == 'jsearch' else {'title': 'Bengali trainer', 'link': 'https://example.org/' + str(i), 'snippet': pay}
    def payload(rows):
        return {'data': {'jobs': rows}} if name == 'jsearch' else {'organic': rows}
    return source, record, payload


def fetch(source, replies):
    calls = []
    def handler(request):
        calls.append(request)
        reply = replies[len(calls)-1]
        if reply == 'timeout':
            raise httpx.ReadTimeout('synthetic timeout', request=request)
        if isinstance(reply, int):
            return httpx.Response(reply, headers={'Retry-After': '17', 'Location': 'https://other.example/'})
        return httpx.Response(200, json=reply)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await source.fetch(client)
    return asyncio.run(run()), calls


def test_success_timeout_success_retained(query_source):
    source, item, payload = query_source
    rows, calls = fetch(source, [payload([item('a')]), 'timeout', payload([item('b')])])
    assert len(rows) == 2 and len(calls) == 3
    assert source.health.status == 'partial'
    assert source.health.successful_requests == 2 and source.health.failed_requests == 1
    assert source.health.stages['discovery']['attempted'] == 3
    assert source.health.stages['discovery']['status'] == 'degraded'


def test_changed_payload_preserved_exact_repeats_removed(query_source):
    source, item, payload = query_source
    rows, _ = fetch(source, [payload([item('a')]), payload([item('a', '20')]), payload([item('a')])])
    assert len(rows) == 2
    assert sum(r['duplicates'] for r in source.health.query_results) == 1
    assert source.health.query_results[0]['retained_fingerprints'] == source.health.query_results[2]['duplicate_fingerprints']
    assert all('_query' not in r['raw'] and 'captured_at' not in r['raw'] for r in rows)


def test_query_retention_reaches_real_canonical_pay_conflict(query_source, monkeypatch):
    from datetime import datetime, timezone
    from jobhound.v41.engine import evaluate_raw
    source, item, payload = query_source
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    old, new = item('same', 'Pay USD 10 per hour.'), item('same', 'Pay USD 30 per hour.')
    for record in (old, new):
        if 'job_title' in record:
            record.update(job_apply_link='https://jobs.example.org/same', employer_name='Example',
                          job_country='IN', job_is_remote=True)
    retained, calls = fetch(source, [payload([old]), 'timeout', payload([new])])
    result = evaluate_raw(retained, as_of=datetime(2026, 9, 19, tzinfo=timezone.utc))
    assert len(calls) == 3 and len(retained) == 2
    assert len(result.observations) == 2 and len(result.evaluated) == 1
    assert result.accounting_ok
    assessment = result.evaluated[0].assessment
    assert assessment.pay_conflict
    assert {c.amount_low for c in assessment.pay_candidates} == {10, 30}
    assert not any(c.corroborating_observation_ids for c in assessment.pay_candidates)


def test_jsearch_missing_ids_do_not_collapse(monkeypatch):
    monkeypatch.setattr(CONFIG.sources.jsearch, 'enabled', True)
    monkeypatch.setattr(CONFIG.sources.jsearch, 'queries', ['one'])
    monkeypatch.setattr(settings, 'rapidapi_key', 'synthetic-key')
    rows, _ = fetch(JSearchSource(), [{'data': {'jobs': [{'job_title': str(i)} for i in range(3)]}}])
    assert len(rows) == 3


@pytest.mark.parametrize('status', [401, 403, 429, 503])
def test_access_limits_stop_after_retaining_success(query_source, status):
    source, item, payload = query_source
    rows, calls = fetch(source, [payload([item('a')]), status])
    assert len(rows) == 1 and len(calls) == 2
    assert source.health.query_results[-1]['status'] == 'deferred'
    assert source.health.cooldown_until
    assert 'synthetic-key' not in json.dumps(source.health.as_dict())


@pytest.mark.parametrize('bad', [{}, {'data': None}, {'organic': None}])
def test_malformed_not_empty_and_later_success_survives(query_source, bad):
    source, item, payload = query_source
    rows, _ = fetch(source, [payload([item('a')]), bad, payload([])])
    assert len(rows) == 1
    assert source.health.status == 'partial'
    assert source.health.failed_requests == 0
    assert source.health.query_results[1]['reason'] == 'invalid_payload'


def test_bad_rows_do_not_discard_good_rows(query_source):
    source, item, payload = query_source
    rows, _ = fetch(source, [payload([None, {}, item('a')]), payload([]), payload([])])
    assert len(rows) == 1
    assert source.health.query_results[0]['malformed'] == 2


def test_zero_limit_does_not_send(query_source):
    source, _, _ = query_source
    source.limit = 0
    rows, calls = fetch(source, [])
    assert not rows and not calls
    assert source.health.status == 'partial'


def test_redirect_not_followed(query_source):
    source, item, payload = query_source
    rows, calls = fetch(source, [302, payload([item('a')]), payload([])])
    assert len(rows) == 1 and len(calls) == 3
    assert all(r.url.host != 'other.example' for r in calls)


@pytest.mark.parametrize('cls,key', [(GreenhouseSource, 'greenhouse'), (LeverSource, 'lever'), (AshbySource, 'ashby')])
def test_ats_board_failure_retains_success(cls, key, monkeypatch):
    monkeypatch.setattr(CONFIG.sources.ats, 'enabled', True)
    monkeypatch.setattr(CONFIG.sources.ats, key, {'a': 'A', 'b': 'B', 'c': 'C'})
    def payload(i):
        rows = [{'id': i, 'isListed': True}]
        return rows if key == 'lever' else {'jobs': rows}
    source = cls()
    rows, calls = fetch(source, [payload('a'), 'timeout', payload('c')])
    assert len(rows) == 2 and len(calls) == 3
    assert source.health.status == 'partial'
    assert source.health.stages['discovery']['attempted'] == 3
    assert rows[1]['raw']['_company'] == 'C'


def test_health_does_not_call_parse_failure_remote_error():
    health = SourceHealth('test', 'partial')
    health.note_stage('parsing', 'failed')
    health.note_stage('parsing', 'succeeded')
    assert health.stages['parsing']['status'] == 'degraded'
    assert summarize_stage_health(health)['remote_http_errors'] == 0
