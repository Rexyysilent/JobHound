"""Bounded webcam compatibility from explicit inventory and original role text."""
from dataclasses import asdict
import re

from .extract import extract_sections
from .models import SourceKind

_ORIGINAL = {SourceKind.ORIGINAL_ATS, SourceKind.ORIGINAL_EMPLOYER, SourceKind.EMAIL_OFFER}
_WEBCAM = re.compile(r"\b(?:web\s*cams?|computer\s+cameras?)\b", re.I)
_ASSESSMENT = re.compile(r"\b(?:assessment|test|interview|screening|proctor(?:ing|ed)?)\b", re.I)
_NEGATED = re.compile(r"\b(?:not\s+(?:required|necessary|needed|mandatory)|no\s+web\s*cam\s+(?:is\s+)?(?:required|needed))\b", re.I)
_PREFERRED = re.compile(r"\b(?:preferred|optional|nice\s+to\s+have|bonus)\b", re.I)
_REQUIRED = re.compile(r"\b(?:must|required|mandatory|need(?:ed)?|necessary)\b", re.I)
_SHARED_DEVICE_LIST = re.compile(r"\b(?:web\s*cam|microphone|smartphone|phone)\s*(?:,|\band\b|\bor\b)\s*(?:a\s+)?(?:web\s*cam|microphone|smartphone|phone)\b", re.I)
_ALTERNATIVE_DEVICE = re.compile(r"\bweb\s*cam\s+or\s+(?:a\s+)?(?:microphone|smartphone|phone)\b|\b(?:microphone|smartphone|phone)\s+or\s+(?:a\s+)?web\s*cam\b", re.I)


def _equipment_segments(text):
    for sentence in re.split(r"[\n;•]+|\bbut\b|(?<=[.!?])\s+", text, flags=re.I):
        # Preserve a short coordinated device list's shared trailing modifier.
        # Otherwise conjunctions separate independently qualified hardware.
        if _SHARED_DEVICE_LIST.search(sentence):
            yield sentence
        else:
            yield from re.split(r",|\band\b", sentence, flags=re.I)


def assess_equipment(canonical, profile, as_of):
    """Project per-role/assessment demand without manufacturing device facts.

    No active explicit inventory is the legacy opt-out. Undated/conflicting
    inventory remains unknown; later dated webcam ownership can resolve absence.
    """
    cutoff = as_of.date().isoformat()
    active = [record for record in profile.equipment_evidence
              if record.event_date is None or record.event_date <= cutoff]
    if not active:
        return []
    webcam = [record for record in active if record.device == "webcam"]
    dated = [record for record in webcam if record.event_date is not None]
    latest = max((record.event_date for record in dated), default=None)
    selected = [record for record in webcam if record.event_date in {None, latest}]
    values = {record.available for record in selected}
    inventory = ("unknown" if not selected or None in values or any(record.event_date is None for record in selected)
                 else "conflict" if len(values) > 1
                 else "available" if values == {True} else "unavailable")
    key = lambda record: (record.device, record.event_date or "", record.evidence_id, record.source)
    projection = []
    for observation in canonical.observations:
        if (observation.job is None or observation.source_kind not in _ORIGINAL
            or observation.identity_state not in {"exact", "corroborated"}
            or observation.content_state not in {"complete", "role_complete"} or observation.truncated):
            continue
        sections = extract_sections(observation.job.description or "", native_source=observation.job.source)
        # The full body contributes only explicitly required/preferred/negated
        # clauses. Requirement sections also support a plain hardware bullet.
        texts = [(sections.requirements, "requirements", True),
                 (observation.job.description or "", "description", False)]
        seen = set()
        for text, source_field, default_required in texts:
            for segment in _equipment_segments(text):
                span = segment.strip()
                if not _WEBCAM.search(span) or span in seen:
                    continue
                applicant = re.search(r"\b(?:you|applicants?|candidates?)\b", span, re.I)
                if not applicant and re.search(r"\b(?:our|the)\s+(?:clients?|customers?|company)\s+(?:use|uses|own|owns|have|has|provide|provides)\b|\b(?:we\s+provide|provided\s+by|supplied\s+by)\b", span, re.I):
                    continue
                modality = ("explicitly_not_required" if _NEGATED.search(span) else
                            "preferred" if _PREFERRED.search(span) else
                            "required" if default_required or _REQUIRED.search(span) else "unknown")
                if modality == "unknown":
                    continue
                seen.add(span)
                state = ("not_required" if modality == "explicitly_not_required" else
                         "preferred" if modality == "preferred" else
                         "supported" if inventory == "available" else
                         "unknown" if _ALTERNATIVE_DEVICE.search(span) else
                         "unmet" if inventory == "unavailable" else inventory)
                projection.append(dict(device="webcam", scope="assessment" if _ASSESSMENT.search(span) else "role",
                    modality=modality, state=state, observation_id=observation.observation_id,
                    source_field=source_field, span=span[:500],
                    evidence=[asdict(record) for record in sorted(active, key=key)],
                    selected_evidence_ids=sorted(record.evidence_id for record in selected)))
    return projection
