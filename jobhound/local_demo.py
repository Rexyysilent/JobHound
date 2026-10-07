"""Synthetic native records through the existing engine and outcome workflow."""
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3

from .local_paths import local_scope, profile_name
from .local_state import paths,load_policies,sha


def demonstrate(data_dir,profile=None,name='demo'):
    local=paths(data_dir,profile);profile_name(name)
    config,operator,registry=load_policies(local)
    # This explicit synthetic demo always uses the native interpreter, including
    # when a neutral starter disables V5.5 for ordinary runs. Clone the policy;
    # production approval and provider capabilities remain disabled.
    config=config.model_copy(deep=True)
    config.v55.enabled=True
    target=local.reviews_dir/name
    if target.exists():raise ValueError('local_demo_output_must_be_new')
    with local_scope(local):
        from .run_context import RunContext,run_scope
        from .v41.engine import evaluate_raw
        from .v41.outcomes import OutcomeBinding,OutcomeScope
        from .v41.replay import write_snapshot
        from .v41.outcome_review import run_outcome_review
        now=datetime(2026,10,1,9,tzinfo=timezone.utc)
        context=RunContext.capture(config=config,profile=operator,registry=registry,as_of=now,
            workspace=target,run_id='local-demo-v1-'+local.profile,network_allowed=False,side_effects_allowed=False)
        calls=dict(network=0,provider=0)
        connect,create=socket.socket.connect,socket.create_connection
        def forbidden(*a,**kw):calls['network']+=1;raise RuntimeError('local_demo_network_forbidden')
        socket.socket.connect=socket.create_connection=forbidden
        try:
            with run_scope(context):
                description='''Work
Review English AI responses using supplied guidelines and examples.
Compare response quality, check instruction following and report unclear items.
Requirements
English fluency required. Remote applicants in India may apply.
No prior AI-training experience or university degree required.
This is a contractor role evaluating supplied examples. Follow the rubric,
record your reasons and contact the project coordinator for unclear criteria.
Compensation: USD 10 per working hour.
'''
                raw=[dict(source='ashby',raw=dict(id=key,title='English AI Response Evaluator',
                    _company='Synthetic '+key,jobUrl='https://jobs.ashbyhq.com/synthetic/'+key,
                    location='India',isRemote=True,publishedAt=now.isoformat(),descriptionPlain=description))
                    for key in ('scope','assessment','rejection','access','discovery')]
                result=evaluate_raw(raw,as_of=now)
                target.mkdir(exist_ok=False)
                snapshot=write_snapshot(raw,result,directory=target/'inputs')
                bindings={}
                for item in result.evaluated:
                    key=item.job.url.rsplit('/',1)[-1]
                    if key=='discovery':continue
                    bindings[key]=OutcomeBinding(scope=OutcomeScope(provider='synthetic-'+key,portal='demo',
                        role=key,account='synthetic-user',attempt='attempt-1'),reviewed=True,
                        canonical_id=item.canonical.canonical_id,observation_id=item.canonical.best_observation.observation_id,
                        role_url=item.job.url)
                binding_file=target/'bindings.input.jsonl'
                binding_file.write_text(''.join(b.model_dump_json()+'\n' for b in bindings.values()),encoding='utf-8')
                def event(key,identity,predicate,value,**updates):
                    row=dict(schema_version=1,event_id=identity,scope=bindings[key].scope.model_dump(),
                        predicate=predicate,value=value,actor='provider',source_kind='mail',
                        evidence_ref='synthetic:'+identity,support_path='body.claim',support_sha256='a'*64,
                        event_at=(now-timedelta(hours=2)).isoformat(),observed_at=(now-timedelta(hours=1)).isoformat(),
                        extraction_version='local-demo-1',confidentiality='private',reviewed=True)
                    row.update(updates);return row
                blocked=event('access','access-block','project_access','blocked',
                    event_at=(now-timedelta(days=2)).isoformat(),observed_at=(now-timedelta(days=1)).isoformat())
                before=target/'blocked.input.jsonl';before.write_text(json.dumps(blocked)+'\n',encoding='utf-8')
                run_outcome_review(snapshot,before,binding_file,target/'blocked',as_of=now,handoff_signal=True)
                rows=[event('scope','reply','buyer_reply','scope_changed',actor='buyer'),
                    event('scope','scope-new','scope_revision','revision-2',actor='buyer'),
                    event('scope','scope-old','agreed_scope_revision','revision-1',actor='user',source_kind='user_report'),
                    event('assessment','submitted','application_state','submitted'),
                    event('assessment','invite','assessment_state','invited'),
                    event('assessment','route','assessment_route','https://example.test/synthetic-assessment'),
                    event('assessment','checks','action_checks',dict(privacy=True,schedule=True,equipment=True,cost=True),
                        actor='user',source_kind='user_report'),
                    event('rejection','rejection','application_state','rejected'),
                    event('access','access-restored','project_access','accessible',supersedes=['access-block'])]
                events=target/'events.input.jsonl';events.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
                # Fictional operator review over only the just-generated synthetic draft.
                # This never selects an existing user review or production database.
                state=target/'blocked/delivery.sqlite';drafts=[]
                db=sqlite3.connect(state.resolve().as_uri()+'?mode=ro',uri=True)
                try:
                    for identity,body_hash in db.execute('SELECT id,body_hash FROM delivery_envelopes ORDER BY channel'):
                        ids=[identity for identity,subject in db.execute('''SELECT i.id,r.subject FROM delivery_envelope_items m
                            JOIN delivery_intents i ON i.id=m.intent JOIN delivery_revisions r ON r.id=i.revision
                            WHERE m.envelope=? ORDER BY i.id''',(identity,))
                            if subject not in {b.canonical_id for b in bindings.values()}]
                        if ids:drafts.append(dict(envelope=identity,body_sha256=body_hash,intent_ids=ids))
                finally:db.close()
                plan=target/'synthetic-reissue-plan.json'
                plan.write_text(json.dumps(dict(schema_version=1,reviewed=True,evidence_ref='synthetic:demo-operator-review',
                    source_state_sha256=sha(state),drafts=drafts,pending_intent_ids=[]),indent=2),encoding='utf-8')
                _,manifest=run_outcome_review(snapshot,events,binding_file,target/'updated',as_of=now,
                    previous_events=target/'blocked/events.jsonl',handoff_signal=True,delivery_state=state,
                    reissue_plan_path=plan if drafts else None)
                audit=json.loads((target/'updated/audit.json').read_text(encoding='utf-8'))
                decisions={row['job']['url'].rsplit('/',1)[-1]:row['decision']['next_action'] for row in audit['decisions']}
                receipt=json.loads((target/'updated/signal-receipt.json').read_text(encoding='utf-8'))
                if calls['network'] or receipt['provider_calls'] or manifest['production_store_called']:
                    raise ValueError('local_demo_side_effect_violation')
                report=dict(schema='jobhound-local-demo/v1',profile=local.profile,output=str(target),
                    as_of=now.isoformat(),synthetic=True,decisions=decisions,
                    accounting_ok=audit['accounting_ok'],database_integrity=receipt['database_integrity'],
                    foreign_key_errors=receipt['foreign_key_errors'],baseline_count=receipt['baseline_count'],
                    synthetic_reissued_generations=len(receipt['reviewed_reissue']['generations']) if receipt['reviewed_reissue'] else 0,
                    calls=calls,runtime_versions=receipt['runtime_versions'],
                    limitations=['Synthetic workflow demonstration; no income or recommendation-quality measurement',
                        'Fictional review of the generated synthetic draft is not approval for real user state'])
                (target/'demo-receipt.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
                return report
        finally:socket.socket.connect=connect;socket.create_connection=create
