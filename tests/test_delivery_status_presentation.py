"""Parked job updates must not look like fresh verification recommendations."""
import json
from datetime import timedelta

import pytest

from jobhound.config import CONFIG
from jobhound.delivery_outbox import evaluated_row
from jobhound.delivery_transport import render_envelope
from jobhound.v41.engine import evaluate_observations
from test_v55_action_policy import observation, NOW


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    monkeypatch.setattr(CONFIG.v55,'account_states',[])


def item(*, exhausted=False, closed=False):
    row=observation(content_state='snippet_only',vacancy_state='unknown')
    if exhausted:
        row.job.posted_at=NOW-timedelta(days=40)
        row.raw_payload['account_state']={'automatic_verification_cycles':4,
            'first_failed_verification_at':(NOW-timedelta(days=35)).isoformat()}
    if closed:
        row.content_state='complete';row.vacancy_state='explicitly_closed'
    return evaluate_observations([row],as_of=NOW).evaluated[0]


def render(*items):
    return render_envelope([{'payload':json.dumps(evaluated_row(row))} for row in items],now=NOW.timestamp())


def test_exhausted_native_engine_update_is_compact_watch():
    parked=item(exhausted=True)
    assert parked.decision.lifecycle=='watch'
    assert parked.assessment.lifecycle_reason in {'verification_exhausted','stale_unresolved_copy'}
    body=render(parked)
    assert '1 job status update' in body.splitlines()[0]
    assert 'WATCH | '+parked.job.title in body
    assert 'VERIFY |' not in body
    assert 'Check:' not in body and 'Ordering:' not in body and 'Pay:' not in body
    assert 'Await new evidence' in body
    assert parked.job.url in body


def test_active_verify_card_is_unchanged_by_status_presentation():
    active=item()
    assert active.decision.lifecycle=='active'
    body=render(active)
    assert '1 job card' in body.splitlines()[0]
    assert 'VERIFY |' in body and 'Check:' in body and 'Pay:' in body
    assert 'Status updates' not in body


def test_mixed_active_and_parked_updates_have_separate_presentation():
    body=render(item(),item(exhausted=True))
    assert '1 job card' in body.splitlines()[0]
    cards,status=body.split('Status updates')
    assert cards.count('VERIFY |')==1 and status.count('WATCH |')==1
    assert 'VERIFY |' not in status and 'Check:' not in status


def test_native_closed_update_preserves_closure_without_role_advertising():
    closed=item(closed=True)
    assert closed.decision.lifecycle=='closed'
    body=render(closed)
    assert 'CLOSED |' in body
    assert 'matching source explicitly closed' in body
    assert 'Pay:' not in body and 'Ordering:' not in body
    assert closed.job.url in body


@pytest.mark.parametrize('state',[
    {'application_state':'applied'},
    {'assessment_state':'completed'},
])
def test_completed_step_status_retains_waiting_advice(state,monkeypatch):
    row=observation()
    canonical_id=evaluate_observations([row],as_of=NOW).evaluated[0].canonical.canonical_id
    monkeypatch.setattr(CONFIG.v55,'account_states',[dict(state,canonical_id=canonical_id)])
    completed=evaluate_observations([row],as_of=NOW).evaluated[0]
    assert completed.decision.lifecycle=='watch'
    assert completed.decision.next_action=='await_response'
    body=render(completed)
    assert 'WATCH |' in body and 'do not repeat completed steps' in body
    assert completed.decision.next_step in body
    assert 'VERIFY |' not in body and 'APPLY |' not in body


def test_status_render_does_not_mutate_frozen_payload():
    parked=item(exhausted=True)
    payload=json.dumps(evaluated_row(parked),sort_keys=True)
    row={'payload':payload}
    first=render_envelope([row],now=NOW.timestamp())
    assert row['payload']==payload
    assert render_envelope([row],now=NOW.timestamp())==first
