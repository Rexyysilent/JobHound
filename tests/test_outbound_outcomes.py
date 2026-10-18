"""R37: reviewed outbound evidence stays distinct from buyer progression."""
from datetime import timedelta
import itertools
import json

import pytest

from jobhound.config import CONFIG
from jobhound.delivery_outbox import evaluated_row
from jobhound.handoff_signal import handoff_material_projection, selected_outcome_facts, _select_actions
from jobhound.review_audit import material_projection
from jobhound.v41.engine import decision_fingerprint
from jobhound.v41.outcome_review import preview_outcomes, run_outcome_review
from jobhound.v41.outcomes import OutboundIdentity
from test_v55_action_policy import NOW, DESCRIPTION, observation
from test_v57_outcomes import setup, event
from test_assessment_revisions import ready as assessment_ready
from test_work_outcomes import ready as work_ready
from test_handoff_signals import later
from test_v6_delivery_recovery import store, count


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


ORIGINAL = dict(message_id='message-1', contact_id='contact-1')
ALTERNATE = dict(message_id='message-2', contact_id='contact-2')


def outbound(binding, key, predicate, value, identity=ORIGINAL, **updates):
    actor = 'provider' if predicate == 'outbound_state' and value != 'sent' else 'user'
    return event(binding, key, predicate, value, **(dict(schema_version=4,
        outbound=identity, actor=actor, source_kind='mail' if actor == 'provider' else 'user_report') | updates))


def bounced_and_alternate(binding):
    return [outbound(binding, 'bounce', 'outbound_state', 'bounced'),
            outbound(binding, 'alternate-origin', 'contact_origin', 'generated', ALTERNATE),
            outbound(binding, 'alternate-send', 'outbound_state', 'sent', ALTERNATE),
            outbound(binding, 'alternate-accepted', 'outbound_state', 'provider_accepted', ALTERNATE,
                     event_at=(NOW-timedelta(minutes=30)).isoformat(), observed_at=NOW.isoformat(),
                     supersedes=['alternate-send'])]


def preview(result, binding, records, **kwargs):
    return preview_outcomes(result, records, [binding], as_of=NOW, **kwargs)


def test_r37_bounce_alternate_preserve_discovery_counts_provenance_and_no_reply():
    result, binding = setup(observation(), observation('other'))
    before = result.model_dump_json()
    changed, report = preview(result, binding, bounced_and_alternate(binding))
    projection = report['projections'][0]
    messages = {row['message_id']: row for row in projection.outbound_status()}
    assert messages['message-1']['state'] == 'bounced'
    assert messages['message-2']['state'] == 'provider_accepted'
    assert messages['message-2']['contact_origin'] == 'generated'
    assert all(row['verification_state'] == 'unverified_by_outbound_event' for row in messages.values())
    assert messages['message-1']['evidence'][0]['evidence_ref'] == 'synthetic:message:bounce'
    assert messages['message-1']['evidence'][0]['event_at'] is not None
    assert projection.facts('buyer_reply') == []
    assert projection.facts('collected_payment') == []
    assert changed.counts == result.counts and changed.accounting_ok
    assert decision_fingerprint(changed) == decision_fingerprint(result)
    assert result.model_dump_json() == before
    for left, right in zip(result.evaluated, changed.evaluated):
        assert material_projection(evaluated_row(left)) == material_projection(evaluated_row(right))
        assert handoff_material_projection(evaluated_row(left)) == handoff_material_projection(evaluated_row(right))
    assert selected_outcome_facts(projection.model_dump(mode='json')) is None


def test_outbound_identity_isolates_message_and_contact_and_supersession():
    result, binding = setup()
    rows = [outbound(binding, 'original', 'outbound_state', 'bounced')]
    for key, identity in [('message', dict(ORIGINAL, message_id='other')),
                          ('contact', dict(ORIGINAL, contact_id='other'))]:
        rows.append(outbound(binding, key, 'outbound_state', 'provider_accepted', identity,
            event_at=(NOW-timedelta(minutes=30)).isoformat(), observed_at=NOW.isoformat(), supersedes=['original']))
    _, report = preview(result, binding, rows)
    assert report['counts']['quarantined'] == 2
    assert report['projections'][0].value('outbound_state', outbound=OutboundIdentity(**ORIGINAL)) == 'bounced'


@pytest.mark.parametrize('field', ['provider', 'portal', 'role', 'account', 'attempt'])
def test_unbound_scope_never_changes_any_opportunity(field):
    result, binding = setup()
    raw = outbound(binding, 'foreign', 'outbound_state', 'bounced')
    raw['scope'][field] = 'other'
    changed, report = preview(result, binding, [raw])
    assert report['quarantine'] == [dict(line=1, code='unmatched_scope')]
    assert decision_fingerprint(result) == decision_fingerprint(changed)


def test_shuffle_and_reimport_are_deterministic_and_idempotent():
    result, binding = setup()
    records = bounced_and_alternate(binding)
    changed, report = preview(result, binding, records)
    for order in itertools.permutations(records):
        shuffled, imported = preview(result, binding, list(order))
        assert imported['projection_fingerprint'] == report['projection_fingerprint']
        assert decision_fingerprint(shuffled) == decision_fingerprint(changed)
    again, repeated = preview(result, binding, records, previous=report['events'])
    assert repeated['counts'] == dict(input=4, accepted=0, duplicate=4, quarantined=0)
    assert repeated['projection_fingerprint'] == report['projection_fingerprint']
    assert decision_fingerprint(again) == decision_fingerprint(changed)


