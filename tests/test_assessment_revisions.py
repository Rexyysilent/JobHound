"""Assessment actions follow one explicit step/revision, retaining other history."""
from datetime import timedelta
import json

import pytest

from jobhound.config import CONFIG
from jobhound.v41.digest import build_digest
from jobhound.v41.engine import decision_fingerprint
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.outcomes import OutcomeEvent
from test_v55_action_policy import NOW, observation
from test_v57_outcomes import setup, event, assessment_events

OLD={'step_id':'screening','revision':'terms-1'}
NEW={'step_id':'interview','revision':'terms-2'}

@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    monkeypatch.setattr(CONFIG.v55,'account_states',[])

def scoped(binding, key, predicate, value, identity=NEW, **updates):
    return event(binding,key,predicate,value,schema_version=2,assessment=identity,**updates)

def selected(binding, identity=NEW, **updates):
    return event(binding,'selection','assessment_step',identity,**(dict(schema_version=2)|updates))

def ready(binding, identity=NEW):
    return [selected(binding,identity),
        scoped(binding,'invite','assessment_state','invited',identity),
        scoped(binding,'route','assessment_route','https://example.org/assessment/'+identity['step_id'],identity),
        scoped(binding,'checks','action_checks',dict(privacy=True,schedule=True,equipment=True,cost=True),identity,
               actor='user',source_kind='user_report')]

def preview(rows,result,binding,**kwargs):
    changed,report=preview_outcomes(result,rows,[binding],as_of=NOW,**kwargs)
    assert changed.accounting_ok and build_digest(changed,include_all=True).accounting_ok
    return changed,report

def test_old_step_completion_and_approvals_do_not_complete_new_step():
    result,binding=setup()
    history=[scoped(binding,'old-done','assessment_state','completed',OLD),
             scoped(binding,'old-checks','action_checks',dict(privacy=True,schedule=True,equipment=True,cost=True),OLD,
                    actor='user',source_kind='user_report')]
    changed,report=preview(history+ready(binding),result,binding)
    item=changed.evaluated[0]
    assert item.decision.next_action=='complete_known_step' and item.decision.action_band.value=='primary'
    assert 'interview, revision terms-2' in item.decision.next_step
    projection=item.canonical.outcome_projection
    assert projection.value('assessment_state',assessment=OutcomeEvent.model_validate(history[0]).assessment)=='completed'
    assert len(report['events'])==6 and all(report['projections'][0].dispositions[e['event_id']]=='current' for e in history)

@pytest.mark.parametrize('missing', ['checks','route','invite'])
@pytest.mark.parametrize('history_kind',['other_step','other_revision','legacy'])
def test_other_step_revision_or_legacy_fact_cannot_supply_missing_prerequisite(missing,history_kind):
    result,binding=setup()
    rows=ready(binding)
    relevant=next(row for row in rows if row['event_id']==missing)
    rows.remove(relevant)
    if history_kind=='legacy':
        relevant.pop('assessment');relevant['schema_version']=1
    else:
        relevant['assessment']=OLD if history_kind=='other_step' else dict(NEW,revision='terms-1')
    rows.append(relevant)
    changed,_=preview(rows,result,binding)
    assert changed.evaluated[0].decision.next_action=='verify'
    assert changed.evaluated[0].decision.action_band.value=='verify'
    assert changed.evaluated[0].assessment.verification_tasks

def test_completed_selected_step_prevents_repeat_without_discarding_route_checks():
    result,binding=setup()
    rows=ready(binding)
    rows.append(scoped(binding,'done','assessment_state','completed',actor='user',source_kind='user_report',
        event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat(),supersedes=['invite']))
    changed,report=preview(rows,result,binding)
    assert changed.evaluated[0].decision.next_action=='await_response'
    assert 'interview, revision terms-2' in changed.evaluated[0].decision.next_step
    assert len(report['events'])==5 and report['projections'][0].dispositions['invite']=='superseded'

def test_new_reminder_cannot_automatically_reopen_completed_same_revision():
    result,binding=setup()
    rows=ready(binding)
    rows[1]['value']='completed'
    rows.append(scoped(binding,'reminder','assessment_state','invited',
        event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat()))
    changed,report=preview(rows,result,binding)
    assert report['projections'][0].conflicts
    assert changed.evaluated[0].decision.next_action=='verify'
    assert 'outcome_conflict'==changed.evaluated[0].assessment.lifecycle_reason

