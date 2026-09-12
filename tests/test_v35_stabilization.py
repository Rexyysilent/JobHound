"""Synthetic ranking regressions: source quality, eligibility, title pay, text normalization and exhaustive accounting. No private database rows or historical digest are distributed."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from jobhound.cli import format_run_summary
from jobhound.config import CONFIG, ScoringCfg, TitlePenaltyCfg
from jobhound.dedupe import dedupe
from jobhound.enrich.pay import parse_title_pay
from jobhound.filters.accessibility import (
    EASY_ROLE_WORDS,
    digest_group,
    easy_entry,
    title_fit_cap,
)
from jobhound.filters.eligibility import (
    Profile,
    check as eligibility_check,
    default_profile,
)
from jobhound.filters.listing_quality import farm_source_check, junk_check
from jobhound.models import (
    TERMINAL_VERDICTS,
    Job,
    VERDICT_PENDING,
    VERDICT_REJECTED_INELIGIBLE,
    VERDICT_REJECTED_IRRELEVANT,
    VERDICT_REJECTED_LISTING_QUALITY,
    VERDICT_SURFACED,
)
from jobhound.normalize import apply_text_normalization, canonicalize
from jobhound.notify.digest import build_digest
from jobhound.pipeline import account_ledger, evaluate_jobs, finalize_verdicts
from jobhound.text import normalize_match, normalize_text

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "ranking_regressions.json"

# Canonical Job fields carried by the fixture rows (key/expect/why/db_before
# are metadata). region_tags are passed through as stored so the fixtures start
# from an explicit synthetic tag state; canonicalize() unions its own re-derived
# tags on top, which is what the pipeline does for every real job.
_JOB_FIELDS = (
    "source", "title", "company", "url", "description",
    "location", "is_remote", "region_tags", "pay_raw", "posted_at",
)

# Mojibake tells scanned for in rendered output.
MOJIBAKE_MARKERS = ("â", "Ã", "Â", "�")


@lru_cache(maxsize=1)
def _fixture_rows() -> tuple[dict, ...]:
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return tuple(data["jobs"])


def _build_jobs() -> dict[str, Job]:
    """Fixture rows → canonical Jobs, deduped, gated and given verdicts."""
    keyed: list[tuple[str, Job]] = []
    for row in _fixture_rows():
        fields = {k: row[k] for k in _JOB_FIELDS if row.get(k) is not None}
        keyed.append((row["key"], canonicalize(Job(**fields))))

    jobs = dedupe([job for _, job in keyed])
    assert len(jobs) == len(keyed), "fixtures must not collapse into each other"
    finalize_verdicts(jobs, evaluate_jobs(jobs))
    return {key: job for key, job in keyed}


@pytest.fixture(scope="module")
def prod() -> dict[str, Job]:
    return _build_jobs()


# ── 1. text normalization ────────────────────────────────────────────────────

def test_mojibake_is_repaired_on_the_canonical_job_not_just_the_digest(prod):
    job = prod["elite_job_mojibake"]
    assert job.title == "AI Training Contributor – Remote"
    assert not any(marker in job.title for marker in MOJIBAKE_MARKERS)


def test_legitimate_unicode_punctuation_survives_normalization(prod):
    # HumaniTapp's en dash is real text, not damage — display keeps it.
    assert "–" in prod["humanitapp_title_pay"].title


def test_matching_form_folds_dashes_without_touching_the_display_form():
    display = normalize_text("Rater – Bengali — Remote")
    assert "–" in display and "—" in display
    assert normalize_match("Rater – Bengali — Remote") == (
        "Rater - Bengali - Remote"
    )


def test_normalization_is_idempotent_and_conservative():
    messy = "Data​ Rater\t\tRemote\r\n\r\n\r\n\r\nApply   now  "
    once = normalize_text(messy)
    assert once == "Data Rater Remote\n\nApply now"
    assert normalize_text(once) == once
    # Bengali/Devanagari joiners are meaningful — they must not be stripped.
    joined = "কাজ‍ বাংলা"
    assert normalize_text(joined) == joined
    assert normalize_text(None) is None


def test_all_human_fields_are_normalized_before_interpretation():
    job = Job(
        source="serp",
        title="AI Trainer â€“ Remote",
        company="Elite â€“ Job",
        url="https://example.test/job/1",
        description="Great â€“ role",
        location="Remote â€“ India",
        pay_raw="$12 â€“ $15/hr",
    )
    apply_text_normalization(job)
    for value in (job.title, job.company, job.description, job.location, job.pay_raw):
        assert "–" in value
        assert not any(marker in value for marker in MOJIBAKE_MARKERS)


def test_whitespace_only_optional_fields_normalize_to_none():
    job = Job(source="serp", title="Rater", url="https://example.test/j/1",
              company="    ", location="​", pay_raw=" ")
    apply_text_normalization(job)
    assert job.company is None and job.location is None and job.pay_raw is None


def test_region_tags_are_derived_from_repaired_text():
    job = canonicalize(Job(
        source="serp",
        title="AI Data Trainer â€“ Bengali",
        url="https://example.test/job/2",
        description="Native Bengali speaker required.",
        is_remote=True,
    ))
    assert "bengali" in job.region_tags


# ── 2. title pay with provenance ─────────────────────────────────────────────

def test_humanitapp_title_range_parses_with_title_provenance(prod):
    job = prod["humanitapp_title_pay"]
    assert (job.pay_min_hourly_usd, job.pay_max_hourly_usd) == (120.0, 170.0)
    assert job.pay_basis == "hour"
    assert job.pay_source == "title"
    assert job.pay_display != "pay unknown"
    # The extracted expression is preserved as the audit trail.
    assert "120" in job.pay_raw and "170" in job.pay_raw


@pytest.mark.parametrize("title,expected", [
    ("AI Trainer $120–$170/hr", (120.0, 170.0)),
    ("AI Trainer $120-$170/hr", (120.0, 170.0)),
    ("AI Trainer $120—$170/hr", (120.0, 170.0)),
    ("AI Trainer $120 to $170 per hour", (120.0, 170.0)),
    ("AI Trainer USD 120–170/hour", (120.0, 170.0)),
    ("AI Trainer $25/hr", (25.0, 25.0)),
    ("AI Trainer $25 hourly", (25.0, 25.0)),
])
def test_explicit_hourly_title_expressions_parse(title, expected):
    parsed = parse_title_pay(title, CONFIG.pay)
    assert parsed is not None, title
    assert (parsed[0], parsed[1]) == expected
    assert parsed[2] == "hour"


def test_inr_title_rates_convert_through_the_configured_fx():
    parsed = parse_title_pay("Bengali Rater ₹500–₹800 per hour", CONFIG.pay)
    assert parsed is not None
    fx = CONFIG.pay.fx_to_usd.get("INR", 1.0)
    assert parsed[0] == pytest.approx(round(500 * fx, 2))
    assert parsed[1] == pytest.approx(round(800 * fx, 2))


@pytest.mark.parametrize("title", [
    "AI Trainer 120-170",                      # no currency, no basis
    "AI Trainer $120-$170",                    # currency but no hourly basis
    "AI Trainer 120-170 per hour",             # hourly basis but no currency
    "Project Chiron - Bengali Data Trainer",   # no compensation at all
    "Rater for 2026 - 500 tasks available",    # bare numbers are not pay
    "AI Trainer $60,000 per year",             # annual is not an hourly title rate
])
def test_titles_without_an_explicit_hourly_rate_are_not_guessed(title):
    assert parse_title_pay(title, CONFIG.pay) is None


def test_structured_pay_keeps_priority_over_the_title():
    """A parseable provider field wins; the title is only a fallback."""
    from jobhound.enrich.pay import enrich_pay
    job = Job(source="adzuna", title="AI Data Trainer $120–$170/hr",
              url="https://example.test/j/7", pay_raw="$8 - $12 per hour")
    enrich_pay(job)
    assert job.pay_source == "structured"
    assert job.pay_raw == "$8 - $12 per hour"
    assert (job.pay_min_hourly_usd, job.pay_max_hourly_usd) == (8.0, 12.0)


def test_email_offer_pay_is_labelled_email_not_structured():
    from jobhound.enrich.pay import enrich_pay
    job = Job(source="email:oneforma", title="Bengali Rater",
              url="https://example.test/j/8", pay_raw="$5.00 per hour")
    enrich_pay(job)
    assert job.pay_source == "email"


def test_unknown_pay_stays_unknown_and_is_not_rejected(prod):
    job = prod["ngspice_electronics_reviewer"]
    assert job.pay_source == "unknown"
    assert job.pay_ok is None


def test_title_pay_does_not_scan_description_numbers():
    job = Job(
        source="serp",
        title="AI Data Trainer",
        url="https://example.test/job/3",
        description="Budget is $250,000 per year across 1200 tasks at $95/hr.",
    )
    from jobhound.enrich.pay import enrich_pay
    enrich_pay(job)
    assert job.pay_source == "unknown"
    assert job.pay_min_hourly_usd is None


def test_high_claimed_pay_is_not_treated_as_fraud(prod):
    job = prod["humanitapp_title_pay"]
    assert "too-good-to-be-true" not in job.scam_flags
    assert job.scam_score < CONFIG.scam.threshold


# ── 3. farm quarantine ──────────────────────────────────────────────────────

def test_both_mysmartpros_records_are_listing_quality_rejections(prod):
    for key in ("farm_mysmartpros_stuffed", "farm_mysmartpros_second"):
        job = prod[key]
        assert job.verdict == VERDICT_REJECTED_LISTING_QUALITY, key
        assert any(r.startswith("junk:source_farm:mysmartpros.com")
                   for r in job.fit_reasons), key


def test_elite_job_copy_is_normalized_and_rejected_for_source_quality(prod):
    job = prod["elite_job_mojibake"]
    assert job.verdict == VERDICT_REJECTED_LISTING_QUALITY
    assert "junk:source_farm:theelitejob.com" in job.fit_reasons
    assert "–" in job.title  # repaired even though it was rejected


def test_bebee_copy_has_pay_parsed_but_stays_a_listing_quality_rejection(prod):
    job = prod["humanitapp_title_pay"]
    assert job.verdict == VERDICT_REJECTED_LISTING_QUALITY
    assert "junk:source_farm:bebee.com" in job.fit_reasons
    assert job.pay_max_hourly_usd == 170.0  # parsed regardless of the verdict


def test_farm_path_signal_fires_on_an_otherwise_unremarkable_host():
    reasons = farm_source_check("https://tutorsite.test/tuition/job/ai-trainer-remote")
    assert reasons == ["junk:farm_path:/tuition/job/"]


def test_keyword_stuffing_cannot_override_the_categorical_farm_gate(prod):
    """The farm page's own fit score is irrelevant — the gate is categorical."""
    stuffed = prod["farm_mysmartpros_stuffed"]
    chiron = prod["chiron_bengali_trainer"]
    assert stuffed.verdict == VERDICT_REJECTED_LISTING_QUALITY
    assert chiron.verdict == VERDICT_SURFACED
    # Even if the farm copy out-scored Chiron, it cannot reach the digest.
    surfaced = [j for j in prod.values() if j.verdict == VERDICT_SURFACED]
    assert stuffed not in surfaced


