"""Application UI in native feed HTML cannot become role evidence."""
from datetime import datetime, timezone

import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_raw
from jobhound.text import html_to_text

NOW=datetime(2026,10,7,9,tzinfo=timezone.utc)
BODY='''<h2>Work</h2><p>Review Bengali and English AI responses using supplied guidelines.
Compare response quality, follow the rubric and report unclear examples.</p>
<h2>Requirements</h2><p>Bengali and English fluency required. Remote applicants in India may apply.
No prior AI-training experience or university degree required.</p>
<p>This is a contractor role reviewing supplied examples and recording your reasons.
Compensation: USD 10 per working hour.</p>'''
FORM='''<form><h2>Application form</h2><label for="languages">Language choices</label>
<select id="languages"><option>Dutch fluency required.</option>
<option>Bengali</option><option>Japanese native speaker required.</option></select></form>'''


def record(provider,html):
    common=dict(title='Bengali AI Response Evaluator',_company='Synthetic Example')
    if provider=='ashby':
        raw=dict(common,id='synthetic-role',jobUrl='https://jobs.ashbyhq.com/example/synthetic-role',
                 descriptionHtml=html,location='India',isRemote=True,publishedAt=NOW.isoformat())
    elif provider=='lever':
        raw=dict(common,id='synthetic-role',text=common['title'],hostedUrl='https://jobs.lever.co/example/synthetic-role',
                 description=html,categories={'location':'India'},workplaceType='remote',createdAt=NOW.isoformat())
    else:
        raw=dict(common,id=123,absolute_url='https://boards.greenhouse.io/example/jobs/123',
                 content=html,location={'name':'Remote, India'},updated_at=NOW.isoformat())
    return dict(source=provider,raw=raw)


def engine(provider,html):
    result=evaluate_raw([record(provider,html)],as_of=NOW)
    assert result.accounting_ok and len(result.evaluated)==1
    return result,result.evaluated[0]


@pytest.fixture(autouse=True)
def release(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',True)
    monkeypatch.setattr(CONFIG.v55,'account_states',[])


@pytest.mark.parametrize('provider',['ashby','lever','greenhouse'])
def test_html_feed_application_options_cannot_add_role_language_blockers(provider):
    _,baseline=engine(provider,BODY)
    result,changed=engine(provider,BODY+FORM)
    assert not any('lang_mismatch:' in b for b in baseline.assessment.blockers)
    assert changed.assessment.blockers==baseline.assessment.blockers
    assert changed.decision.action_band==baseline.decision.action_band
    assert 'Dutch' not in changed.job.description
    # Normalized role text is separate from the captured original provider field.
    assert any(FORM in str(value) for value in result.observations[0].raw_payload.values())


@pytest.mark.parametrize('provider',['ashby','lever','greenhouse'])
def test_genuine_language_outside_form_still_blocks(provider):
    _,changed=engine(provider,BODY+'<p>Dutch fluency required.</p>')
    assert any(b.endswith('lang_mismatch:dutch') for b in changed.assessment.blockers)
    assert changed.decision.action_band.value=='reject'


@pytest.mark.parametrize('provider',['ashby','lever','greenhouse'])
@pytest.mark.parametrize('ui',[
    '<nav><p>Dutch fluency required.</p><p>Salary USD 900/hour.</p></nav>',
    '<div role="form"><h2>Requirements</h2><p>Dutch fluency required.</p></div>',
    '<div role="combobox">Dutch fluency required.</div>',
    '<textarea>Dutch fluency required.</textarea>',
    '<form><fieldset><legend>Required</legend><select><option>Dutch fluency required.</select></fieldset></form>',
])
def test_controls_cannot_supply_requirements_or_compensation(provider,ui):
    _,baseline=engine(provider,BODY)
    _,changed=engine(provider,BODY+ui)
    assert changed.job.description==baseline.job.description
    assert changed.assessment.blockers==baseline.assessment.blockers
    assert changed.assessment.selected_pay.model_dump(exclude={'observation_id'})==baseline.assessment.selected_pay.model_dump(exclude={'observation_id'})


@pytest.mark.parametrize('provider',['ashby','lever','greenhouse'])
@pytest.mark.parametrize('malformed',[
    '<form><p>Choose a language.',
    '<div><form><p>Choose a language.</div>',
])
def test_uncertain_control_boundary_cannot_claim_complete_role(provider,malformed):
    result=evaluate_raw([record(provider,BODY+malformed)],as_of=NOW)
    assert result.accounting_ok
    assert result.observations[0].normalization_error
    assert not result.evaluated


def test_escaped_form_example_in_prose_is_not_a_live_control():
    value='<p>Review &lt;form&gt; samples and explain their quality.</p>'
    assert html_to_text(value,role_only=True)==html_to_text(value)


@pytest.mark.parametrize('encoded',[False,True])
def test_whole_encoded_html_retains_role_and_excludes_controls(encoded):
    from html import escape
    value=BODY+FORM
    if encoded:value=escape(value)
    assert html_to_text(value,role_only=True)==html_to_text(BODY)


def test_default_converter_and_feature_off_preserve_legacy_controls(monkeypatch):
    from jobhound.normalize import normalize
    assert 'Dutch' in html_to_text(BODY+FORM)
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    row=record('ashby',BODY+FORM)
    assert 'Dutch' in normalize(row).description


@pytest.mark.parametrize('provider',['ashby','lever'])
def test_matching_plain_view_can_use_native_html_control_boundaries(provider):
    raw=record(provider,BODY+FORM)
    raw['raw']['descriptionPlain']=html_to_text(BODY+FORM)
    result=evaluate_raw([raw],as_of=NOW)
    item=result.evaluated[0]
    assert item.job.description==html_to_text(BODY)
    assert not any('lang_mismatch:' in b for b in item.assessment.blockers)
    assert 'Dutch' in result.observations[0].raw_payload['descriptionPlain']


@pytest.mark.parametrize('provider',['ashby','lever'])
def test_unmatched_html_cannot_erase_genuine_plain_requirement(provider):
    raw=record(provider,BODY+FORM)
    raw['raw']['descriptionPlain']=html_to_text(BODY)+'\nDutch fluency required.'
    result=evaluate_raw([raw],as_of=NOW)
    item=result.evaluated[0]
    assert any(b.endswith('lang_mismatch:dutch') for b in item.assessment.blockers)


def generic_hydration(description, *, extra='', fallback=False):
    import asyncio
    import json
    import httpx
    from jobhound.models import Job
    from jobhound.v41.hydration import hydrate_public_posting, RetrievalBudget, RetrievalPolicy
    original=Job(source='first_party',title='Bengali AI Response Evaluator',company='Synthetic Example',
                 url='https://example.org/jobs/bengali-review',description=html_to_text(BODY),location='India')
    payload={'@type':'JobPosting','title':original.title,'description':'' if fallback else description,
             'hiringOrganization':{'name':original.company},'jobLocation':{'address':{'addressCountry':'India'}}}
    body='<h1>'+original.title+'</h1><script type="application/ld+json">'+json.dumps(payload)+'</script><main>'+description+'</main>'+extra
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=body))) as client:
            return await hydrate_public_posting(original.url,original,client=client,budget=RetrievalBudget(RetrievalPolicy()))
    return asyncio.run(run())


