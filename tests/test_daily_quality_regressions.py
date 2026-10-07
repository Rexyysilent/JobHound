"""October 6 digest: hosted copies, editorial pages, historical demand and fit labels."""
from datetime import datetime, timedelta, timezone
import json
import re

import pytest

from jobhound.config import CONFIG
from jobhound.filters.listing_quality import junk_check
from jobhound.models import Job
from jobhound.v41.documents import classify_document
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.models import ListingObservation, SourceKind
from jobhound.v41.digest import build_digest, _release_entry
from test_v6_delivery_recovery import release, store, EMAIL, stage


NOW = datetime(2026, 10, 6, 12, 30, tzinfo=timezone.utc)
DESCRIPTION = ('Review AI outputs and label responses. Requirements: Fluent English. '
               'Remote applicants in India may apply. No university degree or prior experience required.')


def observation(title, url, *, key='role', kind=SourceKind.AGGREGATOR_UNRESOLVED):
    job = Job(source='serp', title=title, url=url, company='Acme', description=DESCRIPTION,
              is_remote=True, location='India', posted_at=NOW-timedelta(days=1), pay_raw='USD 20 per hour')
    return ListingObservation(observation_id=key, source='serp', job=job,
        original_url=url, normalized_url=url, source_kind=kind, captured_at=NOW,
        identity_state='exact', content_state='complete', vacancy_state='verified_open',
        verified_open_at=NOW, application_route_state='observed_public_route')


ARTICLES = [
    ('Get Paid to Train AI — RLHF Jobs & Platforms', 'https://remotestack.in/ai-training-jobs'),
    ('Companies Offering Remote Evaluator Jobs in 2026: Current ...', 'https://remoteonlineevaluator.com/remote-evaluator-jobs/'),
    ('Companies Offering Remote Annotation Roles', 'https://publisher.example/annotation-roles'),
    ('Get Paid to Train AI - RLHF Jobs and Platforms | Resource Site', 'https://publisher.example/ai-training-jobs'),
]


@pytest.mark.parametrize('title,url', ARTICLES)
def test_articles_fail_specific_vacancy_gate_even_with_job_boilerplate(release, title, url):
    document = classify_document(title, DESCRIPTION+' Apply now. We are hiring.', url)
    assert document.document_type == 'article' and not document.actionable
    result = evaluate_observations([observation(title, url)], as_of=NOW)
    row = result.evaluated[0]
    assert row.decision.action_band.value == 'reject'
    assert row.decision.terminal_reason == 'reject_source_quality'
    assert build_digest(result, include_all=True).displayed == []


@pytest.mark.parametrize('host', ['hirevault.k8p.is-great.org', 'hiretoday.3z.my-board.org',
                                 'another.is-great.org', 'HIRETODAY.3Z.MY-BOARD.ORG.'])
def test_hosted_copies_cannot_promote_themselves_with_jobposting_claims(release, host):
    row = observation('AI Session Reviewer - English Expert - AI Trainer', 'https://'+host+'/jobs/123')
    row.source_kind = SourceKind.ORIGINAL_EMPLOYER  # self-declared identity is insufficient
    result = evaluate_observations([row], as_of=NOW)
    item = result.evaluated[0]
    assert item.decision.action_band.value == 'reject'
    assert any('unresolved_hosted_job_board' in r for r in item.assessment.blockers)


@pytest.mark.parametrize('url', ['https://jobs.ashbyhq.com/acme/123',
    'https://boards.greenhouse.io/acme/jobs/123', 'https://in.linkedin.com/jobs/view/123',
    'https://www.upwork.com/freelance-jobs/apply/evaluator_~0123/',
    'https://careers.employer.example/jobs/123'])
def test_specific_legitimate_roles_remain_usable(release, url):
    title = 'English AI Response Evaluator'
    assert not junk_check(title, url, 'Acme', True)
    assert classify_document(title, DESCRIPTION, url).actionable
    result = evaluate_observations([observation(title, url)], as_of=NOW)
    assert result.evaluated[0].decision.action_band.value != 'reject'


def test_resolved_original_is_not_rejected_because_copy_was_seen(release):
    obs = observation('English AI Response Evaluator', 'https://jobs.ashbyhq.com/acme/123', kind=SourceKind.ORIGINAL_ATS)
    obs.redirect_trail = ['https://hirevault.k8p.is-great.org/jobs/123', obs.job.url]
    obs.job.seen_on = ['jsearch', 'first_party']
    result = evaluate_observations([obs], as_of=NOW)
    assert result.evaluated[0].decision.action_band.value != 'reject'


def test_quality_floor_applies_to_headlines_and_overflow(store):
    records = [observation(title,url,key='article'+str(i)) for i,(title,url) in enumerate(ARTICLES)]
    records += [observation('English AI Response Evaluator', 'https://hiretoday.3z.my-board.org/jobs/1', key='copy')]
    records += [observation('English AI Response Evaluator '+str(i),
        'https://jobs.ashbyhq.com/acme/'+str(i),key='real'+str(i),kind=SourceKind.ORIGINAL_ATS) for i in range(4)]
    result = evaluate_observations(records, as_of=NOW)
    stage(store,result,card_cap=1,status_cap=5)
    report=json.loads(store.conn.execute('SELECT report FROM delivery_runs').fetchone()[0])
    ledger=report['destinations'][0]
    assert ledger['cards']['eligible'] == 4
    assert ledger['counts']['selected_cards'] == 1 and ledger['counts']['overflow_cards'] == 3
    assert len(ledger['overflow']) == 3
    assert all(entry['host']=='jobs.ashbyhq.com' for entry in ledger['overflow'])
    assert result.accounting_ok