@pytest.mark.parametrize('existing', [lambda b: [event(b, 'real-reply', 'buyer_reply', 'substantive', actor='buyer')],
                                       assessment_ready, work_ready])
def test_record_only_facts_and_conflicts_preserve_real_reply_assessment_and_work(existing):
    result, binding = setup()
    baseline, first = preview(result, binding, existing(binding))
    records = bounced_and_alternate(binding) + [outbound(binding, 'contradiction', 'outbound_state', 'provider_accepted')]
    changed, report = preview(baseline, binding, records)
    assert report['projections'][0].conflicts
    assert report['projections'][0].action_conflicts() == []
    assert decision_fingerprint(baseline) == decision_fingerprint(changed)
    assert baseline.counts == changed.counts
    left, right = evaluated_row(baseline.evaluated[0]), evaluated_row(changed.evaluated[0])
    assert material_projection(left) == material_projection(right)
    assert handoff_material_projection(left) == handoff_material_projection(right)
    assert set(e.event_id for e in first['events']) <= set(e.event_id for e in report['events'])


@pytest.mark.parametrize('update', [dict(reviewed=False), dict(reviewed=1), dict(schema_version=1),
    dict(outbound=None), dict(actor='buyer'), dict(value='delivered'), dict(source_kind='reviewed_seed'),
    dict(predicate='buyer_reply', value='substantive', actor='buyer'),
    dict(predicate='contact_verified', value='verified'), dict(value=True),
    dict(outbound=dict(message_id='message-1', contact_id='generated@example.org')),
    dict(support_sha256='missing'), dict(assessment=dict(step_id='one', revision='one')),
    dict(work=dict(project_id='one', allocation_id='one', revision='one'))])
def test_invalid_or_unreviewed_outbound_facts_are_quarantined_without_payload_echo(update):
    result, binding = setup()
    raw = outbound(binding, 'bad', 'outbound_state', 'bounced')
    raw.update(update)
    changed, report = preview(result, binding, [raw])
    assert report['counts']['quarantined'] == 1 and report['events'] == []
    assert report['quarantine'] == [dict(line=1, code='invalid_event_schema')]
    assert 'generated@example.org' not in json.dumps(report['quarantine'])
    assert decision_fingerprint(result) == decision_fingerprint(changed)


@pytest.mark.parametrize('handoff', [False, True])
def test_outbound_import_does_not_reissue_or_change_discovery_reservation(store, handoff):
    result, binding = setup()
    changed, _ = preview(result, binding, bounced_and_alternate(binding))
    options = dict(action_cap=5, verify_cap=2, total_cap=7)
    assert _select_actions(result.evaluated, **options)[2] == _select_actions(changed.evaluated, **options)[2]
    from test_v6_delivery_recovery import EMAIL
    store.record_delivery_run(result, [EMAIL], now=100, handoff_signal=handoff)
    repeated = store.record_delivery_run(later(changed), [EMAIL], now=101, handoff_signal=handoff)
    assert repeated['destinations'][0]['counts']['already_recorded'] == 1
    assert count(store, 'delivery_revisions') == count(store, 'delivery_intents') == 1


def test_review_artifact_roundtrip_preserves_outbound_history_without_provider_or_store(tmp_path, monkeypatch):
    from jobhound.v41.engine import evaluate_raw
    from jobhound.v41.outcomes import OutcomeBinding
    from jobhound.v41.replay import write_snapshot
    from jobhound.v41.store import V41Store
    from jobhound.store import Store
    def forbidden(*args, **kwargs):
        pytest.fail('production store called')
    monkeypatch.setattr(V41Store, '__init__', forbidden)
    monkeypatch.setattr(Store, '__init__', forbidden)
    raw = [dict(source='ashby', raw=dict(id='one', title='Bengali AI Response Evaluator',
        _company='Example', jobUrl='https://jobs.ashbyhq.com/example/one', location='India',
        isRemote=True, descriptionPlain=DESCRIPTION))]
    result = evaluate_raw(raw, as_of=NOW)
    snapshot = write_snapshot(raw, result, directory=tmp_path)
    item = result.evaluated[0]
    _, old = setup()
    binding = OutcomeBinding(scope=old.scope, canonical_id=item.canonical.canonical_id,
        observation_id=item.canonical.observations[0].observation_id, role_url=item.job.url, reviewed=True)
    bindings = tmp_path/'bindings.jsonl'
    bindings.write_text(binding.model_dump_json()+'\n', encoding='utf-8')
    events = tmp_path/'events.jsonl'
    events.write_text(''.join(json.dumps(row)+'\n' for row in bounced_and_alternate(binding)), encoding='utf-8')
    target, manifest = run_outcome_review(snapshot, events, bindings, tmp_path/'review', as_of=NOW)
    statuses = json.loads((target/'outcome_actions.json').read_text(encoding='utf-8'))
    assert len(statuses[0]['outbound_messages']) == 2
    assert manifest['import_counts']['accepted'] == 4
    assert manifest['production_store_called'] is manifest['delivery_called'] is False
    _, again = run_outcome_review(snapshot, events, bindings, tmp_path/'again', as_of=NOW,
                                 previous_events=target/'events.jsonl')
    assert again['import_counts']['duplicate'] == 4
    assert again['projection_fingerprint'] == manifest['projection_fingerprint']
