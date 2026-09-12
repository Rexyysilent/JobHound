"""Trust & EV fixtures (HANDOFF v2 §8) — the product IS the trust in the filters.

Covers: composite math, registry loading, alias→platform matching, effective-rate
math, EV multipliers, freshness decay, the trust hard-floor verdict, and the P1
acceptance test: an Outlier-class listing ranks visibly below a Mercor-class one
at equal fit.
"""
from datetime import datetime, timedelta, timezone

import pytest

from jobhound.config import CONFIG, RankingCfg, TrustCfg
from jobhound.enrich.trust import (
    compute_ev,
    confidence_multiplier,
    effective_hourly,
    enrich_trust,
    freshness,
    trust_multiplier,
)
from jobhound.models import Job, VERDICT_REJECTED_TRUST, VERDICT_SURFACED
from jobhound.pipeline import _decide_verdict
from jobhound.trust.registry import (
    PlatformMatcher,
    composite_trust,
    default_matcher,
    default_registry,
)

WEIGHTS = CONFIG.trust.weights


def _job(company, title="AI Training Specialist", **kw):
    return Job(source="t", title=title, url="http://x",
               description="LLM evaluation and RLHF work.", company=company, **kw)


# ── composite math ──────────────────────────────────────────────────────────

def test_composite_trust_is_weighted_sum():
    dims = {
        "payment_reliability": 1.0,
        "work_consistency": 0.0,
        "account_stability": 0.0,
        "support_quality": 0.0,
        "onboarding_cost": 0.0,
    }
    assert composite_trust(dims, WEIGHTS) == pytest.approx(0.30)
    dims_all = {k: 0.5 for k in dims}
    assert composite_trust(dims_all, WEIGHTS) == pytest.approx(0.5)


# ── registry seeds ──────────────────────────────────────────────────────────

def test_registry_loads_all_seeds_with_valid_scores():
    reg = default_registry()
    assert len(reg) >= 13
    for pt in reg.values():
        assert 0.0 <= pt.trust <= 1.0
        assert pt.evidence, f"{pt.key} must carry a seed evidence entry"


def test_seed_priors_order_mercor_above_outlier_above_remotasks():
    reg = default_registry()
    assert reg["mercor"].trust > reg["outlier"].trust > 0
    assert reg["outlier"].trust > CONFIG.trust.hard_floor  # volatile secondary, not banned
    assert reg["remotasks"].trust < reg["outlier"].trust + 0.1


# ── platform matching ───────────────────────────────────────────────────────

@pytest.mark.parametrize("company, expected", [
    ("Outlier", "outlier"),
    ("Outlier AI", "outlier"),
    ("TELUS Digital", "telus_oneforma"),
    ("TELUS International AI", "telus_oneforma"),
    ("DataAnnotation.Tech", "dataannotation"),
    ("Appen", "appen_crowdgen"),
    ("Mercor", "mercor"),
    ("Acme Corp", None),
    ("Scale AI", None),          # corporate roles are NOT Outlier gig work
    ("Prolific North", "prolific"),  # word-boundary containment is intended
    (None, None),
])
def test_platform_matching(company, expected):
    assert default_matcher().match(company) == expected


def test_title_never_joins_a_platform():
    # "Outlier Detection Engineer" at some analytics shop is not gig work.
    job = enrich_trust(_job("Statwerk GmbH", title="Outlier Detection Engineer"))
    assert job.platform_key is None
    assert job.platform_trust is None


# ── effective-rate math ─────────────────────────────────────────────────────

def test_effective_hourly_discounts_availability_and_overhead():
    reg = default_registry()
    # Outlier seed: availability 0.45, overhead 0.30 → $30 nominal ≈ $9.45 real.
    assert effective_hourly(30.0, reg["outlier"]) == pytest.approx(9.45)
    # Mercor seed: 0.70 avail, 0.10 overhead → $30 ≈ $18.90.
    assert effective_hourly(30.0, reg["mercor"]) == pytest.approx(18.9)


