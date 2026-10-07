"""Real entrypoint regressions; synthetic network, receipts and SQLite only."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import sqlite3

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.bounded_transport import LimitedStream
from jobhound.filters.eligibility import default_profile
from jobhound.v41 import engine
from jobhound.v41.hydration import _generic_job
from jobhound.v41.models import ListingObservation
from jobhound.sources.remotive import RemotiveSource
from jobhound.sources.arbeitnow import ArbeitnowSource
from test_v55_action_policy import observation, NOW
from test_v6_delivery_recovery import store, release, EMAIL, TELEGRAM, stage, start, finish
from test_v6_continuity import run, raw


def test_conjoined_experience_keeps_each_skill_and_years(monkeypatch):
    monkeypatch.setattr(engine, 'default_profile', lambda: replace(default_profile(),
        documented_professional_years={'python': 5}))
    row = observation()
    row.job.description += '\nRequirements\n2 years of Python experience and 5 years of statistics experience required.'
    item = engine.evaluate_observations([row], as_of=NOW).evaluated[0]
    assert item.decision.action_band.value == 'reject'
    assert any('statistics:5_years' in b for b in item.assessment.blockers)


@pytest.mark.parametrize('approved', [False, 'false', 'true', 1, None])
def test_paid_action_requires_literal_boolean_approval(approved):
    row = observation()
    account = ListingObservation(observation_id='cost', source='account_state', origin='account_state',
        parent_observation_ids=[row.observation_id], captured_at=NOW,
        raw_payload={'action_cost': {'cash_required': 10, 'operator_approved': approved}})
    item = engine.evaluate_observations([row, account], as_of=NOW).evaluated[0]
    assert not item.assessment.action_cost_acceptable
    assert item.decision.next_action != 'apply'


def test_native_jobposting_salary_reaches_the_pay_floor():
    original = observation()
    payload = {'@type': 'JobPosting', 'title': original.job.title,
        'description': original.job.description, 'hiringOrganization': {'name': 'Example'},
        'jobLocationType': 'TELECOMMUTE', 'applicantLocationRequirements': {'@type': 'Country', 'name': 'India'},
        'datePosted': '2026-09-07T09:00:00Z',
        'baseSalary': {'@type': 'MonetaryAmount', 'currency': 'USD',
            'value': {'@type': 'QuantitativeValue', 'value': 1, 'unitText': 'HOUR'}}}
    job = _generic_job(payload, original.job.url, '')
    assert job.pay_raw and job.posted_at
    row = original.model_copy(update={'job': job, 'raw_payload': payload}, deep=True)
    item = engine.evaluate_observations([row], as_of=NOW).evaluated[0]
    assert item.assessment.selected_pay.amount_low == 1
    assert item.assessment.selected_pay.structured_path == '/baseSalary'
    assert len(item.assessment.selected_pay.structured_payload_sha256) == 64
    assert 'pay_below_floor' in item.assessment.blockers


@pytest.mark.parametrize('value,literal', [(1234567, '1234567'), (1.23456789, '1.23456789'), (0, '0')])
def test_native_amounts_preserve_precision_without_scientific_notation(value, literal):
    from jobhound.v41.hydration import _posting_salary
    assert _posting_salary({'baseSalary': {'currency': 'USD', 'value': {'value': value, 'unitText': 'HOUR'}}}) == f'USD {literal} per hour'


def test_preferred_experience_does_not_weaken_another_required_skill(monkeypatch):
    monkeypatch.setattr(engine, 'default_profile', lambda: replace(default_profile(),
        documented_professional_years={'python': 5}))
    row = observation()
    row.job.description += '\nRequirements\n2 years of Python experience preferred and 5 years of statistics experience required.'
    item = engine.evaluate_observations([row], as_of=NOW).evaluated[0]
    assert any('statistics:5_years' in b for b in item.assessment.blockers)


def test_a_nearby_skill_mention_cannot_satisfy_other_experience(monkeypatch):
    monkeypatch.setattr(engine, 'default_profile', lambda: replace(default_profile(),
        documented_professional_years={'python': 5}))
    row = observation()
    row.job.description += '\nRequirements\n5 years of statistics experience and Python familiarity required.'
    item = engine.evaluate_observations([row], as_of=NOW).evaluated[0]
    assert any('statistics:5_years' in b for b in item.assessment.blockers)


@pytest.mark.parametrize('connector,rejected', [('and', True), ('or', False)])
def test_shared_years_preserve_all_versus_any_skill_logic(monkeypatch, connector, rejected):
    monkeypatch.setattr(engine, 'default_profile', lambda: replace(default_profile(),
        documented_professional_years={'python': 5}))
    row = observation()
    row.job.description += f'\nRequirements\n5 years of Python {connector} statistics experience required.'
    item = engine.evaluate_observations([row], as_of=NOW).evaluated[0]
    assert (item.decision.action_band.value == 'reject') == rejected


def test_stream_cleanup_releases_both_gates_when_storage_fails():
    async def check():
        gate = asyncio.Semaphore(1)
        host = asyncio.Semaphore(1)
        await gate.acquire(); await host.acquire()
        def failed(*args):
            raise sqlite3.OperationalError('fixture receipt storage failure')
        stream = LimitedStream(httpx.ByteStream(b''), httpx.Request('GET', 'https://example.test'),
            SimpleNamespace(gate=gate, finish=failed), 1, 0, 'identity', host)
        with pytest.raises(sqlite3.OperationalError):
            await stream.aclose()
        assert gate._value == host._value == 1
        await stream.aclose()
        assert gate._value == host._value == 1
    asyncio.run(check())


def test_public_closure_notifies_only_a_previously_delivered_opening(store):
    stage(store)
    finish(store, start(store))
    rows = [raw('Pay USD 20 per hour.')]
    closed = engine.evaluate_raw(rows, as_of=NOW + timedelta(minutes=2))
    closed.evaluated[0].canonical.observations[0].vacancy_state = 'explicitly_closed'
    closed = engine.evaluate_observations(closed.evaluated[0].canonical.observations, as_of=NOW + timedelta(minutes=2))
    ledger = store.record_delivery_run(closed, (EMAIL,), now=200)
    assert ledger['destinations'][0]['counts']['selected_status'] == 1
    again = store.record_delivery_run(closed, (EMAIL,), now=201)
    assert again['destinations'][0]['counts']['selected_status'] == 1  # same run is idempotent
    assert len(store.delivery.inspect()) == 2


def test_never_delivered_rejections_do_not_flood_status_stream(store):
    result = run('Pay USD 1 per hour.')
    ledger = store.record_delivery_run(result, (EMAIL,), now=100)
    assert ledger['destinations'][0]['counts']['selected_status'] == 0


def test_closure_does_not_notify_a_destination_without_an_accepted_baseline(store):
    stage(store)
    finish(store, start(store))
    result = run('Pay USD 1 per hour.', minute=2)
    ledger = store.record_delivery_run(result, (EMAIL, TELEGRAM), now=200)
    assert ledger['destinations'][0]['counts']['selected_status'] == 1
    assert ledger['destinations'][1]['counts']['selected_status'] == 0
    assert all(sum(v for k, v in row['counts'].items() if k != 'total') == row['counts']['total'] for row in ledger['destinations'])


@pytest.mark.parametrize('old_url,new_url,mapped', [
    ('https://example.test/jobs/ABC', 'https://example.test/jobs/abc', 0),
    ('https://example.test/jobs?id=ABC', 'https://example.test/jobs?id=abc', 0),
    ('https://EXAMPLE.test/jobs/ABC', 'https://example.test/jobs/ABC', 1),
])
def test_legacy_crosswalk_preserves_case_sensitive_identity(tmp_path, old_url, new_url, mapped):
    from jobhound.v41.store import V41Store
    db = V41Store(tmp_path / 'crosswalk.sqlite')
    try:
        prior = run('Pay USD 20 per hour.', url=old_url)
        db.record_run(prior)
        db.mark_notified(prior.metadata.run_id, prior.evaluated, 'email', True)
        db.enable_delivery_review(review_root=tmp_path, workspace_id='fixture', profile_id='person')
        current = run('Pay USD 20 per hour.', url=new_url, identity='new')
        # Force distinct canonical IDs so the crosswalk, rather than clustering, is tested.
        current.evaluated[0].canonical.canonical_id = 'new-identity'
        assert db._crosswalk_legacy_urls(current)['mapped'] == mapped
    finally:
        db.close()


@pytest.mark.parametrize('provider', ['remotive', 'arbeitnow'])
def test_partial_fetch_retains_first_success(provider, monkeypatch):
    monkeypatch.setattr(CONFIG.sources.remotive, 'enabled', True)
    monkeypatch.setattr(CONFIG.sources.remotive, 'searches', ['first', 'second'])
    monkeypatch.setattr(CONFIG.sources.arbeitnow, 'enabled', True)
    monkeypatch.setattr(CONFIG.sources.arbeitnow, 'max_pages', 2)
    source = RemotiveSource() if provider == 'remotive' else ArbeitnowSource()
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            raise httpx.ReadTimeout('fixture timeout', request=request)
        return httpx.Response(200, json={('jobs' if provider == 'remotive' else 'data'): [{'id': 1, 'slug': 'one'}]})
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await source.fetch(client)
    rows = asyncio.run(fetch())
    assert len(rows) == 1
    assert source.health.status == 'partial'
    assert source.health.successful_requests == source.health.failed_requests == 1
    assert source.health.stages['discovery']['status'] == 'degraded'


@pytest.mark.parametrize('payload', [{}, {'data': None}, {'data': [{'slug': 'one'}, None]}])
def test_arbeitnow_invalid_payload_is_a_parse_failure(payload, monkeypatch):
    monkeypatch.setattr(CONFIG.sources.arbeitnow, 'enabled', True)
    source = ArbeitnowSource()
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
            return await source.fetch(client)
    assert asyncio.run(fetch()) == []
    assert source.health.failed_requests == 0
    assert source.health.successful_requests == 1
    assert source.health.stages['parsing']['failed'] == 1


def test_arbeitnow_429_preserves_prefix_and_reports_cooldown(monkeypatch):
    monkeypatch.setattr(CONFIG.sources.arbeitnow, 'enabled', True)
    monkeypatch.setattr(CONFIG.sources.arbeitnow, 'max_pages', 3)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'data': [{'slug': 'one'}]}) if len(calls) == 1 else httpx.Response(429, headers={'Retry-After': '60'})
    source = ArbeitnowSource()
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await source.fetch(client)
    assert len(asyncio.run(fetch())) == 1
    assert len(calls) == 2 and source.health.cooldown_until


def test_remotive_retains_changed_payloads_and_distinct_missing_ids(monkeypatch):
    monkeypatch.setattr(CONFIG.sources.remotive, 'enabled', True)
    monkeypatch.setattr(CONFIG.sources.remotive, 'searches', ['first', 'second'])
    source = RemotiveSource()
    async def fetch():
        def handler(request):
            title = request.url.params['search']
            return httpx.Response(200, json={'jobs': [{'id': 1, 'title': title}, {'title': title}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await source.fetch(client)
    assert len(asyncio.run(fetch())) == 4
