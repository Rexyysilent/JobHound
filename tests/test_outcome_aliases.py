"""Exact retained-observation migrations and scoped historical outcomes."""
from copy import deepcopy
import hashlib
import json

import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_observations, decision_fingerprint
from jobhound.v41.models import SourceKind
from jobhound.v41.outcome_aliases import observation_binding_hash
from jobhound.v41.outcome_review import preview_outcomes, run_outcome_review
from jobhound.v41.outcomes import OutcomeBinding, OutcomeScope
from jobhound.v41.provenance import canonicalize_observations
from jobhound.v41.resolve import _collapse_exact_final_urls
from test_v55_action_policy import observation, NOW
from test_v57_outcomes import event, assessment_events


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    monkeypatch.setattr(CONFIG.v55,'account_states',[])


def scoped_observation(key,url,kind=SourceKind.ORIGINAL_ATS):
    row=observation(key,source_kind=kind)
    row.job.url=row.original_url=row.normalized_url=url
    return row


@pytest.mark.parametrize('kind',[SourceKind.ORIGINAL_ATS,SourceKind.ORIGINAL_EMPLOYER,SourceKind.AGGREGATOR_UNRESOLVED,SourceKind.UNKNOWN])
@pytest.mark.parametrize('pair',[
    ('https://jobs.example.org/Role','https://jobs.example.org/role'),
    ('https://jobs.example.org/role?req=AbC','https://jobs.example.org/role?req=abc'),
    ('https://jobs.example.org/role?Req=abc','https://jobs.example.org/role?req=abc'),
    ('https://jobs.example.org/role-one','https://jobs.example.org/role-two'),
])
def test_distinct_requisition_urls_survive_clustering_and_final_url_resolution(kind,pair):
    result=evaluate_observations([scoped_observation(str(i),url,kind) for i,url in enumerate(pair)],as_of=NOW)
    assert result.accounting_ok and len(result.evaluated)==2
    assert _collapse_exact_final_urls(result)==0
    assert len({row.canonical.canonical_id for row in result.evaluated})==2


def test_host_case_and_tracking_do_not_create_distinct_requisitions():
    left=scoped_observation('one','https://JOBS.EXAMPLE.ORG/Role?req=AbC&utm_source=first')
    right=scoped_observation('two','https://jobs.example.org/Role?req=AbC&utm_source=second')
    result=evaluate_observations([left,right],as_of=NOW)
    assert result.accounting_ok and len(result.evaluated)==1
    assert len(result.evaluated[0].canonical.observations)==2


