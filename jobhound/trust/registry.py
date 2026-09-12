"""Trust registry (HANDOFF v2 §8.3): load platform_registry.yaml, derive the
composite trust score from config weights, and match a job's company to a
platform_key.

Read-only this pass — evidence-weighted updates + decay-to-prior arrive with
the feedback CLI (P2/P3). Matching is deliberately company-field-only with
tight aliases: a title like "Outlier Detection Engineer" must not join the
Outlier gig platform.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import CONFIG, TrustCfg
from ..models import PlatformTrust

_ROOT = Path(__file__).resolve().parent.parent.parent

_DIMS = ("payment_reliability", "work_consistency", "account_stability",
         "support_quality", "onboarding_cost")
_NONALNUM = re.compile(r"[^a-z0-9]+")

# Re-emitted on every save — yaml.safe_dump drops comments, and the registry
# must stay self-explaining for a human auditing the evidence log.
_HEADER = """\
# Counterparty trust registry (HANDOFF v2 §8). Seeds are PRIORS as of 2026-07-02,
# not verdicts — updated in place by `jobhound feedback` (operator evidence) and,
# later, incident/review sweeps (P3). Every change appends to `evidence`.
#
# Per platform:
#   aliases        — matched (word-boundary, normalized) against a job's COMPANY
#                    field to set platform_key. Kept tight to avoid false joins
#                    (e.g. Scale AI corporate roles are NOT Outlier gig work).
#   dims           — five 0..1 trust dimensions; the composite `trust` is
#                    DERIVED at load time from config `trust.weights`, never
#                    stored here (weights are tunable without editing seeds).
#   availability_est / unpaid_overhead_est — feed effective-rate math (§8.2):
#                    effective_hourly = nominal × availability × (1 − overhead)
#   prior          — decay target (§8.3; decay itself lands in P3).
#   caveat         — one-liner surfaced in the digest for 🟠-band platforms.
#   evidence       — audit log; every score change appends here.

"""


def registry_path(cfg: "TrustCfg | None" = None) -> Path:
    return _ROOT / (cfg or CONFIG.trust).registry_file


def load_registry_raw(path: Path | str | None = None) -> dict:
    p = Path(path) if path else registry_path()
    if not p.exists():
        return {"platforms": {}}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"platforms": {}}


def save_registry_raw(data: dict, path: Path | str | None = None) -> None:
    p = Path(path) if path else registry_path()
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=88)
    p.write_text(_HEADER + body, encoding="utf-8")
    clear_caches()  # next default_registry() reload picks up the change


def clear_caches() -> None:
    default_registry.cache_clear()
    default_matcher.cache_clear()


def composite_trust(dims: dict[str, float], weights: dict[str, float]) -> float:
    return round(sum(weights.get(d, 0.0) * dims[d] for d in _DIMS), 4)


def load_registry(path: Path | str | None = None,
                  cfg: TrustCfg = CONFIG.trust) -> dict[str, PlatformTrust]:
    p = Path(path) if path else _ROOT / cfg.registry_file
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    registry: dict[str, PlatformTrust] = {}
    for key, entry in (data.get("platforms") or {}).items():
        dims = entry.pop("dims")
        pt = PlatformTrust(key=key, **dims, **entry)
        pt.trust = composite_trust(dims, cfg.weights)
        registry[key] = pt
    return registry


class PlatformMatcher:
    """company string → platform_key via word-boundary alias match on the
    normalized (lowercase, alnum-only) company name."""

    def __init__(self, registry: dict[str, PlatformTrust]):
        self._patterns: list[tuple[re.Pattern, str]] = []
        for key, pt in registry.items():
            for alias in pt.aliases or [key]:
                norm = _NONALNUM.sub(" ", alias.lower()).strip()
                if norm:
                    self._patterns.append(
                        (re.compile(rf"\b{re.escape(norm)}\b"), key))
        # Longest alias first so "telus international ai" beats "telus international".
        self._patterns.sort(key=lambda p: -len(p[0].pattern))

    def match(self, company: str | None) -> str | None:
        if not company:
            return None
        norm = _NONALNUM.sub(" ", company.lower()).strip()
        for pat, key in self._patterns:
            if pat.search(norm):
                return key
        return None


@lru_cache(maxsize=1)
def default_registry() -> dict[str, PlatformTrust]:
    return load_registry()


@lru_cache(maxsize=1)
def default_matcher() -> PlatformMatcher:
    return PlatformMatcher(default_registry())
