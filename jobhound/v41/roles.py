"""Occupation, task-lane and profile-driven specialist-domain assessment."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..filters.eligibility import Profile
from ..text import normalize_match
from .models import CanonicalJob, Evidence, RoleAssessment

_SOFTWARE = re.compile(
    r"\b(?:software|frontend|backend|full[ -]?stack|web|mobile|java|python)\s+"
    r"(?:engineer|developer|programmer)\b|\b(?:software\s+developer|programmer)\b",
    re.I,
)
_DATA_ENGINEER = re.compile(
    r"\b(?:data|analytics?|machine\s+learning)\s+"
    r"(?:engineer|scientist|architect)\b",
    re.I,
)
_QA = re.compile(r"\b(?:qa|quality\s+assurance|software\s+test(?:er|ing))\b", re.I)
_SALES = re.compile(
    r"\b(?:sales|account\s+executive|business\s+development|lead\s+generation)\b",
    re.I,
)
_RESEARCHER = re.compile(
    r"\b(?:research(?:er|\s+scientist|\s+engineer|\s+assistant)|scientist)\b",
    re.I,
)
_ANALYST = re.compile(r"\banalyst\b", re.I)
_TRANSCRIBER = re.compile(r"\b(?:transcriber|transcriptionist|captioner)\b", re.I)
_ANNOTATOR = re.compile(r"\b(?:annotator|data\s+label(?:er|ing)|annotation)\b", re.I)
_LANGUAGE_ROLE = re.compile(
    r"\b(?:linguist|translator|translation\s+specialist|language\s+specialist)\b",
    re.I,
)
_AI_RATER = re.compile(
    r"\b(?:ai|llm|model|search\s+quality|data)\b.{0,45}"
    r"\b(?:rater|evaluator|reviewer|trainer|contributor|tutor|assessor)\b|"
    r"\b(?:rater|evaluator|reviewer|trainer|contributor|tutor|assessor)\b.{0,45}"
    r"\b(?:ai|llm|model|rlhf)\b",
    re.I,
)
_AUTOMATION = re.compile(
    r"\b(?:n8n|zapier|make\.com|webhook|whatsapp|workflow\s+automation|"
    r"api\s+integration|crm\s+automation)\b|\bmake\s+(?:workflow|scenario|automation)\b",
    re.I,
)
_DELIVERABLE = re.compile(
    r"\b(?:build|create|set\s*up|setup|integrate|implement|fix|debug|develop|"
    r"automate|migration|workflow|bot)\b",
    re.I,
)
_SPECIALIST_NOUN = re.compile(
    r"\b(?:expert|specialist|(?:ai\s+)?evaluator|(?:ai\s+)?reviewer|"
    r"(?:ai\s+)?trainer|research\s+assistant|simulation\s+reviewer)\b",
    re.I,
)
_SPECIALIST_PREFIX = re.compile(
    r"^(?P<domain>.+?)\s+(?:(?:ai|llm)(?:\s+training)?\s+)?"
    r"(?:expert|specialist|evaluator|reviewer|trainer|research\s+assistant)\b",
    re.I,
)
_GENERIC_SPECIALIST_PREFIX = re.compile(
    r"^(?:(?:ai|llm|model|data|content|language|search\s+quality)"
    r"(?:\s+(?:ai|llm|model|data|content|training|quality))*)$",
    re.I,
)

_TASK_LANE_MAP = {
    "ai_evaluation": "ai_evaluation",
    "annotation": "annotation",
    "language_ai": "language_data",
    "transcription": "transcription",
    "workflow_automation": "workflow_automation",
    "software": "software_delivery",
    "data_automation": "data_delivery",
}


@dataclass(frozen=True)
class RoleSemantics:
    occupation: str
    task: str
    subject: str | None
    generalist: bool
    status: str


_GENERALIST_TASK = re.compile(
    r"\b(?:label|classify|rate|review|evaluate|annotate)\b.{0,80}"
    r"\b(?:provided\s+guidelines|school[- ]level|general\s+knowledge|no\s+(?:subject\s+)?expertise)\b",
    re.I,
)


def classify_role_semantics(title: str, description: str, url: str, profile: Profile) -> RoleSemantics:
    """Pure diagnostic separating the work shape from its discussed subject."""
    occupation = _occupation(title, url)
    subject, status = _specialist_domain(title, profile)
    explicit_generalist = bool(
        re.search(r"\bgeneralist\b", title, re.I)
        or _GENERALIST_TASK.search(description or "")
    )
    conventional = occupation in {"software_engineer", "data_engineer", "qa", "sales", "researcher"}
    generalist = explicit_generalist and status in {"none", "unresolved"} and not conventional
    task = "workflow_automation" if occupation == "automation_contractor" else (
        "ai_evaluation" if occupation == "ai_rater" else occupation
    )
    return RoleSemantics(occupation, task, subject, generalist, status)


def _phrase_in_title(phrase: str, title: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", title, re.I) is not None


def _occupation(title: str, url: str) -> str:
    scoped_marketplace = "upwork.com/freelance-jobs/apply/" in (url or "").casefold()
    if _AUTOMATION.search(title) and (scoped_marketplace or _DELIVERABLE.search(title)):
        return "automation_contractor"
    # Conventional occupations retain their identity even when "(AI Training)"
    # or another AI modifier appears later in the title.
    if _SOFTWARE.search(title):
        return "software_engineer"
    if _DATA_ENGINEER.search(title):
        return "data_engineer"
    if _QA.search(title):
        return "qa"
    if _SALES.search(title):
        return "sales"
    if _LANGUAGE_ROLE.search(title):
        return "language_specialist"
    if _TRANSCRIBER.search(title):
        return "transcriber"
    if _ANNOTATOR.search(title):
        return "annotator"
    if _RESEARCHER.search(title):
        return "researcher"
    if _ANALYST.search(title):
        return "analyst"
    if _AI_RATER.search(title):
        return "ai_rater"
    if _AUTOMATION.search(title):
        return "automation_contractor"
    return "other"


def _specialist_domain(
    title: str,
    profile: Profile,
) -> tuple[str | None, str]:
    folded = normalize_match(title).casefold()
    if not _SPECIALIST_NOUN.search(folded):
        return None, "none"
    if any(_phrase_in_title(language, folded) for language in profile.known_languages):
        if re.search(
            r"\b(?:language|linguist|translator|translation|ai|llm|"
            r"rater|evaluator|reviewer|trainer|annotator)\b",
            folded,
        ):
            return None, "none"

    aliases: list[tuple[str, str]] = []
    for canonical, values in profile.domain_aliases.items():
        aliases.extend((alias, canonical) for alias in {canonical, *values})
    aliases.extend((domain, domain) for domain in profile.credential_domains)
    matches: list[tuple[int, int, str, str]] = []
    for alias, canonical in aliases:
        match = re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", folded, re.I)
        if match is not None:
            matches.append((match.start(), -len(alias), alias, canonical))
    if matches:
        _, _, alias, canonical = min(matches)
        claimed = profile.claimed_domains()
        status = (
            "claimed"
            if canonical in profile.credential_domains or alias in claimed
            else "unclaimed"
        )
        return canonical, status

    prefix = _SPECIALIST_PREFIX.search(folded)
    if prefix is None:
        return None, "unresolved"
    candidate = re.sub(
        r"\b(?:remote|contract|freelance|part[ -]?time|full[ -]?time)\b",
        " ",
        prefix.group("domain"),
    )
    candidate = re.sub(r"\s+", " ", candidate).strip(" -–—|:;,()[]")
    if not candidate or _GENERIC_SPECIALIST_PREFIX.fullmatch(candidate):
        return None, "none"
    return candidate, "unresolved"


def assess_role(
    canonical: CanonicalJob,
    concepts: list[str],
    concept_evidence: dict[str, str],
    profile: Profile,
) -> RoleAssessment:
    title = canonical.job.title
    semantics = classify_role_semantics(
        title, canonical.job.description or "", canonical.job.url, profile,
    )
    task_lanes = list(dict.fromkeys(
        _TASK_LANE_MAP[concept]
        for concept in concepts
        if concept in _TASK_LANE_MAP
    ))
    specialist_domain, specialist_status = _specialist_domain(title, profile)
    if semantics.generalist:
        specialist_domain, specialist_status = None, "none"
    title_observation_id = canonical.field_sources.get(
        "title", canonical.best_observation.observation_id
    )
    evidence = [
        Evidence(
            dimension="role",
            code=f"occupation:{_occupation(title, canonical.job.url)}",
            value=_occupation(title, canonical.job.url),
            source_field="title",
            observation_id=title_observation_id,
            source_kind=canonical.best_observation.source_kind,
            span=title,
            confidence=0.96,
        )
    ]
    evidence.append(Evidence(
        dimension="role_scope",
        code="generalist" if semantics.generalist else "specialist_or_occupation",
        value=semantics.generalist,
        source_field="description",
        observation_id=canonical.field_sources.get(
            "description", canonical.best_observation.observation_id
        ),
        span=(canonical.job.description or "")[:500],
        confidence=0.92 if semantics.generalist else 0.8,
    ))
    for concept, source in concept_evidence.items():
        field, _, span = source.partition(":")
        evidence.append(Evidence(
            dimension="task_lane",
            code=f"task_lane:{_TASK_LANE_MAP.get(concept, concept)}",
            value=_TASK_LANE_MAP.get(concept, concept),
            source_field=field,
            observation_id=canonical.field_sources.get(
                "description", canonical.best_observation.observation_id
            ),
            span=span,
            confidence=0.9 if field == "title" else 0.75,
        ))
    if specialist_status != "none":
        evidence.append(Evidence(
            dimension="specialist_domain",
            code=f"specialist_domain:{specialist_status}",
            value=specialist_domain or "",
            source_field="title",
            observation_id=title_observation_id,
            source_kind=canonical.best_observation.source_kind,
            span=title,
            confidence=0.95 if specialist_status != "unresolved" else 0.72,
        ))
    return RoleAssessment(
        occupation_family=_occupation(title, canonical.job.url),
        task_lanes=task_lanes,
        matched_profile_domains=(
            [specialist_domain] if specialist_domain and specialist_status == "claimed" else []
        ),
        specialist_domain=specialist_domain,
        specialist_domain_status=specialist_status,
        ai_task_lane=task_lanes[0] if task_lanes else "unknown",
        specialist_domains=[specialist_domain] if specialist_domain else [],
        evidence=evidence,
    )


def specialist_blockers(role: RoleAssessment) -> list[str]:
    if role.specialist_domain_status != "unclaimed" or not role.specialist_domain:
        return []
    return [f"credential_mismatch:specialist_domain:{role.specialist_domain}"]