def test_legacy_policy_keeps_historical_case_folding(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    rows=[scoped_observation('one','https://jobs.example.org/Role'),scoped_observation('two','https://jobs.example.org/role')]
    assert len(canonicalize_observations(rows))==1


def migration():
    parent=observation()
    old=evaluate_observations([parent.model_copy(deep=True)],as_of=NOW)
    binding=OutcomeBinding(scope=OutcomeScope(provider='example',portal='contractors',role='one',account='local',attempt='first'),
        canonical_id=old.evaluated[0].canonical.canonical_id,observation_id=parent.observation_id,role_url=parent.job.url,reviewed=True)
    child=scoped_observation('native','https://jobs.ashbyhq.com/example/exact-one',SourceKind.ORIGINAL_EMPLOYER)
    child.origin='hydration';child.parent_observation_ids=[parent.observation_id]
    current=evaluate_observations([parent,child,observation('unrelated')],as_of=NOW)
    target=next(row for row in current.evaluated if any(o.observation_id=='native' for o in row.canonical.observations))
    assert target.canonical.canonical_id!=binding.canonical_id and len(current.evaluated)==2
    retained=next(o for o in target.canonical.observations if o.observation_id==binding.observation_id)
    alias=dict(schema_version=1,alias_id='reviewed:migration',from_canonical_id=binding.canonical_id,
        to_canonical_id=target.canonical.canonical_id,observation_id=binding.observation_id,role_url=binding.role_url,
        observation_sha256=observation_binding_hash(retained),reviewed=True)
    return current,binding,alias


def target_row(result,alias):
    return next(row for row in result.evaluated if row.canonical.canonical_id==alias['to_canonical_id'])


def test_reviewed_retained_anchor_migrates_exact_rejection_only():
    result,binding,alias=migration()
    before=result.model_dump_json()
    changed,report=preview_outcomes(result,[event(binding,'reject','application_state','rejected')],[binding],as_of=NOW,aliases=[alias])
    assert target_row(changed,alias).decision.lifecycle=='closed'
    other=next(row for row in changed.evaluated if row.canonical.canonical_id!=alias['to_canonical_id'])
    assert other.decision.next_action=='apply'
    assert result.model_dump_json()==before and changed.accounting_ok
    assert report['alias_report']['counts']=={'input':1,'accepted':1,'quarantined':0}
    assert report['alias_report']['binding_resolutions'][0]['resolved_canonical_id']==alias['to_canonical_id']
    assert report['active_attempts'][0]['canonical_id']==binding.canonical_id


def test_old_binding_still_requires_explicit_migration_file():
    result,binding,_=migration()
    with pytest.raises(ValueError,match='unknown_binding_canonical'):
        preview_outcomes(result,[],[binding],as_of=NOW)


@pytest.mark.parametrize('active',['false','true',0,1,None])
def test_typed_binding_copies_cannot_bypass_literal_attempt_selection(active):
    result,binding,alias=migration()
    with pytest.raises(ValueError):
        preview_outcomes(result,[],[binding.model_copy(update={'active':active})],as_of=NOW,aliases=[alias])


def test_typed_alias_copies_cannot_bypass_literal_review():
    from jobhound.v41.outcome_aliases import CanonicalAlias
    result,binding,alias=migration()
    unsafe=CanonicalAlias.model_validate(alias).model_copy(update={'reviewed':1})
    _,report=preview_outcomes(result,[],[binding],as_of=NOW,aliases=[unsafe])
    assert report['alias_report']['quarantine'][0]['code']=='invalid_alias_schema'


def test_alias_anchor_must_equal_the_original_binding_anchor():
    result,binding,alias=migration()
    wrong=binding.model_copy(update={'observation_id':'unrelated'})
    _,report=preview_outcomes(result,[],[wrong],as_of=NOW,aliases=[alias])
    assert report['alias_report']['counts']['accepted']==1
    assert report['alias_report']['binding_resolutions'][0]['reason']=='alias_binding_anchor_mismatch'


@pytest.mark.parametrize('change,reason',[
    ({'reviewed':1},'invalid_alias_schema'),({'schema_version':True},'invalid_alias_schema'),
    ({'role_url':'https://jobs.ashbyhq.com/example/ONE'},'alias_url_mismatch'),
    ({'observation_sha256':'b'*64},'alias_observation_changed'),
    ({'to_canonical_id':'nonexistent'},'unknown_alias_target'),
    ({'observation_id':'nonexistent'},'alias_observation_owner_ambiguous'),
    ({'role_url':'https://jobs.ashbyhq.com/example/one?token=private'},'invalid_alias_schema'),
])
def test_invalid_alias_is_held_without_losing_facts_or_affecting_other_roles(change,reason):
    result,binding,alias=migration();alias.update(change)
    changed,report=preview_outcomes(result,[event(binding,'reject','application_state','rejected')],[binding],as_of=NOW,aliases=[alias])
    assert report['alias_report']['quarantine'][0]['code']==reason
    assert report['alias_report']['binding_resolutions'][0]['state']=='held'
    assert report['counts']['accepted']==1 and len(report['events'])==1
    assert report['projections'][0].value('application_state')=='rejected'
    assert all(row.decision.next_action=='apply' for row in changed.evaluated)


def test_changed_retained_native_payload_invalidates_alias_hash():
    result,binding,alias=migration()
    retained=next(o for o in target_row(result,alias).canonical.observations if o.observation_id==binding.observation_id)
    retained.raw_payload['revision']='changed after review'
    _,report=preview_outcomes(result,[],[binding],as_of=NOW,aliases=[alias])
    assert report['alias_report']['quarantine'][0]['code']=='alias_observation_changed'


def test_duplicate_alias_sources_hold_all_variants_in_either_order():
    result,binding,alias=migration()
    conflicting=dict(alias,alias_id='reviewed:other',to_canonical_id='unknown-other')
    for records in ([alias,conflicting],[conflicting,alias]):
        changed,report=preview_outcomes(result,[],[binding],as_of=NOW,aliases=records)
        assert report['alias_report']['counts']==dict(input=2,accepted=0,quarantined=2)
        assert {q['code'] for q in report['alias_report']['quarantine']}=={'ambiguous_alias_mapping'}
        assert target_row(changed,alias).decision.next_action=='apply'


def test_existing_source_canonical_cannot_be_reassigned_to_another_role():
    result,binding,alias=migration()
    other=next(row for row in result.evaluated if row.canonical.canonical_id!=alias['to_canonical_id'])
    alias['from_canonical_id']=other.canonical.canonical_id
    binding=binding.model_copy(update={'canonical_id':other.canonical.canonical_id,'observation_id':'unrelated','role_url':other.job.url})
    changed,report=preview_outcomes(result,[event(binding,'reject','application_state','rejected')],[binding],as_of=NOW,aliases=[alias])
    assert report['alias_report']['quarantine'][0]['code']=='alias_source_still_current'
    assert target_row(changed,alias).decision.next_action=='apply'
    assert next(row for row in changed.evaluated if row.canonical.canonical_id==other.canonical.canonical_id).decision.lifecycle=='closed'


def test_one_retained_observation_cannot_anchor_two_current_canonicals():
    result,binding,alias=migration()
    other=next(row for row in result.evaluated if row.canonical.canonical_id!=alias['to_canonical_id'])
    duplicate=next(o for o in target_row(result,alias).canonical.observations if o.observation_id==binding.observation_id).model_copy(deep=True)
    other.canonical.observations.append(duplicate)
    _,report=preview_outcomes(result,[],[binding],as_of=NOW,aliases=[alias])
    assert report['alias_report']['quarantine'][0]['code']=='alias_observation_owner_ambiguous'


def test_alias_does_not_choose_between_two_active_attempts():
    result,historical,alias=migration()
    selected=historical.model_copy(update={'canonical_id':alias['to_canonical_id'],
        'scope':historical.scope.model_copy(update={'attempt':'second'})})
    changed,report=preview_outcomes(result,[event(historical,'old-reject','application_state','rejected'),*assessment_events(selected)],
        [historical,selected],as_of=NOW,aliases=[alias])
    row=target_row(changed,alias)
    assert row.decision.next_action=='verify' and row.decision.action_band.value=='verify'
    assert row.canonical.outcome_projection is None
    assert row.canonical.outcome_binding_hold=='multiple_active_attempts_for_one_canonical'
    assert len(report['events'])==4 and len(report['projections'])==2
    assert all(r['state']=='held' for r in report['alias_report']['binding_resolutions'])
    assert any(t['missing_fact']=='active_outcome_attempt' for t in row.assessment.verification_tasks)


def test_inactive_migrated_rejection_keeps_history_without_closing_selected_invitation():
    result,historical,alias=migration()
    historical=historical.model_copy(update={'active':False})
    selected=historical.model_copy(update={'active':True,'canonical_id':alias['to_canonical_id'],
        'scope':historical.scope.model_copy(update={'attempt':'second'})})
    records=[event(historical,'old-reject','application_state','rejected'),*assessment_events(selected)]
    changed,report=preview_outcomes(result,records,[historical,selected],as_of=NOW,aliases=[alias])
    assert target_row(changed,alias).decision.next_action=='complete_known_step'
    assert len(report['events'])==4 and len(report['projections'])==2
    assert len(report['active_attempts'])==1
    repeated,again=preview_outcomes(result,list(reversed(records)),[selected,historical],as_of=NOW,
        aliases=[alias],previous=report['events'])
    assert again['counts']['duplicate']==4
    assert report['projection_fingerprint']==again['projection_fingerprint']
    assert decision_fingerprint(changed)==decision_fingerprint(repeated)


def test_reused_preview_revalidates_old_history_and_new_attempt_selection():
    result,historical,alias=migration()
    closed,initial=preview_outcomes(result,[event(historical,'old-reject','application_state','rejected')],[historical],as_of=NOW,aliases=[alias])
    inactive=historical.model_copy(update={'active':False})
    selected=historical.model_copy(update={'canonical_id':alias['to_canonical_id'],'scope':historical.scope.model_copy(update={'attempt':'second'})})
    changed,report=preview_outcomes(closed,assessment_events(selected),[inactive,selected],as_of=NOW,aliases=[alias])
    assert target_row(changed,alias).decision.next_action=='complete_known_step'
    assert len(report['events'])==4 and report['projections'][0].value('application_state')=='rejected'
    assert target_row(closed,alias).decision.lifecycle=='closed'
    with pytest.raises(ValueError,match='invalid_previous_journal'):
        preview_outcomes(closed,assessment_events(selected),[selected],as_of=NOW,aliases=[])


def test_full_audit_binds_membership_and_alias_cli_preserves_all_artifacts(tmp_path,monkeypatch):
    import socket
    from jobhound.v41.review import _write_audit
    from jobhound.v41.replay import write_snapshot
    from jobhound.v41 import store
    from jobhound import cli
    from jobhound.store import Store
    from jobhound.v41.community import saved_thread_observation,thread_outcome,project_thread
    from test_community_threads import topic,post,URL
    def forbidden(*a,**kw):
        pytest.fail('network or production store called')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(store.V41Store,'__init__',forbidden)
    monkeypatch.setattr(Store,'__init__',forbidden)
    # A saved native observation gives a replayable exact anchor with zero raw inputs.
    pages=[topic(post())]
    parent=saved_thread_observation(thread_outcome(project_thread(pages,URL,NOW),pages))
    result=evaluate_observations([parent],as_of=NOW,raw_count=0)
    snapshot=write_snapshot([],result,directory=tmp_path)
    item=result.evaluated[0]
    binding=OutcomeBinding(scope=OutcomeScope(provider='example',portal='main',role='one',account='local',attempt='one'),
        canonical_id='historical-canonical',observation_id=parent.observation_id,role_url=parent.job.url,reviewed=True)
    alias=dict(schema_version=1,alias_id='native:migration',from_canonical_id=binding.canonical_id,
        to_canonical_id=item.canonical.canonical_id,observation_id=parent.observation_id,role_url=parent.job.url,
        observation_sha256=observation_binding_hash(result.observations[0]),reviewed=True)
    paths={name:tmp_path/(name+'.jsonl') for name in ('bindings','events','aliases')}
    paths['bindings'].write_text(binding.model_dump_json()+'\n',encoding='utf-8')
    paths['events'].write_text(json.dumps(event(binding,'reject','application_state','rejected'))+'\n',encoding='utf-8')
    paths['aliases'].write_text(json.dumps(alias)+'\n',encoding='utf-8')
    output=tmp_path/'review'
    args=cli.build_parser().parse_args(['review','outcomes',str(snapshot),'--bindings',str(paths['bindings']),
        '--events',str(paths['events']),'--aliases',str(paths['aliases']),'--as-of',NOW.isoformat(),'--output-dir',str(output)])
    args.func(args)
    manifest=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['alias_counts']==dict(input=1,accepted=1,quarantined=0) and manifest['held_binding_count']==0
    assert manifest['aliases_input_sha256']==hashlib.sha256(paths['aliases'].read_bytes()).hexdigest()
    audit=json.loads((output/'audit.json').read_text(encoding='utf-8'))
    assert audit['decisions'][0]['observation_ids']==[parent.observation_id]
    assert audit['decisions'][0]['field_sources']
    assert json.loads((output/'outcome_actions.json').read_text(encoding='utf-8'))[0]['canonical_id']==item.canonical.canonical_id
    for name in ('events.jsonl','bindings.jsonl','aliases.jsonl','projections.json','binding_resolutions.json','alias_quarantine.json','active_attempts.json'):
        assert (output/name).is_file()
    _,again=run_outcome_review(snapshot,paths['events'],paths['bindings'],tmp_path/'again',as_of=NOW,
        aliases_path=paths['aliases'],previous_events=output/'events.jsonl')
    assert again['import_counts']['duplicate']==1 and again['decision_fingerprint']==manifest['decision_fingerprint']
