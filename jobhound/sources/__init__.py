"""Source registry."""
from __future__ import annotations

from .adzuna import AdzunaSource
from .arbeitnow import ArbeitnowSource
from .ats_ashby import AshbySource
from .ats_greenhouse import GreenhouseSource
from .ats_lever import LeverSource
from .base import RawRecord, Source, SourceHealth, summarize_stage_health
from .email_offers import EmailOffersSource
from .jsearch import JSearchSource
from .remoteok import RemoteOkSource
from .remotive import RemotiveSource
from .serp_dork import SerpDorkSource

_REGISTRY = {
    RemotiveSource.name: RemotiveSource,
    RemoteOkSource.name: RemoteOkSource,
    ArbeitnowSource.name: ArbeitnowSource,
    AdzunaSource.name: AdzunaSource,
    JSearchSource.name: JSearchSource,
    GreenhouseSource.name: GreenhouseSource,
    LeverSource.name: LeverSource,
    AshbySource.name: AshbySource,
    SerpDorkSource.name: SerpDorkSource,
    EmailOffersSource.name: EmailOffersSource,
}


def build_sources(limit: int | None = None, only: str | None = None) -> list[Source]:
    """Instantiate sources. `only` restricts to a single source by name (debug)."""
    names = [only] if only else list(_REGISTRY)
    out: list[Source] = []
    for name in names:
        cls = _REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"unknown source: {name} (known: {', '.join(_REGISTRY)})")
        out.append(cls(limit=limit))
    return out


__all__ = ["Source", "SourceHealth", "RawRecord", "build_sources", "summarize_stage_health"]