def thread_observations(*, buyer_update=None, seller_update=None, closed=False, buyer_age=128, buyer_bump=False):
    from jobhound.v41.community import project_thread, _thread_payload
    title='WhatsApp API + AI Automation Expert'
    url='https://community.make.com/t/900001'
    original=NOW-timedelta(days=buyer_age)
    def post(key, actor, text, when):
        return dict(id=key,post_number=key,user_id=actor,topic_id=900001,post_type=1,
                    cooked='<p>'+text+'</p>',created_at=when.isoformat(),updated_at=when.isoformat(),hidden=False,wiki=False)
    text='I need a developer to build a WhatsApp API automation. Requirements: remote applicants in India may apply. No degree or prior experience required. Budget: USD 250 fixed.'
    posts=[post(1,10,text,original)]
    posts[0]['updated_at']=NOW.isoformat()  # a cosmetic edit cannot renew demand
    if seller_update: posts.append(post(2,20,'I can help. Hire me for USD 500.',seller_update))
    if buyer_update: posts.append(post(3,10,'Still looking for a developer for this paid WhatsApp API workflow.',buyer_update))
    if buyer_bump: posts.append(post(3,10,'Thanks for your replies.',NOW-timedelta(hours=1)))
    if closed: posts.append(post(4,10,'This project is closed. Found a developer.',NOW-timedelta(days=1)))
    pages=[dict(id=900001,title=title,archetype='regular',category_id=74,closed=False,
                post_stream={'stream':[p['id'] for p in posts],'posts':posts})]
    thread=project_thread(pages,url,NOW)
    job=Job(source='community_thread',title=thread.title,url=thread.url,description=thread.description,
            company='Community requester 10',location='India',is_remote=True,posted_at=thread.original_at)
    return [ListingObservation(observation_id='thread',source='community_thread',job=job,
        original_url=thread.url,normalized_url=thread.url,source_kind=SourceKind.ORIGINAL_EMPLOYER,
        captured_at=NOW,content_state='complete',identity_state='exact',vacancy_state=thread.vacancy_state,
        raw_payload={'community_pages':pages,'community_thread':_thread_payload(thread)})]


@pytest.mark.parametrize('seller_update', [None, NOW-timedelta(hours=1)])
def test_old_buyer_request_is_historical_despite_fresh_capture_or_seller_activity(release, seller_update):
    result=evaluate_observations(thread_observations(seller_update=seller_update),as_of=NOW)
    row=result.evaluated[0]
    assert row.decision.action_band.value!='reject'
    assert row.decision.lifecycle=='watch' and row.assessment.lifecycle_reason=='historical_buyer_request'
    assert row.decision.next_action=='await_change'
    assert _release_entry(row).startswith('• HISTORICAL LEAD')
    digest=build_digest(result,include_all=True)
    assert not digest.displayed and digest.display_counts['suppressed_watch']==1
    assert digest.accounting_ok


def test_substantive_same_buyer_update_restores_daily_eligibility_and_freshness(release):
    result=evaluate_observations(thread_observations(buyer_update=NOW-timedelta(days=1)),as_of=NOW)
    row=result.evaluated[0]
    assert row.decision.lifecycle=='active' and row.decision.action_band.value!='reject'
    assert row.decision.priority_key.freshness>90
    assert row.job.posted_at==NOW-timedelta(days=128)  # preserve provenance
    assert build_digest(result,include_all=True).displayed


def test_non_substantive_buyer_reply_does_not_refresh_demand(release):
    row=evaluate_observations(thread_observations(buyer_bump=True),as_of=NOW).evaluated[0]
    assert row.assessment.lifecycle_reason=='historical_buyer_request'


@pytest.mark.parametrize('days,historical', [(59,False),(60,False),(61,True)])
def test_buyer_activity_window_has_an_explicit_boundary(release,days,historical):
    row=evaluate_observations(thread_observations(buyer_age=days),as_of=NOW).evaluated[0]
    assert (row.assessment.lifecycle_reason=='historical_buyer_request') is historical


def test_closed_buyer_request_is_not_reopened_as_historical(release):
    row=evaluate_observations(thread_observations(closed=True),as_of=NOW).evaluated[0]
    assert row.decision.action_band.value=='reject' and row.decision.lifecycle=='closed'


def test_history_never_leaks_into_compact_daily_pool(store):
    records=thread_observations()+[observation('English AI Response Evaluator',
        'https://jobs.ashbyhq.com/acme/123',key='real',kind=SourceKind.ORIGINAL_ATS)]
    result=evaluate_observations(records,as_of=NOW)
    stage(store,result,card_cap=0,status_cap=0)
    ledger=json.loads(store.conn.execute('SELECT report FROM delivery_runs').fetchone()[0])['destinations'][0]
    assert ledger['cards']['eligible']==1 and ledger['counts']['overflow_cards']==1
    assert ledger['overflow'][0]['host']=='jobs.ashbyhq.com'


def test_task_fit_is_not_rendered_as_a_fraction(release):
    row=evaluate_observations([observation('Quality Assurance Rater - Hindi (IN)',
        'https://in.linkedin.com/jobs/view/telus-123',kind=SourceKind.REPUTABLE_BOARD)],as_of=NOW).evaluated[0]
    text=_release_entry(row)
    assert 'task fit '+row.assessment.match_strength.value in text
    assert 'role priority '+str(row.decision.priority_key.role_priority) in text
    assert not re.search(r'task fit \d+/\d+',text)
