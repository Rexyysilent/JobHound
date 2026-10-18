"""Original R04/R06/R18 assertion gaps, using synthetic native inputs only."""
import asyncio
from datetime import timedelta
import json

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import evaluate_raw, evaluate_observations
from jobhound.v41.hydration import hydrate_public_posting, child_observation, RetrievalBudget, RetrievalPolicy
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.outcomes import OutcomeBinding, OutcomeScope
from test_v55_action_policy import NOW, DESCRIPTION, observation
from test_v57_outcomes import event
from test_handoff_evidence_integration import native
from test_assessment_revisions import ready, scoped
from test_handoff_signals import stage, later, material
from test_v6_delivery_recovery import store, start, finish, count
from jobhound.v41.outcomes import AssessmentIdentity


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    monkeypatch.setattr(CONFIG.v55, "account_states", [])


def native_subject():
    result = evaluate_raw(native(DESCRIPTION), as_of=NOW)
    item = result.evaluated[0]
    binding = OutcomeBinding(scope=OutcomeScope(provider="example", portal="main", role="review",
        account="local", attempt="one"), canonical_id=item.canonical.canonical_id,
        observation_id=item.canonical.best_observation.observation_id, role_url=item.job.url, reviewed=True)
    return result, binding


def test_r01_r02_r03_blocked_rate_marketing_and_restoration_have_exact_queue_transitions(store):
    baseline, binding = native_subject()
    stage(store, baseline); finish(store, start(store))
    rows = ready(binding)+[event(binding, "block", "project_access", "blocked")]
    blocked, imported = preview_outcomes(baseline, rows, [binding], as_of=NOW)
    item = blocked.evaluated[0]
    assert item.canonical.outcome_projection.value("project_access") == "blocked"
    assert item.decision.next_action not in {"apply", "complete_known_step", "start_allocated_task"}
    assert item.assessment.selected_pay == baseline.evaluated[0].assessment.selected_pay
    stage(store, blocked); finish(store, start(store))
    intents = count(store, "delivery_intents")
    marketed, marketing = preview_outcomes(baseline, [event(binding, "marketing", "marketing", "invite")],
        [binding], as_of=NOW, previous=imported["events"])
    assert marketing["counts"]["quarantined"] == 1
    assert material(marketed) == material(blocked)
    repeated = stage(store, later(marketed))["destinations"][0]
    assert repeated["counts"]["already_recorded"] == 1
    assert count(store, "delivery_intents") == intents
    restoration = event(binding, "restored", "project_access", "accessible", supersedes=["block"],
        event_at=(NOW-timedelta(minutes=20)).isoformat(), observed_at=NOW.isoformat())
    restored, report = preview_outcomes(baseline, [restoration], [binding], as_of=NOW, previous=imported["events"])
    assert restored.evaluated[0].decision.next_action == "complete_known_step"
    assert report["projections"][0].dispositions["block"] == "superseded"
    assert {fact.event_id for fact in report["events"]} >= {"block", "restored"}
    queued = stage(store, later(restored))["destinations"][0]
    assert queued["counts"]["selected_cards"] == 1
    finish(store, start(store))
    again, replay = preview_outcomes(baseline, [restoration], [binding], as_of=NOW, previous=report["events"])
    assert replay["counts"]["duplicate"] == 1
    assert stage(store, later(later(again)))["destinations"][0]["counts"]["already_recorded"] == 1
    assert count(store, "delivery_intents") == intents+1


def test_r08_selected_video_reminder_cannot_extend_deadline_or_create_paid_allocation():
    baseline, binding = native_subject()
    video = dict(step_id="video", revision="terms-1")
    due = NOW-timedelta(hours=1)
    rows = ready(binding, video)+[
        scoped(binding, "due", "assessment_deadline", dict(due_at=due.isoformat()), video),
        scoped(binding, "reminder", "assessment_state", "invited", video,
            event_at=(NOW-timedelta(minutes=20)).isoformat(), observed_at=NOW.isoformat()),
        event(binding, "newsletter", "marketing", "invite")]
    changed, report = preview_outcomes(baseline, rows, [binding], as_of=NOW)
    projection = report["projections"][0]
    assert report["counts"]["quarantined"] == 1
    assert projection.value("assessment_deadline", assessment=AssessmentIdentity(**video)).due_at == due
    item = changed.evaluated[0]
    assert item.decision.next_action == "verify"
    assert item.assessment.verification_tasks
    assert not any(fact.predicate in {"allocation_state", "work_terms", "collected_payment"} for fact in projection.events)


