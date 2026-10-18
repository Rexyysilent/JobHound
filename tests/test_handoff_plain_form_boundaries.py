"""R24 unfamiliar-employer native hydration -> child -> engine witnesses."""
import asyncio
from datetime import datetime, timezone
import json

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.text import html_to_text
from jobhound.v41.engine import evaluate_observations
from jobhound.v41.hydration import hydrate_public_posting, child_observation, RetrievalBudget, RetrievalPolicy
from jobhound.v41.models import ListingObservation, SourceKind
from test_html_role_regions import BODY

URL = "https://unfamiliar.example.org/jobs/review"
TITLE = "English AI Response Evaluator"
ROLE = BODY.replace("Bengali and English", "English")
FORM_LINES = ["Application form", "Language choices", "Bengali", "Dutch fluency required.",
              "Expected compensation", "USD 300 per working hour", "This project has been completed.",
              "Submit application"]
FORM = "\n".join(FORM_LINES)
FORM_HTML = "<h2>"+FORM_LINES[0]+"</h2>"+"".join("<p>"+line+"</p>" for line in FORM_LINES[1:])


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", True)
    monkeypatch.setattr(CONFIG.v55, "account_states", [])


def hydrate(text, mode, *, visible=None):
    parent = ListingObservation(observation_id="discovery", source="fixture_discovery",
        job=Job(source="fixture_discovery", title=TITLE, company="Unfamiliar Example", url=URL),
        original_url=URL, normalized_url=URL, source_kind=SourceKind.ORIGINAL_EMPLOYER,
        content_state="snippet_only", identity_state="uncertain")
    description = "" if mode == "fallback" else html_to_text(text)
    payload = {"@type": "JobPosting", "url": URL, "title": TITLE,
        "hiringOrganization": {"name": "Unfamiliar Example"}, "description": description,
        "jobLocationType": "TELECOMMUTE", "applicantLocationRequirements": {"name": "India"}}
    body = "<h1>"+TITLE+"</h1><script type='application/ld+json'>"+json.dumps(payload)+"</script><main>"+(visible if visible is not None else text)+"</main>"
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as client:
            return await hydrate_public_posting(URL, parent.job, client=client,
                budget=RetrievalBudget(RetrievalPolicy(max_retries=0)))
    outcome = asyncio.run(run())
    child = child_observation(parent, outcome)
    result = evaluate_observations([parent, child], as_of=datetime.now(timezone.utc))
    assert result.accounting_ok
    return outcome, child, result


@pytest.mark.parametrize("mode", ["plain", "fallback"])
def test_visible_form_choices_cannot_supply_language_pay_or_closure(mode):
    baseline, _, before = hydrate(ROLE, mode)
    changed, child, after = hydrate(ROLE+FORM_HTML, mode)
    assert baseline.state == changed.state == child.content_state == "complete"
    assert changed.job.description == baseline.job.description
    assert child.vacancy_state != "explicitly_closed"
    assert "_closure_evidence" not in child.raw_payload
    item = after.evaluated[0]
    assert not any("lang_mismatch:dutch" in reason for reason in item.assessment.blockers)
    assert item.assessment.blockers == before.evaluated[0].assessment.blockers
    assert not any("300" in candidate.raw for candidate in item.assessment.pay_candidates)
    assert all("Bengali" not in claim.span for claim in item.assessment.requirements_assessment.languages)
    if mode == "plain":
        assert "Bengali" in changed.payload["description"]


@pytest.mark.parametrize("mode", ["plain", "fallback"])
def test_unclosed_visible_form_boundary_stays_partial(mode):
    outcome, child, result = hydrate(ROLE+FORM_HTML.replace("<p>Submit application</p>", ""), mode)
    assert outcome.state == child.content_state == "partial"
    assert outcome.job is None
    assert all(item.decision.action_band.value != "primary" for item in result.evaluated)


@pytest.mark.parametrize("mode", ["plain", "fallback"])
def test_genuine_role_requirements_after_explicit_form_end_are_retained(mode):
    text = ROLE+FORM_HTML+"<h2>Requirements</h2><p>Dutch fluency required.</p>"
    outcome, _, result = hydrate(text, mode)
    assert outcome.state == "complete"
    assert "Dutch fluency required." in outcome.job.description
    assert "300" not in outcome.job.description
    assert any("lang_mismatch:dutch" in reason for reason in result.evaluated[0].assessment.blockers)


def test_inline_application_form_narrative_is_genuine_prose():
    outcome, child, _ = hydrate(ROLE+"<p>Review the application form and write Bengali explanations.</p>", "plain")
    assert outcome.state == child.content_state == "complete"
    assert "application form and write Bengali" in outcome.job.description


def test_visible_inline_wrapped_heading_and_submit_preserve_boundaries():
    wrapped = FORM_HTML.replace("<h2>Application form</h2>", "<h2><span>Application form</span></h2>")
    wrapped = wrapped.replace("<p>Submit application</p>", "<p><strong>Submit application</strong></p>")
    baseline, _, _ = hydrate(ROLE, "fallback")
    changed, _, _ = hydrate(ROLE+wrapped, "fallback")
    assert changed.state == "complete"
    assert changed.job.description == baseline.job.description


def test_external_form_heading_uses_reliably_closed_semantic_form_boundary():
    semantic = '<h2>Application form</h2><form><select><option>Bengali</option><option>Dutch fluency required.</option></select><button>Submit application</button></form>'
    baseline, _, _ = hydrate(ROLE, "fallback")
    changed, child, _ = hydrate(ROLE+semantic, "fallback")
    assert changed.state == child.content_state == "complete"
    assert changed.job.description == baseline.job.description


@pytest.mark.parametrize("heading", ["<h2>Application form</h2>", "<p>Application questions</p>"])
def test_nested_or_duplicate_visible_form_headings_are_uncertain(heading):
    outcome, child, _ = hydrate(ROLE+'<h2>Application form</h2>'+heading+'<p>Submit application</p>', "fallback")
    assert outcome.state == child.content_state == "partial"


def test_provider_comment_cannot_forge_semantic_form_end():
    outcome, child, _ = hydrate(ROLE+'<h2>Application form</h2><!--jobhound:closed-semantic-form--><p>Bengali</p>', "fallback")
    assert outcome.state == child.content_state == "partial"


def test_external_heading_and_aria_form_have_reliable_outer_boundary():
    aria = '<h2>Application form</h2><div role="form"><div role="listbox"><p>Dutch fluency required.</p><p>Bengali</p></div><button>Submit application</button></div>'
    baseline, _, _ = hydrate(ROLE, "fallback")
    changed, child, _ = hydrate(ROLE+aria, "fallback")
    assert changed.state == child.content_state == "complete"
    assert changed.job.description == baseline.job.description


def test_unclosed_aria_form_keeps_hold():
    outcome, child, _ = hydrate(ROLE+'<h2>Application form</h2><div role="form"><p>Bengali</p><button>Submit application</button>', "fallback")
    assert outcome.state != "complete" and child.content_state != "complete"


def test_genuine_closure_after_form_end_is_retained():
    outcome, child, _ = hydrate(ROLE+FORM_HTML+"<p>This project has been completed.</p>", "fallback")
    assert outcome.state == "complete"
    assert child.vacancy_state == "explicitly_closed"
    assert outcome.payload["_closure_evidence"]["text"] == "This project has been completed."


def test_feature_off_preserves_legacy_plain_form_text(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", False)
    outcome, _, _ = hydrate(ROLE+FORM_HTML, "plain")
    assert outcome.state == "complete"
    assert "Bengali" in outcome.job.description and "300" in outcome.job.description
