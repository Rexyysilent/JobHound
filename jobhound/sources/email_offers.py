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
The From header is spoofable, so platform trust also requires the receiving
provider's topmost Authentication-Results to show DMARC or aligned DKIM pass
for the configured platform domain; unauthenticated mail is ignored.

NOTE: like the rest of the pipeline, a --dry-run still marks UIDs processed.
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import imaplib
import logging
import re
import ssl
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
    return email.utils.parseaddr(from_addr)[1].rpartition("@")[2].lower().strip()


def _within(host: str, domain: str) -> bool:
    host, domain = host.lower().rstrip("."), domain.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def _match_sender(from_addr: str, mapping: dict[str, str]) -> tuple[str, str] | None:
    """-> (platform key, configured platform domain) for a mapped sender."""
    dom = _sender_domain(from_addr)
    for known, key in mapping.items():
        if dom and _within(dom, known):
            return key, known.lower()
    return None


_RESULT_RE = re.compile(r"\b(dmarc|dkim)\s*=\s*(\w+)([^;]*)", re.I)
_PROP_RE = re.compile(r"\bheader\.(from|d|i)\s*=\s*@?([A-Za-z0-9.-]+)", re.I)


def _sender_authenticated(msg, platform_domain: str, trusted: tuple[str, ...] | list[str]) -> bool:
    """DMARC pass or aligned DKIM pass, read from the TOPMOST
    Authentication-Results header (the one the receiving provider added; a
    sender can plant others further down) and only if a trusted receiving
    server wrote it."""
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return False
    top = re.sub(r"\s+", " ", str(headers[0])).strip()
    authserv_id = top.split(";", 1)[0].split()[0].lower() if top else ""
    if authserv_id not in {t.lower() for t in trusted}:
        return False
    for method, result, props in _RESULT_RE.findall(top):
        if result.lower() != "pass":
            continue
        for name, value in _PROP_RE.findall(props):
            if method.lower() == "dmarc" and name.lower() == "from" and _within(value, platform_domain):
                return True
            if method.lower() == "dkim" and name.lower() in {"d", "i"} and _within(value, platform_domain):
                return True
    return False


def _platform_link(body: str, platform_domain: str) -> str:
    """First link on the configured platform domain (or a subdomain); else its
    homepage. (HANDOFF §10: we link to originals — never to anything else in
    the mail.) The configured domain is used as-is: taking the sender's last
    two labels turned alerts.example.co.uk into "co.uk"."""
    for m in _LINK_RE.finditer(body):
        try:
            host = m.group(0).split("/")[2].lower().split(":")[0]
        except IndexError:
            continue
        if _within(host, platform_domain):
            return m.group(0)
    return f"https://{platform_domain}"


def offer_from_message(msg, mapping: dict[str, str], *, require_auth: bool,
                       trusted: tuple[str, ...] | list[str]) -> tuple[dict | None, str]:
    """One message -> (offer or None, reason: accepted / unknown_sender / unauthenticated)."""
    matched = _match_sender(str(msg.get("From", "")), mapping)
    if matched is None:
        return None, "unknown_sender"
    platform, platform_domain = matched
    if require_auth and not _sender_authenticated(msg, platform_domain, trusted):
        return None, "unauthenticated"
    body = _plain_text(msg)
    offer = parse_offer(str(msg.get("Subject", "")), body)
    offer.update(platform_key=platform, url=_platform_link(body, platform_domain))
    return offer, "accepted"


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
            # imaplib's default context verifies neither the certificate nor
            # the hostname, and login sends the mailbox app password.
            with imaplib.IMAP4_SSL(cfg.host, ssl_context=ssl.create_default_context()) as imap:
                imap.login(settings.imap_user_effective, settings.imap_password_effective)
                typ, _ = imap.select(f'"{cfg.folder}"', readonly=True)
                if typ != "OK":
                    log.warning(
                        "email_ingest: Gmail label %r not found — create it plus "
                        "a filter routing platform senders into it (README)",
                        cfg.folder)
                    return out
                _, data = imap.uid("search", None, "ALL")
                unauthenticated = 0
                for uid in (data[0] or b"").split():
                    uid_s = uid.decode()
                    if store.email_uid_seen(uid_s):
                        continue
                    _, msg_data = imap.uid("fetch", uid, "(RFC822)")
                    msg = email.message_from_bytes(
                        msg_data[0][1], policy=email.policy.default)
                    offer, reason = offer_from_message(
                        msg, cfg.sender_platform_map,
                        require_auth=cfg.require_sender_auth,
                        trusted=cfg.trusted_authserv_ids,
                    )
                    if offer is not None:
                        out.append(offer)
                    elif reason == "unauthenticated":
                        unauthenticated += 1
                    store.mark_email_uid(uid_s)
                if unauthenticated:
                    log.warning(
                        "email_ingest: ignored %d message(s) claiming a platform sender "
                        "without DMARC/aligned-DKIM pass (possible spoofing)", unauthenticated)
        finally:
            store.close()
        return out
