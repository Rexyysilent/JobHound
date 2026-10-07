"""Section, concept and provenance-bearing pay extraction for V4.1."""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..config import CONFIG
from ..enrich.pay import parse_pay, parse_title_pay
from ..filters.eligibility import Profile
from ..filters.matching import phrase_in_text
from ..text import normalize_match, fold_dashes
from .models import CanonicalJob, PayCandidate, SourceKind
from .compensation import parse_compensation, money_expression_spans
from .compensation_legacy import parse_compensation as parse_compensation_legacy

_HEADING_CATEGORIES = {
    "role": (
        "about the role",
        "about role",
        "role overview",
        "who should apply",
        "what success looks like",
    ),
    "responsibilities": (
        "what you'll do",
        "responsibilities",
        "responsibility",
        "duties",
        "tasks",
    ),
    "requirements": (
        "requirements",
        "requirement",
        "qualifications",
        "qualification",
        "what we're looking for",
        "what we are looking for",
        "must-haves",
        "must haves",
        "ideal candidate",
        "who you are",
        "what you will need",
        "your profile",
        "about you",
        "skills",
        "experience",
        "minimum qualifications",
        "preferred qualifications",
        "essential criteria",
    ),
    "ignore": (
        "benefits",
        "about us",
        "about company",
        "about the company",
        "company overview",
    ),
}
_HEADING_LOOKUP = {
    heading.casefold(): category
    for category, headings in _HEADING_CATEGORIES.items()
    for heading in headings
}
_HEADING_NAMES = sorted(_HEADING_LOOKUP, key=len, reverse=True)
_HEADING_PATTERN = "|".join(
    re.escape(heading).replace(r"\ ", r"\s+") for heading in _HEADING_NAMES
)
# A heading must begin the text or a new sentence/line. This recognizes legacy
# inline feed prose without treating "our responsibilities include" as a break.
_HEADING = re.compile(
    rf"(?im)(?:^|(?<=[\n.!?]))[ \t]*(?P<heading>{_HEADING_PATTERN})"
    rf"(?=[ \t]*(?:[:.\-–—]|\n|$)|[ \t]+\S)",
)
_CURRENCY = re.compile(r"(?:\$|₹|€|£|\bUSD\b|\bINR\b|\bEUR\b|\bGBP\b|\bCAD\b|\bAUD\b)", re.I)
_HOURLY = re.compile(r"(?:/\s*(?:hr|hour)\b|per\s+hour\b|hourly\b)", re.I)
_PAY_EXPRESSION = re.compile(
    r"(?ix)"
    r"(?:\b(?:compensation|salary|pay(?:\s+rate)?|hourly\s+rate|earnings?)"
    r"\s*(?:range)?\s*(?:is|of|:|-)?\s*)?"
    r"(?:\bup\s+to\s+)?"
    r"(?:\$|₹|€|£|\bUSD\b|\bINR\b|\bEUR\b|\bGBP\b|\bCAD\b|\bAUD\b)"
    r"\s*\d[\d,]*(?:\.\d+)?(?:\s*[kK])?"
    r"(?:\s*(?:-|to)\s*"
    r"(?:(?:\$|₹|€|£|\bUSD\b|\bINR\b|\bEUR\b|\bGBP\b|\bCAD\b|\bAUD\b)\s*)?"
    r"\d[\d,]*(?:\.\d+)?(?:\s*[kK])?)?"
    r"\s*(?:/\s*(?:hr|hour|yr|year|mo|month)\b|"
    r"per\s+(?:hour|year|month|annum)\b|hourly\b|monthly\b|annually\b|annual\b)"
    r"(?:\s+equivalent)?(?:\s+depending\s+on\s+the\s+project)?"
)
_PAY_SUFFIX_EXPRESSION = re.compile(
    r"(?ix)"
    r"\d[\d,]*(?:\.\d+)?(?:\s*[kK])?"
    r"\s*(?:-|to)\s*"
    r"\d[\d,]*(?:\.\d+)?(?:\s*[kK])?"
    r"\s*(?:\$|₹|€|£|\bUSD\b|\bINR\b|\bEUR\b|\bGBP\b|\bCAD\b|\bAUD\b)"
    r"\s*(?:/\s*(?:hr|hour|yr|year|mo|month)\b|"
    r"per\s+(?:hour|year|month|annum)\b|hourly\b|monthly\b|annually\b|annual\b)"
)
_FIXED_PRICE_PATTERNS = (
    re.compile(
        r"(?ix)\b(?:fixed[- ]price|fixed|project\s+budget|budget)"
        r"\s*(?:is|of|:|-)?\s*"
        r"(?P<currency>\$|\u20b9|\u20ac|\u00a3|USD|INR|EUR|GBP|CAD|AUD)"
        r"\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\b"
    ),
    re.compile(
        r"(?ix)(?P<currency>\$|\u20b9|\u20ac|\u00a3|USD|INR|EUR|GBP|CAD|AUD)"
        r"\s*(?P<amount>\d[\d,]*(?:\.\d+)?)"
        r"\s*(?:fixed(?:[- ]price|\s+project)?|project\s+budget)\b"
    ),
)
_MARKETPLACE_BARE_BUDGET = re.compile(
    r"(?ix)\bfreelance\s+job\b.{0,120}?\s[-:]\s*"
    r"(?P<currency>\$|₹|€|£|USD|INR|EUR|GBP|CAD|AUD)"
    r"\s*(?P<amount>\d[\d,]*(?:\.\d+)?)(?=\s*(?:[.;]|$))"
)
_AI_DATA_TITLE_CONTEXT = re.compile(
    r"\b(?:ai|artificial intelligence|llm|rlhf|annotation|annotator|label(?:er|ing)|"
    r"rater|reviewer|evaluator|trainer|linguist|transcri(?:ber|ption))\b", re.I,
)
_AUTOMATION_TITLE_CONTEXT = re.compile(
    r"\b(?:automation|workflow|integration|n8n|zapier|webhook|whatsapp|"
    r"power\s+platform)\b|\bmake\.com\b|\bmake\s+(?:workflow|scenario)\b", re.I,
)
_MARKETPLACE_HOURLY = re.compile(
    r"(?ix)(?P<currency>\$|₹|€|£|USD|INR|EUR|GBP|CAD|AUD)\s*"
    r"(?P<low>\d[\d,]*(?:\.\d+)?)\s*(?:\.\s*)?-\s*(?:\.\s*)?"
    r"(?:(?:\$|₹|€|£|USD|INR|EUR|GBP|CAD|AUD)\s*)?"
    r"(?P<high>\d[\d,]*(?:\.\d+)?)\s*\.?\s*(?:hourly|/\s*(?:hr|hour)|per\s+hour)\b"
)
_OUTPUT_PAY_EXPRESSION = re.compile(
    r"(?ix)(?:\$|₹|€|£|\bUSD\b|\bINR\b|\bEUR\b|\bGBP\b|\bCAD\b|\bAUD\b)"
    r"\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:-|to)\s*(?:\$|₹|€|£|USD|INR|EUR|GBP|CAD|AUD)?\s*\d[\d,]*(?:\.\d+)?)?"
    r"\s*per\s+(?:accepted\s+|paid\s+)?(?:recorded|audio|finished|transcribed)"
    r"(?:\s+audio)?\s+(?:hour|minute)\b"
)
_DELIVERABLE_TITLE = re.compile(
    r"\b(?:build|create|set\s*up|setup|integrate|implement|fix|debug|develop|automate)\b",
    re.I,
)

