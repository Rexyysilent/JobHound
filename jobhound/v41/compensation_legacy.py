"""Unit-preserving compensation claims; no implicit labor-hour conversion."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CompensationClaim:
    currency: str | None
    amount_low: float | None
    amount_high: float | None
    basis: str
    qualifier: str | None = None
    guaranteed_minimum: float | None = None
    labor_hour_equivalent: float | None = None
    labor_equivalent_is_estimate: bool = False
    raw: str = ""


_MONEY = re.compile(r"(?P<currency>USD|INR|EUR|GBP|CAD|AUD|\$|₹|€|£)\s*(?P<low>\d[\d,]*(?:\.\d+)?)(?:\s*(?:-|–|—|to)\s*(?:USD|INR|EUR|GBP|CAD|AUD|\$|₹|€|£)?\s*(?P<high>\d[\d,]*(?:\.\d+)?))?", re.I)
_CURRENCY = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP"}


def parse_compensation(raw: str, *, labor_hours_per_unit: float | None = None) -> CompensationClaim:
    text = raw or ""
    match = _MONEY.search(text)
    if not match:
        return CompensationClaim(None, None, None, "unknown", raw=text)
    currency = _CURRENCY.get(match.group("currency"), match.group("currency").upper())
    low = float(match.group("low").replace(",", ""))
    high = float(match.group("high").replace(",", "")) if match.group("high") else low
    folded = text.casefold()
    if re.search(r"\b(?:recorded|audio|finished|transcribed)\s*(?:audio\s*)?hours?\b|per\s+(?:recorded|audio|finished|transcribed)\s*(?:audio\s*)?hour", folded):
        basis = "output_audio_hour"
    elif re.search(r"\bper\s+(?:recorded|audio|finished|transcribed)\s*(?:audio\s*)?minute\b", folded):
        basis = "output_audio_minute"
    elif re.search(r"\b(?:working|labor|clock)\s+hours?\b|/\s*(?:hr|hour)\b|\bper\s+hour\b|\bhourly\b", folded):
        basis = "labor_hour"
    elif re.search(r"\bper\s+(?:paid\s+)?task[- ]?hours?\b", folded):
        basis = "task_hour"
    elif re.search(r"\b(?:fixed[- ]price|fixed\s+project|project\s+budget|fixed\s+budget|for\s+the\s+(?:whole\s+)?project)\b", folded) or re.search(r"\d\s+fixed\b", folded):
        basis = "fixed_project"
    elif re.search(r"\bper\s+(?:task|item|response|image)\b", folded):
        basis = "output_item"
    elif re.search(r"\b(?:year|annum|annual|salary)\b", folded):
        basis = "year"
    else:
        basis = "unknown"
    is_equivalent = bool(re.search(
        r"(?:/\s*(?:hr|hour)|\b(?:hourly|per\s+(?:working\s+)?hour))\s+equivalent\b|"
        r"\bequivalent\s+(?:hourly|per\s+(?:working\s+)?hour|/\s*(?:hr|hour))\b",
        folded,
    ))
    is_up_to = bool(re.search(r"\bup\s+to\b", folded))
    qualifier = "up_to_equivalent" if is_up_to and is_equivalent else (
        "equivalent" if is_equivalent else ("up_to" if is_up_to else None)
    )
    guaranteed = low if (
        re.search(r"\b(?:guaranteed|minimum)\b", folded)
        and not re.search(r"\b(?:not|isn't|is\s+not|no)\s+guaranteed\b", folded)
        and not is_up_to
    ) else None
    equivalent = None
    estimated = False
    if basis.startswith("output_") and labor_hours_per_unit and labor_hours_per_unit > 0:
        equivalent = low / labor_hours_per_unit
        estimated = True
    elif basis == "labor_hour" and not is_equivalent:
        equivalent = low
    if basis == "labor_hour" and is_equivalent:
        # The publisher supplied an estimate normalized to an hour; it is not
        # evidence of pay for an actual clock/labor hour.
        basis = "labor_hour_equivalent"
        equivalent = low
        estimated = True
    return CompensationClaim(currency, low, high, basis, qualifier, guaranteed, equivalent, estimated, text)
