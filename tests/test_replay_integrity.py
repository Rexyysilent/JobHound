"""Frozen-clock replay is evidence-preserving, not best-effort recovery."""
import json
import os
from pathlib import Path

import pytest

from jobhound.config import CONFIG
from jobhound.v41.engine import decision_fingerprint, evaluate_raw
from jobhound.v41.replay import replay, write_snapshot
from jobhound.v41.review import _write_audit
from jobhound.v41.digest import build_digest
from test_pay_integrity import NOW, DESCRIPTION


def raw_corpus():
    """Synthetic, public fixtures. Not an independently labeled market holdout."""
    specs = [
        ('labor', 'Bengali AI Response Evaluator', 'USD 12 per working hour'),
        ('audio', 'Bengali Audio Transcriptionist', 'USD 80 per recorded audio hour'),
        ('task', 'Bengali AI Response Evaluator', 'USD 15 per paid task-hour'),
        ('monthly', 'Bengali AI Response Evaluator', 'INR 50,000 monthly salary'),
        ('mixed', 'Bengali AI Response Evaluator', 'USD 10 - EUR 20 per hour'),
        ('index', 'Remote AI Jobs: Training and Data Labeling', 'USD 90 per hour'),
    ]
    rows = []
    for key, title, pay in specs:
        rows.append({'source': 'ashby', 'raw': {
            'id': key, '_company': 'Example ' + key, 'title': title,
            'jobUrl': f'https://jobs.ashbyhq.com/example/{key}',
            'location': 'India', 'isRemote': True, 'isListed': True,
            'publishedAt': '2026-09-16T09:00:00Z',
            'descriptionPlain': DESCRIPTION,
            'compensation': {'scrapeableCompensationSalarySummary': pay},
        }})
    rows[-1]['raw']['jobUrl'] = 'https://careers.example.org/jobs/'
    rows[-1]['raw']['descriptionPlain'] = 'Browse all remote jobs and open roles.'
    return rows


@pytest.mark.parametrize('enabled', [False, True])
def test_actual_pipeline_round_trip_is_deterministic(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(CONFIG.v55, 'enabled', enabled)
    raw = raw_corpus()
    result = evaluate_raw(raw, as_of=NOW)
    snapshot = write_snapshot(raw, result, directory=tmp_path)
    first, second = replay(snapshot), replay(snapshot)
    assert decision_fingerprint(first) == decision_fingerprint(second)
    assert decision_fingerprint(result) == decision_fingerprint(first)
    assert first.accounting_ok
    assert build_digest(first, include_all=True).accounting_ok
    assert len(first.observations) == len(raw)
    # Optional explicit evidence export. Ordinary tests write only to tmp_path.
    if os.environ.get('JOBHOUND_EVIDENCE_DIR'):
        target = Path(os.environ['JOBHOUND_EVIDENCE_DIR']) / ('review' if enabled else 'v42')
        target.mkdir(parents=True, exist_ok=True)
        write_snapshot(raw, result, directory=target)
        _write_audit(result, target)
        (target/'digest.md').write_text(build_digest(result, include_all=True).text, encoding='utf-8')
        (target/'receipt.json').write_text(json.dumps({
            'synthetic': True, 'record_count': len(raw),
            'fingerprint': decision_fingerprint(first),
            'repeated_replay_equal': True, 'accounting_ok': first.accounting_ok,
        }, indent=2), encoding='utf-8')


@pytest.mark.parametrize('field', ['hydration_observations', 'account_state_observations'])
def test_malformed_appended_evidence_is_not_silently_discarded(tmp_path, monkeypatch, field):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    raw = raw_corpus()
    snapshot = write_snapshot(raw, evaluate_raw(raw, as_of=NOW), directory=tmp_path)
    lines = snapshot.read_text(encoding='utf-8').splitlines()
    header = json.loads(lines[0]); header[field] = [{'malformed': True}]
    snapshot.write_text('\n'.join([json.dumps(header), *lines[1:]])+'\n', encoding='utf-8')
    with pytest.raises(ValueError, match=f'invalid snapshot {field}'):
        replay(snapshot)
    assert CONFIG.v55.enabled is True


def test_unknown_policy_field_is_not_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG.v55, 'enabled', True)
    raw = raw_corpus()
    snapshot = write_snapshot(raw, evaluate_raw(raw, as_of=NOW), directory=tmp_path)
    lines = snapshot.read_text(encoding='utf-8').splitlines()
    header = json.loads(lines[0]); header['release_policy']['future_uninterpreted_gate'] = True
    snapshot.write_text('\n'.join([json.dumps(header), *lines[1:]])+'\n', encoding='utf-8')
    with pytest.raises(ValueError, match='unsupported snapshot release policy'):
        replay(snapshot)