_TARGET_TITLE_CONTEXT = re.compile(
    r"\b(?:ai|artificial intelligence|llm|rlhf|data|annotation|annotator|"
    r"label(?:er|ing)|rater|reviewer|evaluator|trainer|linguist|language|"
    r"transcri(?:ber|ption)|locali[sz]ation|translation|research|python|"
    r"automation|workflow|integration|n8n|zapier|webhook|whatsapp|"
    r"etl|api|sql|developer|programmer|software|qa|tester|"
    r"quality)\b",
    re.IGNORECASE,
)
_GENERIC_ROLE_NOUNS = {"rater", "reviewer", "evaluator", "annotator"}
_TITLE_ONLY_CONCEPTS = {"software"}
_TITLE_ONLY_LANGUAGE_TERMS = {"translation", "localization", "multilingual"}


def _exact_role_noun(term: str, text: str) -> bool:
    """Match role nouns as nouns, not fuzzy stems such as review/rate."""
    return re.search(rf"\b{re.escape(term)}s?\b", text, re.IGNORECASE) is not None


_LANGUAGE_NAMES: tuple[object, frozenset[str]] = (None, frozenset())


def _build_language_names(profile) -> frozenset[str]:
    return frozenset(str(name).casefold() for name in (*profile.languages, *profile.known_languages))


def _language_names() -> frozenset[str]:
    """Cached per profile. A run context rebuilds its Profile on every call,
    so the key is the run's profile JSON (or the cached default profile)."""
    global _LANGUAGE_NAMES
    from ..filters.eligibility import _default_profile, default_profile
    from ..run_context import current_run
    context = current_run()
    key = ("run", context.profile_json) if context else ("default", id(_default_profile()))
    if _LANGUAGE_NAMES[0] != key:
        _LANGUAGE_NAMES = (key, _build_language_names(default_profile()))
    return _LANGUAGE_NAMES[1]


def _term_matches(concept: str, term: str, field_name: str, text: str) -> bool:
    if concept in _TITLE_ONLY_CONCEPTS and field_name != "title":
        return False
    if term in _GENERIC_ROLE_NOUNS:
        return _exact_role_noun(term, text)
    if concept == "language_ai" and term.casefold() in _language_names():
        # Language names match exactly: the fuzzy stem match read the place
        # "West Bengal" as the language Bengali (2026-10-02 email).
        return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None
    if concept == "language_ai" and term in _TITLE_ONLY_LANGUAGE_TERMS:
        return field_name == "title" and phrase_in_text(term, text)
    return phrase_in_text(term, text)


@dataclass
class ExtractedSections:
    role: str = ""
    responsibilities: str = ""
    requirements: str = ""
    other: str = ""
    headings_found: list[str] = field(default_factory=list)
    completeness: str = "unknown"

    @property
    def scoped_text(self) -> str:
        chosen = "\n".join(
            item for item in (self.role, self.responsibilities, self.requirements)
            if item
        )
        return chosen or self.other


def extract_sections(description: str, *, native_source: str | None = None) -> ExtractedSections:
    """Best-effort section routing that keeps boilerplate out of fit evidence."""
    text = description or ""
    # Curly and ASCII apostrophes have equal length, so offsets remain valid.
    scan_text = text.replace("’", "'")
    lookup=_HEADING_LOOKUP
    pattern=_HEADING
    aliases={
        'turing_role': {'key qualifications':'requirements','education & experience':'requirements',
            'description':'responsibilities','offer details':'ignore','evaluation process':'ignore'},
        'micro1_role': {'scope of work':'responsibilities','required skills':'requirements',
            'preferred qualifications':'other','role title':'ignore','role type':'ignore','location':'ignore'},
    }.get(native_source)
    if aliases:
        lookup=_HEADING_LOOKUP|aliases
        names='|'.join(re.escape(h).replace(r'\ ',r'\s+') for h in sorted(lookup,key=len,reverse=True))
        pattern=re.compile(rf'(?im)(?:^|(?<=[\n.!?]))[ \t]*(?P<heading>{names})(?=[ \t]*(?:[:.\-–—]|\n|$)|[ \t]+\S)')
    matches = list(pattern.finditer(scan_text))
    if not matches:
        return ExtractedSections(other=text[:3500])

    result = ExtractedSections()
    requirements_found = False
    for index, match in enumerate(matches):
        heading = re.sub(r"\s+", " ", match.group("heading").casefold()).strip()
        category = lookup[heading]
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[start:end].strip(" :.-\n\t–—")
        result.headings_found.append(heading)
        if category == "role":
            result.role += " " + content
        elif category == "responsibilities":
            result.responsibilities += " " + content
        elif category == "requirements":
            requirements_found = True
            result.requirements += " " + content
        elif category == "other":
            result.other += " " + content
    result.role = result.role.strip()[:3000]
    result.responsibilities = result.responsibilities.strip()[:4000]
    result.requirements = result.requirements.strip()[:4000]
    result.other = result.other.strip()[:2500]
    result.completeness = "partial" if requirements_found else "unknown"
    return result


def _currency(raw: str) -> str:
    low = raw.lower()
    if "₹" in raw or "inr" in low or re.search(r"\brs\.?\b", low):
        return "INR"
    if "€" in raw or "eur" in low:
        return "EUR"
    if "£" in raw or "gbp" in low:
        return "GBP"
    if "cad" in low or "c$" in low:
        return "CAD"
    if "aud" in low or "a$" in low:
        return "AUD"
    return "USD"


