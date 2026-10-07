"""Hash-bound review recovery through real engine, queues and shared caps."""
from copy import deepcopy
import hashlib
import json

import pytest

from jobhound.handoff_reissue import read_reissue_plan,ReissuePlan
from jobhound.handoff_review import write_handoff_review,DESTINATIONS
from jobhound.notify.receipt import DeliveryReceipt
from test_handoff_signals import engine,later,many,policy
from test_handoff_preview_lifecycle import open_review
from test_work_outcomes import ready,work_event
from test_v6_transport import Sender,send
from test_v55_action_policy import NOW


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def records(binding):
    return ready(binding)+[work_event(binding,'done','work_state','submitted',actor='user',source_kind='user_report')]


def initial(tmp_path,count=5):
    root=tmp_path/'initial';root.mkdir()
    result,binding,_=engine(ready,many(count))
    receipt=write_handoff_review(result,root)
    return root,result,binding,receipt


def plan(root,*,exclude_subject=None,pending=(),drafts=True):
    db,transport=open_review(root)
    selections=[]
    if drafts:
        for (identity,) in db.conn.execute('SELECT id FROM delivery_envelopes ORDER BY id'):
            envelope=transport.inspect(identity)
            ids=[r['id'] for r in transport._items(identity) if r['subject']!=exclude_subject
                 and r['revision']==db.delivery.current_revision(r['subject'])]
            if ids:selections.append(dict(envelope=identity,body_sha256=envelope['body_hash'],intent_ids=ids))
    db.close()
    return dict(schema_version=1,reviewed=True,evidence_ref='synthetic:operator-reissue',
        source_state_sha256=sha(root/'delivery.sqlite'),drafts=selections,pending_intent_ids=list(pending))


def save_plan(tmp_path,data,name='plan.json'):
    path=tmp_path/name;path.write_text(json.dumps(data),encoding='utf-8');return path


def recover(tmp_path,root,result,data,name='recovered'):
    target=tmp_path/name;target.mkdir()
    receipt=write_handoff_review(later(result),target,previous_state=root/'delivery.sqlite',
        reissue_plan_path=save_plan(tmp_path,data,name+'.json'))
    return target,receipt


def test_mixed_recovery_caps_new_and_reissued_union_and_preserves_deferred_lineage(tmp_path):
    root,_,binding,old=initial(tmp_path)
    updated,_,_=engine(records,many(9))
    data=plan(root,exclude_subject=binding.canonical_id)
    source_hash=sha(root/'delivery.sqlite')
    target,receipt=recover(tmp_path,root,updated,data)
    assert sha(root/'delivery.sqlite')==source_hash
    assert receipt['provider_calls']==receipt['baseline_count']==0 and not receipt['preparation_holds']
    report=receipt['reviewed_reissue']
    assert len(report['generations'])==8 and len(receipt['previews'])==2
    assert all(d['displayed']=={'action':5,'status':1} and len(d['deferred_pending'])==3 for d in report['destinations'])
    assert all(d['total']==sum(d['counts'].values())==len(d['dispositions'])==9 for d in report['destinations'])
    db,transport=open_review(target)
    for row in report['generations']:
        original=db.delivery._owned(row['original']);replacement=db.delivery._owned(row['replacement'])
        assert original['status']=='superseded'
        assert replacement['replaces_intent']==original['id'] and replacement['generation']==1
        assert replacement['revision']==original['revision'] and replacement['payload']==original['payload']
    for preview in old['previews']:
        historic=transport.inspect(preview['envelope'])
        assert historic['status']=='abandoned' and historic['body_hash']==preview['body_sha256']
        assert db.conn.execute('SELECT count(*) FROM delivery_part_events WHERE envelope=?',
            (historic['id'],)).fetchone()[0]==len(historic['parts'])
    assert all(db.delivery._owned(i)['status']=='pending' for d in report['destinations'] for i in d['deferred_pending'])
    db.close()
    # A later explicit plan selects only unfrozen overflow; it never drains
    # the earlier newly frozen digest and never creates another generation.
    pending=[i for d in report['destinations'] for i in d['deferred_pending']]
    next_plan=plan(target,pending=pending,drafts=False)
    followup,again=recover(tmp_path,target,later(updated),next_plan,name='overflow')
    assert not again['reviewed_reissue']['generations']
    assert all(d['displayed']=={'action':3} for d in again['reviewed_reissue']['destinations'])
    assert again['baseline_count']==again['provider_calls']==0


