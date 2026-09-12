"""sqlite persistence: seen-set + full history (incl. the reject pile for tuning)."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Job

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jobhound.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    source         TEXT,
    title          TEXT,
    company        TEXT,
    url            TEXT,
    verdict        TEXT,
    fit_score      REAL,
    scam_score     REAL,
    pay_min_usd    REAL,
    pay_max_usd    REAL,
    region_tags    TEXT,
    scam_flags     TEXT,
    fit_reasons    TEXT,
    platform_key   TEXT,
    platform_trust REAL,
    effective_usd  REAL,
    ev_score       REAL,
    first_seen     TEXT,
    last_seen      TEXT,
    payload        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_verdict ON jobs(verdict);
CREATE TABLE IF NOT EXISTS feedback_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT,
    platform_key  TEXT,
    job_id        TEXT,
    event         TEXT,
    amount        REAL,
    note          TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
    url           TEXT PRIMARY KEY,
    platform_key  TEXT,
    title         TEXT,
    source        TEXT,
    ts            TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key           TEXT PRIMARY KEY,
    value         TEXT
);
CREATE TABLE IF NOT EXISTS email_uids (
    uid           TEXT PRIMARY KEY,
    ts            TEXT
);
"""

# v2 columns ALTERed onto pre-trust DBs so the seen-set survives the upgrade.
_V2_COLUMNS = {
    "platform_key": "TEXT",
    "platform_trust": "REAL",
    "effective_usd": "REAL",
    "ev_score": "REAL",
}


class Store:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col, ctype in _V2_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ctype}")

    def is_seen(self, job_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM jobs WHERE id = ? LIMIT 1", (job_id,))
        return cur.fetchone() is not None

    def upsert(self, job: Job) -> None:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.conn.execute(
            "SELECT first_seen FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()
        first_seen = existing["first_seen"] if existing else now
        self.conn.execute(
            """
            INSERT INTO jobs (id, source, title, company, url, verdict, fit_score,
                              scam_score, pay_min_usd, pay_max_usd, region_tags,
                              scam_flags, fit_reasons, platform_key, platform_trust,
                              effective_usd, ev_score, first_seen, last_seen, payload)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                verdict=excluded.verdict,
                fit_score=excluded.fit_score,
                scam_score=excluded.scam_score,
                pay_min_usd=excluded.pay_min_usd,
                pay_max_usd=excluded.pay_max_usd,
                region_tags=excluded.region_tags,
                scam_flags=excluded.scam_flags,
                fit_reasons=excluded.fit_reasons,
                platform_key=excluded.platform_key,
                platform_trust=excluded.platform_trust,
                effective_usd=excluded.effective_usd,
                ev_score=excluded.ev_score,
                last_seen=excluded.last_seen,
                payload=excluded.payload
            """,
            (
                job.id, job.source, job.title, job.company, job.url, job.verdict,
                job.fit_score, job.scam_score, job.pay_min_hourly_usd,
                job.pay_max_hourly_usd, ",".join(job.region_tags),
                ",".join(job.scam_flags), " | ".join(job.fit_reasons),
                job.platform_key, job.platform_trust, job.effective_hourly_usd,
                job.ev_score, first_seen, now, job.model_dump_json(),
            ),
        )
        self.conn.commit()

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return cur.fetchone()

    # ── feedback events (HANDOFF v2 §8.5) ───────────────────────────────────

    def add_feedback(self, event: str, platform_key: str | None = None,
                     job_id: str | None = None, amount: float | None = None,
                     note: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO feedback_events (ts, platform_key, job_id, event, amount, note) "
            "VALUES (?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), platform_key, job_id,
             event, amount, note),
        )
        self.conn.commit()

    def count_feedback(self, platform_key: str, event: str) -> int:
        """Prior occurrences of this (platform, event) — drives diminishing positives."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM feedback_events WHERE platform_key = ? AND event = ?",
            (platform_key, event),
        )
        return cur.fetchone()[0]

    def feedback_log(self, platform_key: str | None = None,
                     limit: int = 50) -> list[sqlite3.Row]:
        if platform_key:
            cur = self.conn.execute(
                "SELECT * FROM feedback_events WHERE platform_key = ? "
                "ORDER BY ts DESC LIMIT ?", (platform_key, limit))
        else:
            cur = self.conn.execute(
                "SELECT * FROM feedback_events ORDER BY ts DESC LIMIT ?", (limit,))
        return cur.fetchall()

    # ── incidents (P3 news sweep) ───────────────────────────────────────────

    def seen_incident_urls(self, platform_key: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT url FROM incidents WHERE platform_key = ?", (platform_key,))
        return {r["url"] for r in cur.fetchall()}

    def add_incident(self, platform_key: str, url: str, title: str, source: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO incidents (url, platform_key, title, source, ts) "
            "VALUES (?,?,?,?,?)",
            (url, platform_key, title, source, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    # ── email-offer UID checkpoint (v3 Patch #2) ────────────────────────────

    def email_uid_seen(self, uid: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM email_uids WHERE uid = ?", (uid,))
        return cur.fetchone() is not None

    def mark_email_uid(self, uid: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO email_uids (uid, ts) VALUES (?,?)",
            (uid, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    # ── meta kv (cadence bookkeeping for sweeps/reports) ────────────────────

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def days_since_meta(self, key: str) -> float | None:
        """Days since the ISO timestamp stored under `key`; None if never set."""
        raw = self.get_meta(key)
        if not raw:
            return None
        try:
            then = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - then).total_seconds() / 86400

    # ── external API budgets ────────────────────────────────────────────────

    @staticmethod
    def _llm_calls_key(day: date | None = None) -> str:
        # Gemini's free quota is daily. The host's local day matches the task
        # scheduler and avoids surprising resets during Pacific evenings.
        local_day = day or datetime.now().astimezone().date()
        return f"llm_calls:{local_day.isoformat()}"

    def llm_calls_today(self, day: date | None = None) -> int:
        raw = self.get_meta(self._llm_calls_key(day))
        try:
            return max(0, int(raw or 0))
        except ValueError:
            return 0

    def count_llm_call(self, day: date | None = None) -> int:
        key = self._llm_calls_key(day)
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
            (key,),
        )
        self.conn.commit()
        return self.llm_calls_today(day)

    def recent_rejected(self, limit: int = 100) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE verdict LIKE 'rejected_%'
            ORDER BY last_seen DESC LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()

    def close(self) -> None:
        self.conn.close()