def test_retained_canonical_url_is_assessed_on_its_own_merits():
    """A farm entry in seen_on must not reject a canonical ATS/employer link."""
    job = Job(
        source="greenhouse",
        title="Bengali Data Annotator",
        company="Welo Data",
        url="https://boards.greenhouse.io/welodata/jobs/4411",
        description="Bengali data annotation and AI training work. " * 6,
        seen_on=["greenhouse", "serp"],
    )
    assert junk_check(job.title, job.url, job.company, has_rate=False) == []
    assert farm_source_check(job.url) == []


def test_employer_domain_posting_is_not_mistaken_for_a_farm():
    assert farm_source_check(
        "https://careers.humanitapp.test/jobs/ai-data-trainer",
        company_domain="humanitapp.test",
    ) == []


def test_theelitejob_is_configured_as_a_farm_domain():
    assert "theelitejob.com" in CONFIG.listing_quality.farm_domains


def test_farm_confidence_prior_and_ev_inputs_are_unchanged():
    """v3.5 quarantines farms; it must not retune the EV/trust machinery."""
    assert CONFIG.listing_quality.tier_confidence["farm"] == 0.35
    assert CONFIG.trust.unrated_multiplier == 0.85
    assert CONFIG.trust.hard_floor == 0.25


# ── 4. credential gates ─────────────────────────────────────────────────────

