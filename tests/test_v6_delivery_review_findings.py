"""Delivery code review (2026-09-28): findings that could stall every later run.

1. An envelope that is not accepted (unsent retry, uncertain, failed or
   cancelled) reserved its job, and prepare() raised for any later revision of
   it; the run command passed every newly staged intent straight in, so the
   run crashed, and so did every run after it.
2. A lease expired only when that same envelope was touched again; after a
   crash between claim and receipt the envelope stayed leased forever.
3. A legacy crosswalk for an ID already staged in an earlier run raised and
   rolled back that run and every later one.
8. Only newly selected intents were prepared, so intents left over (cap
   overflow, a skipped reserved job) dropped out of later runs.
"""
import asyncio

from jobhound.delivery_transport import DigestTransport, deliver_destination
from jobhound.notify.receipt import DeliveryReceipt
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.store import V41Store
from test_v6_continuity import NOW, raw, run
from test_v6_delivery_recovery import EMAIL, release, store  # noqa: F401
from test_v6_production_cutover import EMAIL as PROD_EMAIL
from test_v6_transport import Sender, prepared, send


def deliver(transport, sender, now, **kw):
    return asyncio.run(deliver_destination(
        transport, EMAIL, sender, run_id=None, now=lambda: now, **kw))


def test_job_change_after_an_unsent_retry_delivers_the_new_revision(store):
    transport, old = prepared(store)
    assert send(transport, old, Sender(receipts=[DeliveryReceipt('not_sent', 'busy', 60)]), now=102) == 'pending'
    store.record_delivery_run(run('Pay USD 30 per hour.', minute=5), (EMAIL,), now=500)
    sender = Sender()
    deliver(transport, sender, 1000)
    assert transport.inspect(old)['status'] == 'cancelled'        # stale, never sent
    assert len(sender.calls) == 1 and '30' in sender.calls[0][0]


def test_an_uncertain_envelope_holds_its_job_without_crashing_later_runs(store):
    transport, old = prepared(store)
    assert send(transport, old, Sender(receipts=[DeliveryReceipt('uncertain', 'lost')])) == 'uncertain'
    store.record_delivery_run(run('Pay USD 30 per hour.', minute=5), (EMAIL,), now=500)
    sender = Sender()
    for now in (1000, 90_000):                                     # every later run
        deliver(transport, sender, now)
    assert sender.calls == []                                      # held for reconciliation


def test_a_lease_left_by_a_crash_before_sending_expires_and_is_retried(store):
    transport, env = prepared(store)
    assert transport.claim(env, EMAIL, now=102) is not None        # process dies here
    sender = Sender()
    deliver(transport, sender, 102 + 3600)
    assert transport.inspect(env)['status'] == 'accepted' and len(sender.calls) == 1


def test_a_lease_left_mid_send_becomes_uncertain_not_resent(store):
    transport, env = prepared(store)
    token = transport.claim(env, EMAIL, now=102)
    assert transport.begin_part(env, token, now=102) is not None   # dies mid-send
    sender = Sender()
    deliver(transport, sender, 102 + 3600)
    assert transport.inspect(env)['status'] == 'uncertain' and sender.calls == []


def test_leftover_intents_are_delivered_on_a_later_run(store):
    rows = [raw('Pay USD 20 per hour.', identity=f'job-{i}') for i in range(3)]
    for i, row in enumerate(rows):
        row['raw']['_company'] = f'Employer {i}'      # distinct employers: no company cap
    result = evaluate_raw(rows, as_of=NOW)
    store.record_delivery_run(result, (EMAIL,), now=100)
    transport = DigestTransport(store.delivery)
    sender = Sender()
    deliver(transport, sender, 200, max_intents=2)
    deliver(transport, sender, 300, max_intents=2)
    delivered = {row['id'] for row in store.delivery.inspect() if row['status'] == 'accepted'}
    assert len(sender.calls) == 2 and len(delivered) == 3


def test_legacy_crosswalk_for_an_already_staged_id_does_not_crash(tmp_path):
    db = V41Store(tmp_path / 'cutover.sqlite')
    prior = run('Pay USD 20 per hour.')
    old_id = prior.evaluated[0].canonical.canonical_id
    db.record_run(prior)
    db.mark_notified(prior.metadata.run_id, prior.evaluated, 'email', True)
    db.enable_delivery_production(workspace_id='fixture', profile_id='person', approved=True)

    def current(minute):
        value = run('Pay USD 20 per hour.', minute=minute)
        value.evaluated[0].canonical.canonical_id = 'v6-' + old_id
        value.evaluated[0].job.id = 'v6-' + old_id
        return value

    db.record_delivery_run(current(1), [PROD_EMAIL], now=100, adopt_legacy=False)
    report = db.record_delivery_run(current(2), [PROD_EMAIL], now=200, adopt_legacy=True)
    assert report['legacy_crosswalk']['mapped'] == 0
    db.close()


def test_expired_lease_keeps_superseded_intents_superseded(store):  # finding 7
    transport, env = prepared(store)
    old_intent = transport.inspect(env)['intents'][0]
    assert transport.claim(env, EMAIL, now=102) is not None
    store.record_delivery_run(run('Pay USD 30 per hour.', minute=5), (EMAIL,), now=150)
    assert store.delivery._owned(old_intent)['status'] == 'superseded'
    transport.expire_leases(EMAIL, now=102 + 3600)
    assert store.delivery._owned(old_intent)['status'] == 'superseded'


def test_legacy_adoption_releases_every_destination_on_the_channel(tmp_path):  # finding 6
    from jobhound.delivery_outbox import Destination
    second = Destination.from_address('email', 'test-sender:second@example.test')
    db = V41Store(tmp_path / 'cutover.sqlite')
    prior = run('Pay USD 20 per hour.')
    db.record_run(prior)
    db.mark_notified(prior.metadata.run_id, prior.evaluated, 'email', True)
    db.enable_delivery_production(workspace_id='fixture', profile_id='person', approved=True)
    report = db.record_delivery_run(run('Pay USD 20 per hour.', minute=1), [PROD_EMAIL, second],
                                    now=100, adopt_legacy=True)
    assert report['legacy_baselines_adopted'] == 2
    assert {row['status'] for row in db.delivery.inspect()} == {'accepted'}
    db.close()
