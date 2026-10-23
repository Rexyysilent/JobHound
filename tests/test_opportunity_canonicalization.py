"""Posting identity and positive retention, using synthetic observations only."""
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import permutations

import pytest

from jobhound.config import CONFIG
from jobhound.models import Job
from jobhound.v41.engine import evaluate_observations, evaluate_raw
from jobhound.v41.models import ListingObservation, SourceKind
from jobhound.v41.provenance import canonicalize_observations


NOW = datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
ROLE = "Bengali AI Evaluator"
DESCRIPTION = (
    "About the role. Review Bengali and English AI responses using supplied "
    "guidelines. Requirements: Bengali and English fluency, attention to "
    "detail and reliable internet. No university degree or prior experience "
    "required. Remote applicants in India may apply. Follow the supplied rubric "
    "and report unclear examples to the project coordinator. Pay USD 20 per hour."
)


@pytest.fixture(params=[False, True], ids=["v42", "v55"])
def policy(request, monkeypatch):
    monkeypatch.setattr(CONFIG.v55, "enabled", request.param)
    return request.param


def observation(key, url="https://jobs.ashbyhq.com/fixture/role", **job_updates):
    values = dict(
        source="ashby", title=ROLE, company="Fixture Labs", url=url,
        description=DESCRIPTION, location="India", is_remote=True,
        region_tags=["india"], posted_at=NOW - timedelta(days=1),
        pay_raw="USD 20 per hour",
    )
    values.update(job_updates)
    job = Job(**values)
    return ListingObservation(
        observation_id=key, source=job.source, job=job,
        normalized_url=url, original_url=url,
        source_kind=SourceKind.ORIGINAL_ATS, source_confidence=0.92,
        content_state="complete", identity_state="exact",
        vacancy_state="verified_open", application_route_state="observed_public_route",
        captured_at=NOW, verified_open_at=NOW,
    )


@pytest.mark.parametrize("urls", [
    ("https://jobs.example.test/roles/one", "https://jobs.example.test/roles/two"),
    ("https://jobs.example.test/roles?job=one", "https://jobs.example.test/roles?job=two"),
    ("https://jobs.example.test/roles/One", "https://jobs.example.test/roles/one"),
    ("https://jobs.example.test/roles?job=One", "https://jobs.example.test/roles?job=one"),
])
def test_distinct_requisitions_keep_separate_batch_independent_ids(policy, urls):
    rows = [observation(str(index), url) for index, url in enumerate(urls)]
    solo = {row.job.url: canonicalize_observations([row])[0].canonical_id for row in rows}
    assert len(set(solo.values())) == 2
    for ordered in (rows, list(reversed(rows))):
        result = canonicalize_observations(ordered)
        assert len(result) == 2
        assert {item.job.url: item.canonical_id for item in result} == solo
        assert len({item.legacy_canonical_id for item in result}) == 1


@pytest.mark.parametrize("updates", [
    {"company": "Different Labs"},
    {"title": "Bengali AI Evaluator II"},
    {"location": "United States", "region_tags": ["us"]},
    {"location": None},
])
def test_same_url_cannot_override_counterparty_title_or_location(policy, updates):
    rows = [observation("first"), observation("second", **updates)]
    for ordered in (rows, list(reversed(rows))):
        result = canonicalize_observations(ordered)
        assert len(result) == 2
        assert len({item.canonical_id for item in result}) == 2
        assert all(len(item.observations) == 1 for item in result)


def test_remote_india_role_survives_beside_richer_us_only_role(policy):
    india = observation("india", "https://jobs.ashbyhq.com/fixture/india")
    us = observation(
        "us", "https://jobs.ashbyhq.com/fixture/us", location="United States",
        region_tags=["us"], description=DESCRIPTION.replace(
            "Remote applicants in India may apply.",
            "This remote position is open only to residents of the United States. "
            "Applicants must live and be authorized to work in the United States.",
        ),
    )
    for ordered in ([india, us], [us, india]):
        result = evaluate_observations(ordered, as_of=NOW)
        assert result.accounting_ok, result.accounting_errors
        assert result.counts["canonical_jobs"] == 2
        by_url = {item.job.url: item for item in result.evaluated}
        assert by_url[india.job.url].decision.action_band.value == "primary"
        assert by_url[us.job.url].decision.action_band.value == "reject"
        assert by_url[india.job.url].job.location == "India"


