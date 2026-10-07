"""Explicit closed-copy reissue, with shared presentation caps and no dispatch."""
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictInt,field_validator,model_validator

from .delivery_outbox import evaluated_row,transaction
from .handoff_signal import SIGNAL_SCHEMA,handoff_material_projection,_lane,_select_actions
from .review_audit import canonical_json
from .v41.provenance import sanitize_payload


class DraftSelection(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    envelope: str=Field(pattern=r'^[a-f0-9]{32}$')
    body_sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    intent_ids: list[StrictInt]=Field(min_length=1,max_length=100)


class ReissuePlan(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    schema_version: Literal[1]
    reviewed: StrictBool
    evidence_ref: str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$')
    source_state_sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    drafts: list[DraftSelection]=Field(default_factory=list,max_length=10)
    pending_intent_ids: list[StrictInt]=Field(default_factory=list,max_length=100)

    @field_validator('schema_version',mode='before')
    @classmethod
    def integer_version(cls,value):
        if type(value) is not int:raise ValueError('integer_reissue_schema_required')
        return value

    @field_validator('reviewed')
    @classmethod
    def explicit_review(cls,value):
        if value is not True:raise ValueError('literal_reissue_review_required')
        return value

    @model_validator(mode='after')
    def bounded_unique_selection(self):
        ids=self.pending_intent_ids+[i for draft in self.drafts for i in draft.intent_ids]
        if not 1<=len(ids)<=100 or len(set(ids))!=len(ids) or any(i<1 for i in ids):
            raise ValueError('bounded_unique_reissue_intents_required')
        if len({d.envelope for d in self.drafts})!=len(self.drafts):
            raise ValueError('unique_reissue_drafts_required')
        return self


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_reissue_json_key')
        result[key]=value
    return result


def read_reissue_plan(path,source_sha256):
    """Reject bad plans before creating an output directory; never echo content."""
    path=Path(path)
    if not path.is_file() or path.stat().st_size>65536:
        raise ValueError('bounded_reissue_plan_file_required')
    raw=path.read_bytes()
    if len(raw)>65536:raise ValueError('bounded_reissue_plan_file_required')
    try:
        plan=ReissuePlan.model_validate(json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_object))
    except (ValueError,UnicodeError):
        raise ValueError('invalid_reissue_plan') from None
    if plan.source_state_sha256!=source_sha256:
        raise ValueError('reissue_plan_source_changed')
    return plan


def prepare_reissue_selection(transport,result,report,plan,*,destinations,now):
    """Recover every explicitly reviewed current member, then cap the union.

    Reissue overflow stays pending as a linked generation. A later plan can
    select these exact unfrozen pending IDs, without draining any other backlog.
    The caller's transaction also covers rendering/freezing both destinations.
    """
    if not getattr(transport.box,'_handoff_review_enabled',False):
        raise ValueError('disposable_reissue_review_required')
    # Revalidate even an internal model instance; lists must not bypass bounds.
    plan=ReissuePlan.model_validate(plan.model_dump(mode='json'))
    box,conn=transport.box,transport.conn
    version=conn.execute("SELECT value FROM delivery_meta WHERE key='version'").fetchone()
    if version is None or version[0]!='2':
        raise ValueError('explicit_notification_generation_migration_required')
    targets={(d.channel,d.key):d for d in destinations}
    items={box.subject(item.canonical.canonical_id):item for item in result.evaluated}
    requested=[*plan.pending_intent_ids,*[i for draft in plan.drafts for i in draft.intent_ids]]

    def member(identity,*,unfrozen=False):
        row=box._owned(identity)
        item=items.get(row['subject'])
        latest=conn.execute('''SELECT id FROM delivery_intents WHERE revision=? AND channel=?
            AND destination=? ORDER BY generation DESC LIMIT 1''',
            (row['revision'],row['channel'],row['destination'])).fetchone()
        if ((row['channel'],row['destination']) not in targets or item is None
                or row['revision']!=report['revisions'][item.canonical.canonical_id]
                or latest is None or latest[0]!=identity or row['attempts']!=0
                or row['status'] not in {'pending','failed'}):
            raise ValueError('reissue_member_not_current_reviewed_subject')
        try:
            payload=json.loads(row['payload'])
            if (payload.get('signal_schema')!=SIGNAL_SCHEMA or payload.get('kind') not in {'handoff_card','handoff_status'}
                    or payload.get('signal_lane')!=_lane(item)
                    or handoff_material_projection(payload)!=handoff_material_projection(sanitize_payload(evaluated_row(item)))):
                raise ValueError('different_material')
        except (ValueError,TypeError,KeyError,AttributeError):
            raise ValueError('reissue_requires_current_handoff_material') from None
        if conn.execute('''SELECT 1 FROM delivery_baselines WHERE workspace=? AND profile=?
                AND subject=? AND channel=? AND destination=? AND revision=?''',
                (box.workspace,box.profile,row['subject'],row['channel'],row['destination'],row['revision'])).fetchone():
            raise ValueError('reissue_member_already_accepted')
        if unfrozen and not transport._preparable(row,targets[(row['channel'],row['destination'])],now):
            raise ValueError('requested_pending_intent_is_not_deliverable')
        return row

    with transaction(conn):
        # Every check runs against the new projection before any old draft is
        # abandoned. Omitted current members would otherwise become failed.
        for draft in plan.drafts:
            envelope=transport._owned(draft.envelope)
            members=transport._items(draft.envelope)
            current={i['id'] for i in members if i['revision']==box.current_revision(i['subject'])}
            if (envelope['body_hash']!=draft.body_sha256
                    or hashlib.sha256(envelope['body'].encode()).hexdigest()!=draft.body_sha256
                    or current!=set(draft.intent_ids)):
                raise ValueError('reissue_draft_body_or_current_members_changed')
            try:
                payloads=[json.loads(i['payload']) for i in members]
            except (ValueError,TypeError):
                raise ValueError('reissue_draft_contains_other_workflow') from None
            if any(not isinstance(p,dict) or p.get('signal_schema')!=SIGNAL_SCHEMA for p in payloads):
                raise ValueError('reissue_draft_contains_other_workflow')
        originals={identity:member(identity) for identity in requested}
        for identity in plan.pending_intent_ids:
            if conn.execute('SELECT 1 FROM delivery_envelope_items WHERE intent=?',(identity,)).fetchone():
                raise ValueError('requested_pending_intent_is_frozen')
        ids=[i for draft in plan.drafts for i in draft.intent_ids]
        replacements=transport.reissue_unsent([d.envelope for d in plan.drafts],ids,
            reviewed=True,evidence=plan.evidence_ref,now=now) if ids else []
        linked=dict(zip(ids,replacements))
        for identity in plan.pending_intent_ids:member(identity,unfrozen=True)
        requested_ids=[linked.get(identity,identity) for identity in requested]
        ledgers=[]
        for staged in report['destinations']:
            target=targets[(staged['channel'],staged['destination'])]
            proposed=list(dict.fromkeys(staged['intent_ids']+[i for i in requested_ids
                if (box._owned(i)['channel'],box._owned(i)['destination'])==(target.channel,target.key)]))
            ready={i for i in proposed if transport._preparable(box._owned(i),target,now)}
            opportunity_ids={};coverage=[];dispositions=dict(staged['dispositions']);holds=[]
            for identity in proposed:
                row=box._owned(identity)
                if identity not in ready:
                    holds.append(dict(intent=identity,reason='held_reserved_or_unavailable'))
                    if row['subject'] in items:
                        dispositions[items[row['subject']].canonical.canonical_id]='held_reserved_or_unavailable'
                    continue
                payload=json.loads(row['payload'])
                if payload.get('kind')=='coverage':coverage.append(identity);continue
                item=items.get(row['subject'])
                if item is None:raise ValueError('prepared_subject_absent_from_current_review')
                if row['subject'] in opportunity_ids:raise ValueError('duplicate_preparation_subject')
                opportunity_ids[row['subject']]=identity
            available=[items[subject] for subject in opportunity_ids if _lane(items[subject])!='status']
            selected,reasons,reservation=_select_actions(available,action_cap=5,verify_cap=2,total_cap=7)
            statuses=sorted((items[subject] for subject in opportunity_ids if _lane(items[subject])=='status'),
                key=lambda item:item.decision.priority_key.sort_tuple)
            dispositions.update(reasons)
            for n,item in enumerate(statuses):dispositions[item.canonical.canonical_id]='displayed_status' if n<3 else 'status_cap'
            selected+=statuses[:3]
            chosen=[opportunity_ids[box.subject(item.canonical.canonical_id)] for item in selected]
            counts=Counter(dispositions.values())
            if len(dispositions)!=len(result.evaluated) or sum(counts.values())!=len(result.evaluated):
                raise ValueError('reissue_presentation_ledger_failed')
            origins={i:('reissued_current_member' if i in replacements else
                'explicit_pending_member' if i in plan.pending_intent_ids else 'current_run_staging') for i in proposed}
            ledgers.append(dict(channel=target.channel,destination=target.key,intent_ids=chosen+coverage[:3],
                dispositions=dispositions,total=len(result.evaluated),counts=dict(counts),
                reservation=reservation,holds=holds,origins=origins,
                displayed=dict(Counter(_lane(item) for item in selected)),
                deferred_pending=[i for i in proposed if i in ready and i not in chosen+coverage[:3]],
                coverage_displayed=len(coverage[:3])))
        return dict(schema='jobhound-handoff-reissue/v1',evidence_ref=plan.evidence_ref,
            source_state_sha256=plan.source_state_sha256,plan_sha256=hashlib.sha256(canonical_json(plan.model_dump(mode='json')).encode()).hexdigest(),
            generations=[dict(original=i,replacement=linked[i],channel=originals[i]['channel'],
                subject=originals[i]['subject']) for i in ids],requested_pending_intents=plan.pending_intent_ids,
            draft_resolutions=[dict(envelope=d.envelope,body_sha256=d.body_sha256,
                original_current_intents=d.intent_ids,replacements=[linked[i] for i in d.intent_ids],
                status=transport._owned(d.envelope)['status']) for d in plan.drafts],
            destinations=ledgers,provider_calls=0,baseline_consumed=False)
