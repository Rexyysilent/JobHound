"""Explicit reviewed canonical migrations anchored to retained observations.

Aliases never match a company/title or select an attempt. Held bindings keep
their journals and projections but cannot refine any current job's action.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .outcomes import StrictModel
from .provenance import public_url, sanitize_payload


class CanonicalAlias(StrictModel):
    schema_version: Literal[1]
    alias_id: str = Field(min_length=1, max_length=160, pattern=r'^[\w.:-]+$')
    from_canonical_id: str = Field(min_length=1, max_length=160, pattern=r'^[\w.:-]+$')
    to_canonical_id: str = Field(min_length=1, max_length=160, pattern=r'^[\w.:-]+$')
    observation_id: str = Field(min_length=1, max_length=160)
    role_url: str = Field(min_length=1, max_length=2048)
    observation_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    reviewed: Literal[True]

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError('integer_schema_version_required')
        return value

    @model_validator(mode='after')
    def public_reference(self):
        from .hydration import _safe_public_http
        if not _safe_public_http(self.role_url) or public_url(self.role_url) != self.role_url:
            raise ValueError('public_role_reference_required')
        return self


def observation_binding_hash(observation):
    """Canonical hash of the exact sanitized observation, including native data."""
    payload = sanitize_payload(observation.model_dump(mode='json'))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def resolve_alias_bindings(result, bindings, records):
    """One-hop explicit migrations; ambiguity holds every affected selection."""
    if not isinstance(records, list) or len(records) > 5000:
        raise ValueError('alias_record_limit')
    index = {row.canonical.canonical_id: row for row in result.evaluated}
    by_source = defaultdict(list)
    owners = defaultdict(set)
    for row in result.evaluated:
        for observation in row.canonical.observations:
            owners[observation.observation_id].add(row.canonical.canonical_id)
    parsed, quarantine = [], []
    for line, record in enumerate(records, 1):
        try:
            if isinstance(record, CanonicalAlias):
                record = record.model_dump(mode='json', warnings=False)
            alias = CanonicalAlias.model_validate(record)
            parsed.append((line, alias))
        except (ValueError, TypeError):
            quarantine.append({'line': line, 'code': 'invalid_alias_schema'})
    ids = Counter(alias.alias_id for _, alias in parsed)
    sources = Counter(alias.from_canonical_id for _, alias in parsed)
    bound_sources = {binding.canonical_id for binding in bindings}
    accepted = {}
    for line, alias in parsed:
        target = index.get(alias.to_canonical_id)
        code = None
        if ids[alias.alias_id] > 1 or sources[alias.from_canonical_id] > 1:
            code = 'ambiguous_alias_mapping'
        elif alias.from_canonical_id in index:
            code = 'alias_source_still_current'
        elif alias.from_canonical_id not in bound_sources:
            code = 'unused_alias_source'
        elif target is None:
            code = 'unknown_alias_target'
        elif owners.get(alias.observation_id) != {alias.to_canonical_id}:
            code = 'alias_observation_owner_ambiguous'
        else:
            matched = [o for o in target.canonical.observations if o.observation_id == alias.observation_id]
            if not matched or any(not o.job or public_url(o.job.url).rstrip('/') != alias.role_url.rstrip('/') for o in matched):
                code = 'alias_url_mismatch'
            else:
                try:
                    if any(observation_binding_hash(o) != alias.observation_sha256 for o in matched):
                        code = 'alias_observation_changed'
                except (ValueError, TypeError, UnicodeError):
                    code = 'alias_observation_unhashable'
        if code:
            quarantine.append({'line': line, 'code': code})
        else:
            accepted[alias.from_canonical_id] = alias
    resolutions = []
    for binding in bindings:
        alias = accepted.get(binding.canonical_id)
        destination = binding.canonical_id if binding.canonical_id in index else alias.to_canonical_id if alias else None
        code = None
        if destination is None:
            code = 'unresolved_canonical_alias'
        elif alias and (binding.observation_id != alias.observation_id or binding.role_url.rstrip('/') != alias.role_url.rstrip('/')):
            code = 'alias_binding_anchor_mismatch'
        else:
            matched = [o for o in index[destination].canonical.observations if o.observation_id == binding.observation_id]
            if not matched or any(not o.job for o in matched):
                code = 'unknown_binding_observation'
            elif any(public_url(o.job.url).rstrip('/') != binding.role_url.rstrip('/') for o in matched):
                code = 'binding_url_mismatch'
        row = {'scope': binding.scope.model_dump(mode='json'), 'canonical_id': binding.canonical_id,
               'resolved_canonical_id': None if code else destination, 'active': binding.active,
               'held_target_canonical_id': None,
               'alias_id': alias.alias_id if alias else None, 'state': 'held' if code else 'resolved', 'reason': code}
        resolutions.append(row)
        if not code and binding.active:
            by_source[destination].append(row)
    for selections in by_source.values():
        if len(selections) > 1:
            for resolution in selections:
                resolution.update(held_target_canonical_id=resolution['resolved_canonical_id'],
                                  resolved_canonical_id=None, state='held', reason='multiple_active_attempts_for_one_canonical')
    report = {'accepted': [alias.model_dump(mode='json') for alias in sorted(accepted.values(), key=lambda a: a.alias_id)],
              'quarantine': sorted(quarantine, key=lambda row: row['line']),
              'counts': {'input': len(records), 'accepted': len(accepted), 'quarantined': len(quarantine)},
              'binding_resolutions': resolutions}
    assert report['counts']['input'] == report['counts']['accepted'] + report['counts']['quarantined']
    return index, report
