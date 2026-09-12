"""Evidence-weighted trust updates (HANDOFF v2 §8.3/§8.5).

An operator feedback event maps (via config `feedback.events`) to one or more
trust-dimension deltas. Asymmetry is built in: negative deltas apply in full
(drops are fast), positive deltas are scaled by `positive_scale` and diminish
on repetition (`diminishing_factor ** prior_count` — one good payout doesn't
undo a deactivation wave). Every applied event appends a dated class-1
`operator` evidence entry to platform_registry.yaml, keeping the registry an
auditable log, not just numbers. Decay-to-prior is P3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import CONFIG, Config
from .registry import composite_trust, load_registry_raw, save_registry_raw

_DIMS = ("payment_reliability", "work_consistency", "account_stability",
         "support_quality", "onboarding_cost")


@dataclass
class FeedbackResult:
    platform_key: str
    event: str
    old_trust: float
    new_trust: float
    # dim -> (old, new); empty for log-only events like false_positive_scam
    dim_changes: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def delta(self) -> float:
        return round(self.new_trust - self.old_trust, 4)


def event_deltas(event: str, prior_count: int = 0,
                 cfg: Config = CONFIG) -> dict[str, float]:
    """Resolve an event to effective dim deltas (asymmetry + diminishing applied)."""
    if event not in cfg.feedback.events:
        known = ", ".join(sorted(cfg.feedback.events))
        raise ValueError(f"unknown feedback event {event!r} (known: {known})")
    out: dict[str, float] = {}
    for dim, delta in cfg.feedback.events[event].items():
        if dim not in _DIMS:
            raise ValueError(f"event {event!r} maps to unknown dimension {dim!r}")
        if delta > 0:
            delta *= cfg.feedback.positive_scale
            delta *= cfg.feedback.diminishing_factor ** prior_count
        out[dim] = delta
    return out


def _apply_deltas(platform_key: str, deltas: dict[str, float], *,
                  evidence_class: str, note: str, event: str,
                  registry_file=None, cfg: Config = CONFIG) -> FeedbackResult:
    """Shared core: mutate dims, append an evidence entry, save. All trust
    changes — operator, incident, review, decay — flow through here so the
    registry stays a complete audit log."""
    data = load_registry_raw(registry_file)
    platforms = data.get("platforms") or {}
    if platform_key not in platforms:
        known = ", ".join(sorted(platforms))
        raise ValueError(f"unknown platform {platform_key!r} (known: {known})")
    entry = platforms[platform_key]
    dims = entry["dims"]

    old_trust = composite_trust(dims, cfg.trust.weights)
    changes: dict[str, tuple[float, float]] = {}
    for dim, delta in deltas.items():
        if dim not in _DIMS:
            raise ValueError(f"unknown dimension {dim!r}")
        old = dims[dim]
        new = round(min(1.0, max(0.0, old + delta)), 4)
        dims[dim] = new
        changes[dim] = (old, new)
    new_trust = composite_trust(dims, cfg.trust.weights)

    entry.setdefault("evidence", []).append({
        "class": evidence_class,
        "date": date.today(),
        "delta": round(new_trust - old_trust, 4),
        "note": note,
    })
    entry["last_reviewed"] = date.today()

    save_registry_raw(data, registry_file)
    return FeedbackResult(platform_key, event, old_trust, new_trust, changes)


def apply_feedback(platform_key: str, event: str, *,
                   amount: float | None = None,
                   note: str | None = None,
                   prior_count: int = 0,
                   registry_file=None,
                   cfg: Config = CONFIG) -> FeedbackResult:
    """Class-1 operator event. Raises ValueError on unknown platform/event."""
    deltas = event_deltas(event, prior_count, cfg)
    parts = [event]
    if amount is not None:
        parts.append(f"${amount:g}")
    if note:
        parts.append(note)
    return _apply_deltas(platform_key, deltas, evidence_class="operator",
                         note="; ".join(parts), event=event,
                         registry_file=registry_file, cfg=cfg)


def apply_incident(platform_key: str, dimension: str, note: str, *,
                   registry_file=None, cfg: Config = CONFIG) -> FeedbackResult:
    """Class-2 corroborated incident (news sweep) — §8.3."""
    return _apply_deltas(platform_key, {dimension: cfg.incidents.delta},
                         evidence_class="incident", note=note, event="incident",
                         registry_file=registry_file, cfg=cfg)


def apply_review(platform_key: str, rating: float, *, scale: float = 5.0,
                 source: str = "reviews", registry_file=None,
                 cfg: Config = CONFIG) -> FeedbackResult:
    """Class-3 aggregate-review level (Trustpilot/Glassdoor/Indeed), entered
    manually — automating those pulls would breach their ToS. Rating maps to a
    uniform dim delta capped at ±0.03/cycle (§8.3): clearly bad ratings
    (≤2.2/5) saturate at −0.03, clearly good (≥3.8/5) at +0.03, near-midpoint
    ratings barely move anything.
    """
    if not (0 <= rating <= scale):
        raise ValueError(f"rating {rating} outside 0..{scale}")
    raw = (rating / scale - 0.5) * 0.5           # pivot at the scale midpoint
    delta = max(-0.03, min(0.03, raw))           # capped at the §8.3 ceiling
    deltas = {dim: delta for dim in _DIMS}       # uniform → composite moves ~delta
    return _apply_deltas(platform_key, deltas, evidence_class="aggregate_reviews",
                         note=f"{source}: {rating:g}/{scale:g}", event="review",
                         registry_file=registry_file, cfg=cfg)


def apply_decay(registry_file=None, cfg: Config = CONFIG,
                today: date | None = None) -> list[str]:
    """§8.3 decay-to-prior: platforms untouched for min_idle_days drift toward
    their prior at rate_per_month. Applied uniformly across dims (weights sum
    to 1, so the composite moves by the same amount). Returns changed keys."""
    today = today or date.today()
    data = load_registry_raw(registry_file)
    changed: list[str] = []
    for key, entry in (data.get("platforms") or {}).items():
        last = entry.get("last_reviewed")
        idle_days = (today - last).days if last else cfg.trust.decay.min_idle_days
        if idle_days < cfg.trust.decay.min_idle_days:
            continue
        dims = entry["dims"]
        current = composite_trust(dims, cfg.trust.weights)
        gap = entry.get("prior", current) - current
        if abs(gap) < 1e-4:
            continue
        drift = cfg.trust.decay.rate_per_month * (idle_days / 30.0)
        delta = max(-drift, min(drift, gap))
        for dim in _DIMS:
            dims[dim] = round(min(1.0, max(0.0, dims[dim] + delta)), 4)
        new = composite_trust(dims, cfg.trust.weights)
        entry.setdefault("evidence", []).append({
            "class": "decay",
            "date": today,
            "delta": round(new - current, 4),
            "note": f"idle {idle_days}d — drift toward prior {entry.get('prior')}",
        })
        entry["last_reviewed"] = today
        changed.append(key)
    if changed:
        save_registry_raw(data, registry_file)
    return changed
