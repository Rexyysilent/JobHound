"""Interpret captured posting deadlines without consulting the wall clock.

Date-only deadlines remain valid through the stated UTC calendar day. Naive
timestamps use UTC, matching the other captured timestamp fields in JobHound.
An invalid or absent deadline supplies no closure evidence.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re


DEADLINE_FIELDS = ("application_deadline", "validThrough", "deadline")


def deadline_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        try:
            return datetime.combine(value + timedelta(days=1), time.min, timezone.utc)
        except OverflowError:
            return None
    elif isinstance(value, str):
        value = value.strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return datetime.combine(date.fromisoformat(value) + timedelta(days=1),
                                        time.min, timezone.utc)
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def has_elapsed_deadline(payload: dict, as_of: datetime) -> bool:
    reference = as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of
    return any(deadline <= reference for field in DEADLINE_FIELDS
               if (deadline := deadline_timestamp(payload.get(field))) is not None)


def _captured_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


@dataclass(frozen=True)
class PostingStatus:
    deadline_values: tuple[object, ...] = ()
    deadline_observation_ids: tuple[str, ...] = ()
    closed_observation_ids: tuple[str, ...] = ()
    superseded_deadline_closure_ids: tuple[str, ...] = ()


def resolve_posting_status(records: Iterable[Mapping], as_of: datetime) -> PostingStatus:
    """Resolve saved public status/deadline evidence without operator policy.

    Each record supplies observation_id, source_kind, a normalized public url,
    identity_state, content_state, truncated, captured_at, vacancy_state and a
    raw_payload containing only deadline fields and optional _closure_evidence.
    This same pure function serves live decisions and saved-audit checks.

    A later complete original can replace an earlier deadline for the same
    posting. Copies cannot overrule originals. Tied/undated captures preserve
    uncertainty. A scoped closure notice never becomes a deadline revision.
    """
    now = _captured_at(as_of)
    if now is None:
        raise ValueError("posting status requires a frozen as-of timestamp")
    original_kinds = {"original_ats", "original_employer"}

    def payload(record):
        value = record.get("raw_payload")
        return value if isinstance(value, Mapping) else {}

    def key(record):
        return record.get("source_kind"), str(record.get("url") or "").rstrip("/")

    observed = [record for record in records if isinstance(record, Mapping)
                and record.get("identity_state") in {"exact", "corroborated"}
                and ((captured := _captured_at(record.get("captured_at"))) is None or captured <= now)]
    originals = [record for record in observed if record.get("source_kind") in original_kinds
                 and record.get("content_state") in {"complete", "role_complete"}
                 and not record.get("truncated")]
    groups = {}
    for record in originals or observed:
        if not any(deadline_timestamp(payload(record).get(field)) is not None for field in DEADLINE_FIELDS):
            continue
        groups.setdefault(key(record), []).append(record)
    current = []
    for group in groups.values():
        latest = max((captured for record in group
                      if (captured := _captured_at(record.get("captured_at"))) is not None), default=None)
        current.extend(record for record in group
                       if _captured_at(record.get("captured_at")) in {None, latest})

    original_open = any(record.get("vacancy_state") == "verified_open"
                        and not has_elapsed_deadline(payload(record), now) for record in originals)
    closed, superseded = [], []
    for record in observed:
        if record.get("vacancy_state") != "explicitly_closed":
            continue
        raw = payload(record)
        # A board's expired listing or closure notice applies to that copy.
        if original_open and record.get("source_kind") not in original_kinds:
            if has_elapsed_deadline(raw, now):
                superseded.append(str(record.get("observation_id") or ""))
            continue
        old_time = _captured_at(record.get("captured_at"))
        replaced = (not raw.get("_closure_evidence") and has_elapsed_deadline(raw, now)
                    and old_time is not None and any(
                        key(newer) == key(record) and newer.get("source_kind") in original_kinds
                        and newer.get("vacancy_state") == "verified_open"
                        and (new_time := _captured_at(newer.get("captured_at"))) is not None
                        and old_time < new_time <= now
                        and not has_elapsed_deadline(payload(newer), now)
                        for newer in current))
        (superseded if replaced else closed).append(str(record.get("observation_id") or ""))
    return PostingStatus(
        deadline_values=tuple(payload(record).get(field) for record in current for field in DEADLINE_FIELDS
                              if deadline_timestamp(payload(record).get(field)) is not None),
        deadline_observation_ids=tuple(str(record.get("observation_id") or "") for record in current),
        closed_observation_ids=tuple(closed),
        superseded_deadline_closure_ids=tuple(superseded),
    )
