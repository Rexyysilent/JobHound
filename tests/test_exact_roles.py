"""Synthetic native structures paired with actual hydration/decision boundaries."""
import asyncio
from datetime import datetime,timezone
import json
import pytest
import httpx
from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.exact_roles import parse_native_role,role_route
from jobhound.v41.hydration import hydrate_public_posting,child_observation,RetrievalBudget,RetrievalPolicy
from jobhound.v41.engine import evaluate_observations
from test_v55_action_policy import observation,NOW

TURING='https://work.turing.com/r/synthetic01'
MICRO='https://jobs.micro1.ai/post/11111111-2222-3333-4444-555555555555'
TITLE='Bengali AI Evaluation Analyst'

def original(url,company):return Job(source='fixture',url=url,title=TITLE,company=company)

def turing_body(*,hidden=False,move=True,metadata=None,section='Key Qualifications',extra=''):
    content=f'''<div class="space-y-6"><div><h1>{TITLE}</h1><p>Remote</p></div>
    <div data-slot="card"><div data-slot="card-header">Overview</div><div data-slot="card-content">
    <p>About Turing:</p><p>General brand marketing and a referral reward of USD 500.</p>
    <p>Role Overview:</p><p>Review Bengali model answers and write English rationales.</p>
    <p>{section}:</p><p>Bengali proficiency and careful evaluation are required.</p>
    <p>Description:</p><p>Evaluate responses to personal-context prompts.</p>
    <p>Education &amp; Experience:</p><p>Degree or equivalent relevant experience.</p>
    <p>Offer Details:</p><p>Contract work with scheduled overlap hours.</p>
    <p>Evaluation Process:</p><p>Assessment follows a reviewed invitation.</p>{extra}</div></div></div>'''
    if hidden:
        content='<template id="P:2"></template><div hidden id="S:2">'+content+'</div>'
        if move:content+='<script>$RS("S:2","P:2")</script>'
    return f'<meta property="og:url" content="{metadata or TURING}">'+content

def description():return f'''<p>Role Title: {TITLE}</p><p>Location: Remote</p>
    <p>Scope of Work</p><ul><li>Review Bengali audio and explain judgments in English.</li></ul>
    <p>Preferred Qualifications</p><ul><li>Linguistics experience is preferred.</li></ul>'''

def micro_body(*,role_changes=None,component_changes=None,text=None,records_extra='',pay=None):
    text=description() if text is None else text
    data=dict(client_job_id=MICRO.rsplit('/',1)[-1],job_role_name=TITLE,job_status='closed',
        job_description='$16',required_skills=['Bengali proficiency','English communication'],
        ideal_hourly_rate={'min':30,'max':65} if pay is None else pay,
        client_details={'client_name':'micro1'},create_datetime='2026-10-01T00:00:00+00:00',
        ideal_monthly_salary_min=None,ideal_monthly_salary_max=None,referral_reward_amount=500)
    data.update(role_changes or {})
    component=dict(id=MICRO.rsplit('/',1)[-1],data=data,error=None,loading=False,
        job_qualifying_question_list=[{'question_text':'Unrelated application field: Japanese PhD required.'}])
    component.update(component_changes or {})
    stream='16:T'+format(len(text.encode()),'x')+','+text+'6:'+json.dumps(component)+'\n'+records_extra
    # Match the captured split/concatenation contract, including UTF-8 text length.
    parts=[stream[:7],stream[7:30],stream[30:]]
    return ''.join('<script>self.__next_f.push('+json.dumps([1,p])+')</script>' for p in parts)

def parse(body,url=MICRO):return parse_native_role(body,url,original(url,'micro1' if url==MICRO else 'Turing'))

def test_turing_native_role_card_excludes_company_referral_and_account_text():
    result=parse(turing_body()+ '<div>Create an account. A referral rate is USD 800/hour.</div>',TURING)
    assert result.state=='complete' and result.identity_state=='exact'
    assert result.job.location=='Remote' and result.vacancy_state=='unknown'
    assert 'USD 500' not in result.job.description and '800' not in result.job.description
    assert result.application_route_state=='account_unknown' and result.task_availability_state=='advertised_only'

def test_completed_turing_stream_fragment_is_distinct_from_unfinished_hidden_content():
    assert parse(turing_body(hidden=True),TURING).state=='complete'
    assert parse(turing_body(hidden=True,move=False),TURING).error=='native_role_heading_missing'