def _candidate(
    raw: str,
    source_field: str,
    observation_id: str,
    confidence: float,
    source_kind: SourceKind,
    *,
    guaranteed: bool = False,
    list_item: bool = False,
) -> PayCandidate | None:
    # Search snippets sometimes render the range dash as either ``.-.`` or
    # ``. -.``.  Repair only the parsing copy; PayCandidate.raw remains the
    # exact captured evidence.
    semantic_raw = re.sub(r"\s*\.\s*-\s*\.\s*", " - ", raw)
    semantic_raw = re.sub(r"\.\s*(Hourly|/\s*(?:hr|hour)|per\s+hour)\b", r" \1", semantic_raw, flags=re.I)
    if list_item:
        semantic_raw=re.sub(r'^-\s+','',semantic_raw)
    if CONFIG.v55.enabled:
        claim = parse_compensation(semantic_raw)
        if "no_supported_money_expression" in claim.warnings:
            return None
        currency = claim.currency or "unknown"
        fx = CONFIG.pay.fx_to_usd.get(currency)
        warnings = list(claim.warnings)
        if fx is None and claim.currency:
            warnings.append("missing_currency_conversion")
        hourly = claim.basis == "labor_hour"
        fixed = claim.basis == "fixed_project"
        return PayCandidate(
            raw=raw.strip(), source_field=source_field, observation_id=observation_id,
            confidence=confidence, observation_source_kind=source_kind,
            currency=currency, basis="fixed" if fixed else claim.basis,
            actual_unit=claim.basis, literal_unit=claim.literal_unit,
            amount_low=claim.amount_low, amount_high=claim.amount_high,
            min_hourly_usd=round(claim.amount_low * fx, 4) if hourly and fx is not None and claim.amount_low is not None else None,
            max_hourly_usd=round(claim.amount_high * fx, 4) if hourly and fx is not None and claim.amount_high is not None else None,
            fixed_amount_usd=round(claim.amount_low * fx, 4) if fixed and fx is not None and claim.amount_low is not None else None,
            qualifier=claim.qualifier or "unknown", parse_warnings=warnings,
            estimated=claim.labor_equivalent_is_estimate,
            guaranteed=claim.guaranteed_minimum is not None,
            guaranteed_minimum=claim.guaranteed_minimum,
            up_to=claim.qualifier in {"up_to", "up_to_equivalent"},
            labor_hourly_supported=hourly,
        )
    unit_claim = parse_compensation_legacy(semantic_raw)
    lo, hi, basis, estimated = parse_pay(semantic_raw, CONFIG.pay)
    precise_units = CONFIG.v55.enabled
    fixed_amount_usd = None
    if precise_units and unit_claim.basis == "fixed_project" and unit_claim.amount_low is not None:
        fixed_amount_usd = round(
            unit_claim.amount_low * CONFIG.pay.fx_to_usd.get(unit_claim.currency or "USD", 1.0),
            2,
        )
    if lo is None and hi is None and fixed_amount_usd is None:
        return None
    non_labor_unit = unit_claim.basis.startswith("output_") or unit_claim.basis in {"fixed_project", "labor_hour_equivalent"}
    return PayCandidate(
        raw=raw.strip(),
        min_hourly_usd=None if precise_units and non_labor_unit else lo,
        max_hourly_usd=None if precise_units and non_labor_unit else hi,
        fixed_amount_usd=fixed_amount_usd,
        currency=_currency(raw),
        basis=(
            unit_claim.basis
            if precise_units and unit_claim.basis != "unknown" else basis
        ),
        source_field=source_field,
        observation_id=observation_id,
        confidence=confidence,
        estimated=estimated or unit_claim.labor_equivalent_is_estimate,
        guaranteed=guaranteed,
        observation_source_kind=source_kind,
        up_to=bool(re.search(r"\bup to\b", raw, re.I)),
        amount_low=unit_claim.amount_low,
        amount_high=unit_claim.amount_high,
        qualifier=unit_claim.qualifier or "unknown",
        actual_unit=unit_claim.basis,
        labor_hourly_supported=unit_claim.basis == "labor_hour",
    )

def _fixed_candidate(
    text: str,
    source_field: str,
    observation_id: str,
    confidence: float,
    source_kind: SourceKind,
    *,
    marketplace_project: bool = False,
) -> PayCandidate | None:
    patterns = (
        *_FIXED_PRICE_PATTERNS,
        *([_MARKETPLACE_BARE_BUDGET] if marketplace_project else []),
    )
    for pattern in patterns:
        match = pattern.search(text or "")
        if not match:
            continue
        raw = match.group(0).strip()
        currency = _currency(match.group("currency"))
        amount = float(match.group("amount").replace(",", ""))
        fx = CONFIG.pay.fx_to_usd.get(currency, None if CONFIG.v55.enabled else 1.0)
        amount_usd = amount * fx if fx is not None else None
        return PayCandidate(
            raw=raw,
            fixed_amount_usd=round(amount_usd, 2) if amount_usd is not None else None,
            currency=currency,
            # Legacy projection retained; ``actual_unit`` carries V5.5's
            # precise fixed-project vocabulary.
            basis="fixed",
            source_field=source_field,
            observation_id=observation_id,
            confidence=confidence,
            observation_source_kind=source_kind,
            amount_low=amount,
            amount_high=amount,
            actual_unit="fixed_project",
            literal_unit="fixed_project",
            labor_hourly_supported=False,
            parse_warnings=["missing_currency_conversion"] if fx is None else [],
        )
    return None


def _claim_credibility(candidate: PayCandidate) -> float:
    """Score the claim's own observation and field; never borrow URL trust."""
    if CONFIG.v55.enabled and (candidate.actual_unit == "unknown" or candidate.scope in {
        "non_opportunity_document", "other_assignment", "non_pay_context", "unattributed_thread_copy",
        "historical_thread_terms", "unresolved_thread_terms", "role_benefit_unverified", "unattributed_amount",
    } or candidate.scope.startswith('community_benefit:')):
        return 0.0
    source_kind = candidate.observation_source_kind or SourceKind.UNKNOWN
    if source_kind == SourceKind.CONTENT_FARM:
        return 0.0
    structured_field = candidate.source_field in {
        "email", "structured", "marketplace_snippet",
    }
    if source_kind in {
        SourceKind.EMAIL_OFFER,
        SourceKind.ORIGINAL_EMPLOYER,
        SourceKind.ORIGINAL_ATS,
    }:
        provenance = 1.0
    elif structured_field:
        provenance = {
            SourceKind.REPUTABLE_BOARD: 0.82,
            SourceKind.AGGREGATOR_UNRESOLVED: 0.70,
            SourceKind.UNKNOWN: 0.55,
        }.get(source_kind, 0.50)
    else:
        provenance = {
            SourceKind.REPUTABLE_BOARD: 0.60,
            SourceKind.AGGREGATOR_UNRESOLVED: 0.52,
            SourceKind.UNKNOWN: 0.45,
        }.get(source_kind, 0.50)
    if candidate.estimated:
        provenance *= 0.80
        candidate.selection_reasons.append("estimated_piece_rate")
    if candidate.up_to:
        provenance *= 0.72
        candidate.selection_reasons.append("up_to_claim_discount")
    if candidate.corroborating_observation_ids:
        provenance = min(1.0, provenance * 1.08)
        candidate.selection_reasons.append("corroborated_across_observations")
    return round(max(0.0, min(1.0, candidate.confidence * provenance)), 3)


