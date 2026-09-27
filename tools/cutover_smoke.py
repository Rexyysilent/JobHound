"""Offline production-cutover rehearsal against an explicit database copy."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhound.config import CONFIG
from jobhound.delivery_outbox import Destination
from jobhound.delivery_transport import DigestTransport
from jobhound.run_context import RunContext, run_scope
from jobhound.v41.engine import recount_result
from jobhound.v41.replay import replay
from jobhound.v41.resolve import _collapse_exact_final_urls
from jobhound.v41.store import V41Store


MARKER = ".jobhound-cutover-smoke"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--snapshot", required=True)
    return parser.parse_args()


def main():
    args = parse_args()
    root = Path(args.root).resolve()
    database = Path(args.database).resolve()
    snapshot = Path(args.snapshot).resolve()
    if not (root / MARKER).is_file():
        raise SystemExit("explicit cutover-smoke marker required")
    if database.parent != root or not database.is_file():
        raise SystemExit("database must be an existing direct child of smoke root")
    if not snapshot.is_file():
        raise SystemExit("snapshot missing")

    production = Path(CONFIG.v41.sidecar_db)
    if not production.is_absolute():
        production = Path(__file__).resolve().parent.parent / production
    if database == production.resolve():
        raise SystemExit("production database is forbidden")

    config = CONFIG.model_copy(deep=True)
    config.v55.enabled = True
    config.v55.production_approved = True
    config.delivery.enabled = True
    config.delivery.production_approved = True
    context = RunContext.capture(
        config=config,
        workspace=root,
        network_allowed=False,
        side_effects_allowed=False,
    )
    with run_scope(context):
        result = replay(snapshot)
    collapsed = _collapse_exact_final_urls(result)
    if collapsed:
        recount_result(result)
    result.metadata.run_id = "cutover-smoke-" + uuid.uuid4().hex
    result.metadata.started_at = datetime.now(timezone.utc)

    targets = [
        Destination.from_address(
            "email", "smtp.gmail.com:465:synthetic-sender:synthetic-recipient"
        ),
        Destination.from_address("telegram", "synthetic-bot:synthetic-chat"),
    ]
    store = V41Store(database)
    try:
        store.enable_delivery_production(
            workspace_id="cutover-smoke",
            profile_id="synthetic-profile",
            approved=True,
        )
        report = store.record_delivery_run(
            result,
            targets,
            now=time.time(),
            card_cap=config.delivery.card_cap,
            status_cap=config.delivery.status_cap,
            adopt_legacy=True,
        )
        transport = DigestTransport(store.delivery)
        previews = []
        for ledger, target in zip(report["destinations"], targets):
            pending = [
                intent_id
                for intent_id in ledger["intent_ids"]
                if store.delivery._owned(intent_id)["status"] == "pending"
            ]
            if pending:
                envelope = transport.prepare(
                    target,
                    pending,
                    now=time.time(),
                    summary_run_id=result.metadata.run_id,
                )
                preview = transport.inspect(envelope)
                previews.append(
                    {
                        "channel": target.channel,
                        "intent_count": len(pending),
                        "body_hash": preview["body_hash"],
                        "body_bytes": len(preview["body"].encode()),
                        "replacement_characters": preview["body"].count("\ufffd"),
                        "part_count": len(preview["parts"]),
                        "status": preview["status"],
                    }
                )
        check = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = store.conn.execute("PRAGMA foreign_key_check").fetchall()
        payload = {
            "source_snapshot": snapshot.name,
            "evaluated": len(result.evaluated),
            "exact_url_duplicates_collapsed": collapsed,
            "decision_accounting_ok": result.accounting_ok,
            "legacy_crosswalk": report.get("legacy_crosswalk"),
            "legacy_baselines_adopted": report.get("legacy_baselines_adopted", 0),
            "destinations": report["destinations"],
            "previews": previews,
            "database_integrity": check,
            "foreign_key_errors": len(foreign_keys),
            "provider_calls": 0,
        }
        print(json.dumps(payload, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
