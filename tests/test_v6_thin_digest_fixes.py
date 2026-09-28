"""Thin-digest fixes from the 2026-09-28 run audit (run 377d6dc97fa3bb5134b7).

The run surfaced 77 eligible jobs and emailed 2. Among the causes: every
Mercor posting lost its action-cost point because the Upwork "Connects"
check matched Mercor's boilerplate ("Mercor connects elite ... talent"), and
Upwork freelancer profiles were verified as if they were buyer requests.
"""
from datetime import datetime, timezone

from jobhound.models import Job
from jobhound.v41.action_policy import apply_action_policy
from jobhound.v41.documents import classify_document
from jobhound.v41.models import (
    Assessment, CanonicalJob, EligibilityStatus, ListingObservation, SourceKind,
)
from test_v6_delivery_recovery import release, store  # noqa: F401  (pytest fixtures)

NOW = datetime(2026, 9, 28, 12, 30, tzinfo=timezone.utc)
MERCOR = ("About the job Mercor connects elite creative and technical talent with leading "
          "AI research labs. Headquartered in San Francisco. We are hiring Hindi transcription "
          "audit specialists.")


def _cost(description, url="https://www.adzuna.in/details/1", platform_key="mercor"):
    job = Job(source="adzuna", title="Hindi Transcription Audit Specialist", company="Mercor",
              url=url, description=description, is_remote=True, platform_key=platform_key)
    observation = ListingObservation(observation_id="o", source="adzuna", job=job,
                                     source_kind=SourceKind.AGGREGATOR_UNRESOLVED)
    canonical = CanonicalJob(canonical_id="c", job=job, observations=[observation],
                             field_sources={"url": "o", "description": "o"})
    assessment = Assessment(eligibility=EligibilityStatus.PASSED, lifecycle="active")
    apply_action_policy(canonical, assessment, NOW)
    return assessment


def test_company_boilerplate_verb_connects_is_not_an_upwork_bid_cost():
    assessment = _cost(MERCOR)
    assert assessment.action_cost_acceptable is True
    assert not any(t["missing_fact"] == "onboarding_cost" for t in assessment.verification_tasks)


def test_real_connects_costs_and_long_assessments_still_count_as_high_effort():
    for text in ("Submitting a proposal costs 16 Connects.",
                 "This job requires 6 connects to apply.",
                 "Applicants complete a 90 minute assessment before onboarding.",
                 "A paid test is required."):
        assessment = _cost(f"We are hiring Hindi reviewers. {text}")
        assert assessment.action_cost_acceptable is False, text


def test_upwork_freelancer_profiles_are_sellers_not_buyer_requests():
    for url in ("https://www.upwork.com/freelancers/~018d54795fbd8a68a7",
                "https://www.upwork.com/freelancers/~01abc?mp_source=share",
                "https://www.upwork.com/fl/juliansmith"):
        result = classify_document("Julian S. - n8n workflow automation & No-Code/Low- ...",
                                   "Top Rated. n8n, Make, Zapier. 100% job success.", url)
        assert (result.document_type, result.actionable) == ("seller_service", False), url
        assert result.reasons == ("seller_profile_route",)
    directory = classify_document("Find n8n freelancers", "Browse profiles",
                                  "https://www.upwork.com/freelancers")
    assert (directory.document_type, directory.actionable) == ("talent_directory", False)
    buyer = classify_document("N8N Automation Expert Whatsapp API Webhooks", "Need a developer",
                              "https://www.upwork.com/freelance-jobs/apply/n8n_~01/")
    assert (buyer.document_type, buyer.actionable) == ("buyer_request", True)


