"""Per-destination send planning for approved production runs.

Regression: the first retry-drain draft shared one global envelope cap across
channels and drained old retries first, so a Telegram outage (ISP-blocked on
this network at times) accumulated retries until they consumed the whole cap
and the healthy email digest stopped being sent.
"""
from jobhound.delivery_transport import plan_dispatch
from test_v6_delivery_recovery import release, store  # noqa: F401


def test_plan_keeps_a_slot_for_todays_envelope():
    assert plan_dispatch(['a', 'b', 'c', 'd', 'e'], 'today', limit=4) == ['a', 'b', 'c', 'today']


def test_plan_without_todays_envelope_uses_the_whole_limit():
    assert plan_dispatch(['a', 'b', 'c', 'd', 'e'], None, limit=4) == ['a', 'b', 'c', 'd']


def test_plan_sends_todays_envelope_once_even_if_already_ready():
    assert plan_dispatch(['a', 'today', 'b'], 'today', limit=4) == ['a', 'b', 'today']


def test_plan_with_limit_one_sends_only_today():
    assert plan_dispatch(['a', 'b'], 'today', limit=1) == ['today']


def test_retry_backlog_still_leaves_todays_digest_a_slot(store):
    # Real delivery path (deliver_destination, one call per destination):
    # an unsent retry is attempted first, and today's new card still goes out.
    import asyncio
    from jobhound.delivery_transport import deliver_destination
    from jobhound.notify.receipt import DeliveryReceipt
    from test_v6_continuity import run
    from test_v6_delivery_recovery import EMAIL as REAL_EMAIL
    from test_v6_transport import Sender, prepared, send
    transport, old = prepared(store)
    assert send(transport, old, Sender(receipts=[DeliveryReceipt('not_sent', 'busy', 60)]), now=102) == 'pending'
    other = run('Pay USD 20 per hour.', minute=5, identity='second-job')
    other.evaluated[0].job.company = 'Second employer'
    store.record_delivery_run(other, (REAL_EMAIL,), now=500)
    sender = Sender()
    outcomes = asyncio.run(deliver_destination(transport, REAL_EMAIL, sender,
                                               run_id=None, now=lambda: 1000, limit=2))
    assert [outcome for _, outcome in outcomes] == ['accepted', 'accepted']
    assert outcomes[0][0] == old and len(sender.calls) == 2
