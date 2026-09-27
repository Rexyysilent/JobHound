"""Per-destination send planning for approved production runs.

Regression: the first retry-drain draft shared one global envelope cap across
channels and drained old retries first, so a Telegram outage (ISP-blocked on
this network at times) accumulated retries until they consumed the whole cap
and the healthy email digest stopped being sent.
"""
from types import SimpleNamespace

from jobhound import cli
from jobhound.delivery_transport import plan_dispatch

EMAIL = SimpleNamespace(channel='email', key='e')
TELEGRAM = SimpleNamespace(channel='telegram', key='t')


def test_plan_keeps_a_slot_for_todays_envelope():
    assert plan_dispatch(['a', 'b', 'c', 'd', 'e'], 'today', limit=4) == ['a', 'b', 'c', 'today']


def test_plan_without_todays_envelope_uses_the_whole_limit():
    assert plan_dispatch(['a', 'b', 'c', 'd', 'e'], None, limit=4) == ['a', 'b', 'c', 'd']


def test_plan_sends_todays_envelope_once_even_if_already_ready():
    assert plan_dispatch(['a', 'today', 'b'], 'today', limit=4) == ['a', 'b', 'today']


def test_plan_with_limit_one_sends_only_today():
    assert plan_dispatch(['a', 'b'], 'today', limit=1) == ['today']


class FakeTransport:
    def __init__(self, ready):
        self.ready = ready

    def ready_envelopes(self, destination, *, now, limit):
        return self.ready[destination.channel][:limit]


def test_failing_telegram_backlog_cannot_starve_email():
    transport = FakeTransport({
        'email': [],
        'telegram': ['t1', 't2', 't3', 't4', 't5'],       # five days of blocked Telegram
    })
    plans = cli._dispatch_plans(
        transport, [EMAIL, TELEGRAM],
        {('email', 'e'): 'email-today', ('telegram', 't'): 'telegram-today'},
        now=1000, limit=4,
    )
    assert plans[('email', 'e')] == ['email-today']
    assert plans[('telegram', 't')] == ['t1', 't2', 't3', 'telegram-today']


def test_destination_without_new_cards_still_drains_its_retries():
    transport = FakeTransport({'email': ['e-old'], 'telegram': []})
    plans = cli._dispatch_plans(transport, [EMAIL, TELEGRAM], {}, now=1000, limit=4)
    assert plans == {('email', 'e'): ['e-old'], ('telegram', 't'): []}
