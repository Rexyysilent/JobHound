"""Synthetic outcome controls through the existing decision engine, not a toy ranker."""
from datetime import timedelta
import json

import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_observations, decision_fingerprint
from jobhound.v41.outcomes import OutcomeBinding, OutcomeEvent, OutcomeScope, import_events
from jobhound.v41.outcome_review import preview_outcomes, read_jsonl, run_outcome_review
from test_v55_action_policy import observation, NOW, DESCRIPTION


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def setup(*rows):
    result = evaluate_observations(list(rows or [observation()]), as_of=NOW)
    row = next(r for r in result.evaluated if r.canonical.observations[0].observation_id == 'one')
    scope = OutcomeScope(provider='example', portal='contractors', role='role-1', account='local-1', attempt='attempt-1')
    binding = OutcomeBinding(scope=scope, canonical_id=row.canonical.canonical_id,
        observation_id='one', role_url=row.job.url, reviewed=True)
    return result, binding


def event(binding, key, predicate, value, **updates):
    row = dict(schema_version=1, event_id=key, scope=binding.scope.model_dump(),
        predicate=predicate, value=value, actor='provider', source_kind='mail',
        evidence_ref='synthetic:message:'+key, support_path='body.claim',
        support_sha256='a'*64, event_at=(NOW-timedelta(hours=2)).isoformat(),
        observed_at=(NOW-timedelta(hours=1)).isoformat(),
        extraction_version='synthetic-v1', confidentiality='private', reviewed=True)
    row.update(updates)
    return row


def preview(records, result=None, binding=None, **kw):
    if result is None:
        result, binding = setup()
    return preview_outcomes(result, records, [binding], as_of=NOW, **kw)


def test_r06_exact_rejection_keeps_new_role_and_input_immutable():
    result, binding = setup(observation(), observation('other'))
    before = result.model_dump_json()
    changed, report = preview([event(binding,'reject','application_state','rejected')], result, binding)
    old = next(r for r in changed.evaluated if r.canonical.canonical_id == binding.canonical_id)
    new = next(r for r in changed.evaluated if r.canonical.canonical_id != binding.canonical_id)
    assert old.decision.lifecycle == 'closed'
    assert old.decision.next_action == 'await_change'
    assert new.decision.action_band.value == 'primary'
    assert result.model_dump_json() == before
    assert changed.accounting_ok and report['counts']['accepted'] == 1


@pytest.mark.parametrize('field,value', [('portal','other-portal'),('account','other-account'),('attempt','other-attempt'),('role','role-2')])
def test_r05_exact_scoping_not_company_aliases(field, value):
    result, binding = setup()
    raw = event(binding,'wrong','application_state','rejected')
    raw['scope'][field] = value
    changed, report = preview([raw], result, binding)
    assert report['quarantine'][0]['code'] == 'unmatched_scope'
    assert changed.evaluated[0].decision.action_band.value == 'primary'


def test_r03_block_restoration_and_duplicate_replay():
    result, binding = setup()
    blocked = event(binding,'block','project_access','blocked',event_at=(NOW-timedelta(days=2)).isoformat(),observed_at=(NOW-timedelta(days=1)).isoformat())
    restored = event(binding,'restore','project_access','accessible',supersedes=['block'])
    first, initial = preview([blocked], result, binding)
    assert first.evaluated[0].decision.next_action != 'apply'
    second, report = preview([restored,blocked], result, binding, previous=initial['events'])
    assert second.evaluated[0].decision.next_action == 'apply'
    assert report['counts'] == dict(input=2, accepted=1, duplicate=1, quarantined=0)
    again, repeat = preview([restored],result,binding,previous=report['events'])
    assert repeat['counts']['duplicate'] == 1
    assert decision_fingerprint(second) == decision_fingerprint(again)
    assert report['projections'][0].dispositions['block'] == 'superseded'


def test_r02_marketing_and_unearned_payment_are_quarantined():
    result, binding = setup()
    records = [event(binding,'x','marketing','invite'), event(binding,'pay','collected_payment','received')]
    _, report = preview(records,result,binding)
    assert report['counts']['quarantined'] == 2


