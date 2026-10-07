"""Revision-specific buyer economics through native projection and the engine."""
from copy import deepcopy
from datetime import timedelta
import pytest
from jobhound.config import CONFIG
from jobhound.v41.community import (LEGACY_VERSION, observation_thread, project_thread,
    saved_thread_observation, thread_outcome)
from jobhound.v41.engine import evaluate_observations
from test_community_threads import BODY, NOW, URL, post, topic

@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    monkeypatch.setattr(CONFIG.v55, 'account_states', [])

def native(*updates, partial=False, version=None):
    pages=[topic(post(text=BODY+' Budget: USD 500 fixed.'), *updates)]
    if partial:pages[0]['post_stream']['stream'].append(99)
    kwargs={'schema_version': version} if version else {}
    thread=project_thread(pages,URL,NOW,**kwargs)
    child=saved_thread_observation(thread_outcome(thread,pages))
    result=evaluate_observations([child],as_of=NOW,raw_count=0)
    assert result.accounting_ok and len(result.evaluated)==1
    return thread,child,result.evaluated[0]

def update(text, key=2, uid=10, **kw):
    return post(key,uid,text,created_at=(NOW-timedelta(hours=key)).isoformat(),**kw)

def test_explicit_budget_revision_selects_current_amount_and_retains_old_native_claim():
    thread,child,row=native(update('Updated budget: USD 800 fixed.'))
    assert thread.terms_state=='budget_changed' and thread.current_terms_post_ids==[2]
    assert row.assessment.selected_pay.amount_low==800 and not row.assessment.pay_conflict
    old=next(c for c in row.assessment.pay_candidates if c.amount_low==500)
    assert old.scope=='historical_thread_terms' and old.actor=='forum_actor:10'
    assert old.structured_path.endswith('/0/cooked')
    assert '500' in child.job.description and '800' in child.job.description
    assert row.assessment.selected_pay.scope.endswith(':revision:'+thread.current_terms_revision_id)
    assert thread.revisions[1].supersedes==[thread.revisions[0].revision_id]
    assert child.job.posted_at==NOW-timedelta(days=30)
    assert 'community_terms:budget_changed' in row.assessment.unresolved
    assert any(t['missing_fact']=='community_scope_revision' for t in row.assessment.verification_tasks)
    assert row.decision.next_action not in {'start_allocated_task','complete_known_step','bid'}

def test_scope_changed_without_new_budget_cannot_reuse_original_quote():
    thread,child,row=native(update('The scope now requires self-hosted n8n migration.'))
    assert thread.terms_state=='scope_changed' and thread.current_terms_post_ids==[]
    assert row.assessment.selected_pay is None and not row.assessment.pay_conflict
    assert any(c.amount_low==500 and c.scope=='historical_thread_terms' for c in row.assessment.pay_candidates)
    assert 'migration' in child.job.description

def test_scope_and_new_budget_in_same_buyer_post_are_one_revision():
    thread,_,row=native(update('The scope now requires self-hosted n8n migration. New budget: USD 900 fixed.'))
    assert thread.terms_state=='scope_changed' and thread.current_terms_post_ids==[2]
    assert row.assessment.selected_pay.amount_low==900 and not row.assessment.pay_conflict

def test_later_explicit_budget_supersedes_scope_revision_with_no_carried_quote():
    earlier=post(2,10,'The scope now requires self-hosted n8n migration.',created_at=(NOW-timedelta(hours=3)).isoformat())
    later=post(3,10,'Current budget: USD 750 fixed.',created_at=(NOW-timedelta(hours=1)).isoformat())
    thread,_,row=native(earlier,later)
    assert [r.kind for r in thread.revisions]==['original','scope_changed','budget_changed']
    assert thread.revisions[2].supersedes==[thread.revisions[1].revision_id]
    assert row.assessment.selected_pay.amount_low==750

@pytest.mark.parametrize('text',[
    'Thanks for reviewing the scope and budget!',
    'Thanks for the updated scope!',
    'Would the updated scope include hosting?',
    'I am available. My services start at USD 900 fixed.',
    '<blockquote>Updated budget: USD 900 fixed. The scope now requires hosting.</blockquote>',
])
def test_thanks_seller_and_quoted_changes_cannot_create_buyer_revision(text):
    uid=20 if text.startswith('I am') else 10
    thread,_,row=native(update(text,uid=uid))
    assert len(thread.revisions)==1 and thread.terms_state=='original'
    assert row.assessment.selected_pay.amount_low==500
    assert thread.last_substantive_buyer_at==thread.original_at

