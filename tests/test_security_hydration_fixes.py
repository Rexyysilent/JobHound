"""Offline regressions for certificate verification and untrusted posting bodies."""
import asyncio
from datetime import datetime, timezone
import json
import ssl

import httpx
import pytest

from jobhound.models import Job
from jobhound.notify.email import EmailNotifier
from jobhound.v41.hydration import (
    RetrievalBudget, RetrievalPolicy, _jobposting_payload, hydrate_public_posting,
)


URL = 'https://employer.example/jobs/1'
POSTING = {'@type': 'JobPosting', 'title': 'Bengali AI Evaluator',
           'hiringOrganization': {'name': 'Acme'},
           'description': 'Review Bengali AI outputs. Fluent Bengali required.'}


def script(payload):
    return '<script type="application/ld+json">' + json.dumps(payload) + '</script>'


@pytest.mark.parametrize('durable', [False, True])
def test_smtp_requires_verified_context_and_keeps_delivery_contract(monkeypatch, durable):
    calls = []

    class SMTP:
        def __init__(self, host, port, *, timeout, context=None):
            assert context is not None
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            assert (host, port, timeout) == ('smtp.gmail.com', 465, 30)
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def login(self, *args): calls.append('login')
        def send_message(self, msg, **kwargs):
            calls.append('send'); assert 'digest' in msg.get_content(); return {}
        def close(self): calls.append('close')

    monkeypatch.setattr('jobhound.notify.email.smtplib.SMTP_SSL', SMTP)
    notifier = EmailNotifier('sender@example.com', 'test-password', 'recipient@example.com')
    result = asyncio.run(notifier.send_receipt('digest', delivery_key='test')) if durable else asyncio.run(notifier.send('digest'))
    assert result.outcome == 'accepted' if durable else result is True
    assert calls == ['login', 'send', 'close']


@pytest.mark.parametrize('durable', [False, True])
def test_untrusted_smtp_certificate_fails_before_login(monkeypatch, durable):
    def rejected(*args, context=None, **kwargs):
        assert context is not None and context.check_hostname
        assert context.verify_mode == ssl.CERT_REQUIRED
        raise ssl.SSLCertVerificationError('offline untrusted certificate')
    monkeypatch.setattr('jobhound.notify.email.smtplib.SMTP_SSL', rejected)
    notifier = EmailNotifier('sender@example.com', 'test-password', 'recipient@example.com')
    result = asyncio.run(notifier.send_receipt('digest', delivery_key='test')) if durable else asyncio.run(notifier.send('digest'))
    assert result.outcome == 'not_sent' if durable else result is False


@pytest.mark.parametrize('payload', [POSTING, [POSTING], {'@graph': POSTING},
    {'@graph': [POSTING]}, dict(POSTING, **{'@type': ['Thing', 'JobPosting']})])
def test_legitimate_jsonld_forms(payload):
    assert _jobposting_payload(script(payload))['title'] == POSTING['title']


@pytest.mark.parametrize('bad', [{'@graph': 7}, {'@graph': None},
    {'@type': 7}, {'@type': None}, {'@type': ['JobPosting', {}]}])
def test_malformed_script_does_not_hide_later_valid_posting(bad):
    assert _jobposting_payload(script(bad) + script(POSTING)) == POSTING


@pytest.mark.parametrize('depth', [100, 1500])
def test_deep_json_cannot_escape_parser(depth):
    body = '<script type="application/ld+json">' + '[' * depth + '0' + ']' * depth + '</script>'
    assert _jobposting_payload(body + script(POSTING)) == POSTING


@pytest.mark.parametrize('bad', [{'@graph': 7}, dict(POSTING, baseSalary={
    'currency': 'USD', 'value': {'value': 10 ** 400}})])
def test_bad_bodies_are_isolated_and_not_cached(tmp_path, bad):
    async def run():
        budget = RetrievalBudget(RetrievalPolicy(), review_namespace=tmp_path / 'review')
        budget.cache[URL] = {'status': 200, 'body': script(bad), 'final_url': URL,
                             'captured_at': datetime.now(timezone.utc).isoformat()}
        responses = []
        def handler(request):
            responses.append(str(request.url)); return httpx.Response(200, text=script(POSTING))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            original = Job(source='aggregator', title=POSTING['title'], company='Acme', url=URL)
            first = await hydrate_public_posting(URL, original, client=client, budget=budget)
            assert first.state in {'failed', 'partial'}
            assert URL not in budget.cache and responses == []
            second = await hydrate_public_posting(URL, original, client=client, budget=budget)
            third = await hydrate_public_posting(URL, original, client=client, budget=budget)
        assert second.state == third.state == 'complete'
        assert third.from_cache and responses == [URL]
    asyncio.run(run())


def test_bad_listing_does_not_cancel_sibling_hydration(monkeypatch):
    from jobhound.config import CONFIG
    from jobhound.v41 import resolve
    from jobhound.v41.engine import evaluate_raw
    from jobhound.v41.hydration import HydrationResult
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    result = evaluate_raw([{'source': 'greenhouse', 'raw': {
        'id': i, 'title': f'Bengali AI Evaluator {i}', '_company': 'Acme',
        'absolute_url': f'https://boards.greenhouse.io/acme/jobs/{i}',
        'content': 'Remote contract. Review Bengali AI output. Fluent Bengali and English required.',
        'location': {'name': 'Remote'},
    }} for i in (1, 2)], as_of=datetime.now(timezone.utc))

    async def hydrate(url, job, **kwargs):
        if url.endswith('/1'): raise TypeError('untrusted shape')
        return HydrationResult(state='complete', job=job, source='greenhouse',
            public_url=url, request_url=url, payload={'id': 2}, identity_state='exact')
    monkeypatch.setattr(resolve, 'hydrate_public_posting', hydrate)
    summary = asyncio.run(resolve.hydrate_result(result, transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    stage = summary['stage_health']
    assert stage['attempted'] == 2 and stage['succeeded'] == 1 and stage['failed'] == 1
    assert any(obs.origin == 'hydration' for obs in result.observations)


def test_cancellation_is_not_reinterpreted_as_bad_content(monkeypatch):
    from jobhound.v41 import hydration
    async def cancelled(*args, **kwargs): raise asyncio.CancelledError()
    monkeypatch.setattr(hydration, '_get_redirected', cancelled)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
            with pytest.raises(asyncio.CancelledError):
                await hydrate_public_posting(URL, Job(source='test', title='Evaluator', url=URL),
                    client=client, budget=RetrievalBudget(RetrievalPolicy()))
    asyncio.run(run())
