"""Source caps/cadence use actual bounded HTTP and explicit coverage evidence."""
import asyncio
from datetime import timedelta
import json

import httpx
import pytest

from jobhound.bounded_transport import BoundedTransport,RequestDeferred,RequestLimits,RunBudget
from jobhound.source_policy import SourceCheck,SourcePlan,SourcePolicyDecision,source_value_report
from test_v6_isolation_limits import context,NOW

def route(key='make',mode='core',category='buyers',**changes):
    return dict(source_id=key,family=key,category=category,mode=mode,parser_state='capture_only',
        requests=8,inspections=12,searches=12,hosts=['example.org'],task_scope_ref='task:'+key,
        evidence_refs=['synthetic:capture:'+key],admission_ref='admit:'+key if mode!='watch' else None,
        reviewed=True)|changes

def plan(*routes,**updates):
    routes=routes or (route(),route('n8n','pilot'),route('watch','watch'))
    raw=dict(schema_version=1,request_ceiling=60,inspection_ceiling=12,search_ceiling=12,routes=list(routes),
        categories=[dict(category=category,requests=60,inspections=12,searches=12) for category in sorted({r['category'] for r in routes})],
        policy_decisions=[dict(decision_id=r['admission_ref'],source_id=r['source_id'],observed_at=NOW.isoformat(),
            disposition='continue',reason='bounded_capture',evidence_refs=['synthetic:proof'],reviewed=True)
            for r in routes if r['admission_ref']],reviewed=True)
    raw.update(updates);return SourcePlan.model_validate(raw)

def owner(tmp_path,p=None,**kwargs):
    return RunBudget(context(tmp_path),RequestLimits(),source_plan=p or plan(),**kwargs)

def check(key='check-1',source='make',**changes):
    raw=dict(check_id=key,source_id=source,query_family='buyers',profile_hash='a'*16,
        window_start=(NOW-timedelta(days=2)).isoformat(),window_end=(NOW-timedelta(days=1)).isoformat(),
        observed_at=NOW.isoformat(),allowed_pages=['page-1'],fetched_pages=['page-1'],bounded_set_complete=True,
        retrieval_state='complete',novelty_evaluated=True,novel_action_ids=[],evidence_ref='synthetic:check:'+key,
        support_sha256='a'*64,reviewed=True)
    raw.update(changes);return SourceCheck.model_validate(raw)

def send(budget,calls,scopes):
    async def run():
        async def handler(request):
            calls.append(str(request.url));await asyncio.sleep(.001)
            return httpx.Response(200,text='public document')
        async with httpx.AsyncClient(transport=BoundedTransport(httpx.MockTransport(handler),budget)) as client:
            async def one(source,doc):
                try:
                    with budget.sources.inspection(source,doc):return await client.get('https://example.org/'+doc)
                except RequestDeferred as exc:return str(exc)
            return await asyncio.gather(*(one(*scope) for scope in scopes))
    return asyncio.run(run())

def test_two_exploratory_inspections_are_ceiling_not_quota_and_actual_ratio_is_reported(tmp_path):
    budget=owner(tmp_path);calls=[]
    results=send(budget,calls,[('n8n','a'),('n8n','b'),('n8n','c'),('make','d')])
    assert len(calls)==3 and 'exploration_inspection_ceiling' in results
    receipt=budget.receipt()['source_budget']
    assert receipt['actual']['exploratory_inspections']==2 and receipt['actual']['inspections']==3
    assert receipt['actual']['exploration_inspection_share']==pytest.approx(2/3)
    assert receipt['planning_ceilings']['exploratory_inspections']==2
    assert next(r for r in receipt['routes'] if r['source_id']=='watch')['requests_reserved']==0

def test_search_and_document_counters_are_distinct_and_cache_costs_zero_http(tmp_path):
    budget=owner(tmp_path)
    budget.sources.search('n8n','query-1');budget.sources.search('n8n','query-1');budget.sources.search('n8n','query-2')
    with pytest.raises(RequestDeferred,match='exploration_search_ceiling'):budget.sources.search('n8n','query-3')
    with budget.sources.inspection('make','cached-document'):budget.sources.cached_inspection()
    report=budget.receipt()
    assert report['requests_reserved']==0
    assert report['source_budget']['actual']['inspections']==1 and report['source_budget']['actual']['searches']==2

