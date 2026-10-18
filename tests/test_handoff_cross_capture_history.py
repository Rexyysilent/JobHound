"""R16/R17 saved native cross-capture witnesses; no authenticated edit history."""
from copy import deepcopy
from datetime import timedelta
import itertools
import json
from pathlib import Path

import pytest

from jobhound.config import CONFIG
from jobhound.delivery_outbox import evaluated_row
from jobhound.review_audit import fingerprint
from jobhound.handoff_signal import handoff_material_projection
from jobhound.v41.community import (project_thread, thread_outcome,
    saved_thread_observation, observation_thread)
from jobhound.v41.engine import evaluate_observations, decision_fingerprint
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.outcomes import OutcomeBinding, OutcomeScope
from jobhound.v41.models import ListingObservation
from test_community_threads import NOW, URL, BODY, post, topic
from test_work_outcomes import ready


ORIGINAL = NOW-timedelta(days=60)
EARLY = NOW-timedelta(days=40)
LATE = NOW-timedelta(days=1)
AS_OF = NOW+timedelta(days=1)


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])
    monkeypatch.setattr(CONFIG.v55, 'community_buyer_activity_max_age_days', 14)


def root(**updates):
    return post(created_at=ORIGINAL.isoformat(), updated_at=ORIGINAL.isoformat(), **updates)


def capture(pages, at, *, selected_post_id=None):
    raw = deepcopy(pages)
    thread = project_thread(raw, URL, at, selected_post_id=selected_post_id)
    observation = saved_thread_observation(thread_outcome(thread, raw, selected_post_id=selected_post_id))
    assert observation_thread(observation) == thread
    assert raw == pages
    return observation


def evaluate(*observations):
    result = evaluate_observations(list(observations), as_of=AS_OF, raw_count=0)
    assert result.accounting_ok
    return result


@pytest.mark.parametrize('change', ['seller', 'buyer-thanks', 'format', 'edit-metadata', 'capture-only'])
def test_r16_separate_captures_cannot_refresh_historical_demand(change):
    first_pages = [topic(root())]
    later_pages = deepcopy(first_pages)
    if change == 'seller':
        seller = post(2, 20, 'Hire me. I offer this workflow repair for USD 900 fixed.',
                      created_at=(LATE-timedelta(hours=1)).isoformat())
        later_pages = [topic(root(), seller)]
    elif change == 'buyer-thanks':
        later_pages = [topic(root(), post(2, 10, 'Thanks for your help!',
                        created_at=(LATE-timedelta(hours=1)).isoformat()))]
    elif change == 'format':
        later_pages[0]['post_stream']['posts'][0]['cooked'] = '<div>  '+BODY.replace(' ', '  ')+' </div>'
        later_pages[0]['post_stream']['posts'][0]['updated_at'] = LATE.isoformat()
    elif change == 'edit-metadata':
        later_pages[0]['post_stream']['posts'][0]['updated_at'] = LATE.isoformat()
    first, second = capture(first_pages, EARLY), capture(later_pages, LATE)
    before = [row.model_dump_json() for row in (first, second)]
    baseline = evaluate(first)
    for ordered in [(first, second), (second, first)]:
        combined = evaluate(*ordered)
        assert len(combined.observations) == 2
        assert len(combined.evaluated) == 1
        item = combined.evaluated[0]
        assert len(item.canonical.observations) == 2
        assert item.job.posted_at == ORIGINAL
        assert item.assessment.lifecycle_reason == 'historical_buyer_request'
        assert item.assessment.freshness == baseline.evaluated[0].assessment.freshness
        assert item.decision.next_action != 'start_allocated_task'
        assert fingerprint(handoff_material_projection(evaluated_row(item))) == fingerprint(handoff_material_projection(evaluated_row(baseline.evaluated[0])))
        for native in item.canonical.observations:
            parsed = observation_thread(native)
            assert parsed.original_at == parsed.last_substantive_buyer_at == ORIGINAL
            assert parsed.observed_at in {EARLY, LATE}
    assert before == [row.model_dump_json() for row in (first, second)]
    if change != 'capture-only':
        assert first.observation_id != second.observation_id
        assert first.content_hash != second.content_hash