_BUSINESS_AMOUNT_LEFT = re.compile(
    r"\b(?:revenues?|turnover|valuation|profits?|average\s+deal\s+size|"
    r"funding|financing|growth\s+investment|loans\s+funded|assets\s+under\s+management|deal\s+cycles?|ACV)"
    r"\s*(?:(?:of|at|is|was|were|are|now|currently|already|exceeding|exceeds|reached|total(?:s|ling|ing)?|"
    r"over|about|approximately|more\s+than|less\s+than|from|in|the|range|[:=])\s*)*$", re.I,
)
_BUSINESS_AMOUNT_RIGHT = re.compile(
    r"^\s*(?:billion\b|trillion\b|[bB]\b)?\s*\+?\s*(?:(?:million|billion|trillion|[mMbB])\b\s*)?"
    r"(?:(?:USD|INR|EUR|GBP|CAD|AUD)\s+)?"
    r"(?:(?:in|of)\s+)?(?:(?:annual|yearly|lifetime|monthly|recurring|active|project|global|cumulative)\s+)*"
    r"(?:revenues?(?!\s+shar(?:e|ing))|profits?(?!\s+shar(?:e|ing))|turnover|funding|financing|growth\s+investment|"
    r"loans\s+funded|assets\s+under\s+management|"
    r"valuation|market|industry|ARR|ACV|AUM|ad\s+spend|advertising\s+spend|merchandise|"
    r"(?:customer|ecosystem)\s+value|assets\b|capital|credit\b|digital\s+asset\s+transfers|raised|series\s+[A-Z](?:\s+funding)?)\b", re.I,
)
_RAISED_AMOUNT_LEFT = re.compile(
    r"\braised\s*(?:(?:a|an|total\s+of|over|more\s+than|about|approximately)\s+)*[~≈]?\s*$", re.I,
)
_OWN_WAGE_LEFT = re.compile(
    r"\b(?:salary|salaire|rémunération|compensation|pay(?:\s+rate)?|wages?|hourly\s+rate|"
    r"(?:project\s+)?budget|project\s+funding|commission|stipend|bonus)\s*"
    r"(?:(?:annuels?|annuelles?|mensuels?|mensuelles?|horaires?|bruts?|nettes?|range|is|of|at|from|between|raised|to|up\s+to|over|more\s+than|[:=])\s*)*$", re.I,
)
_PERK_AMOUNT_LEFT = re.compile(
    r"\b(?:fertility\s+support|(?:(?:personal|professional|learning\s+(?:and|&)|learning|career)\s+development|"
    r"(?:personal\s+)?wellness|well[- ]being|home\s+office|equipment|conference|laundry|meals?)\s+"
    r"(?:budget|allowance|stipend|reimbursement))\s*(?:(?:of|is|at|up\s+to|[:=])\s*)*$|"
    r"\btechnology\s+stipend\s*(?:[-–—]\s*)?(?:equivalent\s+to\s*)?$", re.I,
)
_PERK_AMOUNT_RIGHT = re.compile(
    r"^\s*(?:/\s*(?:USD|INR|EUR|GBP|CAD|AUD)\b)?\s*"
    r"(?:/\s*(?:months?|mos?|years?|yrs?)\b)?\s*"
    r"(?:(?:[\w]+\s+)?[\w]+['’]s\s+)?"
    r"(?:(?:annual|yearly|monthly)\s+)?"
    r"(?:(?:personal|professional|learning\s+(?:and|&)|learning|career)\s+development|"
    r"(?:personal\s+)?wellness|well[- ]being|home\s+office|equipment|conference|laundry)\s+"
    r"(?:budget|allowance|stipend|reimbursement)\b", re.I,
)
_BUSINESS_COST_LEFT = re.compile(
    r"\b(?:system\s+migrations?|migrations?|software|infrastructure|operations?|"
    r"business\s+operations?|office\s+rent|marketing\s+campaigns?)\s+"
    r"(?:costs?|expenses?)\s*(?:(?:of|over|about|more\s+than|up\s+to|[:=])\s*)*$", re.I,
)
_MEAL_AMOUNT_RIGHT = re.compile(
    r"^\s*(?:/\s*(?:months?|mos?|years?|yrs?)\b)?\s*"
    r"(?:(?:annual|yearly|monthly)\s+)?"
    r"(?:meals?\s+(?:allowance|stipend|budget)|(?:allowance|stipend|budget)\s+for\s+meals?)\b", re.I,
)
_COMPANY_VALUE_LEFT = re.compile(r"\bcompany\s+valued\s+(?:at|above|over)\s*$", re.I)
_INSURANCE_VALUE_LEFT = re.compile(
    r"\b(?:(?:high[- ]value|motor|insurance)\s+)*claims?\s*,?\s*"
    r"(?:(?:are|is)\s+)?(?:(?:typically|initially|each)\s+)?(?:valued|worth)\s*"
    r"(?:(?:at|above|over|more\s+than|up\s+to|between|from)\s*)*$", re.I,
)
_INSURANCE_VALUE_RIGHT = re.compile(
    r"^\s*(?:(?:in|of|worth\s+of)\s+)?(?:motor|insurance)\s+claims?\b", re.I,
)
_INSURANCE_AUTHORITY_LEFT = re.compile(
    r"\b(?:motor|insurance|bodily\s+injury)\s+claims?\b.{0,90}\b"
    r"(?:current\s+)?(?:settlement\s+|payment\s+)?authority\s*"
    r"(?:(?:of|at|least|above|over|up\s+to)\s*)*$",re.I,
)
_BUSINESS_SIZE_LEFT = re.compile(
    r"\b(?:markets?|industry)\s+(?:(?:expected|estimated|projected|forecast|worth|to|reach|over|at|of|[:=])\s*)*$|"
    r"\bcompany\s*[-—–]?\s*(?:privately\s+)?valued\s+(?:(?:at|over|above)\s*)*$|"
    r"\b(?:completed|delivered)\s+\d+\s+projects?\s+(?:worth|valued\s+at)\s*$",re.I,
)
_BUSINESS_SIZE_RIGHT = re.compile(
    r"^\s*\+?\s*(?!(?:per|hourly|monthly|yearly|annual|fixed|for)\b)"
    r"(?:[\w-]+\s+){0,5}(?:industry|markets?|company|outcome|problem)\b",re.I,
)
_DESIGNATED_BENEFIT_LEFT = re.compile(
    r"\b(?:corporate\s+)?benefits?\s+program(?:me)?\b.{0,120}\b(?:health|mobility|learning)\b.{0,100}\bfor\s*$|"
    r"\b(?:tickets?|titres?)\s+restaurant\s*(?:(?:d['’]une\s+valeur\s+de|de)\s*)?$|"
    r"^\s*(?:carte\s+[\w-]+\s+créditée\s+de)\s*$",re.I,
)
_GRANTMAKING_AMOUNT_LEFT = re.compile(
    r"\b(?:total\s+)?grantmaking\s*(?:(?:is|was|likely|to|approach|or|even|exceed|exceeds|of|over|[:=])\s*)*$",re.I,
)
_CASH_BENEFIT_LEFT = re.compile(
    r"\b(?:housing|proximity|relocation|sign[- ]on|signing)\s+(?:bonus|allowance|stipend|grant|payment)"
    r"\s*(?:(?:of|is|at|up\s+to|[:=])\s*)*$|"
    r"\bparticipation\s+annuelle\s+aux\s+bénéfices\b.{0,80}\bde\s*$", re.I,
)
_CASH_BENEFIT_RIGHT = re.compile(
    r"^\s*(?:(?:/\s*|per\s+|a\s+)(?:months?|years?|annum)\s+)?"
    r"(?:(?:monthly|annual|yearly|one[- ]time)\s+)?"
    r"(?:housing|proximity|relocation|sign[- ]on|signing)\s+(?:bonus|allowance|stipend|grant|payment)\b", re.I,
)


