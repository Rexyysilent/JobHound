"""Offline native-page review through the actual JobHound engine; never sends."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from jobhound.v41.community import project_thread, thread_outcome, saved_thread_observation
from jobhound.v41.engine import evaluate_observations, decision_fingerprint
from jobhound.v41.digest import build_digest
from jobhound.v41.review import _write_audit, _review_enabled
from jobhound.v41.replay import write_snapshot, replay
from jobhound.compact_audit import write_compact_audit
from jobhound.config import CONFIG


@contextmanager
def _offline_network():
    def denied(*args, **kwargs):
        raise RuntimeError('saved-thread review forbids network')
    with patch.object(socket.socket, 'connect', denied), patch.object(socket.socket, 'connect_ex', denied), patch.object(socket, 'create_connection', denied):
        yield


@_offline_network()
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, action='append', required=True, help='Initial topic JSON, then captured post pages in order')
    parser.add_argument('--url', required=True)
    parser.add_argument('--observed-at', required=True)
    parser.add_argument('--selected-post-id', type=int)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    now=datetime.fromisoformat(args.observed_at.replace('Z','+00:00'))
    root=Path(__file__).resolve().parent.parent
    target=args.output.resolve()
    database=Path(CONFIG.v41.sidecar_db)
    database=(database if database.is_absolute() else root/database).resolve()
    if target.exists() or any(target==p or p in target.parents for p in (root/'data',root/'state',root/'runtime',database.parent)):
        raise ValueError('new_nonproduction_review_directory_required')
    if not 1<=len(args.input)<=10 or sum(path.stat().st_size for path in args.input)>2_000_000:
        raise ValueError('thread_input_budget_reached')
    pages=[json.loads(path.read_text(encoding='utf-8')) for path in args.input]
    thread=project_thread(pages,args.url,now,selected_post_id=args.selected_post_id)
    outcome=thread_outcome(thread,pages,selected_post_id=args.selected_post_id)
    with _review_enabled():
        result=evaluate_observations([saved_thread_observation(outcome)],as_of=now,raw_count=0)
        digest=build_digest(result,include_all=True)
        if not result.accounting_ok or not digest.accounting_ok:
            raise RuntimeError('thread_review_accounting_failed')
        target.mkdir(parents=True,exist_ok=False)
        snapshot=write_snapshot([],result,directory=target)
        rerun=replay(snapshot)
        if decision_fingerprint(result)!=decision_fingerprint(rerun):
            raise RuntimeError('saved_thread_replay_changed_decision')
        result.metadata.snapshot_path=str(snapshot)
        _write_audit(result,target)
        write_compact_audit(result,target/'compact.json',snapshot_reference=str(snapshot),
                            snapshot_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest())
    (target/'thread.json').write_text(thread.model_dump_json(indent=2),encoding='utf-8')
    (target/'preview.md').write_text(digest.text,encoding='utf-8')
    manifest={'mode':'offline_native_community_review','url':thread.url,'observed_at':now.isoformat(),
        'input_sha256':{str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in args.input},
        'snapshot_sha256':hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        'runtime_versions':result.metadata.runtime_versions,'profile_hash':result.metadata.profile_hash,
        'registry_hash':result.metadata.trust_registry_hash,'decision_fingerprint':decision_fingerprint(result),
        'accounting_ok':result.accounting_ok,'presentation_ok':digest.accounting_ok,'replay_equal':True,
        'provider_calls':0,'production_store_called':False,'counts':result.counts,
        'limits':['Saved public pages and supplied capture time, not authenticated identity or payment verification',
                  'Partial coverage and unknown hiring/budget/eligibility remain unresolved']}
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({'document':thread.document_type,'complete':thread.complete,'counts':result.counts,'replay_equal':True}))


if __name__=='__main__':
    main()