def test_sent_429_consumes_request_and_inspection_and_cooldown_survives_restart(tmp_path):
    budget=owner(tmp_path)
    async def run():
        async with httpx.AsyncClient(transport=BoundedTransport(httpx.MockTransport(
            lambda r:httpx.Response(429,headers={'retry-after':'120'},text='throttled')),budget)) as client:
            with budget.sources.inspection('n8n','category-page'):
                assert (await client.get('https://example.org/category')).status_code==429
    asyncio.run(run());again=RunBudget(budget.context,budget.limits,source_plan=plan())
    assert again.receipt()['requests_reserved']==1 and again.receipt()['source_budget']['actual']['inspections']==1
    calls=[];result=send(again,calls,[('make','different')]);assert not calls and 'host_or_account_cooldown' in result
    assert again.receipt()['source_budget']['actual']['inspections']==1

def test_source_category_global_caps_share_one_atomic_owner_across_clients_and_resume(tmp_path):
    p=plan(route('a'),route('b'))
    raw=p.model_dump(mode='json');raw['categories'][0]['requests']=2;p=SourcePlan.model_validate(raw)
    budget=owner(tmp_path,p);calls=[]
    results=send(budget,calls,[('a','a1'),('b','b1'),('a','a2'),('b','b2')])
    assert len(calls)==2 and results.count('category_request_ceiling')==2
    resumed=RunBudget(budget.context,budget.limits,source_plan=p)
    assert resumed.receipt()['requests_reserved']==2
    assert sum(r['requests_reserved'] for r in resumed.receipt()['source_budget']['routes'])==2
    with pytest.raises(ValueError,match='identity reused'):
        RunBudget(budget.context,budget.limits,source_plan=plan(route('a'),route('b')))

def test_unsent_only_refund_and_nested_same_document_reservation_keeps_other_holder(tmp_path):
    budget=owner(tmp_path)
    with budget.sources.inspection('n8n','same'):
        with budget.sources.inspection('n8n','same'):pass
        assert budget.receipt()['source_budget']['actual']['inspection_reservations']==1
        budget.sources.cached_inspection()
    with budget.sources.inspection('n8n','unused'):pass
    assert budget.receipt()['source_budget']['actual']['inspections']==1
    calls=[];send(budget,calls,[('n8n','second')]);assert len(calls)==1
    assert budget.receipt()['source_budget']['actual']['inspections']==2

def test_different_run_or_workspace_cannot_borrow_active_scope(tmp_path):
    first=owner(tmp_path/'first');second=owner(tmp_path/'second')
    with first.sources.inspection('make','one'):
        with pytest.raises(RequestDeferred,match='source_run_mismatch'):
            second.reserve(httpx.Request('GET','https://example.org/one'))
    assert second.receipt()['requests_reserved']==0

def test_watch_unknown_host_and_private_requests_are_unsent(tmp_path):
    budget=owner(tmp_path)
    for key,code in [('watch','watch_route_audit_only'),('missing','unknown_source_route')]:
        with pytest.raises(RequestDeferred,match=code):
            with budget.sources.inspection(key,'one'):pass
    for request in [httpx.Request('GET','https://different.example.org/one'),
        httpx.Request('GET','http://example.org/one'),httpx.Request('GET','https://example.org/one',headers={'authorization':'synthetic'})]:
        with budget.sources.inspection('make','one'):
            with pytest.raises(RequestDeferred):budget.reserve(request)
    assert budget.receipt()['requests_reserved']==0 and budget.receipt()['source_budget']['actual']['inspections']==0

def test_existing_unclassified_adapters_remain_counted_without_watch_register_becoming_loop(tmp_path):
    budget=owner(tmp_path)
    budget.reserve(httpx.Request('GET','https://legacy.example.org/api'))
    receipt=budget.receipt()
    assert receipt['requests_reserved']==1 and receipt['source_budget']['actual']['unattributed_existing_requests']==1
    assert receipt['source_budget']['actual']['inspections']==0

