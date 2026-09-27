"""Explicit offline delivery candidate sharing the opportunity store transaction.

No network or credentials. Acceptance means provider acceptance, never inbox
arrival. Claims are not sends: workers must durably enter `sending` first.
Unknown sends require operator reconciliation. This is NOT exactly-once SMTP.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
import uuid

from .review_audit import canonical_json, material_projection

# notification_events.transition for receipts mirrored from durable delivery,
# so a V4.2 rollback sees them; never re-imported as legacy receipts.
V6_RECEIPT_TRANSITION = 'v6_durable'

def coverage_scope(health):
    """Stable operational identity, independent of result counts and timestamps.

    Discovery is a provider-wide aggregate unless a host scope is supplied.
    Hydration emits one row per adapter/host, so source alone is not unique.
    The versioned structured encoding avoids separator collisions. Existing
    legacy subjects are retained, not silently rebound to a different scope.
    """
    fields = {}
    for key, default in (('source', 'unknown'), ('stage', 'discovery'), ('transport_host', '')):
        value = health.get(key, default)
        if value is None:
            value = default
        if not isinstance(value, str) or len(value) > 253 or any(ord(c) < 32 for c in value):
            raise ValueError('invalid coverage scope')
        value = value.strip().casefold()
        fields[key] = value or default
    fields['transport_host'] = fields['transport_host'].rstrip('.')
    identity = canonical_json(['coverage-v2', fields['source'], fields['stage'], fields['transport_host']])
    return 'coverage:v2:' + hashlib.sha256(identity.encode()).hexdigest(), fields


SCHEMA_VERSION = '2'
_SCHEMA = """
CREATE TABLE delivery_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE delivery_runs(
 workspace TEXT NOT NULL, profile TEXT NOT NULL, run_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, report TEXT NOT NULL,
 PRIMARY KEY(workspace,profile,run_id));
CREATE TABLE delivery_subjects(
 workspace TEXT NOT NULL, profile TEXT NOT NULL, subject TEXT NOT NULL,
 revision TEXT NOT NULL, policy TEXT NOT NULL, last_as_of TEXT NOT NULL,
 PRIMARY KEY(workspace,profile,subject));
CREATE TABLE delivery_revisions(
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL, profile TEXT NOT NULL,
 subject TEXT NOT NULL, sequence INTEGER NOT NULL, run_id TEXT NOT NULL,
 material TEXT NOT NULL, cause TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(workspace,profile,subject,sequence));
CREATE TABLE delivery_aliases(
 workspace TEXT NOT NULL, profile TEXT NOT NULL, alias TEXT NOT NULL,
 subject TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(workspace,profile,alias));
CREATE TABLE delivery_intents(
 id INTEGER PRIMARY KEY, revision TEXT NOT NULL REFERENCES delivery_revisions(id),
 channel TEXT NOT NULL, destination TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN
 ('pending','leased','sending','accepted','uncertain','failed','superseded','legacy_hold')),
 payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL, available REAL NOT NULL,
 token TEXT, lease_until REAL,
 generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
 replaces_intent INTEGER REFERENCES delivery_intents(id),
 UNIQUE(revision,channel,destination,generation));
CREATE TABLE delivery_attempt_events(
 id INTEGER PRIMARY KEY, intent INTEGER NOT NULL REFERENCES delivery_intents(id),
 attempt INTEGER NOT NULL, event TEXT NOT NULL, token TEXT,
 evidence TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE delivery_baselines(
 workspace TEXT NOT NULL, profile TEXT NOT NULL, subject TEXT NOT NULL,
 channel TEXT NOT NULL, destination TEXT NOT NULL, revision TEXT NOT NULL,
 PRIMARY KEY(workspace,profile,subject,channel,destination));
CREATE TABLE delivery_legacy(
 workspace TEXT NOT NULL, profile TEXT NOT NULL, subject TEXT NOT NULL,
 channel TEXT NOT NULL, evidence TEXT NOT NULL,
 PRIMARY KEY(workspace,profile,subject,channel));
CREATE INDEX delivery_ready ON delivery_intents(status,available,id);
CREATE TRIGGER delivery_events_no_update BEFORE UPDATE ON delivery_attempt_events
 BEGIN SELECT RAISE(ABORT,'attempt evidence is append-only'); END;
CREATE TRIGGER delivery_events_no_delete BEFORE DELETE ON delivery_attempt_events
 BEGIN SELECT RAISE(ABORT,'attempt evidence is append-only'); END;
CREATE TRIGGER delivery_revisions_no_update BEFORE UPDATE ON delivery_revisions
 BEGIN SELECT RAISE(ABORT,'revision is immutable'); END;
CREATE TRIGGER delivery_revisions_no_delete BEFORE DELETE ON delivery_revisions
 BEGIN SELECT RAISE(ABORT,'revision is immutable'); END;
"""


@contextmanager
def transaction(conn):
    """Composable transactions: never commit a caller's revision/run half-write."""
    from .run_context import deny_review_side_effect
    deny_review_side_effect('delivery persistence')
    nested = conn.in_transaction
    savepoint = 'delivery_' + uuid.uuid4().hex
    conn.execute('SAVEPOINT ' + savepoint if nested else 'BEGIN IMMEDIATE')
    try:
        yield
        conn.execute('RELEASE ' + savepoint if nested else 'COMMIT')
    except BaseException:
        if nested:
            conn.execute('ROLLBACK TO ' + savepoint)
            conn.execute('RELEASE ' + savepoint)
        elif conn.in_transaction:
            conn.execute('ROLLBACK')
        raise


def identifier(value, field='identifier'):
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(c) < 32 for c in value)):
        raise ValueError('invalid ' + field)
    return value


