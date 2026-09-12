"""Sidecar persistence for immutable V4.1 runs and transitions."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import CONFIG
from .models import ActionBand, EvaluatedJob, RunResult

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    engine_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    as_of TEXT NOT NULL,
    ruleset_hash TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    trust_registry_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    accounting_errors_json TEXT NOT NULL,
    snapshot_path TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    run_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    publisher_domain TEXT,
    apply_domain TEXT,
    normalized_url TEXT,
    normalization_error TEXT,
    resolution_json TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    normalized_job_json TEXT,
    PRIMARY KEY (run_id, observation_id)
);
CREATE TABLE IF NOT EXISTS canonical_jobs (
    canonical_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    current_job_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_observations (
    run_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    PRIMARY KEY (run_id, canonical_id, observation_id)
);
CREATE TABLE IF NOT EXISTS run_jobs (
    run_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    action_band TEXT NOT NULL,
    terminal_reason TEXT NOT NULL,
    priority_score REAL NOT NULL,
    rank_key_json TEXT NOT NULL,
    priority_key_json TEXT NOT NULL DEFAULT '{}',
    canonical_url TEXT NOT NULL,
    selected_pay_json TEXT,
    assessment_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (run_id, canonical_id)
);
CREATE INDEX IF NOT EXISTS idx_run_jobs_canonical
ON run_jobs(canonical_id, decided_at DESC);
CREATE TABLE IF NOT EXISTS notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    transition TEXT NOT NULL,
    channel TEXT NOT NULL,
    success INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health_state (
    source TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    error_type TEXT,
    last_seen TEXT NOT NULL,
    last_alerted TEXT
);
CREATE TABLE IF NOT EXISTS canonical_id_aliases (
    old_id TEXT PRIMARY KEY,
    new_id TEXT NOT NULL,
    evidence_reference TEXT NOT NULL
);
"""