@pytest.mark.parametrize('newer_capture', ['closure', 'seller-after-closure', 'cosmetic-after-closure'])
def test_r17_new_buyer_closure_beats_longer_old_hiring_and_retains_both_native_captures(newer_capture):
    old_pages = [topic(root(text=BODY+' We need a developer for this workflow. '*25))]
    closing = post(2, 10, 'This project is closed. I have hired a developer.',
                   created_at=(LATE-timedelta(hours=2)).isoformat())
    new_pages = [topic(root(), closing)]
    if newer_capture == 'seller-after-closure':
        seller = post(3, 20, 'I offer workflow repair. I am available and looking for paid work.',
                      created_at=(LATE-timedelta(hours=1)).isoformat())
        new_pages = [topic(root(), closing, seller)]
    elif newer_capture == 'cosmetic-after-closure':
        new_pages[0]['post_stream']['posts'][1]['cooked'] = '<div> This project is closed.  I have hired a developer. </div>'
        new_pages[0]['post_stream']['posts'][1]['updated_at'] = LATE.isoformat()
    older, closed = capture(old_pages, EARLY), capture(new_pages, LATE)
    assert len(older.job.description) > len(closed.job.description)
    retained = [older.model_dump_json(), closed.model_dump_json()]
    fingerprints = set()
    for ordered in itertools.permutations([older, closed]):
        result = evaluate(*ordered)
        fingerprints.add(decision_fingerprint(result))
        assert len(result.evaluated) == 1 and len(result.observations) == 2
        item = result.evaluated[0]
        assert item.job.posted_at == ORIGINAL
        assert item.decision.lifecycle == 'closed'
        assert item.decision.action_band.value == 'reject'
        assert 'source_quality:explicitly_closed' in item.assessment.blockers
        assert {row.content_hash for row in item.canonical.observations} == {older.content_hash, closed.content_hash}
        assert observation_thread(closed).closure_post_id == 2
        assert observation_thread(older).closure_post_id is None
    assert len(fingerprints) == 1
    assert retained == [older.model_dump_json(), closed.model_dump_json()]


def test_seller_closure_or_forum_lock_in_later_capture_cannot_close_buyer_request():
    earlier = capture([topic(root())], EARLY)
    seller = post(2, 20, 'I offer workflow repair. This project is closed.',
                  created_at=(LATE-timedelta(hours=1)).isoformat())
    later = capture([topic(root(), seller, closed=True)], LATE)
    result = evaluate(earlier, later)
    assert result.evaluated[0].decision.lifecycle != 'closed'
    assert all(observation_thread(row).closure_post_id is None for row in result.observations)


def test_partial_later_capture_retains_closure_text_without_establishing_current_closure():
    earlier = capture([topic(root())], EARLY)
    pages = [topic(root(), post(2, 10, 'This project is closed.',
        created_at=(LATE-timedelta(hours=1)).isoformat()))]
    pages[0]['post_stream']['stream'].append(99)
    later = capture(pages, LATE)
    assert observation_thread(later).closure_post_id == 2
    assert later.content_state == 'partial' and later.vacancy_state == 'unknown'
    result = evaluate(earlier, later)
    assert result.evaluated[0].decision.lifecycle != 'closed'
    assert len(result.evaluated[0].canonical.observations) == 2


def test_exact_closure_does_not_close_a_different_buyer_post_in_the_same_topic():
    selected = post(2, 20, BODY+' Budget: USD 250 fixed.',
                    created_at=(LATE-timedelta(hours=3)).isoformat())
    closing = post(3, 10, 'This project is closed.', created_at=(LATE-timedelta(hours=1)).isoformat())
    pages = [topic(root(), selected, closing)]
    original = capture(pages, LATE)
    separate = capture(pages, LATE, selected_post_id=2)
    result = evaluate(original, separate)
    assert len(result.evaluated) == 2
    indexed = {row.job.url: row for row in result.evaluated}
    assert indexed[original.job.url].decision.lifecycle == 'closed'
    assert indexed[separate.job.url].decision.lifecycle != 'closed'
    assert observation_thread(separate).buyer_actor_id == 20


