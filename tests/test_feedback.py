"""Feedback-loop fixtures (HANDOFF v2 §8.3/§8.5) — asymmetric updates, evidence
audit trail, and the sqlite event log. Registry writes run against a tmp copy so
the real seeds are never touched by tests.
"""
import shutil
from datetime import date
from pathlib import Path

import pytest

from jobhound.config import CONFIG
from jobhound.store import Store
from jobhound.trust.registry import (
    clear_caches,
    composite_trust,
    load_registry,
    load_registry_raw,
    registry_path,
)
from jobhound.trust.update import apply_feedback, event_deltas

WEIGHTS = CONFIG.trust.weights


@pytest.fixture
def tmp_registry(tmp_path):
    dst = tmp_path / "platform_registry.yaml"
    shutil.copy(registry_path(), dst)
    yield dst
    clear_caches()  # saves against the tmp file cleared the default cache


# ── event → delta resolution ────────────────────────────────────────────────

def test_negative_deltas_apply_in_full():
    assert event_deltas("deactivated_no_reason") == {"account_stability": -0.25}
    # repetition does NOT soften a drop
    assert event_deltas("deactivated_no_reason", prior_count=3) == {"account_stability": -0.25}


def test_positive_deltas_diminish_on_repetition():
    f = CONFIG.feedback.diminishing_factor
    assert event_deltas("paid_on_time")["payment_reliability"] == pytest.approx(0.05)
    assert event_deltas("paid_on_time", prior_count=1)["payment_reliability"] == pytest.approx(0.05 * f)
    assert event_deltas("paid_on_time", prior_count=3)["payment_reliability"] == pytest.approx(0.05 * f**3)


def test_unknown_event_raises():
    with pytest.raises(ValueError, match="unknown feedback event"):
        event_deltas("got_rich_quick")


def test_log_only_event_has_no_deltas():
    assert event_deltas("false_positive_scam") == {}


# ── apply_feedback against a tmp registry ───────────────────────────────────

def test_deactivation_drops_account_stability_and_appends_evidence(tmp_registry):
    before = load_registry_raw(tmp_registry)["platforms"]["outlier"]
    n_evidence = len(before["evidence"])
    old_stability = before["dims"]["account_stability"]
    expected_stability = round(max(0.0, old_stability - 0.25), 4)

    res = apply_feedback("outlier", "deactivated_no_reason",
                         note="no appeal path", registry_file=tmp_registry)

    assert res.dim_changes["account_stability"] == (
        old_stability, expected_stability
    )
    assert res.new_trust < res.old_trust
    assert res.delta == pytest.approx(-0.25 * WEIGHTS["account_stability"], abs=1e-4)

    after = load_registry_raw(tmp_registry)["platforms"]["outlier"]
    assert after["dims"]["account_stability"] == expected_stability
    assert len(after["evidence"]) == n_evidence + 1
    ev = after["evidence"][-1]
    assert ev["class"] == "operator"
    assert "deactivated_no_reason" in ev["note"] and "no appeal path" in ev["note"]
    assert ev["date"] == date.today()
    assert after["last_reviewed"] == date.today()


def test_amount_lands_in_the_evidence_note(tmp_registry):
    apply_feedback("mercor", "paid_on_time", amount=120, registry_file=tmp_registry)
    ev = load_registry_raw(tmp_registry)["platforms"]["mercor"]["evidence"][-1]
    assert "$120" in ev["note"]


def test_dims_clamp_at_zero(tmp_registry):
    for _ in range(4):  # 4 × −0.25 on a 0.40 seed
        apply_feedback("outlier", "deactivated_no_reason", registry_file=tmp_registry)
    dims = load_registry_raw(tmp_registry)["platforms"]["outlier"]["dims"]
    assert dims["account_stability"] == 0.0


def test_unknown_platform_raises_without_writing(tmp_registry):
    before = load_registry_raw(tmp_registry)
    with pytest.raises(ValueError, match="unknown platform"):
        apply_feedback("clickfarm9000", "paid_on_time", registry_file=tmp_registry)
    assert load_registry_raw(tmp_registry) == before


def test_updated_registry_still_loads_as_models(tmp_registry):
    before = load_registry_raw(tmp_registry)["platforms"]["outlier"]
    old_consistency = before["dims"]["work_consistency"]
    delta = event_deltas("eq_extended")["work_consistency"]
    apply_feedback("outlier", "eq_extended", registry_file=tmp_registry)
    reg = load_registry(tmp_registry)
    pt = reg["outlier"]
    assert pt.work_consistency == pytest.approx(
        round(max(0.0, old_consistency + delta), 4)
    )
    assert pt.trust == pytest.approx(
        composite_trust({d: getattr(pt, d) for d in (
            "payment_reliability", "work_consistency", "account_stability",
            "support_quality", "onboarding_cost")}, WEIGHTS))
    assert pt.evidence[-1].evidence_class == "operator"


def test_feedback_measurably_moves_rank(tmp_registry):
    # §13 P2 acceptance: an operator event measurably moves a platform's rank.
    from jobhound.enrich.trust import trust_multiplier
    old = load_registry(tmp_registry)["alignerr"].trust
    apply_feedback("alignerr", "unpaid_dispute", registry_file=tmp_registry)
    new = load_registry(tmp_registry)["alignerr"].trust
    assert trust_multiplier(new) < trust_multiplier(old)


# ── sqlite event log ────────────────────────────────────────────────────────

def test_store_feedback_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.count_feedback("outlier", "paid_on_time") == 0
    store.add_feedback("paid_on_time", platform_key="outlier", amount=50.0, note="w1")
    store.add_feedback("paid_on_time", platform_key="outlier")
    store.add_feedback("eq_extended", platform_key="outlier")
    store.add_feedback("paid_on_time", platform_key="mercor")

    assert store.count_feedback("outlier", "paid_on_time") == 2
    assert store.count_feedback("outlier", "eq_extended") == 1
    log = store.feedback_log("outlier")
    assert len(log) == 3
    all_log = store.feedback_log()
    assert len(all_log) == 4
    store.close()
