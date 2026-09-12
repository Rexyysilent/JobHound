"""Relevance + language-edge fixtures. Guards the two real bugs found in the
first live run: short-keyword substring hits (dpo→endpoint) and the language
lane firing on company etymology (hindi)."""
from jobhound.config import KeywordsCfg, RelevanceCfg
from jobhound.models import Job
from jobhound.normalize import language_signal
from jobhound.filters.listing_quality import location_gate
from jobhound.filters.relevance import score_relevance

RCFG = RelevanceCfg(
    fuzzy_threshold=88,
    keywords=KeywordsCfg(
        boost_high=["dpo", "rlhf", "data annotation"],
        boost_mid=["python", "automation"],
        exclude_hard=["mlm", "reshipping"],
        exclude_soft=["unpaid intern"],
    ),
)


def _job(title, desc="", is_remote=True, region_tags=None):
    return Job(source="t", title=title, url="http://x", description=desc,
               is_remote=is_remote, region_tags=region_tags or [])


def test_short_keyword_not_matched_as_substring():
    job = _job("Senior DevOps Engineer", "You will manage the API endpoint and CI/CD.")
    score_relevance(job, RCFG)
    assert not any("dpo" in r for r in job.fit_reasons)  # 'dpo' ⊄ 'endpoint'


def test_short_keyword_matched_as_word():
    job = _job("RLHF DPO Tuning Specialist", "Reinforcement learning from human feedback.")
    score_relevance(job, RCFG)
    assert any("dpo" in r for r in job.fit_reasons)
    assert any("rlhf" in r for r in job.fit_reasons)


def test_language_lane_fires_on_title():
    # Flagship case (HANDOFF §9): language in the title is role-defining.
    assert language_signal("Internet Safety Evaluator – Bengali", "Evaluate content.")


def test_language_lane_ignores_etymology():
    assert not language_signal(
        "Sales Director Europe",
        "Bidgely (which means electricity in hindi) accelerates clean energy.",
    )


def test_language_lane_fires_on_body_with_cue():
    assert language_signal("Data Annotator", "We need a fluent Bengali speaker.")


def test_boost_lang_from_region_tag():
    job = _job("Bengali Content Evaluator", "x", region_tags=["worldwide", "bengali"])
    score_relevance(job, RCFG)
    assert any(r.startswith("boost_lang") for r in job.fit_reasons)
    assert job.fit_score >= 20


def test_non_remote_rejected_by_hard_location_gate():
    job = _job("Python Developer", "python role", is_remote=False, region_tags=[])
    score_relevance(job, RCFG)
    assert job.fit_score == 22.0  # relevance no longer owns location policy
    assert location_gate(job.is_remote, job.region_tags, job.location)


def test_exclude_hard_short_circuits():
    job = _job("Join our MLM team", "build your mlm downline")
    assert score_relevance(job, RCFG) is True
    assert job.fit_score == 0.0