def test_neuroscience_specialist_is_ineligible(prod):
    job = prod["neuroscience_specialist"]
    assert job.verdict == VERDICT_REJECTED_INELIGIBLE
    assert any("credential_mismatch:neuroscience" in r for r in job.fit_reasons)


def test_ngspice_reviewer_is_ineligible(prod):
    job = prod["ngspice_electronics_reviewer"]
    assert job.verdict == VERDICT_REJECTED_INELIGIBLE
    assert any("credential_mismatch:electronics" in r for r in job.fit_reasons)


@pytest.mark.parametrize("title,domain", [
    ("Neuroscience Specialist (AI Training Project)", "neuroscience"),
    ("Neurobiology Expert Reviewer", "neuroscience"),
    ("Neurology Specialist - Remote", "neuroscience"),
    ("Neuroscientist for AI Evaluation", "neuroscience"),
    ("Electronics Simulation Reviewer", "electronics"),
    ("Electrical Design Engineer", "electronics"),
    ("Circuit Simulation Specialist", "electronics"),
    ("NgSpice Electronics Simulation Reviewer", "eda_tools"),
    ("LTspice Model Reviewer", "eda_tools"),
    ("SPICE Netlist Reviewer", "eda_tools"),
    ("PCB Layout Specialist", "hardware_eda"),
    ("VLSI Design Expert", "hardware_eda"),
    ("Verilog Verification Engineer", "hardware_eda"),
    ("FPGA Design Specialist", "hardware_eda"),
])
def test_title_stated_specialist_domains_are_gated(title, domain):
    eligible, reasons = eligibility_check(title, default_profile())
    assert not eligible, title
    assert any(f"credential_mismatch:{domain}" in r for r in reasons), (title, reasons)