def test_plan_cannot_add_requests_and_zero_capacity_is_preserved(tmp_path):
    with pytest.raises(ValueError,match='cannot add'):
        RunBudget(context(tmp_path),RequestLimits(requests=10),source_plan=plan())
    p=plan(route(requests=0,inspections=0,searches=0));budget=owner(tmp_path/'zero',p)
    with pytest.raises(RequestDeferred,match='route_inspection_ceiling'):
        with budget.sources.inspection('make','one'):pass

@pytest.mark.parametrize('change',[dict(reviewed='true'),dict(schema_version=True),dict(request_ceiling=61),
    dict(inspection_ceiling=13),dict(search_ceiling=13),dict(request_ceiling=True),dict(policy_decisions=[])])
def test_invalid_or_unlogged_overlay_is_rejected(change):
    with pytest.raises(ValueError):plan(**change)

def test_more_than_two_pilot_families_are_rejected_independently_of_other_routes():
    with pytest.raises(ValueError,match='two_pilot_families'):
        plan(route('a','pilot'),route('b','pilot'),route('c','pilot'))

def test_explicit_reduced_cadence_is_enforced_without_automatic_blacklist(tmp_path):
    raw=plan().model_dump(mode='json');raw['policy_decisions'][0]['disposition']='reduce_cadence'
    raw['routes'][0]['next_check_at']=(NOW+timedelta(days=7)).isoformat();p=SourcePlan.model_validate(raw)
    budget=owner(tmp_path,p)
    with pytest.raises(RequestDeferred,match='source_cadence_not_due'):
        with budget.sources.inspection('make','one'):pass
    assert budget.receipt()['requests_reserved']==0

@pytest.mark.parametrize('reason',['partial','failed','throttled','unsupported'])
def test_inadequate_checks_never_become_covered_zero_yield(reason):
    rows=[check('c'+str(i),retrieval_state=reason,novel_action_ids=None,novelty_evaluated=False,bounded_set_complete=False,fetched_pages=[])
        for i in range(8)]
    report=source_value_report(plan(),rows,as_of=NOW);cohort=report['cohorts'][0]
    assert cohort['adequately_covered_checks']==0 and cohort['covered_zero_yield_streak']==0
    assert not cohort['cadence_review_required'] and cohort['unknown_novelty_checks']==8
    assert cohort['measured_review_minutes'] is None and cohort['known_duplicate_observations'] is None

def test_five_adequate_zero_checks_flag_review_only_and_core_routes_can_be_stale():
    rows=[check('c'+str(i),window_start=(NOW-timedelta(days=10-i)).isoformat(),
        window_end=(NOW-timedelta(days=9-i)).isoformat()) for i in range(5)]
    p=plan();before=p.model_dump_json();report=source_value_report(p,rows+[rows[0]],as_of=NOW)
    cohort=report['cohorts'][0]
    assert cohort['checks']==5 and cohort['cadence_review_required'] and cohort['covered_zero_yield_streak']==5
    assert p.model_dump_json()==before and set(report['unchecked_routes'])=={'n8n','watch'}
    positive=check('positive',novel_action_ids=['action-1'],window_start=(NOW-timedelta(hours=2)).isoformat(),window_end=(NOW-timedelta(hours=1)).isoformat())
    assert not source_value_report(p,rows+[positive],as_of=NOW)['cohorts'][0]['cadence_review_required']

@pytest.mark.parametrize('change',[dict(omissions=['unsupported_section']),dict(fetched_pages=[]),
    dict(bounded_set_complete=False),dict(novelty_evaluated=False,novel_action_ids=None)])
def test_nominal_success_is_not_adequate_without_full_planned_set_and_novelty(change):
    row=check(**change);assert not row.adequate

