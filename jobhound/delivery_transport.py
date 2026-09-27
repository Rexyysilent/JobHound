"""Opt-in digest transport, sharing the outbox transaction and frozen payloads.

One email is one envelope; Telegram has individually acknowledged parts.
Provider acceptance is not inbox arrival. Ambiguous sends NEVER auto-retry.
No production migration, scheduler hook, or automatic backlog drain lives here.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from types import SimpleNamespace

from .delivery_outbox import Destination, clock, transaction
from .notify.receipt import DeliveryReceipt

_SCHEMA = """
CREATE TABLE delivery_envelopes(
 id TEXT PRIMARY KEY, workspace TEXT NOT NULL, profile TEXT NOT NULL,
 channel TEXT NOT NULL, destination TEXT NOT NULL, body TEXT NOT NULL,
 body_hash TEXT NOT NULL, status TEXT NOT NULL, token TEXT, lease_until REAL,
 created REAL NOT NULL);
CREATE TABLE delivery_envelope_items(
 intent INTEGER PRIMARY KEY REFERENCES delivery_intents(id),
 envelope TEXT NOT NULL REFERENCES delivery_envelopes(id));
CREATE TABLE delivery_parts(
 envelope TEXT NOT NULL REFERENCES delivery_envelopes(id), ordinal INTEGER NOT NULL,
 body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 available REAL NOT NULL, PRIMARY KEY(envelope,ordinal));
CREATE TABLE delivery_part_events(
 id INTEGER PRIMARY KEY, envelope TEXT NOT NULL, ordinal INTEGER NOT NULL,
 attempt INTEGER NOT NULL, event TEXT NOT NULL, evidence TEXT NOT NULL,
 created REAL NOT NULL, FOREIGN KEY(envelope,ordinal) REFERENCES delivery_parts(envelope,ordinal));
CREATE TRIGGER delivery_parts_evidence_no_update BEFORE UPDATE ON delivery_part_events
 BEGIN SELECT RAISE(ABORT,'part evidence is append-only'); END;
CREATE TRIGGER delivery_parts_evidence_no_delete BEFORE DELETE ON delivery_part_events
 BEGIN SELECT RAISE(ABORT,'part evidence is append-only'); END;
CREATE TRIGGER delivery_envelope_frozen BEFORE UPDATE OF id,workspace,profile,channel,destination,body,body_hash ON delivery_envelopes
 BEGIN SELECT RAISE(ABORT,'envelope is frozen'); END;
CREATE TRIGGER delivery_part_frozen BEFORE UPDATE OF envelope,ordinal,body ON delivery_parts
 BEGIN SELECT RAISE(ABORT,'part is frozen'); END;
CREATE TRIGGER delivery_mapping_frozen BEFORE UPDATE ON delivery_envelope_items
 BEGIN SELECT RAISE(ABORT,'envelope membership is frozen'); END;
CREATE TRIGGER delivery_mapping_no_delete BEFORE DELETE ON delivery_envelope_items
 BEGIN SELECT RAISE(ABORT,'envelope membership is frozen'); END;
