"""Thread actors, chronology and partial retrieval through the actual engine."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.community import project_thread, observation_thread, hydrate_community_topic
from jobhound.v41.engine import evaluate_observations, decision_fingerprint
from jobhound.v41.hydration import RetrievalBudget, RetrievalPolicy, child_observation, hydrate_public_posting
from jobhound.v41.models import ListingObservation, SourceKind
from jobhound.v41.resolve import hydrate_result

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
URL = 'https://community.make.com/t/whatsapp-workflow-repair/123'
BODY = 'I need a developer to repair a WhatsApp and Google Sheets workflow. Requirements: Remote applicants in India may apply. No degree or prior years of experience required.'


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])


def post(key=1, uid=10, text=BODY, **updates):
    raw = dict(id=key, post_number=key, user_id=uid, topic_id=123, post_type=1,
        cooked='<p>'+text+'</p>', created_at=(NOW-timedelta(days=30)).isoformat(),
        updated_at=(NOW-timedelta(days=29)).isoformat(), hidden=False, wiki=False)
    raw.update(updates)
    if 'created_at' in updates and 'updated_at' not in updates:
        raw['updated_at'] = updates['created_at']
    return raw


def topic(*posts, **updates):
    rows = list(posts or [post()])
    raw = dict(id=123, title='WhatsApp workflow repair developer wanted', archetype='regular', category_id=74,
        post_stream={'stream':[r['id'] for r in rows], 'posts':rows}, closed=False)
    raw.update(updates)
    return raw


def parent(description='', pay=None):
    job = Job(source='serp', title='WhatsApp workflow repair developer wanted', company='Make', url=URL,
        description=description, location='India', is_remote=True, pay_raw=pay)
    return ListingObservation(observation_id='discovery', source='serp', job=job, original_url=URL,
        normalized_url=URL, source_kind=SourceKind.AGGREGATOR_UNRESOLVED, captured_at=NOW, content_state='snippet_only')


def retrieve(payload, *, extra_pages=(), retrieval_policy=None, selected_post_id=None, url=URL):
    seen=[]
    def handler(request):
        seen.append(request)
        value = payload if len(seen)==1 else extra_pages[len(seen)-2]
        return value if isinstance(value, httpx.Response) else httpx.Response(200,json=value)
    async def run():
        budget=RetrievalBudget(retrieval_policy or RetrievalPolicy())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if selected_post_id is None:
                result=await hydrate_public_posting(url,parent().job,client=client,budget=budget)
            else:
                result=await hydrate_community_topic(url,parent().job,client=client,budget=budget,selected_post_id=selected_post_id)
        return result,budget
    outcome,budget=asyncio.run(run())
    return outcome,seen,budget


def assessed(outcome, original=None):
    original=original or parent()
    child=child_observation(original,outcome)
    result=evaluate_observations([original,child],as_of=outcome.captured_at or NOW)
    assert result.accounting_ok and len(result.evaluated)==1
    return result,result.evaluated[0],child


def test_seller_quotes_never_become_buyer_budget_even_in_a_search_snippet():
    payload=topic(post(),post(2,20,'I offer this workflow repair for USD 120 fixed.'),post(3,30,'Hire me for USD 250 fixed.'))
    outcome,_,_=retrieve(payload)
    original=parent('Project budget: USD 120-250 per hour', 'USD 120-250 per hour')
    result,row,child=assessed(outcome,original)
    assert row.assessment.document_type=='buyer_request'
    assert row.assessment.selected_pay is None
    assert not row.assessment.pay_conflict
    assert all(c.scope=='unattributed_thread_copy' for c in row.assessment.pay_candidates)
    assert len(result.observations)==2 and original.job.pay_raw=='USD 120-250 per hour'
    assert observation_thread(child).posts[1].actor_role=='reply_seller'
    assert '120' not in row.job.description and '250' not in row.job.description


def test_buyer_budget_binds_actor_native_post_and_exact_text_span():
    payload=topic(post(text=BODY+'\nBudget: USD 500 fixed.'))
    outcome,_,_=retrieve(payload)
    _,row,child=assessed(outcome)
    pay=row.assessment.selected_pay
    assert pay is not None and pay.amount_low==500 and pay.actual_unit=='fixed_project'
    assert pay.actor=='forum_actor:10' and pay.scope=='community_request:123:1'
    assert pay.structured_path=='/community_pages/0/post_stream/posts/0/cooked'
    assert pay.structured_payload_sha256==hashlib.sha256(payload['post_stream']['posts'][0]['cooked'].encode()).hexdigest()
    assert pay.raw==child.job.description[pay.source_span_start:pay.source_span_end]
    assert pay.source_text_sha256==hashlib.sha256(child.job.description.encode()).hexdigest()
    assert row.decision.next_action not in {'bid','start_paid_work','begin_paid_work'}


def test_seller_original_remains_a_non_opportunity():
    payload=topic(post(text='I offer workflow repairs. My services start at USD 249 fixed.'),title='For hire: workflow repair service')
    outcome,_,_=retrieve(payload)
    _,row,child=assessed(outcome)
    assert row.assessment.document_type=='seller_service'
    assert row.decision.action_band.value=='reject'
    assert row.assessment.selected_pay is None
    assert observation_thread(child).buyer_actor_id is None
    assert observation_thread(child).last_substantive_buyer_at is None


def test_seller_withdrawal_cannot_become_public_buyer_closure():
    payload=topic(post(text='I offer workflow repairs. This project is closed.'),title='For hire: workflow repair service')
    outcome,_,_=retrieve(payload)
    parsed=outcome.payload['community_thread']
    assert parsed['intent']=='seller' and parsed['vacancy_state']=='unknown'
    assert parsed['closure_post_id'] is None and parsed['last_substantive_buyer_at'] is None


def test_later_buyer_unpaid_only_update_withdraws_paid_request():
    outcome,_,_=retrieve(topic(post(),post(2,10,'No budget. Free help only, not hiring.')))
    _,row,child=assessed(outcome)
    assert observation_thread(child).intent=='discussion'
    assert row.decision.action_band.value=='reject' and row.assessment.selected_pay is None


def test_explicit_new_paid_request_can_supersede_earlier_unpaid_only_update():
    outcome,_,_=retrieve(topic(post(),post(2,10,'Free help only, not hiring.'),post(3,10,'I need a developer for paid work. Budget USD 400 fixed.')))
    assert outcome.payload['community_thread']['intent']=='buyer'


def test_category_help_needs_paid_scope_even_when_the_thread_is_open_with_a_route():
    payload=topic(post(text=BODY.replace('I need a developer','I need help')))
    outcome,_,_=retrieve(payload)
    outcome.vacancy_state='verified_open';outcome.application_route_state='observed_public_route'
    _,row,_=assessed(outcome)
    assert row.decision.action_band.value=='verify' and row.decision.next_action=='verify'
    assert row.assessment.verification_tasks[0]['missing_fact']=='community_paid_scope'
    assert 'paid contractor help' in row.decision.next_step


def test_new_explicit_buyer_hiring_reply_resolves_root_help_intent_caveat():
    payload=topic(post(text=BODY.replace('I need a developer','I need help')),
                  post(2,10,'I need a developer for paid work. Budget: USD 400 fixed.'))
    outcome,_,_=retrieve(payload)
    assert outcome.payload['community_thread']['intent']=='buyer'
    assert outcome.payload['community_thread']['caveats']==[]


def test_other_buyer_under_seller_topic_requires_its_own_post_scope():
    payload=topic(post(text='I offer workflow repairs.'),post(2,20,BODY+' Budget: USD 400 fixed.'),title='For hire: workflow service')
    default,_,_=retrieve(payload)
    _,root,_=assessed(default)
    assert root.assessment.document_type=='seller_service'
    selected,_,_=retrieve(payload,selected_post_id=2)
    _,row,child=assessed(selected)
    assert row.assessment.document_type=='buyer_request'
    assert row.assessment.selected_pay.amount_low==400
    assert row.assessment.selected_pay.actor=='forum_actor:20'
    assert row.job.url.endswith('/123/2') and observation_thread(child).selected_post_id==2
    assert 'I offer' not in row.job.description


def test_saved_individual_buyers_share_capture_but_not_identity_or_budget():
    from jobhound.v41.community import thread_outcome,saved_thread_observation
    pages=[topic(post(text='I offer workflow repairs.'),post(2,20,BODY+' Budget: USD 400 fixed.'),
                 post(3,30,BODY+' Budget: USD 800 fixed.'),title='For hire: workflow service')]
    observations=[saved_thread_observation(thread_outcome(project_thread(pages,URL,NOW,selected_post_id=key),
                    pages,selected_post_id=key)) for key in (2,3)]
    assert observations[0].content_hash==observations[1].content_hash
    assert observations[0].observation_id!=observations[1].observation_id
    result=evaluate_observations(observations,as_of=NOW,raw_count=0)
    assert result.accounting_ok and len(result.evaluated)==2
    assert sorted(row.assessment.selected_pay.amount_low for row in result.evaluated)==[400,800]
    assert all(not row.assessment.pay_conflict for row in result.evaluated)


def test_recent_applicant_and_buyer_thanks_do_not_refresh_original_demand():
    rows=[post(),post(2,20,'I am available.',created_at=(NOW-timedelta(hours=2)).isoformat()),
          post(3,10,'Thank you!',created_at=(NOW-timedelta(hours=1)).isoformat())]
    parsed=project_thread([topic(*rows)],URL,NOW)
    assert parsed.original_at==NOW-timedelta(days=30)
    assert parsed.last_substantive_buyer_at==parsed.original_at
    assert parsed.posts[0].edited_at==NOW-timedelta(days=29)
    assert parsed.observed_at==NOW


def test_substantive_buyer_scope_update_has_its_own_date():
    parsed=project_thread([topic(post(),post(2,10,'The scope now requires self-hosted n8n migration.',created_at=(NOW-timedelta(hours=1)).isoformat()))],URL,NOW)
    assert parsed.original_at==NOW-timedelta(days=30)
    assert parsed.last_substantive_buyer_at==NOW-timedelta(hours=1)
    assert 'self-hosted' in parsed.description


@pytest.mark.parametrize('html', [
    '<blockquote>Budget: USD 900 fixed. This project is closed.</blockquote>',
    '<aside class="quote"><div>Budget: USD 900 fixed. This project is closed.</div></aside>',
    '<span hidden>Budget: USD 900 fixed. This project is closed.</span>',
    '<div style="display:none">Budget: USD 900 fixed. This project is closed.</div>',
    '<script>Budget: USD 900 fixed. This project is closed.</script>',
    '<pre><code>Budget: USD 900 fixed. This project is closed.</code></pre>',
])
def test_quote_hidden_and_code_text_cannot_establish_budget_or_closure(html):
    payload=topic(post(cooked='<p>'+BODY+'</p>'+html))
    outcome,_,_=retrieve(payload)
    _,row,child=assessed(outcome)
    assert row.assessment.selected_pay is None
    assert observation_thread(child).vacancy_state=='unknown'
    assert '900' not in child.job.description


def test_exact_buyer_closure_wins_over_hiring_snippet_and_is_preserved():
    payload=topic(post(),post(2,10,'This project is closed. I have hired a developer.',created_at=(NOW-timedelta(days=1)).isoformat()))
    outcome,_,_=retrieve(payload)
    _,row,child=assessed(outcome,parent('Hiring now. Budget USD 500 fixed.'))
    assert row.decision.lifecycle=='closed' and row.decision.action_band.value=='reject'
    parsed=observation_thread(child)
    assert parsed.closure_post_id==2 and parsed.posts[1].status_signal=='closed'


def test_seller_closure_and_moderator_lock_are_not_buyer_closure():
    payload=topic(post(),post(2,20,'This project is closed.'),closed=True)
    outcome,_,_=retrieve(payload)
    _,row,child=assessed(outcome)
    assert observation_thread(child).vacancy_state=='unknown'
    assert 'community_reply_route_locked' in row.assessment.unresolved
    assert row.decision.lifecycle!='closed'


def test_later_buyer_reopening_retains_closure_fact():
    parsed=project_thread([topic(post(),post(2,10,'This project is closed.'),post(3,10,'This project has been reopened.'))],URL,NOW)
    assert parsed.vacancy_state=='unknown' and parsed.posts[1].status_signal=='closed'
    assert parsed.posts[2].status_signal=='reopened' and parsed.closure_post_id is None


def test_contradictory_buyer_status_stays_unresolved():
    parsed=project_thread([topic(post(text=BODY+'\nThis project is closed. We are still hiring.'))],URL,NOW)
    assert not parsed.complete and parsed.vacancy_state=='unknown'
    assert 'conflicting_buyer_status' in parsed.issues


def test_bounded_pagination_uses_exact_post_ids_and_stops_when_complete():
    whole=topic(post(),post(2,20,'I can do it.'),post(3,10,'Budget: USD 400 fixed.'))
    first=deepcopy(whole);first['post_stream']['posts']=first['post_stream']['posts'][:1]
    second={'post_stream':{'posts':whole['post_stream']['posts'][1:]}}
    outcome,requests,budget=retrieve(first,extra_pages=[second])
    assert outcome.state=='complete' and len(requests)==budget.requests==2
    assert requests[1].url.path=='/t/123/posts.json'
    assert requests[1].url.params.get_list('post_ids[]')==['2','3']
    _,row,_=assessed(outcome)
    assert row.assessment.selected_pay.amount_low==400


def test_selected_buyer_outside_initial_page_is_prioritized_and_actor_scoped():
    original=post(text='I offer workflow repairs.')
    buyer=post(50,20,BODY+' Budget: USD 400 fixed.',post_number=22)
    first=topic(original,title='For hire: workflow services')
    first['post_stream']['stream']=[1,*range(2,22),50]
    remaining=[post(i,30,'I am available.') for i in range(2,22)]
    second={'post_stream':{'posts':[buyer,*remaining[:19]]}}
    third={'post_stream':{'posts':remaining[19:]}}
    outcome,requests,_=retrieve(first,extra_pages=[second,third],selected_post_id=50)
    assert requests[1].url.params.get_list('post_ids[]')[0]=='50'
    assert outcome.state=='complete' and outcome.job.url.endswith('/123/22')
    _,row,child=assessed(outcome)
    assert row.assessment.selected_pay.actor=='forum_actor:20'
    assert row.assessment.selected_pay.amount_low==400
    assert observation_thread(child).selected_post_id==50


@pytest.mark.parametrize('route',['https://community.make.com/t/123/2',URL+'/2'])
def test_reply_url_fetches_selected_post_and_does_not_fall_back_to_seller_root(route):
    first=topic(post(text='I offer workflow repairs.'),title='For hire: workflow services')
    first['post_stream']['stream']=[1,50]
    second={'post_stream':{'posts':[post(50,20,BODY+' Budget: USD 400 fixed.',post_number=2)]}}
    outcome,requests,_=retrieve(first,extra_pages=[second],url=route)
    assert len(requests)==2 and outcome.job.url.endswith('/123/2')
    assert outcome.payload['community_selected_post_id']==50
    assert observation_thread(child_observation(parent(),outcome)).buyer_actor_id==20


def test_missing_selected_post_at_cap_keeps_pages_without_recommending_the_root():
    first=topic(post(text='I offer workflow repairs.'));first['post_stream']['stream']=[1,50]
    outcome,requests,_=retrieve(first,selected_post_id=50,retrieval_policy=RetrievalPolicy(community_max_pages=1))
    assert len(requests)==1 and outcome.state=='partial' and outcome.job is None
    assert outcome.error=='selected_post_missing' and outcome.payload['community_pages']


def test_initial_page_without_root_recovers_root_before_projection():
    first=topic(post(2,20,'I can do it.'));first['post_stream']['stream']=[1,2]
    second={'post_stream':{'posts':[post()]}}
    outcome,requests,_=retrieve(first,extra_pages=[second])
    assert len(requests)==2 and outcome.state=='complete'
    assert outcome.payload['community_thread']['selected_post_id']==1


def test_selected_native_id_cannot_disagree_with_individual_reply_route():
    data=topic(post(),post(50,20,BODY,post_number=2))
    outcome,_,_=retrieve(data,selected_post_id=1,url=URL+'/2')
    assert outcome.job is None and outcome.error=='selected_post_route_mismatch'


@pytest.mark.parametrize('failure', [httpx.Response(429,headers={'Retry-After':'120'}),httpx.Response(503),httpx.Response(200,json={'post_stream':{'posts':None}})])
def test_failed_later_page_keeps_valid_prefix_and_explicit_partial_state(failure):
    first=topic(post());first['post_stream']['stream']=[1,2]
    outcome,requests,budget=retrieve(first,extra_pages=[failure])
    assert outcome.state=='partial' and outcome.job.description
    assert outcome.error is not None and len(requests)==2
    if failure.status_code in {429,503}:
        assert budget.cooldown_until
    _,row,child=assessed(outcome)
    assert 'thread_pagination_incomplete' in row.assessment.unresolved
    assert child.content_state=='partial' and child.truncated


def test_one_page_cap_cannot_claim_complete_or_current_closure():
    first=topic(post(text=BODY+' This project is closed.'));first['post_stream']['stream']=[1,2]
    outcome,requests,_=retrieve(first,retrieval_policy=RetrievalPolicy(community_max_pages=1))
    assert len(requests)==1 and outcome.state=='partial' and outcome.vacancy_state=='unknown'
    assert outcome.payload['community_thread']['closure_post_id']==1


def test_fully_fetched_hidden_post_is_content_unknown_not_missing_pagination():
    outcome,_,_=retrieve(topic(post(),post(2,20,'Hidden content',hidden=True)))
    assert outcome.state=='partial' and outcome.error=='unattributable_post_content'
    coverage=outcome.payload['community_coverage']
    assert coverage['fetched_posts']==coverage['expected_posts']==2
    assert 'thread_pagination_incomplete' not in outcome.payload['community_thread']['issues']


def test_post_cap_keeps_original_and_reports_missing_coverage():
    outcome,requests,_=retrieve(topic(post(),post(2,20,'Hello')),retrieval_policy=RetrievalPolicy(community_max_posts=1))
    assert len(requests)==1 and outcome.state=='partial'
    assert outcome.payload['community_coverage']['fetched_posts']==1
    assert outcome.payload['community_coverage']['expected_posts']==2


@pytest.mark.parametrize('change', [
    lambda p:p.update(id=124), lambda p:p.update(archetype='private_message'),
    lambda p:p['post_stream']['posts'][0].update(topic_id=999),
    lambda p:p['post_stream']['posts'][0].update(id=True),
    lambda p:p['post_stream'].update(stream=[1,1]),
])
def test_malformed_or_wrong_topic_fails_before_an_accepted_observation(change):
    data=topic();change(data)
    outcome,_,_=retrieve(data)
    assert outcome.job is None and outcome.state=='failed'


def test_missing_dates_remain_unknown_and_do_not_borrow_capture_time():
    data=topic(post(created_at=None,updated_at='invalid'))
    parsed=project_thread([data],URL,NOW)
    assert parsed.original_at is parsed.last_substantive_buyer_at is None
    assert 'post_time_unknown' in parsed.issues


def test_saved_actor_projection_cannot_be_relabelled_or_description_mutated():
    outcome,_,_=retrieve(topic(post()))
    _,_,child=assessed(outcome)
    changed=child.model_copy(deep=True)
    changed.raw_payload['community_thread']['buyer_actor_id']=999
    assert observation_thread(changed) is None
    changed=child.model_copy(deep=True);changed.job.description+=' Budget USD 999 fixed.'
    result=evaluate_observations([changed],as_of=outcome.captured_at)
    assert result.evaluated[0].decision.action_band.value=='reject'
    assert 'source_quality:thread_evidence_invalid' in result.evaluated[0].assessment.blockers


def test_unknown_help_and_explicit_free_help_do_not_become_paid_demand():
    help_topic=topic(post(text='I need help connecting WhatsApp to Google Sheets.'))
    parsed=project_thread([help_topic],URL,NOW)
    assert parsed.intent=='buyer' and parsed.caveats==['paid_intent_unconfirmed']
    help_topic['category_id']=5
    assert project_thread([help_topic],URL,NOW).document_type=='discussion'
    help_topic['category_id']=74;help_topic['post_stream']['posts'][0]['cooked']='<p>I need help. Free help only, not hiring.</p>'
    assert project_thread([help_topic],URL,NOW).document_type=='discussion'


def test_old_ats_closure_is_not_overwritten_by_stale_watch():
    from test_v55_action_policy import observation
    closed=observation(vacancy_state='explicitly_closed')
    closed.job.posted_at=NOW-timedelta(days=60)
    result=evaluate_observations([closed],as_of=NOW)
    assert result.evaluated[0].decision.lifecycle=='closed'
    assert result.evaluated[0].decision.action_band.value=='reject'


def test_partial_thread_health_is_degraded_and_verification_is_not_reset(tmp_path):
    original=parent()
    result=evaluate_observations([original],as_of=NOW)
    data=topic(post());data['post_stream']['stream']=[1,2]
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=data) if request.url.path.endswith('/123.json') else httpx.Response(429,headers={'Retry-After':'60'})
    report=asyncio.run(hydrate_result(result,policy=RetrievalPolicy(shortlist=1,request_budget=3),
                                    review_namespace=tmp_path,transport=httpx.MockTransport(handler)))
    assert report['changed']==1 and report['stage_health']['status']=='partial'
    assert report['stage_health']['partial']==1 and report['stage_health']['remote_http_errors']==1
    assert report['adapter_health'][0]['status']=='partial'
    saved=json.loads((tmp_path/'retrieval_state.json').read_text(encoding='utf-8'))
    assert max(saved['verification_cycles'].values())==1
    assert saved['verification_first_failed']


def test_topic_title_pay_has_title_provenance_not_body_offsets():
    payload=topic(post(),title='Hiring WhatsApp API workflow developer: USD 25 per hour')
    outcome,_,_=retrieve(payload)
    _,row,_=assessed(outcome)
    pay=row.assessment.selected_pay
    assert pay is not None and pay.structured_path=='/community_pages/0/title'
    assert pay.structured_payload_sha256==hashlib.sha256(payload['title'].encode()).hexdigest()
    assert pay.source_field=='title' and pay.actor=='forum_actor:10'


def test_saved_page_cli_is_network_and_store_free_and_replays(tmp_path,monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from jobhound.v41 import store
    import socket
    original_connect=socket.socket.connect
    original_connect_ex=socket.socket.connect_ex
    calls=[]
    def forbidden(*a,**kw):
        calls.append('store');raise AssertionError('production store forbidden')
    monkeypatch.setattr(store,'V41Store',forbidden)
    raw=tmp_path/'input.json';raw.write_text(json.dumps(topic(post(text=BODY+' Budget: USD 500 fixed.'))),encoding='utf-8')
    output=tmp_path/'review'
    monkeypatch.setattr(sys,'argv',['community_review','--input',str(raw),'--url',URL,'--observed-at',NOW.isoformat(),'--output',str(output)])
    spec=importlib.util.spec_from_file_location('community_review',Path(__file__).resolve().parents[1]/'tools'/'community_review.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.main()
    assert socket.socket.connect is original_connect
    assert socket.socket.connect_ex is original_connect_ex
    with module._offline_network(), socket.socket() as blocked:
        with pytest.raises(RuntimeError,match='forbids network'):
            blocked.connect(('127.0.0.1',1))
        with pytest.raises(RuntimeError,match='forbids network'):
            blocked.connect_ex(('127.0.0.1',1))
    async def event_loop_control():
        return True
    assert asyncio.run(event_loop_control())
    manifest=json.loads((output/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['replay_equal'] and manifest['accounting_ok'] and manifest['presentation_ok']
    assert manifest['input_sha256'][str(raw.resolve())]==hashlib.sha256(raw.read_bytes()).hexdigest()
    assert not calls and manifest['provider_calls']==0
    audit=json.loads((output/'audit.json').read_text(encoding='utf-8'))
    assert audit['decisions'][0]['assessment']['selected_pay']['actor']=='forum_actor:10'
    assert len(list(output.glob('*.jsonl')))==1
