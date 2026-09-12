"""Batched Gemini second-pass for borderline scam scores.

Rules remain authoritative and cheap. Gemini refines ambiguous listings in
structured batches of up to 20 jobs, so a normal run uses about three calls
instead of one call per job. API failure never blocks the digest: unclassified
jobs keep their rules-only score, receive ``llm_pending``, and are prioritized
from the persisted pending queue on the next run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import httpx

from ..config import CONFIG, ScamLlmCfg
from ..models import Job
from ..settings import settings

log = logging.getLogger("jobhound.scam_llm")

_API = ("https://generativelanguage.googleapis.com/v1beta/models/"
        "{model}:generateContent")
_PENDING_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "llm_pending.json"

_BATCH_PROMPT = """You classify job listings for fraud.

For EVERY input job, return one result with the exact same id. Scam signals:
upfront fees/deposits/kit purchases, gift-card or personal-UPI payment,
reshipping/check-cashing/mystery-shopper archetypes, guaranteed income, and a
recruiter using only free email or WhatsApp/Telegram. A short unpaid
qualification test on a legitimate AI-training platform is not fraud by
itself. Return only the requested JSON array; no prose or markdown.

Jobs:
{jobs_json}
"""

_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "scam_likelihood": {"type": "NUMBER", "minimum": 0, "maximum": 1},
            "reasons": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["id", "scam_likelihood", "reasons"],
    },
}

_DURATION_RE = re.compile(r"^(\d+)(?:\.(\d{1,9}))?s$")
SleepFn = Callable[[float], Awaitable[None]]
JitterFn = Callable[[float, float], float]


class CallBudget(Protocol):
    def llm_calls_today(self) -> int: ...
    def count_llm_call(self) -> int: ...


def _response_text(response: dict) -> str | None:
    try:
        return response["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


def parse_batch_response(text: str) -> dict[str, tuple[float, list[str]]]:
    """Validate a JSON-array response, tolerating an unnecessary code fence."""
    clean = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE
    ).strip()
    try:
        items = json.loads(clean)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(items, list):
        return {}

    parsed: dict[str, tuple[float, list[str]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        request_id = item.get("id")
        reasons = item.get("reasons")
        try:
            likelihood = float(item["scam_likelihood"])
        except (KeyError, TypeError, ValueError):
            continue
        if (not isinstance(request_id, str) or not request_id
                or not isinstance(reasons, list)
                or not math.isfinite(likelihood)):
            continue
        clean_reasons = [str(reason)[:80] for reason in reasons[:3]
                         if isinstance(reason, str) and reason.strip()]
        parsed[request_id] = (
            min(1.0, max(0.0, likelihood)),
            clean_reasons,
        )
    return parsed


def _parse_llm_batch(response: dict) -> dict[str, tuple[float, list[str]]]:
    """Extract and validate Gemini's structured batch response."""
    text = _response_text(response)
    return parse_batch_response(text) if text is not None else {}


