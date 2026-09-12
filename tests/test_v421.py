"""V4.2.1 regressions from the 2026-08-17 production run."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.filters.listing_quality import location_gate
from jobhound.pipeline import ingest_raw_with_health
from jobhound.sources.adzuna import AdzunaSource
from jobhound.sources.base import Source
from jobhound.v41.digest import build_digest
from jobhound.v41.engine import evaluate_raw
from jobhound.v41.models import ActionBand
from jobhound.v41.replay import replay, write_snapshot


AS_OF = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _requirements(extra: str = "") -> str:
    return (
        "About the Role. Complete remote project work. What You'll Do. "
        "Review outputs and document results. Requirements. Excellent written "
        f"English, attention to detail, and reliable internet. {extra}"
    )


def _jsearch(
    title: str,
    company: str,
    description: str,
    *,
    url: str = "https://www.linkedin.com/jobs/view/421",
    remote: bool = True,
    city: str | None = None,
    country: str | None = "India",
) -> dict:
    return {
        "source": "jsearch",
        "raw": {
            "job_title": title,
            "employer_name": company,
            "job_apply_link": url,
            "job_description": description,
            "job_is_remote": remote,
            "job_city": city,
            "job_country": country,
            "job_posted_at_datetime_utc": "2026-08-16T08:00:00Z",
        },
    }


def _arbeitnow(title: str, location: str, *, description: str = "") -> dict:
    return {
        "source": "arbeitnow",
        "raw": {
            "slug": title.lower().replace(" ", "-"),
            "title": title,
            "company_name": "Fixture Employer",
            "description": description or _requirements(),
            "remote": False,
            "location": location,
            "url": f"https://www.arbeitnow.com/jobs/{title.lower().replace(' ', '-')}",
            "tags": ["worldwide", "remote-first"],
            "job_types": ["full-time"],
            "created_at": "2026-08-16T08:00:00Z",
        },
    }


def _serp(title: str, link: str, snippet: str | None = None) -> dict:
    return {
        "source": "serp",
        "raw": {
            "title": title,
            "link": link,
            "snippet": snippet or _requirements(),
        },
    }


def _configure_adzuna(monkeypatch, *, searches, pages, retries, page_size=1):
    cfg = CONFIG.sources.adzuna
    monkeypatch.setattr(cfg, "searches", searches)
    monkeypatch.setattr(cfg, "max_pages", pages)
    monkeypatch.setattr(cfg, "max_retries", retries)
    monkeypatch.setattr(cfg, "results_per_page", page_size)
    monkeypatch.setattr(cfg, "retry_backoff_seconds", 0.0)
    monkeypatch.setattr(cfg, "request_timeout_seconds", 1.0)
    monkeypatch.setattr(
        AdzunaSource, "enabled", property(lambda self: True)
    )


def test_adzuna_transient_timeout_retries_then_succeeds(monkeypatch):
    _configure_adzuna(monkeypatch, searches=["ai training"], pages=1, retries=1)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(
            200,
            request=request,
            json={"results": [{"id": "recovered"}]},
        )

    async def exercise():
        source = AdzunaSource()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            records = await source.fetch(client)
        return source, records

    source, records = asyncio.run(exercise())
    assert calls == 2
    assert [record["raw"]["id"] for record in records] == ["recovered"]
    assert source.health.status == "ok"
    assert source.health.retries == 1
    assert source.health.successful_requests == 1


def test_adzuna_retains_partial_pages_and_opens_source_circuit(monkeypatch):
    _configure_adzuna(
        monkeypatch,
        searches=["first", "must-not-run"],
        pages=2,
        retries=0,
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                request=request,
                json={"results": [{"id": "kept"}]},
            )
        raise httpx.ReadTimeout("permanent", request=request)

    async def exercise():
        source = AdzunaSource()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            records = await source.fetch(client)
        return source, records

    source, records = asyncio.run(exercise())
    assert calls == 2
    assert [record["raw"]["id"] for record in records] == ["kept"]
    assert source.health.status == "partial"
    assert source.health.item_count == 1
    assert source.health.failed_requests == 1
    assert source.health.error_type == "ReadTimeout"


def test_source_health_is_non_secret_and_reaches_pipeline(monkeypatch):
    class DeadSource(Source):
        name = "dead_fixture"

        @property
        def enabled(self):
            return True

        async def _fetch(self, client):
            raise httpx.ReadTimeout(
                "https://api.example.test?q=x&app_key=fake-secret"
            )

    monkeypatch.setattr(
        "jobhound.pipeline.build_sources",
        lambda limit, only: [DeadSource(limit=limit)],
    )
    records, health = asyncio.run(ingest_raw_with_health(None, None))
    serialized = str(health[0].as_dict())
    assert records == []
    assert health[0].status == "failed"
    assert health[0].error_type == "ReadTimeout"
    assert "fake-secret" not in serialized
    assert "api.example.test" not in serialized


def test_source_health_banner_and_snapshot_roundtrip(tmp_path):
    result = evaluate_raw([_jsearch(
        "Bengali AI Evaluator",
        "Example",
        _requirements("Native Bengali required."),
    )], as_of=AS_OF)
    result.source_health = [{
        "source": "adzuna",
        "status": "failed",
        "item_count": 0,
        "successful_requests": 0,
        "failed_requests": 1,
        "retries": 1,
        "error_type": "ReadTimeout",
    }]
    digest = build_digest(result, include_all=True, min_priority=0)
    assert "Source coverage degraded" in digest.text
    assert "Adzuna: unavailable" in digest.text
    assert digest.operational_alerts
    path = write_snapshot(
        [_jsearch(
            "Bengali AI Evaluator",
            "Example",
            _requirements("Native Bengali required."),
        )],
        result,
        directory=tmp_path,
    )
    restored = replay(path)
    assert restored.source_health == result.source_health


@pytest.mark.parametrize(
    "location",
    ["London", "São Paulo, BR", "Philadelphia, PA", "New York City, NY"],
)
def test_nonremote_foreign_location_beats_worldwide_noise(location):
    item = evaluate_raw([_arbeitnow(
        "Junior Data Engineer",
        location,
        description=(
            "We are a worldwide, remote-first company. Requirements. Build ETL "
            "pipelines and work from the advertised office location."
        ),
    )], as_of=AS_OF).evaluated[0]
    assert "worldwide" in item.job.region_tags
    assert item.decision.terminal_reason == "reject_location"


def test_nonremote_india_location_remains_eligible_for_location_gate():
    assert location_gate(
        False,
        ["worldwide", "india"],
        "Bangalore, India",
        "Worldwide company.",
        title="Data Annotation Specialist",
    ) == []


@pytest.mark.parametrize(
    "title",
    ["QA Tester Entry Level", "Software Engineer (Java)", "Junior Data Engineer"],
)
def test_generic_technical_roles_are_rejected(title):
    item = evaluate_raw([_jsearch(
        title,
        "Example",
        _requirements("Build Python APIs, SQL ETL pipelines, and production software."),
    )], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_role"


def test_ai_training_software_and_deliverable_data_work_still_pass_role_gate():
    records = [
        _jsearch(
            "Software Engineer - AI Training",
            "Alignerr",
            _requirements("Evaluate model code and write Python tasks."),
        ),
        _jsearch(
            "Web Scraping and Data Cleaning Automation",
            "Upwork Client",
            _requirements("Deliver a CSV and automate spreadsheet cleanup."),
            url=(
                "https://www.upwork.com/freelance-jobs/apply/"
                "Web-Scraping-Data-Cleaning_~022087206200827997104/"
            ),
        ),
    ]
    result = evaluate_raw(records, as_of=AS_OF)
    assert all(
        item.decision.action_band != ActionBand.REJECT
        for item in result.evaluated
    )


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("B2B Sales AI Training Expert", "sales_domain"),
        ("Film and Television Production Evaluator", "film_television"),
        (
            "Financial Modeling and Investment AI Training Expert",
            "finance_domain",
        ),
        ("Physical Sciences AI Evaluation Research Assistant", "physical_sciences"),
    ],
)
def test_unclaimed_ai_training_domains_are_credential_gated(title, reason):
    item = evaluate_raw([_jsearch(
        title,
        "OpenTrain AI",
        _requirements(),
    )], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_credentials"
    assert any(
        blocker.startswith(f"credentials:credential_mismatch:{reason}")
        for blocker in item.assessment.blockers
    )


@pytest.mark.parametrize(
    "title",
    [
        "9 Rlhf Jobs and Vacancies in Remote - 16 August 2026",
        "RLHF opportunities available now",
    ],
)
def test_indeed_query_page_is_rejected_as_search_index(title):
    item = evaluate_raw([_serp(
        title,
        "https://in.indeed.com/q-rlhf-l-remote-jobs.html",
    )], as_of=AS_OF).evaluated[0]
    assert item.decision.terminal_reason == "reject_source_quality"


def test_upwork_gigs_have_listing_identity_but_keep_platform_trust():
    base = "https://www.upwork.com/freelance-jobs/apply/"
    records = [
        _jsearch(
            "Make.com WhatsApp API Automation Specialist",
            "Upwork Client",
            _requirements("Build Make.com webhooks and WhatsApp integrations."),
            url=f"{base}Make-WhatsApp_~022087206200827997101/",
        ),
        _jsearch(
            "Make.com WhatsApp API Automation Specialist",
            "Upwork Client",
            _requirements("Build Make.com webhooks and WhatsApp integrations."),
            url=f"{base}Make-WhatsApp_~022087206200827997102/",
        ),
    ]
    result = evaluate_raw(records, as_of=AS_OF)
    assert len(result.evaluated) == 2
    assert len({
        item.canonical.counterparty_key for item in result.evaluated
    }) == 2
    assert {item.job.platform_key for item in result.evaluated} == {"upwork"}
    assert len({item.job.platform_trust for item in result.evaluated}) == 1
    digest = build_digest(
        result,
        include_all=True,
        min_priority=0,
        top_n=5,
        per_company_cap=1,
    )
    assert digest.display_counts["displayed"] == 2
    assert digest.display_counts["hidden_company_cap"] == 0


def test_upwork_tracking_variants_of_same_gig_still_collapse():
    base = (
        "https://www.upwork.com/freelance-jobs/apply/"
        "Make-WhatsApp_~022087206200827997101/"
    )
    result = evaluate_raw([
        _jsearch(
            "Make.com WhatsApp API Automation Specialist",
            "Upwork Client",
            _requirements(),
            url=f"{base}?utm_source=one",
        ),
        _jsearch(
            "Make.com WhatsApp API Automation Specialist",
            "Upwork Client",
            _requirements(),
            url=f"{base}?utm_source=two",
        ),
    ], as_of=AS_OF)
    assert len(result.evaluated) == 1
    assert len(result.evaluated[0].canonical.observations) == 2


def test_arbeitnow_dictionary_job_types_normalizes_without_failure():
    record = _arbeitnow("Product & Business Analyst", "Zürich")
    record["raw"]["job_types"] = {"1": "entry"}
    result = evaluate_raw([record], as_of=AS_OF)
    assert result.counts["normalization_failures"] == 0
    assert len(result.evaluated) == 1


def test_http_user_agent_contains_no_personal_email():
    assert "@" not in CONFIG.http.user_agent