def test_r09_public_offered_rate_and_45_minute_onboarding_cost_are_not_contract_or_work():
    baseline, binding = native_subject()
    rows = ready(binding)+[scoped(binding, "cost", "action_cost",
        dict(minutes=45, cash=0, currency="USD", basis="provider_estimate"))]
    changed, report = preview_outcomes(baseline, rows, [binding], as_of=NOW)
    item = changed.evaluated[0]
    projection = report["projections"][0]
    identity = projection.value("assessment_step")
    assert item.assessment.selected_pay == baseline.evaluated[0].assessment.selected_pay
    assert item.assessment.selected_pay.amount_low == 10
    assert projection.value("action_cost", assessment=identity).minutes == 45
    assert projection.value("action_cost", assessment=identity).basis == "provider_estimate"
    assert item.decision.next_action == "complete_known_step"
    assert not any(fact.predicate in {"allocation_state", "work_terms", "invoice", "collected_payment"} for fact in projection.events)


def test_r04_r06_native_three_exact_rejections_replace_old_pending_and_keep_every_fact():
    raw = [dict(source="ashby", raw=dict(id=key, title="Bengali AI Response Evaluator",
        _company="Example", jobUrl="https://jobs.ashbyhq.com/example/"+key, location="India",
        isRemote=True, publishedAt=NOW.isoformat(), descriptionPlain=DESCRIPTION,
        compensation=dict(scrapeableCompensationSalarySummary="USD 10 per working hour")))
        for key in ("first", "second", "third")]
    original = evaluate_raw(raw, as_of=NOW)
    bindings, records = [], []
    for index, item in enumerate(original.evaluated):
        binding = OutcomeBinding(scope=OutcomeScope(provider="example", portal="main", role=str(index),
            account="local", attempt="one"), canonical_id=item.canonical.canonical_id,
            observation_id=item.canonical.best_observation.observation_id, role_url=item.job.url, reviewed=True)
        bindings.append(binding)
        records += [event(binding, "pending-"+str(index), "application_state", "submitted",
            event_at=(NOW-timedelta(days=30)).isoformat(), observed_at=(NOW-timedelta(days=29)).isoformat()),
            event(binding, "reject-"+str(index), "application_state", "rejected")]
    changed, report = preview_outcomes(original, list(reversed(records)), bindings, as_of=NOW)
    assert original.accounting_ok and changed.accounting_ok
    assert len(changed.evaluated) == len(report["projections"]) == 3
    assert len(report["events"]) == 6 and report["counts"]["accepted"] == 6
    assert all(item.decision.lifecycle == "closed" and item.decision.next_action == "await_change" for item in changed.evaluated)
    for projection in report["projections"]:
        assert projection.value("application_state") == "rejected"
        assert {fact.value for fact in projection.events} == {"submitted", "rejected"}
    assert all(item.assessment.selected_pay.amount_low == 10 for item in changed.evaluated)


@pytest.mark.parametrize("closed", [False, True])
def test_r18_full_page_footer_closure_keeps_fixed_offer_and_selection_explanation(closed):
    parent = observation()
    parent.job.url = "https://unfamiliar.example.org/jobs/synthetic-project"
    parent.job.pay_raw = "USD 18 fixed project budget"
    payload = {"@type": "JobPosting", "url": parent.job.url, "title": parent.job.title,
        "description": DESCRIPTION+"\nCompensation: USD 18 fixed project budget.",
        "hiringOrganization": {"name": "Example"},
        "jobLocationType": "TELECOMMUTE", "applicantLocationRequirements": {"name": "India"}}
    body = "<h1>"+parent.job.title+"</h1><header>We are recruiting for this project.</header>"
    body += '<script type="application/ld+json">'+json.dumps(payload)+"</script><main>"+("<p>Project information and task instructions.</p>"*450)+"</main>"
    body += "<footer><p>"+("This project has been completed." if closed else "This project is accepting applications.")+"</p><a href='/apply'>Apply</a></footer>"
    assert len(body) > 20_000
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as client:
            return await hydrate_public_posting(parent.job.url, parent.job, client=client,
                budget=RetrievalBudget(RetrievalPolicy(max_retries=0)))
    outcome = asyncio.run(run())
    child = child_observation(parent, outcome)
    result = evaluate_observations([parent, child], as_of=max(NOW, child.captured_at))
    assert outcome.state == child.content_state == "complete" and result.accounting_ok
    assert len(result.observations) == 2
    item = result.evaluated[0]
    assert item.assessment.selected_pay.amount_low == 18
    assert item.assessment.selected_pay.actual_unit == "fixed_project"
    assert "USD 18" in outcome.payload["description"]
    if closed:
        assert child.vacancy_state == "explicitly_closed"
        assert outcome.application_route_state == "account_unknown"
        assert outcome.payload["_closure_evidence"]["text"] == "This project has been completed."
        assert item.decision.lifecycle == "closed"
        assert item.decision.next_action not in {"apply", "complete_known_step", "start_allocated_task"}
        assert "source_quality:explicitly_closed" in item.assessment.blockers
        assert item.assessment.lifecycle_reason == "matching_source_explicitly_closed"
    else:
        assert child.vacancy_state != "explicitly_closed"
        assert outcome.application_route_state == "observed_public_route"
        assert "_closure_evidence" not in outcome.payload
        assert item.decision.lifecycle != "closed"