@pytest.mark.parametrize("title", [
    "Python Automation Engineer",
    "Data Annotation Specialist",
    "Software Engineer - Web Scraping",
    "AI Training Contributor - Remote",
    "Bengali Data Trainer",
    "Search Quality Rater",
    "Game QA Tester",
    "Lead Generation Specialist",
    "Embedded Software Engineer",
    "Spice Blend Product Reviewer",
])
def test_claimed_and_unrelated_domains_remain_eligible(title):
    eligible, reasons = eligibility_check(title, default_profile())
    assert eligible, (title, reasons)


def test_credential_gates_read_titles_not_descriptions():
    """Description boilerplate must not trigger a credential rejection."""
    eligible, _ = eligibility_check("AI Data Trainer", default_profile())
    assert eligible


# ── 4b. canonical credential opt-ins ────────────────────────────────────────

def _profile_claiming(*domains: str) -> Profile:
    """The shipped profile with credential_domains swapped for `domains`."""
    base = default_profile()
    return Profile(
        languages=base.languages,
        credential_domains=set(domains),
        known_languages=base.known_languages,
        gated_role_patterns=base.gated_role_patterns,
        domain_aliases=base.domain_aliases,
        anchors=base.anchors,
        no_anchor_fit_cap=base.no_anchor_fit_cap,
    )