@pytest.mark.parametrize('change',[dict(metadata='https://work.turing.com/r/different01'),dict(section='Other Section'),
    dict(extra='<h1>Another role</h1>'),dict(extra='<p>Key Qualifications:</p><p>Unrelated duplicate text.</p>')])
def test_turing_missing_ambiguous_or_mismatched_role_fields_stay_partial(change):
    assert parse(turing_body(**change),TURING).state=='partial'

@pytest.mark.parametrize('url',['http://work.turing.com/r/synthetic01',TURING+'?token=synthetic',
    'https://work.turing.com/jobs','https://work.turing.com/r/synthetic01/extra',
    'https://jobs.micro1.ai/post/not-a-role','https://other.example.org/post/11111111-2222-3333-4444-555555555555'])
def test_route_contract_does_not_treat_company_root_or_other_identity_as_role(url):
    assert role_route(url) is None

def test_micro1_closed_native_status_dominates_recruiting_text_and_future_jsonld_date():
    body=micro_body()+'<script type="application/ld+json">'+json.dumps({'@type':'JobPosting','validThrough':'2099-01-01'})+'</script>'
    result=parse(body)
    assert result.state=='complete' and result.identity_state=='exact' and result.vacancy_state=='explicitly_closed'
    assert result.payload['advertised_pay']==dict(min='30',max='65',currency_symbol='$',unit='hour',basis='advertised_public_range')
    assert 'Japanese' not in result.job.description and '500' not in result.job.description
    assert result.application_route_state=='account_unknown' and result.task_availability_state=='advertised_only'
    assert result.job.posted_at==datetime(2026,10,1,tzinfo=timezone.utc)

def test_unknown_micro1_status_and_public_advertised_range_do_not_prove_open_allocation_or_account_route():
    result=parse(micro_body(role_changes={'job_status':'unreviewed_value'}))
    assert result.state=='complete' and result.vacancy_state=='unknown'
    assert result.task_availability_state=='advertised_only' and result.application_route_state=='account_unknown'

@pytest.mark.parametrize('change',[{'client_job_id':'different'}, {'job_description':'$ff'}, {'job_role_name':'Different role'},
    {'required_skills':None}, {'client_details':{'client_name':'Other company'}}, {'job_status':None}])
def test_micro1_identity_missing_referenced_text_or_required_fields_cannot_complete(change):
    assert parse(micro_body(role_changes=change)).state=='partial'

@pytest.mark.parametrize('change',[{'id':'different'},{'loading':True},{'loading':'false'},{'error':'fetch failed'},{'data':None}])
def test_micro1_component_identity_and_loaded_state_require_native_typed_values(change):
    assert parse(micro_body(component_changes=change)).state=='partial'

@pytest.mark.parametrize('pay',[{'min':True,'max':65},{'min':0,'max':65},{'min':80,'max':65},
    {'min':float('nan'),'max':65},{'min':'30','max':'65'},{'min':30,'max':65,'currency':'guessed'}])
def test_micro1_malformed_money_cannot_create_a_claim(pay):
    assert parse(micro_body(pay=pay)).error=='native_pay_malformed'

def test_micro1_shell_marketing_jsonld_and_script_text_are_not_complete_native_role():
    for body in ['<html><script>start()</script></html>', '<h1>'+TITLE+'</h1>',
        '<script type="application/ld+json">'+json.dumps({'@type':'JobPosting','title':TITLE})+'</script>']:
        assert parse(body).state=='partial' and parse(body).error=='native_js_shell'

def test_micro1_duplicate_record_truncated_utf8_or_description_sections_stay_partial():
    assert parse(micro_body(records_extra='16:T1,x')).error=='native_stream_duplicate_record'
    assert parse(micro_body(text='<p>Role Title: '+TITLE+'</p>')).error=='native_sections_incomplete'
    result=parse(micro_body(text=description()+'<p>Bengali বাংলা.</p>'))
    assert result.state=='complete' and 'বাংলা' in result.job.description
    assert parse('<script>self.__next_f.push([1,"16:Tffff,x"])</script>').error=='native_stream_truncated'

def test_native_hydration_actual_entrypoint_and_closed_decision_skip(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    parent=observation();parent.job.url=MICRO;parent.job.title=TITLE;parent.job.company='micro1'
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=micro_body()))) as client:
            return await hydrate_public_posting(MICRO,parent.job,client=client,budget=RetrievalBudget(RetrievalPolicy()))
    result=asyncio.run(run());child=child_observation(parent,result)
    assert result.source=='micro1_native' and child.vacancy_state=='explicitly_closed'
    evaluated=evaluate_observations([parent,child],as_of=datetime.now(timezone.utc))
    assert len(evaluated.evaluated)==1
    row=evaluated.evaluated[0]
    assert row.decision.next_action=='skip' and row.assessment.action_readiness=='closed'