def _cash_benefit_context(quote):
    spans = money_expression_spans(quote)
    if not spans:
        return False
    for start, end in spans:
        left, right = quote[max(0,start-120):start], quote[end:end+120]
        # A project budget to build a benefit workflow belongs to the buyer.
        if re.search(r"\b(?:project\s+budget|fixed\s+budget|hourly\s+(?:pay|rate)|base\s+salary)"
                     r"\s*(?:(?:of|is|at|[:=])\s*)*$",left,re.I):
            return False
        if not (_CASH_BENEFIT_LEFT.search(left) or _CASH_BENEFIT_RIGHT.search(right)):
            return False
    return True


def _business_amount_context(quote):
    """Every amount must refer to finances, expenses or perks to exclude it."""
    spans = money_expression_spans(quote)
    if not spans:
        return False
    previous_end = None
    for start, end in spans:
        left, right = quote[max(0, start-180):start], quote[end:end+180]
        if _PERK_AMOUNT_LEFT.search(left) or _BUSINESS_COST_LEFT.search(left) or _DESIGNATED_BENEFIT_LEFT.search(left):
            continue
        if _OWN_WAGE_LEFT.search(left):
            return False
        attached = bool(_BUSINESS_AMOUNT_LEFT.search(left) or _BUSINESS_AMOUNT_RIGHT.search(right)
                        or _PERK_AMOUNT_RIGHT.search(right) or _MEAL_AMOUNT_RIGHT.search(right)
                        or _COMPANY_VALUE_LEFT.search(left) or _INSURANCE_VALUE_LEFT.search(left)
                        or _INSURANCE_VALUE_RIGHT.search(right) or _INSURANCE_AUTHORITY_LEFT.search(left)
                        or _BUSINESS_SIZE_LEFT.search(left) or _GRANTMAKING_AMOUNT_LEFT.search(left) or (
                            re.search(r'\b(?:billion|trillion)\b|\d\s*[bB]\b',quote[start:end],re.I)
                            and _BUSINESS_SIZE_RIGHT.search(right)))
        # A literal range shares the same attached subject. Do not propagate
        # ownership past a new labelled clause or an unlabelled alternative.
        if not attached and previous_end is not None:
            attached = bool(re.fullmatch(r'\s*(?:to|[-–—])\s*', quote[previous_end:start], re.I))
        if not attached and _RAISED_AMOUNT_LEFT.search(left):
            attached = True
        if not attached and re.search(r'\bmanage\s+(?:more\s+than\s+|over\s+)?$', left, re.I):
            attached = bool(re.search(r'^\s*(?:billion\b|[bB]\b)?\s+for\b.{0,80}\binvestors?\b', right, re.I))
        if not attached and re.search(r'\bsecured\s*$', left, re.I):
            attached = bool(re.search(r'\b(?:series\s+[A-Z]|funding|financing|growth\s+investment)\b', right, re.I))
        if not attached:
            return False
        previous_end = end
    return True


def _platform_pool_amount_context(quote):
    """Bind every amount to an explicit platform-wide distribution statement."""
    spans = money_expression_spans(quote)
    if not spans or re.search(r'\b(?:each|per\s+(?:worker|person|contractor|expert))\b', quote, re.I):
        return False
    for start, end in spans:
        left, right = quote[max(0, start-180):start], quote[end:end+180]
        if _OWN_WAGE_LEFT.search(left):
            return False
        group_recipient = (re.search(r'\bmoves\s+(?:over\s+|more\s+than\s+)?$', left, re.I)
            and re.match(r'\s*(?:a|per)\s+day\s+to\s+(?:hundreds|thousands|millions)\b'
                         r'.{0,80}\b(?:experts|workers|contractors|people)\b', right, re.I))
        platform_total = (re.search(r'\bplatform\b.{0,160}\bpays\s+out\s+(?:over\s+|more\s+than\s+)?$', left, re.I)
            and re.match(r'\s*(?:a|per)\s+day\b', right, re.I))
        if not (group_recipient or platform_total):
            return False
    return True


def _pay_context_scope(quote, title):
    """Explicit attribution only; keep rejected claims available for inspection."""
    # Denying an expense does not turn the adjacent wage into an expense.
    context = re.sub(
        r"\b(?:no|without|zero)\s+(?:application\s+fees?|security\s+deposits?|subscription\s+costs?)\b",
        "", quote, flags=re.I,
    )
    if _business_amount_context(quote) or re.search(
        r"\b(?:applicants?|candidates?|you)\s+(?:must\s+|will\s+)?pay\b|"
        r"\b(?:subscription costs?|application fees?|security deposit)\b|"
        r"\b(?:training\s+example(?:\s+only)?\s*:|example\s+only\b)|"
        r"\b(?:illustrative|fictional|hypothetical)\s+(?:pay|rate|wage|salary|amount|figure|example)\b|"
        r"\b(?:pay|rate|wage|salary|amount|figure)\s+(?:is|are)\s+(?:purely\s+)?(?:illustrative|fictional|hypothetical)\b|"
        r"\bnot\s+(?:an?\s+)?offer\s+of\s+(?:pay|compensation)\b|"
        r"^\s*(?:for\s+)?example\s*:", context, re.I,
    ):
        return "non_pay_context"
    if re.search(
        r"\b(?:other|different)\s+(?:assignments?|roles?|jobs?|projects?|compan(?:y|ies))\b|"
        r"\b(?:comment\s+by|previous\s+worker|i\s+(?:earned|was\s+paid))\b", quote, re.I,
    ):
        return "other_assignment"
    # A dated cohort average is not a current offer for this worker. Preserve
    # an explicit wage attached to its own amount when it merely cites a benchmark.
    amount_spans=money_expression_spans(quote)
    if amount_spans and re.search(r'\b(?:moyenne|moyen)\b.{0,100}\b20\d{2}\b',quote,re.I):
        if all(not _OWN_WAGE_LEFT.search(quote[max(0,start-120):start])
               for start,_ in amount_spans):
            return 'other_assignment'
    if _platform_pool_amount_context(quote):
        return 'other_assignment'
    if (re.search(r'\b(?:millions|thousands|hundreds)\s+of\s+(?:domain\s+)?'
                  r'(?:experts|workers|contractors|people)\b.{0,100}\b(?:are|were)\s+paid\b', quote, re.I)
            and re.search(r'\b(?:on|across)\s+(?:(?:the|our|this)\s+)?platform\b', quote, re.I)
            and (parse_compensation(quote).basis != 'labor_hour'
                 or re.search(r'\b(?:in\s+total|collectively|combined)\b', quote, re.I))
            and not re.search(r'\b(?:each|per\s+(?:worker|person|contractor|expert))\b', quote, re.I)):
        return 'other_assignment'
    # Explicit pay subjects may describe another rung of the same team.
    subject = re.match(
        r"\s*(?:our\s+|the\s+)?(supervisors?|managers?|directors?|evaluators?|reviewers?|annotators?|trainers?)"
        r"\s+(?:receive|earn|are\s+paid)\b", quote, re.I,
    )
    if subject and title and not re.search(
        rf"\b{re.escape(subject[1].rstrip('s'))}s?\b", title, re.I
    ):
        return "other_assignment"
    if _cash_benefit_context(quote):
        return "role_benefit_unverified"
    # A truncated aggregate snippet loses its financial subject. Its scale is
    # literal evidence, while ownership remains unknown rather than assumed pay.
    if (amount_spans and re.search(r'\bwith\s+(?:over|more\s+than)\s*$',quote[:amount_spans[0][0]],re.I)
            and re.search(r'\b(?:billion|trillion)\b|\d\s*[bB]\b',quote,re.I)
            and re.search(r'\bin\s+(?:an?|the)\s*…\s*$',quote,re.I)):
        return 'unattributed_amount'
    return "role_claim_unverified"


