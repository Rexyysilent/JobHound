"""Reviewed, scoped outcome facts. Pure projection; no mail, network or stores.

This first slice deliberately accepts only the predicates needed for application,
assessment, access and buyer-scope review. It cannot establish collected payment.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

MAX_JOURNAL_EVENTS = 5000

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    @field_validator('reviewed', check_fields=False, mode='before')
    @classmethod
    def explicit_review(cls, value):
        if value is not True:
            raise ValueError('explicit_review_required')
        return value


class OutcomeScope(StrictModel):
    provider: str = Field(min_length=1, max_length=80, pattern=r'^[\w.-]+$')
    portal: str = Field(min_length=1, max_length=80, pattern=r'^[\w.-]+$')
    role: str = Field(min_length=1, max_length=160, pattern=r'^[\w.:-]+$')
    account: str = Field(min_length=1, max_length=80, pattern=r'^[\w.-]+$')
    # Opaque local IDs, never email addresses. Public role identity is not an attempt.
    attempt: str = Field(min_length=1, max_length=80, pattern=r'^[\w.-]+$')

    def key(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(',', ':'))


class OutcomeBinding(StrictModel):
    scope: OutcomeScope
    canonical_id: str = Field(min_length=1, max_length=160)
    observation_id: str = Field(min_length=1, max_length=160)
    role_url: str = Field(min_length=1, max_length=2048)
    reviewed: Literal[True]


class ActionChecks(StrictModel):
    privacy: StrictBool | None = None
    schedule: StrictBool | None = None
    equipment: StrictBool | None = None
    cost: StrictBool | None = None


class ActionCost(StrictModel):
    minutes: float | None = Field(default=None, ge=0, le=10000)
    cash: float | None = Field(default=None, ge=0, le=100000)
    currency: str = Field(default='USD', pattern=r'^[A-Z]{3}$')
    basis: Literal['measured', 'user_estimate', 'provider_estimate']


_ENUMS = {
    'application_state': {'submitted', 'rejected', 'withdrawn', 'reopened'},
    'assessment_state': {'invited', 'completed', 'passed'},
    'project_access': {'blocked', 'accessible'},
    'buyer_reply': {'substantive', 'scope_changed'},
}
_PREDICATES = {*_ENUMS, 'scope_revision', 'agreed_scope_revision',
               'assessment_route', 'action_checks', 'action_cost'}


class OutcomeEvent(StrictModel):
    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=160, pattern=r'^[\w.:-]+$')
    scope: OutcomeScope
    predicate: str
    value: str | ActionChecks | ActionCost
    actor: Literal['user', 'provider', 'buyer']
    source_kind: Literal['user_report', 'mail', 'community', 'reviewed_seed']
    evidence_ref: str = Field(min_length=1, max_length=160, pattern=r'^[\w.:-]+$')
    support_path: str = Field(min_length=1, max_length=160, pattern=r'^[\w./:\[\]-]+$')
    support_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    event_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    extraction_version: str = Field(min_length=1, max_length=80)
    confidentiality: Literal['private']
    reviewed: Literal[True]
    supersedes: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('integer_schema_version_required')
        return value

    @model_validator(mode='after')
    def supported_fact(self):
        if self.predicate not in _PREDICATES:
            raise ValueError('unsupported_predicate')
        if self.predicate in _ENUMS and (
            not isinstance(self.value, str) or self.value not in _ENUMS[self.predicate]
        ):
            raise ValueError('unsupported_value')
        expected = {'action_checks': ActionChecks, 'action_cost': ActionCost}
        if self.predicate in expected and not isinstance(self.value, expected[self.predicate]):
            raise ValueError('wrong_value_type')
        if self.predicate.endswith('scope_revision'):
            import re
            if not isinstance(self.value, str) or not re.fullmatch(r'[\w.-]{1,80}', self.value):
                raise ValueError('invalid_scope_revision')
        if self.predicate == 'assessment_route':
            if not isinstance(self.value, str):
                raise ValueError('invalid_route')
            parts = urlsplit(self.value)
            from .hydration import _safe_public_http
            # Routes are public references only; authenticated links stay outside imports.
            if (parts.scheme != 'https' or not _safe_public_http(self.value)
                    or parts.query or parts.fragment or any(ord(c) < 32 for c in self.value)):
                raise ValueError('private_or_unsafe_route')
        if self.event_at and self.event_at > self.observed_at:
            raise ValueError('event_after_observation')
        if self.expires_at and self.expires_at <= (self.event_at or self.observed_at):
            raise ValueError('invalid_expiry')
        if self.source_kind == 'user_report' and self.actor != 'user':
            raise ValueError('actor_source_mismatch')
        if self.source_kind == 'community' and (self.actor != 'buyer' or self.predicate not in {
            'buyer_reply', 'scope_revision', 'agreed_scope_revision'
        }):
            raise ValueError('community_cannot_establish_account_state')
        if self.predicate in {'buyer_reply', 'scope_revision'} and self.actor != 'buyer':
            raise ValueError('buyer_fact_requires_buyer')
        if self.predicate in {'assessment_route'} and self.actor != 'provider':
            raise ValueError('provider_route_required')
        if self.predicate == 'action_checks' and self.actor != 'user':
            raise ValueError('checks_require_user_review')
        return self


class OutcomeProjection(StrictModel):
    scope: OutcomeScope
    events: list[OutcomeEvent]
    current: dict[str, list[str]]
    conflicts: list[str]
    dispositions: dict[str, str]

    def facts(self, predicate: str) -> list[OutcomeEvent]:
        ids = set(self.current.get(predicate, []))
        return [e for e in self.events if e.event_id in ids]

    def value(self, predicate: str):
        facts = self.facts(predicate)
        return facts[0].value if facts and predicate not in self.conflicts else None


def _encoded(event: OutcomeEvent) -> str:
    return event.model_dump_json()


def project(events: list[OutcomeEvent], scope: OutcomeScope) -> OutcomeProjection:
    """Predicate-local supersession; incompatible actors/times remain conflicts."""
    rows = sorted([e for e in events if e.scope == scope], key=lambda e: e.event_id)
    grouped: dict[str, list[OutcomeEvent]] = defaultdict(list)
    for event in rows:
        grouped[event.predicate].append(event)
    current, conflicts, dispositions = {}, [], {}
    for predicate, facts in sorted(grouped.items()):
        removed = {old for e in facts for old in e.supersedes}
        for old in facts:
            for new in facts:
                same_authority = (old.actor, old.source_kind) == (new.actor, new.source_kind)
                terminal = predicate == 'application_state' and old.value in {'rejected', 'withdrawn'}
                if (same_authority and old.event_at and new.event_at and new.event_at > old.event_at
                        and not terminal and old.event_id not in new.supersedes):
                    removed.add(old.event_id)
        active = [e for e in facts if e.event_id not in removed]
        values = {json.dumps(e.model_dump(mode='json')['value'], sort_keys=True) for e in active}
        conflict = len(values) > 1
        if conflict:
            conflicts.append(predicate)
        current[predicate] = [e.event_id for e in active]
        for event in facts:
            dispositions[event.event_id] = ('superseded' if event.event_id in removed else
                'conflict' if conflict else 'current')
    return OutcomeProjection(scope=scope, events=rows, current=current,
                             conflicts=conflicts, dispositions=dispositions)


def import_events(records: list[dict], bindings: list[OutcomeBinding], as_of: datetime,
                  previous: list[OutcomeEvent] = ()) -> dict:
    """Validate without echoing rejected content. Prior accepted facts are immutable.

    A conflicting ID in one new batch quarantines ALL variants, independent of
    input order. A conflicting re-import cannot overwrite a previous journal.
    """
    if as_of.tzinfo is None:
        raise ValueError('as_of_requires_timezone')
    scopes = {b.scope.key() for b in bindings}
    if len(scopes) != len(bindings):
        raise ValueError('duplicate_binding_scope')
    # A supplied journal is input, not a privileged bypass around validation.
    if previous:
        checked = import_events([e.model_dump(mode='json') for e in previous], bindings, as_of)
        if checked['counts']['quarantined'] or checked['counts']['duplicate']:
            raise ValueError('invalid_previous_journal')
    known = {e.event_id: e for e in previous}
    if len(known) != len(previous):
        raise ValueError('duplicate_previous_event_id')
    batch: dict[str, list[tuple[int, OutcomeEvent]]] = defaultdict(list)
    quarantine, duplicates = [], 0
    def reject(line, code):
        quarantine.append({'line': line, 'code': code})
    for line, raw in enumerate(records, 1):
        try:
            event = OutcomeEvent.model_validate(raw)
        except (ValueError, TypeError):
            reject(line, 'invalid_event_schema')
            continue
        if event.scope.key() not in scopes:
            reject(line, 'unmatched_scope')
        elif event.observed_at > as_of or (event.event_at and event.event_at > as_of):
            reject(line, 'future_evidence')
        else:
            batch[event.event_id].append((line, event))
    candidates = {}
    for identity, group in sorted(batch.items()):
        signatures = {_encoded(e) for _, e in group}
        prior = known.get(identity)
        if len(signatures) > 1 or (prior and _encoded(prior) not in signatures):
            for line, _ in group:
                reject(line, 'event_id_collision')
        elif prior:
            duplicates += len(group)
        else:
            candidates[identity] = group
    # Validate the entire supersession graph, then cascade invalid predecessors.
    pending = {identity: group[0][1] for identity, group in candidates.items()}
    while True:
        invalid = []
        available = {**known, **pending}
        for identity, event in pending.items():
            for old_id in event.supersedes:
                old = available.get(old_id)
                if (old is None or old_id == identity or old.scope != event.scope
                    or old.predicate != event.predicate or event.observed_at <= old.observed_at
                    or (old.event_at and event.event_at and event.event_at <= old.event_at)
                    or (event.source_kind == 'reviewed_seed' and old.source_kind != 'reviewed_seed')):
                    invalid.append(identity)
                    break
        if not invalid:
            break
        for identity in invalid:
            pending.pop(identity)
            for line, _ in candidates[identity]:
                reject(line, 'invalid_supersession')
    accepted = list(pending.values())
    duplicates += sum(len(candidates[e.event_id])-1 for e in accepted)
    journal = sorted([*known.values(), *accepted], key=lambda e: e.event_id)
    if len(journal) > MAX_JOURNAL_EVENTS:
        raise ValueError('outcome_journal_limit')
    return {'events': journal, 'projections': [project(journal, b.scope) for b in bindings],
            'quarantine': sorted(quarantine, key=lambda q: q['line']),
            'counts': {'input': len(records), 'accepted': len(accepted),
                       'duplicate': duplicates, 'quarantined': len(quarantine)}}


def projection_hash(projection: OutcomeProjection) -> str:
    return hashlib.sha256(projection.model_dump_json().encode()).hexdigest()


def compatibility_state(projection: OutcomeProjection) -> dict:
    mapping = {'application_state': 'application_state', 'assessment_state': 'assessment_state',
               'project_access': 'project_access', 'assessment_route': 'assessment_route'}
    state = {target: projection.value(source) for source, target in mapping.items()
             if projection.value(source) is not None}
    if state.get('application_state') == 'submitted':
        state['application_state'] = 'applied'
    # No synthetic observed_at: action freshness is checked against its own facts.
    return state
