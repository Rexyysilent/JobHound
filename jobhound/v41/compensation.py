"""Conservative single-claim compensation parser.

Native amounts/units are preserved. This is not an earnings predictor. A string
containing several money claims must be segmented by its caller, with source
spans retained, rather than combining one amount with another claim's unit.
"""
from __future__ import annotations

import math
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
    warnings: tuple[str, ...] = ()
    literal_unit: str = "unknown"


_CUR = r"(?:USD|INR|EUR|GBP|CAD|AUD|US\$|AU\$|C\$|A\$|\$|₹|€|£)"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_SCALE = r"(?:lakh|lakhs|lac|lacs|lpa|million|k|m)\b"
_MONEY = re.compile(
    rf"(?<!\w)(?P<currency>{_CUR})\s*(?P<low>{_NUM})\s*(?P<ls>{_SCALE})?"
    rf"(?:\s*(?:-|–|—|to)\s*(?P<currency2>{_CUR})?\s*"
    rf"(?P<high>{_NUM})\s*(?P<hs>{_SCALE})?)?", re.I,
)
_CURRENCY = {"$": "USD", "US$": "USD", "C$": "CAD", "A$": "AUD", "AU$": "AUD",
             "₹": "INR", "€": "EUR", "£": "GBP"}
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



_SUFFIX_MONEY = re.compile(
    rf"(?<![\w.,])(?P<low>{_NUM})\s*(?P<ls>{_SCALE})?"
    rf"(?:\s*(?:-|–|—|to)\s*(?P<high>{_NUM})\s*(?P<hs>{_SCALE})?)?"
    rf"\s*(?P<currency>{_CUR})(?!\w)", re.I,
)
_CODES = re.compile(r"\b(?:USD|INR|EUR|GBP|CAD|AUD)\b", re.I)
# Unit matching is adjacent to the monetary expression, not a bag of words
# over a paragraph. Workload and payroll cadence are not wage denominators.
_UNIT = re.compile(
    r"^\s*(?:(?:gross|net|base)\s+)?(?:"
    r"(?P<audio>per\s+(?:accepted\s+|paid\s+)?(?:recorded|audio|finished|transcribed)\s+(?:audio\s+)?(?:hours?|minutes?))|"
    r"(?P<taskhour>per\s+(?:paid\s+)?task[- ]?hours?)|"
    r"(?P<item>per\s+(?:accepted\s+)?(?:task|item|response|image|word)|/\s*(?:task|item|response|image|word))|"
    r"(?P<fixed>fixed(?:[- ]price|\s+project)?|for\s+the\s+(?:whole\s+)?project|project\s+budget)|"
    r"(?P<period>(?:/\s*|per\s+|a\s+)(?:working\s+|labor\s+|clock\s+)?(?:hrs?|hours?|h|yrs?|years?|annum|months?|mos?|weeks?|days?)|hourly|monthly|weekly|daily|annually|annual|yearly)"
    r")(?!\w)", re.I,
)
_PREFIX_UNIT = re.compile(
    r"\b(?P<unit>hourly|monthly|weekly|daily|annual|annually|yearly|project\s+budget|fixed\s+budget|fixed[- ]price)"
    r"\s*(?:pay|rate|salary|wage|budget)?\s*(?:is|of|:|=)?\s*$", re.I,
)


