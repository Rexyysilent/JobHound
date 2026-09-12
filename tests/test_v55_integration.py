"""Verifier-owned rollback and successful-delivery compatibility controls."""
from datetime import timedelta

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.store import V41Store
from test_v55_action_policy import observation, NOW


def test_release_review_policy_is_not_production_default():
    from jobhound.config import V55Cfg
    assert V55Cfg().enabled is False


def test_release_cannot_accidentally_enter_legacy_production_path(monkeypatch):
    from argparse import Namespace
    import pytest
    from jobhound import cli
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    monkeypatch.setattr(cli,'_run_v41',lambda *_: pytest.fail('production path entered'))
    with pytest.raises(SystemExit,match='review-only'):
        cli.cmd_run(Namespace(engine='v4.2'))


def test_runtime_policy_changes_metadata_hash(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    first=evaluate_observations([observation()],as_of=NOW)
    monkeypatch.setattr(CONFIG.v55, 'max_verify_actions', 2)
    changed=evaluate_observations([observation()],as_of=NOW)
    assert first.metadata.config_hash != changed.metadata.config_hash
    assert first.metadata.run_id != changed.metadata.run_id


def test_snapshot_release_identity_survives_restored_default(tmp_path,monkeypatch):
    import json
    from jobhound.v41.engine import evaluate_raw,decision_fingerprint
    from jobhound.v41.replay import write_snapshot,replay
    from test_v55_action_policy import DESCRIPTION
    raw=[{'source':'ashby','raw':{'id':'one','title':'Bengali AI Response Evaluator','_company':'Example','jobUrl':'https://jobs.ashbyhq.com/example/one','location':'India','isRemote':True,'descriptionPlain':DESCRIPTION}}]
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    result=evaluate_raw(raw,as_of=NOW)
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    path=write_snapshot(raw,result,directory=tmp_path)
    header=json.loads(path.read_text(encoding='utf-8').splitlines()[0])
    assert header['release_policy']['enabled'] is True
    again=replay(path)
    assert again.metadata.engine_version=='v5.0.0-rc1'
    assert decision_fingerprint(result)==decision_fingerprint(again)
    assert CONFIG.v55.enabled is False


def test_previous_engine_and_release_share_additive_temp_sidecar(tmp_path,monkeypatch):
    path=tmp_path/'rollback-copy.db'
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    previous=evaluate_observations([observation()],as_of=NOW)
    old_item=previous.evaluated[0]
    store=V41Store(path)
    store.record_run(previous)
    store.mark_notified(previous.metadata.run_id,[old_item],'test',True)
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    release=evaluate_observations([observation()],as_of=NOW+timedelta(minutes=1))
    new_item=release.evaluated[0]
    store.register_identity_crosswalk(old_item.canonical.canonical_id,new_item.canonical.canonical_id,'same captured exact ATS URL and requisition')
    assert store.annotate_transitions(release)==[]
    store.record_run(release)
    store.close()
    # Roll back policy and reopen the migrated copy; do not touch live state.
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    rollback=evaluate_observations([observation()],as_of=NOW+timedelta(minutes=2))
    reopened=V41Store(path)
    assert reopened.annotate_transitions(rollback)==[]
    reopened.record_run(rollback)
    assert reopened.conn.execute('SELECT count(*) FROM runs').fetchone()[0]==3
    assert reopened._was_notified(old_item.canonical.canonical_id)
    reopened.close()


def test_failed_material_change_delivery_is_retried(tmp_path,monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    store=V41Store(tmp_path/'notification.db')
    initial=evaluate_observations([observation()],as_of=NOW)
    store.record_run(initial)
    store.mark_notified(initial.metadata.run_id,initial.evaluated,'test',True)
    row=observation(); row.job.pay_raw='USD 14 per working hour'
    changed=evaluate_observations([row],as_of=NOW+timedelta(minutes=1))
    first=store.annotate_transitions(changed)
    assert first and first[0].decision.notification_transition=='material_change'
    store.record_run(changed)
    store.mark_notified(changed.metadata.run_id,first,'test',False)
    retry=evaluate_observations([row],as_of=NOW+timedelta(minutes=2))
    assert store.annotate_transitions(retry)
    store.record_run(retry)
    store.mark_notified(retry.metadata.run_id,retry.evaluated,'test',True)
    unchanged=evaluate_observations([row],as_of=NOW+timedelta(minutes=3))
    assert store.annotate_transitions(unchanged)==[]
    store.close()