def _bind_pay_span(item, text, start, end, document, title=""):
    """Offsets are into the normalized observation field, never folded text."""
    item.raw = text[start:end]
    item.source_span_start, item.source_span_end = start, end
    item.source_text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    item.actor = "posting_author_unverified"
    item.scope = _pay_context_scope(item.raw, title)
    amounts = money_expression_spans(item.raw)
    if len(amounts) == 1:
        begin, finish = amounts[0]
        before, after = item.raw[:begin], item.raw[finish:]
        prefix = re.search(r"\b(gross|net|brut|bruts|nette|nettes)\s*(?:(?:base\s+)?(?:pay|salary|salaire)\s*)?[:=]?\s*$",before,re.I)
        suffix = re.match(r"\s*(?:fixe\s+)?(gross|net|brut|bruts|nette|nettes)\b",after,re.I)
        interpretations = {'gross' if match[1].casefold() in {'gross','brut','bruts'} else 'net'
                           for match in (prefix,suffix) if match}
        if len(interpretations) == 1:
            item.gross_net = interpretations.pop()
        elif len(interpretations) > 1:
            item.parse_warnings.append('conflicting_gross_net_claims')
    if not document.actionable:
        item.scope = "non_opportunity_document"
    if item.scope == "other_assignment":
        item.actor = "other_worker_or_role"
    elif item.scope == "non_pay_context":
        item.actor = "non_compensation_context"
    headings = list(re.finditer(r"(?im)^\s*(compensation|pay|salary|benefits|responsibilities|requirements|about us)\s*:?\s*$", text[:start]))
    item.source_section = headings[-1][1].casefold() if headings else item.source_field
    return item


_INDEPENDENT_MONEY_CLAUSE = re.compile(
    r"(?:,\s*(?:(?:and|but|while|whereas|plus)\s+)?|\s+(?:and|but|while|whereas|plus)\s+)"
    r"(?=(?:(?:base|annual|hourly|monthly|weekly|fixed[- ]price)\s+)?"
    r"(?:salary|compensation|wages?|pay\b|project\s+budget|budget\b)|"
    r"(?:(?:our|company|annual|monthly)\s+)*(?:revenues?|valuation|funding)\b|"
    r"we(?:['’]ve|\s+have)?\s+raised\b|"
    r"(?:housing|proximity|relocation|sign[- ]on|signing)\s+(?:bonus|allowance|stipend|grant|payment)\b)", re.I,
)


def _independent_pay_spans(text, left, right):
    """Split explicit labelled facts between amounts, preserving literal offsets."""
    amounts = money_expression_spans(text[left:right])
    if len(amounts) < 2:
        yield left, right
        return
    cuts=[]
    for boundary in _INDEPENDENT_MONEY_CLAUSE.finditer(text, left, right):
        if (any(left+end <= boundary.start() for _, end in amounts)
                and any(left+start >= boundary.end() for start, _ in amounts)):
            cuts.append(boundary.span())
    # "$10K housing bonus" labels its amount on the right. Keep its own
    # qualifiers/conditions without folding it into the preceding base wage.
    for (_,previous_end),(start,end) in zip(amounts,amounts[1:]):
        if not _CASH_BENEFIT_RIGHT.search(text[left+end:right]):
            continue
        between=text[left+previous_end:left+start]
        join=re.search(r",\s*(?:(?:and|plus)\s+)?|\s+(?:and|plus)\s+|\s*\+\s*",between,re.I)
        if join:
            cuts.append((left+previous_end+join.start(),left+previous_end+join.end()))
    cursor = left
    for begin,finish in sorted(set(cuts)):
        if begin < cursor:
            continue
        yield cursor,begin
        cursor=finish
    yield cursor, right


def _prose_pay_candidates(text, field, observation, document, marketplace=False):
    # Split only explicit sentence/line boundaries, retaining decimal punctuation.
    # Independently labelled clauses are split; unlabelled multiple claims still
    # abstain in the single-claim parser. Do not
    # use folded-string offsets to slice the original field.
    # Upwork search snippets use literal dots around range dashes and Hourly.
    # Protect those already-recognized rate spans from sentence splitting.
    protected = [m.span() for m in _MARKETPLACE_HOURLY.finditer(fold_dashes(text))] if marketplace else []
    boundaries = [0, *[m.end() for m in re.finditer(r"\n|[;!?]|\.(?=\s|$)", text)
                      if not any(a <= m.start() < b for a, b in protected)], len(text)]
    spans = (span for left, right in zip(boundaries, boundaries[1:])
             for span in _independent_pay_spans(text, left, right))
    for left, right in spans:
        span = text[left:right]
        if not _CURRENCY.search(span):
            continue
        start = left + len(span) - len(span.lstrip())
        end = right - len(span) + len(span.rstrip())
        quote = text[start:end]
        source_field = "marketplace_snippet" if marketplace else field
        confidence = .95 if marketplace else .85 if field == "title" else .70
        headings=list(re.finditer(r'(?im)^\s*(compensation|pay|salary|benefits|responsibilities|requirements|about us)\s*:?\s*$',text[:start]))
        list_item=(field=='description' and bool(headings)
                   and headings[-1][1].casefold() in {'compensation','pay','salary','benefits'}
                   and bool(re.match(r'^-\s+\S',quote))
                   and not re.search(r'\b(?:negative|deduct(?:ed|ion)?|clawback|repay(?:ment)?)\b',quote,re.I))
        item = _candidate(quote, source_field, observation.observation_id, confidence, observation.source_kind,list_item=list_item)
        if item is None:
            continue
        # Bare fixed budgets are only inferred on an individual buyer route.
        if marketplace and item.actual_unit == "unknown" and (
            item.amount_low is not None or (
                item.parse_warnings == ["negative_or_ambiguous_amount"]
                and _MARKETPLACE_BARE_BUDGET.search(quote)
            )
        ):
            fixed = _fixed_candidate(quote, source_field, observation.observation_id, confidence,
                                     observation.source_kind, marketplace_project=True)
            if fixed:
                item = fixed
                item.parse_warnings.append("fixed_budget_from_explicit_buyer_context")
        yield _bind_pay_span(item, text, start, end, document, observation.job.title)