def test_exact_duplicate_keeps_all_evidence_and_authoritative_original(policy):
    original = observation("original")
    copy = observation("copy", source="jsearch", description=DESCRIPTION * 2)
    copy.source_kind = SourceKind.AGGREGATOR_UNRESOLVED
    copy.source_confidence = 0.5
    result = canonicalize_observations([copy, original])
    assert len(result) == 1
    item = result[0]
    assert item.job.source == "ashby"
    assert item.job.description == DESCRIPTION
    assert item.job.seen_on == ["ashby", "jsearch"]
    assert {row.observation_id for row in item.observations} == {"original", "copy"}
    assert item.field_sources["url"] == "original"


def test_tracking_variants_still_collapse_without_losing_requisition_query(policy):
    url = "https://jobs.example.test/opening?job=One"
    rows = [
        observation("first", url + "&utm_source=fixture"),
        observation("second", url + "&utm_source=another&gclid=synthetic"),
    ]
    result = canonicalize_observations(rows)
    assert len(result) == 1
    assert len(result[0].observations) == 2
    assert result[0].canonical_id == canonicalize_observations([observation("clean", url)])[0].canonical_id


def test_canonical_id_reuses_existing_v55_url_bound_formula(policy):
    row = observation("one")
    item = canonicalize_observations([row])[0]
    legacy = hashlib.sha256(
        b"bengali ai evaluator|company:fixture labs|india"
    ).hexdigest()[:20]
    expected = hashlib.sha256((legacy + "|" + row.job.url).encode()).hexdigest()[:20]
    assert item.canonical_id == expected
    assert item.job.id == expected
    assert item.legacy_canonical_id == legacy


def test_pay_description_and_source_updates_do_not_change_posting_id(policy):
    first = observation("first")
    changed = observation("changed", pay_raw="USD 25 per hour", description=DESCRIPTION + " More detail.")
    assert canonicalize_observations([first])[0].canonical_id == canonicalize_observations([changed])[0].canonical_id


def test_independent_similar_titles_are_not_original_page_lineage(policy):
    original = observation("original")
    copy = observation("copy", "https://aggregator.example.test/roles/copy", source="jsearch")
    copy.source_kind = SourceKind.AGGREGATOR_UNRESOLVED
    result = canonicalize_observations([copy, original])
    assert len(result) == 2
    assert all(len(item.observations) == 1 for item in result)


@pytest.mark.parametrize("identity", ["exact", "corroborated"])
def test_accepted_hydration_lineage_still_joins_original_and_copy(policy, identity):
    copy = observation("copy", "https://aggregator.example.test/roles/copy", source="jsearch")
    copy.source_kind = SourceKind.AGGREGATOR_UNRESOLVED
    child = observation("child")
    child.origin = "hydration"
    child.identity_state = identity
    child.parent_observation_ids = [copy.observation_id]
    result = canonicalize_observations([copy, child])
    assert len(result) == 1
    assert result[0].job.url == child.job.url
    assert result[0].job.seen_on == ["ashby", "jsearch"]
    assert len(result[0].observations) == 2


@pytest.mark.parametrize("identity,kind", [
    ("uncertain", SourceKind.ORIGINAL_ATS),
    ("conflict", SourceKind.ORIGINAL_ATS),
    ("exact", SourceKind.CONTENT_FARM),
])
def test_unaccepted_lineage_cannot_merge_independent_postings(policy, identity, kind):
    copy = observation("copy", "https://aggregator.example.test/roles/copy", source="jsearch")
    copy.source_kind = SourceKind.AGGREGATOR_UNRESOLVED
    child = observation("child")
    child.origin = "hydration"
    child.identity_state = identity
    child.source_kind = kind
    child.parent_observation_ids = [copy.observation_id]
    assert len(canonicalize_observations([copy, child])) == 2


def _raw_row(description, *, title=ROLE, url="https://jobs.ashbyhq.com/fixture/role"):
    return {"source": "ashby", "raw": {
        "id": url.rsplit("/", 1)[-1], "title": title, "_company": "Fixture Labs",
        "jobUrl": url, "location": "India", "isRemote": True,
        "descriptionPlain": description,
        "publishedAt": (NOW - timedelta(days=1)).isoformat(),
    }}


