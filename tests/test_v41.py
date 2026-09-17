from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from jobhound.config import CONFIG
from jobhound.v41.digest import build_digest
from jobhound.v41.engine import decision_fingerprint, evaluate_raw
from jobhound.v41.models import ActionBand, MatchStrength, SourceKind
from jobhound.v41.replay import load_snapshot, replay, write_snapshot
from jobhound.v41.provenance import public_url, sanitize_url
from jobhound.v41.resolve import hydrate_ats_url, resolve_url
from jobhound.v41.store import V41Store
from jobhound.sources.base import Source


def _requirements(extra: str = "") -> str:
    return (
        "About the Role. This contractor evaluates AI outputs and produces "
        "careful written feedback for model quality. What You'll Do review "
        "responses, annotate errors, and follow detailed task instructions. "
        "Requirements excellent written English, attention to detail, reliable "
        "internet, and ability to work independently. "
        + extra
        + " Benefits flexible project scheduling and remote collaboration."
    )


def _jsearch(
    title: str,
    company: str,
    url: str,
    description: str | None = None,
    *,
    remote: bool = True,
    city: str | None = None,
    country: str = "India",
    salary: str | None = None,
) -> dict:
    return {
        "source": "jsearch",
        "raw": {
            "job_title": title,
            "employer_name": company,
            "job_apply_link": url,
            "job_description": description or _requirements(),
            "job_is_remote": remote,
            "job_city": city,
            "job_country": country,
            "job_salary_string": salary,
            "job_posted_at_datetime_utc": "2026-07-30T06:00:00Z",
        },
    }


def _greenhouse(
    title: str,
    company: str,
    description: str | None = None,
    *,
    location: str = "Remote",
) -> dict:
    slug = company.lower().replace(" ", "-")
    return {
        "source": "greenhouse",
        "raw": {
            "title": title,
            "_company": company,
            "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/12345",
            "content": description or _requirements(),
            "location": {"name": location},
            "first_published": "2026-07-30T06:00:00Z",
        },
    }


def _adzuna(title: str, company: str, description: str | None = None) -> dict:
    return {
        "source": "adzuna",
        "raw": {
            "title": title,
            "company": {"display_name": company},
            "redirect_url": "https://www.adzuna.in/details/5809895702?utm_source=fake",
            "description": description or _requirements(),
            "location": {"display_name": "Remote"},
            "created": "2026-07-30T06:00:00Z",
        },
    }


def _arbeitnow(
    title: str,
    company: str,
    description: str,
    *,
    location: str,
    remote: bool,
) -> dict:
    return {
        "source": "arbeitnow",
        "raw": {
            "title": title,
            "company_name": company,
            "url": f"https://www.arbeitnow.com/jobs/{company.lower()}-123",
            "description": description,
            "location": location,
            "remote": remote,
            "created_at": 1785391200,
        },
    }


def _item(result, title_prefix: str):
    return next(
        row for row in result.evaluated
        if row.job.title.startswith(title_prefix)
    )


def test_farm_only_is_source_quality_reject():
    raw = [_jsearch(
        "Remote Data Annotation positions",
        "vmysmartpros",
        "https://mysmartpros.com/tuition/job/remote-data-annotation",
        _requirements("AI training RLHF SFT annotation " * 20),
    )]
    item = _item(evaluate_raw(raw), "Remote Data")
    assert item.decision.terminal_reason == "reject_source_quality"
    assert item.assessment.source_actionability == SourceKind.CONTENT_FARM


def test_canonical_original_beats_farm_copy_without_taint():
    title = "Bengali AI Data Evaluator"
    farm = _jsearch(
        title,
        "Example Labs",
        "https://bebee.com/in/jobs/example",
        _requirements("Bengali annotation and model evaluation."),
    )
    ats = _greenhouse(
        title,
        "Example Labs",
        _requirements("Bengali annotation and model evaluation."),
    )
    result = evaluate_raw([farm, ats])
    assert result.counts["canonical_jobs"] == 1
    assert result.counts["duplicate_observations_collapsed"] == 1
    item = result.evaluated[0]
    assert len(item.canonical.observations) == 2
    assert item.assessment.source_actionability == SourceKind.ORIGINAL_ATS
    assert item.decision.action_band == ActionBand.PRIMARY
    assert "greenhouse.io" in item.job.url