def extract_pay_candidates(canonical: CanonicalJob) -> tuple[list[PayCandidate], PayCandidate | None, bool]:
    """Collect candidates from every non-farm observation and select by credibility."""
    candidates: list[PayCandidate] = []
    seen: set[tuple] = set()
    thread_parent_ids = set()
    if CONFIG.v55.enabled:
        from .community import observation_thread, bind_pay_actor
        for row in canonical.observations:
            if row.origin == 'hydration' and row.identity_state in {'exact', 'corroborated'} and observation_thread(row):
                thread_parent_ids.update(row.parent_observation_ids)
    has_non_farm = any(
        observation.job is not None
        and observation.source_kind != SourceKind.CONTENT_FARM
        for observation in canonical.observations
    )
    for observation in canonical.observations:
        job = observation.job
        if job is None or (
            has_non_farm and observation.source_kind == SourceKind.CONTENT_FARM
        ):
            continue
        if CONFIG.v55.enabled:
            from .documents import classify_observation
            document = classify_observation(observation)
        if job.pay_raw:
            source_field = "email" if job.source.startswith("email:") else "structured"
            confidence = 0.98 if source_field == "email" else 0.95
            item = _candidate(
                job.pay_raw, source_field, observation.observation_id, confidence,
                observation.source_kind,
                guaranteed=job.guaranteed_hours,
            )
            if item is not None:
                if CONFIG.v55.enabled:
                    _bind_pay_span(item, job.pay_raw, 0, len(job.pay_raw), document, job.title)
                    if job.source == 'first_party' and isinstance(observation.raw_payload.get('baseSalary'), dict):
                        # The normalized literal is derived from this exact
                        # native payload; its string offsets are not JSON offsets.
                        import json
                        item.structured_path = '/baseSalary'
                        try:
                            item.structured_payload_sha256 = hashlib.sha256(json.dumps(
                                observation.raw_payload['baseSalary'], sort_keys=True, separators=(',', ':'),
                                ensure_ascii=False, allow_nan=False,
                            ).encode()).hexdigest()
                        except (TypeError, ValueError):
                            item.parse_warnings.append('structured_provenance_unavailable')
                key = (item.raw.casefold(), item.source_field, item.observation_id)
                if key not in seen:
                    seen.add(key)
                    candidates.append(item)

        if CONFIG.v55.enabled:
            marketplace = "individual_buyer_route" in document.reasons
            for text, field in ((job.title, "title"), (job.description or "", "description")):
                for item in _prose_pay_candidates(text, field, observation, document, marketplace):
                    key = (item.raw.casefold(), item.source_field, item.observation_id)
                    if key not in seen:
                        seen.add(key)
                        candidates.append(item)
            continue

        title_item = parse_title_pay(job.title, CONFIG.pay)
        if title_item is not None:
            _, _, _, _, raw = title_item
            item = _candidate(
                raw, "title", observation.observation_id, 0.85,
                observation.source_kind, guaranteed=job.guaranteed_hours,
            )
            key = (item.raw.casefold(), item.source_field, item.observation_id) if item else None
            if item is not None and key not in seen:
                seen.add(key)
                candidates.append(item)

        marketplace_project = (
            "upwork.com/freelance-jobs/apply/" in (job.url or "").casefold()
        )
        for text, source_field, confidence in (
            (job.title, "title", 0.88),
            (job.description or "", "description", 0.65),
        ):
            if marketplace_project:
                source_field = "marketplace_snippet"
                confidence = 0.95
            fixed = _fixed_candidate(
                text, source_field, observation.observation_id, confidence,
                observation.source_kind,
                marketplace_project=marketplace_project,
            )
            if fixed is not None:
                key = (fixed.raw.casefold(), fixed.source_field, fixed.observation_id)
                if key not in seen:
                    seen.add(key)
                    candidates.append(fixed)

            if marketplace_project:
                hourly_match = _MARKETPLACE_HOURLY.search(normalize_match(text or ""))
                if hourly_match:
                    raw = hourly_match.group(0)
                    item = _candidate(raw, source_field, observation.observation_id, confidence, observation.source_kind)
                    key = (item.raw.casefold(), item.source_field, item.observation_id) if item else None
                    if item is not None and key not in seen:
                        seen.add(key)
                        candidates.append(item)

        description = job.description or ""
        folded_description = normalize_match(description)
        pay_matches = [
            *_PAY_EXPRESSION.finditer(folded_description),
            *_PAY_SUFFIX_EXPRESSION.finditer(folded_description),
            *_OUTPUT_PAY_EXPRESSION.finditer(folded_description),
        ]
        for match in sorted(pay_matches, key=lambda item: item.start()):
            raw = description[match.start():match.end()].strip()
            # The match stops at the explicit period. This is deliberately
            # narrower than the old 180-character clause capture, which could
            # swallow a later "401k" benefit and parse it as $401,000.
            item = _candidate(
                raw, "description", observation.observation_id, 0.70,
                observation.source_kind,
            )
            if item is None:
                continue
            key = (item.raw.casefold(), item.source_field, item.observation_id)
            if key not in seen:
                seen.add(key)
                candidates.append(item)

        # Some descriptions state "$X-$Y/hr" without a "Compensation:" label.
        folded = normalize_match(job.description or "")
        explicit = None if re.search(r"\b(?:hourly|per\s+hour)\s+equivalent\b", folded, re.I) else parse_title_pay(folded, CONFIG.pay)
        if explicit is not None:
            _, _, _, _, raw = explicit
            if _CURRENCY.search(raw) and _HOURLY.search(raw):
                item = _candidate(
                    raw, "description", observation.observation_id, 0.68,
                    observation.source_kind,
                )
                key = (item.raw.casefold(), item.source_field, item.observation_id) if item else None
                if item is not None and key not in seen:
                    seen.add(key)
                    candidates.append(item)

    if CONFIG.v55.enabled:
        observations = {o.observation_id: o for o in canonical.observations}
        for candidate in candidates:
            if candidate.observation_id in thread_parent_ids:
                candidate.actor, candidate.scope = 'unattributed_copy', 'unattributed_thread_copy'
            elif observations[candidate.observation_id].source == 'community_thread':
                bind_pay_actor(candidate, observations[candidate.observation_id])
    observations_by_id = {o.observation_id: o for o in canonical.observations}

    def lineage(identifier):
        visited, pending = set(), [identifier]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            observation = observations_by_id.get(current)
            if observation:
                pending.extend(observation.parent_observation_ids)
        return visited

    families = {key: lineage(key) for key in observations_by_id}

    def distinct_source(left, right):
        if not CONFIG.v55.enabled:
            return True
        if families.get(left, {left}) & families.get(right, {right}):
            return False
        a, b = observations_by_id[left], observations_by_id[right]
        host_a = (urlsplit(a.original_url or a.job.url).hostname or '').removeprefix('www.')
        host_b = (urlsplit(b.original_url or b.job.url).hostname or '').removeprefix('www.')
        if not host_a or not host_b or host_a == host_b:
            return False
        if a.content_hash and a.content_hash == b.content_hash:
            return False
        # Cross-publisher agreement is not proof of independence. Suppress known
        # duplicates here; retain every candidate and source span for inspection.
        return True

    for candidate in candidates:
        candidate.corroborating_observation_ids = sorted({
            other.observation_id
            for other in candidates
            if other.observation_id != candidate.observation_id
            and distinct_source(candidate.observation_id, other.observation_id)
            and (not CONFIG.v55.enabled or candidate.scope == other.scope)
            and other.basis == candidate.basis
            and other.min_hourly_usd == candidate.min_hourly_usd
            and other.max_hourly_usd == candidate.max_hourly_usd
            and other.fixed_amount_usd == candidate.fixed_amount_usd
            and (not CONFIG.v55.enabled or (
                candidate.amount_low is not None
                and (other.currency, other.actual_unit, other.literal_unit,
                     other.amount_low, other.amount_high, other.qualifier)
                == (candidate.currency, candidate.actual_unit, candidate.literal_unit,
                    candidate.amount_low, candidate.amount_high, candidate.qualifier)
            ))
        })
        candidate.claim_credibility = _claim_credibility(candidate)
    candidates.sort(key=lambda item: (
        -item.claim_credibility,
        -item.confidence,
        item.observation_id,
        item.source_field,
        item.raw.casefold(),
    ))
    selectable = [c for c in candidates if not CONFIG.v55.enabled or c.scope not in {
        "non_opportunity_document", "other_assignment", "non_pay_context", "unattributed_thread_copy",
        "historical_thread_terms", "unresolved_thread_terms", "role_benefit_unverified", "unattributed_amount",
    } and not c.scope.startswith('community_benefit:')]
    selected = selectable[0] if selectable else None
    if selected is not None:
        selected.selection_reasons.append("highest_claim_credibility")
    conflict = False
    strong = [candidate for candidate in selectable if candidate.confidence >= 0.65]
    bases = {candidate.basis for candidate in strong}
    if any(value.startswith("fixed") for value in bases) and len(bases) > 1:
        conflict = True
    elif CONFIG.v55.enabled and "labor_hour" in bases and any(
        value.startswith("output_") for value in bases
    ):
        conflict = True
    elif len({
        candidate.fixed_amount_usd for candidate in strong
        if candidate.fixed_amount_usd is not None
    }) > 1:
        conflict = True
    elif selected and selected.midpoint:
        for candidate in selectable[1:]:
            midpoint = candidate.midpoint
            if midpoint is None or candidate.confidence < 0.65:
                continue
            difference = abs(midpoint - selected.midpoint) / max(selected.midpoint, 0.01)
            if difference > 0.25:
                conflict = True
                break
    if CONFIG.v55.enabled:
        native = [c for c in strong if c.amount_low is not None]
        units = {(c.currency, c.actual_unit, c.literal_unit, c.qualifier) for c in native}
        conflict |= len(units) > 1
        output = {(c.currency, c.literal_unit, c.amount_low, c.amount_high) for c in native if not c.labor_hourly_supported}
        conflict |= len(output) > 1
        conflict |= any(c.parse_warnings and c.amount_low is None for c in strong)
    return candidates, selected, conflict