class V41Store:
    def __init__(self, path: Path | str | None = None):
        configured = Path(path or CONFIG.v41.sidecar_db)
        self.path = configured if configured.is_absolute() else _ROOT / configured
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate_schema()
        self.conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '4')"
        )
        self.conn.commit()

    def _migrate_schema(self) -> None:
        """Apply additive migrations to sidecars created by early V4.1 runs."""
        observation_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(observations)")
        }
        if "resolution_json" not in observation_columns:
            self.conn.execute(
                "ALTER TABLE observations "
                "ADD COLUMN resolution_json TEXT NOT NULL DEFAULT '{}'"
            )
        run_job_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(run_jobs)")
        }
        if "priority_key_json" not in run_job_columns:
            self.conn.execute(
                "ALTER TABLE run_jobs "
                "ADD COLUMN priority_key_json TEXT NOT NULL DEFAULT '{}'"
            )

    def _previous(self, canonical_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT action_band, terminal_reason, canonical_url, selected_pay_json
            FROM run_jobs
            WHERE canonical_id = ?
            ORDER BY decided_at DESC, rowid DESC
            LIMIT 1
            """,
            (canonical_id,),
        ).fetchone()

    def _was_notified(self, canonical_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM notification_events
            WHERE canonical_id = ? AND success = 1
            LIMIT 1
            """,
            (canonical_id,),
        ).fetchone()
        return row is not None

    def annotate_transitions(self, result: RunResult) -> list[EvaluatedJob]:
        """Set deterministic notification transitions before persistence."""
        if result.metadata.engine_version == 'v5.0.0-rc1':
            return self._release_transitions(result)
        notifiable: list[EvaluatedJob] = []
        for item in result.evaluated:
            if item.decision.action_band == ActionBand.REJECT:
                continue
            previous = self._previous(item.canonical.canonical_id)
            transition = "none"
            if previous is None or not self._was_notified(item.canonical.canonical_id):
                transition = "new"
            elif (
                previous["action_band"] == ActionBand.VERIFY.value
                and item.decision.action_band == ActionBand.PRIMARY
            ):
                transition = "promoted"
            elif previous["action_band"] == ActionBand.REJECT.value:
                transition = "restored"
            elif (
                previous["canonical_url"] != item.job.url
                and item.decision.action_band == ActionBand.PRIMARY
            ):
                transition = "canonicalized"
            else:
                old_pay = json.loads(previous["selected_pay_json"] or "null")
                new_pay = (
                    item.assessment.selected_pay.model_dump(mode="json")
                    if item.assessment.selected_pay else None
                )
                if old_pay is None and new_pay is not None:
                    transition = "pay_resolved"
            item.decision.notification_transition = transition
            if transition != "none":
                notifiable.append(item)
        return notifiable

    def _release_transitions(self, result: RunResult) -> list[EvaluatedJob]:
        """Compare with the last delivered facts, not the last scrape or score."""
        notifiable = []
        for item in result.evaluated:
            item.decision.notification_transition = 'none'
            if item.decision.action_band == ActionBand.REJECT or item.decision.lifecycle != 'active':
                continue
            identity = item.canonical.canonical_id
            aliases = [row[0] for row in self.conn.execute('SELECT old_id FROM canonical_id_aliases WHERE new_id=?', (identity,))]
            identities = [identity, *aliases]
            placeholders = ','.join('?' for _ in identities)
            previous = self.conn.execute(f'''SELECT r.* FROM run_jobs r JOIN notification_events n
                ON n.run_id=r.run_id AND n.canonical_id=r.canonical_id
                WHERE n.success=1 AND r.canonical_id IN ({placeholders})
                ORDER BY n.ts DESC,n.id DESC LIMIT 1''', identities).fetchone()
            transition = 'new'
            if previous is not None:
                old_decision = json.loads(previous['decision_json'])
                old_pay = json.loads(previous['selected_pay_json'] or 'null')
                new_pay = item.assessment.selected_pay.model_dump(mode='json') if item.assessment.selected_pay else None
                old_material = self._material_pay(old_pay)
                new_material = self._material_pay(new_pay)
                old_action = old_decision.get('next_action', 'verify')
                if old_decision.get('engine_version') != 'v5.0.0-rc1':
                    old_action = 'apply' if previous['action_band'] == 'primary' else 'verify'
                changed = (
                    old_action != item.decision.next_action
                    or previous['action_band'] != item.decision.action_band.value
                    or old_material != new_material
                )
                transition = 'material_change' if changed else 'none'
            item.decision.notification_transition = transition
            if transition != 'none':
                notifiable.append(item)
        return notifiable

    @staticmethod
    def _material_pay(pay: dict | None) -> dict | None:
        if not pay:
            return None
        # Old sidecars lack the additive V5 amount/unit fields. Reparse the same
        # captured expression so a schema upgrade is not a new compensation offer.
        from .compensation import parse_compensation
        parsed = parse_compensation(str(pay.get('raw') or ''))
        if parsed.amount_low is not None:
            return {key: getattr(parsed, key) for key in ('basis', 'currency', 'amount_low', 'amount_high', 'qualifier')}
        fields = ('basis', 'min_hourly_usd', 'max_hourly_usd', 'fixed_amount_usd', 'currency', 'up_to')
        return {key: pay.get(key) for key in fields}

    def register_identity_crosswalk(self, old_id: str, new_id: str, evidence_reference: str) -> None:
        """Explicit validated identity migration; never infer from company names."""
        if not old_id or not new_id or not evidence_reference:
            raise ValueError('identity crosswalk requires both IDs and supporting evidence')
        with self.conn:
            self.conn.execute('INSERT INTO canonical_id_aliases VALUES(?,?,?)', (old_id, new_id, evidence_reference))

    def annotate_source_health(self, result: RunResult) -> list[dict]:
        """Mark only meaningful source-health transitions as notification-worthy."""
        as_of = result.metadata.as_of
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        for health in result.source_health:
            source = str(health.get("source") or "unknown")
            status = str(health.get("status") or "empty")
            item_count = int(health.get("item_count") or 0)
            error_type = str(health.get("error_type") or "")
            previous = self.conn.execute(
                "SELECT * FROM source_health_state WHERE source = ?",
                (source,),
            ).fetchone()
            transition = "none"
            notify = False
            current_degraded = status in {"partial", "failed"}
            if previous is None:
                if current_degraded:
                    transition = status
                    notify = True
            else:
                prior_status = str(previous["status"])
                prior_degraded = prior_status in {"partial", "failed"}
                prior_count = int(previous["item_count"])
                if prior_degraded and not current_degraded:
                    transition = "recovered"
                    notify = True
                elif prior_status != "failed" and status == "failed":
                    transition = "failed"
                    notify = True
                elif not prior_degraded and current_degraded:
                    transition = status
                    notify = True
                else:
                    last_alerted = (
                        datetime.fromisoformat(previous["last_alerted"])
                        if previous["last_alerted"] else None
                    )
                    cooldown_ok = (
                        last_alerted is None
                        or (as_of - last_alerted).total_seconds()
                        >= CONFIG.v41.source_health_alert_cooldown_hours * 3600
                    )
                    prior_error = str(previous["error_type"] or "")
                    if current_degraded and error_type != prior_error and cooldown_ok:
                        transition = "error_changed"
                        notify = True
                    elif (
                        prior_count > 0
                        and item_count < prior_count
                        and item_count <= prior_count * CONFIG.v41.source_health_drop_ratio
                        and cooldown_ok
                    ):
                        transition = "coverage_drop"
                        notify = True
            health["alert_transition"] = transition
            health["alert_notify"] = notify
        return result.source_health

    def record_run(self, result: RunResult) -> None:
        now = datetime.now(timezone.utc).isoformat()
        status = "complete" if result.accounting_ok else "failed_accounting"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO runs(
                    run_id, engine_version, started_at, as_of, ruleset_hash,
                    profile_hash, trust_registry_hash, config_hash, status,
                    counts_json, accounting_errors_json, snapshot_path
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.metadata.run_id,
                    result.metadata.engine_version,
                    result.metadata.started_at.isoformat(),
                    result.metadata.as_of.isoformat(),
                    result.metadata.ruleset_hash,
                    result.metadata.profile_hash,
                    result.metadata.trust_registry_hash,
                    result.metadata.config_hash,
                    status,
                    json.dumps(result.counts, sort_keys=True),
                    json.dumps(result.accounting_errors),
                    result.metadata.snapshot_path,
                ),
            )
            for health in result.source_health:
                alert_time = (
                    result.metadata.as_of.isoformat()
                    if health.get("alert_notify") else None
                )
                self.conn.execute(
                    """
                    INSERT INTO source_health_state(
                        source, status, item_count, error_type, last_seen, last_alerted
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(source) DO UPDATE SET
                        status=excluded.status,
                        item_count=excluded.item_count,
                        error_type=excluded.error_type,
                        last_seen=excluded.last_seen,
                        last_alerted=COALESCE(
                            excluded.last_alerted, source_health_state.last_alerted
                        )
                    """,
                    (
                        str(health.get("source") or "unknown"),
                        str(health.get("status") or "empty"),
                        int(health.get("item_count") or 0),
                        health.get("error_type"),
                        result.metadata.as_of.isoformat(),
                        alert_time,
                    ),
                )
            for observation in result.observations:
                self.conn.execute(
                    """
                    INSERT INTO observations(
                        run_id, observation_id, source, source_kind,
                        publisher_domain, apply_domain, normalized_url,
                        normalization_error, resolution_json, raw_payload_json,
                        normalized_job_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        result.metadata.run_id,
                        observation.observation_id,
                        observation.source,
                        observation.source_kind.value,
                        observation.publisher_domain,
                        observation.apply_domain,
                        observation.normalized_url,
                        observation.normalization_error,
                        json.dumps({
                            "redirect_trail": observation.redirect_trail,
                            "source_confidence": observation.source_confidence,
                            "hydrated_source": observation.hydrated_source,
                            "attempted": observation.resolution_attempted,
                            "error": observation.resolution_error,
                            "origin": observation.origin,
                            "parent_observation_ids": observation.parent_observation_ids,
                            "captured_at": observation.captured_at.isoformat() if observation.captured_at else None,
                            "content_hash": observation.content_hash,
                            "resolution_state": observation.resolution_state,
                            "content_state": observation.content_state,
                            "identity_state": observation.identity_state,
                            "application_route_state": observation.application_route_state,
                            "vacancy_state": observation.vacancy_state,
                            "task_availability_state": observation.task_availability_state,
                            "retrieval_error": observation.retrieval_error,
                            "truncated": observation.truncated,
                            "verified_open_at": observation.verified_open_at.isoformat() if observation.verified_open_at else None,
                            "extraction_version": observation.extraction_version,
                        }, ensure_ascii=False, sort_keys=True),
                        json.dumps(observation.raw_payload, ensure_ascii=False, sort_keys=True),
                        observation.job.model_dump_json() if observation.job else None,
                    ),
                )
            for item in result.evaluated:
                existing = self.conn.execute(
                    "SELECT first_seen FROM canonical_jobs WHERE canonical_id = ?",
                    (item.canonical.canonical_id,),
                ).fetchone()
                first_seen = existing["first_seen"] if existing else now
                self.conn.execute(
                    """
                    INSERT INTO canonical_jobs(
                        canonical_id, first_seen, last_seen, current_job_json
                    ) VALUES(?,?,?,?)
                    ON CONFLICT(canonical_id) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        current_job_json=excluded.current_job_json
                    """,
                    (
                        item.canonical.canonical_id,
                        first_seen,
                        now,
                        item.job.model_dump_json(),
                    ),
                )
                for observation in item.canonical.observations:
                    self.conn.execute(
                        """
                        INSERT INTO job_observations(run_id, canonical_id, observation_id)
                        VALUES(?,?,?)
                        """,
                        (
                            result.metadata.run_id,
                            item.canonical.canonical_id,
                            observation.observation_id,
                        ),
                    )
                selected_pay = (
                    json.dumps(
                        item.assessment.selected_pay.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if item.assessment.selected_pay else None
                )
                self.conn.execute(
                    """
                    INSERT INTO run_jobs(
                        run_id, canonical_id, action_band, terminal_reason,
                        priority_score, rank_key_json, priority_key_json, canonical_url,
                        selected_pay_json, assessment_json, decision_json, decided_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        result.metadata.run_id,
                        item.canonical.canonical_id,
                        item.decision.action_band.value,
                        item.decision.terminal_reason,
                        item.decision.priority_score,
                        json.dumps(item.decision.rank_key),
                        item.decision.priority_key.model_dump_json(),
                        item.job.url,
                        selected_pay,
                        item.assessment.model_dump_json(),
                        item.decision.model_dump_json(),
                        now,
                    ),
                )

    def mark_notified(
        self,
        run_id: str,
        items: list[EvaluatedJob],
        channel: str,
        success: bool,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            for item in items:
                self.conn.execute(
                    """
                    INSERT INTO notification_events(
                        run_id, canonical_id, transition, channel, success, ts
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        item.canonical.canonical_id,
                        item.decision.notification_transition,
                        channel,
                        int(success),
                        now,
                    ),
                )

    def close(self) -> None:
        self.conn.close()