def test_project_chiron_is_strong_verify_not_lost():
    result = evaluate_raw([_adzuna(
        "Project Chiron - Bengali Data Trainer",
        "Welo Global",
        _requirements("Bengali language data annotation and image labeling."),
    )])
    item = result.evaluated[0]
    assert item.assessment.match_strength == MatchStrength.STRONG
    assert item.decision.action_band == ActionBand.VERIFY
    assert item.decision.terminal_reason == "surface_verify"


def test_ngspice_and_neuroscience_are_credential_rejects():
    raw = [
        _jsearch(
            "NgSpice Electronics Simulation Reviewer",
            "OpenTrain AI",
            "https://opentrain.ai/jobs/ngspice",
            _requirements("Electrical engineering and circuit simulation required."),
        ),
        _jsearch(
            "Neuroscience Specialist (AI Training Project)",
            "Gramian",
            "https://apply.workable.com/gramian/j/abc",
            _requirements("PhD in neuroscience required."),
        ),
    ]
    result = evaluate_raw(raw)
    assert _item(result, "NgSpice").decision.terminal_reason == "reject_credentials"
    assert _item(result, "Neuroscience").decision.terminal_reason == "reject_credentials"


def test_leadership_and_senior_are_seniority_rejects():
    raw = [
        _jsearch(
            "LLM Training Delivery Leader",
            "OpenTrain AI",
            "https://opentrain.ai/jobs/leader",
            _requirements("Lead a large delivery team and own program outcomes."),
        ),
        _jsearch(
            "Senior Data Scientist",
            "Makersite",
            "https://makersite.io/jobs/senior-data-scientist",
            _requirements("Build Python data pipelines."),
        ),
    ]
    result = evaluate_raw(raw)
    assert _item(result, "LLM Training").decision.terminal_reason == "reject_seniority"
    assert _item(result, "Senior Data").decision.terminal_reason == "reject_seniority"


def test_lead_generation_is_not_misread_as_management():
    result = evaluate_raw([_greenhouse(
        "Lead Generation Data Specialist",
        "Example Sales Data",
        _requirements("Use Python automation for lead generation research."),
    )])
    item = result.evaluated[0]
    assert item.assessment.management_scope is False
    assert item.decision.terminal_reason != "reject_seniority"


def test_upwork_workflow_automation_surfaces_without_direct_scraping():
    result = evaluate_raw([{
        "source": "serp",
        "raw": {
            "title": "n8n Automation Expert for WhatsApp Webhooks - Remote",
            "snippet": (
                "Build an n8n workflow, connect our CRM, and implement a "
                "WhatsApp Business API webhook. Fixed-price freelance project."
            ),
            "link": (
                "https://www.upwork.com/freelance-jobs/apply/"
                "n8n-WhatsApp-Webhook-Automation_~0123456789/"
            ),
        },
    }])
    item = result.evaluated[0]
    assert item.job.company == "Upwork Client"
    assert item.job.platform_key == "upwork"
    assert item.job.platform_trust is not None
    assert item.assessment.source_actionability == SourceKind.REPUTABLE_BOARD
    assert "workflow_automation" in item.assessment.matched_concepts
    assert item.decision.action_band == ActionBand.VERIFY


def test_incidental_whatsapp_support_is_not_workflow_automation():
    result = evaluate_raw([{
        "source": "serp",
        "raw": {
            "title": "WhatsApp Customer Support Agent - Remote",
            "snippet": "Answer customer messages in WhatsApp and update tickets.",
            "link": (
                "https://www.upwork.com/freelance-jobs/apply/"
                "Customer-Support-Agent_~9876543210/"
            ),
        },
    }])
    item = result.evaluated[0]
    assert "workflow_automation" not in item.assessment.matched_concepts
    assert item.decision.action_band == ActionBand.REJECT


def test_workflow_discovery_seeds_cover_upwork_and_adzuna():
    assert any(
        "upwork.com" in query and "n8n" in query
        for query in CONFIG.sources.serp.dorks
    )
    assert "n8n automation" in CONFIG.sources.adzuna.searches
    assert CONFIG.email_ingest.sender_platform_map["upwork.com"] == "upwork"


def test_mojibake_repairs_before_source_rejection():
    result = evaluate_raw([_jsearch(
        "AI Training Contributor â€“ Remote",
        "The Elite Job",
        "https://www.theelitejob.com/job/18974/example",
        _requirements(),
    )])
    item = result.evaluated[0]
    assert "â" not in item.job.title
    assert "–" in item.job.title
    assert item.decision.terminal_reason == "reject_source_quality"