@pytest.mark.parametrize("title", [
    "Neuroscience Specialist (AI Training Project)",
    "Neurobiology Expert Reviewer",
    "Neurology Specialist - Remote",
    "Neuroscientist for AI Evaluation",
    "Neurologist",
])
def test_claiming_neuroscience_authorizes_its_aliases(title):
    """Opting into a domain must not require auditing every gate pattern."""
    eligible, reasons = eligibility_check(title, _profile_claiming("neuroscience"))
    assert eligible, (title, reasons)


@pytest.mark.parametrize("title", [
    "Electronics Simulation Reviewer",
    "Electrical Design Engineer",
    "Circuit Simulation Specialist",
    "NgSpice Electronics Simulation Reviewer",
    "LTspice Model Reviewer",
    "SPICE Netlist Reviewer",
    "PCB Layout Specialist",
    "VLSI Design Expert",
    "Verilog Verification Engineer",
    "FPGA Design Specialist",
])
def test_claiming_electronics_authorizes_its_aliases(title):
    eligible, reasons = eligibility_check(title, _profile_claiming("electronics"))
    assert eligible, (title, reasons)


def test_alias_groups_do_not_leak_across_domains():
    """Claiming one canonical domain must not authorize an unrelated one."""
    neuro_only = _profile_claiming("neuroscience")
    electronics_only = _profile_claiming("electronics")
    assert not eligibility_check("NgSpice Electronics Simulation Reviewer",
                                 neuro_only)[0]
    assert not eligibility_check("Neurobiology Expert Reviewer",
                                 electronics_only)[0]


def test_claiming_an_alias_directly_still_works():
    eligible, _ = eligibility_check("Verilog Verification Engineer",
                                    _profile_claiming("verilog"))
    assert eligible
    # ...but it does not pull in the rest of the canonical group.
    assert not eligibility_check("PCB Layout Specialist",
                                 _profile_claiming("verilog"))[0]


def test_claimed_domains_expansion_is_explicit():
    profile = _profile_claiming("neuroscience")
    expanded = profile.claimed_domains()
    assert "neuroscience" in expanded and "neurologist" in expanded
    assert "pcb" not in expanded
    # An unaliased domain expands to itself only.
    assert _profile_claiming("software").claimed_domains() == {"software"}


def test_the_shipped_profile_claims_neither_domain():
    """The production profile must still reject both gated domains."""
    claimed = default_profile().claimed_domains()
    assert "neuroscience" not in claimed
    assert "electronics" not in claimed and "ngspice" not in claimed


# ── 5. leadership titles ────────────────────────────────────────────────────

def test_delivery_leader_is_capped_below_the_surface_cutoff(prod):
    job = prod["llm_delivery_leader"]
    assert job.fit_score <= 30
    assert job.fit_score < CONFIG.relevance.min_fit_to_surface
    assert job.verdict != VERDICT_SURFACED
    assert job.verdict == VERDICT_REJECTED_IRRELEVANT
    assert any(r.startswith("leadership_cap:leadership_track")
               for r in job.fit_reasons), job.fit_reasons


@pytest.mark.parametrize("title", [
    "LLM Training Delivery Leader",
    "Team Leader - Data Annotation",
    "Delivery Leader",
    "Head of AI Training",
    "Data Delivery Head",
    "Director of Annotation Operations",
    "Vice President, Model Evaluation",
    "VP Data Operations",
    "Chief Data Officer",
    "Principal Data Scientist",
])
def test_leadership_titles_receive_a_fit_cap(title):
    cap, rule = title_fit_cap(title)
    assert cap == 30, title
    assert rule == "leadership_track"