def _unit_name(token: str) -> str:
    token = token.casefold()
    if re.search(r"\b(?:hourly|hrs?|hours?|h)\b", token):
        return "labor_hour"
    if re.search(r"\b(?:monthly|months?|mos?)\b", token):
        return "month"
    if re.search(r"\b(?:weekly|weeks?)\b", token):
        return "week"
    if re.search(r"\b(?:daily|days?)\b", token):
        return "day"
    if re.search(r"\b(?:yearly|annual|annually|annum|yrs?|years?)\b", token):
        return "year"
    return "fixed_project"


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
    suffix_matches = list(_SUFFIX_MONEY.finditer(raw))
    for candidate in suffix_matches:
        if not any(candidate.start() < existing.end() and existing.start() < candidate.end()
                   for existing in matches):
            matches.append(candidate)
    matches.sort(key=lambda item: item.start())
    if not matches:
        return _unknown(raw, "no_supported_money_expression")
    if len(matches) != 1:
        return _unknown(raw, "multiple_money_claims_require_segmentation")
    match = matches[0]
    currency = _CURRENCY.get(match["currency"].upper(), match["currency"].upper())
    second = match.groupdict().get("currency2")
    if second and _CURRENCY.get(second.upper(), second.upper()) != currency:
        return _unknown(raw, "mixed_currency_range")
    if re.search(r"[-−]\s*$", raw[:match.start()]):
        return _unknown(raw, "negative_or_ambiguous_amount")
    # Do not accept a truncated numeric token (e.g. 1.234,56 or 1e6).
    number_field = "hs" if match["hs"] else ("high" if match["high"] else ("ls" if match["ls"] else "low"))
    if match.re is _MONEY and re.match(r"[.,]\d|[A-Za-z0-9]", raw[match.end(number_field):]):
        return _unknown(raw, "unsupported_number_format")
    try:
        low_scale, high_scale = match["ls"], match["hs"]
        if match["high"]:
            if not low_scale and high_scale and _number(match["low"], None) < 1000:
                low_scale = high_scale
            if not high_scale and low_scale and _number(match["high"], None) < 1000:
                high_scale = low_scale
        low = _number(match["low"], low_scale)
        high = _number(match["high"], high_scale) if match["high"] else low
    except ValueError as exc:
        return _unknown(raw, str(exc))
    if low > high:
        return _unknown(raw, "reversed_range")
    start = max((m.end() for m in _BOUNDARY.finditer(raw, 0, match.start())), default=0)
    end_match = _BOUNDARY.search(raw, match.end())
    end = end_match.start() if end_match else len(raw)
    clause = raw[start:end].casefold()
    prefix, suffix = raw[start:match.start()], raw[match.end():end]
    codes = {code.upper() for code in _CODES.findall(raw[start:end])}
    if match["currency"] == "$" and len(codes) == 1:
        currency = next(iter(codes))
    elif len(codes - {currency}) > 0:
        return _unknown(raw, "conflicting_currency_context")
    # A currency code immediately after a prefix-money expression is explicit
    # evidence, not an extra amount. Remove it only from the unit-matching copy.
    suffix = re.sub(r"^\s*(?:USD|INR|EUR|GBP|CAD|AUD)\b", "", suffix, flags=re.I)
    unit = _UNIT.match(suffix)
    prefix_unit = _PREFIX_UNIT.search(prefix)
    basis = _unit_name(prefix_unit["unit"]) if prefix_unit else "unknown"
    if (match["ls"] or match["hs"] or "").casefold() == "lpa":
        basis = "year"
    if unit:
        if unit["audio"]:
            adjacent = "output_audio_minute" if "minute" in unit["audio"].casefold() else "output_audio_hour"
        elif unit["taskhour"]:
            adjacent = "task_hour"
        elif unit["item"]:
            adjacent = "output_item"
        elif unit["fixed"]:
            adjacent = "fixed_project"
        else:
            adjacent = _unit_name(unit["period"])
        if basis != "unknown" and basis != adjacent:
            return _unknown(raw, "conflicting_periods_require_segmentation")
        basis = adjacent
        remaining = suffix[unit.end():]
        # Alternative wage denominators differ from "paid monthly" payroll.
        alternative = re.match(r"\s*(?:or|alternatively|/)\s*(.*)", remaining, re.I)
        if alternative and _UNIT.match(alternative[1]):
            return _unknown(raw, "conflicting_periods_require_segmentation")
    equivalent = bool(re.search(
        r"(?:/\s*(?:hr|hour)|\b(?:hourly|per\s+(?:working\s+)?hour))\s+equivalent\b|"
        r"\bequivalent\s+(?:hourly|per\s+(?:working\s+)?hour|/\s*(?:hr|hour))\b", clause)
        or re.search(r"\bequivalent\s+(?:of\s+)?(?:up\s+to\s+)?$", prefix, re.I))
    up_to = bool(re.search(r"\bup\s+to\s*$", prefix, re.I))
    qualifier = "up_to_equivalent" if up_to and equivalent else (
        "equivalent" if equivalent else ("up_to" if up_to else None))
    guaranteed = low if not up_to and re.search(
        r"\b(?:guaranteed(?:\s+(?:minimum|base|pay|rate))?|minimum\s+(?:pay|rate|wage|salary))\s*(?:of|is|:)?\s*$", prefix, re.I
    ) and not re.search(r"\b(?:not|never|no|isn't|is\s+not)\s+guaranteed", prefix, re.I) else None
    hourly, estimated = None, False
    if basis.startswith("output_") and labor_hours_per_unit is not None:
        hourly, estimated = low / labor_hours_per_unit, True
    elif basis == "labor_hour":
        hourly = low
        if equivalent:
            basis, estimated = "labor_hour_equivalent", True
    warnings = ("bare_dollar_assumed_usd_legacy_contract",) if match["currency"] == "$" and not codes else ()
    literal_unit = basis
    if unit and unit["item"]:
        literal_unit = "per_" + unit["item"].strip().split()[-1].lstrip("/").casefold()
    return CompensationClaim(currency, low, high, basis, qualifier, guaranteed,
                             hourly, estimated, raw, warnings, literal_unit)
