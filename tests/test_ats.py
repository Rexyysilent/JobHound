"""P2 ATS-lane fixtures: the three ATS normalizers, canonical-link dedupe
(ATS original beats aggregator copy — the §13 P2 acceptance), seen_on
corroboration, and listing-confidence scoring.
"""
from datetime import datetime, timedelta, timezone

import pytest

from jobhound.dedupe import dedupe
from jobhound.enrich.confidence import score_confidence
from jobhound.models import Job
from jobhound.normalize import normalize

GREENHOUSE_RAW = {
    "id": 111, "_slug": "labelbox", "_company": "Labelbox",
    "title": "AI Data Trainer",
    "content": "&lt;p&gt;Label data for &lt;strong&gt;LLM training&lt;/strong&gt;. Remote worldwide. "
               "Long form description follows with plenty of detail about the role and team.&lt;/p&gt;",
    "absolute_url": "https://job-boards.greenhouse.io/labelbox/jobs/111",
    "location": {"name": "Remote"},
    "first_published": "2026-06-25T12:00:00-04:00",
    "company_name": "Labelbox",
}

LEVER_RAW = {
    "id": "aaa-bbb", "_slug": "appen", "_company": "Appen",
    "text": "AI Trainer - Bengali Speakers",
    "descriptionPlain": "Independent contractor role evaluating LLM output as a fluent "
                        "Bengali speaker. Flexible hours, project based, work from anywhere.",
    "hostedUrl": "https://jobs.lever.co/appen/aaa-bbb",
    "createdAt": 1782000000000,  # MILLISECOND epoch
    "workplaceType": "remote",
    "country": "IN",
    "categories": {"location": "India", "department": "AI Trainers - Domain Experts",
                   "team": "Data Collection", "commitment": "Independent Contractor"},
    "salaryRange": {"min": 8, "max": 14, "currency": "USD", "interval": "per-hour-wage"},
}

ASHBY_RAW = {
    "id": "ccc", "_slug": "mercor", "_company": "Mercor",
    "title": "Data Annotation Expert",
    "descriptionPlain": "Fully remote annotation work on frontier model training data. "
                        "Premium rates for domain experts across many fields worldwide.",
    "jobUrl": "https://jobs.ashbyhq.com/mercor/ccc",
    "location": "Remote",
    "secondaryLocations": [{"location": "India", "address": None}],
    "isRemote": True, "isListed": True,
    "publishedAt": "2026-06-29T07:00:00.000+00:00",
    "compensation": {"scrapeableCompensationSalarySummary": "$30 - $45 per hour"},
}


def test_greenhouse_mapping_unescapes_entity_html():
    job = normalize({"source": "greenhouse", "raw": GREENHOUSE_RAW})
    assert job.company == "Labelbox"
    assert "LLM training" in job.description and "<" not in job.description
    assert job.is_remote
    assert job.posted_at is not None


def test_lever_mapping_ms_epoch_and_salary_range():
    job = normalize({"source": "lever", "raw": LEVER_RAW})
    assert job.company == "Appen"
    assert job.is_remote
    assert "bengali" in job.region_tags and "india" in job.region_tags
    assert job.pay_raw == "USD 8 - 14 per hour"
    assert job.posted_at.year == 2026  # ms epoch didn't overflow


def test_ashby_mapping_compensation_and_locations():
    job = normalize({"source": "ashby", "raw": ASHBY_RAW})
    assert job.company == "Mercor"
    assert job.pay_raw == "$30 - $45 per hour"
    assert job.location == "Remote, India"
    assert job.is_remote


# ── canonical-link dedupe (§13 P2 acceptance) ───────────────────────────────

def _pair():
    ats = Job(source="lever", title="AI Trainer - Bengali",
              company="Appen", url="https://jobs.lever.co/appen/x",
              description="short")
    agg = Job(source="remotive", title="AI Trainer – Bengali",
              company="Appen", url="https://remotive.com/jobs/123",
              description="a much longer aggregator copy of the same posting " * 5,
              pay_raw="$10/hr")
    return ats, agg


def test_ats_original_beats_richer_aggregator_copy():
    ats, agg = _pair()
    survivors = dedupe([agg, ats])
    assert len(survivors) == 1
    kept = survivors[0]
    assert kept.source == "lever"            # canonical original wins…
    assert kept.url.startswith("https://jobs.lever.co")
    assert kept.seen_on == ["lever", "remotive"]  # …but corroboration survives


def test_seen_on_single_source():
    job = Job(source="remotive", title="X", company="Acme", url="http://x", description="d")
    assert dedupe([job])[0].seen_on == ["remotive"]


# ── listing confidence ──────────────────────────────────────────────────────

def _conf(job):
    return score_confidence(job).listing_confidence


def test_confidence_rewards_ats_pay_corroboration_freshness():
    now = datetime.now(timezone.utc)
    best = Job(source="lever", title="X", url="http://x", company="Appen",
               description="d" * 300, pay_raw="$10/hr",
               seen_on=["lever", "remotive"], posted_at=now)
    assert _conf(best) == pytest.approx(1.0)  # 0.5+0.2+0.15+0.1+0.1 capped


def test_confidence_penalises_undated_thin_aggregator_copy():
    worst = Job(source="remoteok", title="X", url="http://x",
                description="thin", seen_on=["remoteok"])
    assert _conf(worst) == pytest.approx(0.3)  # 0.5 −0.1 undated −0.1 thin


def test_confidence_moves_ev_ranking():
    from jobhound.enrich.trust import compute_ev
    now = datetime.now(timezone.utc)
    a = Job(source="lever", title="X", url="http://a", company="Appen",
            description="d" * 300, pay_raw="$10/hr", seen_on=["lever"],
            posted_at=now, fit_score=50.0)
    b = Job(source="remoteok", title="X", url="http://b",
            description="thin", seen_on=["remoteok"], posted_at=now, fit_score=50.0)
    for j in (a, b):
        score_confidence(j)
        compute_ev(j)
    assert a.ev_score > b.ev_score
