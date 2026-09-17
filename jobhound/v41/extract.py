"""Section, concept and provenance-bearing pay extraction for V4.1."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..config import CONFIG
from ..enrich.pay import parse_pay, parse_title_pay
from ..filters.eligibility import Profile
from ..filters.matching import phrase_in_text
from ..text import normalize_match
from .models import CanonicalJob, PayCandidate, SourceKind
from .compensation import parse_compensation

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
    unit_claim = parse_compensation(semantic_raw)
    # The deployed V4.2 path must not manufacture labor rates from output units
    # either. Keep legacy currency-free formats only when no native expression
    # was recognized at all; recognized ambiguous claims always abstain.
    precise_units = CONFIG.v55.enabled or unit_claim.amount_low is not None
    if unit_claim.amount_low is None and unit_claim.warnings != ("no_supported_money_expression",):
        return None
    fixed_amount_usd = None
    if precise_units:
        # Abstention is terminal for this expression. The legacy parser must not
        # turn a rejected mixed claim into a fabricated hourly rate.
        if unit_claim.amount_low is None:
            return None
        basis, estimated = unit_claim.basis, unit_claim.labor_equivalent_is_estimate
        lo = hi = None
        fx = CONFIG.pay.fx_to_usd.get(unit_claim.currency or "")
        if fx is not None and fx > 0:
            if basis == "labor_hour":
                lo = unit_claim.amount_low * fx
                hi = unit_claim.amount_high * fx
            elif basis == "fixed_project":
                fixed_amount_usd = round(unit_claim.amount_low * fx, 2)
    else:
        lo, hi, basis, estimated = parse_pay(semantic_raw, CONFIG.pay)
        if lo is None and hi is None:
            return None
    non_labor_unit = unit_claim.basis != "labor_hour"
    return PayCandidate(
        raw=raw.strip(),
        min_hourly_usd=None if precise_units and non_labor_unit else lo,
        max_hourly_usd=None if precise_units and non_labor_unit else hi,
        fixed_amount_usd=fixed_amount_usd,
        currency=unit_claim.currency if precise_units else _currency(raw),
        basis="hour" if basis == "labor_hour" and not CONFIG.v55.enabled else basis,
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
        selection_reasons=list(unit_claim.warnings),
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
        amount_usd = amount * CONFIG.pay.fx_to_usd.get(currency, 1.0)
        return PayCandidate(
            raw=raw,
            fixed_amount_usd=round(amount_usd, 2),
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
            labor_hourly_supported=False,
        )
    return None


def _claim_credibility(candidate: PayCandidate) -> float:
    """Score the claim's own observation and field; never borrow URL trust."""
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


def extract_pay_candidates(canonical: CanonicalJob) -> tuple[list[PayCandidate], PayCandidate | None, bool]:
    """Collect candidates from every non-farm observation and select by credibility."""
    candidates: list[PayCandidate] = []
    invalid_structured_claim = False
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
        if job.pay_raw:
            parsed_claim = parse_compensation(job.pay_raw)
            if (parsed_claim.amount_low is None and parsed_claim.warnings
                    and parsed_claim.warnings != ("no_supported_money_expression",)):
                invalid_structured_claim = True
            source_field = "email" if job.source.startswith("email:") else "structured"
            confidence = 0.98 if source_field == "email" else 0.95
            item = _candidate(
                job.pay_raw, source_field, observation.observation_id, confidence,
                observation.source_kind,
                guaranteed=job.guaranteed_hours,
            )
            if item is not None:
                key = (item.raw.casefold(), item.source_field, item.observation_id)
                if key not in seen:
                    seen.add(key)
                    candidates.append(item)

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

    def native_key(candidate):
        # Equal null hourly projections are not corroboration of output-unit pay.
        return (candidate.currency, candidate.actual_unit, candidate.amount_low,
                candidate.amount_high, candidate.qualifier, candidate.scope)

    for candidate in candidates:
        candidate.corroborating_observation_ids = sorted({
            other.observation_id
            for other in candidates
            if other.observation_id != candidate.observation_id
            and distinct_source(candidate.observation_id, other.observation_id)
            and (
                (native_key(other) == native_key(candidate)
                 and other.amount_low is not None and candidate.amount_low is not None)
                if (CONFIG.v55.enabled or other.amount_low is not None
                    or candidate.amount_low is not None) else (
                    other.basis == candidate.basis
                    and other.min_hourly_usd == candidate.min_hourly_usd
                    and other.max_hourly_usd == candidate.max_hourly_usd
                    and other.fixed_amount_usd == candidate.fixed_amount_usd
                )
            )
        })
        candidate.claim_credibility = _claim_credibility(candidate)
    candidates.sort(key=lambda item: (
        -item.claim_credibility,
        -item.confidence,
        item.observation_id,
        item.source_field,
        item.raw.casefold(),
    ))
    selected = candidates[0] if candidates else None
    if selected is not None:
        selected.selection_reasons.append("highest_claim_credibility")
    conflict = invalid_structured_claim
    strong = [candidate for candidate in candidates if candidate.confidence >= 0.65]
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
        for candidate in candidates[1:]:
            midpoint = candidate.midpoint
            if midpoint is None or candidate.confidence < 0.65:
                continue
            difference = abs(midpoint - selected.midpoint) / max(selected.midpoint, 0.01)
            if difference > 0.25:
                conflict = True
                break
    known_native = [c for c in strong if c.amount_low is not None]
    if len({native_key(c) for c in known_native}) > 1:
        # Until scope-specific alternatives can be resolved, preserve every
        # claim but withhold a single trusted compensation conclusion.
        conflict = True
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