def test_cohorts_do_not_mix_profiles_or_queries_and_union_is_not_finder_attribution():
    rows=[check('a',novel_action_ids=['shared'],review_minutes=3,external_cost=1,cost_currency='USD'),
          check('b','n8n',novel_action_ids=['shared'],review_minutes=None),
          check('c',profile_hash='b'*16,novel_action_ids=['other']),
          check('d',query_family='language',novel_action_ids=['shared'])]
    report=source_value_report(plan(),rows,as_of=NOW)
    assert len(report['cohorts'])==4 and report['union_known_action_ids']==['other','shared']
    measured=next(c for c in report['cohorts'] if c['source_id']=='make' and c['query_family']=='buyers' and c['profile_hash']=='a'*16)
    assert measured['measured_review_minutes']==3 and measured['measured_external_cost_by_currency']=={'USD':1}

@pytest.mark.parametrize('change',[dict(review_minutes=True),dict(review_minutes='0'),dict(review_minutes=float('nan')),
    dict(external_cost=1),dict(fetched_pages=['outside']),dict(allowed_pages=[]),dict(novelty_evaluated='true')])
def test_missing_false_or_malformed_measurements_are_not_coerced_to_zero(change):
    with pytest.raises(ValueError):check(**change)

def test_future_and_colliding_check_evidence_is_rejected():
    with pytest.raises(ValueError,match='future_source_check'):
        source_value_report(plan(),[check(observed_at=(NOW+timedelta(days=1)).isoformat())],as_of=NOW)
    with pytest.raises(ValueError,match='check_id_collision'):
        source_value_report(plan(),[check(),check(novel_action_ids=['changed'])],as_of=NOW)


def test_relabelled_or_overlapping_windows_do_not_multiply_zero_yield_evidence():
    rows=[check('c'+str(i)) for i in range(5)]
    cohort=source_value_report(plan(),rows,as_of=NOW)['cohorts'][0]
    assert cohort['adequately_covered_checks']==5
    assert cohort['covered_zero_yield_streak']==1 and not cohort['cadence_review_required']
    assert len(cohort['overlapping_zero_check_ids'])==4


def test_partial_check_with_known_action_resets_zero_streak_without_claiming_coverage():
    rows=[check('c'+str(i),window_start=(NOW-timedelta(days=10-i)).isoformat(),
        window_end=(NOW-timedelta(days=9-i)).isoformat()) for i in range(5)]
    rows.append(check('partial-positive',retrieval_state='partial',bounded_set_complete=False,
        allowed_pages=['page-1','page-2'],novel_action_ids=['known-action'],
        window_start=(NOW-timedelta(hours=2)).isoformat(),window_end=(NOW-timedelta(hours=1)).isoformat()))
    cohort=source_value_report(plan(),rows,as_of=NOW)['cohorts'][0]
    assert cohort['adequately_covered_checks']==5 and cohort['inadequate_check_ids']==['partial-positive']
    assert cohort['covered_zero_yield_streak']==0 and not cohort['cadence_review_required']
    assert cohort['known_unique_action_ids']==['known-action']


def test_capture_entrypoint_installs_overlay_without_claiming_legacy_source_attribution(tmp_path):
    from jobhound.config import CONFIG
    from jobhound.run_context import RunContext
    from jobhound.v41.review import capture_review
    from test_v6_isolation_limits import record
    cfg=CONFIG.model_copy(deep=True);cfg.v55.enabled=True;cfg.sources.ats.enabled=True
    cfg.sources.ats.ashby={'example':'Example','second':'Second'}
    ctx=RunContext.capture(config=cfg,as_of=NOW,workspace=tmp_path)
    p=plan(request_ceiling=1,routes=[route(requests=1)],
        categories=[dict(category='buyers',requests=1,inspections=12,searches=12)])
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200,json={'jobs':[{**record()['raw'],'isListed':True}]})
    snapshot,_=asyncio.run(capture_review(tmp_path,source='ashby',context=ctx,
        source_plan=p,transport=httpx.MockTransport(handler)))
    receipt=json.loads((tmp_path/'request_budget_receipt.json').read_text())
    assert snapshot.exists() and len(calls)==receipt['requests_reserved']==receipt['limit']==1
    assert receipt['source_budget']['actual']['unattributed_existing_requests']==1
    assert receipt['source_budget']['actual']['inspections']==0
    assert not (tmp_path/'.active-run').exists()
