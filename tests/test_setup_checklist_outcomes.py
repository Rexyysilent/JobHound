"""R07 reviewed pre-allocation forms, separate from work/payment approval."""
from datetime import timedelta

import pytest

from jobhound.delivery_outbox import evaluated_row
from jobhound.handoff_signal import compact_status
from jobhound.v41.outcomes import OutcomeEvent, SetupIdentity
from jobhound.v41.outcome_review import preview_outcomes
from test_closure_outcome_requirements import native_subject, policy
from test_assessment_revisions import ready
from test_work_outcomes import ready as work_ready
from test_v57_outcomes import event, NOW
from test_handoff_signals import stage, later, material
from test_v6_delivery_recovery import store, start, finish, count

SETUP = dict(checklist_id='payment-forms', revision='terms-1')


def setup_fact(binding, key, predicate, value, **updates):
    return event(binding, key, predicate, value, schema_version=5, **updates)


def pending(binding):
    return [setup_fact(binding, 'setup-selection', 'setup_selection', SETUP),
        setup_fact(binding, 'old-reminder', 'setup_checklist', dict(SETUP, state='pending'),
            event_at=(NOW-timedelta(days=3)).isoformat(), observed_at=(NOW-timedelta(days=2)).isoformat())]


def completed(binding):
    return pending(binding)+[setup_fact(binding, 'user-completed', 'setup_checklist', dict(SETUP, state='completed'),
        actor='user', source_kind='user_report', event_at=(NOW-timedelta(hours=1)).isoformat(),
        observed_at=(NOW-timedelta(minutes=30)).isoformat(), supersedes=['old-reminder']),
        setup_fact(binding, 'details-mail', 'setup_details_changed', SETUP,
        event_at=(NOW-timedelta(minutes=20)).isoformat(), observed_at=(NOW-timedelta(minutes=10)).isoformat())]


def test_r07_native_reviewed_completion_retains_provenance_and_status_is_delivered_once(store):
    baseline, binding = native_subject()
    before, _ = preview_outcomes(baseline, pending(binding), [binding], as_of=NOW)
    assert before.evaluated[0].decision.next_action == 'verify'
    stage(store, before); finish(store, start(store))
    done, report = preview_outcomes(baseline, list(reversed(completed(binding))), [binding], as_of=NOW)
    item = done.evaluated[0]
    assert done.accounting_ok and item.decision.next_action == 'await_response'
    assert item.assessment.lifecycle_reason == 'setup_checklist_completed'
    projection = report['projections'][0]
    assert len(report['events']) == 4 and projection.dispositions['old-reminder'] == 'superseded'
    facts = projection.facts('setup_checklist', setup=SetupIdentity(**SETUP))
    assert facts[0].actor == 'user' and facts[0].source_kind == 'user_report'
    assert not any(fact.predicate in {'allocation_state', 'payable_approval', 'payment_setup', 'work_terms'} for fact in projection.events)
    text = compact_status(evaluated_row(item))
    assert 'user-reported setup checklist' in text and 'payable approval and task allocation unconfirmed' in text
    status = stage(store, later(done))['destinations'][0]
    assert status['counts']['selected_status'] == 1 and status['counts']['selected_cards'] == 0
    finish(store, start(store))
    total = count(store, 'delivery_intents')
    again, replay = preview_outcomes(baseline, completed(binding), [binding], as_of=NOW, previous=report['events'])
    assert replay['counts']['duplicate'] == 4
    assert stage(store, later(later(again)))['destinations'][0]['counts']['already_recorded'] == 1
    assert count(store, 'delivery_intents') == total


def test_details_changed_alone_does_not_complete_forms_or_reissue_completed_status():
    baseline, binding = native_subject()
    rows = pending(binding)+[completed(binding)[-1]]
    changed, _ = preview_outcomes(baseline, rows, [binding], as_of=NOW)
    assert changed.evaluated[0].decision.next_action == 'verify'
    left, _ = preview_outcomes(baseline, completed(binding)[:-1], [binding], as_of=NOW)
    right, _ = preview_outcomes(baseline, completed(binding), [binding], as_of=NOW)
    assert material(left) == material(right)


@pytest.mark.parametrize('change', ['undated', 'conflict', 'other-checklist'])
def test_setup_unknown_conflicting_or_different_selected_checklist_stays_verify(change):
    baseline, binding = native_subject()
    rows = completed(binding)
    if change == 'undated': rows[2]['event_at'] = None
    elif change == 'conflict': rows[2]['supersedes'] = []
    else: rows[0]['value'] = dict(SETUP, checklist_id='different-checklist')
    changed, _ = preview_outcomes(baseline, rows, [binding], as_of=NOW)
    assert changed.evaluated[0].decision.next_action == 'verify'


@pytest.mark.parametrize('state', ['assessment', 'work', 'blocked', 'rejected'])
def test_completed_setup_cannot_override_independent_action_or_account_state(state):
    baseline, binding = native_subject()
    extra = ready(binding) if state == 'assessment' else work_ready(binding) if state == 'work' else [event(binding, state,
        'project_access' if state == 'blocked' else 'application_state', 'blocked' if state == 'blocked' else 'rejected')]
    changed, _ = preview_outcomes(baseline, completed(binding)+extra, [binding], as_of=NOW)
    expected = {'assessment':'complete_known_step', 'work':'start_allocated_task', 'blocked':'verify', 'rejected':'await_change'}
    assert changed.evaluated[0].decision.next_action == expected[state]


def test_version5_does_not_weaken_legacy_serialization_or_outbound_boundary():
    _, binding = native_subject()
    old = OutcomeEvent.model_validate(event(binding, 'old', 'application_state', 'submitted')).model_dump(mode='json')
    assert 'setup' not in old and 'assessment' not in old and 'work' not in old and 'outbound' not in old
    assert OutcomeEvent.model_validate(old).model_dump(mode='json') == old
    for version in (1, 2, 3, 4):
        with pytest.raises(ValueError):
            OutcomeEvent.model_validate(dict(pending(binding)[0], schema_version=version))
    with pytest.raises(ValueError):
        OutcomeEvent.model_validate(dict(event(binding, 'bad', 'application_state', 'submitted'), schema_version=5))


@pytest.mark.parametrize('actor,source', [('user','mail'), ('provider','user_report')])
def test_setup_actor_source_cannot_forge_completion_provenance(actor, source):
    _, binding = native_subject()
    with pytest.raises(ValueError):
        OutcomeEvent.model_validate(setup_fact(binding, 'bad', 'setup_checklist', dict(SETUP, state='completed'), actor=actor, source_kind=source))


def test_setup_selection_conflict_cannot_veto_selected_assessment():
    baseline, binding = native_subject()
    rows = completed(binding)+[setup_fact(binding, 'other-selection', 'setup_selection', dict(SETUP, checklist_id='other'))]
    held, _ = preview_outcomes(baseline, rows, [binding], as_of=NOW)
    assert held.evaluated[0].decision.next_action == 'verify'
    actionable, _ = preview_outcomes(baseline, rows+ready(binding), [binding], as_of=NOW)
    assert actionable.evaluated[0].decision.next_action == 'complete_known_step'
