"""P3 fixtures: decay-to-prior, review-level evidence, incident detection
(corroboration rules), the weekly report, LLM-pass parsing/gating, and the
SERP normalizer. Registry writes run against tmp copies."""
import asyncio
import json
import shutil
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.normalize import normalize
from jobhound.store import Store
from jobhound.trust.incidents import NewsItem, detect, news_query_name, parse_rss
from jobhound.trust.registry import clear_caches, default_registry, load_registry, load_registry_raw, registry_path
from jobhound.trust.registry import save_registry_raw
from jobhound.trust.update import apply_decay, apply_review


@pytest.fixture
def tmp_registry(tmp_path):
    dst = tmp_path / "platform_registry.yaml"
    shutil.copy(registry_path(), dst)
    yield dst
    clear_caches()


# ── decay-to-prior (§8.3) ───────────────────────────────────────────────────

@pytest.fixture
def idle_decay_registry(tmp_registry):
    # Freeze the documented historical conditions; the operator's live registry
    # accumulates feedback/decay and is not a stable unit-test fixture.
    data = load_registry_raw(tmp_registry)
    for entry in data['platforms'].values():
        entry['last_reviewed'] = date(2026, 7, 2)
    entry = data['platforms']['outlier']
    entry['dims'] = {key: 0.42 for key in entry['dims']}
    entry['prior'] = 0.45
    save_registry_raw(data, tmp_registry)
    return tmp_registry


def test_decay_drifts_idle_platform_toward_prior(idle_decay_registry):
    tmp_registry = idle_decay_registry
    today = date(2026, 10, 2)  # seeds reviewed 2026-07-02 → 92 idle days
    changed = apply_decay(registry_file=tmp_registry, today=today)
    # outlier: composite 0.42 vs prior 0.45 → drifts UP (goodwill isn't the
    # only thing that fossilizes); drift cap = 0.01 × 92/30 ≈ 0.031 ≥ gap.
    assert "outlier" in changed
    reg = load_registry(tmp_registry)
    assert reg["outlier"].trust == pytest.approx(0.45, abs=0.005)
    ev = load_registry_raw(tmp_registry)["platforms"]["outlier"]["evidence"][-1]
    assert ev["class"] == "decay"


def test_decay_skips_recently_touched(idle_decay_registry):
    tmp_registry = idle_decay_registry
    today = date(2026, 7, 20)  # 18 days < min_idle_days 30
    assert apply_decay(registry_file=tmp_registry, today=today) == []


def test_decay_is_rate_limited(idle_decay_registry):
    tmp_registry = idle_decay_registry
    today = date(2026, 8, 2)   # 31 idle days → max drift ≈ 0.0103
    apply_decay(registry_file=tmp_registry, today=today)
    reg = load_registry(tmp_registry)
    # outlier gap is ~0.03; one month may close at most ~0.0103 of it
    assert 0.42 < reg["outlier"].trust < 0.435


# ── review levels (class 3) ─────────────────────────────────────────────────

def test_review_delta_is_capped(tmp_registry):
    res = apply_review("remotasks", 2.2, scale=5, source="trustpilot",
                       registry_file=tmp_registry)
    assert res.delta == pytest.approx(-0.03, abs=0.002)   # capped at §8.3 max
    res2 = apply_review("prolific", 4.6, scale=5, registry_file=tmp_registry)
    assert res2.delta == pytest.approx(0.03, abs=0.002)   # capped positive
    res3 = apply_review("mindrift", 2.6, scale=5, registry_file=tmp_registry)
    assert abs(res3.delta) < 0.015                        # near-midpoint ≈ noise
    ev = load_registry_raw(tmp_registry)["platforms"]["remotasks"]["evidence"][-1]
    assert ev["class"] == "aggregate_reviews" and "trustpilot" in ev["note"]


# ── incident detection ──────────────────────────────────────────────────────