def test_repeated_synonyms_cannot_raise_match_band():
    one = _greenhouse(
        "AI Evaluation Contractor",
        "One Signal Labs",
        _requirements("AI training work."),
    )
    repeated = _greenhouse(
        "AI Evaluation Contractor",
        "Repeated Signal Labs",
        _requirements(("AI training RLHF SFT DPO model evaluation " * 20)),
    )
    result = evaluate_raw([one, repeated])
    first = _item(result, "AI Evaluation").assessment.match_strength
    rows = [row for row in result.evaluated if row.job.title == "AI Evaluation Contractor"]
    assert len(rows) == 2
    assert rows[0].assessment.matched_concepts.count("ai_evaluation") == 1
    assert rows[1].assessment.matched_concepts.count("ai_evaluation") == 1
    assert all(row.assessment.match_strength == first for row in rows)


def test_unknown_pay_reviewer_is_not_automatically_easy_entry():
    result = evaluate_raw([_greenhouse(
        "AI Content Reviewer",
        "Review Labs",
        _requirements(),
    )])
    item = result.evaluated[0]
    assert item.assessment.easy_entry is False
    assert item.assessment.economics_band.value == "unknown"


def test_global_challenges_do_not_create_worldwide_remote():
    description = (
        "About the Role. This engineer solves global challenges in medical "
        "imaging. What You'll Do evaluate AI research outputs and build data "
        "pipelines. Requirements Python, medical-imaging experience, and the "
        "ability to work from the Düsseldorf office. Find jobs in Germany."
    )
    result = evaluate_raw([_arbeitnow(
        "AI Research Engineer",
        "Nano GmbH",
        description,
        location="Düsseldorf",
        remote=False,
    )])
    item = result.evaluated[0]
    assert item.job.is_remote is False
    assert "worldwide" not in item.job.region_tags
    assert item.decision.terminal_reason == "reject_location"


def test_eu_remote_lock_overrides_remote_flag():
    description = (
        "Location: EU (Remote). Work from anywhere in the EU. "
        + _requirements("Python data automation.")
    )
    result = evaluate_raw([_arbeitnow(
        "Data Automation Specialist",
        "Makersite",
        description,
        location="Remote",
        remote=True,
    )])
    assert result.evaluated[0].decision.terminal_reason == "reject_location"


def test_direct_ats_high_pay_is_not_fraud_signal():
    result = evaluate_raw([_greenhouse(
        "AI Evaluation Contractor | $120-$170/hr",
        "Verified Research Labs",
        _requirements("AI model evaluation and detailed written feedback."),
    )])
    item = result.evaluated[0]
    assert item.assessment.selected_pay is not None
    assert item.assessment.selected_pay.min_hourly_usd == 120
    assert item.decision.terminal_reason == "surface_primary"
    assert item.job.scam_score < 0.6


def test_description_pay_candidate_and_conflict_route_to_verify():
    title = "Bengali AI Evaluator | $30/hr"
    ats = _greenhouse(
        title,
        "Conflict Labs",
        _requirements("Compensation: $30 per hour. Bengali annotation."),
    )
    board = _jsearch(
        title,
        "Conflict Labs",
        "https://boards.greenhouse.io/conflict-labs/jobs/12345",
        _requirements("Pay rate: $10 per hour. Bengali annotation."),
        salary="$10/hr",
    )
    result = evaluate_raw([ats, board])
    item = result.evaluated[0]
    assert item.assessment.pay_conflict is True
    assert {candidate.source_field for candidate in item.assessment.pay_candidates} >= {
        "structured", "title", "description"
    }
    assert item.decision.action_band == ActionBand.VERIFY


def test_ledger_equations_and_digest_accounting():
    result = evaluate_raw([
        _greenhouse("Bengali AI Evaluator", "Good Labs", _requirements("Bengali annotation.")),
        _jsearch(
            "Remote Data Annotation positions",
            "vmysmartpros",
            "https://mysmartpros.com/tuition/job/example",
        ),
    ])
    assert result.accounting_ok
    assert result.counts["raw_observations"] == (
        result.counts["normalized_observations"]
        + result.counts["normalization_failures"]
    )
    assert result.counts["normalized_observations"] == (
        result.counts["canonical_jobs"]
        + result.counts["duplicate_observations_collapsed"]
    )
    digest = build_digest(result, include_all=True, min_priority=0)
    assert digest.accounting_ok
    assert digest.display_counts["surface_total"] == sum(
        digest.display_counts[key] for key in (
            "displayed", "hidden_company_cap", "hidden_digest_cap",
            "hidden_priority_floor", "suppressed_by_notification_policy",
        )
    )
    assert "fit 100" not in digest.text
    assert "Decision ledger: PASS" in digest.text


