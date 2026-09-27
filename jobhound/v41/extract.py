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
from .compensation import parse_compensation
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


def _term_matches(concept: str, term: str, field_name: str, text: str) -> bool:
    if concept in _TITLE_ONLY_CONCEPTS and field_name != "title":
        return False
    if term in _GENERIC_ROLE_NOUNS:
        return _exact_role_noun(term, text)
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


def extract_sections(description: str) -> ExtractedSections:
    """Best-effort section routing that keeps boilerplate out of fit evidence."""
    text = description or ""
    # Curly and ASCII apostrophes have equal length, so offsets remain valid.
    scan_text = text.replace("’", "'")
    matches = list(_HEADING.finditer(scan_text))
    if not matches:
        return ExtractedSections(other=text[:3500])

    result = ExtractedSections()
    requirements_found = False
    for index, match in enumerate(matches):
        heading = re.sub(r"\s+", " ", match.group("heading").casefold()).strip()
        category = _HEADING_LOOKUP[heading]
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
) -> PayCandidate | None:
    # Search snippets sometimes render the range dash as either ``.-.`` or
    # ``. -.``.  Repair only the parsing copy; PayCandidate.raw remains the
    # exact captured evidence.
    semantic_raw = re.sub(r"\s*\.\s*-\s*\.\s*", " - ", raw)
    semantic_raw = re.sub(r"\.\s*(Hourly|/\s*(?:hr|hour)|per\s+hour)\b", r" \1", semantic_raw, flags=re.I)
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
        "non_opportunity_document", "other_assignment", "non_pay_context",
    }):
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


def _pay_context_scope(quote, title):
    """Explicit attribution only; keep rejected claims available for inspection."""
    # Denying an expense does not turn the adjacent wage into an expense.
    context = re.sub(
        r"\b(?:no|without|zero)\s+(?:application\s+fees?|security\s+deposits?|subscription\s+costs?)\b",
        "", quote, flags=re.I,
    )
    if re.search(
        r"\b(?:applicants?|candidates?|you)\s+(?:must\s+|will\s+)?pay\b|"
        r"\b(?:company revenue|annual revenue|funding raised|subscription costs?|application fees?|security deposit)\b|"
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
    # Explicit pay subjects may describe another rung of the same team.
    subject = re.match(
        r"\s*(?:our\s+|the\s+)?(supervisors?|managers?|directors?|evaluators?|reviewers?|annotators?|trainers?)"
        r"\s+(?:receive|earn|are\s+paid)\b", quote, re.I,
    )
    if subject and title and not re.search(
        rf"\b{re.escape(subject[1].rstrip('s'))}s?\b", title, re.I
    ):
        return "other_assignment"
    return "role_claim_unverified"


def _bind_pay_span(item, text, start, end, document, title=""):
    """Offsets are into the normalized observation field, never folded text."""
    item.raw = text[start:end]
    item.source_span_start, item.source_span_end = start, end
    item.source_text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    item.actor = "posting_author_unverified"
    item.scope = _pay_context_scope(item.raw, title)
    if not document.actionable:
        item.scope = "non_opportunity_document"
    if item.scope == "other_assignment":
        item.actor = "other_worker_or_role"
    elif item.scope == "non_pay_context":
        item.actor = "non_compensation_context"
    headings = list(re.finditer(r"(?im)^\s*(compensation|pay|salary|benefits|responsibilities|requirements|about us)\s*:?\s*$", text[:start]))
    item.source_section = headings[-1][1].casefold() if headings else item.source_field
    return item


def _prose_pay_candidates(text, field, observation, document, marketplace=False):
    # Split only explicit sentence/line boundaries, retaining decimal punctuation.
    # Multiple claims within one clause abstain in the single-claim parser. Do not
    # use folded-string offsets to slice the original field.
    # Upwork search snippets use literal dots around range dashes and Hourly.
    # Protect those already-recognized rate spans from sentence splitting.
    protected = [m.span() for m in _MARKETPLACE_HOURLY.finditer(fold_dashes(text))] if marketplace else []
    boundaries = [0, *[m.end() for m in re.finditer(r"\n|[;!?]|\.(?=\s|$)", text)
                      if not any(a <= m.start() < b for a, b in protected)], len(text)]
    for left, right in zip(boundaries, boundaries[1:]):
        span = text[left:right]
        if not _CURRENCY.search(span):
            continue
        start = left + len(span) - len(span.lstrip())
        end = right - len(span) + len(span.rstrip())
        quote = text[start:end]
        source_field = "marketplace_snippet" if marketplace else field
        confidence = .95 if marketplace else .85 if field == "title" else .70
        item = _candidate(quote, source_field, observation.observation_id, confidence, observation.source_kind)
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
            from .documents import classify_document
            document = classify_document(job.title, job.description or "", job.url)
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
        "non_opportunity_document", "other_assignment", "non_pay_context",
    }]
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
