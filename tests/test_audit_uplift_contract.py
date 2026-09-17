"""Public synthetic contracts using the real normalization and adapter stack."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from dataclasses import asdict
import math
import pytest
import httpx
from types import SimpleNamespace
from jobhound.v41 import compensation, documents
from jobhound.sources import base, ats_greenhouse, ats_lever, ats_ashby

patched=SimpleNamespace(compensation=compensation, documents=documents, base=base,
    sources={'greenhouse':ats_greenhouse.GreenhouseSource,'lever':ats_lever.LeverSource,'ashby':ats_ashby.AshbySource})

@pytest.fixture(autouse=True)
def isolated_source_config(monkeypatch):
    # Inject only adapter configuration, not algorithms or the text normalizer.
    cfg=SimpleNamespace(sources=SimpleNamespace(ats=SimpleNamespace(enabled=True,greenhouse={},lever={},ashby={})))
    patched.config=cfg
    for module in (ats_greenhouse,ats_lever,ats_ashby):
        monkeypatch.setattr(module,'CONFIG',cfg)


parse = patched.compensation.parse_compensation
classify = patched.documents.classify_document

@pytest.mark.parametrize('raw,expected', [
    ('INR 50,000 monthly salary', {'basis':'month','amount_low':50000}),
    ('$5 per image. Other assignments pay $60 per hour.', {'basis':'unknown','amount_low':None}),
    ('Minimum 2 years of experience. Pay $20/hr.', {'guaranteed_minimum':None,'basis':'labor_hour'}),
    ('$80k-$100k per year', {'amount_low':80000,'amount_high':100000,'basis':'year'}),
    ('$10-EUR 20/hr', {'basis':'unknown','amount_low':None}),
    ('USD 30 per paid task-hour', {'basis':'task_hour','labor_hour_equivalent':None}),
    ('USD 60 per recorded audio hour', {'basis':'output_audio_hour','labor_hour_equivalent':None}),
    ('Up to USD 50 per hour equivalent', {'basis':'labor_hour_equivalent','qualifier':'up_to_equivalent'}),
    ('INR 6-8 lakh per year', {'amount_low':600000,'amount_high':800000,'basis':'year'}),
    ('INR 6 LPA', {'amount_low':600000,'basis':'year'}),
    ('INR 1,00,000 per month', {'amount_low':100000,'basis':'month'}),
    ('EUR 12,50 per hour', {'basis':'unknown','amount_low':None}),
    ('EUR 300 per week', {'basis':'week','currency':'EUR'}),
    ('GBP 150 per day', {'basis':'day'}),
    ('USD 60 salary', {'basis':'unknown'}),
    ('Guaranteed minimum rate: USD 20/hr', {'guaranteed_minimum':20}),
    ('No guaranteed rate: USD 20/hr', {'guaranteed_minimum':None}),
    ('Minimum 2 years experience; USD 20 per hour', {'guaranteed_minimum':None}),
    ('Up to USD 40 per hour', {'guaranteed_minimum':None,'qualifier':'up_to'}),
    ('USD 4 per audio minute', {'basis':'output_audio_minute'}),
    ('USD 5 per accepted image', {'basis':'output_item'}),
    ('USD 300 fixed project budget', {'basis':'fixed_project','labor_hour_equivalent':None}),
    ('USD 20-10 per hour', {'basis':'unknown'}),
    ('-USD 20/hr', {'basis':'unknown'}),
    ('USD 20 hourly or monthly', {'basis':'unknown'}),
    ('₹500 per hour', {'currency':'INR','basis':'labor_hour'}),
    ('€20 per hour', {'currency':'EUR','basis':'labor_hour'}),
    ('CAD 20 per hour', {'currency':'CAD','basis':'labor_hour'}),
    ('AUD 25 per hour', {'currency':'AUD','basis':'labor_hour'}),
])
def test_pay(raw,expected):
    observed=asdict(parse(raw))
    assert all(observed[key]==value for key,value in expected.items()),observed

@pytest.mark.parametrize('bad',[0,-1,float('inf'),float('nan'),True,'2'])
def test_invalid_labor_assumptions(bad):
    with pytest.raises(ValueError): parse('USD 20 per image',labor_hours_per_unit=bad)

def test_explicit_labor_estimate_is_not_payment_guarantee():
    claim=parse('USD 60 per recorded audio hour',labor_hours_per_unit=4)
    assert claim.labor_hour_equivalent==15 and claim.labor_equivalent_is_estimate
    assert claim.basis=='output_audio_hour' and claim.guaranteed_minimum is None

def test_bare_dollar_assumption_disclosed():
    assert 'bare_dollar_assumed_usd_legacy_contract' in parse('$20/hr').warnings

def test_type_and_size_bound():
    with pytest.raises(TypeError): parse(None)
    assert parse('USD 10/hr '+('x'*32768)).basis=='unknown'

@pytest.mark.parametrize('title,text,url,expected',[
    ('Remote AI Jobs: Training & Data Labeling - OpenTrain AI','Remote AI training opportunities.','https://www.opentrain.ai/jobs/', 'job_index'),
    ('Browse jobs','Browse our open jobs.','https://example.org/jobs/?q=remote','job_index'),
    ('','','https://example.org/mystery','unknown'),
    ('Bengali AI Evaluator','Requirements: Bengali fluency. Responsibilities: moderate discussion of model outputs. Apply for job 123.','https://example.org/jobs/123','individual_job'),
    ('Bengali AI Evaluator','Requirements: Bengali fluency. Apply for job 123. Footer: Join our talent network for future opportunities.','https://example.org/jobs/123','individual_job'),
    ('Bengali AI Evaluator','Requirements: Bengali fluency. Apply for job 123. Not right for you? Join our talent network for future opportunities.','https://example.org/jobs/123','individual_job'),
    ('Build an automation','Looking for a developer for this project.','https://www.upwork.com/freelance-jobs/apply/example_~123/','buyer_request'),
    ('Bengali Language Expert','Requirements: Bengali fluency. Apply for this role.','https://www.opentrain.ai/jobs/bengali-language-expert--example/','individual_job'),
    ('Bengali AI Evaluator','Requirements: Bengali fluency. Browse jobs after applying.','https://example.org/careers?gh_jid=123','individual_job'),
    ('Join our talent pool','Register for future opportunities.','https://example.org/talent','talent_pool'),
    ('Outlier AI Jobs video','Bengali AI training discussed.','https://www.youtube.com/watch?v=example','article'),
    ('Looking for AI trainer job in India','I am looking for work.','https://www.reddit.com/r/jobs/comments/example/test/','discussion'),
    ('Hiring Bengali annotators','We are hiring Bengali annotators for a project.','https://www.reddit.com/r/jobs/comments/example/test/','individual_job'),
    ('Buy automation service','I will build your chatbot.','https://www.upwork.com/services/product/example','seller_service'),
    ('Automation specialists','Hire freelancers.','https://www.upwork.com/hire/automation/','talent_directory'),
])
def test_document_types(title,text,url,expected):
    assert classify(title,text,url).document_type==expected


def payload(provider, identifier='good'):
    row={'id':identifier,'title':'Bengali evaluator','isListed':True}
    return [row] if provider=='lever' else {'jobs':[row]}

def exercise(provider, status=404, shape=None, limit=None):
    setattr(patched.config.sources.ats,provider,{'good':'Good company','bad':'Bad board','after':'After company'})
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        if '/bad' in request.url.path:
            if status=='timeout': raise httpx.ReadTimeout('synthetic timeout',request=request)
            if shape is not None: return httpx.Response(200,json=shape)
            return httpx.Response(status,headers={'Retry-After':'120'})
        return httpx.Response(200,json=payload(provider,request.url.path))
    source=patched.sources[provider](limit=limit)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await source.fetch(client)
    rows=asyncio.run(run())
    return source,rows,calls

@pytest.mark.parametrize('provider',['greenhouse','lever','ashby'])
@pytest.mark.parametrize('status',[404,500,'timeout'])
def test_board_failure_retains_both_healthy_boards(provider,status):
    source,rows,calls=exercise(provider,status)
    assert len(rows)==2 and len(calls)==3
    assert {row['raw']['_slug'] for row in rows}=={'good','after'}
    assert source.health.status=='partial' and source.health.failed_requests==1
    assert source.health.successful_requests==2
    assert source.health.stages['discovery']['status']=='degraded'

@pytest.mark.parametrize('provider',['greenhouse','lever','ashby'])
@pytest.mark.parametrize('status',[403,429])
def test_access_or_rate_limit_stops_batch_without_losing_prior_rows(provider,status):
    source,rows,calls=exercise(provider,status)
    assert len(rows)==1 and len(calls)==2 and source.health.retries==0
    assert source.health.board_results[-1]['status']=='deferred'
    assert source.health.cooldown_until

@pytest.mark.parametrize('provider',['greenhouse','lever','ashby'])
def test_schema_failure_is_not_empty_or_remote_http_failure(provider):
    source,rows,calls=exercise(provider,shape={'wrong':'schema'})
    assert len(rows)==2 and source.health.status=='partial'
    assert source.health.failed_requests==0
    assert source.health.stages['parsing']['failed']==1
    assert patched.base.summarize_stage_health(source.health)['remote_http_errors']==0

@pytest.mark.parametrize('provider',['greenhouse','lever','ashby'])
def test_budget_avoids_more_board_requests(provider):
    source,rows,calls=exercise(provider,limit=1)
    assert len(rows)==1 and len(calls)==1 and source.health.status=='partial'
    assert source.health.stages['discovery']['deferred']==2


def test_ashby_does_not_publish_unlisted_or_unknown_visibility():
    patched.config.sources.ats.ashby={'one':'One'}
    rows=[{'id':'yes','isListed':True},{'id':'no','isListed':False},{'id':'unknown'},{'id':'string','isListed':'true'}]
    source=patched.sources['ashby']()
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'jobs':rows}))) as client:
            return await source.fetch(client)
    out=asyncio.run(run())
    assert [row['raw']['id'] for row in out]==['yes']
    assert '_slug' not in rows[0]
    assert source.health.board_results[0]['unknown_listing']==2

@pytest.mark.parametrize('provider',['greenhouse','lever','ashby'])
def test_empty_board_is_healthy_empty(provider):
    setattr(patched.config.sources.ats,provider,{'one':'One'})
    source=patched.sources[provider]()
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=[] if provider=='lever' else {'jobs':[]}))) as client:
            return await source.fetch(client)
    assert asyncio.run(run())==[] and source.health.status=='empty'


def test_mixed_resolution_success_does_not_claim_recovery():
    health=patched.base.SourceHealth(source='example',status='partial')
    health.note_stage('resolution','failed'); health.note_stage('resolution','succeeded')
    assert not patched.base.summarize_stage_health(health,previous={'resolution':'degraded'})['resolution_recovered']

@pytest.mark.parametrize('provider',['greenhouse','lever','ashby'])
def test_zero_budget_is_deferred_not_a_remote_failure(provider):
    source,rows,calls=exercise(provider,limit=0)
    assert rows==[] and calls==[] and source.health.failed_requests==0
    assert source.health.status=='partial' and source.health.stages['discovery']['deferred']==3
