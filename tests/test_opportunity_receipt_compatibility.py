"""A legacy receipt belongs to its immutable posting, never a sibling role."""
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from jobhound.v41.engine import evaluate_raw
from jobhound.v41.models import ActionBand
from jobhound.v41.provenance import _canonical_id, _legacy_canonical_id, public_url
from jobhound.v41.store import V41Store
from test_v41 import _greenhouse, _requirements


NOW = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
URL = "https://boards.greenhouse.io/receipt-example/jobs/RoleA"


@pytest.fixture
def store(tmp_path):
    value = V41Store(tmp_path / "receipt-compatibility.db")
    try:
        yield value
    finally:
        value.close()


def _result(*, url=URL, minute=0, legacy=False, band=ActionBand.PRIMARY,
            company="Receipt Example", title="Bengali AI Evaluator",
            location="India", pay=None):
    description = _requirements("Bengali annotation. " + (pay or ""))
    record = _greenhouse(title, company, description, location=location)
    record["raw"]["absolute_url"] = url
    result = evaluate_raw([record], as_of=NOW + timedelta(minutes=minute))
    item = result.evaluated[0]
    old_id = _legacy_canonical_id(item.job)
    current_base = _canonical_id(item.job, item.canonical.counterparty_key)
    new_id = hashlib.sha256(
        (current_base + "|" + public_url(item.job.url).rstrip("/")).encode()
    ).hexdigest()[:20]
    # Construct both persisted schemas explicitly, independent of the current
    # canonicalizer. This fixture remains a real old database after the upgrade.
    item.canonical.legacy_canonical_id = old_id if not legacy else None
    item.canonical.canonical_id = old_id if legacy else new_id
    item.job.id = item.canonical.canonical_id
    item.decision.action_band = band
    return result


def _record(store, result, success=None):
    store.record_run(result)
    if success is not None:
        store.mark_notified(result.metadata.run_id, result.evaluated, "email", success)


def test_successful_legacy_receipt_preserves_same_posting_without_rewriting_history(store):
    old = _result(legacy=True)
    _record(store, old, True)
    before = list(store.conn.iterdump())
    current = _result(minute=1)

    assert store.annotate_transitions(current) == []
    assert current.evaluated[0].decision.notification_transition == "none"
    assert list(store.conn.iterdump()) == before


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/receipt-example/jobs/RoleB",
    "https://boards.greenhouse.io/receipt-example/jobs/rolea",
    URL + "?requisition=two",
])
def test_legacy_receipt_cannot_silence_distinct_posting(store, url):
    old = _result(legacy=True)
    _record(store, old, True)
    current = _result(url=url, minute=1)

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "new"


@pytest.mark.parametrize("changes", [
    {"company": "Other Employer"},
    {"title": "Hindi AI Evaluator"},
    {"location": "United States"},
])
def test_url_match_alone_cannot_cross_a_different_legacy_identity(store, changes):
    _record(store, _result(legacy=True), True)
    current = _result(minute=1, **changes)

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "new"


@pytest.mark.parametrize("success", [None, False])
def test_observation_or_failed_send_is_not_a_legacy_delivery(store, success):
    _record(store, _result(legacy=True), success)
    current = _result(minute=1)

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "new"


def test_receipt_uses_immutable_run_url_despite_later_mutable_identity_overwrite(store):
    _record(store, _result(legacy=True), True)
    sibling_url = "https://boards.greenhouse.io/receipt-example/jobs/RoleB"
    # The old identity lacked URL: another scrape overwrites canonical_jobs,
    # while the successful run_jobs row still identifies the delivered posting.
    sibling = _result(url=sibling_url, minute=1, legacy=True)
    _record(store, sibling, False)

    delivered = _result(minute=2)
    undispatched = _result(url=sibling_url, minute=3)
    assert store.annotate_transitions(delivered) == []
    assert store.annotate_transitions(undispatched) == undispatched.evaluated
    assert undispatched.evaluated[0].decision.notification_transition == "new"


def test_failed_new_identity_run_does_not_forget_successful_legacy_receipt(store):
    _record(store, _result(legacy=True), True)
    _record(store, _result(minute=1), False)
    current = _result(minute=2)

    assert store.annotate_transitions(current) == []


def test_failed_promotion_is_retried_against_last_successful_legacy_receipt(store):
    _record(store, _result(legacy=True, band=ActionBand.VERIFY), True)
    first = _result(minute=1)
    assert store.annotate_transitions(first) == first.evaluated
    assert first.evaluated[0].decision.notification_transition == "promoted"
    _record(store, first, False)
    retry = _result(minute=2)
    assert store.annotate_transitions(retry) == retry.evaluated
    assert retry.evaluated[0].decision.notification_transition == "promoted"
    _record(store, retry, True)
    assert store.annotate_transitions(_result(minute=3)) == []


def test_new_identity_success_takes_precedence_over_legacy_baseline(store):
    _record(store, _result(legacy=True, band=ActionBand.VERIFY), True)
    _record(store, _result(minute=1), True)

    assert store.annotate_transitions(_result(minute=2)) == []


def test_tracking_only_url_changes_do_not_resurface_delivered_legacy_job(store):
    _record(store, _result(url=URL + "?utm_source=old", legacy=True), True)
    current = _result(url=URL + "?utm_source=new", minute=1)

    assert store.annotate_transitions(current) == []