def test_snapshot_replay_is_deterministic_and_sanitized(tmp_path):
    raw = [_adzuna(
        "Project Chiron - Bengali Data Trainer",
        "Welo Global",
        _requirements("Bengali annotation."),
    )]
    raw[0]["raw"]["redirect_url"] += "&app_key=fake-secret-value"
    as_of = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
    first = evaluate_raw(raw, as_of=as_of)
    path = write_snapshot(raw, first, directory=tmp_path)
    header, records = load_snapshot(path)
    assert header["as_of"] == as_of.isoformat()
    assert "fake-secret-value" not in path.read_text(encoding="utf-8")
    assert records
    second = replay(path)
    assert decision_fingerprint(first) == decision_fingerprint(second)
    assert first.counts == second.counts


def test_notification_transition_new_then_promoted_once(tmp_path):
    path = tmp_path / "v41.db"
    store = V41Store(path)
    first = evaluate_raw(
        [_adzuna(
            "Bengali AI Evaluator",
            "Transition Labs",
            _requirements("Bengali annotation."),
        )],
        as_of=datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc),
    )
    first_items = store.annotate_transitions(first)
    assert first_items[0].decision.notification_transition == "new"
    store.record_run(first)
    store.mark_notified(first.metadata.run_id, first_items, "email", True)

    second = evaluate_raw(
        [_greenhouse(
            "Bengali AI Evaluator",
            "Transition Labs",
            _requirements("Bengali annotation."),
        )],
        as_of=datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc),
    )
    second_items = store.annotate_transitions(second)
    assert second_items[0].decision.notification_transition == "promoted"
    store.record_run(second)
    store.mark_notified(second.metadata.run_id, second_items, "email", True)

    third = evaluate_raw(
        [_greenhouse(
            "Bengali AI Evaluator",
            "Transition Labs",
            _requirements("Bengali annotation."),
        )],
        as_of=datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc),
    )
    assert store.annotate_transitions(third) == []
    store.close()


def test_display_floor_is_accounted_not_a_terminal_reject():
    result = evaluate_raw([_greenhouse(
        "AI Content Reviewer",
        "Floor Labs",
        _requirements(),
    )])
    assert result.evaluated[0].decision.action_band == ActionBand.PRIMARY
    digest = build_digest(result, include_all=True, min_priority=10_000)
    assert digest.display_counts["hidden_priority_floor"] == 1
    assert digest.display_counts["displayed"] == 0
    assert digest.accounting_ok


def test_synthetic_golden_rows_meet_v41_outcomes():
    fixture_path = Path(__file__).parent / "fixtures" / "ranking_regressions.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    raw: list[dict] = []
    for row in payload["jobs"]:
        if row["source"] == "adzuna":
            record = _adzuna(row["title"], row["company"], row["description"])
            record["raw"]["location"] = {"display_name": row.get("location") or "Remote"}
            record["raw"]["created"] = row.get("posted_at")
        else:
            record = _jsearch(
                row["title"],
                row.get("company") or "Unknown",
                row["url"],
                row["description"],
                remote=bool(row.get("is_remote")),
                city=row.get("location"),
                country="India",
                salary=row.get("pay_raw"),
            )
            record["raw"]["job_posted_at_datetime_utc"] = row.get("posted_at")
        raw.append(record)

    result = evaluate_raw(
        raw,
        as_of=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
    )
    by_company_title = {
        ((item.job.company or "").casefold(), item.job.title.casefold()): item
        for item in result.evaluated
    }

    def find(company: str, title_prefix: str):
        return next(
            item for (candidate_company, title), item in by_company_title.items()
            if candidate_company == company.casefold()
            and title.startswith(title_prefix.casefold())
        )

    assert find("vmysmartpros", "Remote Data").decision.terminal_reason == "reject_source_quality"
    assert find("vmysmartpros", "Data Annotation").decision.terminal_reason == "reject_source_quality"
    chiron = find("Welo Global", "Project Chiron")
    assert chiron.assessment.match_strength == MatchStrength.STRONG
    assert chiron.decision.action_band == ActionBand.VERIFY
    assert find("OpenTrain AI", "NgSpice").decision.terminal_reason == "reject_credentials"
    assert find("Gramian Consulting Group", "Neuroscience").decision.terminal_reason == "reject_credentials"
    delivery = find("OpenTrain AI", "LLM Training Delivery")
    # V5 extracts the explicit 8+ year requirement. Credential/experience
    # blockers precede seniority by the documented terminal-order policy.
    assert delivery.decision.terminal_reason == "reject_credentials"
    assert "credentials:experience_mismatch:8_years" in delivery.assessment.blockers
    humanitapp = find("HumaniTapp", "Data Science Experts")
    assert humanitapp.decision.terminal_reason == "reject_source_quality"
    assert humanitapp.assessment.selected_pay is not None
    assert humanitapp.assessment.selected_pay.min_hourly_usd == 120
    elite = find("The Elite Job", "AI Training Contributor")
    assert elite.decision.terminal_reason == "reject_source_quality"
    assert "â" not in elite.job.title


