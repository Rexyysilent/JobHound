"""Production cutover contracts. Synthetic stores/notifiers only; no network."""
from copy import deepcopy
from pathlib import Path

import pytest

from jobhound.config import CONFIG
from jobhound.delivery_outbox import Destination
from jobhound.run_context import RunContext, deny_review_side_effect, run_scope
from jobhound.v41.store import V41Store
from test_v6_continuity import run


EMAIL = Destination.from_address(
    'email',
    'smtp.gmail.com:465:sender@example.test:receiver@example.test',
)


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)


def test_review_context_denies_but_explicit_production_context_allows(tmp_path):
    with run_scope(RunContext.capture(workspace=tmp_path)):
        with pytest.raises(RuntimeError, match='review context forbids'):
            deny_review_side_effect('write')
    with run_scope(RunContext.capture(
        workspace=tmp_path,
        network_allowed=True,
        side_effects_allowed=True,
    )):
        deny_review_side_effect('write')


def test_production_delivery_requires_literal_approval(tmp_path):
    store = V41Store(tmp_path / 'cutover.sqlite')
    with pytest.raises(ValueError, match='literal production'):
        store.enable_delivery_production(
            workspace_id='fixture',
            profile_id='person',
            approved=False,
        )
    assert not store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='delivery_meta'"
    ).fetchone()
    store.close()


def test_legacy_success_is_adopted_once_then_future_change_is_pending(tmp_path):
    store = V41Store(tmp_path / 'cutover.sqlite')
    prior = run('Pay USD 20 per hour.')
    store.record_run(prior)
    store.mark_notified(
        prior.metadata.run_id,
        prior.evaluated,
        'email',
        True,
    )
    store.enable_delivery_production(
        workspace_id='fixture',
        profile_id='person',
        approved=True,
    )

    current = run('Pay USD 20 per hour.', minute=1)
    first = store.record_delivery_run(
        current,
        [EMAIL],
        now=100,
        adopt_legacy=True,
    )
    assert first['legacy_baselines_adopted'] == 1
    rows = store.delivery.inspect()
    assert len(rows) == 1 and rows[0]['status'] == 'accepted'
    assert store.delivery.claim_next(EMAIL, now=101) is None

    changed = run('Pay USD 30 per hour.', minute=2)
    second = store.record_delivery_run(
        changed,
        [EMAIL],
        now=200,
        adopt_legacy=True,
    )
    assert second['legacy_baselines_adopted'] == 0
    assert len(second['destinations'][0]['intent_ids']) == 1
    claim = store.delivery.claim_next(EMAIL, now=201)
    assert claim is not None and claim['status'] == 'leased'
    store.close()


def test_adopted_legacy_receipt_is_not_reimported_on_later_runs(tmp_path):
    # Production re-enables delivery (and re-imports legacy receipts) every run.
    # Regression: adoption deleted the legacy row, the next run re-imported it
    # from V4.2 history, and the job's next material change was swallowed as
    # "legacy adopted" again, on every change, for every job V4.2 ever sent.
    store = V41Store(tmp_path / 'cutover.sqlite')
    prior = run('Pay USD 20 per hour.')
    store.record_run(prior)
    store.mark_notified(prior.metadata.run_id, prior.evaluated, 'email', True)

    def next_run(quote, minute, now):
        store.enable_delivery_production(workspace_id='fixture', profile_id='person', approved=True)
        return store.record_delivery_run(run(quote, minute=minute), [EMAIL], now=now, adopt_legacy=True)

    assert next_run('Pay USD 20 per hour.', 1, 100)['legacy_baselines_adopted'] == 1
    changed = next_run('Pay USD 30 per hour.', 2, 200)
    assert changed['legacy_baselines_adopted'] == 0
    statuses = [store.delivery._owned(i)['status'] for i in changed['destinations'][0]['intent_ids']]
    assert statuses == ['pending']
    store.close()


def test_delivery_report_exposes_only_newly_selected_intent_ids(tmp_path):
    store = V41Store(tmp_path / 'cutover.sqlite')
    store.enable_delivery_production(
        workspace_id='fixture',
        profile_id='person',
        approved=True,
    )
    first = store.record_delivery_run(run(), [EMAIL], now=100)
    assert len(first['destinations'][0]['intent_ids']) == 1

    second = store.record_delivery_run(run(minute=1), [EMAIL], now=200)
    assert second['destinations'][0]['intent_ids'] == []
    assert second['destinations'][0]['counts']['already_recorded'] == 1
    store.close()


