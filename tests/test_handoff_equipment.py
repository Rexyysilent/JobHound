"""R22: synthetic native ATS input, explicit inventory and real action engine."""
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from jobhound.config import CONFIG
from jobhound.filters.eligibility import EquipmentEvidence, Profile, default_profile
from jobhound.run_context import RunContext, run_scope
from jobhound.v41.engine import evaluate_raw, evaluate_observations, decision_fingerprint
from jobhound.v41.outcome_review import preview_outcomes
from jobhound.v41.outcomes import OutcomeBinding, OutcomeScope
from test_v55_action_policy import NOW, DESCRIPTION
from test_assessment_revisions import ready, NEW


def hardware(device, available, key, when="2026-09-01"):
    return EquipmentEvidence(device, available, key, "synthetic_user_report", when)


NO_CAM = hardware("webcam", False, "no_webcam")
PHONE = hardware("phone", True, "owns_phone")
CAM = hardware("webcam", True, "new_webcam", "2026-09-07")


def native(requirement, key="equipment"):
    return [dict(source="ashby", raw=dict(id=key, title="Bengali AI Response Evaluator",
        _company="Example", jobUrl="https://jobs.ashbyhq.com/example/"+key,
        location="India", isRemote=True, isListed=True, publishedAt=NOW.isoformat(),
        descriptionPlain=DESCRIPTION+"\n"+requirement,
        compensation=dict(scrapeableCompensationSalarySummary="USD 10 per working hour")))]


def evaluate(monkeypatch, records, requirement="A webcam is required.", *, profile=None):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    monkeypatch.setattr(CONFIG.v55, "account_states", [])
    profile = profile or replace(default_profile(), equipment_evidence=list(records))
    context = RunContext.capture(profile=profile, as_of=NOW)
    with run_scope(context):
        result = evaluate_raw(native(requirement), as_of=NOW)
    assert result.accounting_ok
    return result, context


def binding(result):
    item = result.evaluated[0]
    return OutcomeBinding(scope=OutcomeScope(provider="example", portal="contractors", role="equipment",
        account="local", attempt="one"), canonical_id=item.canonical.canonical_id,
        observation_id=item.canonical.best_observation.observation_id, role_url=item.job.url, reviewed=True)


def test_native_required_webcam_missing_phone_is_not_substitute(monkeypatch):
    result, _ = evaluate(monkeypatch, [NO_CAM, PHONE])
    item = result.evaluated[0]
    facts = item.assessment.requirements_assessment.equipment_compatibility
    assert facts and facts[0]["state"] == "unmet"
    assert facts[0]["selected_evidence_ids"] == ["no_webcam"]
    assert facts[0]["observation_id"] == item.canonical.best_observation.observation_id
    assert item.decision.action_band.value == "verify"
    assert item.decision.lifecycle == "active"
    assert not item.assessment.blockers
    assert any(task["missing_fact"] == "equipment_compatibility:webcam:unmet" for task in item.decision.verification_tasks)


@pytest.mark.parametrize("requirement", ["A webcam is preferred.", "A webcam is optional.",
    "A webcam is not required.", "No webcam is required."])
def test_preferred_optional_negated_webcam_do_not_veto_apply(monkeypatch, requirement):
    result, _ = evaluate(monkeypatch, [NO_CAM, PHONE], requirement)
    assert result.evaluated[0].decision.next_action == "apply"


def test_new_supported_webcam_resolves_only_equipment_dimension(monkeypatch):
    result, _ = evaluate(monkeypatch, [NO_CAM, PHONE, CAM])
    assert result.evaluated[0].decision.next_action == "apply"
    blocked, _ = evaluate(monkeypatch, [NO_CAM, PHONE, CAM], "A webcam is required.\nFluent German required.")
    assert blocked.evaluated[0].decision.action_band.value == "reject"
    assert any("lang_mismatch:german" in reason for reason in blocked.evaluated[0].assessment.blockers)


