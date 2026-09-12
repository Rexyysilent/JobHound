"""Patch #4 regressions from the 2026-07-21 production digest."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jobhound.config import CONFIG, PayCfg, RankingCfg
from jobhound.enrich.confidence import score_confidence
from jobhound.enrich.pay_gate import apply_pay_gate, clamp_effective
from jobhound.enrich.trust import compute_ev, freshness
from jobhound.filters.eligibility import Profile, language_gate
from jobhound.filters.listing_quality import (
    absurd_language_check,
    content_page_check,
    free_host_check,
    junk_check,
    region_lock_check,
)
from jobhound.filters.scam import is_scam, score_scam
from jobhound.filters.scam_llm import parse_batch_response, parse_retry_delay
from jobhound.models import Job
from jobhound.normalize import normalize, strip_title_prefix
from jobhound.notify.digest import build_digest, cap_per_company
from jobhound.store import Store


def _job(title: str, url: str = "https://example.test/jobs/1", **kwargs) -> Job:
    return Job(
        source="test",
        title=title,
        company=kwargs.pop("company", "Acme"),
        url=url,
        description=kwargs.pop("description", "Detailed legitimate role. " * 20),
        **kwargs,
    )


def test_retry_delay_and_fenced_batch_response_are_parsed():
    body = {"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.RetryInfo",
        "retryDelay": "37s",
    }]}}
    assert parse_retry_delay(body) == 37.0
    assert parse_retry_delay({"error": {}}) is None
    result = parse_batch_response(
        '```json\n[{"id":"a1","scam_likelihood":0.9,"reasons":["fee"]}]\n```'
    )
    assert result == {"a1": (0.9, ["fee"])}
    assert parse_batch_response("sorry, as an AI...") == {}


def test_free_host_fake_board_changes_scam_score_and_confidence():
    job = _job(
        "Spanish Search Quality Rater",
        "https://remoteclickjobs-production.up.railway.app/job/spanish-rater",
    )
    assert free_host_check(job.url) == ["scam:free_host_board:railway.app"]

    score_scam(job)
    score_confidence(job)

    assert job.scam_score == pytest.approx(CONFIG.scam.threshold)
    assert is_scam(job)
    assert job.listing_confidence == pytest.approx(0.2)
    assert free_host_check("https://brighthush.8r.unaux.com/job/2507393")
    assert not free_host_check("https://boards.greenhouse.io/turing/jobs/1")


def test_absurd_language_is_a_scam_tell():
    job = _job("AI Trainer / Data Annotator Tier 1 (English & Sumerian)")
    assert absurd_language_check(job.title)
    score_scam(job)
    assert job.scam_score == pytest.approx(0.4)
    assert not absurd_language_check("AI Trainer (English & Hindi)")


def test_spanish_search_quality_rater_is_language_gated():
    profile = Profile(
        languages={"english"},
        credential_domains=set(),
        known_languages={"english", "spanish"},
        gated_role_patterns={},
    )
    assert language_gate("Spanish Search Quality Rater", profile) == [
        "lang_mismatch:spanish"
    ]


def test_language_specialist_title_is_language_gated():
    profile = Profile(
        languages={"english", "bengali"},
        credential_domains=set(),
        known_languages={"english", "bengali", "marathi"},
        gated_role_patterns={},
    )
    assert language_gate(
        "Marathi AI Language Specialist / AI Content Quality Analyst", profile
    ) == ["lang_mismatch:marathi"]


def test_non_india_region_locks_are_rejected():
    assert region_lock_check("Spanish Search Quality Rater - (USA)") == [
        "region_locked:usa"
    ]
    assert region_lock_check("AI Rater", "Applicants must be based in the UK") == [
        "region_locked:uk"
    ]
    assert region_lock_check("Scout Search Quality Rater - English (located in India)") == []
    assert region_lock_check("Search Quality Rater - Hindi [Remote/Freelance]") == []


def test_editorial_pages_and_homepages_are_listing_junk():
    title = "LinkedIn AI Jobs: How to Find Real Remote AI Training Roles"
    url = "https://www.remoteworkunion.com/blog/linkedin-ai-jobs-find-real-remote-ai-training-roles"
    assert "junk:content_page_url" in content_page_check(title, url)
    assert "junk:content_page_title" in junk_check(title, url, "Remote Work Union", False)
    assert "junk:root_domain" in content_page_check(
        "TELUS Digital AI | Get paid for your skills",
        "https://www.telusinternational.ai/",
    )
    assert content_page_check(
        "Data Annotator", "https://boards.greenhouse.io/x/jobs/1"
    ) == []


def test_repeated_copy_and_reply_title_prefixes_are_stripped_during_normalize():
    assert strip_title_prefix("*** Re: Copy of Senior Python Developer") == (
        "Senior Python Developer"
    )
    job = normalize({
        "source": "serp",
        "raw": {
            "title": "Copy of AI Data Trainer",
            "link": "https://example.test/jobs/2",
            "snippet": "Remote AI training role in India",
        },
    })
    assert job is not None and job.title == "AI Data Trainer"


def test_effective_rate_is_clamped_and_nominal_cents_are_visible():
    assert clamp_effective(13.27, 13.0) == 13.0
    job = _job(
        "Data Annotator",
        platform_key="odd",
        pay_max_hourly_usd=13.0,
    )
    registry = {
        "odd": SimpleNamespace(availability_est=1.1, unpaid_overhead_est=-0.1)
    }
    apply_pay_gate(job, registry, PayCfg(floor_usd=4.5))
    assert job.effective_hourly_usd == 13.0

    cents = _job("Rater", pay_max_hourly_usd=13.27, effective_hourly_usd=13.27)
    assert cents.pay_display == "$13.27/hr"
    assert "$13.27/hr → eff ~$13.27/hr" in cents.pay_display_full


def test_easy_entry_jobs_use_the_longer_freshness_half_life():
    cfg = RankingCfg(freshness_half_life_days=14, easy_entry_half_life_days=45)
    now = datetime.now(timezone.utc)
    posted = now - timedelta(days=45)
    assert freshness(posted, cfg, now=now, half_life_days=45) == pytest.approx(
        0.5, abs=0.001
    )

    regular = _job("Data Engineer", posted_at=posted, fit_score=60)
    easy = _job("AI Quality Rater", posted_at=posted, fit_score=60, easy_entry=True)
    compute_ev(regular, ranking_cfg=cfg)
    compute_ev(easy, ranking_cfg=cfg)
    assert easy.ev_score > regular.ev_score * 3


def test_digest_caps_each_company_and_reports_hidden_count():
    jobs = [
        _job(
            f"Data Annotator {index}",
            f"https://example.test/jobs/{index}",
            company="Deccan AI",
            fit_score=50,
            ev_score=100 - index,
            fit_reasons=["boost_mid: data annotation"],
            pay_ok=True,
        )
        for index in range(6)
    ]
    kept, hidden = cap_per_company(jobs, n=2)
    assert len(kept) == 2 and hidden == {"Deccan AI": 4}

    digest = build_digest(jobs, top_n=15, per_company_cap=2)
    assert digest.count("https://example.test/jobs/") == 2
    assert "+4 more from Deccan AI" in digest


def test_store_daily_llm_budget_rolls_over_by_date(tmp_path):
    store = Store(tmp_path / "budget.db")
    first = date(2026, 7, 21)
    next_day = date(2026, 7, 22)
    try:
        assert store.llm_calls_today(first) == 0
        assert store.count_llm_call(first) == 1
        assert store.count_llm_call(first) == 2
        assert store.llm_calls_today(first) == 2
        assert store.llm_calls_today(next_day) == 0
    finally:
        store.close()
