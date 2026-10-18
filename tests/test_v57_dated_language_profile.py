"""R25: synthetic explicit profile declarations through the real engine."""
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from jobhound.config import CONFIG
from jobhound.filters.eligibility import LanguageEvidence, Profile, default_profile
from jobhound.run_context import RunContext, run_scope
from jobhound.v41.engine import decision_fingerprint, evaluate_observations
from jobhound.v41.models import RequirementClaim, RequirementsAssessment
from jobhound.v41.requirements import requirement_outcomes
from test_v55_action_policy import NOW, observation


def fact(key, *, modality="general", level="fluent", stance="capability",
         when="2026-01-01", language="hindi"):
    return LanguageEvidence(language, modality, level, stance, key, "synthetic_profile", when)


OLD_CV = fact("cv_old")
WRITTEN_AVOID = fact("preference_new", modality="written", level="advanced",
                     stance="avoid", when="2026-09-01")
SPOKEN = fact("spoken_current", modality="spoken", when="2026-09-01")


def evaluate(monkeypatch, facts, requirement="Advanced written Hindi proficiency required.", extra="", title="Bengali AI Response Evaluator"):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    profile = replace(default_profile(), language_evidence=list(facts))
    row = observation()
    row.job.title = title
    row.job.description = ("Work\nReview Bengali and English AI responses using supplied guidelines.\n"
        "Requirements\n" + requirement + "\nRemote applicants in India may apply.\n"
        "No prior AI-training experience or university degree required.\n" + extra)
    context = RunContext.capture(profile=profile, as_of=NOW)
    with run_scope(context):
        result = evaluate_observations([row], as_of=NOW)
    assert result.accounting_ok
    return result, profile, context


def test_new_written_preference_scoped_and_history_retained(monkeypatch):
    result, profile, _ = evaluate(monkeypatch, [OLD_CV, WRITTEN_AVOID, SPOKEN])
    item = result.evaluated[0]
    assert item.decision.action_band.value == "reject"
    assert "language:lang_mismatch:hindi:written:preference_avoided" in item.assessment.blockers
    projection = item.assessment.requirements_assessment.profile_language_evidence
    assert any({record["evidence_id"] for record in check["evidence"]} ==
               {"cv_old", "preference_new"} for check in projection)
    assert profile.language_evidence == [OLD_CV, WRITTEN_AVOID, SPOKEN]


def test_spoken_positive_survives_written_preference(monkeypatch):
    result, _, _ = evaluate(monkeypatch, [OLD_CV, WRITTEN_AVOID, SPOKEN],
                            "Fluent spoken Hindi required.")
    item = result.evaluated[0]
    assert item.decision.action_band.value == "primary", item.assessment.unresolved
    assert not any("lang_mismatch:" in reason for reason in item.assessment.blockers)


def test_spoken_native_is_not_native_editorial(monkeypatch):
    result, _, _ = evaluate(monkeypatch, [fact("native_speech", modality="spoken", level="native")],
                            "Native written Hindi editorial proficiency required.")
    item = result.evaluated[0]
    assert item.decision.action_band.value == "verify"
    assert "language_unverified:hindi:written:unknown" in item.assessment.unresolved


def test_absent_evidence_and_future_only_preserve_decisions(monkeypatch):
    baseline, _, _ = evaluate(monkeypatch, [])
    future, _, _ = evaluate(monkeypatch, [replace(WRITTEN_AVOID,
        event_date=(NOW + timedelta(days=1)).date().isoformat())])
    assert baseline.evaluated[0].decision.action_band.value == "primary"
    assert decision_fingerprint(baseline) == decision_fingerprint(future)


@pytest.mark.parametrize("facts,state", [
    ([OLD_CV, replace(WRITTEN_AVOID, event_date=None)], "unknown"),
    ([fact("one", modality="written", level="native"),
      fact("two", modality="written", level="basic")], "conflict"),
    ([WRITTEN_AVOID, replace(WRITTEN_AVOID, stance="allow", evidence_id="allow")], "conflict"),
])
def test_undated_and_same_date_conflicts_verify(monkeypatch, facts, state):
    result, _, _ = evaluate(monkeypatch, facts)
    item = result.evaluated[0]
    assert item.decision.action_band.value == "verify"
    assert f"language_unverified:hindi:written:{state}" in item.assessment.unresolved


def test_newer_written_capability_does_not_revoke_preference(monkeypatch):
    result, _, _ = evaluate(monkeypatch, [WRITTEN_AVOID,
        fact("later_ability", modality="written", level="native", when="2026-09-02")])
    assert result.evaluated[0].decision.action_band.value == "reject"


@pytest.mark.parametrize("requirement", ["Native Hindi Editor required.",
    "Native Hindi Writer required.", "Native Hindi Proofreader required.",
    "Advanced written and spoken Hindi required."])
