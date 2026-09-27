"""V4.2 rollback must see what V5.5 durable delivery already sent.

Durable delivery records acceptance only in delivery_* tables, so a rollback
to the V4.2 engine (which reads notification_events) would resend every job
V5.5 had delivered. Accepted intents now also write a notification_events
row tagged 'v6_durable'. Those rows must never be re-imported as legacy
receipts: import_legacy runs on every production run, and a legacy hold
would swallow the job's next genuine material update.
"""
from test_v6_delivery_recovery import EMAIL, count, finish, release, stage, start, store  # noqa: F401
from test_v6_continuity import run


def delivered_subject(store):
    report = stage(store)
    intent = report['destinations'][0]['intent_ids'][0]
    subject = store.delivery._owned(intent)['subject']
    finish(store, start(store))
    return subject


def test_accepted_delivery_is_visible_to_legacy_v42_notifications(store):
    subject = delivered_subject(store)
    assert store._was_notified(subject)
    row = store.conn.execute(
        'SELECT transition, channel, success FROM notification_events WHERE canonical_id=?',
        (subject,)).fetchone()
    assert tuple(row) == ('v6_durable', 'email', 1)


def coverage_rows(store):
    return store.conn.execute(
        "SELECT count(*) FROM notification_events WHERE canonical_id LIKE 'coverage:%'").fetchone()[0]


def test_only_job_subjects_are_mirrored_into_v42_history(store, tmp_path):
    # Coverage/status items share the outbox but are not jobs V4.2 could resend.
    result = run('Pay USD 20 per hour.')
    result.source_health = [{'source': 'jsearch', 'status': 'failed', 'error_type': 'Timeout'}]
    report = stage(store, result)
    intents = report['destinations'][0]['intent_ids']
    assert any(store.delivery._owned(i)['subject'].startswith('coverage:') for i in intents)
    while (claim := store.delivery.claim_next(EMAIL, now=101)) is not None:
        store.delivery.begin_send(claim['id'], claim['token'], now=101)
        finish(store, claim)
    assert coverage_rows(store) == 0
    store.conn.execute("DELETE FROM notification_events")
    store.conn.commit()
    store.enable_delivery_review(review_root=tmp_path, workspace_id='fixture', profile_id='person')
    assert coverage_rows(store) == 0
    assert store.conn.execute("SELECT count(*) FROM notification_events").fetchone()[0] >= 1


def test_deliveries_accepted_before_mirroring_are_backfilled_once(store, tmp_path):
    subject = delivered_subject(store)
    # Simulate the 2026-09-27 cutover run, accepted before receipts were mirrored.
    store.conn.execute("DELETE FROM notification_events WHERE transition='v6_durable'")
    store.conn.commit()
    assert not store._was_notified(subject)
    for _ in range(2):
        store.enable_delivery_review(review_root=tmp_path, workspace_id='fixture', profile_id='person')
    assert store._was_notified(subject)
    assert store.conn.execute(
        "SELECT count(*) FROM notification_events WHERE transition='v6_durable'").fetchone()[0] == 1


def test_v6_receipts_are_not_reimported_as_legacy_holds(store, tmp_path):
    subject = delivered_subject(store)
    # Every production run re-enables delivery, which re-imports legacy receipts.
    store.enable_delivery_review(review_root=tmp_path, workspace_id='fixture', profile_id='person')
    assert count(store, 'delivery_legacy') == 0
    changed = store.record_delivery_run(run('Pay USD 30 per hour.', minute=5), (EMAIL,), now=200)
    statuses = [store.delivery._owned(i)['status'] for i in changed['destinations'][0]['intent_ids']]
    assert statuses == ['pending']
    assert store.delivery._owned(changed['destinations'][0]['intent_ids'][0])['subject'] == subject


def test_rollback_receipt_covers_the_current_id_after_an_alias(store):  # review finding 9
    first = run('Pay USD 20 per hour.')
    stage(store, first)
    finish(store, start(store))
    old = first.evaluated[0].canonical.canonical_id
    store.delivery.register_alias(old, 'new-canonical', evidence='reviewed-exact-identity')
    updated = run('Pay USD 30 per hour.', minute=1)
    updated.evaluated[0].canonical.canonical_id = 'new-canonical'
    store.record_delivery_run(updated, (EMAIL,), now=200)
    finish(store, start(store, now=300), now=301)
    assert store._was_notified('new-canonical')      # what a V4.2 rollback looks up


def test_one_rollback_receipt_per_job_run_and_channel(store):  # review finding 9
    from jobhound.delivery_outbox import Destination
    second = Destination.from_address('email', 'test-sender:bob@example.test')
    stage(store, targets=(EMAIL, second))
    for destination in (EMAIL, second):
        finish(store, start(store, destination))
    assert store.conn.execute(
        "SELECT count(*) FROM notification_events WHERE transition='v6_durable'").fetchone()[0] == 1
