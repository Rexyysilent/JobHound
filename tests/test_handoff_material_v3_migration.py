"""Proof-gated disposable v2 -> v3 material adoption; no provider calls."""
from datetime import timedelta
import json

import pytest

from jobhound.config import CONFIG
from jobhound.delivery_outbox import evaluated_row
from jobhound import handoff_signal as signal
from jobhound.review_audit import fingerprint
from test_community_threads import NOW, BODY, post, topic
from test_handoff_cross_capture_history import capture, evaluate
from test_handoff_signals import stage, later
from test_v6_delivery_recovery import EMAIL, TELEGRAM, store, start, finish, count


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def buyer(reply=None):
    original = post(text=BODY+' Budget: USD 500 fixed.',
        created_at=(NOW-timedelta(days=2)).isoformat(), updated_at=(NOW-timedelta(days=2)).isoformat())
    posts = [original]
    if reply:
        posts.append(post(2, 10, reply, created_at=(NOW-timedelta(hours=1)).isoformat()))
    return evaluate(capture([topic(*posts)], NOW))


def seed_v2(store, result, monkeypatch, *, schema=None):
    old = signal.handoff_material_projection_v2
    def project(row):
        material = old(row)
        if schema is not None:
            material['schema'] = schema
        return material
    with monkeypatch.context() as scope:
        scope.setattr(signal, 'handoff_material_projection', project)
        stage(store, result)
    finish(store, start(store))
    return store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0]


def durable_evidence(store):
    return dict(events=[tuple(row) for row in store.conn.execute('SELECT * FROM delivery_attempt_events ORDER BY id')],
                receipts=[tuple(row) for row in store.conn.execute('SELECT * FROM notification_events ORDER BY rowid')],
                accepted=[tuple(row) for row in store.conn.execute("SELECT id,revision,payload,attempts FROM delivery_intents WHERE status='accepted' ORDER BY id")])


def test_v2_thanks_material_is_verified_then_adopted_without_reissue_or_receipt(store, monkeypatch):
    first, thanks = buyer(), buyer('Thanks for your help!')
    left, right = evaluated_row(first.evaluated[0]), evaluated_row(thanks.evaluated[0])
    assert fingerprint(signal.handoff_material_projection_v2(left)) != fingerprint(signal.handoff_material_projection_v2(right))
    assert fingerprint(signal.handoff_material_projection(left)) == fingerprint(signal.handoff_material_projection(right))
    previous = seed_v2(store, first, monkeypatch)
    proof = durable_evidence(store)
    report = stage(store, later(thanks))['destinations'][0]
    assert report['migrations'] == {'adopted': 1}
    assert report['counts']['already_recorded'] == 1
    assert report['counts']['selected_cards'] == report['counts']['selected_status'] == 0
    assert count(store, 'delivery_material_adoptions') == 1
    assert count(store, 'delivery_intents') == 1
    assert durable_evidence(store) == proof
    migrated = store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0]
    assert migrated != previous
    assert json.loads(store.conn.execute('SELECT material FROM delivery_revisions WHERE id=?', (migrated,)).fetchone()[0])['schema'] == signal.MATERIAL_SCHEMA
    repeated = stage(store, later(later(thanks)))['destinations'][0]
    assert repeated['counts']['already_recorded'] == 1
    assert count(store, 'delivery_material_adoptions') == count(store, 'delivery_intents') == 1
    assert durable_evidence(store) == proof


@pytest.mark.parametrize('reply', ['The scope now requires self-hosted n8n migration.',
                                   'Updated budget: USD 800 fixed.',
                                   'I can pay USD 650 fixed for the workflow.'])
def test_genuine_scope_budget_or_unmarked_money_change_requires_new_action(store, monkeypatch, reply):
    first, changed = buyer(), buyer(reply)
    previous = seed_v2(store, first, monkeypatch)
    evidence = durable_evidence(store)
    assert signal.handoff_material_projection(evaluated_row(first.evaluated[0])) != signal.handoff_material_projection(evaluated_row(changed.evaluated[0]))
    report = stage(store, later(changed))['destinations'][0]
    assert report['counts']['selected_cards'] == 1
    assert count(store, 'delivery_material_adoptions') == 0
    assert count(store, 'delivery_intents') == 2
    assert store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0] == previous
    assert durable_evidence(store) == evidence


@pytest.mark.parametrize('damage', ['missing', 'tampered', 'corrupt-json', 'wrong-destination', 'unknown-schema'])
def test_missing_tampered_foreign_or_unknown_proof_holds_without_adoption_or_resend(store, monkeypatch, damage):
    result = buyer()
    previous = seed_v2(store, result, monkeypatch,
        schema='jobhound-material-action/future' if damage == 'unknown-schema' else None)
    if damage == 'missing':
        store.conn.execute("UPDATE delivery_intents SET status='superseded'")
    elif damage == 'tampered':
        raw = json.loads(store.conn.execute('SELECT payload FROM delivery_intents').fetchone()[0])
        claim = next(item for item in raw['assessment']['evidence'] if item.get('code') == 'buyer_terms_revision')
        claim['value']['revision_id'] = 'tampered'
        store.conn.execute('UPDATE delivery_intents SET payload=?', (json.dumps(raw),))
    elif damage == 'corrupt-json':
        store.conn.execute("UPDATE delivery_intents SET payload='invalid-json'")
    elif damage == 'wrong-destination':
        store.conn.execute('UPDATE delivery_intents SET destination=?', (TELEGRAM.key,))
    store.conn.commit()
    evidence = durable_evidence(store)
    report = stage(store, later(result))['destinations'][0]
    assert report['counts']['policy_review'] == 1
    assert report['counts']['selected_cards'] == report['counts']['selected_status'] == 0
    assert set(report['dispositions'].values()) == {'migration_review_required'}
    assert store.conn.execute('SELECT revision FROM delivery_baselines').fetchone()[0] == previous
    assert count(store, 'delivery_material_adoptions') == 0
    assert count(store, 'delivery_intents') == 1
    assert durable_evidence(store) == evidence


def test_adoption_is_destination_scoped_and_does_not_suppress_new_recipient(store, monkeypatch):
    result = buyer()
    seed_v2(store, result, monkeypatch)
    report = stage(store, later(result), targets=(EMAIL, TELEGRAM))
    assert report['destinations'][0]['migrations'] == {'adopted': 1}
    assert report['destinations'][1]['counts']['selected_cards'] == 1
    assert count(store, 'delivery_baselines') == 1
    assert store.conn.execute('SELECT channel FROM delivery_baselines').fetchone()[0] == 'email'


@pytest.mark.parametrize('version', ['v2', 'v3'])
def test_both_handoff_material_versions_require_pre_handoff_database_for_legacy_rollback(store, monkeypatch, version):
    result = buyer()
    if version == 'v2': seed_v2(store, result, monkeypatch)
    else: stage(store, result)
    intents = count(store, 'delivery_intents')
    with pytest.raises(ValueError, match='pre-handoff'):
        store.record_delivery_run(later(result), [EMAIL], now=103)
    assert count(store, 'delivery_intents') == intents
