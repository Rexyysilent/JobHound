"""Relevance / fit scoring — v3 scoring core (Patch #1).

Replaces the v2 scorer. Changes:
  1. All keyword matching goes through matching.phrase_in_text (whole-word,
     ordered-window, per-token typo tolerance). No more partial_ratio — a
     2-word phrase can no longer be awarded for matching ONE of its tokens
     ("prompt engineer" never fires on "QA Engineer").
  2. ANCHOR RULE: category boosts (boost_high) alone cannot carry a job.
     Zero profile-anchor matches -> fit capped at profile.no_anchor_fit_cap
     (default 30, below the surface cutoff) with reason "no_profile_anchor".
     Category terms describe the LANE; anchors prove it's YOUR lane.

The language-edge boost keeps v2 semantics: driven by the "bengali" region tag
(title-mention, or body-mention + a language-intent cue — normalize.py), so
company etymology never triggers the lane. It counts as a lang hit for the
anchor rule.
"""
from __future__ import annotations

from ..config import CONFIG, RelevanceCfg
from ..models import Job
from .accessibility import apply as apply_accessibility
from .eligibility import Profile, default_profile
from .matching import matched_keywords

_WEIGHTS = {"boost_high": 25.0, "boost_mid": 12.0, "boost_lang": 20.0, "exclude_soft": -15.0}
_BASE_FIT = 10.0


def score(job, keywords_cfg: dict, profile: Profile, *,
          lang_edge: bool = False) -> tuple[float, list[str]]:
    """Returns (fit_score 0..100, reasons). `job` needs title/description/
    is_remote/region_tags — duck-typed so test stubs work."""
    text = f"{job.title}\n{job.description or ''}"
    reasons: list[str] = []
    fit = _BASE_FIT

    # hard excludes first
    hard = matched_keywords(keywords_cfg.get("exclude_hard", []), text)
    if hard:
        return 0.0, [f"exclude_hard: {hard[0]}"]

    hi = matched_keywords(keywords_cfg.get("boost_high", []), text)
    mid = matched_keywords(keywords_cfg.get("boost_mid", []), text)
    lang = matched_keywords(keywords_cfg.get("boost_lang", []), text)
    soft = matched_keywords(keywords_cfg.get("exclude_soft", []), text)

    for kw in hi:
        fit += _WEIGHTS["boost_high"]; reasons.append(f"boost_high: {kw} (+25)")
    for kw in mid:
        fit += _WEIGHTS["boost_mid"]; reasons.append(f"boost_mid: {kw} (+12)")
    for kw in lang:
        fit += _WEIGHTS["boost_lang"]; reasons.append(f"boost_lang: {kw} (+20)")
    if lang_edge and not lang:
        fit += _WEIGHTS["boost_lang"]
        reasons.append("boost_lang: language-edge (+20)")
    for kw in soft:
        fit += _WEIGHTS["exclude_soft"]; reasons.append(f"exclude_soft: {kw} (-15)")

    fit = max(0.0, min(100.0, fit))

    # --- ANCHOR RULE --------------------------------------------------------
    has_lang = bool(lang) or lang_edge
    anchors_hit = matched_keywords(profile.anchors, text) if profile.anchors else []
    if profile.anchors and hi and not anchors_hit and not has_lang and not mid:
        cap = float(profile.no_anchor_fit_cap)
        if fit > cap:
            fit = cap
            reasons.append(f"no_profile_anchor (capped at {int(cap)})")
    elif anchors_hit:
        reasons.append(f"anchors: {', '.join(anchors_hit[:4])}")

    return fit, reasons


def score_relevance(job: Job, cfg: RelevanceCfg = CONFIG.relevance,
                    profile: Profile | None = None) -> bool:
    """Set job.fit_score + job.fit_reasons. Return True if exclude_hard matched."""
    profile = profile or default_profile()
    kws = cfg.keywords
    kw_cfg = {
        "boost_high": kws.boost_high,
        "boost_mid": kws.boost_mid,
        # lang boost comes from the region tag (v2 §9 semantics), not raw keywords
        "boost_lang": [],
        "exclude_hard": kws.exclude_hard,
        "exclude_soft": kws.exclude_soft,
    }
    fit, reasons = score(
        job, kw_cfg, profile, lang_edge="bengali" in job.region_tags
    )
    hard_excluded = bool(reasons) and reasons[0].startswith("exclude_hard")
    job.fit_score = fit
    job.fit_reasons = reasons
    if not hard_excluded:
        apply_accessibility(job)
    job.fit_score = round(job.fit_score, 1)
    return hard_excluded
