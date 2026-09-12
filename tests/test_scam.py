"""Scam-gate fixtures — labeled good/scam rows. The gate's whole value is trust."""
from jobhound.config import ScamCfg
from jobhound.filters.scam import is_scam, score_scam
from jobhound.models import Job

CFG = ScamCfg(threshold=0.6, min_description_chars=80)

LEGIT_DESC = (
    "We are hiring a senior Python engineer to build and maintain our data "
    "pipelines. Remote-friendly, competitive salary, strong team. You will work "
    "with httpx, pydantic and sqlite across our ingestion services."
)


def _job(title, desc, company="Acme Corp"):
    return Job(source="t", title=title, url="http://x", description=desc, company=company)


def test_legit_job_passes():
    job = score_scam(_job("Senior Python Engineer (Remote)", LEGIT_DESC), CFG)
    assert not is_scam(job, CFG)
    assert job.scam_score < 0.6


def test_obvious_scam_is_caught():
    job = score_scam(_job(
        "EARN $5000 PER WEEK FROM HOME 💰💰💰",
        "No experience needed, guaranteed income! Pay a registration fee to "
        "start. Contact us on WhatsApp. recruiter@example.invalid",
        company=None,
    ), CFG)
    assert is_scam(job, CFG)
    assert job.scam_score >= 0.6
    assert "asks-for-money" in job.scam_flags
    assert "too-good-to-be-true" in job.scam_flags


def test_reshipping_archetype_flagged():
    job = score_scam(_job(
        "Package Reshipping Coordinator",
        "Receive packages at your home address and reship them onward. We also "
        "process payments on behalf of overseas clients every single week.",
    ), CFG)
    assert "scam-archetype" in job.scam_flags
    assert is_scam(job, CFG)


def test_mlm_flagged():
    job = score_scam(_job(
        "Network Marketing Representative",
        "Build your downline and recruit others to grow your monthly income.",
    ), CFG)
    assert "mlm" in job.scam_flags


def test_thin_post_weighted_not_auto_killed():
    # Thin + no-company are signals, not an instant kill (HANDOFF §7a).
    job = score_scam(_job("Data Entry", "", company=None), CFG)
    assert "thin-description" in job.scam_flags
    assert "no-company" in job.scam_flags
    assert not is_scam(job, CFG)  # 0.35 < threshold
