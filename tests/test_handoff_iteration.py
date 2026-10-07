"""Bounded-source completeness and explicit application-attempt selection."""
import asyncio

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.sources.arbeitnow import ArbeitnowSource
from jobhound.v41.outcomes import OutcomeBinding
from jobhound.v41.outcome_review import preview_outcomes
from test_v57_outcomes import setup, event, assessment_events, NOW


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def attempts():
    result, selected = setup()
    old_scope = selected.scope.model_copy(update={'attempt': 'previous-attempt'})
    historical = selected.model_copy(update={'scope': old_scope, 'active': False})
    return result, selected, historical


def test_historical_rejection_does_not_close_selected_invitation():
    result, selected, historical = attempts()
    records = [event(historical, 'old-rejection', 'application_state', 'rejected'),
               *assessment_events(selected, privacy=True, schedule=True, equipment=True, cost=True)]
    changed, report = preview_outcomes(result, records, [historical, selected], as_of=NOW)
    assert changed.evaluated[0].decision.next_action == 'complete_known_step'
    assert changed.evaluated[0].canonical.outcome_projection.scope == selected.scope
    assert len(report['events']) == len(records)
    assert report['projections'][0].value('application_state') == 'rejected'
    assert report['active_attempts'] == [selected.model_dump(mode='json')]
    again, repeated = preview_outcomes(result, [], [selected, historical], as_of=NOW, previous=report['events'])
    assert again.metadata.run_id == changed.metadata.run_id
    assert len(repeated['events']) == len(records)


def test_selected_rejection_stays_closed_despite_historical_invitation():
    result, selected, historical = attempts()
    records = [event(selected, 'new-rejection', 'application_state', 'rejected'),
               *assessment_events(historical, privacy=True, schedule=True, equipment=True, cost=True)]
    changed, report = preview_outcomes(result, records, [historical, selected], as_of=NOW)
    assert changed.evaluated[0].decision.lifecycle == 'closed'
    assert changed.evaluated[0].decision.next_action == 'await_change'
    assert len(report['projections']) == 2


def test_multiple_selected_attempts_fail_closed():
    result, selected, historical = attempts()
    with pytest.raises(ValueError, match='multiple_active_attempts'):
        preview_outcomes(result, [], [selected, historical.model_copy(update={'active': True})], as_of=NOW)


@pytest.mark.parametrize('invalid', ['false', 'true', 0, 1, None])
def test_attempt_selection_requires_literal_boolean(invalid):
    _, selected, _ = attempts()
    raw = selected.model_dump(mode='json')
    raw['active'] = invalid
    with pytest.raises(ValueError):
        OutcomeBinding.model_validate(raw)


def test_historical_bindings_still_require_exact_url():
    result, selected, historical = attempts()
    with pytest.raises(ValueError, match='binding_url_mismatch'):
        preview_outcomes(result, [], [selected, historical.model_copy(update={'role_url': 'https://example.org/wrong'})], as_of=NOW)


def fetch(monkeypatch, responses, *, pages=1, limit=None):
    monkeypatch.setattr(CONFIG.sources.arbeitnow, 'enabled', True)
    monkeypatch.setattr(CONFIG.sources.arbeitnow, 'max_pages', pages)
    source = ArbeitnowSource(limit=limit)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=responses[len(requests)-1])
    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await source.fetch(client)
    return asyncio.run(collect()), source.health, requests


def test_full_nonterminal_page_ceiling_is_partial(monkeypatch):
    rows, health, requests = fetch(monkeypatch, [{'data': [{'slug': 'one'}], 'links': {'next': '?page=2'}}])
    assert len(rows) == len(requests) == 1
    assert health.status == 'partial'
    assert health.query_results[-1]['reason'] == 'page_budget_reached'
    assert health.failed_requests == 0 and health.stages['discovery']['deferred'] == 1


def test_explicit_last_page_is_complete_at_the_ceiling(monkeypatch):
    rows, health, requests = fetch(monkeypatch, [{'data': [{'slug': 'one'}], 'links': {'next': None}}], pages=3)
    assert len(rows) == len(requests) == 1
    assert health.status == 'ok' and health.stages['discovery']['deferred'] == 0


def test_within_page_result_ceiling_reports_omitted_records(monkeypatch):
    rows, health, _ = fetch(monkeypatch, [{'data': [{'slug': 'one'}, {'slug': 'two'}], 'links': {'next': None}}], limit=1)
    assert len(rows) == 1 and rows[0]['raw']['slug'] == 'one'
    assert health.status == 'partial'
    assert health.query_results[-1]['omitted_item_count'] == 1
    assert health.failed_requests == 0


def test_zero_page_budget_is_unexercised_coverage(monkeypatch):
    rows, health, requests = fetch(monkeypatch, [], pages=0)
    assert rows == requests == [] and health.status == 'partial'
    assert health.successful_requests == health.failed_requests == 0
    assert health.query_results[-1]['reason'] == 'page_budget_reached'


def test_empty_terminal_page_is_an_observed_empty_result(monkeypatch):
    rows, health, requests = fetch(monkeypatch, [{'data': []}])
    assert rows == [] and len(requests) == 1 and health.status == 'empty'
    assert health.stages['discovery']['deferred'] == 0


def test_empty_page_with_next_is_incomplete_at_the_ceiling(monkeypatch):
    rows, health, requests = fetch(monkeypatch, [{'data': [], 'links': {'next': '?page=2'}}])
    assert rows == [] and len(requests) == 1 and health.status == 'partial'
    assert health.query_results[-1]['reason'] == 'page_budget_reached'
