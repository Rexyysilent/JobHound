"""Scam gate (HANDOFF §7a) — rules only this pass (LLM second-pass is a later
lane). Cheap signals accumulate into scam_score (0 clean … 1 almost-certainly
scam) with human-readable flags. Rejected rows are KEPT in the DB so the reject
pile can be audited and thresholds tuned.
"""
from __future__ import annotations

import re

from ..config import CONFIG, ListingQualityCfg, ScamCfg
from .listing_quality import absurd_language_check, free_host_check
from ..models import Job

# (flag, weight, compiled-regex). Weight is added to scam_score on match.
_PHRASE_SIGNALS: list[tuple[str, float, re.Pattern]] = [
    ("asks-for-money", 0.6, re.compile(
        r"registration fee|security deposit|refundable deposit|processing fee|"
        r"pay (?:a )?(?:fee|deposit)|training fee|pay for (?:training|kit|materials)|"
        r"buy your own|gift card|upi to|pay.{0,15}recruiter", re.I)),
    ("scam-archetype", 0.6, re.compile(
        r"reshipping|package forwarding|re-?ship|check cashing|cash(?:ing)? checks|"
        r"process payments|payment processing agent|mystery shopper|rebate processor|"
        r"money mule|wire transfer", re.I)),
    ("mlm", 0.5, re.compile(r"\bmlm\b|network marketing|multi-?level|downline|recruit others", re.I)),
    ("too-good-to-be-true", 0.3, re.compile(
        r"guaranteed income|earn .{0,20}(?:per (?:day|week)|/(?:day|week))|"
        r"\$\d{3,}.{0,10}(?:per day|/day|a day)|₹\s?\d{3,}.{0,10}(?:day|week)|"
        r"no experience (?:needed|required).{0,40}(?:\$|₹|earn|income)|"
        r"work (?:from )?home.{0,20}(?:\$\d{3,}|₹\s?\d{3,})", re.I)),
    ("offsite-contact", 0.2, re.compile(
        r"contact (?:us )?(?:on |via |through )?(?:whatsapp|telegram)|"
        r"(?:whatsapp|telegram) (?:only|us|me|number)|dm (?:on|me) (?:whatsapp|telegram)", re.I)),
    ("free-email-recruiter", 0.25, re.compile(
        r"[\w.+-]+@(?:gmail|outlook|hotmail|yahoo|protonmail)\.com", re.I)),
    # NOTE: unpaid assessments/quals are deliberately NOT a scam signal — they
    # are common on legit AI platforms and belong to the trust layer as
    # onboarding_cost (HANDOFF v2 §7a). Paying for training stays a scam
    # signal above ("asks-for-money").
]

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿]")


def _looks_all_caps(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    if len(letters) < 10:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.8


def score_scam(job: Job, cfg: ScamCfg = CONFIG.scam,
               listing_cfg: ListingQualityCfg = CONFIG.listing_quality) -> Job:
    text = f"{job.title}\n{job.description}"
    score = 0.0
    flags: list[str] = []

    for flag, weight, pat in _PHRASE_SIGNALS:
        if pat.search(text):
            score += weight
            flags.append(flag)

    free_host_flags = free_host_check(job.url)
    if free_host_flags:
        score += listing_cfg.free_host_scam_bump
        # A job board hosted on a disposable/free subdomain is a categorical
        # source-risk signal. Keep the configured bump for auditability, but
        # guarantee that this signal reaches the terminal scam threshold.
        score = max(score, cfg.threshold)
        flags.extend(free_host_flags)
    absurd_language_flags = absurd_language_check(job.title)
    if absurd_language_flags:
        score += listing_cfg.absurd_language_scam_bump
        flags.extend(absurd_language_flags)

    # Structural / thinness signals.
    if not job.company:
        score += 0.15
        flags.append("no-company")
    if len((job.description or "").strip()) < cfg.min_description_chars:
        score += 0.2
        flags.append("thin-description")
    if _looks_all_caps(job.title):
        score += 0.15
        flags.append("all-caps-title")
    if len(_EMOJI_RE.findall(job.title)) >= 3:
        score += 0.1
        flags.append("emoji-stuffed-title")

    job.scam_score = round(min(score, 1.0), 3)
    job.scam_flags = flags
    return job


def is_scam(job: Job, cfg: ScamCfg = CONFIG.scam) -> bool:
    return job.scam_score >= cfg.threshold