def test_exact_url_crosswalk_adopts_pre_v6_canonical_id(tmp_path):
    store = V41Store(tmp_path / 'cutover.sqlite')
    prior = run('Pay USD 20 per hour.')
    old_id = prior.evaluated[0].canonical.canonical_id
    store.record_run(prior)
    store.mark_notified(prior.metadata.run_id, prior.evaluated, 'email', True)
    store.enable_delivery_production(
        workspace_id='fixture',
        profile_id='person',
        approved=True,
    )

    current = run('Pay USD 20 per hour.', minute=1)
    current.evaluated[0].canonical.canonical_id = 'v6-' + old_id
    current.evaluated[0].job.id = 'v6-' + old_id
    report = store.record_delivery_run(
        current,
        [EMAIL],
        now=100,
        adopt_legacy=True,
    )
    assert report['legacy_crosswalk'] == {'mapped': 1, 'ambiguous': 0}
    assert report['legacy_baselines_adopted'] == 1
    row = store.delivery.inspect()[0]
    assert row['status'] == 'accepted'
    assert store.delivery.subject(old_id) == current.evaluated[0].canonical.canonical_id
    store.close()


def test_exact_url_crosswalk_allows_many_legacy_ids_for_one_current_id(tmp_path):
    store = V41Store(tmp_path / 'cutover.sqlite')
    old_ids = []
    for minute, prefix in enumerate(('old-a-', 'old-b-')):
        prior = run('Pay USD 20 per hour.', minute=minute)
        original = prior.evaluated[0].canonical.canonical_id
        prior.evaluated[0].canonical.canonical_id = prefix + original
        prior.evaluated[0].job.id = prefix + original
        old_ids.append(prefix + original)
        store.record_run(prior)
        store.mark_notified(prior.metadata.run_id, prior.evaluated, 'email', True)

    store.enable_delivery_production(
        workspace_id='fixture',
        profile_id='person',
        approved=True,
    )
    current = run('Pay USD 20 per hour.', minute=2)
    report = store.record_delivery_run(
        current,
        [EMAIL],
        now=100,
        adopt_legacy=True,
    )

    assert report['legacy_crosswalk'] == {'mapped': 2, 'ambiguous': 0}
    assert report['legacy_baselines_adopted'] == 1
    new_id = current.evaluated[0].canonical.canonical_id
    assert all(store.delivery.subject(old_id) == new_id for old_id in old_ids)
    assert store.delivery.inspect()[0]['status'] == 'accepted'
    store.close()


def test_exact_url_crosswalk_quarantines_ambiguous_current_ids(tmp_path):
    store = V41Store(tmp_path / 'cutover.sqlite')
    prior = run('Pay USD 20 per hour.')
    old_id = prior.evaluated[0].canonical.canonical_id
    store.record_run(prior)
    store.mark_notified(prior.metadata.run_id, prior.evaluated, 'email', True)
    store.enable_delivery_production(
        workspace_id='fixture',
        profile_id='person',
        approved=True,
    )

    current = run('Pay USD 20 per hour.', minute=1)
    current.evaluated[0].canonical.canonical_id = 'first-current-' + old_id
    current.evaluated[0].job.id = current.evaluated[0].canonical.canonical_id
    duplicate = deepcopy(current.evaluated[0])
    duplicate.canonical.canonical_id = 'second-current-' + old_id
    duplicate.job.id = duplicate.canonical.canonical_id
    current.evaluated.append(duplicate)
    report = store.record_delivery_run(
        current,
        [EMAIL],
        now=100,
        adopt_legacy=True,
    )

    assert report['legacy_crosswalk'] == {'mapped': 0, 'ambiguous': 1}
    assert store.delivery.subject(old_id) == old_id
    # Still no automatic send for an ambiguous identity (review finding 5):
    # both current IDs are adopted as already-delivered baselines instead of
    # being held forever, so a genuine later change is delivered normally.
    assert report['legacy_baselines_adopted'] == 2
    assert {row['status'] for row in store.delivery.inspect()} == {'accepted'}
    assert store.delivery.claim_next(EMAIL, now=101) is None
    changed = run('Pay USD 30 per hour.', minute=2)
    changed.evaluated[0].canonical.canonical_id = 'first-current-' + old_id
    changed.evaluated[0].job.id = changed.evaluated[0].canonical.canonical_id
    store.record_delivery_run(changed, [EMAIL], now=200, adopt_legacy=True)
    assert store.delivery.claim_next(EMAIL, now=201) is not None
    store.close()
