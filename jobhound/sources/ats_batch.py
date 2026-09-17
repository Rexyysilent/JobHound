"""Isolate failures per configured ATS board, without retries or new sources.

This helper retains the existing keyless GET endpoints. It never authenticates,
submits an application, follows a redirect, or works around access restrictions.
Cooldown is reported for a future scheduler to persist; this helper is not that
scheduler. Source objects are per-run, as in the existing pipeline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from .base import Source


def _cooldown(header: str | None, now: datetime) -> str:
    try:
        if header and re.fullmatch(r"\d{1,9}", header.strip()):
            until = now + timedelta(seconds=int(header))
        elif header:
            until = parsedate_to_datetime(header)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
        else:
            until = now + timedelta(hours=1)
        return max(until, now).isoformat()
    except (TypeError, ValueError, OverflowError):
        return (now + timedelta(hours=1)).isoformat()


async def fetch_boards(
    source: Source,
    client: httpx.AsyncClient,
    boards: dict[str, str],
    *,
    api: str,
    params: dict[str, str],
    items_key: str | None,
    require_listed: bool = False,
) -> list[dict]:
    health = source.health
    out: list[dict] = []
    entries = list(boards.items())
    degraded = False
    stop_reason: str | None = None
    valid_boards = 0
    for index, (slug, company) in enumerate(entries):
        if stop_reason or (source.limit is not None and len(out) >= max(source.limit, 0)):
            reason = stop_reason or "result_budget_reached"
            health.board_results.append({"board": slug, "status": "deferred", "reason": reason})
            health.note_stage("discovery", "deferred")
            degraded = True
            continue
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", slug):
            health.board_results.append({"board": "invalid", "status": "failed", "reason": "invalid_board_slug"})
            health.note_stage("parsing", "failed")
            health.error_type = "invalid_board_slug"
            degraded = True
            continue
        url = api.format(slug=slug)
        host = urlsplit(url).hostname
        if host and host not in health.transport_hosts:
            health.transport_hosts.append(host)
        try:
            response = await client.get(url, params=params, follow_redirects=False)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            reason = f"http_{status}"
            health.failed_requests += 1
            health.note_stage("discovery", "failed")
            health.error_type = reason
            health.board_results.append({"board": slug, "status": "failed", "reason": reason})
            degraded = True
            if status in {403, 429} or (status == 503 and exc.response.headers.get("Retry-After")):
                stop_reason = reason + "_stop_batch"
                health.cooldown_until = _cooldown(exc.response.headers.get("Retry-After"), datetime.now(timezone.utc))
            continue
        except httpx.RequestError as exc:
            reason = type(exc).__name__
            health.failed_requests += 1
            health.note_stage("discovery", "failed")
            health.error_type = reason
            health.board_results.append({"board": slug, "status": "failed", "reason": reason})
            degraded = True
            continue
        health.successful_requests += 1
        health.note_stage("discovery", "succeeded")
        try:
            payload = response.json()
            if items_key is not None:
                if not isinstance(payload, dict) or items_key not in payload:
                    raise ValueError("missing_items_key")
                rows = payload[items_key]
            else:
                rows = payload
            if not isinstance(rows, list):
                raise ValueError("items_not_list")
        except (ValueError, TypeError) as exc:
            reason = "invalid_payload"
            health.note_stage("parsing", "failed")
            health.error_type = reason
            health.board_results.append({"board": slug, "status": "failed", "reason": reason})
            degraded = True
            continue
        retained, malformed, unlisted, unknown_listing = 0, 0, 0, 0
        for row in rows:
            if not isinstance(row, dict):
                malformed += 1
                continue
            if require_listed:
                if row.get("isListed") is False:
                    unlisted += 1
                    continue
                if row.get("isListed") is not True:
                    unknown_listing += 1
                    continue
            if source.limit is not None and len(out) >= max(source.limit, 0):
                break
            out.append({**row, "_slug": slug, "_company": company})
            retained += 1
        valid_boards += 1
        row_degraded = bool(malformed or unknown_listing)
        degraded = degraded or row_degraded
        health.note_stage("parsing", "failed" if row_degraded else "succeeded")
        if row_degraded:
            health.error_type = "invalid_rows_or_listing_visibility"
        health.board_results.append({
            "board": slug, "status": "partial" if row_degraded else ("ok" if rows else "empty"),
            "retained": retained, "malformed": malformed, "unlisted": unlisted,
            "unknown_listing": unknown_listing,
            "budget_truncated": source.limit is not None and retained < len(rows) - malformed - unlisted - unknown_listing and len(out) >= max(source.limit, 0),
        })
        if health.board_results[-1]["budget_truncated"]:
            degraded = True
    any_failure = health.failed_requests > 0 or health.stages["parsing"]["failed"] > 0
    health.status = ("partial" if (out or valid_boards or not any_failure) else "failed") if degraded else ("ok" if out else "empty")
    return out