def test_r13_changed_buyer_scope_requires_reconfirmation():
    result, binding = setup()
    records = [event(binding,'reply','buyer_reply','scope_changed',actor='buyer'),
               event(binding,'scope','scope_revision','revision-2',actor='buyer'),
               event(binding,'quote','agreed_scope_revision','revision-1',actor='user',source_kind='user_report')]
    changed, _ = preview(records,result,binding)
    row = changed.evaluated[0]
    assert row.decision.next_action == 'clarify_scope'
    assert 'no work or payment' in row.decision.next_step
    assert row.assessment.selected_pay == result.evaluated[0].assessment.selected_pay


def assessment_events(binding, **checks):
    return [event(binding,'invite','assessment_state','invited'),
        event(binding,'route','assessment_route','https://example.org/assessment'),
        event(binding,'checks','action_checks',dict(privacy=True,schedule=True,equipment=True,cost=True) | checks,
              actor='user',source_kind='user_report')]


def test_r09_existing_application_can_progress_to_compatible_assessment():
    result,binding=setup()
    records = [event(binding,'submitted','application_state','submitted'), *assessment_events(binding)]
    changed,_=preview(records,result,binding)
    assert changed.evaluated[0].decision.next_action == 'complete_known_step'
    assert changed.evaluated[0].decision.action_band.value == 'primary'
    assert 'not selection' in changed.evaluated[0].decision.next_step


@pytest.mark.parametrize('field', ['privacy','schedule','equipment','cost'])
@pytest.mark.parametrize('missing', [None, False])
def test_r10_unknown_or_unacceptable_prerequisite_blocks_test(field, missing):
    result,binding=setup()
    records=assessment_events(binding)
    records[-1]['value'][field]=missing
    changed,_=preview(records,result,binding)
    assert changed.evaluated[0].decision.next_action=='verify'
    assert changed.evaluated[0].decision.action_band.value=='verify'


def test_r41_user_completion_explicitly_supersedes_older_invite():
    result,binding=setup()
    records=assessment_events(binding)
    records.append(event(binding,'done','assessment_state','completed',actor='user',source_kind='user_report',
        event_at=(NOW-timedelta(minutes=30)).isoformat(),observed_at=NOW.isoformat(),supersedes=['invite']))
    changed,_=preview(records,result,binding)
    assert changed.evaluated[0].decision.next_action=='await_response'


def test_different_actors_without_explicit_supersession_conflict():
    result,binding=setup()
    records=[event(binding,'blocked','project_access','blocked'),
        event(binding,'open','project_access','accessible',actor='user',source_kind='user_report')]
    changed,report=preview(records,result,binding)
    assert report['projections'][0].conflicts==['project_access']
    assert changed.evaluated[0].decision.lifecycle=='watch'
    assert changed.evaluated[0].decision.next_action=='verify'


def test_unknown_event_time_is_not_fetch_time():
    result,binding=setup()
    records=assessment_events(binding)
    records[0]['event_at']=None
    changed,_=preview(records,result,binding)
    assert changed.evaluated[0].decision.next_action=='verify'


def test_expired_invitation_never_becomes_new_deadline():
    result,binding=setup()
    records=assessment_events(binding)
    records[0]['expires_at']=(NOW-timedelta(minutes=20)).isoformat()
    changed,_=preview(records,result,binding)
    assert changed.evaluated[0].decision.next_action=='verify'


def test_import_order_and_collisions_are_deterministic():
    result,binding=setup()
    records=assessment_events(binding)
    left,report=preview(records,result,binding)
    right,rev=preview(list(reversed(records)),result,binding)
    assert report['projection_fingerprint']==rev['projection_fingerprint']
    assert decision_fingerprint(left)==decision_fingerprint(right)
    bad=dict(records[0],value='completed')
    for order in [[records[0],bad],[bad,records[0]]]:
        _, collision=preview(order,result,binding)
        assert collision['counts']['quarantined']==2
        assert collision['events']==[]


def test_stale_seed_cannot_override_reviewed_event():
    result,binding=setup()
    old=event(binding,'old','project_access','blocked')
    seed=event(binding,'seed','project_access','accessible',source_kind='reviewed_seed',
        event_at=(NOW-timedelta(minutes=20)).isoformat(),observed_at=NOW.isoformat(),supersedes=['old'])
    changed,report=preview([old,seed],result,binding)
    assert report['counts']['quarantined']==1
    assert changed.evaluated[0].decision.lifecycle=='watch'