@pytest.mark.parametrize("title", [
    "Head of AI Training",              # leading, with "of"
    "Head, AI Training",                # comma-separated
    "Head - Delivery Operations",       # ASCII hyphen
    "Head – Data Annotation",           # en dash
    "Head — Model Evaluation",          # em dash
    "Head: Data Programs",              # colon
    "Head; Annotation Ops",             # semicolon
    "Head / Data Operations",           # slash
    "Head | Model Evaluation",          # pipe
    "Annotation Head (Remote)",         # parenthesised suffix
    "Delivery Head [India]",            # bracketed suffix
    "Delivery Head, India",             # trailing head, then comma
    "Data Delivery Head",               # trailing head, end of title
])
def test_punctuation_separated_head_titles_are_capped(title):
    """"Head" rarely ends a real title — it is usually followed by punctuation."""
    cap, rule = title_fit_cap(title)
    assert cap == 30, title
    assert rule == "leadership_track"


@pytest.mark.parametrize("title", [
    "Lead Generation Specialist",
    "Leaderboard Data Annotator",
    "AI Data Trainer",
    "Search Quality Rater",
    "Headphones Product Reviewer",      # "head" inside a longer word
    "Headset Audio Transcriber",
    "Overhead Cost Analyst",            # "head" as a word suffix
    "Subheading Content Rater",
])
def test_unrelated_titles_are_not_capped(title):
    cap, _ = title_fit_cap(title)
    assert cap is None, title


def test_the_cap_sits_below_the_surface_cutoff():
    cap = CONFIG.scoring.title_penalties["leadership_track"].fit_cap
    assert cap is not None and cap < CONFIG.relevance.min_fit_to_surface


def test_ordinary_manager_handling_is_unchanged():
    """`manager` keeps the -20 senior_track penalty and gains no hard cap."""
    senior = CONFIG.scoring.title_penalties["senior_track"]
    assert senior.delta == -20 and senior.fit_cap is None
    cap, _ = title_fit_cap("Annotation Program Manager")
    assert cap is None


def test_a_configured_cap_beats_any_keyword_stack():
    cfg = ScoringCfg(title_penalties={
        "leadership_track": TitlePenaltyCfg(pattern=r"\bleader\b", fit_cap=30),
    })
    job = Job(source="test", title="RLHF AI Training Delivery Leader",
              url="https://example.test/j/9", description="")
    job.fit_score = 100.0
    from jobhound.filters.accessibility import apply as apply_accessibility
    apply_accessibility(job, cfg)
    assert job.fit_score == 30.0


# ── 6. easy-entry presentation bypass ───────────────────────────────────────

def test_digest_group_only_trusts_the_scored_easy_entry_flag():
    rescued = Job(source="test", title="AI Content Reviewer",
                  url="https://example.test/j/4", description="")
    rescued.pay_ok = None
    assert rescued.easy_entry is False
    assert digest_group(rescued) is None

    genuine = Job(source="test", title="AI Content Reviewer",
                  url="https://example.test/j/5", description="")
    genuine.easy_entry = True
    assert digest_group(genuine) == "\U0001f3af"


def test_ngspice_never_enters_the_easy_entry_group(prod):
    job = prod["ngspice_electronics_reviewer"]
    assert job.easy_entry is False
    # The pre-patch bypass condition is still satisfied by this listing —
    # unknown pay plus a role word in the title — and must now be inert.
    assert job.pay_ok is None
    assert EASY_ROLE_WORDS.search(job.title)
    assert digest_group(job) is None


@pytest.mark.parametrize("description", [
    "Fluent English is required and the schedule is flexible.",
    "Flexible hours, work when you want.",
    "English proficiency required for this reviewer role.",
])
def test_weak_signals_alone_do_not_establish_easy_entry(description):
    accessible, reasons = easy_entry("AI Content Reviewer", description)
    assert accessible is False
    assert reasons == ["easy_entry_weak_only: no explicit low-barrier claim"]


def test_a_generic_reviewer_title_alone_is_not_easy_entry():
    accessible, _ = easy_entry("Data Annotator", "Review model outputs daily.")
    assert accessible is False