def _many_eligible():
    from jobhound.v41.engine import evaluate_raw
    from test_v55_action_policy import DESCRIPTION, NOW as FIXTURE_NOW

    def raw(i, company, title):
        return {"source": "ashby", "raw": {
            "id": f"id{i}", "title": title, "_company": company,
            "jobUrl": f"https://jobs.ashbyhq.com/{company.lower()}/id{i}", "location": "India",
            "isRemote": True, "descriptionPlain": DESCRIPTION + "\nCompensation\nPay USD 20 per hour."}}
    records = ([raw(i, "Lilt", f"Bengali AI Response Evaluator {i}") for i in range(9)]
               + [raw(20 + i, f"Co{i}", f"Hindi AI Response Evaluator {i}") for i in range(3)])
    return evaluate_raw(records, as_of=FIXTURE_NOW)


def test_overflow_cards_are_listed_in_the_email_not_silently_dropped(store):
    import json
    from jobhound.delivery_transport import DigestTransport
    from test_v6_delivery_recovery import EMAIL, stage

    value = _many_eligible()
    stage(store, value, card_cap=15, status_cap=5)
    report = json.loads(store.conn.execute("SELECT report FROM delivery_runs").fetchone()[0])
    ledger = report["destinations"][0]
    # 12 eligible: 5 primary cards (2 per employer), 7 Lilt roles overflow.
    assert (ledger["counts"]["selected_cards"], ledger["counts"]["overflow_cards"]) == (5, 7)
    assert len(ledger["overflow"]) == 7
    first = ledger["overflow"][0]
    assert set(first) == {"title", "company", "host", "url", "band", "source"}
    assert first["company"] == "Lilt" and first["host"] == "jobs.ashbyhq.com"

    transport = DigestTransport(store.delivery)
    ids = [r["id"] for r in store.delivery.inspect()]
    body = transport.inspect(transport.prepare(EMAIL, ids, now=101,
                                               summary_run_id=value.metadata.run_id))["body"]
    assert "Also eligible (not carded)" in body
    # Wording revised after review of 0836247 (card-only counts, current states).
    assert ("Jobs this run: 12 eligible; 5 newly queued, 7 listed below, "
            "0 already queued or sent in an earlier run.") in body
    assert "This email carries 5 job cards" in body
    section = body.split("Also eligible (not carded)", 1)[1]
    listed = [line for line in section.splitlines() if line.startswith("• ")]
    assert len(listed) == 5  # at most five per employer
    assert all("Lilt" in line and "https://jobs.ashbyhq.com/lilt/" in line for line in listed)
    assert "+ 2 more from Lilt" in body
    # The status-cap line is about data sources, never jobs.
    assert "source-health audit" in body.lower() and "data sources, not jobs" in body


def _render_with_report(store, edit):
    import json
    from jobhound.delivery_transport import DigestTransport
    from test_v6_delivery_recovery import EMAIL, stage

    value = _many_eligible()
    stage(store, value, card_cap=15, status_cap=5)
    row = store.conn.execute("SELECT rowid, report FROM delivery_runs").fetchone()
    report = json.loads(row[1])
    for destination in report["destinations"]:
        edit(destination)
    store.conn.execute("UPDATE delivery_runs SET report=? WHERE rowid=?", (json.dumps(report), row[0]))
    transport = DigestTransport(store.delivery)
    ids = [r["id"] for r in store.delivery.inspect()]
    return transport.inspect(transport.prepare(EMAIL, ids, now=101,
                                               summary_run_id=value.metadata.run_id))["body"]


def test_older_reports_without_an_overflow_list_still_render(store):
    def pre_0836247(destination):
        for key in ("overflow", "cards", "card_intent_ids"):
            destination.pop(key)
    body = _render_with_report(store, pre_0836247)
    assert "Also eligible" not in body and "Jobs this run" not in body
    assert "7 more eligible jobs are in the local run audit" in body


def test_reports_from_0836247_keep_their_overflow_list(store):
    # Staged with the overflow list but before card accounting.
    def from_0836247(destination):
        for key in ("cards", "card_intent_ids"):
            destination.pop(key)
    body = _render_with_report(store, from_0836247)
    assert "Also eligible (not carded)" in body and "+ 2 more from Lilt" in body
    assert "Jobs this run" not in body