@pytest.mark.parametrize('bad',[False,1,'yes',None])
def test_plan_requires_literal_review_and_rejects_before_copy(tmp_path,bad):
    root,result,_,_=initial(tmp_path,count=2);data=plan(root);data['reviewed']=bad
    target=tmp_path/'bad';target.mkdir()
    with pytest.raises(ValueError,match='invalid_reissue_plan'):
        write_handoff_review(later(result),target,previous_state=root/'delivery.sqlite',reissue_plan_path=save_plan(tmp_path,data))
    assert not (target/'delivery.sqlite').exists()


@pytest.mark.parametrize('failure',['source_hash','body_hash','omit_current','stale_member','unknown_member','duplicate_member'])
def test_changed_or_incomplete_plan_cannot_half_abandon_a_source_draft(tmp_path,failure):
    root,result,binding,old=initial(tmp_path,count=3)
    updated,_,_=engine(records,many(3))
    data=plan(root,exclude_subject=binding.canonical_id)
    if failure=='source_hash':data['source_state_sha256']='0'*64
    if failure=='body_hash':data['drafts'][0]['body_sha256']='0'*64
    if failure=='omit_current':data['drafts'][0]['intent_ids'].pop()
    if failure=='stale_member':
        db,transport=open_review(root)
        row=next(r for r in transport._items(data['drafts'][0]['envelope']) if r['subject']==binding.canonical_id)
        db.close();data['drafts'][0]['intent_ids'].append(row['id'])
    if failure=='unknown_member':data['drafts'][0]['intent_ids'].append(99999)
    if failure=='duplicate_member':data['drafts'][0]['intent_ids'].append(data['drafts'][0]['intent_ids'][0])
    original_hash=sha(root/'delivery.sqlite')
    target=tmp_path/'bad';target.mkdir()
    with pytest.raises(ValueError):
        write_handoff_review(later(updated),target,previous_state=root/'delivery.sqlite',reissue_plan_path=save_plan(tmp_path,data))
    assert sha(root/'delivery.sqlite')==original_hash
    if (target/'delivery.sqlite').exists():
        db,transport=open_review(target)
        assert db.conn.execute('SELECT count(*) FROM delivery_runs').fetchone()[0]==1
        assert db.conn.execute('SELECT count(*) FROM delivery_intents WHERE generation>0').fetchone()[0]==0
        assert all(transport.inspect(p['envelope'])['status']=='pending' for p in old['previews'])
        db.close()


@pytest.mark.parametrize('outcome',['not_sent','uncertain','permanent_failure','accepted'])
def test_any_attempt_or_receipt_prevents_recomposition_and_consumption(tmp_path,outcome):
    root,_,binding,old=initial(tmp_path,count=3)
    db,transport=open_review(root)
    sender=Sender(DESTINATIONS[0],[DeliveryReceipt(outcome,'synthetic-attempt')])
    actual=send(transport,old['previews'][0]['envelope'],sender,now=NOW.timestamp()+1)
    assert actual=={'not_sent':'pending','uncertain':'uncertain','permanent_failure':'failed','accepted':'accepted'}[outcome]
    assert sender.calls
    db.close()
    data=plan(root,exclude_subject=binding.canonical_id)
    updated,_,_=engine(records,many(3));original_hash=sha(root/'delivery.sqlite')
    target=tmp_path/'bad';target.mkdir()
    with pytest.raises(ValueError):write_handoff_review(later(updated),target,
        previous_state=root/'delivery.sqlite',reissue_plan_path=save_plan(tmp_path,data))
    assert sha(root/'delivery.sqlite')==original_hash
    db,transport=open_review(target)
    assert db.conn.execute('SELECT count(*) FROM delivery_intents WHERE generation>0').fetchone()[0]==0
    assert transport.inspect(old['previews'][1]['envelope'])['status']=='pending'
    db.close()


def test_second_destination_renderer_failure_rolls_back_all_generations_and_freezes(tmp_path,monkeypatch):
    root,_,binding,old=initial(tmp_path,count=3)
    data=plan(root,exclude_subject=binding.canonical_id)
    updated,_,_=engine(records,many(3))
    import jobhound.delivery_transport as module
    render=module.render_envelope;calls=[]
    def fail(rows,**kw):
        calls.append(rows)
        if len(calls)==2:raise ValueError('synthetic-render-failure')
        return render(rows,**kw)
    monkeypatch.setattr(module,'render_envelope',fail)
    target=tmp_path/'bad';target.mkdir()
    with pytest.raises(ValueError,match='synthetic-render-failure'):
        write_handoff_review(later(updated),target,previous_state=root/'delivery.sqlite',reissue_plan_path=save_plan(tmp_path,data))
    db,transport=open_review(target)
    assert db.conn.execute('SELECT count(*) FROM delivery_intents WHERE generation>0').fetchone()[0]==0
    assert db.conn.execute('SELECT count(*) FROM delivery_envelopes').fetchone()[0]==2
    assert all(transport.inspect(p['envelope'])['status']=='pending' for p in old['previews'])
    db.close()


