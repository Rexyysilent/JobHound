"""Material action evidence and bounded handoff selection for disposable review.

Uses the existing durable queue and PriorityKey. No provider or account access.
"""
from collections import Counter
from decimal import Decimal
import json

from .review_audit import canonical_json, material_projection

MATERIAL_SCHEMA = 'jobhound-material-action/v2'
SIGNAL_SCHEMA = 'jobhound-handoff-signal/v1'


def _native_value(value):
    """Normalize only known monetary decimal fields, not opaque references."""
    if isinstance(value, dict):
        result = {key: _native_value(item) for key, item in value.items()}
        if isinstance(result.get('amount'), str) and 'currency' in result:
            amount = format(Decimal(result['amount']), 'f')
            result['amount'] = amount.rstrip('0').rstrip('.') if '.' in amount else amount
        return result
    if isinstance(value, list):
        return [_native_value(item) for item in value]
    return value


def selected_outcome_facts(raw):
    """Selected step/allocation facts only; retain history elsewhere in the payload."""
    if raw is None:
        return None
    from .v41.outcomes import (OutcomeProjection, AssessmentIdentity, WorkIdentity,
        ASSESSMENT_PREDICATES, WORK_PREDICATES, predicate_key)
    projection = OutcomeProjection.model_validate(raw)
    step = projection.value('assessment_step')
    work = projection.value('work_selection')
    step = step if isinstance(step, AssessmentIdentity) else None
    work = work if isinstance(work, WorkIdentity) else None
    facts = []
    keys = set()
    for event in projection.events:
        if event.predicate=='work_deadline' and work is not None and (
                projection.facts('work_state',work=work) or projection.facts('invoice',work=work)
                or projection.facts('collected_payment',work=work)):
            continue
        if event.predicate=='assessment_deadline' and step is not None and projection.value('assessment_state',assessment=step) in {'completed','passed'}:
            continue
        if work is not None:
            relevant = (event.work == work or event.work is None and event.assessment is None
                and event.predicate in {'work_selection', 'application_state', 'project_access'})
        elif step is not None:
            relevant = (event.assessment == step or event.assessment is None and event.work is None
                and event.predicate not in ASSESSMENT_PREDICATES | WORK_PREDICATES | {'work_selection'})
        else:
            relevant = event.assessment is None and event.work is None
        if not relevant:
            continue
        key = predicate_key(event.predicate, event.assessment, event.work)
        keys.add(key)
        if event.event_id not in projection.current.get(key, []):
            continue
        serialized = event.model_dump(mode='json')
        facts.append(_native_value({key: serialized.get(key) for key in
            ('predicate', 'value', 'actor', 'source_kind', 'assessment', 'work', 'expires_at')}))
    # Duplicate/re-captured supporting events cannot multiply a material fact.
    unique = {canonical_json(fact): fact for fact in facts}
    return {'scope': projection.scope.model_dump(),
            'assessment': step.model_dump() if step else None,
            'work': work.model_dump() if work else None,
            'facts': [unique[key] for key in sorted(unique)],
            'conflicts': sorted(key for key in projection.conflicts if key in keys)}


def handoff_material_projection(row):
    """Versioned material semantics; excludes capture, scores and reminder clocks."""
    base = material_projection(row)
    base.pop('scoped_outcomes')
    assessment = row['assessment']
    buyer = [claim.get('value') for claim in assessment.get('evidence', [])
             if claim.get('dimension') == 'thread' and claim.get('code') == 'buyer_terms_revision']
    plan = (assessment.get('account_state') or {}).get('_reviewed_work_action')
    work = ({key: plan.get(key) for key in ('authority', 'target', 'attempt_scope',
        'prerequisites', 'terms', 'next_action_cost', 'read_only', 'next_action')}
        if isinstance(plan, dict) else None)
    return json.loads(canonical_json({'schema': MATERIAL_SCHEMA, 'opening': base,
        'selected_outcomes': selected_outcome_facts(row.get('outcome_projection')),
        'buyer_terms': buyer, 'work_action': _native_value(work)}))