def test_current_revision_correction_retains_old_fact_and_requires_explicit_edge():
    result,binding=setup()
    rows=ready(binding);rows[1]['value']='completed'
    rows.append(scoped(binding,'corrected','assessment_state','invited',
        event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat(),supersedes=['invite']))
    changed,report=preview(rows,result,binding)
    assert changed.evaluated[0].decision.next_action=='complete_known_step'
    assert not report['projections'][0].conflicts and len(report['events'])==5

def test_cross_revision_supersession_is_quarantined_not_history_erasure():
    result,binding=setup()
    rows=ready(binding)
    rows.append(scoped(binding,'old','assessment_state','completed',OLD,
        event_at=(NOW-timedelta(days=2)).isoformat(),observed_at=(NOW-timedelta(days=1)).isoformat()))
    rows[1]['supersedes']=['old']
    changed,report=preview(rows,result,binding)
    assert report['counts']['quarantined']==1 and report['quarantine'][0]['code']=='invalid_supersession'
    assert changed.evaluated[0].decision.next_action=='verify'
    assert next(e for e in report['events'] if e.event_id=='old').value=='completed'

def test_unselected_scoped_history_does_not_borrow_legacy_completed_state():
    result,binding=setup()
    rows=[row for row in ready(binding) if row['event_id']!='selection']
    rows.append(event(binding,'legacy-done','assessment_state','completed'))
    changed,_=preview(rows,result,binding)
    item=changed.evaluated[0]
    assert item.decision.next_action=='verify' and item.assessment.lifecycle_reason=='assessment_step_not_selected'
    assert any(t['missing_fact']=='current_assessment_step' for t in item.assessment.verification_tasks)

def test_historical_conflict_does_not_block_different_selected_step():
    result,binding=setup()
    old=[scoped(binding,'old-provider','assessment_state','invited',OLD),
         scoped(binding,'old-user','assessment_state','completed',OLD,actor='user',source_kind='user_report')]
    changed,report=preview(old+ready(binding),result,binding)
    assert report['projections'][0].conflicts
    assert changed.evaluated[0].decision.next_action=='complete_known_step'
    assert len(report['events'])==6

def test_old_attempt_wide_account_completion_is_retained_without_completing_new_step(monkeypatch):
    result,binding=setup()
    monkeypatch.setattr(CONFIG.v55,'account_states',[dict(canonical_id=binding.canonical_id,
        assessment_state='completed',observed_at=(NOW-timedelta(days=1)).isoformat())])
    changed,_=preview(ready(binding),result,binding)
    assert changed.evaluated[0].decision.next_action=='complete_known_step'
    assert changed.evaluated[0].assessment.account_state['assessment_state']=='completed'

def test_incomparable_provider_selections_hold_instead_of_choosing_by_id():
    result,binding=setup();rows=ready(binding)
    other=selected(binding,OLD);other['event_id']='other-selection';rows.append(other)
    changed,report=preview(rows,result,binding)
    assert 'assessment_step' in report['projections'][0].conflicts
    assert changed.evaluated[0].decision.next_action=='verify'
    assert any(t['missing_fact']=='outcome_conflict:assessment_step' for t in changed.evaluated[0].assessment.verification_tasks)

def test_newer_selected_step_preserves_previous_step_and_its_completed_history():
    result,binding=setup();rows=ready(binding)
    previous=selected(binding,OLD,event_at=(NOW-timedelta(days=2)).isoformat(),observed_at=(NOW-timedelta(days=1)).isoformat())
    previous['event_id']='old-selection'
    rows += [previous,scoped(binding,'old-done','assessment_state','completed',OLD)]
    changed,report=preview(list(reversed(rows)),result,binding)
    assert changed.evaluated[0].decision.next_action=='complete_known_step'
    projection=report['projections'][0]
    assert projection.dispositions['old-selection']=='superseded' and projection.dispositions['old-done']=='current'
    assert len(projection.events)==6