@pytest.mark.parametrize('history',['attempted','accepted_event','accepted_baseline'])
def test_obsolete_member_receipt_cannot_be_erased_by_reissuing_other_current_members(tmp_path,history):
    root,_,binding,old=initial(tmp_path,count=3)
    db,transport=open_review(root)
    obsolete=next(i for i in transport._items(old['previews'][0]['envelope']) if i['subject']==binding.canonical_id)
    if history=='attempted':
        db.conn.execute('UPDATE delivery_intents SET attempts=1 WHERE id=?',(obsolete['id'],))
        db.delivery._event(obsolete['id'],'not_sent',evidence='synthetic:older-intent-receipt',now=NOW.timestamp())
    if history=='accepted_event':db.delivery._event(obsolete['id'],'accepted',evidence='synthetic:imported-intent-receipt',now=NOW.timestamp())
    if history=='accepted_baseline':db.conn.execute('INSERT INTO delivery_baselines VALUES(?,?,?,?,?,?)',
        (db.delivery.workspace,db.delivery.profile,obsolete['subject'],obsolete['channel'],obsolete['destination'],obsolete['revision']))
    db.conn.commit();db.close()
    data=plan(root,exclude_subject=binding.canonical_id)
    updated,_,_=engine(records,many(3))
    original_hash=sha(root/'delivery.sqlite');target=tmp_path/'bad';target.mkdir()
    with pytest.raises(ValueError,match='draft member'):
        write_handoff_review(later(updated),target,previous_state=root/'delivery.sqlite',reissue_plan_path=save_plan(tmp_path,data))
    assert sha(root/'delivery.sqlite')==original_hash
    db,transport=open_review(target)
    assert transport.inspect(old['previews'][0]['envelope'])['status']=='pending'
    assert db.conn.execute('SELECT count(*) FROM delivery_intents WHERE generation>0').fetchone()[0]==0
    db.close()


@pytest.mark.parametrize('case',['duplicate_keys','oversized','version_bool','empty','bad_id','unknown_field'])
def test_reissue_reader_has_finite_strict_contract_and_no_silent_overrides(tmp_path,case):
    data=dict(schema_version=1,reviewed=True,evidence_ref='synthetic:review',source_state_sha256='a'*64,pending_intent_ids=[1])
    if case=='version_bool':data['schema_version']=True
    if case=='empty':data['pending_intent_ids']=[]
    if case=='bad_id':data['pending_intent_ids']=[True]
    if case=='unknown_field':data['recipient']='synthetic@example.test'
    path=save_plan(tmp_path,data)
    if case=='duplicate_keys':path.write_text('{"reviewed":false,"reviewed":true}',encoding='utf-8')
    if case=='oversized':path.write_text(' '*65537,encoding='utf-8')
    with pytest.raises(ValueError):read_reissue_plan(path,'a'*64)


@pytest.mark.parametrize('suffix',['-wal','-shm','-journal'])
def test_open_database_sidecars_cannot_bypass_source_byte_binding(tmp_path,suffix):
    root,result,_,_=initial(tmp_path,count=2)
    data=plan(root);(root/('delivery.sqlite'+suffix)).write_bytes(b'synthetic-active-sidecar')
    target=tmp_path/'bad';target.mkdir()
    with pytest.raises(ValueError,match='marked_disposable'):
        write_handoff_review(later(result),target,previous_state=root/'delivery.sqlite',reissue_plan_path=save_plan(tmp_path,data))
    assert not (target/'delivery.sqlite').exists()


def test_accepted_email_does_not_suppress_explicit_unsent_telegram_recovery(tmp_path):
    root,_,binding,old=initial(tmp_path,count=3)
    db,transport=open_review(root)
    sender=Sender(DESTINATIONS[0])
    assert send(transport,old['previews'][0]['envelope'],sender,now=NOW.timestamp()+1)=='accepted'
    baseline_count=db.conn.execute('SELECT count(*) FROM delivery_baselines').fetchone()[0]
    db.close()
    data=plan(root,exclude_subject=binding.canonical_id)
    data['drafts']=[d for d in data['drafts'] if d['envelope']==old['previews'][1]['envelope']]
    updated,_,_=engine(records,many(3))
    target,receipt=recover(tmp_path,root,updated,data)
    assert receipt['baseline_count']==baseline_count
    assert all(r['channel']=='telegram' for r in receipt['reviewed_reissue']['generations'])
    assert receipt['reviewed_reissue']['destinations'][1]['displayed']=={'action':2,'status':1}
    db,transport=open_review(target)
    assert transport.inspect(old['previews'][0]['envelope'])['status']=='accepted'
    assert transport.inspect(old['previews'][1]['envelope'])['status']=='abandoned'
    db.close()