def test_config_conflict_not_silently_overwritten(monkeypatch):
    result,binding=setup()
    monkeypatch.setattr(CONFIG.v55,'account_states',[dict(canonical_id=binding.canonical_id,project_access='blocked')])
    changed,_=preview([event(binding,'open','project_access','accessible')],result,binding)
    assert changed.evaluated[0].assessment.lifecycle_reason=='outcome_conflict'


def test_support_and_scope_are_required_and_no_secrets_in_quarantine():
    result,binding=setup()
    raw=event(binding,'bad','assessment_route','https://example.org/test?token=synthetic-secret')
    _,report=preview([raw],result,binding)
    assert 'synthetic-secret' not in json.dumps(report['quarantine'])
    assert report['counts']['quarantined']==1


@pytest.mark.parametrize('url', ['https://127.0.0.1/test','https://localhost/test',
                                'https://example.org/test\nforged-line'])
def test_nonpublic_or_control_character_routes_are_quarantined(url):
    result,binding=setup()
    _,report=preview([event(binding,'bad','assessment_route',url)],result,binding)
    assert report['counts']['quarantined']==1


def test_binding_requires_exact_canonical_observation_and_url():
    result,binding=setup()
    for change in [dict(canonical_id='missing'),dict(observation_id='missing'),dict(role_url='https://example.org/wrong')]:
        with pytest.raises(ValueError):
            preview([],result,binding.model_copy(update=change))
    with pytest.raises(ValueError,match='multiple_active_attempts'):
        preview_outcomes(result,[],[binding,binding],as_of=NOW)


def test_no_outcomes_keeps_decisions_and_legacy_policy_is_not_enabled(monkeypatch):
    result,binding=setup()
    changed,_=preview([],result,binding)
    assert decision_fingerprint(result)==decision_fingerprint(changed)
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    with pytest.raises(ValueError,match='review_policy'):
        preview([],result,binding)


def test_review_cli_roundtrip_and_no_delivery(tmp_path,monkeypatch):
    import socket
    from jobhound.v41.replay import write_snapshot
    from jobhound.v41.engine import evaluate_raw
    from jobhound.v41.store import V41Store
    from jobhound.store import Store
    from jobhound import cli
    def forbidden(*args,**kwargs):
        pytest.fail('network or production store called')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(V41Store,'__init__',forbidden)
    monkeypatch.setattr(Store,'__init__',forbidden)
    raw=[dict(source='ashby',raw=dict(id='one',title='Bengali AI Response Evaluator',
        _company='Example',jobUrl='https://jobs.ashbyhq.com/example/one',
        location='India',isRemote=True,descriptionPlain=DESCRIPTION))]
    result=evaluate_raw(raw,as_of=NOW)
    snapshot=write_snapshot(raw,result,directory=tmp_path)
    item=result.evaluated[0]
    binding=OutcomeBinding(scope=OutcomeScope(provider='example',portal='main',role='one',account='local',attempt='one'),
        canonical_id=item.canonical.canonical_id,observation_id=item.canonical.observations[0].observation_id,
        role_url=item.job.url,reviewed=True)
    bindings=tmp_path/'bindings.jsonl'; bindings.write_text(binding.model_dump_json()+'\n')
    events=tmp_path/'events.jsonl'; events.write_text(json.dumps(event(binding,'reject','application_state','rejected'))+'\n')
    args=cli.build_parser().parse_args(['review','outcomes',str(snapshot),'--events',str(events),
        '--bindings',str(bindings),'--as-of',NOW.isoformat(),'--output-dir',str(tmp_path/'result')])
    args.func(args)
    manifest=json.loads((tmp_path/'result/manifest.json').read_text())
    assert manifest['import_counts']['accepted']==1
    assert manifest['delivery_called'] is False
    assert 'await_change' in (tmp_path/'result/preview.md').read_text(encoding='utf-8')
    target,again=run_outcome_review(snapshot,events,bindings,tmp_path/'again',as_of=NOW,
                                  previous_events=tmp_path/'result/events.jsonl')
    assert again['import_counts']['duplicate']==1
    assert again['decision_fingerprint']==manifest['decision_fingerprint']
    with pytest.raises(ValueError,match='output_requires'):
        run_outcome_review(snapshot,events,bindings,target,as_of=NOW)
    with pytest.raises(ValueError,match='review_time_precedes'):
        run_outcome_review(snapshot,events,bindings,tmp_path/'too-early',as_of=NOW-timedelta(days=1))