@pytest.mark.parametrize("description", [
    "No experience required. Apply today.",
    "No prior experience needed, training will be provided.",
    "Entry level position, freshers welcome.",
    "Paid training provided for all new contributors.",
    "No degree required and all backgrounds are welcome.",
])
def test_explicit_low_barrier_claims_remain_easy_entry(description):
    accessible, reasons = easy_entry("AI Content Reviewer", description)
    assert accessible is True, description
    assert reasons and reasons[0].startswith("easy_entry: ")


def test_specialist_demands_and_senior_titles_still_block_easy_entry():
    blocked, reasons = easy_entry(
        "AI Reviewer", "No experience required. PhD in physics required.")
    assert blocked is False
    assert reasons == ["easy_entry_blocked:specialist_demands"]

    senior, reasons = easy_entry(
        "Annotation Team Leader", "No experience required, training provided.")
    assert senior is False
    assert reasons == ["easy_entry_blocked:senior_title"]


# ── 7. the ledger ───────────────────────────────────────────────────────────

def test_production_ledger_reconciles_exactly():
    """The 2026-07-22 run never lost 415 jobs; the summary omitted two lines."""
    production = {
        "surfaced": 87,
        "rejected_scam": 5,
        "rejected_pay": 48,
        "rejected_trust": 0,
        "rejected_ineligible": 210,
        "rejected_location": 372,
        "rejected_listing_quality": 43,
        "rejected_irrelevant": 529,
    }
    assert set(production) == set(TERMINAL_VERDICTS)
    assert sum(production.values()) == 1294

    jobs: list[Job] = []
    index = 0
    for verdict, count in production.items():
        for _ in range(count):
            jobs.append(Job(source="test", title=f"Job {index}", id=f"id{index}",
                            url=f"https://example.test/j/{index}", verdict=verdict))
            index += 1

    counts, unaccounted_ids = account_ledger(jobs, deduped=1294)
    assert counts["accounted"] == 1294
    assert counts["unaccounted"] == 0
    assert unaccounted_ids == []
    for verdict, expected in production.items():
        assert counts[verdict] == expected, verdict


def test_every_fixture_job_lands_on_exactly_one_terminal_verdict(prod):
    jobs = list(prod.values())
    counts, unaccounted_ids = account_ledger(jobs, deduped=len(jobs))
    assert unaccounted_ids == []
    assert counts["unaccounted"] == 0
    assert counts["accounted"] == len(jobs)
    assert sum(counts[v] for v in TERMINAL_VERDICTS) == len(jobs)


def test_a_non_terminal_verdict_is_reported_as_unaccounted():
    jobs = [
        Job(source="test", title="Scored", url="https://example.test/j/1",
            id="a1", verdict=VERDICT_SURFACED),
        Job(source="test", title="Fell through", url="https://example.test/j/2",
            id="b2", verdict=VERDICT_PENDING),
    ]
    counts, unaccounted_ids = account_ledger(jobs, deduped=2)
    assert counts["unaccounted"] == 1
    assert unaccounted_ids == ["b2"]


def test_the_digest_summary_prints_every_ledger_category():
    counts = {
        "raw": 4210, "normalized": 3980, "deduped": 1294,
        "surfaced": 87, "rejected_scam": 5, "rejected_pay": 48,
        "rejected_trust": 0, "rejected_ineligible": 210,
        "rejected_location": 372, "rejected_listing_quality": 43,
        "rejected_irrelevant": 529, "accounted": 1294, "unaccounted": 0,
    }
    summary = format_run_summary(counts, new_count=12)
    for label, value in (("scam", 5), ("pay", 48), ("trust", 0),
                         ("ineligible", 210), ("location", 372),
                         ("listing quality", 43), ("irrelevant", 529),
                         ("unaccounted", 0)):
        assert f"{label} {value}" in summary, label
    assert "deduped 1294" in summary
    assert "surfaced 87 (new 12)" in summary