_RSS_FIXTURE = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Outlier contractors report platform not paying for June work</title>
<link>https://news.example.com/a1</link><source url="x">TechCrunch</source>
<pubDate>{d1}</pubDate></item>
<item><title>Mass deactivations hit Outlier workers without explanation</title>
<link>https://news.example.com/a2</link><source url="x">Rest of World</source>
<pubDate>{d1}</pubDate></item>
<item><title>Outlier not paying freelancers, workers say</title>
<link>https://news.example.com/a3</link><source url="x">The Verge</source>
<pubDate>{d1}</pubDate></item>
<item><title>Gig platforms in general face scrutiny</title>
<link>https://news.example.com/a4</link><source url="x">Wired</source>
<pubDate>{d1}</pubDate></item>
</channel></rss>"""


def _fixture_items(days_ago=1):
    d1 = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")
    return parse_rss(_RSS_FIXTURE.format(d1=d1))


def test_parse_rss_extracts_items():
    items = _fixture_items()
    assert len(items) == 4
    assert items[0].source == "TechCrunch"
    assert items[0].published is not None


def test_corroborated_incident_fires_on_dominant_dimension():
    hit = detect("Outlier", _fixture_items(), seen_urls=set())
    assert hit is not None
    dim, evidence = hit
    # "not paying" carried by TechCrunch + The Verge (2 distinct outlets) →
    # corroborated; the single-outlet deactivation story alone would not fire.
    assert dim == "payment_reliability"
    assert len(evidence) == 2
    assert {i.source for i in evidence} == {"TechCrunch", "The Verge"}


def test_single_outlet_never_corroborates():
    items = [i for i in _fixture_items() if i.source == "TechCrunch"]
    assert detect("Outlier", items, seen_urls=set()) is None


def test_seen_urls_do_not_refire():
    items = _fixture_items()
    seen = {i.link for i in items}
    assert detect("Outlier", items, seen_urls=seen) is None


def test_stale_items_ignored():
    items = _fixture_items(days_ago=CONFIG.incidents.lookback_days + 10)
    assert detect("Outlier", items, seen_urls=set()) is None


def test_platform_must_be_story_subject():
    assert detect("Mercor", _fixture_items(), seen_urls=set()) is None


def test_news_query_name_strips_disambiguator():
    reg = default_registry()
    assert news_query_name(reg["outlier"]) == "Outlier"
    assert news_query_name(reg["mercor"]) == "Mercor"


# ── weekly report ───────────────────────────────────────────────────────────

def test_weekly_report_covers_trust_and_rejects(tmp_path):
    from jobhound.models import Job
    from jobhound.notify.report import build_weekly_report
    store = Store(tmp_path / "t.db")
    job = Job(source="t", title="X", url="http://x", verdict="rejected_scam",
              scam_flags=["asks-for-money"])
    job.id = "r1"
    store.upsert(job)
    report = build_weekly_report(store, days=7)
    store.close()
    assert report is not None and "reject pile" in report
    assert "scam 1" in report and "asks-for-money" in report


# ── LLM second pass ─────────────────────────────────────────────────────────

def _gemini_response(items, status=200, retry_delay=None):
    if status == 200:
        body = {"candidates": [{"content": {"parts": [{"text": json.dumps(items)}]}}]}
    else:
        details = []
        if retry_delay is not None:
            details.append({
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": retry_delay,
            })
        body = {"error": {"code": status, "status": "RESOURCE_EXHAUSTED",
                          "details": details}}
    return httpx.Response(status, json=body,
                          request=httpx.Request("POST", "https://example.test"))


def _install_fake_gemini(monkeypatch, responses):
    from jobhound.filters import scam_llm
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return responses.pop(0)

    monkeypatch.setattr(scam_llm.httpx, "AsyncClient", FakeClient)
    return calls


class _FakeBudget:
    def __init__(self, calls=0):
        self.calls = calls

    def llm_calls_today(self):
        return self.calls

    def count_llm_call(self):
        self.calls += 1
        return self.calls


def _llm_job(number):
    from jobhound.models import Job
    return Job(id=f"job-{number}", source="t", title=f"Role {number}",
               company="Acme", url=f"https://example.test/{number}",
               description="A normal remote job description. " * 20,
               scam_score=0.5)


def _result(job, likelihood=0.8):
    return {"id": job.id, "scam_likelihood": likelihood,
            "reasons": ["suspicious payment request"]}


def test_llm_batch_json_parsing_is_strict():
    from jobhound.filters.scam_llm import _parse_llm_batch
    good = _gemini_response([
        {"id": "a", "scam_likelihood": 0.85,
         "reasons": ["asks for deposit"]},
    ]).json()
    assert _parse_llm_batch(good) == {"a": (0.85, ["asks for deposit"])}
    assert _parse_llm_batch({"candidates": []}) == {}
    bad_json = {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}
    assert _parse_llm_batch(bad_json) == {}
    wrong_shape = _gemini_response(
        {"id": "a", "scam_likelihood": 0.5, "reasons": []}).json()
    assert _parse_llm_batch(wrong_shape) == {}


def test_45_jobs_use_three_structured_batch_calls(monkeypatch, tmp_path):
    from jobhound.config import ScamLlmCfg
    from jobhound.filters.scam_llm import second_pass
    from jobhound.settings import settings

    jobs = [_llm_job(i) for i in range(45)]
    responses = [
        _gemini_response([_result(job) for job in jobs[:20]]),
        _gemini_response([_result(job) for job in jobs[20:40]]),
        _gemini_response([_result(job) for job in jobs[40:]]),
    ]
    calls = _install_fake_gemini(monkeypatch, responses)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    cfg = ScamLlmCfg(model="gemini-2.5-flash", batch_size=20,
                     max_jobs_per_run=60, min_interval_seconds=9.5,
                     max_retries=0)
    budget = _FakeBudget()
    checked = asyncio.run(second_pass(
        jobs, cfg, sleep=no_sleep, pending_path=tmp_path / "pending.json",
        store=budget,
    ))

    assert checked == 45
    assert len(calls) == 3
    assert budget.calls == 3
    batch_sizes = []
    for url, kwargs in calls:
        assert "gemini-2.5-flash:generateContent" in url
        generation = kwargs["json"]["generationConfig"]
        assert generation["responseMimeType"] == "application/json"
        assert generation["responseSchema"]["type"] == "ARRAY"
        prompt = kwargs["json"]["contents"][0]["parts"][0]["text"]
        batch_sizes.append(len(json.loads(prompt.split("Jobs:\n", 1)[1])))
    assert batch_sizes == [20, 20, 5]
    assert sleeps == [9.5, 9.5]
    assert all(not job.llm_pending for job in jobs)
    assert all(job.scam_score == 0.65 for job in jobs)


def test_llm_cannot_lower_authoritative_rules_score(monkeypatch, tmp_path):
    from jobhound.config import ScamLlmCfg
    from jobhound.filters.scam_llm import second_pass
    from jobhound.settings import settings

    job = _llm_job(1)
    responses = [_gemini_response([_result(job, 0.0)])]
    _install_fake_gemini(monkeypatch, responses)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    checked = asyncio.run(second_pass(
        [job],
        ScamLlmCfg(max_retries=0),
        pending_path=tmp_path / "pending.json",
        store=_FakeBudget(),
    ))

    assert checked == 1
    assert job.scam_score == 0.5
    assert any(flag.startswith("llm:0.00") for flag in job.scam_flags)


def test_retryinfo_delay_is_respected_with_jitter(monkeypatch, tmp_path):
    from jobhound.config import ScamLlmCfg
    from jobhound.filters.scam_llm import second_pass
    from jobhound.settings import settings

    job = _llm_job(1)
    responses = [
        _gemini_response([], status=429, retry_delay="12.250s"),
        _gemini_response([_result(job, 0.4)]),
    ]
    calls = _install_fake_gemini(monkeypatch, responses)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    cfg = ScamLlmCfg(max_retries=1, min_interval_seconds=9.5,
                     retry_jitter_seconds=1.0)
    checked = asyncio.run(second_pass(
        [job], cfg, sleep=no_sleep, jitter=lambda _lo, _hi: 0.25,
        pending_path=tmp_path / "pending.json", store=_FakeBudget(),
    ))

    assert checked == 1 and len(calls) == 2
    assert sleeps == [12.25]
    assert not job.llm_pending


def test_persistent_429_is_nonfatal_and_requeued(monkeypatch, tmp_path):
    from jobhound.config import ScamLlmCfg
    from jobhound.filters.scam_llm import second_pass
    from jobhound.settings import settings

    job = _llm_job(7)
    responses = [
        _gemini_response([], status=429),
        _gemini_response([], status=429),
    ]
    _install_fake_gemini(monkeypatch, responses)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    pending_path = tmp_path / "pending.json"
    cfg = ScamLlmCfg(max_retries=1, min_interval_seconds=9.5,
                     retry_backoff_seconds=2.0, retry_jitter_seconds=0.0)
    checked = asyncio.run(second_pass(
        [job], cfg, sleep=no_sleep, pending_path=pending_path,
        store=_FakeBudget(),
    ))

    assert checked == 0
    assert job.scam_score == 0.5             # rules-only verdict survives
    assert job.llm_pending and "llm_pending" in job.scam_flags
    assert json.loads(pending_path.read_text(encoding="utf-8")) == [job.id]
    assert sleeps == [9.5]                   # pacing floor beats 2s fallback


def test_pending_job_is_prioritized_on_next_run(monkeypatch, tmp_path):
    from jobhound.config import ScamLlmCfg
    from jobhound.filters.scam_llm import second_pass
    from jobhound.settings import settings

    fresh, pending = _llm_job(1), _llm_job(2)
    pending_path = tmp_path / "pending.json"
    pending_path.write_text(json.dumps([pending.id]), encoding="utf-8")
    responses = [_gemini_response([_result(pending, 0.2)])]
    calls = _install_fake_gemini(monkeypatch, responses)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")

    async def no_sleep(_delay):
        pass

    cfg = ScamLlmCfg(max_jobs_per_run=1, max_retries=0)
    checked = asyncio.run(second_pass(
        [fresh, pending], cfg, sleep=no_sleep, pending_path=pending_path,
        store=_FakeBudget(),
    ))

    prompt = calls[0][1]["json"]["contents"][0]["parts"][0]["text"]
    sent = json.loads(prompt.split("Jobs:\n", 1)[1])
    assert checked == 1 and sent[0]["id"] == pending.id
    assert not pending.llm_pending
    assert fresh.llm_pending                    # overflow remains queued


def test_missing_retryinfo_uses_jittered_exponential_fallback(monkeypatch, tmp_path):
    from jobhound.config import ScamLlmCfg
    from jobhound.filters.scam_llm import second_pass
    from jobhound.settings import settings

    job = _llm_job(3)
    responses = [
        _gemini_response([], status=429),
        _gemini_response([_result(job)]),
    ]
    _install_fake_gemini(monkeypatch, responses)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    sleeps = []

    async def no_sleep(delay):
        sleeps.append(delay)

    cfg = ScamLlmCfg(max_retries=1, min_interval_seconds=0,
                     retry_backoff_seconds=2.0, retry_jitter_seconds=1.0)
    checked = asyncio.run(second_pass(
        [job], cfg, sleep=no_sleep, jitter=lambda _lo, _hi: 0.4,
        pending_path=tmp_path / "pending.json", store=_FakeBudget(),
    ))
    assert checked == 1 and sleeps == [2.4]


def test_daily_budget_exhaustion_is_nonfatal_and_requeued(monkeypatch, tmp_path):
    from jobhound.config import ScamLlmCfg
    from jobhound.filters.scam_llm import second_pass
    from jobhound.settings import settings

    job = _llm_job(9)
    calls = _install_fake_gemini(monkeypatch, [])
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    pending_path = tmp_path / "pending.json"
    budget = _FakeBudget(calls=40)
    cfg = ScamLlmCfg(max_calls_per_day=40, max_retries=0)

    checked = asyncio.run(second_pass(
        [job], cfg, pending_path=pending_path, store=budget
    ))

    assert checked == 0 and calls == [] and budget.calls == 40
    assert job.llm_pending and job.scam_score == 0.5
    assert json.loads(pending_path.read_text(encoding="utf-8")) == [job.id]


def test_llm_pass_noops_without_key(monkeypatch):
    from jobhound.filters.scam_llm import second_pass
    from jobhound.models import Job
    from jobhound.settings import settings
    monkeypatch.setattr(settings, "gemini_api_key", "")  # a real key lives in .env now
    j = Job(source="t", title="X", url="http://x", scam_score=0.5)
    assert asyncio.run(second_pass([j])) == 0
    assert j.scam_score == 0.5                    # untouched


# ── SERP normalizer ─────────────────────────────────────────────────────────

def test_serp_normalizer_is_thin_but_usable():
    raw = {"title": "Bengali AI Training Jobs - Remote | LinkedIn",
           "link": "https://linkedin.com/jobs/view/1",
           "snippet": "Apply for remote Bengali AI training roles in India."}
    job = normalize({"source": "serp", "raw": raw})
    assert job.source == "serp"
    assert job.is_remote and "bengali" in job.region_tags
    assert job.company is None