@pytest.mark.parametrize('value', ['yes', 'true', 1, 'false', 0])
def test_prerequisites_require_literal_boolean(value):
    result,binding=setup()
    rows=assessment_events(binding)
    rows[-1]['value']['privacy']=value
    changed,report=preview(rows,result,binding)
    assert report['counts']['quarantined']==1
    assert changed.evaluated[0].decision.next_action=='verify'


@pytest.mark.parametrize('value', [1, 'true', False, None])
def test_review_marker_cannot_be_coerced(value):
    result,binding=setup()
    _,report=preview([event(binding,'bad','application_state','rejected',reviewed=value)],result,binding)
    assert report['counts']['quarantined']==1


@pytest.mark.parametrize('kind', ['future','foreign','broken_edge','duplicate'])
def test_previous_journal_is_not_a_validation_bypass(kind):
    result,binding=setup()
    row=event(binding,'old','project_access','accessible')
    if kind=='future':
        row['observed_at']=(NOW+timedelta(days=1)).isoformat()
    elif kind=='foreign':
        row['scope']['account']='different'
    elif kind=='broken_edge':
        row['supersedes']=['missing']
    previous=[OutcomeEvent.model_validate(row)]
    if kind=='duplicate':
        previous*=2
    with pytest.raises(ValueError,match='invalid_previous_journal'):
        preview([],result,binding,previous=previous)


def test_invitation_does_not_recommend_test_before_requirements_known():
    result,binding=setup(observation(content_state='snippet_only'))
    changed,_=preview(assessment_events(binding),result,binding)
    assert changed.evaluated[0].decision.next_action=='verify'
    assert changed.evaluated[0].decision.action_band.value!='primary'


def test_terminal_rejection_requires_explicit_reopening():
    result,binding=setup()
    reject=event(binding,'reject','application_state','rejected',
                 event_at=(NOW-timedelta(days=2)).isoformat(), observed_at=(NOW-timedelta(days=1)).isoformat())
    reopen=event(binding,'open','application_state','reopened')
    changed,report=preview([reject,reopen],result,binding)
    assert report['projections'][0].conflicts==['application_state']
    assert changed.evaluated[0].decision.next_action=='verify'
    reopen['supersedes']=['reject']
    changed,report=preview([reject,reopen],result,binding)
    assert not report['projections'][0].conflicts
    assert changed.evaluated[0].decision.next_action=='apply'


def test_supersession_cannot_cross_predicate_and_invalid_edges_cascade():
    result,binding=setup()
    rows=[event(binding,'block','project_access','blocked'),
          event(binding,'bad','assessment_state','invited',supersedes=['block']),
          event(binding,'child','assessment_state','completed',supersedes=['bad'],
                event_at=(NOW-timedelta(minutes=20)).isoformat(),observed_at=NOW.isoformat())]
    changed,report=preview(rows,result,binding)
    assert report['counts']['quarantined']==2
    assert changed.evaluated[0].decision.next_action!='apply'


@pytest.mark.parametrize('version',[True,1.0,'1'])
def test_schema_version_is_an_integer_not_a_coerced_flag(version):
    result,binding=setup()
    _,report=preview([event(binding,'bad','project_access','blocked',schema_version=version)],result,binding)
    assert report['counts']['quarantined']==1


def test_combined_journal_cannot_exceed_reimport_record_limit(monkeypatch):
    from jobhound.v41 import outcomes
    monkeypatch.setattr(outcomes,'MAX_JOURNAL_EVENTS',2)
    result,binding=setup()
    previous=[OutcomeEvent.model_validate(event(binding,'submitted','application_state','submitted'))]
    with pytest.raises(ValueError,match='outcome_journal_limit'):
        preview(assessment_events(binding),result,binding,previous=previous)
