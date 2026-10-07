"""Native application controls must not become role requirements or pay."""
import asyncio
from datetime import datetime, timezone
import hashlib

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.v41.hydration import hydrate_public_posting, child_observation, RetrievalBudget, RetrievalPolicy
from jobhound.v41.engine import evaluate_observations
from test_exact_roles import TURING, MICRO, TITLE, turing_body, micro_body, description
from test_v55_action_policy import observation


def native(provider, extra):
    if provider == 'turing':
        return TURING, turing_body(extra=extra)
    return MICRO, micro_body(role_changes={'job_status':'open'}, text=description()+extra)


def engine(provider, extra):
    url, body = native(provider, extra)
    parent = observation()
    parent.job.url = parent.original_url = parent.normalized_url = url
    parent.job.title = TITLE
    parent.job.company = 'Turing' if provider == 'turing' else 'micro1'
    async def fetch():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=body))) as client:
            return await hydrate_public_posting(url, parent.job, client=client,
                budget=RetrievalBudget(RetrievalPolicy()))
    outcome = asyncio.run(fetch())
    assert outcome.state == 'complete'
    assert outcome.payload['native_body_sha256'] == hashlib.sha256(body.encode()).hexdigest()
    child = child_observation(parent, outcome)
    result = evaluate_observations([parent, child], as_of=datetime.now(timezone.utc))
    assert result.accounting_ok and len(result.evaluated) == 1
    return outcome, result.evaluated[0]


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


@pytest.mark.parametrize('provider', ['turing', 'micro1'])
def test_application_dropdown_cannot_create_a_foreign_language_requirement(provider):
    _, baseline = engine(provider, '')
    outcome, changed = engine(provider, '''<form><h2>Application form</h2>
        <label for="language">Languages</label><select id="language">
        <option>Dutch fluency required.</option><option>Bengali</option>
        <option>Japanese native speaker required.</option></select></form>''')
    assert not any('lang_mismatch:' in b for b in baseline.assessment.blockers)
    assert changed.assessment.blockers == baseline.assessment.blockers
    assert changed.decision.action_band == baseline.decision.action_band
    assert 'Dutch' not in outcome.job.description and 'Japanese' not in outcome.job.description
    omitted = outcome.payload['excluded_document_sections']
    assert len(omitted) == 1 and omitted[0]['tag'] == 'form'
    assert len(omitted[0]['text_sha256']) == 64 and omitted[0]['text_characters'] > 0


@pytest.mark.parametrize('provider', ['turing', 'micro1'])
def test_genuine_role_language_requirement_outside_form_still_blocks(provider):
    _, item = engine(provider, '<p>Dutch fluency required.</p>')
    assert any(b.endswith('lang_mismatch:dutch') for b in item.assessment.blockers)
    assert item.decision.action_band.value == 'reject'


@pytest.mark.parametrize('provider', ['turing', 'micro1'])
@pytest.mark.parametrize('control', [
    '<nav><p>Dutch fluency required.</p><p>Salary USD 900/hour.</p></nav>',
    '<select><option>Dutch fluency required.</option></select>',
    '<textarea>Dutch fluency required.</textarea>',
    '<div role="form"><h1>Application</h1><p>Dutch fluency required.</p></div>',
    '<div role="combobox"><p>Dutch fluency required.</p></div>',
    '<form><fieldset><legend>Requirements</legend><p>Dutch fluency required.</p></fieldset></form>',
])
def test_semantic_controls_and_navigation_cannot_change_role_evidence(provider, control):
    before, baseline = engine(provider, '')
    after, changed = engine(provider, control)
    assert after.job.description == before.job.description
    assert changed.assessment.blockers == baseline.assessment.blockers
    assert changed.assessment.selected_pay == baseline.assessment.selected_pay
    assert after.payload['excluded_document_sections']


@pytest.mark.parametrize('provider', ['turing', 'micro1'])
def test_filtering_form_cannot_erase_a_genuine_requirement(provider):
    _, item = engine(provider, '''<p>Dutch fluency required.</p>
        <form><select><option>Bengali</option></select></form>''')
    assert any(b.endswith('lang_mismatch:dutch') for b in item.assessment.blockers)
    assert item.decision.action_band.value == 'reject'