def clock(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('finite nonnegative time required')
    return float(value)


@dataclass(frozen=True)
class Destination:
    channel: str
    key: str

    @classmethod
    def from_address(cls, channel: str, address: str):
        """Include the sender/account namespace in address for shared transports.

        Store only a hash, not an email address/token. Hashing is not encryption.
        Do not casefold email local parts or conflate Telegram account/chat IDs.
        """
        identifier(channel, 'channel')
        identifier(address, 'destination')
        if channel not in {'email', 'telegram'}:
            raise ValueError('unsupported delivery channel')
        return cls(channel, hashlib.sha256(address.strip().encode()).hexdigest())


class DeliveryOutbox:
    def __init__(self, conn: sqlite3.Connection, workspace: str, profile: str):
        self.conn = conn
        self.workspace = identifier(workspace, 'workspace')
        self.profile = identifier(profile, 'stable profile ID')
        if conn.in_transaction:
            raise ValueError('initialize outside a transaction')
        conn.execute('PRAGMA foreign_keys=ON')
        with transaction(conn):
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_meta'").fetchone()
            if not exists:
                # sqlite complete_statement keeps trigger bodies intact; executescript
                # would implicitly commit the surrounding transaction.
                statement = ''
                for line in _SCHEMA.splitlines(True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        conn.execute(statement)
                        statement = ''
                conn.execute('INSERT INTO delivery_meta VALUES(?,?)', ('version', SCHEMA_VERSION))
            version = conn.execute("SELECT value FROM delivery_meta WHERE key='version'").fetchone()
            if not version or version[0] not in {'1', SCHEMA_VERSION}:
                raise ValueError('unsupported delivery schema')

    def current_revision(self, subject):
        """The subject's current revision ID, or None if it was never staged."""
        row = self.conn.execute('SELECT revision FROM delivery_subjects WHERE workspace=? AND profile=? AND subject=?',
                                (self.workspace, self.profile, subject)).fetchone()
        return row[0] if row else None

    def _has_table(self, name):
        """Cached: a table never disappears once created, so only misses re-query."""
        known = self.__dict__.setdefault('_tables', set())
        if name not in known and self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
            known.add(name)
        return name in known

    def subject(self, canonical_id):
        identity = identifier(canonical_id)
        alias = self.conn.execute('SELECT subject FROM delivery_aliases WHERE workspace=? AND profile=? AND alias=?',
                                  (self.workspace, self.profile, identity)).fetchone()
        return alias[0] if alias else identity

    def register_alias(self, old_id, new_id, *, evidence):
        """Alias only an untracked ID onto one established stream; no guessing/merging."""
        identifier(evidence, 'evidence reference')
        with transaction(self.conn):
            subject = self.subject(old_id)
            new_id = identifier(new_id)
            if subject == new_id:
                raise ValueError('self/cyclic alias')
            current = self.conn.execute('SELECT 1 FROM delivery_subjects WHERE workspace=? AND profile=? AND subject=?',
                                        (self.workspace, self.profile, subject)).fetchone()
            occupied = self.conn.execute('SELECT 1 FROM delivery_subjects WHERE workspace=? AND profile=? AND subject=?',
                                         (self.workspace, self.profile, new_id)).fetchone()
            if not current or occupied or self.subject(new_id) != new_id:
                raise ValueError('ambiguous or already tracked identity; manual reconciliation required')
            self.conn.execute('INSERT INTO delivery_aliases VALUES(?,?,?,?,?)',
                              (self.workspace, self.profile, new_id, subject, evidence))

    def register_legacy_crosswalk(self, old_id, new_id, *, evidence):
        """Move channel-only legacy holds across an exact reviewed identity link.

        Runs inside every production run's staging transaction, so a link that
        cannot be applied is skipped (False), never raised: raising rolled back
        that run and every later one. Skipped cases: the old ID already maps
        elsewhere, either ID is already an alias, or either ID was already
        staged (V6 tracks it; a late crosswalk can no longer be applied safely).
        """
        old_id = identifier(old_id, 'legacy canonical ID')
        new_id = identifier(new_id, 'current canonical ID')
        identifier(evidence, 'crosswalk evidence')
        if old_id == new_id:
            return False
        existing = self.conn.execute(
            '''SELECT subject FROM delivery_aliases
               WHERE workspace=? AND profile=? AND alias=?''',
            (self.workspace, self.profile, old_id),
        ).fetchone()
        if existing:
            return False
        if self.subject(old_id) != old_id or self.subject(new_id) != new_id:
            return False
        if self.conn.execute(
            '''SELECT 1 FROM delivery_subjects
               WHERE workspace=? AND profile=? AND subject IN (?,?)''',
            (self.workspace, self.profile, old_id, new_id),
        ).fetchone():
            return False
        legacy = self.conn.execute(
            '''SELECT channel,evidence FROM delivery_legacy
               WHERE workspace=? AND profile=? AND subject=?''',
            (self.workspace, self.profile, old_id),
        ).fetchall()
        if not legacy:
            return False
        self.conn.execute(
            'INSERT INTO delivery_aliases VALUES(?,?,?,?,?)',
            (self.workspace, self.profile, old_id, new_id, evidence),
        )
        for row in legacy:
            self.conn.execute(
                '''INSERT OR IGNORE INTO delivery_legacy
                   VALUES(?,?,?,?,?)''',
                (self.workspace, self.profile, new_id, row['channel'], row['evidence']),
            )
        self.conn.execute(
            '''DELETE FROM delivery_legacy
               WHERE workspace=? AND profile=? AND subject=?''',
            (self.workspace, self.profile, old_id),
        )
        return True

    def revise(self, subject, material, policy, *, run_id, as_of, now):
        """Caller transaction required. A -> B -> A produces three event IDs."""
        if not self.conn.in_transaction:
            raise RuntimeError('revision and intents require a caller transaction')
        subject = self.subject(subject)
        material, policy = canonical_json(material), canonical_json(policy)
        previous = self.conn.execute('''SELECT s.*,r.material,r.sequence FROM delivery_subjects s
            JOIN delivery_revisions r ON r.id=s.revision
            WHERE s.workspace=? AND s.profile=? AND s.subject=?''',
            (self.workspace, self.profile, subject)).fetchone()
        if previous and as_of < previous['last_as_of']:
            raise ValueError('out-of-order delivery snapshot')
        if previous and previous['material'] == material:
            self.conn.execute('UPDATE delivery_subjects SET policy=?,last_as_of=? WHERE workspace=? AND profile=? AND subject=?',
                              (policy, as_of, self.workspace, self.profile, subject))
            return previous['revision']
        revision = uuid.uuid4().hex
        sequence = previous['sequence'] + 1 if previous else 1
        cause = 'new' if not previous else ('policy_review' if previous['policy'] != policy else 'material_change')
        self.conn.execute('INSERT INTO delivery_revisions VALUES(?,?,?,?,?,?,?,?,?)',
                          (revision, self.workspace, self.profile, subject, sequence, run_id, material, cause, clock(now)))
        self.conn.execute('''INSERT INTO delivery_subjects VALUES(?,?,?,?,?,?)
            ON CONFLICT(workspace,profile,subject) DO UPDATE SET revision=excluded.revision,
            policy=excluded.policy,last_as_of=excluded.last_as_of''',
            (self.workspace, self.profile, subject, revision, policy, as_of))
        if previous:
            # An unsent stale card must never resurrect after a newer closure.
            rows = self.conn.execute('''SELECT i.id FROM delivery_intents i JOIN delivery_revisions r ON r.id=i.revision
                WHERE r.workspace=? AND r.profile=? AND r.subject=? AND i.status IN
                ('pending','leased','failed','legacy_hold')''', (self.workspace,self.profile,subject)).fetchall()
            for row in rows:
                self._event(row['id'], 'superseded', now=now)
                self.conn.execute("UPDATE delivery_intents SET status='superseded',token=NULL,lease_until=NULL WHERE id=?", (row['id'],))
        return revision

    def enqueue(self, revision, destination, payload, *, now, max_attempts=3):
        if not self.conn.in_transaction:
            raise RuntimeError('revision and intents require a caller transaction')
        if not isinstance(destination, Destination) or destination.channel not in {'email','telegram'}:
            raise ValueError('validated destination required')
        if len(destination.key) != 64 or any(c not in '0123456789abcdef' for c in destination.key):
            raise ValueError('destination must be a hashed target')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError('max_attempts must be 1..10')
        row = self.conn.execute('SELECT * FROM delivery_revisions WHERE id=? AND workspace=? AND profile=?',
                                (revision, self.workspace, self.profile)).fetchone()
        if not row:
            raise ValueError('revision outside owner')
        # A 'policy_review' cause (the material changed while the policy
        # fingerprint also changed) is recorded for audit, not held: the
        # fingerprint includes routinely changing trust/config hashes, and a
        # hold was never released, freezing real updates indefinitely.
        if self.current_revision(row['subject']) != revision:
            raise ValueError('cannot enqueue stale revision')
        encoded = canonical_json(payload)
        if len(encoded.encode()) > 1_048_576:
            raise ValueError('payload exceeds 1 MiB')
        existing = self.conn.execute('SELECT status FROM delivery_intents WHERE revision=? AND channel=? AND destination=? ORDER BY id DESC LIMIT 1',
                                     (revision,destination.channel,destination.key)).fetchone()
        if existing:
            return existing[0]
        legacy = self.conn.execute('SELECT 1 FROM delivery_legacy WHERE workspace=? AND profile=? AND subject=? AND channel=?',
                                   (self.workspace,self.profile,row['subject'],destination.channel)).fetchone()
        status = 'legacy_hold' if legacy else 'pending'
        self.conn.execute('''INSERT INTO delivery_intents(revision,channel,destination,status,payload,max_attempts,available)
            VALUES(?,?,?,?,?,?,?)''', (revision,destination.channel,destination.key,status,encoded,max_attempts,clock(now)))
        return status

    def _owned(self, intent):
        row = self.conn.execute('''SELECT i.*,r.workspace,r.profile,r.subject,r.sequence FROM delivery_intents i
            JOIN delivery_revisions r ON r.id=i.revision WHERE i.id=? AND r.workspace=? AND r.profile=?''',
            (intent,self.workspace,self.profile)).fetchone()
        if row is None:
            raise ValueError('intent outside owner')
        return row

    def _event(self, intent, event, *, evidence='', now):
        row = self._owned(intent)
        self.conn.execute('INSERT INTO delivery_attempt_events(intent,attempt,event,token,evidence,created) VALUES(?,?,?,?,?,?)',
                          (intent,row['attempts'],event,row['token'],evidence,clock(now)))

    def _reserved(self, intent):
        return bool(self._has_table('delivery_envelope_items') and self.conn.execute('SELECT 1 FROM delivery_envelope_items WHERE intent=?', (intent,)).fetchone())

    def _subject_reserved(self, row, *, except_envelope=''):
        if not self._has_table('delivery_envelope_items'):
            return False
        return bool(self.conn.execute('''SELECT 1 FROM delivery_envelope_items m
            JOIN delivery_envelopes e ON e.id=m.envelope
            JOIN delivery_intents i ON i.id=m.intent JOIN delivery_revisions r ON r.id=i.revision
            WHERE r.workspace=? AND r.profile=? AND r.subject=? AND i.channel=? AND i.destination=?
            AND e.id!=? AND e.status IN ('pending','leased','uncertain')''',
            (self.workspace,self.profile,row['subject'],row['channel'],row['destination'],except_envelope)).fetchone())

    def _single_only(self, intent):
        if self._reserved(intent):
            raise ValueError('envelope owns this intent; use per-part receipts')

    def _expire(self, now):
        rows = self.conn.execute('''SELECT i.id,i.status FROM delivery_intents i JOIN delivery_revisions r ON r.id=i.revision
            WHERE r.workspace=? AND r.profile=? AND i.status IN ('leased','sending') AND i.lease_until<=?''',
            (self.workspace,self.profile,now)).fetchall()
        for row in rows:
            if self._reserved(row['id']):
                continue
            state = 'pending' if row['status'] == 'leased' else 'uncertain'
            self._event(row['id'], 'lease_expired_' + state, now=now)
            self.conn.execute('UPDATE delivery_intents SET status=?,token=NULL,lease_until=NULL WHERE id=?', (state,row['id']))

    def claim_next(self, destination, *, now, lease_seconds=60):
        now, lease_seconds = clock(now), clock(lease_seconds)
        if not 0 < lease_seconds <= 3600:
            raise ValueError('lease must be in (0,3600]')
        with transaction(self.conn):
            self._expire(now)
            rows = self.conn.execute('''SELECT i.id FROM delivery_intents i JOIN delivery_revisions r ON r.id=i.revision
                WHERE r.workspace=? AND r.profile=? AND i.channel=? AND i.destination=?
                AND i.status='pending' AND i.available<=? AND i.attempts<i.max_attempts
                AND NOT EXISTS(SELECT 1 FROM delivery_intents b JOIN delivery_revisions br ON br.id=b.revision
                    WHERE br.workspace=r.workspace AND br.profile=r.profile AND br.subject=r.subject
                    AND b.channel=i.channel AND b.destination=i.destination
                    AND b.status IN ('leased','sending','uncertain','legacy_hold')) ORDER BY i.id''',
                (self.workspace,self.profile,destination.channel,destination.key,now)).fetchall()
            row = next((r for r in rows if not self._reserved(r['id'])
                        and not self._subject_reserved(self._owned(r['id']))), None)
            if not row:
                return None
            token = uuid.uuid4().hex
            self.conn.execute("UPDATE delivery_intents SET status='leased',token=?,lease_until=? WHERE id=?", (token,now+lease_seconds,row['id']))
            self._event(row['id'],'claimed',now=now)
            return dict(self._owned(row['id']))

    def _lease(self, intent, token, status, now):
        row = self._owned(intent)
        if row['token'] != token or row['status'] != status or row['lease_until'] <= now:
            raise ValueError('stale or invalid lease')
        return row

    def begin_send(self, intent, token, *, now):
        with transaction(self.conn):
            self._single_only(intent)
            self._lease(intent,token,'leased',clock(now))
            self.conn.execute("UPDATE delivery_intents SET status='sending',attempts=attempts+1 WHERE id=?", (intent,))
            self._event(intent,'send_started',now=now)

    def _accept(self, row):
        self.conn.execute('''INSERT INTO delivery_baselines VALUES(?,?,?,?,?,?)
            ON CONFLICT(workspace,profile,subject,channel,destination) DO UPDATE SET revision=excluded.revision
            WHERE (SELECT sequence FROM delivery_revisions WHERE id=excluded.revision) >
                  (SELECT sequence FROM delivery_revisions WHERE id=delivery_baselines.revision)''',
            (self.workspace,self.profile,row['subject'],row['channel'],row['destination'],row['revision']))
        self._record_rollback_receipt(row)

    def backfill_rollback_receipts(self):
        """Idempotently mirror accepted intents that predate receipt mirroring."""
        if not self._has_table('notification_events'):
            return 0
        with transaction(self.conn):
            rows = self.conn.execute(
                '''SELECT DISTINCT r.run_id, r.subject, i.channel
                   FROM delivery_intents i JOIN delivery_revisions r ON r.id=i.revision
                   WHERE r.workspace=? AND r.profile=? AND i.status='accepted'
                   AND r.subject NOT LIKE 'coverage:%' ''',
                (self.workspace, self.profile)).fetchall()
            return sum(self._write_rollback_receipt(run_id, subject, channel)
                       for run_id, subject, channel in rows)

    def _record_rollback_receipt(self, row):
        """Mirror acceptance into the V4.2 notification history for rollback.

        Only job subjects: coverage/status items are not jobs V4.2 could resend.
        """
        if row['subject'].startswith('coverage:'):
            return
        if not self._has_table('notification_events'):
            return
        run_id = self.conn.execute('SELECT run_id FROM delivery_revisions WHERE id=?', (row['revision'],)).fetchone()[0]
        self._write_rollback_receipt(run_id, row['subject'], row['channel'])

    def _write_rollback_receipt(self, run_id, subject, channel):
        """One receipt per job ID, run and channel. A V4.2 rollback looks jobs
        up by their current canonical ID, which after register_alias() is an
        alias of the stream, so the stream and every alias get a receipt."""
        aliases = [row[0] for row in self.conn.execute(
            'SELECT alias FROM delivery_aliases WHERE workspace=? AND profile=? AND subject=?',
            (self.workspace, self.profile, subject))]
        written = 0
        for canonical_id in (subject, *aliases):
            if self.conn.execute(
                    '''SELECT 1 FROM notification_events WHERE run_id=? AND canonical_id=?
                       AND channel=? AND transition=?''',
                    (run_id, canonical_id, channel, V6_RECEIPT_TRANSITION)).fetchone():
                continue
            self.conn.execute(
                'INSERT INTO notification_events(run_id, canonical_id, transition, channel, success, ts) VALUES(?,?,?,?,1,?)',
                (run_id, canonical_id, V6_RECEIPT_TRANSITION, channel,
                 datetime.now(timezone.utc).isoformat()))
            written += 1
        return written

    def _settle(self, row, outcome, *, now, evidence):
        if outcome == 'accepted':
            self._accept(row)
            state = 'accepted'
        elif outcome == 'uncertain':
            state = 'uncertain'
        elif outcome == 'permanent_failure':
            state = 'failed'
        else:
            current = self.current_revision(row['subject'])
            state = 'superseded' if current != row['revision'] else ('failed' if row['attempts'] >= row['max_attempts'] else 'pending')
        self._event(row['id'],outcome,evidence=evidence,now=now)
        self.conn.execute('UPDATE delivery_intents SET status=?,available=?,token=NULL,lease_until=NULL WHERE id=?',
                          (state,now+min(3600,30*2**min(row['attempts'],7)) if state=='pending' else now,row['id']))

    def finish(self, intent, token, outcome, *, now, evidence):
        self._single_only(intent)
        if outcome not in {'accepted','not_sent','uncertain','permanent_failure'}:
            raise ValueError('unsupported outcome')
        identifier(evidence, 'opaque evidence reference')
        now = clock(now)
        # Commit expiry even if the late acknowledgement is rejected below.
        with transaction(self.conn):
            self._expire(now)
        with transaction(self.conn):
            row = self._lease(intent,token,'sending',now)
            self._settle(row,outcome,now=now,evidence=evidence)

    def reconcile(self, intent, outcome, *, evidence, reviewed, now):
        self._single_only(intent)
        if reviewed is not True or outcome not in {'accepted','not_sent'}:
            raise ValueError('explicit reviewed reconciliation required')
        identifier(evidence, 'evidence reference')
        with transaction(self.conn):
            row = self._owned(intent)
            if row['status'] not in {'uncertain','legacy_hold'}:
                raise ValueError('intent does not need reconciliation')
            if row['status'] == 'legacy_hold':
                raise ValueError('legacy state requires an explicit historical destination mapping')
            self._settle(row,outcome,now=clock(now),evidence=evidence)

    def import_legacy(self, *, evidence):
        """No destination in old receipts: quarantine, never guess the current one.

        Runs on every production run, so a subject/channel that already has a
        V6 baseline is skipped: re-importing an adopted receipt would hold and
        swallow that job's next genuine material change, every time.
        """
        identifier(evidence, 'migration evidence')
        with transaction(self.conn):
            for row in self.conn.execute('SELECT DISTINCT canonical_id,channel FROM notification_events WHERE success=1 AND transition<>?',
                                         (V6_RECEIPT_TRANSITION,)).fetchall():
                subject = self.subject(row['canonical_id'])
                if self.conn.execute('SELECT 1 FROM delivery_baselines WHERE workspace=? AND profile=? AND subject=? AND channel=?',
                                     (self.workspace,self.profile,subject,row['channel'])).fetchone():
                    continue
                self.conn.execute('INSERT OR IGNORE INTO delivery_legacy VALUES(?,?,?,?,?)',
                                  (self.workspace,self.profile,subject,row['channel'],evidence))

    def adopt_legacy_baselines(self, destinations, *, now,
                               evidence='production_cutover_legacy_receipt'):
        """Adopt the first current revision for an old successful channel receipt.

        Old receipts lack destination and material hashes. Cutover suppresses
        the first current occurrence, records the adopted revision/destination,
        then clears only that subject/channel hold. A later material revision
        can therefore be delivered normally. Holds from an ambiguous URL
        crosswalk are adopted the same way: still no automatic send, but no
        permanent hold (nothing ever released them before).
        """
        targets = list(destinations)
        if any(not isinstance(target, Destination) for target in targets):
            raise ValueError('validated destinations required')
        identifier(evidence, 'migration evidence')
        adopted = 0
        released = set()
        for target in targets:
            rows = self.conn.execute(
                '''SELECT i.*,r.subject FROM delivery_intents i
                   JOIN delivery_revisions r ON r.id=i.revision
                   JOIN delivery_legacy l ON l.workspace=r.workspace
                    AND l.profile=r.profile AND l.subject=r.subject
                    AND l.channel=i.channel
                   WHERE r.workspace=? AND r.profile=? AND i.channel=?
                   AND i.destination=? AND i.status='legacy_hold' ''',
                (self.workspace, self.profile, target.channel, target.key),
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    'INSERT OR IGNORE INTO delivery_baselines VALUES(?,?,?,?,?,?)',
                    (self.workspace, self.profile, row['subject'], row['channel'],
                     row['destination'], row['revision']),
                )
                self._event(
                    row['id'], 'legacy_baseline_adopted',
                    evidence=evidence, now=clock(now),
                )
                self.conn.execute(
                    "UPDATE delivery_intents SET status='accepted',token=NULL,lease_until=NULL WHERE id=?",
                    (row['id'],),
                )
                released.add((row['subject'], row['channel']))
                adopted += 1
        # Clear holds only after every destination on the channel adopted:
        # deleting per destination left later destinations' intents held forever.
        for subject, channel in released:
            self.conn.execute(
                '''DELETE FROM delivery_legacy
                   WHERE workspace=? AND profile=? AND subject=? AND channel=?''',
                (self.workspace, self.profile, subject, channel),
            )
        return adopted

    def inspect(self):
        return [dict(row) for row in self.conn.execute('''SELECT i.*,r.subject,r.sequence,r.cause FROM delivery_intents i
            JOIN delivery_revisions r ON r.id=i.revision WHERE r.workspace=? AND r.profile=? ORDER BY i.id''',
            (self.workspace,self.profile))]


def evaluated_row(item):
    return {'canonical_id': item.canonical.canonical_id, 'job': item.job.model_dump(mode='json'),
            'assessment': item.assessment.model_dump(mode='json'), 'decision': item.decision.model_dump(mode='json'),
            'outcome_projection': item.canonical.outcome_projection.model_dump(mode='json') if item.canonical.outcome_projection else None}


def stage_delivery_run(outbox, result, destinations, *, now, card_cap, status_cap):
    from .v41.digest import build_digest
    from .v41.provenance import sanitize_payload
    if not outbox.conn.in_transaction:
        raise RuntimeError('run and delivery must share a transaction')
    for cap in (card_cap, status_cap):
        if type(cap) is not int or not 0 <= cap <= 100:
            raise ValueError('caps must be integers in [0,100]')
    targets = list(destinations)
    if len(targets) > 64 or len(set(targets)) != len(targets):
        raise ValueError('unique bounded destinations required')
    metadata = result.metadata.model_dump(mode='json')
    policy = {key: metadata.get(key) for key in
              ('profile_hash','ruleset_hash','config_hash','trust_registry_hash','engine_version')}
    if result.metadata.as_of.tzinfo is None:
        raise ValueError('aware delivery timestamp required')
    as_of = result.metadata.as_of.astimezone(timezone.utc).isoformat()
    revisions, rows, active, statuses = {}, {}, [], []
    subjects = [outbox.subject(item.canonical.canonical_id) for item in result.evaluated]
    if any(identity.startswith('coverage:') for identity in subjects):
        raise ValueError('reserved coverage namespace')
    if len(set(subjects)) != len(subjects):
        raise ValueError('two canonical rows resolve to one delivery subject')
    for item in result.evaluated:
        identity = item.canonical.canonical_id
        row = sanitize_payload(evaluated_row(item))
        rows[identity] = row
        revisions[identity] = outbox.revise(identity,material_projection(row),policy,
                                           run_id=result.metadata.run_id,as_of=as_of,now=now)
        if item.decision.action_band.value != 'reject' and item.decision.lifecycle == 'active':
            active.append(item)
        elif item.canonical.outcome_projection is not None:
            statuses.append(item)
    ledgers = []
    for target in targets:
        counts = {'total': len(rows), 'ineligible': len(rows)-len(active)-len(statuses),
                  'already_recorded': 0, 'policy_review': 0, 'selected_cards': 0,
                  'selected_status': 0, 'overflow_cards': 0, 'overflow_status': 0}
        available = []
        policy_changed = 0          # informational; 'policy_review' counts holds (none now)
        for item in [*active,*statuses]:
            revision = revisions[item.canonical.canonical_id]
            cause = outbox.conn.execute('SELECT cause FROM delivery_revisions WHERE id=?',(revision,)).fetchone()[0]
            exists = outbox.conn.execute('SELECT 1 FROM delivery_intents WHERE revision=? AND channel=? AND destination=?',
                                        (revision,target.channel,target.key)).fetchone()
            if cause == 'policy_review' and not exists:
                policy_changed += 1
            if exists:
                counts['already_recorded'] += 1
            else:
                available.append(item)
        active_ids = {item.canonical.canonical_id for item in active}
        cards = [item for item in available if item.canonical.canonical_id in active_ids]
        status_items = [item for item in available if item.canonical.canonical_id not in active_ids]
        # Reuse the real digest's quality, company/platform and top-N controls.
        # A transport cap may reduce the preview, never expand its per-band limits.
        # The digest reads selection inputs frozen in result.metadata.
        digest = build_digest(result,items=cards) if card_cap else None
        if digest is not None and not digest.accounting_ok:
            raise ValueError('presentation ledger failed')
        selected = digest.displayed[:card_cap] if digest is not None else []
        selected_status = sorted(status_items,key=lambda item:item.canonical.canonical_id)[:status_cap]
        counts.update(selected_cards=len(selected),selected_status=len(selected_status),
                      overflow_cards=len(cards)-len(selected),overflow_status=len(status_items)-len(selected_status))
        if sum(value for key,value in counts.items() if key!='total') != counts['total']:
            raise ValueError('delivery selection ledger failed')
        states = {}
        intent_ids = []
        for item in [*selected,*selected_status]:
            identity = item.canonical.canonical_id
            revision = revisions[identity]
            state = outbox.enqueue(revision,target,rows[identity],now=now)
            states[state] = states.get(state,0)+1
            intent_ids.append(outbox.conn.execute(
                '''SELECT id FROM delivery_intents
                   WHERE revision=? AND channel=? AND destination=?
                   ORDER BY id DESC LIMIT 1''',
                (revision, target.channel, target.key),
            ).fetchone()[0])
        ledgers.append({'channel':target.channel,'destination':target.key,
                        'counts':counts,'states':states,'intent_ids':intent_ids,
                        'policy_changed':policy_changed})
    # Operational state has a separate capped stream and denominator, not fake jobs.
    health_revisions = []
    seen_scopes = set()
    for health in result.source_health:
        subject, scope = coverage_scope(health)
        if subject in seen_scopes:
            raise ValueError('duplicate source health scope')
        seen_scopes.add(subject)
        material = {**scope, **{key:health.get(key) for key in ('status','error_type')}}
        revision = outbox.revise(subject,material,policy,run_id=result.metadata.run_id,as_of=as_of,now=now)
        previous = outbox.conn.execute('''SELECT material FROM delivery_revisions
            WHERE workspace=? AND profile=? AND subject=?
            AND sequence < (SELECT sequence FROM delivery_revisions WHERE id=?)
            ORDER BY sequence DESC LIMIT 1''',(outbox.workspace,outbox.profile,subject,revision)).fetchone()
        if material['status'] in {'partial','failed'} or previous and json.loads(previous[0]).get('status') in {'partial','failed'}:
            health_revisions.append((revision,sanitize_payload({'kind':'coverage','health':{**health, **scope}})))
    for target,ledger in zip(targets,ledgers):
        coverage = {'total':len(result.source_health),'unchanged_or_healthy':len(result.source_health)-len(health_revisions),
                    'already_recorded':0,'selected':0,'overflow':0,'policy_review':0}
        for revision,payload in health_revisions:
            exists = outbox.conn.execute('SELECT 1 FROM delivery_intents WHERE revision=? AND channel=? AND destination=?',
                                         (revision,target.channel,target.key)).fetchone()
            if exists:
                coverage['already_recorded'] += 1
            elif coverage['selected'] >= status_cap:
                coverage['overflow'] += 1
            else:
                outbox.enqueue(revision,target,payload,now=now)
                coverage['selected'] += 1
                ledger['intent_ids'].append(outbox.conn.execute(
                    '''SELECT id FROM delivery_intents
                       WHERE revision=? AND channel=? AND destination=?
                       ORDER BY id DESC LIMIT 1''',
                    (revision, target.channel, target.key),
                ).fetchone()[0])
        ledger['coverage'] = coverage
    return {'run_id':result.metadata.run_id,'destinations':ledgers,'revisions':revisions}


async def dispatch_fake(outbox, destination, sender, *, now):
    """Offline harness only. A Boolean send result cannot establish retry safety.

    No existing SMTP/Telegram notifier is connected. Exceptions and legacy False
    results are uncertain; only explicit known-not-sent receipts permit retries.
    """
    row = outbox.claim_next(destination,now=now())
    if row is None:
        return None
    outbox.begin_send(row['id'],row['token'],now=now())
    try:
        receipt = await sender(json.loads(row['payload']))
        outcome, evidence = receipt
        if outcome not in {'accepted','not_sent','uncertain','permanent_failure'}:
            raise ValueError('invalid receipt')
        identifier(evidence)
    except Exception:
        outcome, evidence = 'uncertain', 'sender_exception'
    outbox.finish(row['id'],row['token'],outcome,now=now(),evidence=evidence)
    return outcome