def test_enrich_trust_sets_effective_from_nominal_midpoint():
    job = _job("Outlier", pay_min_hourly_usd=15.0, pay_max_hourly_usd=50.0)
    enrich_trust(job)
    assert job.platform_key == "outlier"
    # midpoint 32.5 × 0.45 × 0.7
    assert job.effective_hourly_usd == pytest.approx(10.24, abs=0.01)
    assert "eff ~$10.24/hr" in job.pay_display_full  # v3: cents shown (Patch #2 digest format)


def test_unknown_platform_or_pay_leaves_effective_none():
    assert enrich_trust(_job("Acme Corp", pay_max_hourly_usd=40.0)).effective_hourly_usd is None
    assert enrich_trust(_job("Outlier")).effective_hourly_usd is None


# ── EV multipliers (§8.2) ───────────────────────────────────────────────────

def test_trust_multiplier_is_damped_never_zero():
    cfg = TrustCfg()
    assert trust_multiplier(1.0, cfg) == pytest.approx(1.0)
    assert trust_multiplier(0.0, cfg) == pytest.approx(0.4)
    assert trust_multiplier(None, cfg) == pytest.approx(cfg.unrated_multiplier)


def test_confidence_multiplier_band():
    assert confidence_multiplier(1.0) == pytest.approx(1.0)
    assert confidence_multiplier(0.0) == pytest.approx(0.7)


def test_freshness_decay():
    cfg = RankingCfg(freshness_half_life_days=14, unknown_age_freshness=0.8)
    now = datetime.now(timezone.utc)
    assert freshness(now, cfg, now=now) == pytest.approx(1.0)
    aged = freshness(now - timedelta(days=14), cfg, now=now)
    assert aged == pytest.approx(0.5, abs=0.001)
    assert freshness(None, cfg) == pytest.approx(0.8)
    # naive datetimes are treated as UTC, not crashed on
    naive = datetime.now().replace(tzinfo=None)
    assert 0.0 < freshness(naive, cfg) <= 1.0


# ── the P1 acceptance test ──────────────────────────────────────────────────

def test_outlier_class_ranks_below_mercor_class_at_equal_fit():
    now = datetime.now(timezone.utc)
    a = _job("Outlier", posted_at=now)
    b = _job("Mercor", posted_at=now)
    for j in (a, b):
        enrich_trust(j)
        j.fit_score = 70.0
        compute_ev(j)
    assert a.ev_score < b.ev_score
    # damped multiplier: down-ranked, not erased
    assert a.ev_score > 0.4 * b.ev_score


def test_unrated_platform_gets_neutral_multiplier():
    job = enrich_trust(_job("Acme Corp"))
    job.fit_score = 70.0
    job.posted_at = datetime.now(timezone.utc)
    compute_ev(job)
    assert job.ev_score == pytest.approx(70 * 0.85 * (0.7 + 0.3 * 0.5), abs=0.1)


# ── trust hard floor ────────────────────────────────────────────────────────

def test_trust_below_hard_floor_is_rejected_but_logged():
    job = _job("Sketchy Platform Inc")
    job.platform_trust = 0.2   # below default hard_floor 0.25
    job.fit_score = 90.0
    job.pay_ok = True
    assert _decide_verdict(job, hard_excluded=False, ineligible=False) == VERDICT_REJECTED_TRUST


def test_outlier_seed_survives_the_floor():
    job = enrich_trust(_job("Outlier"))
    job.fit_score = 70.0
    job.pay_ok = True
    assert _decide_verdict(job, hard_excluded=False, ineligible=False) == VERDICT_SURFACED


# ── registry matcher isolation ──────────────────────────────────────────────

def test_matcher_prefers_longest_alias():
    reg = default_registry()
    m = PlatformMatcher(reg)
    assert m.match("TELUS International AI Inc.") == "telus_oneforma"
