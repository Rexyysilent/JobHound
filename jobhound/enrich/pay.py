"""Pay normalization — the sweatshop gate. Everything → $/hour.

Pay arrives as $/yr, $/hr, ₹/month, per-task/word/audio-min, or nothing. We
parse → convert currency to USD → normalize to hourly. Piece-rates are *flagged*
and only roughly estimated from config throughput assumptions. Unknown pay is
NOT rejected (pay_ok=None → down-ranked); below-floor pay is.
"""
from __future__ import annotations

import re

from ..config import CONFIG, PayCfg
from ..models import Job
from ..text import normalize_match

_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")

# basis keyword → canonical basis
_BASIS_PATTERNS = [
    ("audio_min", re.compile(r"audio\s*min|per\s*audio|/\s*audio", re.I)),
    ("word", re.compile(r"per\s*word|/\s*word|\bword\b", re.I)),
    ("image", re.compile(r"per\s*image|/\s*image|\bimage\b", re.I)),
    ("task", re.compile(r"per\s*task|/\s*task|\btask\b|per\s*hit", re.I)),
    ("hour", re.compile(r"per\s*hour|/\s*hr\b|/\s*hour|hourly|\ban?\s*hour\b|\bhr\b", re.I)),
    ("month", re.compile(r"per\s*month|/\s*mo\b|/\s*month|monthly|\bmonth\b", re.I)),
    ("year", re.compile(r"per\s*year|/\s*yr\b|/\s*year|yearly|annual|per\s*annum|p\.?a\.?\b", re.I)),
]

_PIECE = {"task", "word", "audio_min", "image"}

# --- title pay (v3.5 item 2) -------------------------------------------------
# HumaniTapp posted a valid "$120–$170/hr" range in its TITLE and rendered as
# "pay unknown" because the parser only ever read job.pay_raw. This is a
# deliberately high-precision fallback: BOTH an explicit currency AND an
# explicit hourly basis must be present. Description bodies are NOT scanned —
# arbitrary numbers in boilerplate are not compensation.
_CUR = r"(?:\$|₹|€|£|\bUSD\b|\bINR\b|\bEUR\b|\bGBP\b|\bRs\.?)"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_HOURLY = r"(?:/\s*(?:hr|hour)\b|per\s+hour\b|hourly\b|an?\s+hour\b)"

# Ranges first — "$120-$170/hr", "$120 to $170 per hour", "USD 120-170/hour".
# Input is dash-folded by normalize_match, so "-" covers en and em dashes.
_TITLE_PAY_RANGE = re.compile(
    rf"(?:\bup\s+to\s+)?{_CUR}\s*{_NUM}\s*(?:-|to)\s*"
    rf"(?:{_CUR}\s*)?{_NUM}\s*{_HOURLY}",
    re.IGNORECASE,
)
_TITLE_PAY_SUFFIX_RANGE = re.compile(
    rf"{_NUM}\s*(?:-|to)\s*{_NUM}\s*{_CUR}\s*{_HOURLY}",
    re.IGNORECASE,
)
_TITLE_PAY_SINGLE = re.compile(
    rf"(?:\bup\s+to\s+)?{_CUR}\s*{_NUM}\s*{_HOURLY}",
    re.IGNORECASE,
)


def parse_title_pay(
    title: str | None, cfg: PayCfg
) -> tuple[float, float, str, bool, str] | None:
    """Explicit hourly pay stated in a title → (lo, hi, basis, est, source_text).

    Returns None unless the title states a currency and an hourly basis and the
    result actually parses to an hourly rate.
    """
    if not title:
        return None
    folded = normalize_match(title)
    for pattern in (_TITLE_PAY_RANGE, _TITLE_PAY_SUFFIX_RANGE, _TITLE_PAY_SINGLE):
        match = pattern.search(folded)
        if match is None:
            continue
        source_text = match.group(0).strip()
        lo, hi, basis, est = parse_pay(source_text, cfg)
        # Guard the reuse: only an explicitly hourly parse counts here.
        if basis != "hour" or (lo is None and hi is None):
            continue
        return lo, hi, basis, est, source_text
    return None


