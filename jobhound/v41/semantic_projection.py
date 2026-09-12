"""Thin review projection over production semantic extractors."""
from __future__ import annotations
import re
from dataclasses import replace
from typing import Any
from ..filters.eligibility import default_profile
from ..config import CONFIG
from ..models import Job
from .documents import classify_offer
from .extract import extract_pay_candidates
from .models import CanonicalJob, ListingObservation, SourceKind
from .requirements import assess_requirements
from .roles import assess_role, classify_role_semantics

_LANG_ALIAS = {"bn": "bengali", "hi": "hindi", "en": "english", "nl": "dutch", "de": "german"}

def _profile(data: dict[str, Any]):
    base = default_profile()
    languages = {_LANG_ALIAS.get(str(x).casefold(), str(x).casefold()) for x in data.get("working_languages", base.languages)}
    return replace(base, languages=languages, working_languages=languages,
        capability_domains={str(x).casefold() for x in data.get("declared_capability_domains", base.capability_domains)},
        formal_credentials={str(x).casefold() for x in data.get("formal_credentials", []) if not str(x).startswith("no_")},
        absent_formal_credentials={
            str(x).casefold() for x in data.get("absent_formal_credentials", [])
        } | {
            str(key).casefold() for key, value in data.items()
            if value is False and str(key).casefold() in {
                "medical_licence", "tax_professional", "university_degree"
            }
        },
        documented_professional_years={str(k).casefold(): float(v) for k, v in data.get("documented_professional_years", {}).items()})

def project_semantic_case(case_input: dict[str, Any]) -> dict[str, Any]:
    profile_data = dict(case_input.get("profile") or {})
    profile_data.update(case_input.get("profile_patch") or {})
    profile = _profile(profile_data)
    title = str(case_input.get("title") or "")
    html = str(case_input.get("description_html") or "") + " " + str(case_input.get("append_text") or "")
    description = re.sub(r"<[^>]+>", "\n", html)
    try:
        source_kind = SourceKind(str(case_input.get("source_kind") or "original_ats"))
    except ValueError:
        source_kind = SourceKind.UNKNOWN
    job = Job(source="semantic_fixture", title=title, company="Fixture",
        url=str(case_input.get("opportunity_url") or "https://example.invalid/fixture"),
        description=description, location=case_input.get("applicant_location"), pay_raw=case_input.get("pay_raw"))
    observation = ListingObservation(observation_id="fixture", source="semantic_fixture", job=job,
        source_kind=source_kind, content_state=str(case_input.get("content_state") or "role_complete"),
        identity_state=str(case_input.get("identity_match") or "uncertain"))
    canonical = CanonicalJob(canonical_id="semantic-fixture", job=job, observations=[observation],
        field_sources={"title": "fixture", "description": "fixture", "url": "fixture"})
    prior_enabled = CONFIG.v55.enabled
    CONFIG.v55.enabled = True
    try:
        requirements = assess_requirements(canonical, profile)
        pay_candidates, selected_pay, pay_conflict = extract_pay_candidates(canonical)
    finally:
        CONFIG.v55.enabled = prior_enabled
    semantics = classify_role_semantics(title, description, job.url, profile)
    role = assess_role(canonical, [], {}, profile)
    return {"requirements": {"mandatory_languages": requirements.language_required,
            "credential_tags": requirements.credentials_required,
            "required_years": {c.scope: float(c.value) for c in requirements.experience if c.modality in {"required", "minimum", "mandatory"}},
            "claims": requirements.claims},
        "location": {"excluded": sorted({str(v) for c in requirements.location_scope if c.polarity == "negative" for v in c.value})},
        "document": classify_offer(canonical), "role": semantics, "role_assessment": role,
        "pay": selected_pay, "pay_candidates": pay_candidates, "pay_conflict": pay_conflict}