def test_url_resolver_is_bounded_read_only_and_sanitized():
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        if request.url.host == "jobs.example":
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://boards.greenhouse.io/example/jobs/123"
                        "?utm_source=x&app_key=fake-secret"
                    )
                },
            )
        return httpx.Response(200)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            resolved = await resolve_url(
                "https://jobs.example/start?token=fake-secret",
                client=client,
            )
            blocked = await resolve_url("http://127.0.0.1/secret", client=client)
            return resolved, blocked

    resolved, blocked = asyncio.run(exercise())
    assert resolved.final == "https://boards.greenhouse.io/example/jobs/123"
    assert len(resolved.trail) == 2
    assert resolved.error is None
    assert blocked.error == "unsafe_or_invalid_url"
    assert set(seen_methods) == {"HEAD"}


def test_incidental_body_vocabulary_does_not_define_the_role():
    """Regression from the 2026-07-30 full-source V4.1 preview."""
    raw = [
        _arbeitnow(
            "Contract Business Analyst",
            "YLD.com",
            (
                "About the Role. Translate business needs into project plans. "
                "Requirements. Interview with our senior developers and work "
                "inside a software consultancy."
            ),
            location="Remote",
            remote=True,
        ),
        _greenhouse(
            "Strategic Account Executive",
            "Prolific",
            (
                "About the Role. Sell our model evaluation platform. "
                "Requirements. Experience managing technical sales cycles."
            ),
        ),
        _greenhouse(
            "Security Engineer, Cloud Infrastructure",
            "Mercor",
            (
                "About the Role. Secure cloud infrastructure. Requirements. "
                "Automate code review and improve deployment speed."
            ),
        ),
        _greenhouse(
            "Transportation Analyst",
            "RemoteFront",
            (
                "About the Role. Plan freight routes. Requirements. Experience "
                "with transportation planning software and rate review."
            ),
        ),
    ]
    result = evaluate_raw(raw)
    for item in result.evaluated:
        assert item.assessment.match_strength == MatchStrength.WEAK
        assert item.decision.action_band == ActionBand.REJECT
        assert item.decision.terminal_reason in {
            "reject_credentials", "reject_role", "reject_low_evidence"
        }


def test_description_pay_expression_does_not_swallow_401k():
    result = evaluate_raw([_greenhouse(
        "Data Labeling Specialist",
        "Workada",
        (
            _requirements()
            + " Compensation. Salary Range: $56,500 - $73,400 annual. "
            "Bonus eligibility, paid time off, and 401k company match."
        ),
    )])
    item = result.evaluated[0]
    selected = item.assessment.selected_pay
    assert selected is not None
    assert selected.basis == "year"
    # Preserve the annual claim without assuming 2,080 hours of labor.
    assert selected.amount_low == 56500
    assert selected.amount_high == 73400
    assert selected.min_hourly_usd is None
    assert selected.max_hourly_usd is None
    assert "401k" not in selected.raw.casefold()


def test_explicit_non_india_remote_location_is_rejected():
    result = evaluate_raw([_greenhouse(
        "AI Content Reviewer - United States",
        "TELUS Digital",
        _requirements(),
        location="United States",
    )])
    assert result.evaluated[0].decision.terminal_reason == "reject_location"


def test_translation_pair_requires_both_languages():
    result = evaluate_raw([
        _greenhouse(
            "Machine Translation Evaluation - English <> German",
            "Appen",
            _requirements("Evaluate translated text in both languages."),
        ),
        _greenhouse(
            "Machine Translation Evaluation - English to Spanish",
            "Appen Two",
            _requirements("Evaluate translated text in both languages."),
        ),
    ])
    for item, language in zip(
        sorted(result.evaluated, key=lambda row: row.job.company),
        ("german", "spanish"),
    ):
        assert item.assessment.language_requirements == ["english", language]
        assert item.decision.terminal_reason == "reject_language"


