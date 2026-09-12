"""Weekly tuning appendix (HANDOFF v2 §12): trust deltas + reject-pile stats.

The reject pile is threshold-tuning training data — this surfaces it once a
week so thresholds get adjusted from evidence, not vibes.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone

from ..store import Store
from ..trust.registry import default_registry


def _trust_delta_lines(days: int) -> list[str]:
    cutoff = date.today() - timedelta(days=days)
    lines: list[str] = []
    for pt in sorted(default_registry().values(), key=lambda p: p.key):
        recent = [e for e in pt.evidence
                  if e.date >= cutoff and e.evidence_class != "seed"]
        if not recent:
            continue
        total = sum(e.delta for e in recent)
        kinds = ", ".join(sorted({e.evidence_class for e in recent}))
        lines.append(f"  {pt.key}: Δ{total:+.3f} over {len(recent)} event(s) [{kinds}]"
                     f" → trust {pt.trust:.2f}")
    return lines


def _reject_lines(store: Store, days: int) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = store.conn.execute(
        "SELECT verdict, scam_flags FROM jobs WHERE last_seen >= ? "
        "AND verdict LIKE 'rejected_%'", (cutoff,)).fetchall()
    if not rows:
        return []
    verdicts = Counter(r["verdict"] for r in rows)
    flags = Counter(f for r in rows for f in (r["scam_flags"] or "").split(",") if f)
    lines = ["  " + " · ".join(f"{v.removeprefix('rejected_')} {n}"
                               for v, n in verdicts.most_common())]
    if flags:
        lines.append("  top scam flags: " +
                     ", ".join(f"{f} ×{n}" for f, n in flags.most_common(4)))
    return lines


def build_weekly_report(store: Store, days: int = 7) -> str | None:
    """Markdown appendix, or None when there is nothing worth saying."""
    trust_lines = _trust_delta_lines(days)
    reject_lines = _reject_lines(store, days)
    if not trust_lines and not reject_lines:
        return None
    lines = [f"📈 Weekly tuning report (last {days}d)"]
    if trust_lines:
        lines.append("trust deltas:")
        lines.extend(trust_lines)
    if reject_lines:
        lines.append("reject pile:")
        lines.extend(reject_lines)
    return "\n".join(lines)
