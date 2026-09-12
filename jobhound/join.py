"""Tier B join-checklist (HANDOFF v2 §10) — which platforms to sign up for next.

Priority = trust × language_fit, so the ordering falls out of the data: the
registry says who's reliable, this file says where the Bengali edge pays, and
the checklist says "TELUS/Appen first, Outlier only as diversification backup"
without anyone hand-ranking it. v2 rule: a platform with trust < 0.5 is never
nudged without a warning badge; below the hard floor it isn't nudged at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import CONFIG
from .models import PlatformTrust
from .trust.registry import default_registry

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PATH = _ROOT / "platforms_to_join.yaml"

STATUSES = ("todo", "in_progress", "joined", "skipped")

# Re-emitted on save — yaml.safe_dump drops comments.
_HEADER = """\
# Tier B signup checklist (HANDOFF v2 §10). Ordering is computed at runtime as
# trust × language_fit; trust lives in platform_registry.yaml, never here.
#   status       — todo | in_progress | joined | skipped
#                  (update via `python run.py join set <platform> <status>`)
#   language_fit — 0..1 Bengali/India-multilingual edge on this platform
#   signup_url   — seed URLs; correct them if a portal moved
#   note         — signup intel (requirements, eligibility, caveats)

"""


@dataclass
class JoinCandidate:
    key: str
    display_name: str
    status: str
    language_fit: float
    signup_url: str
    note: str
    trust: float
    priority: float          # trust × language_fit
    warn: bool               # trust < 0.5 → must carry a warning badge


def _load_raw(path: Path | str | None = None) -> dict:
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        return {"platforms": {}}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"platforms": {}}


def load_checklist(path: Path | str | None = None,
                   registry: dict[str, PlatformTrust] | None = None) -> list[JoinCandidate]:
    """All checklist entries joined with live registry trust, priority-ordered."""
    registry = registry if registry is not None else default_registry()
    out: list[JoinCandidate] = []
    for key, entry in (_load_raw(path).get("platforms") or {}).items():
        pt = registry.get(key)
        trust = pt.trust if pt else 0.0
        fit = float(entry.get("language_fit", 0.5))
        out.append(JoinCandidate(
            key=key,
            display_name=pt.display_name if pt else key,
            status=str(entry.get("status", "todo")),
            language_fit=fit,
            signup_url=str(entry.get("signup_url", "")),
            note=str(entry.get("note", "")),
            trust=trust,
            priority=round(trust * fit, 3),
            warn=trust < 0.5,
        ))
    out.sort(key=lambda c: -c.priority)
    return out


def pending(path: Path | str | None = None,
            registry: dict[str, PlatformTrust] | None = None) -> list[JoinCandidate]:
    """Nudge pool: not yet acted on, and above the trust hard floor."""
    floor = CONFIG.trust.hard_floor
    return [c for c in load_checklist(path, registry)
            if c.status == "todo" and c.trust >= floor]


def render_nudges(n: int | None = None,
                  path: Path | str | None = None,
                  registry: dict[str, PlatformTrust] | None = None) -> list[str]:
    """Digest lines for the top-n unjoined platforms (§10)."""
    n = n if n is not None else CONFIG.digest.join_nudges
    lines: list[str] = []
    for c in pending(path, registry)[:n]:
        head = (f"• {c.display_name} — trust {c.trust:.2f} · lang-fit {c.language_fit:.1f} "
                f"· priority {c.priority:.2f}")
        if c.warn:
            head += "  ⚠ low trust — diversification backup only"
        lines.append(head)
        if c.note:
            lines.append(f"  {c.note}")
        if c.signup_url:
            lines.append(f"  {c.signup_url}")
    return lines


def set_status(key: str, status: str, path: Path | str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r} (known: {', '.join(STATUSES)})")
    p = Path(path) if path else _DEFAULT_PATH
    data = _load_raw(p)
    platforms = data.get("platforms") or {}
    if key not in platforms:
        raise ValueError(f"unknown platform {key!r} (known: {', '.join(sorted(platforms))})")
    platforms[key]["status"] = status
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=88)
    p.write_text(_HEADER + body, encoding="utf-8")