@pytest.mark.parametrize('date',[None,'2026-12-01T00:00:00Z','2026-01-01T00:00:00Z'])
def test_unknown_future_or_backwards_revision_time_never_establishes_current_terms(date):
    changed=post(2,10,'Updated budget: USD 900 fixed.',created_at=date,updated_at=date)
    thread,_,row=native(changed)
    assert thread.terms_state in {'ambiguous','incomplete'}
    assert thread.current_terms_revision_id is None and not thread.current_terms_post_ids
    assert not thread.revisions[1].supersedes
    assert row.assessment.selected_pay is None
    assert all(c.scope=='unresolved_thread_terms' for c in row.assessment.pay_candidates)

def test_missing_later_post_does_not_certify_current_budget_or_supersession():
    thread,_,row=native(update('Updated budget: USD 900 fixed.'),partial=True)
    assert thread.terms_state=='incomplete' and row.assessment.selected_pay is None
    assert not thread.revisions[1].supersedes

def test_older_post_edited_after_revision_leaves_current_amount_unresolved():
    pages=[topic(post(text=BODY+' Budget: USD 500 fixed.',updated_at=NOW.isoformat()),
        update('Updated budget: USD 900 fixed.'))]
    thread=project_thread(pages,URL,NOW)
    child=saved_thread_observation(thread_outcome(thread,pages))
    row=evaluate_observations([child],as_of=NOW,raw_count=0).evaluated[0]
    assert thread.terms_state=='ambiguous' and row.assessment.selected_pay is None
    assert thread.current_terms_revision_id is None
    assert not thread.revisions[1].supersedes

def test_formatting_capture_time_and_edit_metadata_do_not_change_material_revision_identity():
    pages=[topic(post(text=BODY+' Budget: USD 500 fixed.'),update('Updated budget: USD 800 fixed.'))]
    first=project_thread(pages,URL,NOW)
    changed=deepcopy(pages)
    changed[0]['post_stream']['posts'][1]['cooked']='<p> Updated   budget: USD 800 fixed. </p>'
    changed[0]['post_stream']['posts'][1]['updated_at']=(NOW-timedelta(hours=1)).isoformat()
    second=project_thread(changed,URL,NOW+timedelta(days=1))
    assert first.current_terms_revision_id==second.current_terms_revision_id
    assert first.original_at==second.original_at and first.last_substantive_buyer_at==second.last_substantive_buyer_at
    assert first.revisions[1].edited_at!=second.revisions[1].edited_at
    assert not second.revisions[1].edit_history_available

def test_legacy_native_observation_remains_replayable_without_relabeling_its_terms():
    thread,child,row=native(update('Updated budget: USD 800 fixed.'),version=LEGACY_VERSION)
    assert observation_thread(child).schema_version==LEGACY_VERSION
    assert 'revisions' not in child.raw_payload['community_thread']
    assert row.assessment.pay_conflict
    assert not any(c.scope=='historical_thread_terms' for c in row.assessment.pay_candidates)

def test_tampered_revision_metadata_is_rederived_and_rejected():
    _,child,_=native(update('Updated budget: USD 800 fixed.'))
    child.raw_payload['community_thread']['current_terms_post_ids']=[1]
    assert observation_thread(child) is None

def test_budget_question_preserves_conflict_without_claiming_supersession():
    thread,_,row=native(update('Updated budget: USD 800 fixed?'))
    assert len(thread.revisions)==1 and thread.terms_state=='original'
    assert row.assessment.pay_conflict
    assert not any(c.scope=='historical_thread_terms' for c in row.assessment.pay_candidates)

def test_closed_buyer_revision_retains_history_and_skip():
    changed=post(2,10,'Updated budget: USD 800 fixed.',created_at=(NOW-timedelta(hours=3)).isoformat())
    closed=post(3,10,'This project is closed.',created_at=(NOW-timedelta(hours=1)).isoformat())
    thread,_,row=native(changed,closed)
    assert thread.revisions[-1].kind=='closed' and thread.closure_post_id==3
    assert row.decision.next_action=='skip' and row.assessment.action_readiness=='closed'