def test_shared_verify_cap_and_no_implicit_pending_backlog_drain(tmp_path):
    originals=many(9)
    for raw in originals[5:]:raw.vacancy_state='unknown'
    first,binding,_=engine(ready,originals)
    root=tmp_path/'initial';root.mkdir();old=write_handoff_review(first,root)
    raw=many(12)
    for observation in raw[5:9]:observation.vacancy_state='unknown'
    updated,_,_=engine(records,raw)
    data=plan(root,exclude_subject=binding.canonical_id)
    target,receipt=recover(tmp_path,root,updated,data)
    for ledger in receipt['reviewed_reissue']['destinations']:
        assert ledger['displayed']=={'action':5,'verify':2,'status':1}
        assert ledger['total']==sum(ledger['counts'].values())==12
        assert ledger['deferred_pending']
    # No plan on the next copy means no unsolicited drain or new generation.
    before=receipt['reviewed_reissue']['generations']
    followup=tmp_path/'no-plan';followup.mkdir()
    plain=write_handoff_review(later(later(updated)),followup,previous_state=target/'delivery.sqlite')
    assert plain['reviewed_reissue'] is None and plain['previews']==[]
    db,transport=open_review(followup)
    assert db.conn.execute('SELECT count(*) FROM delivery_intents WHERE generation>0').fetchone()[0]==len(before)
    db.close()


def test_actual_offline_cli_reissues_only_hash_bound_review_state(tmp_path,monkeypatch):
    from jobhound import cli
    from jobhound.v41.engine import evaluate_raw
    from jobhound.v41.replay import write_snapshot
    from jobhound.v41.outcomes import OutcomeBinding,OutcomeScope
    from test_v55_action_policy import DESCRIPTION
    from jobhound.store import Store
    calls=[]
    def forbidden(*a,**kw):calls.append('production-store');raise AssertionError('production store forbidden')
    monkeypatch.setattr(Store,'__init__',forbidden)
    raw=[dict(source='ashby',raw=dict(id=str(i),title='Bengali AI Response Evaluator',_company='Employer '+str(i),
        jobUrl='https://jobs.ashbyhq.com/example/'+str(i),location='India',isRemote=True,
        descriptionPlain=DESCRIPTION+' Compensation: USD 10 per working hour.')) for i in range(3)]
    result=evaluate_raw(raw,as_of=NOW);item=result.evaluated[0]
    snapshot=write_snapshot(raw,result,directory=tmp_path/'inputs')
    binding=OutcomeBinding(scope=OutcomeScope(provider='example',portal='main',role='one',account='local',attempt='one'),
        canonical_id=item.canonical.canonical_id,observation_id=item.canonical.observations[0].observation_id,
        role_url=item.job.url,reviewed=True)
    bindings=tmp_path/'bindings.jsonl';bindings.write_text(binding.model_dump_json()+'\n',encoding='utf-8')
    def invoke(name,events,state=None,request=None):
        path=tmp_path/(name+'-events.jsonl');path.write_text(''.join(json.dumps(row)+'\n' for row in events),encoding='utf-8')
        args=['review','outcomes',str(snapshot),'--events',str(path),'--bindings',str(bindings),
            '--as-of',NOW.isoformat(),'--output-dir',str(tmp_path/name),'--handoff-signal']
        if state:args+=['--delivery-state',str(state)]
        if request:args+=['--reissue-plan',str(request)]
        parsed=cli.build_parser().parse_args(args);parsed.func(parsed)
        return json.loads((tmp_path/name/'signal-receipt.json').read_text(encoding='utf-8'))
    old=invoke('initial',ready(binding))
    data=plan(tmp_path/'initial',exclude_subject=binding.canonical_id)
    result=invoke('reissued',records(binding),tmp_path/'initial'/'delivery.sqlite',save_plan(tmp_path,data))
    assert result['reviewed_reissue'] and len(result['reviewed_reissue']['generations'])==4
    assert result['provider_calls']==result['baseline_count']==0 and not calls
    manifest=json.loads((tmp_path/'reissued'/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['provider_calls']==0 and not manifest['production_store_called']
    data['source_state_sha256']='0'*64
    with pytest.raises(SystemExit):invoke('wrong-state',records(binding),tmp_path/'initial'/'delivery.sqlite',save_plan(tmp_path,data,'wrong.json'))
    assert not (tmp_path/'wrong-state').exists()