def test_advanced_research_and_security_titles_are_profile_gated():
    result = evaluate_raw([
        _greenhouse(
            "Research Engineer",
            "Turing",
            _requirements("4-5 years of experience building AI data systems."),
        ),
        _greenhouse(
            "Security Engineer, Cloud Infrastructure",
            "Mercor",
            _requirements("Automate security reviews."),
        ),
    ])
    assert _item(result, "Research Engineer").decision.terminal_reason == "reject_credentials"
    assert _item(result, "Security Engineer").decision.terminal_reason == "reject_credentials"


def test_security_clearance_is_a_profile_credential_gate():
    result = evaluate_raw([_greenhouse(
        "Applied AI Engineer - Federal (TS Required)",
        "Snorkel AI",
        _requirements("Python automation and active security clearance."),
    )])
    assert result.evaluated[0].decision.terminal_reason == "reject_credentials"


def test_multilingual_boilerplate_does_not_create_language_edge():
    result = evaluate_raw([_greenhouse(
        "Image & Video Annotator",
        "Example Labs",
        (
            "About the Role. Label images and video frames. What You'll Do. "
            "Draw bounding boxes. Requirements. Work with multilingual "
            "datasets and follow written instructions."
        ),
    )])
    item = result.evaluated[0]
    assert "language_ai" not in item.assessment.matched_concepts
    assert item.assessment.match_strength == MatchStrength.MEDIUM


def test_generic_software_only_role_is_rejected():
    result = evaluate_raw([_greenhouse(
        "Software Engineer",
        "Turing",
        (
            "About the Role. Build production software. What You'll Do. "
            "Implement APIs. Requirements. Python and JavaScript."
        ),
    )])
    item = result.evaluated[0]
    assert "role_mismatch" in item.assessment.blockers
    assert item.decision.action_band == ActionBand.REJECT
    assert item.decision.terminal_reason == "reject_role"


def test_explicit_experience_requirement_uses_profile_limit():
    result = evaluate_raw([_greenhouse(
        "AI Data Quality Specialist",
        "Example Labs",
        _requirements("Requires 4+ years of relevant experience."),
    )])
    item = result.evaluated[0]
    assert item.decision.terminal_reason == "reject_credentials"
    assert any(
        "experience_mismatch:4_years" in reason
        for reason in item.assessment.blockers
    )


