"""V3 dedupe must preserve distinct opportunities before applicant gates run."""
from __future__ import annotations

import hashlib
from itertools import permutations

import pytest

from jobhound.dedupe import dedupe, stable_id
from jobhound.models import Job, VERDICT_REJECTED_LOCATION, VERDICT_SURFACED
from jobhound.pipeline import evaluate_jobs, finalize_verdicts


def _job(**changes) -> Job:
    fields = {
        "source": "remotive",
        "title": "AI Trainer - Bengali",
        "company": "Example Data Labs",
        "url": "https://jobs.example.org/roles/bengali",
        "description": "Evaluate Bengali language responses for AI training.",
    }
    fields.update(changes)
    return Job(**fields)


@pytest.mark.parametrize("left,right", [
    ("https://jobs.example.org/roles/one", "https://jobs.example.org/roles/two"),
    ("https://jobs.example.org/opening?id=101", "https://jobs.example.org/opening?id=102"),
    ("https://jobs.example.org/roles/Alpha", "https://jobs.example.org/roles/alpha"),
    ("https://jobs.example.org/roles/one", "https://jobs.example.org/roles/one/"),
    ("https://jobs.example.org/#/roles/one", "https://jobs.example.org/#/roles/two"),
    ("https://jobs.example.org/opening?ref=101", "https://jobs.example.org/opening?ref=102"),
    ("https://jobs.example.org/opening?source=india", "https://jobs.example.org/opening?source=usa"),
    ("https://jobs.example.org/opening?id=1&id=2", "https://jobs.example.org/opening?id=2&id=1"),
    ("https://jobs.example.org/opening?id=ABC", "https://jobs.example.org/opening?id=abc"),
    ("https://jobs.example.org/roles/one", "http://jobs.example.org/roles/one"),
])
def test_distinct_posting_urls_keep_distinct_records_and_ids(left, right):
    a, b = _job(url=left), _job(url=right)
    assert stable_id(a) != stable_id(b)
    assert len(dedupe([a, b])) == 2


@pytest.mark.parametrize("changes", [
    {"location": "India"},
    {"is_remote": True},
    {"region_tags": ["india"]},
])
def test_scope_is_part_of_identity_even_when_posting_url_is_shared(changes):
    unknown, scoped = _job(), _job(**changes)
    assert stable_id(unknown) != stable_id(scoped)
    assert len(dedupe([unknown, scoped])) == 2


def test_two_remote_geographies_do_not_collapse_on_shared_url():
    india = _job(location="India", is_remote=True, region_tags=["india"])
    usa = _job(location="United States", is_remote=True, region_tags=["us"])
    assert len(dedupe([india, usa])) == 2
    assert india.id != usa.id


def test_near_equal_language_titles_do_not_prove_a_duplicate():
    prefix = "Artificial Intelligence Evaluation Training Contributor - "
    english = _job(title=prefix + "English")
    french = _job(title=prefix + "French")
    assert len(dedupe([english, french])) == 2


@pytest.mark.parametrize("left,right", [
    ("বাংলা ভাষা", "হিন্দি ভাষা"),
    ("C++ Developer", "C# Developer"),
])
def test_title_identity_does_not_erase_languages_or_technical_symbols(left, right):
    a, b = _job(title=left), _job(title=right)
    assert stable_id(a) != stable_id(b)
    assert len(dedupe([a, b])) == 2


@pytest.mark.parametrize("tracking", [
    "utm_source=example", "UTM_campaign=fictional", "gclid=synthetic-click",
    "fbclid=synthetic-click", "mc_cid=synthetic-campaign", "mc_eid=synthetic-id",
])
def test_tracking_only_variations_preserve_confirmed_duplicate(tracking):
    original = _job(source="lever", url="https://jobs.example.org/opening?id=101")
    copy = _job(url="https://jobs.example.org/opening?" + tracking + "&id=101")
    assert stable_id(original) == stable_id(copy)
    kept = dedupe([copy, original])
    assert len(kept) == 1
    assert kept[0].source == "lever"
    assert kept[0].seen_on == ["lever", "remotive"]


