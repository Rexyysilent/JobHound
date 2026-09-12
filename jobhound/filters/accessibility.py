"""Detect and promote easy-entry AI review/evaluation work."""
from __future__ import annotations

import re

from ..config import CONFIG, ScoringCfg
from ..text import fold_dashes


# STRONG evidence: the posting states the barrier to entry is low. Sufficient
# on its own to call a job easy-entry.
_ACCESS_STRONG = re.compile(
    r"("
    r"no (?:prior |previous )?experience(?: (?:is )?(?:required|needed|necessary))?"
    r"|no degree(?: required)?"
    r"|entry[- ]level|freshers?(?: welcome)?|beginners? welcome"
    r"|training (?:will be |is )?provided|paid training|we (?:will )?train"
    r"|all backgrounds|anyone can apply|open to all"
    r"|no technical (?:skills|background|knowledge)"
    r")",
    re.IGNORECASE,
)

# WEAK evidence (v3.5 item 6): true of most gig postings including specialist
# ones, so it can never establish easy-entry by itself. "Fluent English" and
# "flexible hours" appear in the NgSpice electronics listing too.
_ACCESS_WEAK = re.compile(
    r"("
    r"fluent (?:in )?english|english (?:fluency|proficiency) (?:required|is)"
    r"|flexible (?:hours|schedule)"
    r")",
    re.IGNORECASE,
)

_ACCESS_NEG = re.compile(
    r"("
    r"\b\d\s*\+?\s*years?\W{0,3}(?:of\s+)?experience"
    # "(?<!no )" so the ACCESSIBILITY claim "no degree required" is not read as a
    # degree DEMAND — it matched here before v3.5 and silently vetoed itself.
    r"|(?<!no )(?:bachelor|master|phd|ph\.d|doctorate|degree)s?\W{0,3}"
    r"(?:in|required|preferred)"
    r"|expert(?:ise)?\s+in|specialist\s+in|certification\s+required"
    r"|(?:proven|extensive|deep)\s+experience"
    r")",
    re.IGNORECASE,
)

EASY_ROLE_WORDS = re.compile(
    r"\b(rater|evaluator|reviewer|review|annotator|annotation|transcription|"
    r"transcriber|moderat(?:or|ion)|data collection|assessor|labell?er)\b",
    re.IGNORECASE,
)


def easy_entry(title: str, description: str) -> tuple[bool, list[str]]:
    """Return true only on STRONG access evidence without specialist demands.

    v3.5: a generic reviewer/rater title, "fluent English" or "flexible hours"
    are no longer enough. The posting has to actually say the door is open.
    """
    text = f"{title}\n{description or ''}"
    strong = _ACCESS_STRONG.findall(text)
    if not strong:
        if _ACCESS_WEAK.search(text) or EASY_ROLE_WORDS.search(title):
            return False, ["easy_entry_weak_only: no explicit low-barrier claim"]
        return False, []
    if _ACCESS_NEG.search(text):
        return False, ["easy_entry_blocked:specialist_demands"]
    # A capped leadership title is inaccessible even if the body says "no
    # experience required" — and leadership_track carries a cap, not a penalty,
    # so the penalty sum alone no longer catches it.
    if seniority_penalty(title) or title_fit_cap(title)[0] is not None:
        return False, ["easy_entry_blocked:senior_title"]
    return True, [f"easy_entry: {strong[0][:40]}"]


def seniority_penalty(title: str,
                      cfg: ScoringCfg = CONFIG.scoring) -> int:
    """Return the configured cumulative penalty for senior-track titles."""
    return sum(
        rule.delta
        for rule in cfg.title_penalties.values()
        if re.search(rule.pattern, title, flags=re.IGNORECASE)
    )


def title_fit_cap(title: str,
                  cfg: ScoringCfg = CONFIG.scoring) -> tuple[float | None, str | None]:
    """Strictest configured fit ceiling for this title → (cap, rule name).

    v3.5 item 5: leadership titles are structurally inaccessible, so they get a
    ceiling below relevance.min_fit_to_surface instead of a penalty that a
    keyword-rich posting can simply out-score.

    Dashes are folded first so "Head - Delivery", "Head – Delivery" and
    "Head — Delivery" are one case for the cap patterns rather than three.
    """
    probe = fold_dashes(title)
    cap: float | None = None
    rule_name: str | None = None
    for name, rule in cfg.title_penalties.items():
        if rule.fit_cap is None:
            continue
        if re.search(rule.pattern, probe, flags=re.IGNORECASE):
            if cap is None or rule.fit_cap < cap:
                cap, rule_name = float(rule.fit_cap), name
    return cap, rule_name


def apply(job, cfg: ScoringCfg = CONFIG.scoring) -> None:
    """Apply the easy-entry boost and seniority penalty to a scored job."""
    accessible, reasons = easy_entry(job.title, job.description)
    job.easy_entry = accessible
    job.reasons.extend(reasons)
    if accessible:
        boost = cfg.easy_entry_boost
        job.fit_score = min(100.0, job.fit_score + boost)
        job.reasons.append(f"easy_entry (+{boost:g})")

    penalty = seniority_penalty(job.title, cfg)
    if penalty:
        job.fit_score = max(0.0, job.fit_score + penalty)
        job.reasons.append(f"seniority_title ({penalty:+g})")

    # Ceilings apply last: no boost above may lift a capped title back over the
    # surface cutoff.
    cap, rule_name = title_fit_cap(job.title, cfg)
    if cap is not None and job.fit_score > cap:
        job.fit_score = cap
        job.reasons.append(f"leadership_cap:{rule_name} (capped at {cap:g})")


def digest_group(job) -> str | None:
    """Return the easy-entry badge — strictly from the scored verdict.

    v3.5 item 6: the old unknown-pay/role-word rescue is gone. It was a
    presentation bypass that put "NgSpice Electronics Simulation Reviewer" in
    the easy-entry section even though the detector had set easy_entry=False.
    """
    return "🎯" if getattr(job, "easy_entry", False) else None