@pytest.mark.parametrize("text,requires_webcam", [
    ("Microphone is optional, but webcam is required.", True),
    ("Microphone is not required; webcam is required.", True),
    ("Webcam is optional, but microphone is required.", False),
    ("Webcam is not required; microphone is required.", False),
    ("You must use a webcam to meet our clients.", True),
    ("Our clients have webcams available.", False),
])
def test_webcam_local_modality_and_applicant_actor(monkeypatch, text, requires_webcam):
    result, _ = evaluate(monkeypatch, [NO_CAM, PHONE], text)
    assert (result.evaluated[0].decision.next_action == "verify") == requires_webcam


@pytest.mark.parametrize("text,action,state", [
    ("A webcam and microphone are optional.", "apply", "preferred"),
    ("A webcam, microphone are optional.", "apply", "preferred"),
    ("A webcam and microphone are required.", "verify", "unmet"),
    ("A webcam or smartphone is required.", "verify", "unknown"),
])
def test_shared_device_modifier_and_explicit_alternative(monkeypatch, text, action, state):
    result, _ = evaluate(monkeypatch, [NO_CAM, PHONE], text)
    item = result.evaluated[0]
    assert item.decision.next_action == action
    assert item.assessment.requirements_assessment.equipment_compatibility[0]["state"] == state


def test_uncertain_sibling_hardware_terms_do_not_veto_exact_role(monkeypatch):
    result, context = evaluate(monkeypatch, [NO_CAM, PHONE], "")
    original = result.evaluated[0].canonical.best_observation
    sibling = original.model_copy(deep=True)
    sibling.observation_id = "uncertain-sibling"
    sibling.identity_state = "uncertain"
    sibling.job.description += "\nA webcam is required."
    with run_scope(context):
        changed = evaluate_observations([original, sibling], as_of=NOW)
    assert changed.accounting_ok
    assert changed.evaluated[0].decision.next_action == "apply"
    assert not changed.evaluated[0].assessment.requirements_assessment.equipment_compatibility


def test_absent_and_future_only_inventory_keep_legacy_decision(monkeypatch):
    baseline, _ = evaluate(monkeypatch, [])
    future, _ = evaluate(monkeypatch, [replace(NO_CAM, event_date=(NOW+timedelta(days=1)).date().isoformat())])
    assert baseline.evaluated[0].decision.next_action == "apply"
    assert decision_fingerprint(baseline) == decision_fingerprint(future)
    assert not future.evaluated[0].assessment.requirements_assessment.equipment_compatibility


@pytest.mark.parametrize("records,state", [([PHONE], "unknown"),
    ([replace(NO_CAM, event_date=None)], "unknown"),
    ([NO_CAM, replace(CAM, event_date=NO_CAM.event_date)], "conflict")])
def test_unknown_undated_conflicting_inventory_is_verify(monkeypatch, records, state):
    result, _ = evaluate(monkeypatch, records)
    shuffled, _ = evaluate(monkeypatch, list(reversed(records)))
    item = result.evaluated[0]
    assert item.decision.next_action == "verify"
    assert item.assessment.requirements_assessment.equipment_compatibility[0]["state"] == state
    assert decision_fingerprint(result) == decision_fingerprint(shuffled)
    assert item.assessment.requirements_assessment.equipment_compatibility == shuffled.evaluated[0].assessment.requirements_assessment.equipment_compatibility


def test_assessment_only_requirement_does_not_veto_discovery_but_requires_webcam_for_test(monkeypatch):
    result, context = evaluate(monkeypatch, [NO_CAM, PHONE], "The assessment requires a webcam.")
    assert result.evaluated[0].decision.next_action == "apply"
    target = binding(result)
    with run_scope(context):
        changed, _ = preview_outcomes(result, ready(target), [target], as_of=NOW)
    item = changed.evaluated[0]
    assert item.decision.next_action == "verify"
    assert any(task["missing_fact"].startswith("equipment_compatibility:") for task in item.decision.verification_tasks)