@pytest.mark.parametrize('bad_time',['unknown','expired','stale','seed'])
def test_selected_identity_itself_requires_current_supported_evidence(bad_time):
    result,binding=setup();rows=ready(binding)
    if bad_time=='unknown':rows[0]['event_at']=None
    elif bad_time=='expired':rows[0]['expires_at']=(NOW-timedelta(minutes=30)).isoformat()
    elif bad_time=='stale':
        rows[0]['event_at']=(NOW-timedelta(days=90)).isoformat()
        rows[0]['observed_at']=(NOW-timedelta(days=89)).isoformat()
    else:rows[0]['source_kind']='reviewed_seed'
    changed,_=preview(rows,result,binding)
    assert changed.evaluated[0].decision.next_action=='verify'

@pytest.mark.parametrize('case',['v1_subject','v1_selection','missing_subject','foreign_predicate','selection_actor'])
def test_revision_schema_is_explicit_and_inapplicable_subjects_are_quarantined(case):
    result,binding=setup();row=ready(binding)[1]
    if case=='v1_subject':row['schema_version']=1
    elif case=='v1_selection':row=selected(binding,schema_version=1)
    elif case=='missing_subject':row.pop('assessment')
    elif case=='foreign_predicate':row['predicate']='application_state';row['value']='submitted'
    else:row=selected(binding,actor='user',source_kind='user_report')
    _,report=preview([row],result,binding)
    assert report['counts']['quarantined']==1

def test_legacy_event_shape_and_projection_remain_compatible():
    result,binding=setup();rows=assessment_events(binding)
    for raw in rows:
        encoded=OutcomeEvent.model_validate(raw).model_dump(mode='json')
        assert 'assessment' not in encoded and encoded['schema_version']==1
        assert OutcomeEvent.model_validate(encoded).model_dump_json()==OutcomeEvent.model_validate(raw).model_dump_json()
    changed,_=preview(rows,result,binding)
    assert changed.evaluated[0].decision.next_action=='complete_known_step'

def test_revision_import_is_order_independent_idempotent_and_attempt_scoped():
    result,binding=setup();rows=ready(binding)
    first,report=preview(rows,result,binding)
    reverse,rev=preview(list(reversed(rows)),result,binding)
    assert decision_fingerprint(first)==decision_fingerprint(reverse)
    assert report['projection_fingerprint']==rev['projection_fingerprint']
    again,repeated=preview(rows,result,binding,previous=report['events'])
    assert repeated['counts']==dict(input=4,accepted=0,duplicate=4,quarantined=0)
    assert decision_fingerprint(first)==decision_fingerprint(again)
    foreign=json.loads(json.dumps(rows[0]));foreign['scope']['attempt']='other'
    _,wrong=preview([foreign],result,binding)
    assert wrong['quarantine'][0]['code']=='unmatched_scope'

def test_public_veto_and_exact_attempt_rejection_still_dominate_selected_step():
    result,binding=setup(observation())
    rows=ready(binding)+[event(binding,'reject','application_state','rejected')]
    changed,_=preview(rows,result,binding)
    assert changed.evaluated[0].decision.next_action=='await_change'
    source=observation();source.job.description+='\nApplicants must reside in the United States.\n'
    result,binding=setup(source)
    changed,_=preview(ready(binding),result,binding)
    assert changed.evaluated[0].decision.next_action=='skip'
    assert changed.evaluated[0].assessment.action_readiness=='blocked'


