"""Bounded witnesses for original R36 status and R43 selected-cohort scope."""
from copy import deepcopy
import pytest

from jobhound.config import CONFIG
from jobhound.delivery_transport import render_envelope
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.outcomes import OutcomeBinding, OutcomeScope, OutcomeEvent
from test_work_outcomes import ready, work_event, OTHER
from test_v55_action_policy import NOW, observation
from test_v57_outcomes import setup, event
from test_handoff_signals import stage
from test_v6_delivery_recovery import store


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def test_r36_below_floor_event_is_compact_status_and_does_not_displace_new_discovery(store):
    low = observation()
    low.job.company = 'Synthetic RWS'
    low.job.pay_raw = 'USD 2 per working hour'
    low.job.description += '\nAssigned tasks must be completed without AI assistance.'
    suitable = observation('new-role')
    suitable.job.company = 'Suitable new employer'
    result, binding = setup(low, suitable)
    changed, imported = preview_outcomes(result,
        [event(binding, 'submitted', 'application_state', 'submitted')], [binding], as_of=NOW)
    scoped = next(row for row in changed.evaluated if row.canonical.canonical_id == binding.canonical_id)
    assert scoped.decision.action_band.value != 'primary'
    assert 'pay_below_floor' in scoped.assessment.blockers
    assert 'without AI assistance' in scoped.job.description
    report = stage(store, changed)['destinations'][0]
    assert report['counts']['selected_cards'] == report['counts']['selected_status'] == 1
    assert report['reservation']['canonical_id'] != binding.canonical_id
    text = render_envelope(store.delivery.inspect(), now=100)
    assert 'Status updates' in text
    assert 'recorded application state: submitted' in text
    assert imported['counts']['accepted'] == 1
    # This witness proves status/economic separation and source-term retention;
    # explicit reviewed no-AI restrictions are exercised independently below.


def test_r43_three_selected_rejections_preserve_unknown_fourth_without_provider_rule():
    raw = [observation('attempt-'+str(i)) for i in range(4)]
    before = CONFIG.v55.model_dump_json()
    result = evaluate_observations(raw, as_of=NOW)
    bindings = []
    records = []
    for index, item in enumerate(result.evaluated):
        binding = OutcomeBinding(scope=OutcomeScope(provider='same-provider', portal='main',
            role='role-'+str(index), account='local', attempt='attempt-'+str(index)),
            canonical_id=item.canonical.canonical_id,
            observation_id=item.canonical.observations[0].observation_id,
            role_url=item.job.url, reviewed=True)
        bindings.append(binding)
        if index < 3:
            records.append(event(binding, 'rejection-'+str(index), 'application_state', 'rejected'))
    changed, imported = preview_outcomes(result, records, bindings, as_of=NOW)
    rejected = {binding.canonical_id for binding in bindings[:3]}
    assert len(imported['events']) == 3 and len(imported['projections']) == 4
    for item in changed.evaluated:
        if item.canonical.canonical_id in rejected:
            assert item.decision.next_action == 'await_change'
        else:
            assert item.canonical.outcome_projection is None
            assert item.decision.action_band.value == 'primary'
            assert item.decision.next_action == 'apply'
    assert imported['projections'][3].events == []
    assert CONFIG.v55.model_dump_json() == before
    assert changed.accounting_ok


@pytest.mark.parametrize('restriction', ['prohibited', 'allowed', None])
@pytest.mark.parametrize('completed', [False, True])
def test_reviewed_allocation_ai_terms_are_explicit_scoped_and_visible(store, restriction, completed):
    result, binding = setup()
    records = deepcopy(ready(binding))
    terms = next(row for row in records if row['predicate'] == 'work_terms')
    legacy = dict(terms['value'])
    if restriction is not None:
        terms['value']['ai_assistance'] = restriction
    validated = OutcomeEvent.model_validate(terms)
    if restriction is None:
        assert validated.model_dump(mode='json')['value'] == legacy
    if completed:
        records.append(work_event(binding, 'done', 'work_state', 'submitted', actor='user', source_kind='user_report'))
    changed, imported = preview_outcomes(result, records, [binding], as_of=NOW)
    assert imported['counts']['quarantined'] == 0
    row = changed.evaluated[0]
    if completed and restriction is None:
        assert row.assessment.account_state['_reviewed_work_action']['terms'] is None
    assert row.decision.next_action == ('await_response' if completed else 'start_allocated_task')
    assert ('prohibit AI assistance' in row.decision.next_step) == (restriction == 'prohibited')
    report = stage(store, changed)['destinations'][0]
    assert report['counts']['selected_status' if completed else 'selected_cards'] == 1
    text = render_envelope(store.delivery.inspect(), now=100)
    assert ('prohibit AI assistance' in text) == (restriction == 'prohibited')


def test_other_allocation_no_ai_term_cannot_change_selected_work():
    result, binding = setup()
    records = deepcopy(ready(binding))
    other = work_event(binding, 'other-terms', 'work_terms',
        dict(amount='10', currency='USD', reference='other', unit='labor_hour', ai_assistance='prohibited'), OTHER)
    changed, imported = preview_outcomes(result, records+[other], [binding], as_of=NOW)
    assert imported['counts']['quarantined'] == 0
    assert changed.evaluated[0].decision.next_action == 'start_allocated_task'
    assert 'prohibit AI assistance' not in changed.evaluated[0].decision.next_step


@pytest.mark.parametrize('value', [True, 'unknown', 'yes', 1, {}])
def test_ai_term_rejects_untyped_or_invented_permission(value):
    result, binding = setup()
    records = deepcopy(ready(binding))
    next(row for row in records if row['predicate'] == 'work_terms')['value']['ai_assistance'] = value
    changed, imported = preview_outcomes(result, records, [binding], as_of=NOW)
    assert imported['counts']['quarantined'] == 1
    assert changed.evaluated[0].decision.next_action != 'start_allocated_task'