def detect_concepts(
    title: str,
    sections: ExtractedSections,
    profile: Profile,
) -> tuple[list[str], dict[str, str]]:
    """Each configured concept contributes once, with the strongest field."""
    matched: list[str] = []
    evidence: dict[str, str] = {}
    fields = [
        ("title", title),
        ("requirements", sections.requirements),
        ("responsibilities", sections.responsibilities),
        ("role", sections.role),
        ("body", sections.other),
    ]
    title_has_target_context = _TARGET_TITLE_CONTEXT.search(title or "") is not None
    title_has_ai_data_context = _AI_DATA_TITLE_CONTEXT.search(title or "") is not None
    title_has_automation_context = _AUTOMATION_TITLE_CONTEXT.search(title or "") is not None
    for concept, terms in profile.role_concepts.items():
        for field_name, text in fields:
            if not text:
                continue
            # Body evidence is allowed only when the title already describes a
            # plausible target lane. This prevents employer/service vocabulary
            # from turning Account Executive, Transportation Analyst, Security
            # Engineer, or Business Analyst postings into AI/data work.
            if field_name != "title" and not title_has_target_context:
                continue
            if (
                field_name != "title"
                and concept in {"ai_evaluation", "annotation"}
                and not title_has_ai_data_context
            ):
                continue
            if (
                field_name != "title"
                and concept == "workflow_automation"
                and not title_has_automation_context
            ):
                continue
            term = next(
                (term for term in terms
                 if _term_matches(concept, term, field_name, text)),
                None,
            )
            if term is None:
                continue
            matched.append(concept)
            evidence[concept] = f"{field_name}:{term}"
            break
    # Explicit generalist checking work is an evaluation task even when the
    # title uses "checker" rather than a configured rater/evaluator synonym.
    if (
        "ai_evaluation" not in matched
        and re.search(r"\bgeneralist\b.*\b(?:ai|llm)\b.*\bchecker\b", title, re.I)
    ):
        matched.append("ai_evaluation")
        evidence["ai_evaluation"] = "title:generalist AI checker"
    if (
        "workflow_automation" not in matched
        and re.search(r"\bmake\s+(?:workflow|scenario|automation)\b", title, re.I)
        and _DELIVERABLE_TITLE.search(title)
    ):
        matched.append("workflow_automation")
        evidence["workflow_automation"] = "title:Make workflow deliverable"
    return matched, evidence