def test_missing_run_for_success_receipt_cannot_invent_a_delivered_posting(store):
    old = _result(legacy=True)
    # Simulates incomplete history without fabricating the absent run evidence.
    store.mark_notified(old.metadata.run_id, old.evaluated, "email", True)
    current = _result(minute=1)

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "new"


def test_same_url_legacy_delivery_does_not_clear_rejected_job_or_stale_transition(store):
    _record(store, _result(legacy=True), True)
    current = _result(minute=1, band=ActionBand.REJECT)
    current.evaluated[0].decision.notification_transition = "new"

    assert store.annotate_transitions(current) == []
    assert current.evaluated[0].decision.notification_transition == "none"


def test_last_successful_legacy_receipt_supplies_pay_baseline(store):
    _record(store, _result(legacy=True), True)
    paid = _result(minute=1, legacy=True, pay="Compensation: USD 15 per hour.")
    assert paid.evaluated[0].assessment.selected_pay is not None
    _record(store, paid, False)
    current = _result(minute=2, pay="Compensation: USD 15 per hour.")

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "pay_resolved"


def test_each_legacy_receipt_stays_bound_to_its_own_run_when_old_ids_collide(store):
    second_url = "https://boards.greenhouse.io/receipt-example/jobs/RoleB"
    _record(store, _result(legacy=True, band=ActionBand.VERIFY), True)
    _record(store, _result(legacy=True, url=second_url, minute=1), True)

    first = _result(minute=2)
    second = _result(url=second_url, minute=3)
    assert store.annotate_transitions(first) == first.evaluated
    assert first.evaluated[0].decision.notification_transition == "promoted"
    assert store.annotate_transitions(second) == []


@pytest.mark.parametrize("earlier,later", [
    ({"title": "AI Trainer C++"}, {"title": "AI Trainer C#"}),
    ({"title": "AI Trainer বাংলা"}, {"title": "AI Trainer হিন্দি"}),
    ({"company": "গবেষণা এক"}, {"company": "গবেষণা দুই"}),
])
def test_erased_names_in_old_hash_do_not_share_the_same_url_receipt(store, earlier, later):
    old = _result(legacy=True, **earlier)
    current = _result(minute=1, **later)
    assert old.evaluated[0].canonical.canonical_id == current.evaluated[0].canonical.legacy_canonical_id
    _record(store, old, True)

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "new"


@pytest.mark.parametrize("fields", [
    {"title": "AI Trainer C++"},
    {"title": "AI Trainer বাংলা"},
    {"company": "গবেষণা এক"},
])
def test_exact_immutable_unicode_identity_retains_its_own_legacy_receipt(store, fields):
    _record(store, _result(legacy=True, **fields), True)

    assert store.annotate_transitions(_result(minute=1, **fields)) == []


def test_ambiguous_old_same_url_cluster_cannot_prove_which_title_was_delivered(store):
    old = _result(legacy=True, title="AI Trainer C++")
    other = _result(legacy=True, title="AI Trainer C#", minute=1)
    assert old.evaluated[0].canonical.canonical_id == other.evaluated[0].canonical.canonical_id
    # Reproduce the old stored cluster, whose title hash erased + and #.
    old.observations.extend(other.observations)
    old.evaluated[0].canonical.observations.extend(other.observations)
    old.counts["raw_observations"] = 2
    old.counts["normalized_observations"] = 2
    old.counts["duplicate_observations_collapsed"] = 1
    _record(store, old, True)

    for minute, title in enumerate(("AI Trainer C++", "AI Trainer C#"), start=2):
        current = _result(title=title, minute=minute)
        assert store.annotate_transitions(current) == current.evaluated
        assert current.evaluated[0].decision.notification_transition == "new"


@pytest.mark.parametrize("corruption", ["missing", "invalid"])
def test_incomplete_historical_observation_cannot_suppress_a_posting(store, corruption):
    _record(store, _result(legacy=True), True)
    with store.conn:
        if corruption == "missing":
            store.conn.execute("DELETE FROM observations")
        else:
            store.conn.execute("UPDATE observations SET normalized_job_json='invalid'")
    current = _result(minute=1)

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "new"


def test_dangling_link_in_ambiguous_history_cannot_become_identity_evidence(store):
    old = _result(legacy=True, title="AI Trainer C++")
    other = _result(legacy=True, title="AI Trainer C#", minute=1)
    old.observations.extend(other.observations)
    old.evaluated[0].canonical.observations.extend(other.observations)
    old.counts["raw_observations"] = 2
    old.counts["normalized_observations"] = 2
    old.counts["duplicate_observations_collapsed"] = 1
    _record(store, old, True)
    with store.conn:
        store.conn.execute("DELETE FROM observations WHERE observation_id=?",
                           (other.observations[0].observation_id,))
    current = _result(title="AI Trainer C++", minute=2)

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "new"


@pytest.mark.parametrize("payload", ["invalid", "[]", "0", '"quote"', "{}"])
def test_malformed_historical_pay_cannot_crash_or_suppress_migration(store, payload):
    _record(store, _result(legacy=True), True)
    with store.conn:
        store.conn.execute("UPDATE run_jobs SET selected_pay_json=?", (payload,))
    current = _result(minute=1)

    assert store.annotate_transitions(current) == current.evaluated
    assert current.evaluated[0].decision.notification_transition == "new"