def test_tracking_filter_preserves_unknown_and_repeated_query_parts():
    a = _job(url="https://jobs.example.org/opening?id=1&utm_medium=mail&id=2&flag")
    b = _job(url="https://jobs.example.org/opening?id=1&id=2&flag")
    different = _job(url="https://jobs.example.org/opening?id=1&id=2&flag=")
    assert stable_id(a) == stable_id(b)
    assert stable_id(b) != stable_id(different)


def test_scope_normalization_is_order_independent_but_not_wildcard_matching():
    a = _job(location="  India ", is_remote=True, region_tags=["INDIA", "bengali", "india"])
    b = _job(location="india", is_remote=True, region_tags=["bengali", "india"])
    unknown = _job(location=None, is_remote=True, region_tags=["bengali", "india"])
    assert stable_id(a) == stable_id(b)
    assert stable_id(a) != stable_id(unknown)
    assert len(dedupe([a, b, unknown])) == 2


def test_known_duplicate_ats_authority_does_not_require_richer_payload():
    original = _job(source="lever", description="Short authoritative text.")
    copy = _job(description="Longer copy of the same exact posting. " * 10, pay_raw="$10/hr")
    rows = dedupe([copy, original])
    assert len(rows) == 1
    assert rows[0].source == "lever"
    assert rows[0].description == "Short authoritative text."
    assert rows[0].seen_on == ["lever", "remotive"]


def test_duplicate_selection_and_output_are_stable_under_permutations():
    inputs = [
        _job(description="First equal-length description."),
        _job(description="Other equal-length description."),
        _job(source="remoteok", description="Other equal-length description."),
        _job(url="https://jobs.example.org/roles/sibling"),
    ]
    snapshots = []
    for ordering in permutations(inputs):
        rows = dedupe([row.model_copy(deep=True) for row in ordering])
        snapshots.append([row.model_dump(mode="json") for row in rows])
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert len(snapshots[0]) == 2


def test_ids_do_not_depend_on_which_other_opportunities_are_in_the_batch():
    india = _job(location="India", is_remote=True)
    usa = _job(location="United States", is_remote=True)
    alone = dedupe([india.model_copy(deep=True)])[0].id
    combined = dedupe([usa, india.model_copy(deep=True)])
    assert next(row.id for row in combined if row.location == "India") == alone


def test_deduplication_is_idempotent_and_keeps_input_object_contract():
    original, copy = _job(source="lever"), _job()
    first = dedupe([copy, original])
    snapshot = [row.model_dump(mode="json") for row in first]
    second = dedupe(first)
    assert first[0] is original
    assert copy.id == original.id
    assert [row.model_dump(mode="json") for row in second] == snapshot


def test_simple_unscoped_https_identity_preserves_existing_seen_key():
    job = _job()
    old_key = "ai trainer bengali|example data labs|jobs.example.org/roles/bengali"
    assert stable_id(job) == hashlib.sha1(old_key.encode("utf-8")).hexdigest()[:16]


def test_india_eligible_role_survives_a_richer_us_role_through_final_verdicts():
    india = _job(
        location="India", is_remote=True, region_tags=["india", "bengali"],
        description="Remote data annotation work in India evaluating Bengali language AI responses.",
    )
    usa = _job(
        source="lever", location="United States", is_remote=True, region_tags=["us"],
        url="https://jobs.example.org/roles/usa-bengali",
        description="Remote role for United States residents only. " * 10,
        pay_raw="$20/hr",
    )
    rows = dedupe([india, usa])
    finalize_verdicts(rows, evaluate_jobs(rows))
    verdicts = {row.location: row.verdict for row in rows}
    assert verdicts == {"India": VERDICT_SURFACED, "United States": VERDICT_REJECTED_LOCATION}
