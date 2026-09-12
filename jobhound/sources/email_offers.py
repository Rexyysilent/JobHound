"""Email-offer ingestion (v3 Patch #2) — platform notification emails
(OneForma, Appen, Outlier, ...) as a first-class source. These are
pre-targeted by the platform against the operator's actual qualifications and
carry real rates — the highest-confidence listings in the system
(listing_confidence 0.9; 0.6 on the parse-fail safety net).

Setup (README): a Gmail filter routes platform senders to the label
"JobHound"; this adapter reads that folder via IMAP (same Gmail app password
as the SMTP digest channel — app passwords cover both). Processed message
UIDs checkpoint in sqlite so mail is never double-ingested. Unknown senders
inside the folder are ignored (safe default).

Security note: email content is untrusted data — parsed with regex only,
never executed; the digest links only to the platform's own offer links.

NOTE: like the rest of the pipeline, a --dry-run still marks UIDs processed.
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import logging
import re
from html import unescape

import httpx

from ..config import CONFIG
from ..settings import settings
from .base import Source

log = logging.getLogger("jobhound.sources")


# --------------------------------------------------------------- rate parsing

_CUR = {"$": "USD", "us$": "USD", "usd": "USD", "dol": "USD", "dollars": "USD",
        "€": "EUR", "eur": "EUR", "₹": "INR", "inr": "INR", "rs": "INR", "rs.": "INR"}

_BASIS = {"hour": "hour", "hr": "hour", "hourly": "hour",
          "task": "task", "word": "word", "month": "month", "year": "year",
          "audio minute": "audio_min", "audio min": "audio_min"}

# "$3.50/hour" · "USD 5 per hour" · "5 dol/hour" · "Rate: $5.00 hourly" · "₹300/hr"
_RATE_RE = re.compile(
    r"(?:(?P<cur1>us\$|usd|dollars|dol|rs\.?|inr|[$€₹])\s*)?"
    r"(?P<amt>\d{1,5}(?:[.,]\d{1,2})?)"
    r"\s*(?:(?P<cur2>usd|dollars|dol)\s*)?"
    r"\s*(?P<sep>/|per\s+)?\s*"
    r"(?P<basis>hourly|hour|hr|task|word|month|year|audio\s*min(?:ute)?)",
    re.IGNORECASE,
)

_GUARANTEED_RE = re.compile(
    r"\b(full[\s-]?time|guaranteed\s+hours|40\s*(?:\+\s*)?hours?(?:\s*/|\s+per\s+)?\s*week|weekly\s+commitment)\b",
    re.IGNORECASE,
)

# Project names must be Capitalized in the source text — "run your project
# smoothly" is prose, "Project Hermes" is a name (verified against real Outlier
# marketing mail 2026-07-18). Generic words after "Project" ("Project
# Invitation: Project Milky Way") must not swallow the real name either.
_PROJECT_RE = re.compile(
    r"\b[Pp]roject\s+(?i:(?!invitation|update|details|offer|opportunit))"
    r"([A-Z][\w' -]{2,40})")

_LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def _pick_rate(text: str) -> tuple[re.Match, float, str, str] | None:
    """Best rate match in text, or None.

    Real mail is full of rate-shaped noise — "5 years experience" parses as
    5/year, "40 hours per week" as 40/hour (both seen live 2026-07-18). A
    candidate therefore needs a currency marker ($/₹/USD/…), or an explicit
    rate separator ("/", "per") / the word "hourly" for bare numbers;
    currency-marked matches win over bare-hourly ones.
    """
    best: tuple[tuple[bool, bool], re.Match, float, str, str] | None = None
    for m in _RATE_RE.finditer(text):
        has_cur = bool(m.group("cur1") or m.group("cur2"))
        amount = float(m.group("amt").replace(",", "."))
        cur_raw = (m.group("cur1") or m.group("cur2") or "usd").lower().strip()
        currency = _CUR.get(cur_raw, "USD")
        basis_raw = re.sub(r"\s+", " ", m.group("basis").lower())
        basis = _BASIS.get(basis_raw, "hour" if basis_raw in ("hr", "hourly") else basis_raw)
        if not has_cur:
            is_rate_shaped = basis == "hour" and (m.group("sep") or basis_raw == "hourly")
            if not is_rate_shaped:
                continue
        rank = (has_cur, basis == "hour")
        if best is None or rank > best[0]:
            best = (rank, m, amount, currency, basis)
    if best is None:
        return None
    return best[1], best[2], best[3], best[4]


def parse_rate(text: str) -> tuple[float, str, str] | None:
    """-> (amount, currency, basis) or None."""
    picked = _pick_rate(text)
    if picked is None:
        return None
    _, amount, currency, basis = picked
    return amount, currency, basis


def parse_offer(subject: str, body: str) -> dict:
    """One email -> one raw offer dict (pipeline-compatible)."""
    text = f"{subject}\n{body}"
    picked = _pick_rate(text)
    pm = _PROJECT_RE.search(text)
    title = (pm.group(0).strip() if pm else subject.strip())[:120]
    offer = {
        "title": title,
        "description": body[:4000],
        "is_remote": True,
        "guaranteed_hours": bool(_GUARANTEED_RE.search(text)),
        "listing_confidence": 0.9,
        "pay_raw": None, "pay_amount": None, "pay_currency": None,
        "pay_basis": "unknown",
    }
    if picked:
        m, amt, cur, basis = picked
        offer.update(pay_raw=m.group(0),
                     pay_amount=amt, pay_currency=cur, pay_basis=basis)
    else:
        # parse-fail safety net: surface in ❓ group rather than drop
        offer["listing_confidence"] = 0.6
    return offer


def _plain_text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        content = unescape(re.sub(r"<[^>]+>", " ", content))
    return re.sub(r"[ \t]+", " ", content)


def _sender_domain(from_addr: str) -> str:
    return from_addr.split("@")[-1].lower().strip(">").strip()


def _sender_platform(from_addr: str, mapping: dict[str, str]) -> str | None:
    dom = _sender_domain(from_addr)
    for known, key in mapping.items():
        if dom == known or dom.endswith("." + known):
            return key
    return None


def _platform_link(body: str, sender_dom: str) -> str:
    """First link pointing back at the platform's own domain; else its homepage.
    (HANDOFF §10: we link to originals — never to anything else in the mail.)"""
    root = ".".join(sender_dom.split(".")[-2:])
    for m in _LINK_RE.finditer(body):
        try:
            host = m.group(0).split("/")[2].lower()
        except IndexError:
            continue
        if host == root or host.endswith("." + root):
            return m.group(0)
    return f"https://{root}"


# --------------------------------------------------------------- IMAP source

class EmailOffersSource(Source):
    name = "email_offers"

    @property
    def enabled(self) -> bool:
        cfg = CONFIG.email_ingest
        if cfg.enabled and not settings.imap_ready:
            log.warning("email_ingest enabled but IMAP creds missing in .env — skipping")
            return False
        return cfg.enabled

    async def _fetch(self, client: httpx.AsyncClient) -> list[dict]:
        # imaplib is synchronous — run the whole sweep off the event loop.
        return await asyncio.to_thread(self._fetch_sync)

    def _fetch_sync(self) -> list[dict]:
        from ..store import Store  # local import — sources stay store-free otherwise

        cfg = CONFIG.email_ingest
        store = Store()
        out: list[dict] = []
        try:
            with imaplib.IMAP4_SSL(cfg.host) as imap:
                imap.login(settings.imap_user_effective, settings.imap_password_effective)
                typ, _ = imap.select(f'"{cfg.folder}"', readonly=True)
                if typ != "OK":
                    log.warning(
                        "email_ingest: Gmail label %r not found — create it plus "
                        "a filter routing platform senders into it (README)",
                        cfg.folder)
                    return out
                _, data = imap.uid("search", None, "ALL")
                for uid in (data[0] or b"").split():
                    uid_s = uid.decode()
                    if store.email_uid_seen(uid_s):
                        continue
                    _, msg_data = imap.uid("fetch", uid, "(RFC822)")
                    msg = email.message_from_bytes(
                        msg_data[0][1], policy=email.policy.default)
                    from_addr = str(msg.get("From", ""))
                    platform = _sender_platform(from_addr, cfg.sender_platform_map)
                    if platform is None:      # unknown sender in folder -> ignore
                        store.mark_email_uid(uid_s)
                        continue
                    body = _plain_text(msg)
                    offer = parse_offer(str(msg.get("Subject", "")), body)
                    offer.update(
                        platform_key=platform,
                        url=_platform_link(body, _sender_domain(from_addr)),
                    )
                    out.append(offer)
                    store.mark_email_uid(uid_s)
        finally:
            store.close()
        return out
