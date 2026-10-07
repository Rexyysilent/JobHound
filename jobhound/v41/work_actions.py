"""Reviewed existing-work actions inside the authoritative assessment path.

No payment integration, account requests, forecasts or new ranking formula.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..config import CONFIG
from .models import EligibilityStatus
from .outcomes import WORK_PREDICATES, WorkChecks, WorkCost, WorkIdentity, WorkTerms

APPLICATION_ONLY_BLOCKERS = {'source_quality:explicitly_closed', 'source_quality:application_deadline_elapsed'}
APPLICATION_ONLY_TASKS = {'current_open_status', 'application_route'}
RESERVED_WORK_ACTION = '_reviewed_work_action'


def supported_fact(projection, predicate, now, *, work=None, fresh=True):
    return any(e.source_kind != 'reviewed_seed' and e.event_at is not None
               and e.event_at <= now and (e.expires_at is None or now < e.expires_at)
               and (not fresh or (now-e.event_at).total_seconds() <= CONFIG.v55.verification_ttl_hours*3600)
               for e in projection.facts(predicate, work=work))


def existing_work_action_allowed(canonical, assessment):
    """Only an engine-produced plan may change the application-only veto.

    captured_state strips the reserved key from every input overlay. A later
    scam/location/pay veto still blocks starting work. Reading an existing
    payment record does not require eligibility for a new application.
    """
    plan = assessment.account_state.get(RESERVED_WORK_ACTION)
    projection = canonical.outcome_projection if canonical is not None else None
    if not plan or projection is None or canonical.outcome_binding_hold:
        return False
    selected = projection.value('work_selection')
    if (not isinstance(selected, WorkIdentity) or selected.model_dump() != plan.get('target')
            or plan.get('authority') != 'reviewed_work_v3'
            or assessment.next_action != plan.get('next_action')):
        return False
    if plan.get('read_only') is True:
        return assessment.next_action in {'reconcile_payment_status', 'await_response'}
    return (assessment.next_action == 'start_allocated_task'
            and assessment.eligibility == EligibilityStatus.PASSED
            and not (set(assessment.blockers) - APPLICATION_ONLY_BLOCKERS))


def finalize_work_action_view(assessment):
    """Reflect later binding/public vetoes in the already computed plan view."""
    plan = assessment.account_state.get(RESERVED_WORK_ACTION)
    if plan is not None:
        plan['next_action'] = assessment.next_action
        if assessment.next_action != 'start_allocated_task':
            plan['next_action_cost'] = None
        if assessment.next_action not in {'reconcile_payment_status', 'await_response'}:
            plan['read_only'] = False


def apply_reviewed_work_action(canonical, assessment, now: datetime):
    """Return whether exact work facts own this preview's next action."""
    from .action_policy import _task
    projection = canonical.outcome_projection
    if projection is None or not any(e.work is not None or e.predicate == 'work_selection'
                                     for e in projection.events):
        return False
    assessment.time_to_cash_days = None
    selected = projection.value('work_selection')
    work = selected if isinstance(selected, WorkIdentity) else None
    facts = projection.facts('work_selection')
    if work is not None:
        facts += [e for p in sorted(WORK_PREDICATES) for e in projection.facts(p, work=work)]
    plan = dict(authority='reviewed_work_v3', target=work.model_dump() if work else None,
                attempt_scope=projection.scope.model_dump(), event_ids=sorted(e.event_id for e in facts),
                prerequisites=[], terms=None, next_action_cost=None, read_only=False, next_action='verify')
    assessment.account_state[RESERVED_WORK_ACTION] = plan

    def check(fact, instruction):
        if fact not in plan['prerequisites']:
            plan['prerequisites'].append(fact)
            _task(assessment, canonical, fact, instruction, now)

    def finish(action, instruction, *, ready=False, read_only=False, reason='reviewed_existing_work'):
        plan.update(next_action=action, read_only=read_only)
        assessment.lifecycle = 'watch' if read_only or not ready else 'active'
        assessment.lifecycle_reason = reason
        assessment.action_readiness = 'allocated' if ready else 'verification_needed'
        assessment.next_action, assessment.next_step = action, instruction
        return True

    if work is None:
        instruction = 'Select exactly one reviewed project, allocation and terms revision; retain other work as history.'
        check('current_work_selection', instruction)
        return finish('verify', instruction, reason='work_selection_unknown_or_conflicting')

    value = lambda p: projection.value(p, work=work)
    known = lambda p, fresh=True: supported_fact(projection, p, now, work=work, fresh=fresh)
    label = f'project {work.project_id}, allocation {work.allocation_id}, revision {work.revision}'
    conflicts = projection.action_conflicts()
    # Invoice and receipt claims stay independent of acceptance/setup/access.
    # Missing email is never nonpayment. Historical receipts never forecast cash.
    financial = any(projection.facts(p, work=work) for p in ('invoice', 'collected_payment'))
    completed = bool(projection.facts('work_state', work=work))
    if financial or completed:
        assessment.verification_tasks = []
        for key in conflicts:
            check('work_conflict:' + key, 'Review the incompatible claims for this exact existing work; do not infer settlement.')
        if not supported_fact(projection, 'work_selection', now, fresh=False):
            check('work_selection_evidence', 'Date and verify the selected existing-work identity before treating its claims as confirmed.')
        for predicate in ('work_state', 'invoice', 'collected_payment'):
            if projection.facts(predicate, work=work) and not known(predicate, fresh=False):
                check(predicate + '_evidence', 'Verify the date and source of this recorded claim; review seeds and unknown dates are not confirmation.')
        if financial or value('work_state') == 'accepted':
            instruction = (f'Review the existing payment records for {label}. Invoice, acceptance and recipient-reported receipt are separate claims; '
                           'confirm settlement in the existing account records. Missing messages do not establish nonpayment.')
            return finish('reconcile_payment_status', instruction, read_only=True, reason='existing_work_payment_review')
        return finish('await_response', f'Review the recorded submission/acceptance claims for {label} before repeating work; resolve any conflicting status.',
                      read_only=True, reason='existing_work_submitted')

    assessment.verification_tasks = [t for t in assessment.verification_tasks
                                     if t['missing_fact'] not in APPLICATION_ONLY_TASKS]
    if conflicts or assessment.account_state.get('_outcome_legacy_conflicts'):
        for key in sorted(set(conflicts + assessment.account_state.get('_outcome_legacy_conflicts', []))):
            check('work_conflict:' + key, 'Resolve the dated conflicting facts for the selected exact work before starting it.')
    if projection.value('application_state') in {'rejected', 'withdrawn'}:
        check('work_attempt_authority', 'Confirm allocation authority for this closed application; do not repeat its onboarding.')
    if (projection.value('project_access') == 'blocked' or assessment.account_state.get('applicable_block')
            or assessment.account_state.get('project_access') in {'blocked', 'inaccessible'}
            or assessment.account_state.get('application_state') in {'rejected', 'disabled'}
            or assessment.account_state.get('payout_setup') in {'blocked', 'failed', 'disabled'}):
        check('work_account_restriction', 'Resolve the recorded applicable account restriction before starting the allocated work.')
    if not supported_fact(projection, 'work_selection', now):
        check('current_work_selection', 'Reconfirm the selected project, allocation and terms revision in the existing account.')
    expected = {'allocation_state': 'allocated', 'work_access': 'accessible',
                'payment_setup': 'verified', 'payable_approval': 'approved'}
    for predicate, required in expected.items():
        if value(predicate) != required or not known(predicate):
            check(predicate, f'Confirm current {predicate.replace("_", " ")} for {label}; another allocation or a completed assessment does not establish it.')
    terms = value('work_terms')
    if isinstance(terms, WorkTerms):
        plan['terms'] = terms.model_dump()
    if not isinstance(terms, WorkTerms) or not known('work_terms', fresh=False):
        check('work_terms', f'Confirm dated, explicit paid terms in their native currency and unit for {label}.')
    elif terms.unit == 'labor_hour':
        fx = CONFIG.pay.fx_to_usd.get(terms.currency)
        if fx is None or Decimal(terms.amount) * Decimal(str(fx)) < Decimal(str(CONFIG.pay.floor_usd)):
            check('work_economics', 'Verify the explicit labor-hour rate against the configured floor and supported exchange rate before starting work.')
    checks = value('work_checks')
    if not isinstance(checks, WorkChecks) or not all(getattr(checks, key) is True
            for key in ('privacy', 'schedule', 'equipment', 'cost', 'scope', 'terms', 'economics')) or not known('work_checks'):
        check('work_checks', f'Review privacy, schedule, equipment, cost, scope, terms and economics explicitly for {label}.')
    route = value('work_route')
    if not route or not known('work_route'):
        check('work_route', f'Confirm a safe current reference for {label}; open the existing account manually without importing authenticated links.')
    suitability = set(assessment.blockers) - APPLICATION_ONLY_BLOCKERS
    if assessment.eligibility != EligibilityStatus.PASSED or suitability:
        check('work_suitability', 'Resolve the retained requirements, location, pay or risk blockers before starting new work.')
    # Action checks for the allocation supersede only legacy application/onboarding
    # debt, not a hard suitability veto. Never borrow legacy cost or timing.
    assessment.verification_tasks = [t for t in assessment.verification_tasks if t['missing_fact'] not in {
        'project_access', 'onboarding_cost', 'bid_cost_scope_competition'}]
    if assessment.verification_tasks or plan['prerequisites']:
        instruction = assessment.verification_tasks[0]['next_step'] if assessment.verification_tasks else 'Review the selected existing-work prerequisites.'
        return finish('verify', instruction, reason='existing_work_prerequisites')
    cost = value('work_cost')
    if isinstance(cost, WorkCost) and known('work_cost'):
        plan['next_action_cost'] = cost.model_dump()
    assessment.action_cost_known = isinstance(cost, WorkCost) and known('work_cost') and cost.cash is not None
    assessment.action_cost_acceptable = True  # exact fresh user work_checks approved cost
    return finish('start_allocated_task', f'Open the existing account for {label} and follow its confirmed paid task instructions. Public reference: {route}', ready=True)
