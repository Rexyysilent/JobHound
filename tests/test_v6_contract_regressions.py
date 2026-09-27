"""v5.5 acceptance contracts that regressed with the v6 integration.

DOC-03: v6 made only the title able to mark a page as a discussion (so a real
role with discussion-moderation duties stays a job), with narrow patterns, so
an advice thread became an individual vacancy. The first fix over-corrected
in both directions (code review 2026-09-27): casual pay words rescued advice
threads, and broad title/body patterns dropped real roles.

OPS-01: a rate-limited request must be reported as a remote HTTP error, not
disappear. Production hydration filed the 429 as a deferral with no error;
identity mismatches on a successful response are not HTTP errors.
"""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.sources.base import SourceHealth, summarize_stage_health
from jobhound.v41.documents import classify_document
from jobhound.v41.hydration import RetrievalPolicy

URL = 'https://example.invalid/roles/bengali-evaluator-01'


@pytest.mark.parametrize('title,text', [
    ('How do people find AI annotation jobs?', 'Discussion and advice; no buyer request.'),  # DOC-03
    ('How do you get AI training jobs?', 'Share what worked for you.'),
    ('Where can I find remote annotation work?', 'Any pointers appreciated.'),
    ('Is DataAnnotation legit?', 'Has anyone been paid?'),
    ('AI annotation work', 'General discussion thread for tips and experiences.'),
    # Casual pay/requirement words are not role structure (review finding 1).
    ('How do I get a job in AI annotation?', 'Heard the pay is $20 per hour, what are the requirements?'),
    ('Is DataAnnotation legit?', 'Their salary claims seem high'),
    ('Where can I find remote annotation work?', 'What compensation should I expect?'),
    # Normalized text and plurals (findings 7, 8).
    ('AI annotation work', 'discus­sion thread about getting started'),
    ('AI annotation work', 'Weekly discussion threads for tips'),
])
def test_advice_and_discussion_pages_are_not_vacancies(title, text):
    result = classify_document(title, text, URL)
    assert (result.document_type, result.actionable) == ('discussion', False)


@pytest.mark.parametrize('title,text', [
    # v6's reason for the title-only rule: discussion duties inside a real role.
    ('Bengali AI Evaluator', 'Requirements: Bengali fluency. Responsibilities: moderate discussion of model outputs. Apply for job 123.'),
    ('Community Discussion Moderator (Hindi)', 'Responsibilities: moderate the discussion forum. Pay: USD 12 per hour. Apply here.'),
    ('Are you fluent in Bengali? Join our AI evaluation project', 'Requirements: Bengali fluency. Compensation: USD 10 per hour. Apply now.'),
    # Ordinary postings that use "discussion" and non-standard headings
    # (paraphrased from real ATS/job-board descriptions, 2026-09-27 replay).
    ('Machine Learning Fellow', "What you'll bring: ML research experience. Fellows also gain access to newsletters, discussion channels and virtual sessions."),
    ('Working Student Quantitative Development', 'Your profile: you enjoy building software and love a good discussion on technical design. You know Java well.'),
    ('Senior Product Manager', 'Our process: interview with the CPO, then a product case and discussion with key product and engineering partners.'),
    # Review findings 2, 5, 6.
    ('Discussion Moderator', 'Pay: $15. Moderate our community forum nightly.'),
    ('Discussion Moderator', 'Moderate our community forum nightly.'),
    ('Tips for Success Coach', 'Coach students on study habits. Remote.'),
    ('How do we make AI safe? Research Engineer', 'We build alignment tools. You will work on evals.'),
    ('Paid user interview', 'Share your experiences using our app in a 60-minute paid call. $100 gift card.'),
])
def test_real_roles_mentioning_discussion_or_questions_stay_jobs(title, text):
    result = classify_document(title, text, URL)
    assert (result.document_type, result.actionable) == ('individual_job', True)


def _hydrate(handler):
    from jobhound.v41.engine import evaluate_raw
    from jobhound.v41.resolve import hydrate_result
    raw = [{'source': 'adzuna', 'raw': {'title': 'Bengali AI Evaluator',
            'company': {'display_name': 'Acme'}, 'redirect_url': 'https://www.adzuna.in/details/1',
            'description': 'Bengali AI review', 'location': {'display_name': 'Remote'}}}]
    old = CONFIG.v55.enabled
    CONFIG.v55.enabled = True
    try:
        result = evaluate_raw(raw, as_of=datetime(2026, 9, 8, tzinfo=timezone.utc))
        report = asyncio.run(hydrate_result(
            result, policy=RetrievalPolicy(request_budget=4, max_retries=0),
            transport=httpx.MockTransport(handler)))
    finally:
        CONFIG.v55.enabled = old
    return result, report


def test_production_hydration_reports_a_rate_limit_as_a_remote_http_error():  # OPS-01
    from jobhound.v41.digest import _source_health_alerts
    result, report = _hydrate(lambda request: httpx.Response(
        429, headers={'Retry-After': '120'}, request=request))
    row = report['adapter_health'][0]
    assert row['deferred'] == 1 and row['remote_http_errors'] == 1
    assert row['error_codes'] == {'http_429': 1}
    assert report['stage_health']['remote_http_errors'] == 1
    alerts = '\n'.join(_source_health_alerts(result))
    assert '1 remote HTTP error' in alerts and 'http_429' in alerts


def test_identity_mismatch_on_a_successful_response_is_not_an_http_error():
    ats = 'https://boards.greenhouse.io/acme/jobs/12345'

    def handler(request):
        if request.url.host == 'www.adzuna.in':
            return httpx.Response(200, text=f'<a href="{ats}">original role</a>')
        return httpx.Response(200, json={'id': 99999, 'title': 'Senior Tax Accountant',
            '_company': 'Other Co', 'absolute_url': 'https://boards.greenhouse.io/other/jobs/99999',
            'content': 'Unrelated role.', 'location': {'name': 'Berlin'}})

    _result, report = _hydrate(handler)
    assert report['stage_health']['failed'] >= 1
    assert report['stage_health']['remote_http_errors'] == 0


def test_discovery_failure_is_counted_once_and_parse_failures_not_at_all():
    health = SourceHealth(source='jsearch', status='ok')
    health.note_stage('discovery', 'failed')
    health.failed_requests += 1          # adapters record both, as production does
    health.note_stage('parsing', 'failed', count=3)
    assert summarize_stage_health(health)['remote_http_errors'] == 1
