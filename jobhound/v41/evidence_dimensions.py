"""Independent, lossless evidence views over existing assessments.

No ranking weights or cutoffs live here. Raw observations remain available;
claims are source statements, never promises of employment or verified income.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..filters.listing_quality import BOARD_DOMAINS, domain_tier
from ..normalize import company_domain_from_job_url
from .models import Evidence, EvidenceDimension, SourceKind

PROJECTION_VERSION = "evidence-v1"


def public_page_kind(url: str) -> SourceKind:
    """A parseable document cannot authenticate its own publisher/employer."""
    host = (urlsplit(url).hostname or "").casefold()
    # Board identity outranks any embedded hiringOrganization assertion.
    if any(host == d or host.endswith('.' + d) for d in BOARD_DOMAINS):
        return SourceKind.REPUTABLE_BOARD
    tier, _ = domain_tier(url)
    if tier == 'farm':
        return SourceKind.CONTENT_FARM
    if tier == 'ats':
        return SourceKind.ORIGINAL_ATS
    if tier == 'employer' or company_domain_from_job_url(url):
        return SourceKind.ORIGINAL_EMPLOYER
    return SourceKind.UNKNOWN


def work_arrangement(job, payload: dict, observation_id: str = "", source_kind=None):
    claims = []

    def add(value, field, span, modality='explicit'):
        claims.append(Evidence(dimension='work_arrangement', code='work_arrangement_claim',
            value=value, source_field=field, span=str(span), observation_id=observation_id,
            source_kind=source_kind, modality=modality,
            confidence=0.5 if modality == 'inferred' else 1.0))

    values = payload.get('jobLocationType', [])
    values = values if isinstance(values, list) else [values]
    for value in values:
        if str(value).upper() == 'TELECOMMUTE':
            add('remote', 'jobLocationType', value)
    for key in ('isRemote', 'job_is_remote'):
        if payload.get(key) is True:
            add('remote', key, 'true')
    native = str(payload.get('workplaceType') or '').casefold()
    if native in {'remote', 'hybrid', 'on-site', 'onsite'}:
        add('onsite' if native in {'on-site', 'onsite'} else native, 'workplaceType', native)
    # Only role-scoped text; a company having remote employees proves nothing.
    for field, text in (('title', job.title), ('location', job.location or '')):
        if re.search(r'\bhybrid\b', text, re.I):
            add('hybrid', field, text)
        elif re.search(r'\b(?:not remote|non[- ]remote|on[- ]?site|in[- ]office)\b', text, re.I):
            add('onsite', field, text)
        elif re.search(r'\b(?:remote(?!\s+(?:sensing|control|desktop|access)\b)|work from home)\b', text, re.I):
            add('remote', field, text)
    explicit = {c.value for c in claims}
    if len(explicit) > 1:
        state = 'conflicting'
    elif explicit:
        state = next(iter(explicit))
    elif job.is_remote:
        state = 'remote'
        add('remote', 'normalized.is_remote', 'true', 'inferred')
    else:
        # False in the legacy boolean means no detected remote signal, NOT onsite.
        state = 'unknown'
    return EvidenceDimension(state=state, value=state, claims=claims,
        observation_ids=[observation_id] if observation_id else [],
        caveats=['Remote work does not establish applicant-country eligibility.'])


def project_observation(observation):
    """Re-derive views from saved evidence, without rewriting input snapshots."""
    if observation.job is None:
        return observation
    row = observation.model_copy(deep=True)
    if row.origin == 'hydration' and row.source == 'first_party':
        row.source_kind = public_page_kind(row.original_url or row.job.url)
    row.work_arrangement = work_arrangement(row.job, row.raw_payload,
                                           row.observation_id, row.source_kind)
    if row.origin == 'hydration' and row.source == 'first_party':
        if row.work_arrangement.state == 'remote':
            row.job.is_remote = True
        elif row.work_arrangement.state in {'onsite', 'hybrid'}:
            row.job.is_remote = False
    row.evidence_projection_version = PROJECTION_VERSION
    return row


def describe_evidence(canonical, assessment):
    """Independent views: unknown economics never erases relevance."""
    best = canonical.best_observation
    ids = [o.observation_id for o in canonical.observations if o.job is not None]
    complete = any(o.content_state in {'complete', 'role_complete'} for o in canonical.observations)
    eligibility_blocks = [b for b in assessment.blockers if b.startswith(('location:', 'language:', 'credentials:'))]
    eligibility_unknowns = [u for u in assessment.unresolved if u.startswith((
        'credential_unverified:', 'requirement_unverified:', 'experience_unverified:',
        'specialist_', 'location_scope_', 'technical_requirements_', 'requirements_not_'))]
    eligibility_complete = assessment.requirements_assessment.completeness == 'complete'
    eligibility = 'failed' if eligibility_blocks else 'unknown' if eligibility_unknowns or not eligibility_complete else 'passed'
    pay = assessment.selected_pay
    economics = ('conflicting' if assessment.pay_conflict else
                 'estimated' if pay and pay.estimated else
                 'claimed' if pay else 'not_stated' if complete else 'unknown')
    unavailable = {o.retrieval_error or o.resolution_error for o in canonical.observations}
    content = ('complete' if complete else 'extraction_unsupported' if 'jobposting_not_found' in unavailable else
               'retrieval_blocked' if unavailable.intersection({'http_403', 'http_429', 'host_cooldown'}) else 'partial')
    return {
        'publisher': EvidenceDimension(state='observed', value={
            'host': best.publisher_domain or urlsplit(best.job.url).hostname,
            'source_kind': best.source_kind.value, 'claimed_employer': canonical.job.company},
            observation_ids=[best.observation_id], caveats=['Publisher identity and claimed employer are distinct.']),
        'content': EvidenceDimension(state=content, observation_ids=ids),
        'relevance': EvidenceDimension(state=assessment.match_strength.value,
            value={'matched_concepts': assessment.matched_concepts, 'role_families': assessment.role_families},
            claims=list(assessment.role_assessment.evidence), observation_ids=ids),
        'eligibility': EvidenceDimension(state=eligibility,
            value={'blockers': eligibility_blocks, 'unresolved': eligibility_unknowns},
            claims=[e for e in assessment.evidence if e.dimension in {'language', 'experience', 'degree', 'domain', 'tool', 'location'}],
            observation_ids=ids, caveats=['Does not establish selection, account access or task allocation.']),
        'work_arrangement': best.work_arrangement,
        'economics': EvidenceDimension(state=economics,
            value=pay.model_dump(mode='json') if pay else None,
            claims=[Evidence(dimension='pay', code='publisher_pay_claim', value=c.raw,
                span=c.raw, source_field=c.source_field, observation_id=c.observation_id,
                source_kind=c.observation_source_kind) for c in assessment.pay_candidates],
            observation_ids=list(dict.fromkeys(c.observation_id for c in assessment.pay_candidates)),
            caveats=['Stated pay is not verified earnings; copied claims are not independent confirmation.']),
        'readiness': EvidenceDimension(state=assessment.action_readiness,
            value={'next_action': assessment.next_action, 'next_step': assessment.next_step,
                   'verified_open_at': assessment.verified_open_at.isoformat() if assessment.verified_open_at else None},
            observation_ids=ids),
    }
