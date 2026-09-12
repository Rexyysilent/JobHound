"""Build the ranked daily digest (HANDOFF v2 §12) and write it to data/digest_*.md.

Each surfaced job is assigned to exactly one presentation group by priority:
language-edge → AI-training → data/automation → pay-unknown leftover. Within a
group, pay-confirmed jobs rank above pay-unknown, then by ev_score (fit ×
trust × confidence × freshness — §8.2).

Per entry: title · company · nominal → effective $/hr · fit/ev · trust badge ·
confidence badge · 1-line why · link. 🟠-band platforms carry their registry
caveat so a volatile counterparty is never surfaced bare.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

from ..config import CONFIG
from ..filters.accessibility import digest_group
from ..models import Job
from ..trust.registry import default_registry

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

GROUPS = [
    ("easy", "🎯 Easy-entry AI work"),
    ("lang", "🌐 Bengali / multilingual"),
    ("high", "🔥 AI-training / RLHF"),
    ("mid", "💻 Data & automation"),
    ("unknown_pay", "❓ Pay-unknown but interesting"),
]


def _has(reasons: list[str], prefix: str) -> bool:
    return any(r.startswith(prefix) for r in reasons)


def _group_of(job: Job) -> str:
    if digest_group(job) == "🎯":
        return "easy"
    if "bengali" in job.region_tags or _has(job.fit_reasons, "boost_lang"):
        return "lang"
    if _has(job.fit_reasons, "boost_high"):
        return "high"
    if _has(job.fit_reasons, "boost_mid"):
        return "mid"
    if job.pay_ok is None:
        return "unknown_pay"
    return "mid"


def _why(job: Job) -> str:
    for r in job.fit_reasons:
        if r.startswith(("easy_entry (+", "boost_high", "boost_lang", "boost_mid")):
            return r
    return job.fit_reasons[0] if job.fit_reasons else "remote match"


def trust_badge(trust: float | None) -> str:
    """§12 bands. 🔴 (<hard_floor) never reaches the digest — rejected_trust."""
    if trust is None:
        return "❔ unrated"
    if trust >= 0.7:
        icon = "🟢"
    elif trust >= 0.5:
        icon = "🟡"
    elif trust >= CONFIG.trust.hard_floor:
        icon = "🟠"
    else:
        icon = "🔴"
    return f"{icon} trust {trust:.2f}"


def confidence_badge(confidence: float) -> str:
    if confidence >= 0.75:
        return "●●●"
    if confidence >= 0.45:
        return "●●○"
    return "●○○"


def _caveat(job: Job) -> str | None:
    """Registry caveat line for volatile (🟠-band) counterparties."""
    if job.platform_key is None or job.platform_trust is None:
        return None
    if job.platform_trust >= 0.5:
        return None
    pt = default_registry().get(job.platform_key)
    if pt is None or not pt.caveat:
        return None
    return f"⚠ {pt.display_name}: {pt.caveat}"


def _entry(job: Job) -> str:
    company = job.company or "—"
    easy_badge = "🎯 " if job.easy_entry else ""
    head = (f"• {easy_badge}{job.title} — {company} | {job.pay_display_full} | "
            f"fit {job.fit_score:.0f} · ev {job.ev_score:.0f} | "
            f"{trust_badge(job.platform_trust)} | {confidence_badge(job.listing_confidence)}")
    lines = [head, f"  why: {_why(job)}"]
    caveat = _caveat(job)
    if caveat:
        lines.append(f"  {caveat}")
    lines.append(f"  {job.url}")
    return "\n".join(lines)


def _rank_key(job: Job):
    # pay-confirmed first, then higher expected value.
    return (1 if job.pay_ok else 0, job.ev_score)


def cap_per_company(jobs_sorted_by_ev: list[Job], n: int = 2) -> tuple[list[Job], dict[str, int]]:
    """Keep at most ``n`` jobs per company and count the hidden remainder."""
    seen: Counter[str] = Counter()
    hidden: Counter[str] = Counter()
    kept: list[Job] = []
    for job in jobs_sorted_by_ev:
        display = job.company or "?"
        key = display.strip().casefold()
        if seen[key] < max(0, n):
            kept.append(job)
            seen[key] += 1
        else:
            hidden[display] += 1
    return kept, dict(hidden)


def build_digest(jobs: list[Job], summary: str = "", top_n: int = 15,
                 alerts: list[str] | None = None,
                 appendix: str | None = None,
                 per_company_cap: int | None = None) -> str:
    buckets: dict[str, list[Job]] = {k: [] for k, _ in GROUPS}
    for job in jobs:
        buckets[_group_of(job)].append(job)

    lines = [f"🐕 jobhound digest — {date.today().isoformat()}"]
    if summary:
        lines.append(summary)
    lines.append("")

    # ⚠ trust events lead — a platform turning bad outranks any single job.
    if alerts:
        lines.append(f"⚠ Trust events ({len(alerts)})")
        lines.extend(alerts)
        lines.append("")

    if not jobs:
        lines.append("No new surfaced jobs this run.")
        if appendix:
            lines.extend(["", appendix])
        return "\n".join(lines)

    company_seen: Counter[str] = Counter()
    company_cap = (CONFIG.digest.per_company_cap
                   if per_company_cap is None else per_company_cap)
    for key, label in GROUPS:
        group = sorted(buckets[key], key=_rank_key, reverse=True)
        if not group:
            continue
        shown: list[Job] = []
        company_hidden: Counter[str] = Counter()
        top_n_hidden = 0
        for job in group:
            display = job.company or "?"
            company_key = display.strip().casefold()
            if company_seen[company_key] >= max(0, company_cap):
                company_hidden[display] += 1
            elif len(shown) < max(0, top_n):
                shown.append(job)
                company_seen[company_key] += 1
            else:
                top_n_hidden += 1
        lines.append(f"{label} ({len(group)})")
        for job in shown:
            lines.append(_entry(job))
        for company, hidden_count in company_hidden.items():
            lines.append(f"  +{hidden_count} more from {company}")
        if top_n_hidden > 0:
            lines.append(f"  …and {top_n_hidden} more")
        lines.append("")

    # §10: nudge the next platforms worth joining (trust × language-fit).
    if CONFIG.digest.join_nudges > 0:
        from ..join import render_nudges  # local import — join pulls in registry
        nudges = render_nudges(CONFIG.digest.join_nudges)
        if nudges:
            lines.append("📋 Join next (`python run.py join show` for the full list)")
            lines.extend(nudges)
            lines.append("")

    if appendix:
        lines.append(appendix)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_digest_file(text: str, data_dir: Path | None = None) -> Path:
    d = data_dir or _DATA_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"digest_{date.today().strftime('%Y%m%d')}.md"
    path.write_text(text, encoding="utf-8")
    return path