def _baseline_state(outbox, identity, target, revision, row, *, now):
    """Prove a schema-only adoption from immutable accepted evidence per target.

    No synthetic accepted intent or send receipt is created. Missing old payload
    evidence holds this destination for review, leaving its baseline intact.
    """
    from .review_audit import fingerprint
    subject = outbox.subject(identity)
    baseline = outbox.conn.execute('''SELECT b.revision,r.material FROM delivery_baselines b
        LEFT JOIN delivery_revisions r ON r.id=b.revision WHERE b.workspace=? AND b.profile=?
        AND b.subject=? AND b.channel=? AND b.destination=?''',
        (outbox.workspace, outbox.profile, subject, target.channel, target.key)).fetchone()
    if baseline is None:
        return 'new'
    try:
        material=json.loads(baseline['material'])
        if not isinstance(material,dict):return 'migration_review_required'
    except (TypeError,ValueError):
        return 'migration_review_required'
    if baseline['revision'] == revision:
        return 'delivered'
    if material.get('schema') == MATERIAL_SCHEMA:
        return 'changed'
    proofs = outbox.conn.execute('''SELECT i.id,i.payload FROM delivery_intents i
        WHERE i.revision=? AND i.channel=? AND i.destination=? AND i.status='accepted'
        AND EXISTS(SELECT 1 FROM delivery_attempt_events e WHERE e.intent=i.id AND e.event='accepted')''',
        (baseline['revision'], target.channel, target.key)).fetchall()
    for proof in proofs:
        try:
            previous=json.loads(proof['payload'])
            # Missing/malformed evidence is a per-destination review hold,
            # never a resend or a failure of unrelated subjects in the run.
            if not isinstance(previous,dict) or 'outcome_projection' not in previous:
                continue
            if material_projection(previous)!=material:
                continue
            prior_material=handoff_material_projection(previous)
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
        new_material = handoff_material_projection(row)
        if new_material != prior_material:
            # A newly introduced public terms field has no previous schema
            # witness. Hold rather than guess whether it was previously sent.
            if new_material['buyer_terms'] and not prior_material['buyer_terms']:
                continue
            return 'changed'
        evidence = fingerprint({'accepted_intent': proof['id'], 'baseline': baseline['revision'],
                                'material': prior_material})
        outbox.conn.execute('''INSERT INTO delivery_material_adoptions
            (workspace,profile,subject,channel,destination,baseline,revision,evidence,created)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (outbox.workspace, outbox.profile, subject, target.channel, target.key,
             baseline['revision'], revision, evidence, now))
        outbox.conn.execute('''UPDATE delivery_baselines SET revision=? WHERE workspace=?
            AND profile=? AND subject=? AND channel=? AND destination=? AND revision=?''',
            (revision, outbox.workspace, outbox.profile, subject, target.channel,
             target.key, baseline['revision']))
        return 'adopted'
    return 'migration_review_required'


def _lane(item):
    from .v41.work_actions import existing_work_action_allowed
    decision = item.decision
    if (decision.next_action == 'start_allocated_task'
            and existing_work_action_allowed(item.canonical, item.assessment)
            and not item.assessment.account_state['_reviewed_work_action'].get('prerequisites')
            and item.assessment.action_readiness == 'allocated'):
        return 'action'
    if decision.next_action in {'await_response', 'await_change', 'reconcile_payment_status'}:
        return 'status'
    if decision.lifecycle == 'active' and decision.action_band.value != 'reject':
        if decision.action_band.value == 'primary':
            return 'action'
        if decision.verification_tasks:
            return 'verify'
    return 'status'


def _select_actions(items, *, action_cap, verify_cap, total_cap):
    """Use the authoritative PriorityKey, existing breadth caps and shared slots."""
    from .config import CONFIG
    ordered = sorted(items, key=lambda item: (0 if _lane(item) == 'action' else 1,
                                            *item.decision.priority_key.sort_tuple))
    lanes = {_id(item): _lane(item) for item in ordered}
    discovery = next((item for item in ordered if lanes[_id(item)] == 'action'
                      and item.canonical.outcome_projection is None), None)
    reserved = discovery if action_cap else None
    def select(candidates):
        companies,marketplaces,counts=Counter(),Counter(),Counter()
        chosen,reasons=[],{}
        for item in candidates:
            identity,lane=_id(item),lanes[_id(item)]
            employer=item.canonical.counterparty_key or identity
            platform=item.job.platform_key if item.job.platform_key=='upwork' else None
            cap=action_cap if lane=='action' else verify_cap
            if companies[employer]>=CONFIG.v55.max_per_employer:reason='employer_cap'
            elif platform and CONFIG.v55.max_per_marketplace>0 and marketplaces[platform]>=CONFIG.v55.max_per_marketplace:reason='marketplace_cap'
            elif counts[lane]>=cap or len(chosen)>=total_cap:reason='action_cap' if lane=='action' else 'verify_cap'
            else:
                chosen.append(item);companies[employer]+=1;counts[lane]+=1
                if platform:marketplaces[platform]+=1
                reason='displayed_action' if lane=='action' else 'displayed_verify'
            reasons[identity]=reason
        return chosen,reasons
    normal,_=select(ordered)
    ready=[item for item in normal if lanes[_id(item)]=='action']
    displacement=(reserved is not None and len(ready)==action_cap and ready and all(
        item.canonical.outcome_projection is not None and item.decision.priority_key.supported_urgency==1 for item in ready))
    reservation=dict(canonical_id=None,reason='no_available_suitable_discovery',displaced_discovery=None,urgent_existing=[])
    if displacement:
        reservation.update(reason='supported_urgent_existing',displaced_discovery=_id(reserved),
            urgent_existing=[dict(canonical_id=_id(item),evidence=item.assessment.account_state['_reviewed_signal_order']) for item in ready])
        reserved=None
    candidates=([reserved] if reserved is not None else [])+[item for item in ordered if item is not reserved]
    selected,dispositions=select(candidates)
    # Reservation changes membership, not the common ordering comparator.
    selected.sort(key=lambda item: (0 if lanes[_id(item)] == 'action' else 1,
                                   *item.decision.priority_key.sort_tuple))
    if reserved is not None and reserved in selected:
        reservation.update(canonical_id=_id(reserved),reason='suitable_new_discovery')
    return selected,dispositions,reservation


def _id(item):
    return item.canonical.canonical_id


def stage_handoff_signals(outbox, result, destinations, *, now, card_cap, status_cap):
    from datetime import timezone
    from .delivery_outbox import evaluated_row, stage_coverage_updates, clock
    from .v41.provenance import sanitize_payload
    if not getattr(outbox, '_handoff_review_enabled', False):
        raise ValueError('handoff signals require a disposable review store')
    if not outbox.conn.in_transaction:
        raise RuntimeError('run and delivery must share a transaction')
    now = clock(now)
    for cap in (card_cap, status_cap):
        if type(cap) is not int or not 0 <= cap <= 100:
            raise ValueError('caps must be integers in [0,100]')
    targets = list(destinations)
    from .delivery_outbox import Destination
    if len(targets) > 64 or len(set(targets)) != len(targets) or any(not isinstance(t, Destination) for t in targets):
        raise ValueError('unique bounded validated destinations required')
    if result.metadata.as_of.tzinfo is None:
        raise ValueError('aware delivery timestamp required')
    as_of = result.metadata.as_of.astimezone(timezone.utc).isoformat()
    metadata = result.metadata.model_dump(mode='json')
    policy = {key: metadata.get(key) for key in
              ('profile_hash', 'ruleset_hash', 'config_hash', 'trust_registry_hash', 'engine_version')}
    # This journal belongs only to disposable handoff review state. Existing
    # production delivery schema and defaults are left at their current version.
    outbox.conn.execute('''CREATE TABLE IF NOT EXISTS delivery_material_adoptions(
        workspace TEXT NOT NULL,profile TEXT NOT NULL,subject TEXT NOT NULL,
        channel TEXT NOT NULL,destination TEXT NOT NULL,baseline TEXT NOT NULL,
        revision TEXT NOT NULL,evidence TEXT NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(workspace,profile,subject,channel,destination,revision))''')
    for operation in ('UPDATE', 'DELETE'):
        outbox.conn.execute(f'''CREATE TRIGGER IF NOT EXISTS delivery_adoptions_no_{operation.lower()}
            BEFORE {operation} ON delivery_material_adoptions
            BEGIN SELECT RAISE(ABORT,'material adoption evidence is append-only'); END''')
    subjects = [outbox.subject(_id(item)) for item in result.evaluated]
    if len(set(subjects)) != len(subjects) or any(s.startswith('coverage:') for s in subjects):
        raise ValueError('unique opportunity subjects outside coverage namespace required')
    rows, revisions = {}, {}
    for item in result.evaluated:
        identity = _id(item)
        rows[identity] = sanitize_payload(evaluated_row(item))
        revisions[identity] = outbox.revise(identity, handoff_material_projection(rows[identity]), policy,
            run_id=result.metadata.run_id, as_of=as_of, now=now)
    ledgers = []
    for target in targets:
        counts = dict(total=len(rows), ineligible=0, already_recorded=0, policy_review=0,
                      selected_cards=0, selected_status=0, overflow_cards=0, overflow_status=0)
        available, statuses, dispositions, migrations = [], [], {}, Counter()
        candidates = 0
        for item in result.evaluated:
            identity, revision, lane = _id(item), revisions[_id(item)], _lane(item)
            scoped = item.canonical.outcome_projection is not None
            baseline = _baseline_state(outbox, identity, target, revision, rows[identity], now=now)
            if lane == 'status' and not scoped and baseline == 'new':
                counts['ineligible'] += 1
                dispositions[identity] = 'no_supported_status_audience'
                continue
            candidates += lane != 'status'
            if baseline == 'migration_review_required':
                counts['policy_review'] += 1
                dispositions[identity] = baseline
                migrations[baseline] += 1
                continue
            if baseline == 'adopted':
                migrations['adopted'] += 1
            exists = outbox.conn.execute('''SELECT 1 FROM delivery_intents
                WHERE revision=? AND channel=? AND destination=?''', (revision, target.channel, target.key)).fetchone()
            if baseline in {'delivered', 'adopted'} or exists:
                counts['already_recorded'] += 1
                dispositions[identity] = 'delivered' if baseline in {'delivered', 'adopted'} else 'queued_or_held'
                continue
            (statuses if lane == 'status' else available).append(item)
        selected, selection, reservation = _select_actions(available, action_cap=min(5, card_cap),
                                                       verify_cap=min(2, card_cap), total_cap=card_cap)
        dispositions.update(selection)
        selected_status = sorted(statuses, key=lambda item: item.decision.priority_key.sort_tuple)[:min(3, status_cap)]
        shown_status = {_id(item) for item in selected_status}
        for item in statuses:
            dispositions[_id(item)] = 'displayed_status' if _id(item) in shown_status else 'status_cap'
        counts.update(selected_cards=len(selected), selected_status=len(selected_status),
                      overflow_cards=len(available)-len(selected), overflow_status=len(statuses)-len(selected_status))
        if sum(v for k, v in counts.items() if k != 'total') != counts['total'] or len(dispositions) != len(rows):
            raise ValueError('handoff presentation ledger failed')
        states, intent_ids = Counter(), []
        for item in [*selected, *selected_status]:
            identity = _id(item)
            payload = dict(rows[identity], kind='handoff_status' if identity in shown_status else 'handoff_card',
                           signal_schema=SIGNAL_SCHEMA, signal_lane=_lane(item))
            state = outbox.enqueue(revisions[identity], target, payload, now=now)
            states[state] += 1
            intent_ids.append(outbox.conn.execute('''SELECT id FROM delivery_intents
                WHERE revision=? AND channel=? AND destination=? ORDER BY id DESC LIMIT 1''',
                (revisions[identity], target.channel, target.key)).fetchone()[0])
        actions = Counter(dispositions[_id(item)] for item in result.evaluated if _lane(item) != 'status')
        if sum(actions.values()) != candidates:
            raise ValueError('handoff action ledger failed')
        ledgers.append(dict(channel=target.channel, destination=target.key, counts=counts,
            states=dict(states), intent_ids=intent_ids, policy_changed=0,
            dispositions=dispositions, action_accounting=dict(total=candidates, dispositions=dict(actions)),
            reservation=reservation,
            migrations=dict(migrations), cards=dict(eligible=candidates, already_recorded=sum(actions.get(k, 0)
                for k in ('delivered', 'queued_or_held')), selected=len(selected), listed=len(available)-len(selected)),
            card_intent_ids=intent_ids[:len(selected)], overflow=[]))
    stage_coverage_updates(outbox, result, targets, ledgers, policy, as_of, now=now, status_cap=min(3, status_cap))
    return dict(schema=SIGNAL_SCHEMA, run_id=result.metadata.run_id, destinations=ledgers,
                revisions=revisions, limits=dict(action=5, verify=2, status=3))


def compact_status(payload):
    """One supported factual line, without creating another action/Verify card."""
    job, decision = payload['job'], payload['decision']
    title = ' '.join(str(job.get('title') or 'Opportunity').split())[:120]
    company = ' '.join(str(job.get('company') or '').split())[:80]
    facts = selected_outcome_facts(payload.get('outcome_projection'))
    details = []
    if facts:
        for fact in facts['facts']:
            predicate, value = fact['predicate'], fact['value']
            if predicate in {'application_state', 'assessment_state', 'project_access',
                             'allocation_state', 'work_access', 'payment_setup', 'payable_approval', 'work_state'}:
                details.append(f"recorded {predicate.replace('_', ' ')}: {value}")
            elif predicate in {'invoice', 'collected_payment'} and isinstance(value, dict):
                label = 'invoice claim' if predicate == 'invoice' else 'recipient-reported receipt'
                details.append(f"{label}: {value['currency']} {value['amount']}")
    if not details:
        details.append(f"{decision.get('lifecycle', 'unknown')}; {decision.get('next_action', 'await_change').replace('_', ' ')}")
    caveat = ' Conflicting claims require review.' if facts and facts['conflicts'] else ''
    plan = (payload.get('assessment', {}).get('account_state') or {}).get('_reviewed_work_action')
    if isinstance(plan, dict) and plan.get('prerequisites'):
        caveat += ' Evidence/prerequisites remain unresolved.'
    return f"- {title}{' — ' + company if company else ''}: {'; '.join(details)}.{caveat}"
