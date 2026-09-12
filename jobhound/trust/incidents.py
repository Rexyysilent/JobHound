"""Incident detector (HANDOFF v2 §8.3, P3) — news watch per platform.

Weekly sweep of Google News RSS (keyless, stable XML) with the §8.3 query
shape: `"{platform}" (not paying OR deactivated OR lawsuit OR "mass ban")`.
A hit only becomes an incident when the same story pattern is carried by
≥ min_sources DISTINCT outlets inside the lookback window — single-outlet
noise never moves trust. A corroborated incident writes class-2 evidence
(via trust.update) and returns a ⚠ digest alert line; seen article URLs are
remembered in sqlite so a story fires exactly once.

Classification is keyword-rules for now (config `incidents.keywords`); the
LLM classifier can slot in front of `detect` later without touching the flow.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..config import CONFIG, IncidentsCfg
from ..models import PlatformTrust
from ..store import Store
from .registry import default_registry
from .update import apply_incident

log = logging.getLogger("jobhound.incidents")

_RSS = "https://news.google.com/rss/search"
_QUERY_TERMS = '("not paying" OR deactivated OR lawsuit OR "mass ban")'
_PAREN = re.compile(r"\s*\(.*\)$")


@dataclass
class NewsItem:
    title: str
    link: str
    source: str          # outlet name
    published: datetime | None


def news_query_name(pt: PlatformTrust) -> str:
    """Display name minus disambiguators: 'Outlier (Scale AI)' → 'Outlier'."""
    return _PAREN.sub("", pt.display_name).strip()


def parse_rss(xml_text: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("rss parse error: %s", e)
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        source = (it.findtext("source") or "").strip()
        pub = None
        pub_raw = it.findtext("pubDate")
        if pub_raw:
            try:
                pub = parsedate_to_datetime(pub_raw)
            except (TypeError, ValueError):
                pub = None
        if title and link:
            items.append(NewsItem(title=title, link=link, source=source, published=pub))
    return items


def detect(platform_name: str, items: list[NewsItem],
           seen_urls: set[str],
           cfg: IncidentsCfg = CONFIG.incidents,
           now: datetime | None = None) -> tuple[str, list[NewsItem]] | None:
    """Return (wounded_dimension, corroborating_items) or None.

    An item counts when it is fresh, unseen, names the platform, and matches an
    incident keyword. Corroboration = matching items from ≥ min_sources
    distinct outlets; the dimension is taken from the most-matched keyword.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.lookback_days)
    name_l = platform_name.lower()

    by_dim: dict[str, list[NewsItem]] = {}
    for item in items:
        if item.link in seen_urls:
            continue
        if item.published is not None and item.published < cutoff:
            continue
        title_l = item.title.lower()
        if name_l not in title_l:
            continue  # platform must be the story's subject, not a footnote
        for pattern, dim in cfg.keywords.items():
            if re.search(pattern, title_l, re.I):
                by_dim.setdefault(dim, []).append(item)
                break

    for dim, hits in sorted(by_dim.items(), key=lambda kv: -len(kv[1])):
        outlets = {h.source or h.link for h in hits}
        if len(outlets) >= cfg.min_sources:
            return dim, hits
    return None


async def sweep(store: Store,
                registry: dict[str, PlatformTrust] | None = None,
                cfg: IncidentsCfg = CONFIG.incidents,
                dry_run: bool = False) -> list[str]:
    """Run the news sweep across all registry platforms. Returns ⚠ alert lines
    for the digest. dry_run: report what WOULD fire, write nothing."""
    registry = registry if registry is not None else default_registry()
    alerts: list[str] = []
    headers = {"User-Agent": CONFIG.http.user_agent}
    async with httpx.AsyncClient(headers=headers, timeout=CONFIG.http.timeout_seconds,
                                 follow_redirects=True) as client:
        for key, pt in registry.items():
            name = news_query_name(pt)
            try:
                resp = await client.get(_RSS, params={
                    "q": f'"{name}" {_QUERY_TERMS}',
                    "hl": "en-IN", "gl": "IN", "ceid": "IN:en",
                })
                resp.raise_for_status()
            except Exception as e:  # noqa: BLE001 — one platform must not kill the sweep
                log.warning("incident sweep failed for %s: %s", key, e)
                continue
            items = parse_rss(resp.text)
            hit = detect(name, items, store.seen_incident_urls(key), cfg)
            if hit is None:
                continue
            dim, evidence_items = hit
            top = evidence_items[0]
            note = (f"news x{len(evidence_items)} "
                    f"({', '.join(sorted({i.source for i in evidence_items if i.source})[:3])}): "
                    f"{top.title[:120]}")
            alerts.append(f"⚠ trust event: {pt.display_name} — {dim} {cfg.delta:+.2f} — {top.title}")
            if dry_run:
                continue
            apply_incident(key, dim, note)
            for item in evidence_items:
                store.add_incident(key, item.link, item.title, item.source)
    return alerts