def _detect_currency(s: str) -> str:
    low = s.lower()
    if "₹" in s or "inr" in low or re.search(r"\brs\.?\b", low):
        return "INR"
    if "€" in s or "eur" in low:
        return "EUR"
    if "£" in s or "gbp" in low:
        return "GBP"
    if "cad" in low or "c$" in low:
        return "CAD"
    if "aud" in low or "a$" in low:
        return "AUD"
    return "USD"  # bare numbers on these remote boards are overwhelmingly USD


def _detect_basis(s: str) -> str:
    for basis, pat in _BASIS_PATTERNS:
        if pat.search(s):
            return basis
    return "unknown"


def _extract_amounts(s: str) -> list[float]:
    out: list[float] = []
    for m in _AMOUNT_RE.finditer(s):
        raw = m.group(1).replace(",", "")
        if not raw or raw == ".":
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        if m.group(2):  # k suffix
            val *= 1000
        if val > 0:
            out.append(val)
    return out


def parse_pay(pay_raw: str | None, cfg: PayCfg) -> tuple[float | None, float | None, str, bool]:
    """Return (min_hourly_usd, max_hourly_usd, basis, is_estimate).

    basis "unknown" with None hourly means: pay not determinable → down-rank.
    """
    if not pay_raw or not pay_raw.strip():
        return None, None, "unknown", False

    s = pay_raw.strip()
    currency = _detect_currency(s)
    fx = cfg.fx_to_usd.get(currency, 1.0)
    basis = _detect_basis(s)
    amounts = _extract_amounts(s)
    if not amounts:
        return None, None, basis, False

    lo_usd = min(amounts) * fx
    hi_usd = max(amounts) * fx

    # Infer basis from magnitude only when no explicit period was stated.
    if basis == "unknown":
        if hi_usd >= 2000:
            basis = "year"
        elif hi_usd <= 200:
            basis = "hour"
        else:
            return None, None, "unknown", False  # ambiguous → treat as unknown

    is_estimate = False
    if basis == "hour":
        lo_h, hi_h = lo_usd, hi_usd
    elif basis == "year":
        lo_h, hi_h = lo_usd / cfg.hours_per_year, hi_usd / cfg.hours_per_year
    elif basis == "month":
        lo_h, hi_h = lo_usd / cfg.hours_per_month, hi_usd / cfg.hours_per_month
    elif basis in _PIECE:
        rate = cfg.piece_rate_per_hour.get(basis, 0)
        if not rate:
            return None, None, basis, False
        lo_h, hi_h = lo_usd * rate, hi_usd * rate
        is_estimate = True
    else:
        return None, None, "unknown", False

    return round(lo_h, 2), round(hi_h, 2), basis, is_estimate


def enrich_pay(job: Job, cfg: PayCfg = CONFIG.pay) -> Job:
    """Normalization only. The floor decision moved to pay_gate.apply_pay_gate
    (v3 Patch #2): it runs after the trust join because the floor now applies
    to the EFFECTIVE rate (nominal × availability × (1 − unpaid overhead)).

    v3.5: records pay_source provenance and falls back to an explicit hourly
    expression in the title when the structured field is absent or unparseable.
    """
    lo, hi, basis, est = parse_pay(job.pay_raw, cfg)
    if lo is not None or hi is not None:
        # Structured/provider pay stays the first choice.
        job.pay_source = "email" if job.source.startswith("email:") else "structured"
    else:
        from_title = parse_title_pay(job.title, cfg)
        if from_title is not None:
            lo, hi, basis, est, source_text = from_title
            job.pay_source = "title"
            # Keep the text the numbers came from: pay_raw is the audit trail,
            # and an unparseable provider string ("Competitive") carried none.
            job.pay_raw = source_text
            job.reasons.append(f"pay_from_title: {source_text}")
        else:
            job.pay_source = "unknown"
    job.pay_min_hourly_usd = lo
    job.pay_max_hourly_usd = hi
    job.pay_basis = basis
    job.pay_is_estimate = est
    return job