def test_no_new_terminal_verdicts_were_invented():
    assert TERMINAL_VERDICTS == (
        "surfaced", "rejected_scam", "rejected_pay", "rejected_trust",
        "rejected_ineligible", "rejected_location", "rejected_listing_quality",
        "rejected_irrelevant",
    )
    assert "company_capped" not in TERMINAL_VERDICTS
    assert "stale_carryover" not in TERMINAL_VERDICTS


# ── 8. visibility / ordering contract ───────────────────────────────────────

def test_every_fixture_reaches_its_documented_verdict(prod):
    for row in _fixture_rows():
        job = prod[row["key"]]
        assert job.verdict == row["expect"], (row["key"], job.fit_reasons)


def test_only_chiron_survives_to_the_digest(prod):
    surfaced = {k for k, j in prod.items() if j.verdict == VERDICT_SURFACED}
    assert surfaced == {"chiron_bengali_trainer"}


def test_the_synthetic_digest_is_clean(prod):
    surfaced = [j for j in prod.values() if j.verdict == VERDICT_SURFACED]
    digest = build_digest(surfaced, summary="offline fixture render", top_n=15)

    assert "Project Chiron" in digest
    for marker in MOJIBAKE_MARKERS:
        assert marker not in digest, marker
    # No farm listing may appear in the surfaced digest.
    for host in ("mysmartpros.com", "bebee.com", "theelitejob.com",
                 "opentrain.ai", "workable.com"):
        assert host not in digest, host
    # Chiron genuinely has no stated rate, so "pay unknown" is correct here —
    # unknown pay is down-ranked, never rejected.
    assert prod["chiron_bengali_trainer"].pay_ok is None


def test_farm_domains_never_reach_the_surfaced_set(prod):
    for key in ("farm_mysmartpros_stuffed", "farm_mysmartpros_second",
                "humanitapp_title_pay", "elite_job_mojibake"):
        assert prod[key].verdict != VERDICT_SURFACED, key


def test_specialist_and_leadership_mismatches_do_not_surface(prod):
    for key in ("ngspice_electronics_reviewer", "neuroscience_specialist",
                "llm_delivery_leader"):
        assert prod[key].verdict != VERDICT_SURFACED, key


def test_farm_rejections_are_retained_for_audit(prod):
    """Rejected rows keep their reasons and an EV score for threshold tuning."""
    job = prod["farm_mysmartpros_stuffed"]
    assert job.fit_reasons
    assert job.ev_score >= 0.0


def test_fixture_file_carries_no_credential_shaped_strings():
    # The job rows only — the file's own provenance note is documentation.
    rows = json.dumps(_fixture_rows(), ensure_ascii=False).lower()
    for needle in ("api_key", "apikey", "password", "secret", "bearer",
                   "authorization", "app_id", "app_key", "@gmail.com"):
        assert needle not in rows, needle
    for pattern in (r"sk-[a-z0-9]{16,}", r"ghp_[a-z0-9]{20,}",
                    r"aiza[0-9a-z_\-]{30,}", r"[0-9]{9,10}:aa[\w-]{30,}"):
        assert not re.search(pattern, rows), pattern


def test_fixture_urls_carry_no_query_strings():
    """The Adzuna redirect_url embeds utm_source=<the live Adzuna app id>.

    Query strings are inert for every gate under test — dedupe keys strip
    them, domain_tier reads the hostname, and the search-URL junk pattern does
    not match utm params — so stripping them keeps the credential out of the
    repository at zero behavioural cost. This test stops it coming back.
    """
    for row in _fixture_rows():
        parts = urlsplit(row["url"])
        assert parts.query == "", (row["key"], row["url"])
        assert parts.fragment == "", (row["key"], row["url"])


def test_fixture_metadata_is_synthetic_not_private_history():
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["kind"] == "synthetic_regression"
    assert len(payload["jobs"]) == 8
    for row in payload["jobs"]:
        assert "db_before" not in row and "why" not in row
        assert row["expect"] in TERMINAL_VERDICTS
