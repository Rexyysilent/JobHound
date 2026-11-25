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

PROJECTION_VERSION = "evidence-v2"

_REMOTE_ROLE_CLAUSE = re.compile(
    r'^(?:'
    r'(?:this|the)\s+(?:current\s+)?(?:role|position|job|contract)\s+'
    r'(?:is|will\s+be|can\s+be|may\s+be)\s+'
    r'(?:on[- ]?site\s+or\s+)?(?:(?:fully|entirely|100\s*%)\s+)?remote\b|'
    r'(?:fully|100\s*%)\s+remote\s+(?:[a-z-]+\s+){0,3}'
    r'(?:role|position|job|work|contract)\b|'
    r'remote\s+(?:applicants|candidates)\b[^.!?;\n]{0,100}\b(?:may|can)\s+apply\b|'
    r'(?:fully\s+)?remote\s+(?:applicants|candidates)\s+(?:are\s+)?accepted\b|'
    r'(?:you|applicants|candidates|contractors)\s+(?:may|can|will)\s+'
    r'work\s+(?:remotely|from\s+(?:home|anywhere))\b'
    r')', re.I,
)
_REMOTE_LOCATION_CLAUSE = re.compile(
    r'^(?:(?:work(?:ing)?\s+)?(?:location|arrangement)\s*:\s*)?'
    r'(?:(?:fully|entirely|100\s*%)\s+)?remote\s+(?:in\s+)?'
    r'(?:india|worldwide|global(?:ly)?|anywhere|apac|asia)'
    r'(?:\s+only)?'
    r'(?:\s+(?:applicants|candidates)\s+(?:may|can)\s+apply)?$',
    re.I,
)
_REMOTE_TASK_CONTEXT = re.compile(
    r'^(?:(?:this|the)\s+(?:role|position|job|contract|project|assignment)\b|'
    r'(?:buyer|client)\s+(?:needs|requests|seeks)\b|'
    r'(?:job|role)\s+(?:description|duties|scope)\s*:)',
    re.I,
)


def _role_remote_clause(clause: str) -> bool:
    """Resolve a broad native flag only with a current role's remote terms."""
    if re.search(r'\b(?:not|never|no\s+longer)\s+'
                 r'(?:available|offered|supported|permitted|allowed|accepted|possible)\b',
                 clause, re.I):
        return False
    if _REMOTE_ROLE_CLAUSE.search(clause) or _REMOTE_LOCATION_CLAUSE.fullmatch(clause):
        return True
    # A compact trailing location label also occurs in scoped project prose.
    # Retain its subject: splitting every comma would promote company/team
    # boilerplate or the consequence of a negated or conditional statement.
    context, separator, label = clause.rpartition(',')
    return bool(separator and _REMOTE_LOCATION_CLAUSE.fullmatch(label.strip())
                and _REMOTE_TASK_CONTEXT.search(context)
                and not re.search(r'\b(?:not|never|no\s+longer|previously|formerly|'
                                  r'if|unless|might|would|company|teams?|culture)\b',
                                  context, re.I))


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
    native = str(payload.get('workplaceType') or '').casefold()
    ashby_remote = job.source == 'ashby' and payload.get('isRemote') is True
    for key in ('isRemote', 'job_is_remote'):
        if payload.get(key) is True:
            add('remote_or_hybrid' if key == 'isRemote' and ashby_remote else 'remote', key, 'true')
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
    if ashby_remote and native not in {'remote', 'hybrid', 'on-site', 'onsite'}:
        # A role-specific sentence can resolve the broad Ashby flag. Generic
        # remote-company boilerplate and quoted examples cannot establish it.
        role_text = re.sub(r'"[^"\n]*"|“[^”\n]*”', '.', job.description or '')
        for clause in re.split(r'[.!?;\n]', role_text):
            clause = re.sub(r'^[\s•*-]+', '', clause).strip()
            if _role_remote_clause(clause):
                add('remote', 'description', clause)
    explicit = {c.value for c in claims if c.value != 'remote_or_hybrid'}
    if len(explicit) > 1 or (ashby_remote and 'onsite' in explicit):
        state = 'conflicting'
    elif explicit:
        state = next(iter(explicit))
    elif job.is_remote and not ashby_remote:
        state = 'remote'
        add('remote', 'normalized.is_remote', 'true', 'inferred')
    else:
        # False in the legacy boolean means no detected remote signal, NOT onsite.
        state = 'unknown'
    caveats = ['Remote work does not establish applicant-country eligibility.']
    if ashby_remote and not explicit:
        caveats.append('Ashby isRemote includes hybrid roles; confirm workplaceType or explicit fully remote terms.')
    return EvidenceDimension(state=state, value=state, claims=claims,
        observation_ids=[observation_id] if observation_id else [],
        caveats=caveats)


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
        'credential_unverified:', 'requirement_unverified:', 'experience_unverified:', 'language_unverified:',
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
        'equipment': EvidenceDimension(
            state=('unknown' if any(fact['state'] in {'unmet', 'unknown', 'conflict'}
                for fact in assessment.requirements_assessment.equipment_compatibility) else
                'observed' if assessment.requirements_assessment.equipment_compatibility else 'not_stated'),
            value=assessment.requirements_assessment.equipment_compatibility,
            observation_ids=[fact['observation_id'] for fact in assessment.requirements_assessment.equipment_compatibility],
            caveats=['Inventory compatibility is separate from account status and assessment approval.']),
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
