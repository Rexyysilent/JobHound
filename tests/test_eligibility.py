"""tests/test_eligibility.py (v3 Patch #1)

Fixtures are the five synthetic regression cases from the 2026-07 digest
screenshot, plus positive controls that must keep passing.
Run: pytest tests/test_eligibility.py -v
"""
from pathlib import Path

import yaml

from jobhound.filters.eligibility import Profile, check
from jobhound.filters.matching import phrase_in_text

_PROFILE_YAML = Path(__file__).resolve().parent / "fixtures" / "profile.yaml"
PROFILE = Profile.from_config(yaml.safe_load(_PROFILE_YAML.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- language gate
def test_german_title_rejected():
    ok, reasons = check(
        "AI Quality Analyst (Personalization) - German", PROFILE)
    assert not ok
    assert any(r.startswith("lang_mismatch:german") for r in reasons)


def test_bengali_title_passes():
    ok, _ = check("Internet Safety Evaluator India - Bengali Language", PROFILE)
    assert ok


def test_bangla_alias_passes():
    ok, _ = check("Transcriber (Bangla)", PROFILE)
    assert ok


def test_hindi_english_pair_passes():
    ok, _ = check("Hindi-English Bilingual Data Annotator", PROFILE)
    assert ok


def test_english_instructor_passes_language_gate():
    # english IS in profile -> language gate passes; the ANCHOR RULE (not this
    # gate) is what keeps teaching roles out of the digest.
    ok, _ = check("English Language Instructor", PROFILE)
    assert ok


def test_multilingual_never_rejects():
    ok, _ = check("Multilingual AI Rater", PROFILE)
    assert ok


def test_language_word_inside_company_not_flagged():
    # "Thai" only as part of a plain word/company shouldn't trip contextual patterns
    ok, _ = check("Data Analyst at Thaicom Analytics", PROFILE)
    assert ok


# -------------------------------------------------------------- credential gate
def test_chemical_engineering_expert_rejected():
    ok, reasons = check("Chemical Engineering Expert - AI Training", PROFILE)
    assert not ok
    assert any(r.startswith("credential_mismatch:engineering_expert") for r in reasons)


def test_ex_mbb_rejected():
    ok, reasons = check("Ex-MBB Strategy Consultant - AI Training (Remote)", PROFILE)
    assert not ok
    assert any(r.startswith("credential_mismatch:consultant_pedigree") for r in reasons)


def test_md_rejected():
    ok, _ = check("Physician (MD) - Medical AI Evaluator", PROFILE)
    assert not ok


def test_software_engineer_ai_training_passes():
    # software IS a claimable domain
    ok, _ = check("Software Engineering Expert - AI Training", PROFILE)
    assert ok


# ------------------------------------------------------------- phrase matching
def test_prompt_engineer_does_not_match_qa_engineer():
    assert not phrase_in_text("prompt engineer", "QA Engineer — InEight, automation testing role")


def test_prompt_engineer_matches_real_mentions():
    assert phrase_in_text("prompt engineer", "We are hiring a Prompt Engineer")
    assert phrase_in_text("prompt engineer", "prompt engineering experience required")
    assert phrase_in_text("prompt engineer", "promt engineer")  # typo tolerated


def test_short_tokens_exact_only():
    assert phrase_in_text("ai training", "AI training projects")
    assert not phrase_in_text("ai training", "air training academy")  # 'ai' != 'air'


def test_window_bound():
    assert not phrase_in_text(
        "prompt engineer",
        "prompt payment guaranteed for every civil engineer on the team")


def test_annotation_typo():
    assert phrase_in_text("data annotation", "data anotation specialists wanted")


# --------------------------------------------------------- anchor rule (scorer)
class _J:  # minimal job stub
    def __init__(self, title, desc="", remote=True):
        self.title, self.description = title, desc
        self.is_remote, self.region_tags = remote, ["worldwide"]


KW = {
    "boost_high": ["ai training", "rlhf", "prompt engineer", "data annotation",
                    "search evaluator"],
    "boost_mid": ["python", "automation"],
    "boost_lang": ["bengali", "bangla", "hindi", "multilingual"],
    "exclude_hard": ["registration fee"],
    "exclude_soft": [],
}


def _score(job):
    from jobhound.filters.relevance import score
    return score(job, KW, PROFILE)


def test_category_only_job_capped():
    fit, reasons = _score(_J("English Language Instructor",
                             "Help with AI training for language models"))
    assert fit <= PROFILE.no_anchor_fit_cap
    assert any("no_profile_anchor" in r for r in reasons)


def test_anchored_bengali_job_keeps_full_score():
    fit, reasons = _score(_J("Search Evaluator - Bengali",
                             "Evaluate search results. Bengali speakers, India."))
    assert fit >= 50  # base 10 + search evaluator 25 + bengali 20 = 55
    assert not any("no_profile_anchor" in r for r in reasons)


def test_qa_engineer_gets_no_prompt_engineer_points():
    fit, reasons = _score(_J("QA Engineer", "Manual and automated QA testing"))
    assert not any("prompt engineer" in r for r in reasons)