def test_equal_quality_selection_depends_on_content_not_capture_position(policy):
    rows = [
        _raw_row(DESCRIPTION + " Choose project A."),
        _raw_row(DESCRIPTION + " Choose project B.", title=ROLE.upper()),
        _raw_row(DESCRIPTION, url="https://jobs.ashbyhq.com/fixture/other"),
    ]
    selections = set()
    for ordered in permutations(rows):
        result = evaluate_raw(list(ordered), as_of=NOW)
        selected = next(item for item in result.evaluated if item.job.url.endswith("/role"))
        selections.add((selected.canonical.canonical_id, selected.job.title, selected.job.description))
    assert len(selections) == 1


def test_canonical_output_order_is_deterministic(policy):
    rows = [observation(key, f"https://jobs.ashbyhq.com/fixture/{key}") for key in ("b", "a", "c")]
    expected = [item.canonical_id for item in canonicalize_observations(rows)]
    assert len(expected) == 3
    assert expected == sorted(expected)
    for ordered in permutations(rows):
        assert [item.canonical_id for item in canonicalize_observations(list(ordered))] == expected


def test_unusable_urls_do_not_create_duplicate_database_keys(policy):
    rows = [
        observation("first", "", description="First unresolved provider record."),
        observation("second", "", description="Other unresolved provider record."),
    ]
    alone = [canonicalize_observations([row])[0].canonical_id for row in rows]
    together = canonicalize_observations(rows)
    assert len(together) == 2
    assert len(set(alone)) == 2
    assert {item.canonical_id for item in together} == set(alone)


def test_identical_unresolved_observations_retain_evidence_without_key_collision(policy):
    rows = [observation("first", ""), observation("second", "")]
    result = canonicalize_observations(rows)
    assert len(result) == 1
    assert len(result[0].observations) == 2
    assert result[0].canonical_id == canonicalize_observations([rows[0]])[0].canonical_id


@pytest.mark.parametrize("first,second", [
    ({"title": "AI Trainer C++"}, {"title": "AI Trainer C#"}),
    ({"title": "AI Trainer বাংলা"}, {"title": "AI Trainer হিন্দি"}),
    ({"company": "গবেষণা এক"}, {"company": "গবেষণা দুই"}),
    ({"company": "Fixture C++"}, {"company": "Fixture C#"}),
    ({"location": "ঢাকা"}, {"location": "চট্টগ্রাম"}),
])
def test_material_name_characters_keep_identity_and_genuine_legacy_key(policy, first, second):
    rows = [observation("first", **first), observation("second", **second)]
    result = canonicalize_observations(rows)
    assert len(result) == 2
    assert len({item.canonical_id for item in result}) == 2
    # Each pair was conflated by the old ASCII-only identity. Its actual old
    # key must remain available without being reused as the current identity.
    assert len({item.legacy_canonical_id for item in result}) == 1
    assert all(item.legacy_canonical_id != item.canonical_id for item in result)


def test_unicode_company_legacy_key_retains_old_listing_fallback(policy):
    row = observation("one", company="গবেষণা এক")
    expected_legacy = hashlib.sha256(
        b"bengali ai evaluator|listing:jobs.ashbyhq.com:fixture/role|india"
    ).hexdigest()[:20]
    item = canonicalize_observations([row])[0]
    assert item.legacy_canonical_id == expected_legacy
    assert item.counterparty_key == "company:গবেষণা এক"
    current_base = hashlib.sha256(
        "bengali ai evaluator|company:গবেষণা এক|india".encode()
    ).hexdigest()[:20]
    assert item.canonical_id == hashlib.sha256(
        (current_base + "|" + row.job.url).encode()
    ).hexdigest()[:20]


def test_canonically_equivalent_unicode_names_remain_duplicate_observations(policy):
    rows = [
        observation("first", title="Évaluateur AI"),
        observation("second", title="E\u0301VALUATEUR AI"),
    ]
    result = canonicalize_observations(rows)
    assert len(result) == 1
    assert len(result[0].observations) == 2


def test_capture_timestamps_do_not_change_unresolved_content_identity(policy):
    first = observation("first", "")
    later = first.model_copy(update={
        "observation_id": "later",
        "captured_at": NOW + timedelta(minutes=1),
        "verified_open_at": NOW + timedelta(minutes=1),
    }, deep=True)
    assert canonicalize_observations([first])[0].canonical_id == canonicalize_observations([later])[0].canonical_id
