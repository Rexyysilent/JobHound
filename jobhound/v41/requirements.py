"""High-precision, provenance-bearing requirement extraction.

Hard blockers are emitted only from titles, recognized requirement sections,
or structured provider fields. General biography and project-history prose is
deliberately excluded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Iterable

from ..filters.eligibility import (
    Profile,
    _canon_lang,
    _title_language_pairs,
    _title_language_requirements,
    _title_unknown_language_requirements,
    credential_gate,
)
from ..text import normalize_match
from .extract import extract_sections
from .models import (
    CanonicalJob,
    RequirementClaim,
    RequirementsAssessment,
    SourceKind,
)

_SEGMENT_BREAK = re.compile(r"(?:\n+|[;•]+|(?<=[.!?])\s+)")
_LANGUAGE_CUE = re.compile(
    r"\b(?:native|fluent|proficient|proficiency|written|spoken|"
    r"must\s+speak|required\s+language|language\s+required|"
    r"mother\s+tongue|c[12])\b",
    re.IGNORECASE,
)
_PREFERRED = re.compile(
    r"\b(?:preferred|desirable|nice\s+to\s+have|a\s+plus|bonus)\b",
    re.IGNORECASE,
)
_OPTIONAL = re.compile(r"\boptional\b", re.IGNORECASE)
_REQUIRED = re.compile(r"\b(?:required|must|minimum|at\s+least)\b", re.IGNORECASE)
_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_NUMBER = r"(?:\d+(?:\.\d+)?|" + "|".join(_NUMBER_WORDS) + r")"
_EXPERIENCE = re.compile(
    rf"(?ix)\b"
    rf"(?P<prefix>minimum(?:\s+of)?|at\s+least)?\s*"
    rf"(?P<low>{_NUMBER})"
    rf"(?:\s*(?:-|to)\s*(?P<high>{_NUMBER}))?"
    rf"\s*(?P<plus>\+)?\s*years?"
    rf"(?:['’]s?|\s+of|\s+with|\s+in|\s+working|\s+experience)?\b"
)
_DEGREE_GATES = {"advanced_degree", "public_health"}
_TOOL_GATES = {"eda_tools", "hardware_eda", "security_clearance"}
_TRUNCATION_MARKER = re.compile(r"(?:\.\.\.|…)(?:\s|$)")
_AUTHORITATIVE_REQUIREMENTS = {
    SourceKind.EMAIL_OFFER,
    SourceKind.ORIGINAL_EMPLOYER,
    SourceKind.ORIGINAL_ATS,
}
_NON_APPLICANT_CONTEXT = re.compile(
    r"\b(?:consult|ask|contact|speak\s+with|advice\s+from)\s+(?:a|an|your)\b|"
    r"\b(?:our|the)\s+(?:clients?|customers?|users?|partners?)\b|"
    r"\b(?:company|business|platform)\b.{0,40}\b(?:clients?|customers?|users?|partners?)\b",
    re.IGNORECASE,
)
_ADVISORY_CONTEXT = re.compile(
    r"\b(?:consult(?:ing)?|ask|contact|speak\s+with|seek\s+advice\s+from)\s+(?:a|an|your)\b",
    re.IGNORECASE,
)
from ..config import CONFIG
_APPLICANT_ACTOR = re.compile(
    r"\b(?:applicants?|candidates?|you|must\s+be|we\s+require|required\s+to\s+be)\b",
    re.IGNORECASE,
)
_NEGATED_REQUIREMENT = re.compile(
    r"\b(?:no|not)\s+(?:prior\s+)?(?:\w+\s+){0,3}(?:required|necessary)\b|"
    r"\b(?:degree|experience|licen[cs]e)\s+(?:is\s+)?not\s+required\b|"
    r"\bno\b[^.;]{0,120}\brequired\b",
    re.IGNORECASE,
)
_SKILL_EXPERIENCE = re.compile(
    rf"(?ix)\b(?P<years>{_NUMBER})(?:\s*\+)?\s*years?"
    rf"(?:\s+of)?\s+(?P<skill>[a-z][a-z0-9+#. -]{{1,50}}?)\s+experience\b"
)
_SKILL_WORK = re.compile(
    rf"(?ix)\b(?P<years>{_NUMBER})(?:\s*\+)?\s*years?\s+of\s+"
    rf"(?P<skill>[a-z][a-z0-9+#.-]{{1,30}})\s+(?:work|experience)\b"
)
_EXPLICIT_CREDENTIALS = (
    ("tax_professional", re.compile(r"\b(?:qualified|licensed|certified)\s+tax\s+professionals?\b", re.I)),
    ("medical_licence", re.compile(r"\b(?:medical|physician)\s+licen[cs]e\b", re.I)),
    ("university_degree", re.compile(r"\b(?:university|college|bachelor'?s?|master'?s?)\s+degree\b|\ba\s+degree\b", re.I)),
)
_VOICE_READINESS = (
    ("voice_performance_experience", re.compile(
        r"\bexperience\s+with\s+(?:voice\s+acting|narration|podcasting|streaming)", re.I
    )),
    ("recording_setup", re.compile(
        r"\baccess\s+to\s+(?:a\s+)?(?:professional(?:\s+or\s+high[- ]quality)?|high[- ]quality)\s+(?:home\s+)?recording\s+setup\b", re.I
    )),
    ("voice_demo", re.compile(
        r"\b(?:able|required)\s+to\s+provide\s+(?:a\s+)?(?:demo|sample\s+recordings?|voiceover\s+samples?)\b", re.I
    )),
    ("voice_licensing_consent", re.compile(
        r"\b(?:open|agree|willing)\s+to\s+licens(?:e|ing)\s+(?:your|their)\s+voice\s+for\s+AI\s+voice\s+cloning\s*(?:/|or)?\s*TTS", re.I
    )),
)


@dataclass(frozen=True)
class RequirementOutcomes:
    blockers: tuple[str, ...]
    unverified: tuple[str, ...]
    alternatives_satisfied: tuple[str, ...] = ()
_LOCATION_SCOPE_CUE = re.compile(
    r"\b(?:must\s+(?:be|live|reside|work)|residents?\s+of|citizens?\s+of|"
    r"based\s+in|located\s+in|work\s+authorization|eligible\s+to\s+work|"
    r"remote\s+(?:within|in|from)|candidates?\s+(?:in|from)|"
    r"applicants?\s+(?:in|from|residing)|cannot\s+accept\s+applicants?|"
    r"remote\b.{0,50}\bapplicants?|remote\s+(?:worldwide|globally|india|us|usa|u\.s\.|united\s+states)|only)\b",
    re.IGNORECASE,
)
_LOCATION_ALIASES = {
    "india": ("india", "indian", "bengaluru", "bangalore", "kolkata", "mumbai", "delhi", "pune", "hyderabad", "chennai"),
    "worldwide": ("worldwide", "work from anywhere", "globally remote", "remote globally"),
    "apac": ("apac", "asia pacific"),
    "asia": ("asia",),
    "united_arab_emirates": ("united arab emirates", "uae", "dubai", "abu dhabi"),
    "chile": ("chile", "santiago"),
    "israel": ("israel", "tel aviv", "jerusalem"),
    "norway": ("norway", "oslo"),
    "peru": ("peru", "lima"),
    "germany": ("germany", "berlin", "munich", "hamburg"),
    "united_states": ("united states", "usa", "u.s.", "us", "new york", "san francisco"),
    "united_kingdom": ("united kingdom", "london", "england", "scotland", "wales"),
    "canada": ("canada", "toronto", "vancouver"),
    "australia": ("australia", "sydney", "melbourne"),
    "europe": ("europe", "eu", "emea"),
    "latin_america": ("latin america", "latam"),
    "brazil": ("brazil", "são paulo", "sao paulo"),
    "argentina": ("argentina", "buenos aires"),
    "mexico": ("mexico", "mexico city"),
    "ireland": ("ireland", "dublin"),
    "france": ("france", "paris"),
    "spain": ("spain", "madrid", "barcelona"),
    "italy": ("italy", "rome", "milan"),
    "netherlands": ("netherlands", "amsterdam"),
    "singapore": ("singapore",),
    "philippines": ("philippines", "manila"),
    "indonesia": ("indonesia", "jakarta"),
    "malaysia": ("malaysia", "kuala lumpur"),
    "japan": ("japan", "tokyo"),
    "south_korea": ("south korea", "seoul"),
    "south_africa": ("south africa", "cape town", "johannesburg"),
}
_ACCESSIBLE_LOCATION_SCOPES = {"india", "worldwide", "apac", "asia"}


def _segments(text: str) -> Iterable[str]:
    for part in _SEGMENT_BREAK.split(text or ""):
        cleaned = part.strip(" \t\r\n•-*")
        if cleaned:
            yield cleaned


def _number(value: str) -> float:
    lowered = value.casefold()
    if lowered in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[lowered])
    return float(lowered)


def _modality(text: str, *, default_required: bool = False) -> str:
    if _PREFERRED.search(text):
        return "preferred"
    if _OPTIONAL.search(text):
        return "optional"
    if _REQUIRED.search(text):
        return "minimum" if re.search(r"\b(?:minimum|at\s+least)\b", text, re.I) else "required"
    return "required" if default_required else "unknown"


def _known_language_hits(text: str, profile: Profile) -> list[str]:
    folded = normalize_match(text).casefold()
    hits: list[tuple[int, str]] = []
    for language in sorted(profile.known_languages, key=len, reverse=True):
        if language in {"multilingual", "any language"}:
            continue
        # Avoid compiling/searching roughly a thousand language regexes for
        # every full description. The literal containment check is a safe
        # negative filter because the boundary regex uses the same normalized
        # case-folded spelling.
        if language not in folded:
            continue
        match = re.search(rf"(?<!\w){re.escape(language)}(?!\w)", folded)
        if match:
            hits.append((match.start(), _canon_lang(language)))
    return list(dict.fromkeys(language for _, language in sorted(hits)))


def _language_claims(
    text: str,
    *,
    observation_id: str,
    source_field: str,
    profile: Profile,
    default_required: bool,
) -> list[RequirementClaim]:
    claims: list[RequirementClaim] = []
    for segment in _segments(text):
        languages = _known_language_hits(segment, profile)
        if not languages:
            continue
        if _NON_APPLICANT_CONTEXT.search(segment) and not _APPLICANT_ACTOR.search(segment):
            continue
        if not default_required and not _LANGUAGE_CUE.search(segment) and not _REQUIRED.search(segment):
            continue
        modality = _modality(segment, default_required=default_required)
        between = normalize_match(segment).casefold()
        match_mode = "single"
        if len(languages) > 1:
            match_mode = "any" if re.search(r"\bor\b", between) else "all"
        claims.append(RequirementClaim(
            dimension="language",
            value=languages,
            modality=modality,
            match_mode=match_mode,
            required=modality in {"required", "minimum"},
            observation_id=observation_id,
            source_field=source_field,
            span=segment[:500],
            confidence=0.98 if source_field == "title" else 0.94,
        ))
    return claims


def _title_language_claims(
    title: str,
    observation_id: str,
    profile: Profile,
    supporting_text: str = "",
) -> list[RequirementClaim]:
    required = {
        _canon_lang(value)
        for value in _title_language_requirements(title, profile.known_languages)
    }
    pairs = {
        _canon_lang(value)
        for value in _title_language_pairs(title, profile.known_languages)
    }
    target_language_demand = re.search(
        r"\b(?:native|fluent|fluency|proficien(?:t|cy)|c[12])\b.{0,50}"
        r"\b(?:in\s+)?the\s+target\s+language\b|"
        r"\btarget\s+language\b.{0,50}\b(?:required|mandatory|fluency|proficiency)\b",
        normalize_match(supporting_text), re.I,
    )
    unknown = (
        _title_unknown_language_requirements(title, profile.known_languages)
        if target_language_demand else set()
    )
    languages = sorted(required | pairs | unknown)
    if not languages:
        return []
    folded = normalize_match(title).casefold()
    mode = "any" if len(languages) > 1 and re.search(r"\bor\b", folded) else (
        "all" if len(languages) > 1 else "single"
    )
    return [RequirementClaim(
        dimension="language",
        value=languages,
        modality="required",
        match_mode=mode,
        required=True,
        observation_id=observation_id,
        source_field="title",
        span=title[:500],
        confidence=0.99,
    )]


def _experience_claims(
    text: str,
    *,
    observation_id: str,
    source_field: str,
) -> list[RequirementClaim]:
    claims: list[RequirementClaim] = []
    for segment in _segments(text):
        for match in _EXPERIENCE.finditer(normalize_match(segment)):
            value = _number(match.group("low"))
            modality = _modality(segment, default_required=True)
            if _NEGATED_REQUIREMENT.search(segment):
                modality = "explicitly_not_required"
            if match.group("prefix") or match.group("plus"):
                modality = "minimum" if modality not in {"preferred", "optional"} else modality
            skill_match = _SKILL_EXPERIENCE.search(normalize_match(segment))
            skill = skill_match.group("skill").strip().casefold() if skill_match else "general"
            if re.search(r"\brelevant\s+professional\s+work\b", segment, re.I):
                skill = "relevant_work"
            claims.append(RequirementClaim(
                dimension="experience",
                value=value,
                modality=modality,
                required=modality in {"required", "minimum"},
                observation_id=observation_id,
                source_field=source_field,
                span=segment[:500],
                confidence=0.95 if modality != "unknown" else 0.75,
                actor="applicant",
                predicate="has_experience",
                polarity="negative" if _NEGATED_REQUIREMENT.search(segment) else "positive",
                scope=skill,
            ))
        for match in _SKILL_WORK.finditer(normalize_match(segment)):
            value = _number(match.group("years"))
            modality = _modality(segment, default_required=True)
            claims.append(RequirementClaim(
                dimension="experience", value=value, modality=modality,
                required=modality in {"required", "minimum"},
                observation_id=observation_id, source_field=source_field,
                span=segment[:500], confidence=0.95, actor="applicant",
                predicate="has_experience", scope=match.group("skill").casefold(),
            ))
    return claims


def _credential_claims(
    text: str,
    *,
    observation_id: str,
    source_field: str,
    profile: Profile,
) -> list[RequirementClaim]:
    claims: list[RequirementClaim] = []
    for reason in credential_gate(text, profile):
        _, gate, span = (reason.split(":", 2) + [""])[:3]
        occurrence = text.casefold().find(span.casefold())
        local = text[max(0, occurrence - 80):occurrence + len(span) + 80] if occurrence >= 0 else text
        if _ADVISORY_CONTEXT.search(local):
            continue
        if _NON_APPLICANT_CONTEXT.search(local) and not _APPLICANT_ACTOR.search(local):
            continue
        dimension = "degree" if gate in _DEGREE_GATES else (
            "tool" if gate in _TOOL_GATES else "domain"
        )
        claims.append(RequirementClaim(
            dimension=dimension,
            value=gate,
            modality="required",
            required=True,
            observation_id=observation_id,
            source_field=source_field,
            span=span[:500],
            confidence=0.98 if source_field == "title" else 0.93,
        ))
    return claims


def _location_claims(
    text: str,
    *,
    observation_id: str,
    source_field: str,
    structured: bool,
) -> list[RequirementClaim]:
    claims: list[RequirementClaim] = []
    if _NON_APPLICANT_CONTEXT.search(text) and not _APPLICANT_ACTOR.search(text):
        return claims
    for tag, pattern in _EXPLICIT_CREDENTIALS:
        match = pattern.search(text)
        if match is None:
            continue
        left = max(text.rfind(".", 0, match.start()), text.rfind(";", 0, match.start()))
        right_candidates = [
            position for position in (
                text.find(".", match.end()), text.find(";", match.end())
            ) if position >= 0
        ]
        right = min(right_candidates) + 1 if right_candidates else len(text)
        local = text[left + 1:right].strip()
        modality = _modality(local, default_required=False)
        if _NEGATED_REQUIREMENT.search(local):
            modality = "explicitly_not_required"
        claims.append(RequirementClaim(
            dimension="degree" if tag == "university_degree" else "domain",
            value=tag, modality=modality,
            required=modality in {"required", "minimum", "mandatory"},
            observation_id=observation_id, source_field=source_field,
            span=local[:500], confidence=0.97, actor="applicant",
            predicate="holds_credential",
            polarity="negative" if modality == "explicitly_not_required" else "positive",
        ))
    for segment in _segments(text):
        folded = normalize_match(segment).casefold()
        if not structured and _LOCATION_SCOPE_CUE.search(folded) is None:
            continue
        values: list[str] = []
        for canonical, aliases in _LOCATION_ALIASES.items():
            if any(
                re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", folded)
                for alias in aliases
            ):
                values.append(canonical)
        if not values:
            continue
        claims.append(RequirementClaim(
            dimension="location",
            value=sorted(set(values)),
            modality="required" if not structured else "unknown",
            match_mode="any" if len(values) > 1 else "single",
            required=True if not structured else None,
            observation_id=observation_id,
            source_field=source_field,
            span=segment[:500],
            confidence=0.98 if structured else 0.94,
            actor="applicant",
            predicate="resides_in",
            polarity=("negative" if re.search(
                r"\b(?:cannot|can't|not\s+accept|excluding|except)\b", folded
            ) else "positive"),
        ))
    return claims


def _voice_readiness_claims(
    text: str, *, observation_id: str, source_field: str,
) -> list[RequirementClaim]:
    """Explicit applicant readiness for voice-recording opportunities."""
    claims: list[RequirementClaim] = []
    for segment in _segments(text):
        modality = _modality(segment, default_required=True)
        if modality in {"preferred", "optional"} or _NEGATED_REQUIREMENT.search(segment):
            continue
        for tag, pattern in _VOICE_READINESS:
            match = pattern.search(segment)
            if match is None:
                continue
            claims.append(RequirementClaim(
                dimension="tool", value=tag, modality="required", required=True,
                observation_id=observation_id, source_field=source_field,
                span=segment[:500], confidence=0.97, actor="applicant",
                predicate="has_voice_readiness_evidence", scope="voice_work",
            ))
    return claims


def requirement_outcomes(
    requirements: RequirementsAssessment,
    profile: Profile,
) -> RequirementOutcomes:
    """Evaluate only explicit profile evidence against extracted requirements.

    Search preferences and broad capability domains intentionally cannot satisfy
    employment duration, degrees, licences, or specialist credentials.
    """
    blockers: list[str] = []
    unverified: list[str] = []
    alternatives: list[str] = []
    speaks = {_canon_lang(value) for value in (profile.working_languages or profile.languages)}
    credentials = {value.casefold() for value in profile.formal_credentials}
    credential_absences = {
        value.casefold() for value in profile.absent_formal_credentials
    }
    years = {key.casefold(): float(value) for key, value in profile.documented_professional_years.items()}
    grouped: dict[str, list[RequirementClaim]] = {}
    for claim in requirements.claims:
        if claim.logic_group:
            grouped.setdefault(claim.logic_group, []).append(claim)
    satisfied_groups: set[str] = set()
    for group, members in grouped.items():
        for claim in members:
            if claim.dimension == "experience":
                documented = years.get(str(claim.scope).casefold())
                if documented is not None and documented >= float(claim.value):
                    satisfied_groups.add(group)
            elif claim.dimension in {"degree", "domain", "tool"} and str(claim.value).casefold() in credentials:
                satisfied_groups.add(group)
        if group in satisfied_groups:
            alternatives.append(group)
    for claim in requirements.claims:
        if claim.evidence_status == "weak_copy":
            continue
        if claim.logic_group in satisfied_groups:
            continue
        if claim.modality not in {"required", "minimum", "mandatory"}:
            continue
        values = claim.value if isinstance(claim.value, list) else [claim.value]
        if claim.dimension == "language":
            demanded = {_canon_lang(str(value).casefold()) for value in values}
            satisfied = bool(demanded & speaks) if claim.match_mode == "any" else demanded <= speaks
            if not satisfied:
                blockers.extend(f"lang_mismatch:{item}" for item in sorted(demanded - speaks))
        elif claim.dimension == "experience":
            needed = float(claim.value)
            skill = str(getattr(claim, "scope", "") or "general").casefold()
            documented = years.get(skill)
            if documented is None:
                unverified.append(f"experience_unverified:{skill}:{needed:g}_years")
            elif documented < needed:
                blockers.append(f"experience_mismatch:{skill}:{needed:g}_years")
        elif claim.dimension in {"degree", "domain", "tool"}:
            credential = str(claim.value).casefold()
            if credential in {
                "voice_performance_experience", "recording_setup", "voice_demo",
                "voice_licensing_consent",
            }:
                if credential not in profile.demonstrated_skills:
                    unverified.append(f"requirement_unverified:{credential}")
                continue
            if credential in credential_absences:
                blockers.append(f"credential_mismatch:{credential}")
            elif (
                claim.dimension == "domain" and credential.endswith("_professional")
                and credential.removesuffix("_professional") not in profile.capability_domains
                and credential not in credentials
            ):
                blockers.append(f"required_profession_outside_profile:{credential}")
            elif credential not in credentials:
                unverified.append(f"credential_unverified:{credential}")
    return RequirementOutcomes(
        tuple(dict.fromkeys(blockers)),
        tuple(dict.fromkeys(unverified)),
        tuple(dict.fromkeys(alternatives)),
    )


def _dedupe(claims: Iterable[RequirementClaim]) -> list[RequirementClaim]:
    unique: list[RequirementClaim] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for claim in claims:
        key = (
            claim.dimension,
            repr(claim.value),
            claim.modality,
            claim.observation_id,
            claim.span.casefold(),
            claim.scope.casefold(),
        )
        if key not in seen:
            seen.add(key)
            unique.append(claim)
    scoped_experience = {
        (claim.observation_id, claim.span.casefold(), float(claim.value))
        for claim in unique
        if claim.dimension == "experience" and claim.scope != "general"
    }
    return [
        claim for claim in unique
        if not (
            claim.dimension == "experience" and claim.scope == "general"
            and (claim.observation_id, claim.span.casefold(), float(claim.value))
            in scoped_experience
        )
    ]


def assess_requirements(
    canonical: CanonicalJob,
    profile: Profile,
) -> RequirementsAssessment:
    """Extract claims independently from every non-farm observation."""
    claims: list[RequirementClaim] = []
    completeness = "unknown"
    for observation in canonical.observations:
        job = observation.job
        if job is None or observation.source_kind == SourceKind.CONTENT_FARM:
            continue
        claims.extend(_title_language_claims(
            job.title, observation.observation_id, profile, job.description or ""
        ))
        claims.extend(_credential_claims(
            job.title,
            observation_id=observation.observation_id,
            source_field="title",
            profile=profile,
        ))
        # A title such as "Remote US" is explicit listing-level scope, unlike
        # a city embedded only in a copied URL slug.  Preserve it as applicant
        # geography even when the body is a thin aggregator snippet.
        claims.extend(_location_claims(
            job.title,
            observation_id=observation.observation_id,
            source_field="title",
            structured=False,
        ))
        description = job.description or ""
        sections = extract_sections(description)
        authoritative_complete = (
            observation.source_kind in _AUTHORITATIVE_REQUIREMENTS
            and (
                observation.content_state in {"complete", "role_complete"}
                or (
                    not CONFIG.v55.enabled
                    and _TRUNCATION_MARKER.search(description) is None
                )
            )
            and not observation.truncated
        )
        if authoritative_complete and CONFIG.v55.enabled:
            completeness = "complete"
        if sections.requirements:
            candidate = "complete" if authoritative_complete else "partial"
            if candidate == "complete" or completeness == "unknown":
                completeness = candidate
            claims.extend(_language_claims(
                sections.requirements,
                observation_id=observation.observation_id,
                source_field="requirements",
                profile=profile,
                default_required=True,
            ))
            claims.extend(_experience_claims(
                sections.requirements,
                observation_id=observation.observation_id,
                source_field="requirements",
            ))
            claims.extend(_credential_claims(
                sections.requirements,
                observation_id=observation.observation_id,
                source_field="requirements",
                profile=profile,
            ))
            if CONFIG.v55.enabled:
                claims.extend(_voice_readiness_claims(
                    sections.requirements,
                    observation_id=observation.observation_id,
                    source_field="requirements",
                ))
            claims.extend(_location_claims(
                sections.requirements,
                observation_id=observation.observation_id,
                source_field="requirements",
                structured=False,
            ))
        if authoritative_complete and CONFIG.v55.enabled:
            claims.extend(_language_claims(
                description,
                observation_id=observation.observation_id,
                source_field="description",
                profile=profile,
                default_required=False,
            ))
            for segment in _segments(description):
                if not _REQUIRED.search(segment) and not _PREFERRED.search(segment):
                    continue
                segment_claims = _experience_claims(
                    segment,
                    observation_id=observation.observation_id,
                    source_field="description",
                )
                segment_claims.extend(_credential_claims(
                    segment,
                    observation_id=observation.observation_id,
                    source_field="description",
                    profile=profile,
                ))
                if re.search(r"\bdegree\b.+\bor\b.+\byears?\b", segment, re.I):
                    group = f"alternative:{observation.observation_id}:{len(claims)}"
                    segment_claims = [
                        claim.model_copy(update={"logic_group": group, "match_mode": "any"})
                        for claim in segment_claims
                    ]
                claims.extend(segment_claims)
            claims.extend(_location_claims(
                description,
                observation_id=observation.observation_id,
                source_field="description",
                structured=False,
            ))
            claims.extend(_voice_readiness_claims(
                description,
                observation_id=observation.observation_id,
                source_field="description",
            ))
        if job.location:
            claims.extend(_location_claims(
                job.location,
                observation_id=observation.observation_id,
                source_field="location",
                structured=True,
            ))

    claims = _dedupe(claims)
    authoritative_ids = {
        observation.observation_id for observation in canonical.observations
        if CONFIG.v55.enabled
        and observation.source_kind in _AUTHORITATIVE_REQUIREMENTS
        and observation.content_state in {"complete", "role_complete"}
        and not observation.truncated
    }
    if authoritative_ids:
        claims = [
            claim.model_copy(update={"evidence_status": "weak_copy"})
            if claim.observation_id not in authoritative_ids else claim
            for claim in claims
        ]
    result = RequirementsAssessment(
        completeness=completeness,
        languages=[claim for claim in claims if claim.dimension == "language"],
        experience=[claim for claim in claims if claim.dimension == "experience"],
        degrees=[claim for claim in claims if claim.dimension == "degree"],
        domains=[claim for claim in claims if claim.dimension == "domain"],
        tools=[claim for claim in claims if claim.dimension == "tool"],
        location_scope=[claim for claim in claims if claim.dimension == "location"],
        claims=claims,
        mandatory_language_groups=[
            claim for claim in claims
            if claim.dimension == "language"
            and claim.modality in {"required", "minimum", "mandatory"}
        ],
        degree_requirement_state=(
            "mandatory" if any(
                claim.modality in {"required", "minimum", "mandatory"}
                for claim in claims if claim.dimension == "degree"
            ) else "not_stated"
        ),
    )
    required_languages: list[str] = []
    preferred_languages: list[str] = []
    for claim in result.languages:
        target = (
            required_languages
            if claim.modality in {"required", "minimum"}
            else preferred_languages
        )
        target.extend(str(value) for value in (claim.value if isinstance(claim.value, list) else [claim.value]))
    result.language_required = sorted(set(required_languages))
    result.language_preferred = sorted(set(preferred_languages))
    mandatory_years = [
        float(claim.value) for claim in result.experience
        if claim.modality in {"required", "minimum"}
    ]
    preferred_years = [
        float(claim.value) for claim in result.experience
        if claim.modality == "preferred"
    ]
    result.experience_min_years = max(mandatory_years, default=None)
    result.experience_preferred_years = max(preferred_years, default=None)
    result.credentials_required = sorted({
        str(claim.value) for claim in [*result.degrees, *result.domains, *result.tools]
        if claim.modality in {"required", "minimum"}
    })
    return result


def language_mismatches(
    requirements: RequirementsAssessment,
    profile: Profile,
) -> list[str]:
    speaks = {_canon_lang(value) for value in profile.languages}
    reasons: list[str] = []
    for claim in requirements.languages:
        if claim.modality not in {"required", "minimum"}:
            continue
        values = {
            _canon_lang(str(value).casefold())
            for value in (claim.value if isinstance(claim.value, list) else [claim.value])
        }
        if claim.match_mode == "any" and values & speaks:
            continue
        missing = values - speaks
        reasons.extend(f"lang_mismatch:{value}" for value in sorted(missing))
    return list(dict.fromkeys(reasons))


def location_mismatches(requirements: RequirementsAssessment) -> list[str]:
    reasons: list[str] = []
    for claim in requirements.location_scope:
        values = {
            str(value).casefold()
            for value in (
                claim.value if isinstance(claim.value, list) else [claim.value]
            )
        }
        if claim.polarity == "negative":
            if "india" in values:
                reasons.append("location_scope_mismatch:india_excluded")
            continue
        if values.intersection(_ACCESSIBLE_LOCATION_SCOPES):
            continue
        reasons.extend(
            f"location_scope_mismatch:{value}" for value in sorted(values)
        )
    return list(dict.fromkeys(reasons))


def experience_mismatches(
    requirements: RequirementsAssessment,
    max_required_years: int,
) -> list[str]:
    values = [
        float(claim.value) for claim in requirements.experience
        if claim.modality in {"required", "minimum"}
        and float(claim.value) > max_required_years
    ]
    return [
        f"experience_mismatch:{value:g}_years"
        for value in sorted(set(values), reverse=True)
    ]


def preferred_experience_watchouts(
    requirements: RequirementsAssessment,
    max_required_years: int,
) -> list[str]:
    values = [
        float(claim.value) for claim in requirements.experience
        if claim.modality == "preferred" and float(claim.value) > max_required_years
    ]
    return [
        f"preferred_experience:{value:g}_years"
        for value in sorted(set(values), reverse=True)
    ]