@pytest.mark.parametrize('with_cost',[False,True])
def test_real_offline_cli_roundtrip_targets_step_and_cost_without_network_or_store(tmp_path,monkeypatch,with_cost):
    import socket
    from jobhound import cli
    from jobhound.store import Store
    from jobhound.v41.engine import evaluate_raw
    from jobhound.v41.outcomes import OutcomeBinding,OutcomeScope
    from jobhound.v41.replay import write_snapshot
    from jobhound.v41.store import V41Store
    from test_v55_action_policy import DESCRIPTION
    def forbidden(*args,**kwargs):
        pytest.fail('revision review used network or production store')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    monkeypatch.setattr(Store,'__init__',forbidden)
    monkeypatch.setattr(V41Store,'__init__',forbidden)
    raw=[dict(source='ashby',raw=dict(id='one',title='Bengali AI Response Evaluator',
        _company='Example',jobUrl='https://jobs.ashbyhq.com/example/one',location='India',isRemote=True,
        descriptionPlain=DESCRIPTION+'\nCompensation: USD 10 per working hour.'))]
    result=evaluate_raw(raw,as_of=NOW);item=result.evaluated[0]
    snapshot=write_snapshot(raw,result,directory=tmp_path)
    binding=OutcomeBinding(scope=OutcomeScope(provider='example',portal='main',role='one',account='local',attempt='one'),
        canonical_id=item.canonical.canonical_id,observation_id=item.canonical.observations[0].observation_id,
        role_url=item.job.url,reviewed=True)
    old_cost=dict(minutes=5,cash=12,currency='USD',basis='provider_estimate')
    new_cost=dict(minutes=30,cash=0,currency='USD',basis='user_estimate')
    rows=ready(binding)+[scoped(binding,'old-cost','action_cost',old_cost,OLD)]
    if with_cost:rows.append(scoped(binding,'cost','action_cost',new_cost,actor='user',source_kind='user_report'))
    bindings=tmp_path/'bindings.jsonl';bindings.write_text(binding.model_dump_json()+'\n',encoding='utf-8')
    events=tmp_path/'events.jsonl';events.write_text(''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
    def run(output,previous=None):
        command=['review','outcomes',str(snapshot),'--bindings',str(bindings),'--events',str(events),
                 '--as-of',NOW.isoformat(),'--output-dir',str(output)]
        if previous is not None:command+=['--previous-events',str(previous)]
        args=cli.build_parser().parse_args(command);args.func(args)
    first=tmp_path/'first';run(first)
    action=json.loads((first/'outcome_actions.json').read_text(encoding='utf-8'))[0]
    assert action['next_action']=='complete_known_step'
    assert action['assessment_step']==NEW and action['assessment_attempt_scope']==binding.scope.model_dump()
    assert action['assessment_next_action_cost']==(new_cost if with_cost else None)
    assert 'old-cost' in action['event_ids'] and 'old-cost' not in action['assessment_current_event_ids']
    assert set(action['assessment_current_event_ids'])==({'selection','invite','route','checks','cost'} if with_cost else {'selection','invite','route','checks'})
    before=json.loads((first/'manifest.json').read_text(encoding='utf-8'))
    assert before['import_counts']['accepted']==len(rows)
    assert not before['delivery_called'] and not before['production_store_called']
    second=tmp_path/'second';run(second,first/'events.jsonl')
    after=json.loads((second/'manifest.json').read_text(encoding='utf-8'))
    assert after['import_counts']==dict(input=len(rows),accepted=0,duplicate=len(rows),quarantined=0)
    assert after['decision_fingerprint']==before['decision_fingerprint']
    completed=rows+[scoped(binding,'done','assessment_state','completed',actor='user',source_kind='user_report',
        event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat(),supersedes=['invite'])]
    events.write_text(''.join(json.dumps(row)+'\n' for row in completed),encoding='utf-8')
    third=tmp_path/'completed';run(third)
    status=json.loads((third/'outcome_actions.json').read_text(encoding='utf-8'))[0]
    assert status['next_action']=='await_response' and status['assessment_next_action_cost'] is None
    if with_cost:
        for issue in ('stale','unknown_time','expired','seed'):
            altered=json.loads(json.dumps(rows));estimate=next(row for row in altered if row['event_id']=='cost')
            if issue=='stale':
                estimate['event_at']=(NOW-timedelta(days=60)).isoformat()
                estimate['observed_at']=(NOW-timedelta(days=59)).isoformat()
            elif issue=='unknown_time':estimate['event_at']=None
            elif issue=='expired':estimate['expires_at']=(NOW-timedelta(minutes=20)).isoformat()
            else:estimate['source_kind']='reviewed_seed'
            events.write_text(''.join(json.dumps(row)+'\n' for row in altered),encoding='utf-8')
            output=tmp_path/issue;run(output)
            action=json.loads((output/'outcome_actions.json').read_text(encoding='utf-8'))[0]
            assert action['next_action']=='complete_known_step' and action['assessment_next_action_cost'] is None
