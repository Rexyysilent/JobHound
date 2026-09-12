"""Source ABC. Each adapter returns raw provider items tagged with the source
name; normalize.py maps them to the canonical Job. One dead source must never
kill the run (HANDOFF implementer notes), so `fetch` swallows + logs errors.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Literal

import httpx

log = logging.getLogger("jobhound.sources")

# A raw record flowing out of a source: {"source": <name>, "raw": <provider dict>}
RawRecord = dict


@dataclass
class SourceHealth:
    source: str
    status: Literal["ok", "empty", "partial", "failed", "disabled"]
    item_count: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    retries: int = 0
    error_type: str | None = None
    # V5.5 stage diagnostics.  An unexercised stage remains ``unknown``;
    # discovery success must not erase resolver/hydrator degradation.
    stages: dict[str, dict] = field(default_factory=lambda: {
        name: {"status": "unknown", "attempted": 0, "succeeded": 0,
               "deferred": 0, "failed": 0, "skipped": 0}
        for name in ("discovery", "resolution", "hydration", "parsing", "freshness")
    })
    transport_hosts: list[str] = field(default_factory=list)
    cooldown_until: str | None = None
    last_useful_output_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def note_stage(self, stage: str, outcome: str, *, count: int = 1) -> None:
        if stage not in self.stages:
            raise ValueError(f"unknown source stage: {stage}")
        row = self.stages[stage]
        row["status"] = "ok" if outcome == "succeeded" else (
            "degraded" if outcome in {"failed", "deferred", "skipped"} else outcome
        )
        row["attempted"] += count if outcome not in {"deferred", "skipped"} else 0
        if outcome in row:
            row[outcome] += count


def summarize_stage_health(health: SourceHealth, *, previous: dict | None = None) -> dict:
    """Project stage counters without conflating skips and remote failures."""
    exercised = [row for row in health.stages.values() if row["attempted"] or row["deferred"] or row["skipped"]]
    degraded = any(row["failed"] or row["deferred"] or row["skipped"] for row in exercised)
    resolution = health.stages["resolution"]
    prior_resolution = (previous or {}).get("resolution")
    return {
        "overall": "degraded" if degraded else ("ok" if exercised else "unknown"),
        "remote_http_errors": sum(row["failed"] for row in health.stages.values()),
        "skipped_tasks": sum(row["skipped"] for row in health.stages.values()),
        "resolution_recovered": bool(resolution["attempted"] and resolution["succeeded"] and prior_resolution == "degraded"),
    }


class Source(ABC):
    name: str = "base"

    def __init__(self, limit: int | None = None):
        self.limit = limit  # optional cap per source (debug)
        self.health = SourceHealth(source=self.name, status="disabled")

    @property
    @abstractmethod
    def enabled(self) -> bool:
        ...

    @abstractmethod
    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        """Return provider-native dicts. Raise freely — `fetch` handles it."""

    async def fetch(self, client: httpx.AsyncClient) -> list[RawRecord]:
        self.health = SourceHealth(source=self.name, status="disabled")
        if not self.enabled:
            return []
        self.health.status = "ok"
        try:
            items = await self._fetch(client)
        except Exception as e:  # noqa: BLE001 — resilience: isolate per-source failures
            self.health.status = "failed"
            self.health.failed_requests += 1
            self.health.error_type = type(e).__name__
            self.health.note_stage("discovery", "failed")
            # Several HTTPX timeout exceptions stringify to an empty string.
            # Keep the warning useful without logging request objects/URLs.
            detail = str(e).strip()
            log.warning(
                "source %s failed: %s%s",
                self.name,
                type(e).__name__,
                f": {detail}" if detail else "",
            )
            return []
        if self.limit is not None:
            items = items[: self.limit]
        self.health.item_count = len(items)
        self.health.note_stage("discovery", "succeeded")
        if self.health.status == "ok":
            self.health.status = "ok" if items else "empty"
        if self.health.status in {"partial", "failed"}:
            log.warning(
                "source %s %s: %d raw items retained; %s",
                self.name, self.health.status, len(items),
                self.health.error_type or "request failure",
            )
        else:
            log.info("source %s: %d raw items", self.name, len(items))
        return [{"source": self.name, "raw": item} for item in items]