def test_sidecar_additively_migrates_early_observation_schema(tmp_path):
    path = tmp_path / "early-v41.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE observations (
            run_id TEXT NOT NULL,
            observation_id TEXT NOT NULL,
            source TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            publisher_domain TEXT,
            apply_domain TEXT,
            normalized_url TEXT,
            normalization_error TEXT,
            raw_payload_json TEXT NOT NULL,
            normalized_job_json TEXT,
            PRIMARY KEY (run_id, observation_id)
        )
        """
    )
    connection.commit()
    connection.close()

    store = V41Store(path)
    columns = {
        row["name"]
        for row in store.conn.execute("PRAGMA table_info(observations)")
    }
    version = store.conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()["value"]
    store.close()
    assert "resolution_json" in columns
    assert version == "4"


def test_source_failure_warning_keeps_blank_timeout_type(caplog):
    class TimeoutSource(Source):
        name = "timeout_fixture"

        @property
        def enabled(self):
            return True

        async def _fetch(self, client):
            raise httpx.ReadTimeout("")

    async def exercise():
        async with httpx.AsyncClient() as client:
            return await TimeoutSource().fetch(client)

    with caplog.at_level("WARNING", logger="jobhound.sources"):
        assert asyncio.run(exercise()) == []
    assert "timeout_fixture failed: ReadTimeout" in caplog.text


def test_adzuna_interstitial_resolution_preserves_signed_click_parameters():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "www.adzuna.in" and request.url.path == "/details/42":
            if request.method == "HEAD":
                return httpx.Response(200, request=request)
            return httpx.Response(
                200,
                request=request,
                text=(
                    '<a href="/land/ad/42?se=signed-click&v=proof'
                    '&utm_source=fake-app-id">Apply</a>'
                ),
            )
        if request.url.host == "www.adzuna.in" and request.url.path == "/land/ad/42":
            assert request.url.params["se"] == "signed-click"
            assert request.url.params["v"] == "proof"
            assert "utm_source" not in request.url.params
            return httpx.Response(
                302,
                request=request,
                headers={
                    "location": (
                        "https://jobs.ashbyhq.com/lilt-production/posting-id"
                        "?utm_campaign=adzuna"
                    )
                },
            )
        return httpx.Response(200, request=request)

    async def exercise():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await resolve_url(
                "https://www.adzuna.in/details/42?utm_source=fake-app-id",
                client=client,
            )

    resolution = asyncio.run(exercise())
    assert resolution.error is None
    assert resolution.final == (
        "https://jobs.ashbyhq.com/lilt-production/posting-id"
    )
    assert any("se=signed-click" in item for item in resolution.trail)
    assert all("fake-app-id" not in item for item in resolution.trail)
    assert seen


def test_adzuna_observation_keeps_private_resolution_url_not_public_query():
    raw = _adzuna(
        "Bengali AI Evaluator",
        "Example Labs",
        _requirements("Bengali annotation."),
    )
    raw["raw"]["redirect_url"] = (
        "https://www.adzuna.in/land/ad/42?se=signed-click&v=proof"
        "&utm_source=fake-app-id&app_key=fake-secret"
    )
    result = evaluate_raw([raw])
    observation = result.observations[0]
    assert observation.job is not None
    assert observation.job.url == "https://www.adzuna.in/land/ad/42"
    resolution_url = sanitize_url(observation.original_url)
    assert "se=signed-click" in resolution_url
    assert "v=proof" in resolution_url
    assert "app_key" not in resolution_url
    assert "utm_source" not in resolution_url


def test_adzuna_signed_click_parameters_never_render_publicly():
    private = (
        "https://www.adzuna.in/land/ad/42?se=signed-click&v=proof"
        "&search=data+annotation"
    )
    assert public_url(private) == (
        "https://www.adzuna.in/land/ad/42?search=data+annotation"
    )

    result = evaluate_raw([_adzuna(
        "Bengali AI Evaluator",
        "Example Labs",
        _requirements("Bengali annotation."),
    )])
    item = result.evaluated[0]
    item.job.url = private
    digest = build_digest(result, include_all=True, min_priority=0)
    assert "signed-click" not in digest.text
    assert "v=proof" not in digest.text
    assert "search=data+annotation" in digest.text


def test_resolver_records_http_rate_limit_as_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, text="Too Many Requests")

    async def exercise():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await resolve_url(
                "https://www.adzuna.in/land/ad/42?se=signed&v=proof",
                client=client,
            )

    resolution = asyncio.run(exercise())
    assert resolution.error == "http_429"
    assert resolution.final.startswith("https://www.adzuna.in/land/ad/42")


def test_ashby_hydration_recovers_company_and_full_requirements():
    posting_url = "https://jobs.ashbyhq.com/lilt-production/posting-id"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.ashbyhq.com"
        assert request.url.params["includeCompensation"] == "true"
        return httpx.Response(
            200,
            request=request,
            json={
                "jobs": [{
                    "id": "posting-id",
                    "title": "AI Training Contributor - Bengali (India) - Remote",
                    "jobUrl": posting_url,
                    "applyUrl": posting_url,
                    "location": "India",
                    "secondaryLocations": [],
                    "isRemote": True,
                    "isListed": True,
                    "publishedAt": "2026-07-30T06:00:00Z",
                    "descriptionPlain": _requirements(
                        "Native Bengali and strong written English required. "
                        "Experience as an AI reviewer is preferred."
                    ),
                    "compensation": {},
                }]
            },
        )

    async def exercise():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await hydrate_ats_url(posting_url, client=client)

    hydration = asyncio.run(exercise())
    assert hydration.error is None
    assert hydration.source == "ashby"
    assert hydration.job is not None
    assert hydration.job.company == "LILT"
    assert "Native Bengali" in hydration.job.description
    assert hydration.job.url == posting_url


def test_direct_lilt_beats_unverified_crossing_hurdles_pay_claim():
    lilt = {
        "source": "serp",
        "raw": {
            "title": "AI Training Contributor - Bengali (India) - Remote",
            "snippet": (
                "Qualifications · Native fluency in the target language · "
                "Strong command of English · Experience as AI reviewer, "
                "annotator, or evaluator preferred · Comfortable ..."
            ),
            "link": (
                "https://jobs.ashbyhq.com/lilt-production/"
                "70bd32de-e6c4-4999-83b7-2571ec1eee26"
            ),
        },
    }
    crossing = {
        "source": "serp",
        "raw": {
            "title": "AI Trainer (Bengali) | $50/hr Remote",
            "snippet": (
                "Evaluate AI responses as a Bengali evaluator and annotator. "
                "Remote contract opportunity."
            ),
            "link": (
                "https://www.linkedin.com/jobs/view/"
                "ai-trainer-bengali-%2450-hr-remote-at-crossing-hurdles-4377751235"
            ),
        },
    }
    result = evaluate_raw([crossing, lilt])
    lilt_item = _item(result, "AI Training Contributor")
    crossing_item = _item(result, "AI Trainer")

    assert lilt_item.job.company == "LILT"
    assert lilt_item.assessment.eligibility.value == "unknown"
    assert lilt_item.decision.action_band == ActionBand.PRIMARY
    assert crossing_item.job.company == "Crossing Hurdles"
    assert crossing_item.assessment.economics_band.value == "unknown"
    assert crossing_item.job.effective_hourly_usd is None
    assert "pay_unverified_high" in crossing_item.decision.watch_outs
    assert lilt_item.decision.rank_key > crossing_item.decision.rank_key


def test_rws_registry_identity_drives_clustering_and_company_cap():
    duplicate_a = _adzuna(
        "AI Data Specialist - Hindi",
        "RWS",
        _requirements("Hindi language evaluation."),
    )
    duplicate_b = _adzuna(
        "AI Data Specialist - Hindi",
        "RWS Group",
        _requirements("Hindi language evaluation."),
    )
    duplicate_b["raw"]["redirect_url"] = (
        "https://www.adzuna.in/details/2?utm_source=fake"
    )
    clustered = evaluate_raw([duplicate_a, duplicate_b])
    assert clustered.counts["canonical_jobs"] == 1
    assert clustered.evaluated[0].canonical.counterparty_key == (
        "registry:rws_trainai"
    )
    assert len(clustered.evaluated[0].canonical.observations) == 2

    three_roles = [
        _adzuna(
            "Evaluation Specialist - Bengali",
            "RWS Group",
            _requirements("Bengali language evaluation."),
        ),
        _adzuna(
            "AI Data Specialist - Hindi",
            "RWS TrainAI",
            _requirements("Hindi language evaluation."),
        ),
        _adzuna(
            "Speech AI Evaluation Specialist - Hindi (India)",
            "RWS",
            _requirements("Hindi speech evaluation."),
        ),
    ]
    for index, record in enumerate(three_roles, start=10):
        record["raw"]["redirect_url"] = (
            f"https://www.adzuna.in/details/{index}?utm_source=fake"
        )
    result = evaluate_raw(three_roles)
    digest = build_digest(
        result,
        include_all=True,
        min_priority=0,
        top_n=15,
        per_company_cap=2,
    )
    assert digest.display_counts["displayed"] == 2
    assert digest.display_counts["hidden_company_cap"] == 1


def test_unrated_range_has_no_fake_effective_rate_and_uses_lower_bound():
    result = evaluate_raw([_jsearch(
        "Bengali AI Language Evaluator",
        "Unknown Research Cooperative",
        "https://www.linkedin.com/jobs/view/123",
        _requirements("Bengali language evaluation."),
        salary="$8.29-$13.82/hr",
    )])
    item = result.evaluated[0]
    assert item.job.pay_min_hourly_usd == 8.29
    assert item.job.pay_max_hourly_usd == 13.82
    assert item.job.effective_hourly_usd is None
    assert item.assessment.pay_rate_for_ranking == 8.29
    assert "eff ~" not in build_digest(
        result, include_all=True, min_priority=0
    ).text


def test_hydrated_job_survives_sanitized_snapshot_replay(tmp_path):
    raw = [{
        "source": "serp",
        "raw": {
            "title": "AI Training Contributor - Bengali (India) - Remote",
            "snippet": "Bengali AI evaluation opportunity.",
            "link": "https://jobs.ashbyhq.com/lilt-production/posting-id",
        },
    }]
    first = evaluate_raw(raw)
    item = first.evaluated[0]
    observation = item.canonical.best_observation
    observation.hydrated_source = "ashby"
    observation.resolution_attempted = True
    observation.resolution_error = "http_429"
    observation.job.description = _requirements("Native Bengali required.")
    item.canonical.job.description = observation.job.description
    path = write_snapshot(raw, first, directory=tmp_path)
    second = replay(path)
    replayed = second.evaluated[0]
    assert replayed.canonical.best_observation.hydrated_source == "ashby"
    assert replayed.canonical.best_observation.resolution_attempted
    assert replayed.canonical.best_observation.resolution_error == "http_429"
    assert "source_resolution:http_429" in replayed.decision.watch_outs
    assert "Native Bengali required" in replayed.job.description
