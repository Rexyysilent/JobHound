"""Eligibility gates (v3 Patch #1). Runs BEFORE relevance scoring. Answers:
"could the operator plausibly even apply?" — using the job TITLE as the
high-precision signal (title-stated languages/credentials are near-always hard
requirements; descriptions are noisy, so description-level checks stay in the
optional LLM P3 pass).

Returns (eligible, reasons). Ineligible jobs -> verdict="rejected_ineligible".

Profile lives in profile.yaml at the repo root (anchored, not cwd-relative —
Task Scheduler runs with cwd=system32).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

_PROFILE_PATH = Path(__file__).resolve().parent.parent.parent / "profile.yaml"


@dataclass
class Profile:
    languages: set[str]
    credential_domains: set[str]
    known_languages: set[str]
    gated_role_patterns: dict[str, str]
    domain_aliases: dict[str, list[str]] = field(default_factory=dict)
    anchors: list[str] = field(default_factory=list)
    no_anchor_fit_cap: int = 30
    role_concepts: dict[str, list[str]] = field(default_factory=dict)
    role_priorities: dict[str, int] = field(default_factory=dict)
    allowed_seniority: set[str] = field(
        default_factory=lambda: {"entry", "junior", "mid"}
    )
    max_required_years: int = 2
    # Evidence-scoped V5.5 declarations.  Legacy ``credential_domains`` and
    # ``max_required_years`` remain policy inputs; neither is evidence of a
    # degree or of time actually worked.
    working_languages: set[str] = field(default_factory=set)
    capability_domains: set[str] = field(default_factory=set)
    demonstrated_skills: set[str] = field(default_factory=set)
    formal_credentials: set[str] = field(default_factory=set)
    # Explicitly documented absences/refutations (for example a profile form
    # answer of ``medical_licence: false``). Unknown credentials remain absent
    # from both sets and must not be treated as known failures.
    absent_formal_credentials: set[str] = field(default_factory=set)
    documented_professional_years: dict[str, float] = field(default_factory=dict)
    target_lanes: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, cfg: dict) -> "Profile":
        el = cfg.get("eligibility", {})
        languages = {s.lower() for s in cfg.get("languages", [])}
        credential_domains = {
            s.lower() for s in cfg.get("credential_domains", [])
        }
        return cls(
            languages=languages,
            credential_domains=credential_domains,
            known_languages={s.lower() for s in el.get("known_languages", [])},
            gated_role_patterns=dict(el.get("gated_role_patterns", {})),
            domain_aliases={k.lower(): [s.lower() for s in v]
                            for k, v in (el.get("domain_aliases") or {}).items()},
            anchors=[s.lower() for s in cfg.get("anchors", [])],
            no_anchor_fit_cap=int(el.get("no_anchor_fit_cap", 30)),
            role_concepts={
                name.lower(): [str(term).lower() for term in terms]
                for name, terms in (cfg.get("role_concepts") or {}).items()
            },
            role_priorities={
                name.lower(): int(priority)
                for name, priority in (cfg.get("role_priorities") or {}).items()
            },
            allowed_seniority={
                str(level).lower() for level in
                (cfg.get("allowed_seniority") or ["entry", "junior", "mid"])
            },
            max_required_years=int(cfg.get("max_required_years", 2)),
            working_languages={
                str(item).lower() for item in
                (cfg.get("working_languages") or languages)
            },
            capability_domains={
                str(item).lower() for item in
                (cfg.get("capability_domains") or credential_domains)
            },
            demonstrated_skills={
                str(item).lower() for item in
                (cfg.get("demonstrated_skills") or [])
            },
            formal_credentials={
                str(item).lower() for item in
                (cfg.get("formal_credentials") or [])
            },
            absent_formal_credentials={
                str(item).lower() for item in
                (cfg.get("absent_formal_credentials") or [])
            },
            documented_professional_years={
                str(skill).lower(): float(years)
                for skill, years in
                (cfg.get("documented_professional_years") or {}).items()
            },
            target_lanes={
                str(item).lower() for item in (cfg.get("target_lanes") or [])
            },
        )

    def claimed_domains(self) -> set[str]:
        """Claimed credential domains, expanded through canonical alias groups.

        A gate pattern surfaces many surface forms for one domain — the
        electronics gates alone can match "electrical", "circuit", "ngspice" or
        "vlsi". Without expansion, claiming "electronics" in credential_domains
        would authorize only the titles that literally say "electronics", and
        opting into a domain would mean auditing every pattern. Claiming the
        canonical name now authorizes the whole configured group.
        """
        claimed = {d.lower() for d in self.credential_domains}
        for canonical, aliases in self.domain_aliases.items():
            if canonical in claimed:
                claimed.update(aliases)
        return claimed


@lru_cache(maxsize=1)
def _default_profile() -> Profile:
    path = _PROFILE_PATH
    if not path.exists():
        path = path.with_name("profile.example.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Profile.from_config(data)


def default_profile() -> Profile:
    from ..run_context import current_run
    context = current_run()
    return context.profile() if context else _default_profile()


default_profile.cache_clear = _default_profile.cache_clear


# --- language gate -----------------------------------------------------------

# A language token in a title counts as a REQUIREMENT when it appears in one of
# these shapes (guards against false hits like company names containing "Thai").
_LANG_CONTEXT_TEMPLATES = [
    r"[-–—(\[]\s*{lang}\b",                 # "... - German", "(German)"
    r"\b{lang}\s*[)\]]",                     # "... German)"
    r"\b{lang}\s+(language|speakers?|speaking|linguist|translator|transcriber)\b",
    r"\b(bilingual|native|fluent)\s+{lang}\b",
    r"\b{lang}\s*[-–—]\s*(english|{lang2})\b",  # "German-English" pairs
    # Per-language AI evaluation titles (for example, "Azerbaijani AI
    # Content Evaluators") state the language requirement through the role.
    r"\b{lang}\s+(?:ai\s+)?(?:content\s+)?(?:evaluators?|raters?|reviewers?|"
    r"annotators?|trainers?|assessors?)\b",
    # "Marathi AI Language Specialist" and equivalent titles state a hard
    # language requirement even though "language" separates the language name
    # from the role noun.
    r"\b{lang}\s+(?:ai\s+)?language\s+(?:specialists?|analysts?|evaluators?|"
    r"reviewers?|raters?|trainers?)\b",
    # Wider language-role form: "Spanish Search Quality Rater", "German Data
    # Annotator", and similar titles still state a hard language requirement.
    r"\b{lang}\s+(?:search|quality|rater|evaluator|annotator|annotation|"
    r"transcriber|transcription|content|voice|audio|data|linguist|translator|"
    r"translation|tutor|teacher|speaker|speaking|review(?:er)?)\b",
    r"\b{lang}\b\s*$",                       # trailing: "AI Trainer German"
]

_NEVER_A_REQUIREMENT = {"multilingual", "any language"}

# High-confidence Cyrillic homoglyphs used in otherwise-Latin language names.
# This is deliberately scoped to gate matching rather than display text: a title
# such as ``Lаtvian`` (Cyrillic a) must not bypass eligibility, while genuine
# Bengali/Cyrillic content remains untouched in the digest.
_LATIN_GATE_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
    "у": "y", "і": "i", "ј": "j", "к": "k", "м": "m", "т": "t",
})


def _gate_text(text: str) -> str:
    return (text or "").casefold().translate(_LATIN_GATE_CONFUSABLES)


def _title_language_requirements(title: str, known: set[str]) -> set[str]:
    t = " " + _gate_text(title).strip() + " "
    # Nearly all configured language names are absent from any one title.
    # Checking that cheap condition first avoids compiling/searching roughly
    # 1,000 regexes per job on full-source runs.
    title_words = set(re.findall(r"[a-z]+", t))
    found: set[str] = set()
    for lang in known:
        if lang in _NEVER_A_REQUIREMENT:
            continue
        if lang not in title_words:
            continue
        for tpl in _LANG_CONTEXT_TEMPLATES:
            pat = tpl.format(lang=re.escape(lang), lang2=r"[a-z]+")
            if re.search(pat, t):
                found.add(lang)
                break
    return found


_LANG_PAIR = re.compile(
    r"\b([a-z]+)\s*(?:<>|<->|/|\+|&|\band\b|\bto\b)\s*([a-z]+)\b",
    re.IGNORECASE,
)
_UNKNOWN_LANGUAGE_ROLE = re.compile(
    r"(?ix)^\s*(?:ai\s+training\s+contributors?|ai\s+(?:response\s+|content\s+)?"
    r"(?:evaluators?|reviewers?|raters?|trainers?)|language\s+(?:evaluators?|specialists?))"
    r"\s*[-–—|:]\s*(?P<language>[a-z][a-z '\-]{1,35}?)"
    r"\s*[-–—|:]\s*(?:remote|contract|freelance|part[- ]?time|full[- ]?time)\b"
)
# LILT-style language roles: "Linguist/Transcriber - Chichewa/Nyanja - Remote",
# "Linguist - Shona - UI Technical / Marketing - Remote", "Content Writer -
# Chichewa - Remote". The slot may list alternatives and be followed by any
# further title part.
_LANGUAGE_ROLE_NOUN = r"(?:linguists?|transcribers?|translators?|(?:content\s+)?writers?|voice\s+talent)"
_LANGUAGE_WORK_ROLE = re.compile(
    rf"(?ix)^\s*{_LANGUAGE_ROLE_NOUN}(?:\s*/\s*{_LANGUAGE_ROLE_NOUN})*"
    r"\s*[-–—|:]\s*(?P<language>[a-z][a-z '/]{1,40}?)\s*(?:[-–—|:(]|$)"
)
_NON_LANGUAGE_SLOT = {"all genders", "worldwide", "global", "india", "united states", "quality", "content", "data", "remote"}
# Words that mark a title slot as something other than a language name.
_NON_LANGUAGE_WORDS = {
    "ui", "ux", "technical", "marketing", "quality", "content", "data", "remote",
    "legal", "medical", "financial", "finance", "general", "senior", "junior",
    "lead", "contract", "freelance", "part", "full", "time", "worldwide", "global",
    "all", "genders", "india", "united", "states", "audio", "video", "voice", "male",
    "female", "native", "bilingual", "multilingual", "project", "team", "and",
}


def _title_unknown_language_requirements(title: str, known: set[str]) -> set[str]:
    """Read a bound target-language slot without relying on a finite catalog."""
    text = _gate_text(title)
    match = _UNKNOWN_LANGUAGE_ROLE.search(text) or _LANGUAGE_WORK_ROLE.search(text)
    if match is None:
        return set()
    parts = [re.sub(r"\s+", " ", part).strip(" -'")
             for part in match.group("language").split("/")]
    parts = [part for part in parts if part]
    if not parts or any(
        part in _NON_LANGUAGE_SLOT or part in known or len(part.split()) > 2
        or set(part.split()) & _NON_LANGUAGE_WORDS
        for part in parts
    ):
        # A profile-known language, or not a language name at all: the
        # catalog-based readers handle the former.
        return set()
    return set(parts)


def _title_language_pairs(title: str, known: set[str]) -> set[str]:
    """Languages joined as a translation pair are cumulative requirements."""
    required: set[str] = set()
    for left, right in _LANG_PAIR.findall(_gate_text(title)):
        if left.lower() in known and right.lower() in known:
            required.update((left.lower(), right.lower()))
    return required


_LANG_ALIASES = {
    "bangla": "bengali",
    "filipino": "tagalog",
    "persian": "farsi",
    "sinhalese": "sinhala",
    "portugese": "portuguese",
}


def _canon_lang(lang: str) -> str:
    return _LANG_ALIASES.get(lang, lang)


def language_gate(title: str, profile: Profile) -> list[str]:
    """Reasons list; empty = pass."""
    pair_required = {
        _canon_lang(language)
        for language in _title_language_pairs(title, profile.known_languages)
    }
    required = {_canon_lang(l) for l in
                _title_language_requirements(title, profile.known_languages)}
    speaks = {_canon_lang(l) for l in profile.languages}
    if pair_required:
        missing = pair_required - speaks
        return [f"lang_mismatch:{lang}" for lang in sorted(missing)]
    if not required:
        return []
    # Pass if ANY required language is one the operator speaks (multi-language
    # postings often list alternatives; being eligible for one is enough).
    if required & speaks:
        return []
    return [f"lang_mismatch:{lang}" for lang in sorted(required)]


# --- credential gate ---------------------------------------------------------

def credential_gate(title: str, profile: Profile) -> list[str]:
    t = _gate_text(title)
    claimed = profile.claimed_domains()
    reasons: list[str] = []
    for name, pattern in profile.gated_role_patterns.items():
        m = re.search(pattern, t, flags=re.IGNORECASE)
        if not m:
            continue
        matched_text = m.group(0).lower()
        # Allow iff the demanded domain is one the operator actually holds
        # (add e.g. "chemistry" to profile.credential_domains and ChemE roles
        # start passing — no code change). Claiming a canonical domain also
        # authorizes its configured aliases, so "electronics" covers the
        # ngspice/pcb/vlsi surface forms of the same domain.
        if any(re.search(rf"\b{re.escape(dom)}\b", matched_text)
               for dom in claimed):
            continue
        reasons.append(f"credential_mismatch:{name}:{matched_text.strip()}")
    return reasons


# --- public API --------------------------------------------------------------

def check(title: str, profile: Profile) -> tuple[bool, list[str]]:
    reasons = language_gate(title, profile) + credential_gate(title, profile)
    return (len(reasons) == 0, reasons)