@pytest.mark.parametrize('fallback',[False,True])
def test_generic_native_hydration_preserves_control_boundaries(fallback):
    baseline=generic_hydration(BODY,fallback=fallback)
    changed=generic_hydration(BODY+FORM,fallback=fallback)
    assert changed.state==baseline.state=='complete'
    assert changed.job.description==baseline.job.description
    if not fallback:assert FORM in changed.payload['description']


def test_generic_genuine_language_is_retained():
    outcome=generic_hydration(BODY+'<p>Dutch fluency required.</p>'+FORM)
    assert 'Dutch fluency required.' in outcome.job.description
    assert 'Japanese' not in outcome.job.description


@pytest.mark.parametrize('ui',[
    '<form><p>This project has been completed.</p></form>',
    '<div role="listbox"><p>This project has been completed.</p></div>',
])
def test_generic_application_ui_cannot_close_role(ui):
    changed=generic_hydration(BODY,extra=ui)
    assert changed.state=='complete'
    assert changed.vacancy_state!='explicitly_closed'
    assert '_closure_evidence' not in changed.payload


@pytest.mark.parametrize('fallback',[False,True])
def test_generic_uncertain_control_boundary_cannot_claim_complete(fallback):
    outcome=generic_hydration(BODY+'<form><p>Choose language.',fallback=fallback)
    assert outcome.state!='complete'


def test_generic_feature_off_preserves_legacy_description(monkeypatch):
    monkeypatch.setattr(CONFIG.v55,'enabled',False)
    outcome=generic_hydration(BODY+FORM)
    assert outcome.state=='complete' and 'Japanese' in outcome.job.description


def lever_hydration(content):
    import asyncio
    import httpx
    from jobhound.models import Job
    from jobhound.v41.hydration import hydrate_public_posting, RetrievalBudget, RetrievalPolicy
    raw=record('lever',BODY)['raw']
    raw['descriptionPlain']=html_to_text(BODY)
    raw['lists']=[{'text':'Requirements','content':content}]
    original=Job(source='lever',title=raw['text'],company='Example',url=raw['hostedUrl'],
                 description=html_to_text(BODY),location='India')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=raw))) as client:
            return await hydrate_public_posting(original.url,original,client=client,budget=RetrievalBudget(RetrievalPolicy()))
    return asyncio.run(run()),raw


def test_lever_native_list_controls_cannot_supply_role_evidence():
    changed,raw=lever_hydration(FORM)
    assert changed.state=='complete'
    assert 'Dutch' not in changed.job.description
    assert changed.payload['descriptionPlain']==raw['descriptionPlain']
    assert changed.payload['lists']==raw['lists']


def test_lever_native_list_genuine_requirements_are_retained():
    changed,_=lever_hydration('<p>Dutch fluency required.</p>'+FORM)
    assert changed.state=='complete'
    assert 'Dutch fluency required.' in changed.job.description
    assert 'Japanese' not in changed.job.description


def test_lever_uncertain_list_boundary_cannot_claim_complete():
    changed,_=lever_hydration('<form><p>Choose language.')
    assert changed.state!='complete'
