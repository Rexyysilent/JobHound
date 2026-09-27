"""v5.5 acceptance contracts that regressed with the v6 integration.

DOC-03: v6 made only the title able to mark a page as a discussion (so a real
role with discussion-moderation duties stays a job), but the title patterns
were narrow and body signals were ignored entirely, so an advice thread
titled "How do people find AI annotation jobs?" became an individual vacancy.

OPS-01: v6 counted remote HTTP errors from discovery request counters only;
a rate-limited resolution request (recorded as a stage event) vanished.
"""
import pytest

from jobhound.sources.base import SourceHealth, summarize_stage_health
from jobhound.v41.documents import classify_document

URL = 'https://example.invalid/roles/bengali-evaluator-01'


@pytest.mark.parametrize('title,text', [
    ('How do people find AI annotation jobs?', 'Discussion and advice; no buyer request.'),  # DOC-03
    ('How do you get AI training jobs?', 'Share what worked for you.'),
    ('Where can I find remote annotation work?', 'Any pointers appreciated.'),
    ('Is DataAnnotation legit?', 'Has anyone been paid?'),
    ('AI annotation work', 'General discussion thread for tips and experiences.'),
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
])
def test_real_roles_mentioning_discussion_or_questions_stay_jobs(title, text):
    result = classify_document(title, text, URL)
    assert (result.document_type, result.actionable) == ('individual_job', True)


def test_rate_limited_resolution_counts_as_one_remote_http_error():  # OPS-01
    health = SourceHealth(source='adzuna', status='ok')
    health.note_stage('discovery', 'succeeded')
    health.note_stage('resolution', 'failed')
    health.note_stage('resolution', 'skipped', count=26)
    summary = summarize_stage_health(health)
    assert summary['overall'] == 'degraded'
    assert summary['remote_http_errors'] == 1
    assert summary['skipped_tasks'] == 26


def test_parse_failures_are_not_remote_http_errors():
    health = SourceHealth(source='ats', status='ok')
    health.note_stage('discovery', 'succeeded')
    health.note_stage('parsing', 'failed', count=3)
    assert summarize_stage_health(health)['remote_http_errors'] == 0


def test_discovery_failure_is_counted_once():
    # Discovery adapters record both the stage event and the request counter.
    health = SourceHealth(source='jsearch', status='ok')
    health.note_stage('discovery', 'failed')
    health.failed_requests += 1
    assert summarize_stage_health(health)['remote_http_errors'] == 1
