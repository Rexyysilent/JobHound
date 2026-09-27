"""Scoped footer evidence through actual hydration and decision entrypoints."""
import asyncio
import json

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.v41.hydration import RetrievalBudget, RetrievalPolicy, hydrate_public_posting, child_observation
from jobhound.v41.engine import evaluate_observations
from test_v55_action_policy import observation, NOW, DESCRIPTION


def hydrate(footer, *, heading='Bengali AI Response Evaluator', company='Example'):
    parent=observation()
    parent.job.url='https://example.org/jobs/bengali-review'
    payload={'@type':'JobPosting', 'title':parent.job.title, 'description':DESCRIPTION,
             'hiringOrganization':{'name':company}, 'jobLocation':{'address':{'addressCountry':'India'}}}
    body=f'<h1>{heading}</h1><script type="application/ld+json">{json.dumps(payload)}</script><main><p>{DESCRIPTION}</p></main><footer>{footer}</footer>'
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=body))) as client:
            return await hydrate_public_posting(parent.job.url,parent.job,client=client,budget=RetrievalBudget(RetrievalPolicy()))
    result=asyncio.run(run())
    return parent,result,child_observation(parent,result)


def test_same_project_footer_closure_dominates_recruiting_text(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    parent,result,child=hydrate('<p>This project has been completed.</p><a href="/apply">Apply</a>')
    assert result.vacancy_state=='explicitly_closed'
    assert result.application_route_state=='account_unknown'
    assert result.payload['_closure_evidence']['text']=='This project has been completed.'
    evaluated=evaluate_observations([parent,child],as_of=max(NOW,child.captured_at))
    assert all(row.decision.action_band.value not in {'primary','verify'} for row in evaluated.evaluated)


@pytest.mark.parametrize('footer', [
    '<p>Another project has been completed.</p>',
    '<p>This project has not been completed.</p>',
    '<blockquote><p>This project has been completed.</p></blockquote>',
    '<div class="comments"><p>This project has been completed.</p></div>',
    '<script>This project has been completed.</script>',
    '<p hidden>This project has been completed.</p>',
    '<p>This project is accepting applications.</p>',
])
def test_unrelated_quoted_hidden_or_open_text_is_not_closure(monkeypatch,footer):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    _,result,child=hydrate(footer)
    assert result.vacancy_state!='explicitly_closed'
    assert '_closure_evidence' not in result.payload


def test_heading_and_identity_are_required(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    _,result,_=hydrate('<p>This project has been completed.</p>',heading='Other project')
    assert result.vacancy_state!='explicitly_closed'
    _,result,child=hydrate('<p>This project has been completed.</p>',company='Different employer')
    assert result.identity_state=='conflict'
    assert child.vacancy_state!='explicitly_closed'


def test_legacy_hydration_keeps_previous_behavior(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    _,result,_=hydrate('<p>This project has been completed.</p>')
    assert result.vacancy_state=='unknown'