def _duration_seconds(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    whole = float(match.group(1))
    fraction = match.group(2)
    if fraction:
        whole += float(f"0.{fraction}")
    return whole


def parse_retry_delay(body: dict) -> float | None:
    """Read google.rpc.RetryInfo.retryDelay from a Gemini error body."""
    details = (body.get("error", {}).get("details", [])
               if isinstance(body, dict) else [])
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            delay = _duration_seconds(
                detail.get("retryDelay", detail.get("retry_delay"))
            )
            if delay is not None:
                return delay
    return None


def _retry_delay_seconds(response: httpx.Response) -> float | None:
    """Read RetryInfo, with the standard Retry-After header as fallback."""
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    delay = parse_retry_delay(body)
    if delay is not None:
        return delay

    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return None


def _load_pending(path: Path) -> set[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read LLM pending queue (%s)", exc)
        return set()
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _save_pending(path: Path, pending_ids: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(sorted(pending_ids), indent=2), encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        # Queue persistence is helpful, never a reason to suppress the digest.
        log.warning("could not save LLM pending queue (%s)", exc)


def _set_pending(job: Job, pending: bool) -> None:
    job.llm_pending = pending
    if pending:
        if "llm_pending" not in job.scam_flags:
            job.scam_flags.append("llm_pending")
    else:
        job.scam_flags = [flag for flag in job.scam_flags if flag != "llm_pending"]


def _in_band(score: float, cfg: ScamLlmCfg) -> bool:
    lo, hi = cfg.band
    return lo <= score <= hi


def _request_jobs(batch: list[Job], batch_number: int) -> tuple[list[dict], dict[str, Job]]:
    records: list[dict] = []
    jobs_by_id: dict[str, Job] = {}
    for offset, job in enumerate(batch):
        request_id = job.id or f"batch-{batch_number}-job-{offset}"
        # Stable ids are unique after dedupe. Guard test/duck-typed inputs too.
        if request_id in jobs_by_id:
            request_id = f"{request_id}-{offset}"
        jobs_by_id[request_id] = job
        records.append({
            "id": request_id,
            "title": job.title,
            "company": job.company or "unknown",
            "text": (job.description or "")[:1500],
        })
    return records, jobs_by_id


async def _post_batch(client: httpx.AsyncClient, url: str, payload: dict,
                      cfg: ScamLlmCfg, sleep: SleepFn,
                      jitter: JitterFn, store: CallBudget) -> dict | None:
    """POST one batch, respecting RetryInfo and bounded fallback retries."""
    for attempt in range(cfg.max_retries + 1):
        if store.llm_calls_today() >= cfg.max_calls_per_day:
            log.warning("Gemini daily call budget exhausted; deferring remaining jobs")
            return None
        # Count the attempt before I/O: timeouts and connection drops still
        # consume our conservative daily safety budget.
        store.count_llm_call()
        response: httpx.Response | None = None
        error: Exception | None = None
        try:
            response = await client.post(
                url,
                headers={"x-goog-api-key": settings.gemini_api_key},
                json=payload,
            )
        except (httpx.HTTPError, OSError) as exc:
            error = exc

        retryable = (error is not None or response is not None
                     and (response.status_code == 429 or response.status_code >= 500))
        if retryable:
            if attempt >= cfg.max_retries:
                status = response.status_code if response is not None else type(error).__name__
                log.warning("Gemini batch deferred after retries (status=%s)", status)
                return None

            retry_info = (_retry_delay_seconds(response)
                          if response is not None and response.status_code == 429
                          else None)
            if retry_info is not None:
                # Google computed this delay for the active quota window;
                # honor it exactly (subject only to our stricter pace floor).
                delay = retry_info
            else:
                noise = jitter(0.0, max(0.0, cfg.retry_jitter_seconds))
                delay = cfg.retry_backoff_seconds * (2 ** attempt) + noise
            delay = max(cfg.min_interval_seconds, delay)
            log.warning("Gemini batch rate-limited; retrying in %.2fs", delay)
            await sleep(delay)
            continue

        if response is None:
            return None
        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            log.warning("Gemini batch deferred (%s)", exc)
            return None
    return None


async def second_pass(jobs: list[Job], cfg: ScamLlmCfg = CONFIG.scam.llm, *,
                      sleep: SleepFn = asyncio.sleep,
                      jitter: JitterFn = random.uniform,
                      pending_path: Path = _PENDING_PATH,
                      store: CallBudget | None = None) -> int:
    """Batch-refine borderline scores; return the number classified by Gemini."""
    if not cfg.enabled or not settings.gemini_api_key:
        return 0

    pending_ids = _load_pending(pending_path)
    borderline = [job for job in jobs if _in_band(job.scam_score, cfg)]
    if not borderline:
        return 0

    # Previously deferred jobs go first. All fresh pipeline jobs are retried by
    # rules every run, and this persisted ordering prevents pending starvation.
    borderline.sort(key=lambda job: (job.id not in pending_ids,))
    selected = borderline[: cfg.max_jobs_per_run]
    overflow = borderline[cfg.max_jobs_per_run:]
    for job in overflow:
        _set_pending(job, True)
        if job.id:
            pending_ids.add(job.id)

    batch_size = max(1, min(20, cfg.batch_size))
    batches = [selected[index:index + batch_size]
               for index in range(0, len(selected), batch_size)]
    checked = 0
    classified_objects: set[int] = set()
    url = _API.format(model=cfg.model)
    owned_store = None
    if store is None:
        try:
            from ..store import Store
            owned_store = Store()
            store = owned_store
        except Exception as exc:  # noqa: BLE001 - budget failure must not block digest
            log.warning("Gemini budget store unavailable; deferring batch (%s)", exc)
            for job in selected:
                _set_pending(job, True)
                if job.id:
                    pending_ids.add(job.id)
            _save_pending(pending_path, pending_ids)
            return 0

    try:
        async with httpx.AsyncClient(timeout=CONFIG.http.timeout_seconds) as client:
            for batch_number, batch in enumerate(batches):
                records, jobs_by_id = _request_jobs(batch, batch_number)
                prompt = _BATCH_PROMPT.format(
                    jobs_json=json.dumps(records, ensure_ascii=False, separators=(",", ":"))
                )
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0.0,
                        "responseMimeType": "application/json",
                        "responseSchema": _RESPONSE_SCHEMA,
                    },
                }
                response = await _post_batch(
                    client, url, payload, cfg, sleep, jitter, store
                )
                if response is None:
                    # A persistent 429/outage is treated as a queue event, not
                    # a pipeline failure. Avoid hammering later batches today.
                    for remaining in batches[batch_number:]:
                        for job in remaining:
                            _set_pending(job, True)
                            if job.id:
                                pending_ids.add(job.id)
                    break

                results = _parse_llm_batch(response)
                for request_id, job in jobs_by_id.items():
                    result = results.get(request_id)
                    if result is None:
                        _set_pending(job, True)
                        if job.id:
                            pending_ids.add(job.id)
                        continue
                    likelihood, reasons = result
                    rules_score = job.scam_score
                    blended_score = (rules_score + likelihood) / 2
                    # Deterministic evidence is authoritative. The optional LLM
                    # may raise suspicion, but can never erase a rules verdict.
                    job.scam_score = round(max(rules_score, blended_score), 3)
                    reason_text = "; ".join(reasons)[:160]
                    flag = f"llm:{likelihood:.2f}"
                    if reason_text:
                        flag += f" {reason_text}"
                    job.scam_flags.append(flag)
                    _set_pending(job, False)
                    if job.id:
                        pending_ids.discard(job.id)
                    classified_objects.add(id(job))
                    checked += 1

                if batch_number + 1 < len(batches):
                    await sleep(cfg.min_interval_seconds)
    except Exception as exc:  # noqa: BLE001 - Gemini must never abort the digest
        log.warning("Gemini batch pass deferred unexpectedly (%s)", exc)
        for job in selected:
            if id(job) in classified_objects:
                continue
            _set_pending(job, True)
            if job.id:
                pending_ids.add(job.id)
    finally:
        _save_pending(pending_path, pending_ids)
        if owned_store is not None:
            owned_store.close()
    return checked
