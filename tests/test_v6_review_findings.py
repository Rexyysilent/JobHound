"""Regressions for ultrareview findings on the v6 integration branch."""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from jobhound.config import CONFIG
from jobhound.run_context import RunContext
from jobhound.bounded_transport import BoundedTransport, RunBudget, RequestLimits
from jobhound.v41 import requirements
from jobhound.v41.review import capture_review, _exclusive_workspace

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def context(tmp_path):
    cfg = CONFIG.model_copy(deep=True)
    cfg.v55.enabled = True
    return RunContext.capture(config=cfg, workspace=tmp_path, as_of=NOW)


@pytest.mark.parametrize('text', [
    'No specific certification is mandatory, but a degree is a plus',
    'No prior experience is mandatory',
    'No degree is necessary',
    'Certification is not mandatory',
])
def test_negated_mandatory_is_not_required(monkeypatch, text):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    assert requirements._modality(text, default_required=True) == 'explicitly_not_required'


def test_bare_mandatory_still_required(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    assert requirements._modality('A degree is mandatory') == 'required'


@pytest.mark.parametrize('text', [
    'No exceptions: German fluency is mandatory',
    'No remote work; on-site presence is mandatory',
    'No agencies. Fluent German is required',
    'No agencies!\nNative Hindi is required',
])
def test_no_in_earlier_clause_does_not_negate_requirement(monkeypatch, text):
    # "No ..." negates only its own clause, not a requirement stated after it.
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    assert requirements._modality(text) == 'required'


def test_no_negation_spans_commas_within_its_clause(monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    assert requirements._modality('No degree, diploma or certificate is required') == 'explicitly_not_required'


def test_stale_lock_from_crashed_run_is_reclaimed(tmp_path):
    # A crashed process leaves the file behind but no longer holds the OS lock.
    (tmp_path/'.active-run').write_text('12345')
    snapshot, _ = asyncio.run(capture_review(tmp_path, context=context(tmp_path),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'jobs': []}))))
    assert snapshot.exists()
    assert not (tmp_path/'.active-run').exists()


def test_held_lock_blocks_second_capture(tmp_path):
    with _exclusive_workspace(tmp_path):
        with pytest.raises(FileExistsError, match='in use'):
            asyncio.run(capture_review(tmp_path, context=context(tmp_path),
                transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    assert not (tmp_path/'.active-run').exists()


def test_failed_bookkeeping_releases_gates(tmp_path, monkeypatch):
    budget = RunBudget(context(tmp_path), RequestLimits(concurrency=1))
    def broken_finish(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(budget, 'finish', broken_finish)
    def handler(request):
        raise httpx.ConnectError('boom', request=request)
    async def run():
        transport = BoundedTransport(httpx.MockTransport(handler), budget)
        request = httpx.Request('GET', 'https://example.org')
        with pytest.raises((httpx.ConnectError, OSError)):
            await transport.handle_async_request(request)
        assert not budget.gate.locked()
        assert not budget.host_gates['example.org'].locked()
    asyncio.run(run())


def test_lock_held_by_killed_process_is_released(tmp_path):
    import subprocess
    import sys
    holder = subprocess.Popen([sys.executable, '-c',
        'import sys, time; from pathlib import Path; '
        'from jobhound.v41.review import _exclusive_workspace\n'
        'with _exclusive_workspace(Path(sys.argv[1])):\n'
        '    print("held", flush=True); time.sleep(60)', str(tmp_path)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == 'held'
        with pytest.raises(FileExistsError):
            with _exclusive_workspace(tmp_path):
                pass
        holder.kill()  # no finally/cleanup runs in the holder
        holder.wait(timeout=10)
        assert (tmp_path/'.active-run').exists()
        # Windows releases a dead process's byte-range lock during handle
        # teardown, which can lag process exit under load: allow a bounded wait.
        import time
        deadline = time.monotonic() + 5
        while True:
            try:
                with _exclusive_workspace(tmp_path):
                    break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.stdout.close()
