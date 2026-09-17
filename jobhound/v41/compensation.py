"""Conservative single-claim compensation parser.

Native amounts/units are preserved. This is not an earnings predictor. A string
containing several money claims must be segmented by its caller, with source
spans retained, rather than combining one amount with another claim's unit.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace


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
    warnings: tuple[str, ...] = ()


_CUR = r"(?:USD|INR|EUR|GBP|CAD|AUD|\$|₹|€|£)"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_SCALE = r"(?:lakh|lakhs|lac|lacs|lpa|million|k|m)\b"
_MONEY = re.compile(
    rf"(?<!\w)(?P<currency>{_CUR})\s*(?P<low>{_NUM})\s*(?P<ls>{_SCALE})?"
    rf"(?:\s*(?:-|–|—|to)\s*(?P<currency2>{_CUR})?\s*"
    rf"(?P<high>{_NUM})\s*(?P<hs>{_SCALE})?)?", re.I,
)
_MONEY_SUFFIX = re.compile(
    rf"(?<![\w,])(?P<low>{_NUM})\s*(?P<ls>{_SCALE})?"
    rf"(?:\s*(?:-|–|—|to)\s*(?P<high>{_NUM})\s*(?P<hs>{_SCALE})?)?"
    rf"\s*(?P<currency>USD|INR|EUR|GBP|CAD|AUD)\b", re.I,
)
_CURRENCY = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP"}
_SCALES = {"k": 1000, "m": 1_000_000, "million": 1_000_000,
           "lakh": 100_000, "lakhs": 100_000, "lac": 100_000,
           "lacs": 100_000, "lpa": 100_000}
_BOUNDARY = re.compile(r"[;!?\n]|\.(?=\s|$)")


def _unknown(raw: str, reason: str) -> CompensationClaim:
    return CompensationClaim(None, None, None, "unknown", raw=raw, warnings=(reason,))


def _number(raw: str, scale: str | None) -> float:
    # Reject decimal-comma ambiguity rather than silently treating EUR 12,50 as 1250.
    integer = raw.split(".", 1)[0]
    if "," in integer and not (
        re.fullmatch(r"\d{1,3}(?:,\d{3})+", integer)
        or re.fullmatch(r"\d{1,2}(?:,\d{2})*,\d{3}", integer)
    ):
        raise ValueError("ambiguous_number_grouping")
    value = float(raw.replace(",", "")) * _SCALES.get((scale or "").lower(), 1)
    if not math.isfinite(value):
        raise ValueError("nonfinite_amount")
    return value


def parse_compensation(raw: str, *, labor_hours_per_unit: float | None = None) -> CompensationClaim:
    if not isinstance(raw, str):
        raise TypeError("compensation must be text")
    if len(raw) > 32_768:
        return _unknown(raw, "claim_text_too_long")
    if labor_hours_per_unit is not None and (
        isinstance(labor_hours_per_unit, bool)
        or not isinstance(labor_hours_per_unit, (int, float))
        or not math.isfinite(labor_hours_per_unit)
        or labor_hours_per_unit <= 0
    ):
        raise ValueError("labor_hours_per_unit must be finite and positive")
    matches = list(_MONEY.finditer(raw))
    if not matches:
        suffixes = list(_MONEY_SUFFIX.finditer(raw))
        if len(suffixes) == 1:
            suffix = suffixes[0]
            numbers = raw[suffix.start():suffix.start("currency")].strip()
            parsing_copy = (raw[:suffix.start()] + suffix["currency"] + " "
                            + numbers + raw[suffix.end():])
            return replace(parse_compensation(parsing_copy,
                           labor_hours_per_unit=labor_hours_per_unit), raw=raw)
        return _unknown(raw, "multiple_money_claims_require_segmentation" if suffixes
                        else "no_supported_money_expression")
    if len(matches) != 1:
        return _unknown(raw, "multiple_money_claims_require_segmentation")
    match = matches[0]
    currency = _CURRENCY.get(match["currency"], match["currency"].upper())
    second = match["currency2"]
    if second and _CURRENCY.get(second, second.upper()) != currency:
        return _unknown(raw, "mixed_currency_range")
    if re.search(r"[-−]\s*$", raw[:match.start()]):
        return _unknown(raw, "negative_or_ambiguous_amount")
    try:
        low = _number(match["low"], match["ls"] or match["hs"])
        high = _number(match["high"], match["hs"] or match["ls"]) if match["high"] else low
    except ValueError as exc:
        return _unknown(raw, str(exc))
    if low > high:
        return _unknown(raw, "reversed_range")

    # Examine the containing clause only. Numeric decimal points are not boundaries.
    start = max((m.end() for m in _BOUNDARY.finditer(raw, 0, match.start())), default=0)
    end_match = _BOUNDARY.search(raw, match.end())
    end = end_match.start() if end_match else len(raw)
    clause = raw[start:end].casefold()
    if re.search(r"\b(?:recorded|audio|finished|transcribed)\s*(?:audio\s*)?hours?\b", clause):
        basis = "output_audio_hour"
    elif re.search(r"\b(?:recorded|audio|finished|transcribed)\s*(?:audio\s*)?minutes?\b", clause):
        basis = "output_audio_minute"
    elif re.search(r"\bper\s+(?:paid\s+)?task[- ]?hours?\b", clause):
        basis = "task_hour"
    elif re.search(r"\bper\s+(?:accepted\s+)?(?:task|item|response|image|word)\b", clause):
        basis = "output_item"
    elif re.search(r"\b(?:fixed[- ]price|fixed\s+project|project\s+budget|fixed\s+budget|for\s+the\s+(?:whole\s+)?project)\b|\d\s+fixed\b", clause):
        basis = "fixed_project"
    elif re.search(r"\b(?:working|labor|clock)\s+hours?\b|/\s*(?:hr|hour)\b|\bper\s+hour\b|\bhourly\b", clause):
        basis = "labor_hour"
    elif re.search(r"\b(?:monthly|per\s+month|a\s+month)\b|/\s*(?:month|mo)\b", clause):
        basis = "month"
    elif re.search(r"\b(?:weekly|per\s+week|a\s+week)\b|/\s*week\b", clause):
        basis = "week"
    elif re.search(r"\b(?:daily|per\s+day|a\s+day)\b|/\s*day\b", clause):
        basis = "day"
    elif re.search(r"\b(?:year|yearly|annum|annual|annually|lpa)\b|/\s*yr\b", clause):
        basis = "year"
    else:
        basis = "unknown"
    # A single monetary expression with conflicting period labels is not safe.
    periods = sum(bool(re.search(pattern, clause)) for pattern in (
        r"\b(?:monthly|per\s+month)\b", r"\b(?:weekly|per\s+week)\b",
        r"\b(?:daily|per\s+day)\b", r"\b(?:annual|annually|per\s+year)\b",
        r"\b(?:hourly|per\s+hour)\b|/\s*(?:hr|hour)\b",
    ))
    if periods > 1:
        return _unknown(raw, "conflicting_periods_require_segmentation")
    equivalent = bool(re.search(
        r"(?:/\s*(?:hr|hour)|\b(?:hourly|per\s+(?:working\s+)?hour))\s+equivalent\b|"
        r"\bequivalent\s+(?:hourly|per\s+(?:working\s+)?hour|/\s*(?:hr|hour))\b", clause))
    up_to = bool(re.search(r"\bup\s+to\b", clause))
    qualifier = "up_to_equivalent" if up_to and equivalent else (
        "equivalent" if equivalent else ("up_to" if up_to else None))
    prefix = raw[start:match.start()].casefold()
    guaranteed = low if not up_to and re.search(
        r"\b(?:guaranteed(?:\s+(?:minimum|base|pay|rate))?|minimum\s+(?:pay|rate|wage|salary))\s*(?:of|is|:)?\s*$", prefix
    ) and not re.search(r"\b(?:not|never|no|isn't|is\s+not)\s+guaranteed", prefix) else None
    hourly = None
    estimated = False
    if basis.startswith("output_") and labor_hours_per_unit is not None:
        hourly, estimated = low / labor_hours_per_unit, True
    elif basis == "labor_hour":
        hourly = low
        if equivalent:
            basis, estimated = "labor_hour_equivalent", True
    warnings = ("bare_dollar_assumed_usd_legacy_contract",) if match["currency"] == "$" else ()
    return CompensationClaim(currency, low, high, basis, qualifier, guaranteed,
                             hourly, estimated, raw, warnings)