def test_cross_capture_public_closure_preserves_exact_reviewed_allocated_work():
    earlier = capture([topic(root())], EARLY)
    later = capture([topic(root(), post(2, 10, 'This project is closed.',
        created_at=(LATE-timedelta(hours=1)).isoformat()))], LATE)
    result = evaluate(earlier, later)
    row = result.evaluated[0]
    binding = OutcomeBinding(scope=OutcomeScope(provider='example', portal='main',
        role='workflow', account='local', attempt='one'), canonical_id=row.canonical.canonical_id,
        observation_id=earlier.observation_id, role_url=earlier.job.url, reviewed=True)
    # Work fixtures are dated before AS_OF; use that same frozen engine clock.
    work = ready(binding)
    for fact in work:
        fact['event_at'] = (AS_OF-timedelta(hours=2)).isoformat()
        fact['observed_at'] = (AS_OF-timedelta(hours=1)).isoformat()
    changed, imported = preview_outcomes(result, work, [binding], as_of=AS_OF)
    assert imported['counts']['quarantined'] == 0
    assert changed.evaluated[0].decision.next_action == 'start_allocated_task'
    assert 'source_quality:explicitly_closed' in changed.evaluated[0].assessment.blockers


def test_unmarked_monetary_buyer_followup_remains_native_current_terms_support():
    followup = post(2, 10, 'I can pay USD 650 fixed for the workflow.',
                    created_at=(LATE-timedelta(hours=1)).isoformat())
    observed = capture([topic(root(), followup)], LATE)
    parsed = observation_thread(observed)
    assert parsed.current_terms_post_ids == [1, 2]
    row = evaluate(observed).evaluated[0]
    assert row.assessment.selected_pay.amount_low == 650
    assert row.assessment.selected_pay.actor == 'forum_actor:10'
    assert row.assessment.selected_pay.structured_path.endswith('/1/cooked')


def test_actual_scope_revision_retains_original_demand_date_and_has_distinct_action_material():
    first = capture([topic(root())], EARLY)
    update = post(2, 10, 'The scope now requires self-hosted n8n migration.',
                  created_at=(LATE-timedelta(hours=1)).isoformat())
    later = capture([topic(root(), update)], LATE)
    parsed = observation_thread(later)
    assert parsed.original_at == ORIGINAL
    assert parsed.last_substantive_buyer_at == LATE-timedelta(hours=1)
    assert parsed.terms_state == 'scope_changed'
    assert handoff_material_projection(evaluated_row(evaluate(first).evaluated[0])) != handoff_material_projection(evaluated_row(evaluate(later).evaluated[0]))


def test_unmarked_declarative_scope_followup_keeps_native_terms_support():
    first = capture([topic(root())], EARLY)
    extra = post(2, 10, 'Requirements: include the self-hosted n8n migration in the deliverables.',
                 created_at=(LATE-timedelta(hours=1)).isoformat())
    later = capture([topic(root(), extra)], LATE)
    parsed = observation_thread(later)
    assert parsed.current_terms_post_ids == [1, 2]
    assert len(parsed.posts) == 2 and 'migration' in parsed.description
    assert parsed.terms_state == 'original'


def test_archived_baseline_v2_thanks_capture_validates_but_modified_projection_does_not():
    path = Path(__file__).parent/'fixtures/community_history_v2_before.json'
    observation = ListingObservation.model_validate(json.loads(path.read_text(encoding='utf-8')))
    archived = observation_thread(observation)
    assert archived is not None and archived.schema_version == 'community-thread/v2'
    assert archived.current_terms_post_ids == [1, 2]
    assert archived.last_substantive_buyer_at == archived.original_at == ORIGINAL
    altered = observation.model_copy(deep=True)
    altered.raw_payload['community_thread']['current_terms_post_ids'] = [1]
    assert observation_thread(altered) is None