def test_exact_fresh_assessment_checks_and_supported_webcam_can_progress(monkeypatch):
    result, context = evaluate(monkeypatch, [NO_CAM, PHONE, CAM], "The assessment requires a webcam.")
    target = binding(result)
    with run_scope(context):
        changed, _ = preview_outcomes(result, ready(target), [target], as_of=NOW)
    assert changed.evaluated[0].decision.next_action == "complete_known_step"
    rows = ready(target)
    rows[-1]["assessment"] = dict(NEW, revision="older")
    with run_scope(context):
        wrong, _ = preview_outcomes(result, rows, [target], as_of=NOW)
    assert wrong.evaluated[0].decision.next_action == "verify"


def test_generic_equipment_approval_does_not_erase_role_webcam_absence(monkeypatch):
    result, context = evaluate(monkeypatch, [NO_CAM, PHONE])
    target = binding(result)
    with run_scope(context):
        changed, _ = preview_outcomes(result, ready(target), [target], as_of=NOW)
    assert changed.evaluated[0].decision.next_action == "verify"
    assert changed.evaluated[0].assessment.requirements_assessment.equipment_compatibility[0]["state"] == "unmet"


def test_completed_assessment_preserves_terminal_bookkeeping(monkeypatch):
    result, context = evaluate(monkeypatch, [NO_CAM, PHONE])
    target = binding(result)
    rows = ready(target)
    rows[1]["value"] = "completed"
    with run_scope(context):
        changed, _ = preview_outcomes(result, rows, [target], as_of=NOW)
    assert changed.evaluated[0].decision.next_action == "await_response"


def test_profile_context_and_reuse_do_not_leak_hardware(monkeypatch):
    profile = replace(default_profile(), equipment_evidence=[NO_CAM, PHONE])
    result, context = evaluate(monkeypatch, [], profile=profile)
    profile.equipment_evidence.clear()
    assert context.profile().equipment_evidence == [NO_CAM, PHONE]
    detached = context.profile()
    detached.equipment_evidence.clear()
    assert context.profile().equipment_evidence == [NO_CAM, PHONE]
    assert Profile.from_config({"equipment_evidence": asdict(context.profile())["equipment_evidence"]}).equipment_evidence == [NO_CAM, PHONE]
    other, _ = evaluate(monkeypatch, [])
    assert result.evaluated[0].decision.next_action == "verify"
    assert other.evaluated[0].decision.next_action == "apply"
    assert not default_profile().equipment_evidence


@pytest.mark.parametrize("value", ["false", "true", 0, 1])
def test_availability_is_strict(value):
    with pytest.raises(ValueError):
        hardware("webcam", value, "bad")


def test_parent_supported_webcam_satisfies_explicit_device_alternative(monkeypatch):
    result, _ = evaluate(monkeypatch, [NO_CAM, PHONE, CAM], "A webcam or smartphone is required.")
    assert result.evaluated[0].decision.next_action == "apply"
    assert result.evaluated[0].assessment.requirements_assessment.equipment_compatibility[0]["state"] == "supported"


@pytest.mark.parametrize("completed", [False, True])
def test_parent_work_actions_keep_hardware_check_and_completed_bookkeeping(monkeypatch, completed):
    from test_work_outcomes import ready as work_ready, work_event
    result, context = evaluate(monkeypatch, [NO_CAM, PHONE])
    target = binding(result)
    rows = work_ready(target)
    if completed:
        rows.append(work_event(target, "submitted", "work_state", "submitted", actor="user", source_kind="user_report"))
    with run_scope(context):
        changed, _ = preview_outcomes(result, rows, [target], as_of=NOW)
    item = changed.evaluated[0]
    assert item.decision.next_action == ("await_response" if completed else "verify")
    assert item.assessment.account_state["_reviewed_work_action"]["next_action"] == item.decision.next_action
    if not completed:
        assert any(t["missing_fact"].startswith("equipment_compatibility:") for t in item.decision.verification_tasks)