"""


def plan_dispatch(ready, current, *, limit):
    """Envelopes to attempt for ONE destination this run, oldest first.

    Retries of provably unsent envelopes come first, but one slot is always
    kept for today's envelope so a destination's backlog can delay only its
    own retries, never today's digest. Callers plan each destination
    separately, so a failing channel cannot starve a healthy one.
    """
    older = [envelope for envelope in ready if envelope != current]
    if current is None:
        return older[:limit]
    return older[:max(0, limit - 1)] + [current]


def render_envelope(rows):
    """Reuse the release card renderer, not a new scoring/filtering policy."""
    from .models import Job
    from .v41.models import Assessment, Decision
    from .v41.digest import _release_entry, compact_coverage
    from .v41.provenance import sanitize_payload
    sections = {'Apply / act first': [], 'Worth a bounded check': [],
                'Status updates': [], 'Coverage updates': []}
    for row in rows:
        payload = sanitize_payload(json.loads(row['payload']))
        if payload.get('kind') == 'coverage':
            health = payload['health']
            scope = health.get('stage') or 'discovery'
            if health.get('transport_host'):
                scope += ' / ' + health['transport_host']
            sections['Coverage updates'].append(
                f"{health.get('source', 'source')} [{scope}]: {health.get('status', 'unknown')} "
                f"({health.get('error_type') or 'no error type'}). Not a claim that jobs closed.")
            continue
        item = SimpleNamespace(job=Job.model_validate(payload['job']),
            assessment=Assessment.model_validate(payload['assessment']),
            decision=Decision.model_validate(payload['decision']))
        decision = item.decision
        section = ('Status updates' if decision.lifecycle != 'active' or decision.action_band.value == 'reject'
                   else 'Apply / act first' if decision.action_band.value == 'primary'
                   else 'Worth a bounded check')
        sections[section].append(_release_entry(item))
    lines = ['JobHound V6 — durable opportunity digest',
             'Frozen queue selection; provider acceptance does not establish inbox arrival.', '']
    for name, cards in sections.items():
        if cards:
            if name == 'Coverage updates':
                cards = compact_coverage(cards)
            lines.extend([name, '', '\n\n'.join(cards), ''])
    return '\n'.join(lines)


class DigestTransport:
    def __init__(self, outbox):
        self.box, self.conn = outbox, outbox.conn
        with transaction(self.conn):
            version = self.conn.execute("SELECT value FROM delivery_meta WHERE key='transport_version'").fetchone()
            if version and version[0] != '1':
                raise ValueError('unsupported transport schema')
            if not version:
                statement = ''
                for line in _SCHEMA.splitlines(True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self.conn.execute(statement)
                        statement = ''
                self.conn.execute("INSERT INTO delivery_meta VALUES('transport_version','1')")

    def _owned(self, envelope):
        row = self.conn.execute('SELECT * FROM delivery_envelopes WHERE id=? AND workspace=? AND profile=?',
            (envelope, self.box.workspace, self.box.profile)).fetchone()
        if row is None:
            raise ValueError('envelope outside owner')
        return row

    def _items(self, envelope):
        return [self.box._owned(row[0]) for row in self.conn.execute(
            'SELECT intent FROM delivery_envelope_items WHERE envelope=? ORDER BY intent', (envelope,))]

    def _parts(self, envelope):
        return self.conn.execute('SELECT * FROM delivery_parts WHERE envelope=? ORDER BY ordinal', (envelope,)).fetchall()

    def _event(self, part, event, evidence, now):
        self.conn.execute('''INSERT INTO delivery_part_events(envelope,ordinal,attempt,event,evidence,created)
            VALUES(?,?,?,?,?,?)''', (part['envelope'], part['ordinal'], part['attempts'], event, evidence, clock(now)))

    def prepare(self, destination, intent_ids, *, now, summary_run_id=None):
        """Exact reviewed intent IDs only. No implicit draining of older pending mail."""
        ids = list(intent_ids)
        if (not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids)
                or any(type(i) is not int or i < 1 for i in ids)):
            raise ValueError('select 1..100 unique intent IDs')
        with transaction(self.conn):
            rows = [self.box._owned(i) for i in ids]
            for row in rows:
                current = self.conn.execute('SELECT revision FROM delivery_subjects WHERE workspace=? AND profile=? AND subject=?',
                    (self.box.workspace, self.box.profile, row['subject'])).fetchone()[0]
                busy = self.conn.execute('''SELECT 1 FROM delivery_intents i JOIN delivery_revisions r ON r.id=i.revision
                    WHERE r.workspace=? AND r.profile=? AND r.subject=? AND i.channel=? AND i.destination=?
                    AND i.status IN ('leased','sending','uncertain','legacy_hold')''',
                    (self.box.workspace, self.box.profile, row['subject'], destination.channel, destination.key)).fetchone()
                if (row['channel'] != destination.channel or row['destination'] != destination.key
                        or row['status'] != 'pending' or row['revision'] != current
                        or row['available'] > clock(now) or busy or self.box._subject_reserved(row)
                        or self.conn.execute('SELECT 1 FROM delivery_envelope_items WHERE intent=?', (row['id'],)).fetchone()):
                    raise ValueError('intent is stale, held, reserved, unavailable, or has a different destination')
            body = render_envelope(rows)
            if summary_run_id is not None:
                from .delivery_outbox import identifier
                identifier(summary_run_id, 'staged run')
                record = self.conn.execute('SELECT report FROM delivery_runs WHERE workspace=? AND profile=? AND run_id=?',
                    (self.box.workspace,self.box.profile,summary_run_id)).fetchone()
                if record is None:
                    raise ValueError('coverage summary requires a staged run in this namespace')
                ledger = next((d for d in json.loads(record[0])['destinations']
                               if d['channel']==destination.channel and d['destination']==destination.key), None)
                if ledger is None:
                    raise ValueError('coverage summary destination mismatch')
                coverage = ledger['coverage']
                keys = ('unchanged_or_healthy','already_recorded','selected','overflow','policy_review')
                if (any(type(coverage.get(k)) is not int or coverage[k]<0 for k in ('total',*keys))
                        or coverage['total'] != sum(coverage[k] for k in keys)):
                    raise ValueError('coverage summary ledger does not reconcile')
                body += (f"\nRun coverage audit ({summary_run_id}): {coverage['total']} source scopes; "
                         f"{coverage['unchanged_or_healthy']} healthy/unchanged, {coverage['already_recorded']} already recorded, "
                         f"{coverage['selected']} staged, {coverage['overflow']} deferred by the status cap, "
                         f"{coverage['policy_review']} held for policy review.\n"
                         "Deferred/held updates are not individually delivered here; full details remain in the local run audit.\n")
            if len(body.encode()) > 128 * 1024:
                raise ValueError('digest exceeds 128 KiB; select fewer intents')
            if destination.channel == 'telegram':
                from .notify.telegram import _chunk
                chunks = _chunk(body)
                parts = [f'({i+1}/{len(chunks)})\n' + chunk for i, chunk in enumerate(chunks)] if len(chunks) > 1 else chunks
            elif destination.channel == 'email':
                parts = [body]
            else:
                raise ValueError('unsupported channel')
            if len(parts) > 32:
                raise ValueError('digest exceeds 32 parts')
            envelope = uuid.uuid4().hex
            self.conn.execute('INSERT INTO delivery_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (envelope, self.box.workspace, self.box.profile, destination.channel, destination.key,
                 body, hashlib.sha256(body.encode()).hexdigest(), 'pending', None, None, clock(now)))
            self.conn.executemany('INSERT INTO delivery_envelope_items VALUES(?,?)', [(i, envelope) for i in ids])
            self.conn.executemany('INSERT INTO delivery_parts VALUES(?,?,?,\'pending\',0,?)',
                [(envelope, i, part, now) for i, part in enumerate(parts)])
            return envelope

    def inspect(self, envelope):
        row = dict(self._owned(envelope))
        row['parts'] = [dict(part) for part in self._parts(envelope)]
        row['intents'] = [item['id'] for item in self._items(envelope)]
        return row

    def ready_envelopes(self, destination, *, now, limit=4):
        """Return a bounded retry queue containing only provably unsent work.

        Uncertain, failed, cancelled and accepted envelopes are intentionally
        absent. ``claim`` remains the authority for freshness and ownership;
        it cancels an obsolete pending envelope rather than sending it.
        """
        if not isinstance(destination, Destination):
            raise ValueError('validated destination required')
        now = clock(now)
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('envelope limit must be 1..16')
        return [row['id'] for row in self.conn.execute(
            '''SELECT e.id FROM delivery_envelopes e
               WHERE e.workspace=? AND e.profile=? AND e.channel=?
               AND e.destination=? AND e.status='pending'
               AND EXISTS(
                 SELECT 1 FROM delivery_parts p
                 WHERE p.envelope=e.id AND p.status='pending' AND p.available<=?
               )
               ORDER BY e.created,e.id LIMIT ?''',
            (self.box.workspace, self.box.profile, destination.channel,
             destination.key, now, limit),
        ).fetchall()]

    def _set_intents(self, envelope, status, *, now):
        for item in self._items(envelope):
            self.box._event(item['id'], 'envelope_' + status, evidence=envelope, now=now)
            self.conn.execute('UPDATE delivery_intents SET status=?,token=NULL,lease_until=NULL WHERE id=?',
                              (status, item['id']))

    def _state(self, envelope, status):
        self.conn.execute('UPDATE delivery_envelopes SET status=?,token=NULL,lease_until=NULL WHERE id=?', (status, envelope))

    def _expire(self, envelope, now):
        row = self._owned(envelope)
        if row['status'] == 'leased' and row['lease_until'] <= now:
            sending = [p for p in self._parts(envelope) if p['status'] == 'sending']
            for part in sending:
                self.conn.execute("UPDATE delivery_parts SET status='uncertain' WHERE envelope=? AND ordinal=?", (envelope, part['ordinal']))
                self._event(part, 'uncertain', 'lease_expired', now)
            status = 'uncertain' if sending else 'pending'
            self._state(envelope, status)
            self._set_intents(envelope, status, now=now)

    def _current(self, envelope):
        return all(item['revision'] == self.conn.execute(
            'SELECT revision FROM delivery_subjects WHERE workspace=? AND profile=? AND subject=?',
            (self.box.workspace, self.box.profile, item['subject'])).fetchone()[0] for item in self._items(envelope))

    def _stale(self, envelope, now):
        if self._current(envelope):
            return False
        # Partial or possibly delivered obsolete envelopes need manual resolution.
        partial = any(p['status'] in {'accepted','sending','uncertain'} for p in self._parts(envelope))
        self._state(envelope, 'uncertain' if partial else 'cancelled')
        if partial:
            self._set_intents(envelope, 'uncertain', now=now)
        else:
            for item in self._items(envelope):
                current = self.conn.execute('SELECT revision FROM delivery_subjects WHERE workspace=? AND profile=? AND subject=?',
                    (self.box.workspace,self.box.profile,item['subject'])).fetchone()[0]
                state = 'pending' if item['revision'] == current else 'superseded'
                self.conn.execute('UPDATE delivery_intents SET status=?,token=NULL,lease_until=NULL WHERE id=?', (state,item['id']))
                self.box._event(item['id'], 'envelope_cancelled', evidence=envelope, now=now)
            # Keep historic mapping; an operator can explicitly abandon/reprepare,
            # never silently expand the old frozen envelope.
        return True

    def claim(self, envelope, destination, *, now, lease_seconds=180):
        now, lease_seconds = clock(now), clock(lease_seconds)
        if not 1 <= lease_seconds <= 3600:
            raise ValueError('lease must be 1..3600 seconds')
        with transaction(self.conn):
            self._expire(envelope, now)
            row = self._owned(envelope)
            if (row['channel'],row['destination']) != (destination.channel,destination.key):
                raise ValueError('configured sender/destination differs from frozen envelope')
            if row['status'] != 'pending' or self._stale(envelope, now):
                return None
            for item in self._items(envelope):
                if self.box._subject_reserved(item,except_envelope=envelope):
                    return None
            part = next((p for p in self._parts(envelope) if p['status'] != 'accepted'), None)
            if part is None or part['available'] > now:
                return None
            token = uuid.uuid4().hex
            self.conn.execute("UPDATE delivery_envelopes SET status='leased',token=?,lease_until=? WHERE id=?", (token,now+lease_seconds,envelope))
            for item in self._items(envelope):
                self.conn.execute("UPDATE delivery_intents SET status='leased',token=?,lease_until=? WHERE id=?", (token,now+lease_seconds,item['id']))
            return token

    def begin_part(self, envelope, token, *, now):
        with transaction(self.conn):
            self._expire(envelope, clock(now))
            row = self._owned(envelope)
            if row['status'] != 'leased' or row['token'] != token:
                return None
            if self._stale(envelope, now):
                return None
            part = next((p for p in self._parts(envelope) if p['status'] != 'accepted'), None)
            if part is None or part['status'] != 'pending' or part['available'] > now:
                return None
            limit = min(item['max_attempts'] for item in self._items(envelope))
            if part['attempts'] >= limit:
                self._state(envelope, 'failed'); self._set_intents(envelope, 'failed', now=now)
                return None
            self.conn.execute("UPDATE delivery_parts SET status='sending',attempts=attempts+1 WHERE envelope=? AND ordinal=?", (envelope,part['ordinal']))
            for item in self._items(envelope):
                self.conn.execute("UPDATE delivery_intents SET status='sending',attempts=attempts+1 WHERE id=?", (item['id'],))
            part = dict(self._parts(envelope)[part['ordinal']])
            self._event(part, 'send_started', 'transport', now)
            return part

    def _settle_part(self, envelope, part, receipt, now):
        limit = min(item['max_attempts'] for item in self._items(envelope))
        state = {'accepted':'accepted','not_sent':'pending','uncertain':'uncertain','permanent_failure':'failed'}[receipt.outcome]
        if state == 'pending' and part['attempts'] >= limit:
            state = 'failed'
        delay = max(receipt.retry_after, min(3600,30*2**min(part['attempts'],7))) if state == 'pending' else 0
        self.conn.execute('UPDATE delivery_parts SET status=?,available=? WHERE envelope=? AND ordinal=?', (state,now+delay,envelope,part['ordinal']))
        self._event(part, receipt.outcome, receipt.evidence, now)
        parts = self._parts(envelope)
        if all(p['status'] == 'accepted' for p in parts):
            self._state(envelope, 'accepted')
            for item in self._items(envelope):
                self.box._settle(item,'accepted',now=now,evidence='envelope:'+envelope)
        else:
            state = state if state in {'uncertain','failed'} else 'pending'
            self._state(envelope, state)
            self._set_intents(envelope, state, now=now)

    def finish_part(self, envelope, token, ordinal, receipt, *, now):
        if not isinstance(receipt, DeliveryReceipt):
            raise ValueError('typed provider receipt required')
        with transaction(self.conn):
            self._expire(envelope, clock(now))
        with transaction(self.conn):
            row = self._owned(envelope)
            part = next((p for p in self._parts(envelope) if p['ordinal'] == ordinal), None)
            if row['status'] != 'leased' or row['token'] != token or part is None or part['status'] != 'sending':
                raise ValueError('stale receipt; reconcile with evidence')
            self._settle_part(envelope,part,receipt,now)

    def reconcile_part(self, envelope, ordinal, outcome, *, reviewed, evidence, now):
        if reviewed is not True or outcome not in {'accepted','not_sent'}:
            raise ValueError('explicit reviewed provider evidence required')
        receipt = DeliveryReceipt(outcome,evidence)
        with transaction(self.conn):
            row = self._owned(envelope)
            part = next((p for p in self._parts(envelope) if p['ordinal'] == ordinal), None)
            if row['status'] != 'uncertain' or part is None or part['status'] != 'uncertain':
                raise ValueError('part does not need receipt reconciliation')
            self._settle_part(envelope,part,receipt,clock(now))

    def abandon(self, envelope, *, reviewed, evidence, now):
        """Explicitly stop an incomplete digest. No baseline or automatic replacement."""
        if reviewed is not True:
            raise ValueError('explicit reviewed abandonment required')
        DeliveryReceipt('permanent_failure',evidence)
        with transaction(self.conn):
            row = self._owned(envelope)
            if row['status'] not in {'pending','uncertain','failed','cancelled'}:
                raise ValueError('cannot abandon an active lease or completed digest')
            self._state(envelope, 'abandoned')
            self._set_intents(envelope,'failed',now=clock(now))
            for part in self._parts(envelope):
                self._event(part,'abandoned',evidence,now)

    def reissue_unsent(self, envelopes, intent_ids, *, reviewed, evidence, now):
        """Explicitly recover selected current intents from provably-unsent drafts.

        Create a linked notification generation, not a new material revision or
        policy identity. Original intents, bodies, membership and events remain.
        No part that has ever been attempted can pass this boundary, even after
        a 'not_sent' receipt or manual reconciliation.
        """
        from .delivery_outbox import identifier
        if reviewed is not True:
            raise ValueError('literal reviewed approval required')
        identifier(evidence, 'review evidence')
        now = clock(now)
        envelopes, ids = list(envelopes), list(intent_ids)
        if (not 1 <= len(envelopes) <= 10 or len(set(envelopes)) != len(envelopes)
                or not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids)
                or any(type(i) is not int or i < 1 for i in ids)):
            raise ValueError('bounded unique drafts and intent IDs required')
        with transaction(self.conn):
            version = self.conn.execute("SELECT value FROM delivery_meta WHERE key='version'").fetchone()
            if not version or version[0] != '2':
                raise ValueError('explicit notification-generation migration required')
            owned_ids = set()
            for envelope in envelopes:
                row = self._owned(envelope)
                parts = self._parts(envelope)
                if row['status'] not in {'pending','abandoned','cancelled'} or not parts:
                    raise ValueError('draft is not provably unsent')
                if any(p['attempts'] != 0 or p['status'] != 'pending' for p in parts):
                    raise ValueError('an attempted draft cannot be recomposed')
                if self.conn.execute("SELECT 1 FROM delivery_part_events WHERE envelope=? AND (attempt!=0 OR event!='abandoned')", (envelope,)).fetchone():
                    raise ValueError('draft has provider-attempt evidence')
                owned_ids.update(i['id'] for i in self._items(envelope))
            if not set(ids) <= owned_ids:
                raise ValueError('selected intent is outside the reviewed drafts')
            rows = [self.box._owned(i) for i in ids]
            for row in rows:
                current = self.conn.execute('SELECT revision FROM delivery_subjects WHERE workspace=? AND profile=? AND subject=?',
                    (self.box.workspace,self.box.profile,row['subject'])).fetchone()[0]
                latest = self.conn.execute('SELECT id FROM delivery_intents WHERE revision=? AND channel=? AND destination=? ORDER BY generation DESC LIMIT 1',
                    (row['revision'],row['channel'],row['destination'])).fetchone()[0]
                sent = self.conn.execute("SELECT 1 FROM delivery_attempt_events WHERE intent=? AND (attempt>0 OR event IN ('send_started','accepted','uncertain','not_sent','permanent_failure'))", (row['id'],)).fetchone()
                accepted = self.conn.execute('SELECT 1 FROM delivery_baselines WHERE workspace=? AND profile=? AND subject=? AND channel=? AND destination=? AND revision=?',
                    (self.box.workspace,self.box.profile,row['subject'],row['channel'],row['destination'],row['revision'])).fetchone()
                held = self.conn.execute('''SELECT 1 FROM delivery_intents i JOIN delivery_revisions r ON r.id=i.revision
                    WHERE r.workspace=? AND r.profile=? AND r.subject=? AND i.channel=? AND i.destination=?
                    AND i.status IN ('leased','sending','uncertain','legacy_hold')''',
                    (self.box.workspace,self.box.profile,row['subject'],row['channel'],row['destination'])).fetchone()
                if (row['revision'] != current or row['id'] != latest or row['attempts'] != 0
                        or row['status'] not in {'pending','failed'} or sent or accepted or held):
                    raise ValueError('intent is stale, attempted, held, or already reissued')
            # All checks precede mutation; renderer/insert failures also roll back
            # abandonment through the enclosing transaction.
            for envelope in envelopes:
                if self._owned(envelope)['status'] != 'abandoned':
                    self.abandon(envelope, reviewed=True, evidence=evidence, now=now)
            reissued = []
            for row in rows:
                self.box._event(row['id'], 'reviewed_unsent_reissue', evidence=evidence, now=now)
                self.conn.execute("UPDATE delivery_intents SET status='superseded',token=NULL,lease_until=NULL WHERE id=?", (row['id'],))
                new_id = self.conn.execute('''INSERT INTO delivery_intents(
                    revision,channel,destination,status,payload,max_attempts,available,generation,replaces_intent)
                    VALUES(?,?,?,'pending',?,?,?,?,?)''',
                    (row['revision'],row['channel'],row['destination'],row['payload'],row['max_attempts'],now,row['generation']+1,row['id'])).lastrowid
                self.box._event(new_id, 'reviewed_unsent_replacement', evidence=evidence, now=now)
                reissued.append(new_id)
            return reissued


async def dispatch_envelope(transport, envelope, notifier, *, confirm_target, confirm_body,
                            max_parts=1, now=time.time):
    """Explicit bounded attempt; no sleeping, scraping, or legacy Boolean send API."""
    from .run_context import deny_review_side_effect
    deny_review_side_effect('delivery dispatch')
    if type(max_parts) is not int or not 1 <= max_parts <= 32:
        raise ValueError('max_parts must be 1..32')
    initial = transport.inspect(envelope)
    if confirm_target != initial['destination'] or confirm_body != initial['body_hash']:
        raise ValueError('reviewed target and body hashes required')
    if hashlib.sha256(initial['body'].encode()).hexdigest() != confirm_body:
        raise ValueError('frozen envelope integrity failure')
    expected = [initial['body']]
    if initial['channel'] == 'telegram':
        from .notify.telegram import _chunk
        chunks = _chunk(initial['body'])
        expected = [f'({i+1}/{len(chunks)})\n'+p for i,p in enumerate(chunks)] if len(chunks)>1 else chunks
    if [p['body'] for p in initial['parts']] != expected:
        raise ValueError('frozen part integrity failure')
    for _ in range(max_parts):
        # Rebind before every part; configuration changes must never reroute mail.
        token = transport.claim(envelope,notifier.destination,now=now())
        if token is None:
            break
        part = transport.begin_part(envelope,token,now=now())
        if part is None:
            break
        try:
            receipt = await notifier.send_receipt(part['body'],delivery_key=f"{envelope}:{part['ordinal']}")
            if not isinstance(receipt,DeliveryReceipt):
                receipt = DeliveryReceipt('uncertain','invalid_sender_receipt')
        except Exception:
            receipt = DeliveryReceipt('uncertain','sender_exception')
        # Cancellation/process death leaves durable sending -> uncertain on expiry.
        transport.finish_part(envelope,token,part['ordinal'],receipt,now=now())
        if receipt.outcome != 'accepted':
            break
    return transport.inspect(envelope)['status']