def test_legacy_feature_off_retains_original_unsupported_hydration(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=micro_body()))) as client:
            return await hydrate_public_posting(MICRO,original(MICRO,'micro1'),client=client,budget=RetrievalBudget(RetrievalPolicy()))
    result=asyncio.run(run());assert result.state=='partial' and result.error=='jobposting_not_found'

def test_different_original_url_and_identity_conflict_never_attach_role():
    result=parse_native_role(micro_body(),MICRO,original(MICRO.replace('11111111','99999999'),'micro1'))
    assert result.state=='unavailable' and result.error=='native_original_role_mismatch'
    result=parse_native_role(micro_body(),MICRO,Job(source='fixture',url=MICRO,title='German Robotics Engineer',company='Other company'))
    assert result.identity_state=='conflict'

def test_inner_turing_stream_move_cannot_complete_unfinished_outer_boundary():
    body=turing_body()
    outer='<template id="B:0"></template><div hidden id="S:0"><template id="P:2"></template></div>'
    nested=outer+'<div hidden id="S:2">'+body+'</div><script>$RS("S:2","P:2")</script>'
    assert parse(nested,TURING).state=='partial'
    assert parse(nested+'<script>$RC("B:0","S:0")</script>',TURING).state=='complete'

def test_native_cached_hydration_charges_one_scoped_inspection_and_zero_http(tmp_path,monkeypatch):
    from jobhound.bounded_transport import BoundedTransport,RunBudget,RequestLimits
    from jobhound.run_context import RunContext,run_scope
    from test_source_policy import plan,route
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    p=plan(route('micro1',hosts=['jobs.micro1.ai']))
    ctx=RunContext.capture(config=CONFIG.model_copy(deep=True),workspace=tmp_path)
    owner=RunBudget(ctx,RequestLimits(),source_plan=p);retrieval=RetrievalBudget(RetrievalPolicy())
    retrieval.cache[MICRO]=dict(status=200,body=micro_body(),final_url=MICRO,trail=[MICRO],captured_at=datetime.now(timezone.utc).isoformat())
    calls=[]
    async def run():
        async with httpx.AsyncClient(transport=BoundedTransport(httpx.MockTransport(lambda r:calls.append(r)),owner)) as client:
            with owner.sources.inspection('micro1','cached-role'):
                return await hydrate_public_posting(MICRO,original(MICRO,'micro1'),client=client,budget=retrieval)
    with run_scope(ctx):result=asyncio.run(run())
    assert result.state=='complete' and result.from_cache and not calls
    receipt=owner.receipt();assert receipt['requests_reserved']==0
    assert receipt['source_budget']['actual']['inspections']==1

def test_malformed_native_port_is_rejected_without_provider_wide_failure():
    assert role_route('https://work.turing.com:wrong/r/synthetic01') is None

def test_native_turing_sections_reach_requirements_and_duties_without_changing_raw_prose():
    from jobhound.v41.extract import extract_sections
    result=parse(turing_body(),TURING)
    sections=extract_sections(result.job.description,native_source=result.job.source)
    assert 'Bengali proficiency' in sections.requirements
    assert 'Degree or equivalent' in sections.requirements
    assert 'Evaluate responses' in sections.responsibilities
    assert 'Assessment follows' not in sections.requirements
    assert 'Key Qualifications:' in result.job.description

def test_native_micro1_preferred_background_is_not_mandatory_qualification():
    from jobhound.v41.extract import extract_sections
    result=parse(micro_body())
    sections=extract_sections(result.job.description,native_source=result.job.source)
    assert 'Bengali proficiency' in sections.requirements and 'Linguistics' not in sections.requirements
    assert 'Linguistics' in sections.other and 'Review Bengali audio' in sections.responsibilities

def test_one_native_contract_failure_does_not_abort_independent_role_hydration(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,
            text='<html><script>start()</script></html>' if r.url.host=='jobs.micro1.ai' else turing_body()))) as client:
            return await asyncio.gather(*(hydrate_public_posting(url,original(url,company),client=client,
                budget=RetrievalBudget(RetrievalPolicy())) for url,company in [(MICRO,'micro1'),(TURING,'Turing')]))
    micro,turing=asyncio.run(run())
    assert micro.state=='partial' and turing.state=='complete'