def test_editorial_nouns_and_shared_modalities_keep_written_scope(monkeypatch, requirement):
    result, _, _ = evaluate(monkeypatch, [OLD_CV, WRITTEN_AVOID, SPOKEN], requirement)
    assert result.evaluated[0].decision.action_band.value == "reject"
    assert any("written:preference_avoided" in reason for reason in result.evaluated[0].assessment.blockers)


def test_same_date_scoped_support_is_independent_of_evidence_order(monkeypatch):
    general = fact("broad", level="native")
    written = fact("specific", modality="written", level="native")
    first, _, _ = evaluate(monkeypatch, [general, written])
    shuffled, _, _ = evaluate(monkeypatch, [written, general])
    assert first.evaluated[0].decision.action_band.value == "primary"
    assert decision_fingerprint(first) == decision_fingerprint(shuffled)
    assert first.evaluated[0].assessment.requirements_assessment.profile_language_evidence == shuffled.evaluated[0].assessment.requirements_assessment.profile_language_evidence


def test_native_hindi_editor_title_carries_written_demand(monkeypatch):
    result, _, _ = evaluate(monkeypatch, [OLD_CV, WRITTEN_AVOID, SPOKEN],
        "Fluent spoken Hindi required.", title="Native Hindi Editor")
    assert "language:lang_mismatch:hindi:written:preference_avoided" in result.evaluated[0].assessment.blockers


def test_explicit_newer_allow_releases_preference(monkeypatch):
    result, _, _ = evaluate(monkeypatch, [WRITTEN_AVOID,
        fact("ability", modality="written", level="native"),
        fact("allow", modality="written", level="advanced", stance="allow", when="2026-09-02")])
    assert result.evaluated[0].decision.action_band.value == "primary"


def test_alternate_supported_language_passes(monkeypatch):
    result, _, _ = evaluate(monkeypatch, [OLD_CV, WRITTEN_AVOID, SPOKEN],
        "Advanced written Hindi or fluent spoken Bengali required.")
    assert result.evaluated[0].decision.action_band.value == "primary"


def test_another_languages_writing_requirement_does_not_bind_hindi(monkeypatch):
    result, _, _ = evaluate(monkeypatch, [OLD_CV, WRITTEN_AVOID, SPOKEN],
        "Fluent spoken Hindi and native written English required.")
    item = result.evaluated[0]
    assert item.decision.action_band.value == "primary", item.assessment.blockers
    assert all(check["modality"] == "spoken" for check in
               item.assessment.requirements_assessment.profile_language_evidence)


def test_profile_snapshot_and_job_prose_are_isolated(monkeypatch):
    result, profile, context = evaluate(monkeypatch, [OLD_CV, SPOKEN], "Fluent spoken Hindi required.",
        "About us\nOur customers mention avoiding advanced written Hindi tasks.")
    assert result.evaluated[0].decision.action_band.value == "primary"
    assert context.profile().language_evidence == profile.language_evidence
    restored = context.profile()
    restored.language_evidence.clear()
    assert context.profile().language_evidence == [OLD_CV, SPOKEN]
    assert Profile.from_config({"language_evidence": asdict(profile)["language_evidence"]}).language_evidence == [OLD_CV, SPOKEN]
    assert not default_profile().language_evidence


def test_reused_requirements_rebuild_profile_projection_without_old_provenance():
    claim = RequirementClaim(dimension="language", value=["hindi"],
        modality="required", span="Advanced written Hindi required.")
    requirements = RequirementsAssessment(claims=[claim], languages=[claim])
    restricted = replace(default_profile(), language_evidence=[OLD_CV, WRITTEN_AVOID])
    first = requirement_outcomes(requirements, restricted, as_of=NOW)
    initial = list(requirements.profile_language_evidence)
    assert first.blockers
    assert initial
    repeated = requirement_outcomes(requirements, restricted, as_of=NOW)
    assert repeated == first
    assert requirements.profile_language_evidence == initial

    second_fact = fact("other_profile_written", modality="written", level="native")
    second = replace(default_profile(), language_evidence=[second_fact])
    assert not requirement_outcomes(requirements, second, as_of=NOW).blockers
    ids = {record["evidence_id"] for check in requirements.profile_language_evidence
           for record in check["evidence"]}
    assert ids == {"other_profile_written"}

    empty = replace(default_profile(), language_evidence=[])
    assert not requirement_outcomes(requirements, empty, as_of=NOW).blockers
    assert requirements.profile_language_evidence == []


@pytest.mark.parametrize("field,value", [("modality", "telepathy"), ("level", "expertish"),
                                        ("event_date", "yesterday"), ("source", "")])
def test_invalid_explicit_evidence_is_rejected(field, value):
    with pytest.raises(ValueError):
        LanguageEvidence(**{**asdict(OLD_CV), field: value})
