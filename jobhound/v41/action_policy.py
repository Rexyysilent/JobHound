"""V5 release lifecycle and next-action policy, separate from eligibility.

Only captured evidence can establish readiness. Unknown account access, pay,
allocation and timing remain distinct; watch is presentation, not rejection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit

from ..config import CONFIG
from .models import Assessment, CanonicalJob, EligibilityStatus, SourceKind, Evidence
from .provenance import public_url


def timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def captured_state(canonical: CanonicalJob) -> dict:
    """Explicit saved account/verification state, never extracted from prose."""
    result: dict = {}
    for observation in sorted(canonical.observations, key=lambda o: (o.captured_at or datetime.min.replace(tzinfo=timezone.utc), o.observation_id)):
        if observation.origin == 'account_state':
            result.update(observation.raw_payload)
    matches = []
    for state in CONFIG.v55.account_states:
        if state.get('canonical_id') == canonical.canonical_id or (
            state.get('platform') == canonical.job.platform_key
            and (state.get('project_url') == public_url(canonical.job.url)
                 or state.get('restriction_scope') == 'platform')
        ):
            matches.append(state)
    for state in sorted(matches, key=lambda s: str(s.get('observed_at') or '')):
        result.update(state)
    if canonical.outcome_projection is not None:
        from .outcomes import compatibility_state
        conflicts = []
        for key, value in compatibility_state(canonical.outcome_projection).items():
            if key in result and result[key] != value:
                conflicts.append(key)
            else:
                result[key] = value
        result['_outcome_legacy_conflicts'] = conflicts
    # This key is generated only by the reviewed work policy, never an overlay.
    result.pop('_reviewed_work_action', None)
    result.pop('_reviewed_signal_order', None)
    return result


def _task(assessment: Assessment, canonical: CanonicalJob, fact: str, step: str,
          now: datetime, actor: str = 'operator', attempts: int = 0) -> None:
    if any(task['missing_fact'] == fact for task in assessment.verification_tasks):
        return
    assessment.verification_tasks.append({
        'missing_fact': fact, 'why_it_matters': 'Needed before the recommended application or paid work is actionable.',
        'responsible_actor': actor, 'next_step': step,
        'source': public_url(canonical.job.url), 'attempts': attempts,
        'due_at': now.isoformat(),
        'expires_at': (now + timedelta(days=CONFIG.v55.watch_recheck_days)).isoformat(),
    })


def apply_action_policy(canonical: CanonicalJob, assessment: Assessment, now: datetime) -> None:
    from .documents import classify_offer

    job = canonical.job
    document = classify_offer(canonical)
    assessment.document_type = document.document_type
    assessment.opportunity_type = document.opportunity_type
    assessment.buyer_intent = document.intent
    assessment.next_step = ''
    assessment.verification_tasks = []
    non_jobs = {'job_index', 'job_index_search', 'index', 'search_page', 'talent_directory', 'seller_service', 'discussion', 'article', 'none'}
    if document.document_type in non_jobs or document.opportunity_type in {'none', 'unpaid', 'volunteer'}:
        assessment.blockers.insert(0, 'source_quality:non_opportunity:' + document.document_type)
    if document.document_type in {'talent_pool', 'talent_pool_signup'} or document.opportunity_type in {'pool', 'waitlist', 'pool_waitlist'}:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'pool_not_allocated_work'

    observations = [o for o in canonical.observations if o.job and o.origin != 'account_state']
    matched = [o for o in observations if o.identity_state in {'exact', 'corroborated'}]
    complete = [o for o in matched if o.content_state in {'complete', 'role_complete'} and not o.truncated]
    checks = [timestamp(o.verified_open_at) for o in matched if o.vacancy_state == 'verified_open']
    assessment.verified_open_at = max((x for x in checks if x and x <= now), default=None)
    host = (urlsplit(job.url).hostname or '').casefold()
    marketplace = job.platform_key == 'upwork' or host == 'upwork.com' or host.endswith('.upwork.com')
    assessment.search_snippet_only = marketplace and not complete
    ttl = CONFIG.v55.marketplace_ttl_hours if marketplace else CONFIG.v55.verification_ttl_hours
    open_fresh = assessment.verified_open_at is not None and (now - assessment.verified_open_at).total_seconds() <= ttl * 3600
    closed = any(o.vacancy_state == 'explicitly_closed' and o.identity_state in {'exact', 'corroborated'} for o in observations)
    thread = None
    if job.source == 'community_thread':
        from .community import observation_thread
        thread = next((t for o in observations if (t := observation_thread(o)) is not None and t.url == job.url), None)
        if thread is None:
            assessment.unresolved.append('thread_evidence_invalid')
            assessment.blockers.insert(0, 'source_quality:thread_evidence_invalid')
        else:
            assessment.unresolved.extend(thread.issues + thread.caveats)
            assessment.evidence.extend([
                Evidence(dimension='thread', code='demand_original_at', value=thread.original_at.isoformat() if thread.original_at else None,
                         source_field='community_thread.original_at', observation_id=canonical.best_observation.observation_id),
                Evidence(dimension='thread', code='last_substantive_buyer_at', value=thread.last_substantive_buyer_at.isoformat() if thread.last_substantive_buyer_at else None,
                         source_field='community_thread.last_substantive_buyer_at', observation_id=canonical.best_observation.observation_id),
            ])
            if thread.schema_version == 'community-thread/v2' and thread.intent == 'buyer':
                assessment.evidence.append(Evidence(dimension='thread', code='buyer_terms_revision',
                    value={'state': thread.terms_state, 'revision_id': thread.current_terms_revision_id,
                           'post_ids': thread.current_terms_post_ids}, source_field='community_thread.revisions',
                    observation_id=canonical.best_observation.observation_id))
                if thread.terms_state in {'scope_changed', 'budget_changed', 'ambiguous', 'incomplete'}:
                    assessment.unresolved.append('community_terms:' + thread.terms_state)
                    _task(assessment, canonical, 'community_scope_revision',
                          'Verify the current buyer scope, hosting/support responsibilities and budget in this exact thread; earlier amounts remain historical and do not establish agreed terms or paid work.', now)
            if thread.thread_locked and thread.vacancy_state != 'explicitly_closed':
                assessment.unresolved.append('community_reply_route_locked')
                _task(assessment, canonical, 'community_contact_route', 'Verify the buyer contact route; a locked discussion does not prove the work is filled.', now)
    if closed:
        assessment.blockers.insert(0, 'source_quality:explicitly_closed')
        assessment.lifecycle, assessment.lifecycle_reason = 'closed', 'matching_source_explicitly_closed'

    route = any(o.application_route_state in {'observed_public_route', 'account_verified'} for o in matched)
    route = route and bool(public_url(job.url))
    state = captured_state(canonical)
    assessment.account_state = state
    attempts = int(state.get('automatic_verification_cycles') or state.get('verification_attempts') or 0)
    observed_at = timestamp(state.get('observed_at'))
    next_check = timestamp(state.get('next_check_at') or state.get('expires_at'))
    state_expired = next_check is not None and next_check <= now
    evidence_current = bool(observed_at and observed_at <= now and not state_expired and not state.get('evidence_expired'))

    # Deadlines are accepted only from captured structured fields, never prose.
    deadline_values = [state.get('application_deadline'), state.get('deadline')]
    deadline_values.extend(
        o.raw_payload.get('application_deadline') or o.raw_payload.get('deadline')
        for o in matched if isinstance(o.raw_payload, dict)
    )
    deadlines = [parsed for value in deadline_values if (parsed := timestamp(value))]
    deadline = min(deadlines, default=None)
    if deadline is not None and deadline <= now:
        assessment.blockers.insert(0, 'source_quality:application_deadline_elapsed')
        assessment.lifecycle, assessment.lifecycle_reason = 'closed', 'captured_application_deadline_elapsed'
    elif deadline is not None:
        lead_hours = state.get('minimum_lead_time_hours')
        if isinstance(lead_hours, (int, float)) and (deadline - now).total_seconds() < float(lead_hours) * 3600:
            assessment.blockers.insert(0, 'deadline_infeasible')
    account_block = bool(state.get('applicable_block')) or state.get('project_access') in {'inaccessible', 'blocked'} or state.get('application_state') in {'rejected', 'disabled'} or state.get('payout_setup') == 'blocked'
    if account_block:
        assessment.lifecycle = 'watch'
        assessment.lifecycle_reason = 'account_block_recheck_due' if state_expired else 'known_account_block'
        _task(assessment, canonical, 'project_access', 'Check whether the recorded project/account restriction has been lifted; do not repeat the application or create another account.', now)
    elif state.get('application_state') in {'applied', 'applied_waiting', 'waiting'}:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'application_already_submitted'
        assessment.next_action = 'await_response'
    elif state.get('assessment_state') in {'completed', 'passed', 'submitted'}:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'assessment_already_completed'
        assessment.next_action = 'await_response'
    posted = timestamp(job.posted_at)
    # Crawling a discussion, seller replies and cosmetic edits do not renew
    # buyer demand. Keep the posting date as provenance; rank substantive
    # activity by this same requester when native thread evidence exists.
    if thread is not None and thread.intent == 'buyer':
        buyer_activity = thread.last_substantive_buyer_at or thread.original_at
        if buyer_activity is not None and buyer_activity <= now:
            posted = buyer_activity
    age = max(0, (now - posted).total_seconds() / 86400) if posted else None
    # Some sources can never be checked automatically, so failed checks alone
    # do not hide a job: it moves to watch once the posting is older than the
    # watch age or, when undated, once its checks have failed for that long.
    # Operator-recorded attempts carry no first-failure time; they count from
    # when they were recorded.
    first_failed = timestamp(state.get('first_failed_verification_at')) or (observed_at if attempts else None)
    failing_days = max(0, (now - first_failed).total_seconds() / 86400) if first_failed else 0
    watch_age = CONFIG.v55.exhausted_watch_age_days
    old_enough = age > watch_age if age is not None else failing_days > watch_age
    if attempts >= CONFIG.v55.max_automatic_cycles and not state.get('new_material_evidence') and old_enough:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'verification_exhausted'

    authoritative = any(o.source_kind in {SourceKind.ORIGINAL_ATS, SourceKind.ORIGINAL_EMPLOYER, SourceKind.EMAIL_OFFER} and o in complete for o in observations)
    thin_copy = not authoritative
    if thin_copy and age is not None and age > CONFIG.v55.unresolved_copy_max_age_days:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'stale_unresolved_copy'
    elif thin_copy and age is None and attempts >= 1 and old_enough:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'undated_copy_verification_exhausted'
    if (thread is not None and thread.intent == 'buyer'
            and age is not None and age > CONFIG.v55.community_buyer_activity_max_age_days
            and assessment.lifecycle == 'active'):
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'historical_buyer_request'
        assessment.unresolved.append('historical_buyer_request')
    if age is not None:
        half_life = CONFIG.ranking.easy_entry_half_life_days if assessment.easy_entry else CONFIG.ranking.freshness_half_life_days
        assessment.freshness = 2 ** (-age / half_life)
    else:
        assessment.freshness = 0.0

    # Eligibility is separate from operational access/open status.
    if not complete:
        _task(assessment, canonical, 'full_matching_requirements', 'Read the exact original role description and confirm applicant requirements, location and work scope.', now, 'automatic_adapter', attempts)
    if not open_fresh:
        _task(assessment, canonical, 'current_open_status', 'Check that this exact role still accepts applications; a timeout or rate limit does not establish closure.', now, 'automatic_adapter', attempts)
    if not route:
        _task(assessment, canonical, 'application_route', 'Locate the individual role application/contact path, not a board index or company home page.', now, 'automatic_adapter', attempts)
    for unresolved in assessment.unresolved:
        if unresolved.startswith(('credential_unverified:', 'requirement_unverified:', 'experience_unverified:', 'specialist_', 'location_scope_', 'pay_conflict', 'title_truncated', 'company_identity_')):
            _task(assessment, canonical, unresolved, 'Compare the cited requirement or conflicting field against the original posting and the operator’s documented evidence.', now)
    # A contradiction in explicit eligibility evidence is more decision-relevant
    # than generic retrieval debt.  Keep all tasks, but make the conflict the
    # visible first check and therefore the selected next step below.
    assessment.verification_tasks.sort(key=lambda task: (
        0 if str(task.get('missing_fact', '')).startswith('location_scope_conflict:') else 1,
    ))

    text = job.description.casefold()
    cost = state.get('action_cost') if isinstance(state.get('action_cost'), dict) else {}
    cash = cost.get('cash_required')
    # Upwork Connects are a counted bid currency in Upwork's own wordings
    # ("16 Connects", "1 Connect", "Connects: 16", "Required Connects to submit
    # a proposal"); the bare verb ("Mercor connects talent with labs") is
    # company boilerplate, not a cost.
    connects_cost = (r'\d+\s+connects?|connects?\s*:\s*\d+|required\s+connects?'
                     r'|connects?\s+(?:to|required\s+to|needed\s+to)\s+(?:submit|apply|bid|send)')
    high_effort = bool(re.search(r'\b(?:90|120)\s*(?:min|minute)|\b(?:paid\s+(?:test|bid)|lengthy\s+(?:test|assessment)|'
                                 + connects_cost + r')\b', text))
    assessment.action_cost_known = cash is not None
    assessment.action_cost_acceptable = cash == 0 or (cash is None and not marketplace and not high_effort)
    if cash is not None and cash > 0:
        assessment.action_cost_acceptable = cost.get('operator_approved') is True
    if marketplace and not (cost.get('scope_verified') is True and cash is not None and cost.get('competition_checked') is True):
        assessment.action_cost_acceptable = False
        _task(assessment, canonical, 'bid_cost_scope_competition', 'Verify the individual buyer, deliverables, budget, Connects cost and competing proposals before spending on a bid.', now)
    elif not assessment.action_cost_acceptable:
        _task(assessment, canonical, 'onboarding_cost', 'Confirm the unpaid assessment effort or cash cost and economics before committing to this step.', now)
    access_confirmed = state.get('project_access') in {'accessible', 'verified'}
    if job.platform_key in CONFIG.v55.account_specific_platforms and not access_confirmed:
        _task(assessment, canonical, 'project_access', 'Open this exact project in the existing account and confirm it is accessible before starting certification.', now)

    task_allocated = state.get('task_allocation') == 'allocated' or state.get('tasks_allocated') is True
    allocation_at = timestamp(state.get('allocated_at') or state.get('allocation_observed_at')) or observed_at
    allocation_current = bool(
        evidence_current and allocation_at and allocation_at <= now
        and (now - allocation_at).total_seconds() <= CONFIG.v55.verification_ttl_hours * 3600
    )
    from .compensation import parse_compensation
    agreed = parse_compensation(str(state.get('agreed_pay') or ''))
    paid_terms = bool(
        state.get('paid_terms_reference') or (agreed.amount_low is not None and agreed.amount_low > 0)
        or (assessment.selected_pay and assessment.selected_pay.amount_high
            and assessment.selected_pay.amount_high > 0
            and assessment.selected_pay.observation_source_kind in {SourceKind.ORIGINAL_ATS, SourceKind.ORIGINAL_EMPLOYER, SourceKind.EMAIL_OFFER})
    )
    payout_ready = state.get('payout_setup') not in {'blocked', 'failed', 'disabled'}
    payout_confirmed = state.get('payout_setup') in {'verified', 'complete'}
    invitation_pending = state.get('invitation_state') in {'received', 'pending_response'}
    invitation_route = state.get('invitation_route') or state.get('response_route')
    assessment_pending = state.get('assessment_state') in {'pending', 'invited', 'required'}
    assessment_route = state.get('assessment_route') or state.get('next_step_route')
    assessment_step = state.get('assessment_step') or state.get('known_next_step')
    if task_allocated and access_confirmed and allocation_current and paid_terms and payout_ready and not account_block and assessment.eligibility == EligibilityStatus.PASSED and not any(t['missing_fact'] not in {'current_open_status','application_route'} for t in assessment.verification_tasks):
        assessment.action_readiness = 'allocated'
        assessment.next_action = 'start_allocated_task'
        assessment.next_step = 'Open the confirmed allocated task in the existing account; follow its instructions and payment terms.'
        assessment.verification_tasks = [t for t in assessment.verification_tasks if t['missing_fact'] not in {'current_open_status', 'application_route'}]
    elif invitation_pending and evidence_current and invitation_route and not account_block and assessment.action_cost_acceptable and assessment.eligibility == EligibilityStatus.PASSED and not any(t['missing_fact'] not in {'current_open_status','application_route'} for t in assessment.verification_tasks):
        assessment.action_readiness = 'captured_next_step'
        assessment.next_action = 'respond_to_invitation'
        assessment.next_step = f'Respond through the captured invitation route: {public_url(str(invitation_route))}'
        assessment.verification_tasks = []
    elif assessment_pending and evidence_current and assessment_route and assessment_step and assessment.action_cost_acceptable and not account_block and assessment.eligibility == EligibilityStatus.PASSED and not any(t['missing_fact'] not in {'current_open_status','application_route'} for t in assessment.verification_tasks):
        assessment.action_readiness = 'captured_next_step'
        assessment.next_action = 'complete_known_step'
        assessment.next_step = f'{assessment_step} Route: {public_url(str(assessment_route))}'
        assessment.verification_tasks = []
    elif not assessment.verification_tasks and assessment.lifecycle == 'active':
        assessment.action_readiness = 'application_ready'
        assessment.next_action = 'apply'
        assessment.next_step = 'Submit one application through the verified individual role page; selection and task allocation remain pending.'
    else:
        assessment.action_readiness = 'verification_needed'
        assessment.next_action = 'verify'
        assessment.next_step = assessment.verification_tasks[0]['next_step'] if assessment.verification_tasks else 'Wait for a material opening, account-state change or a scheduled recheck.'
    if thread is not None and 'paid_intent_unconfirmed' in thread.caveats and assessment.next_action in {'apply', 'verify'}:
        _task(assessment, canonical, 'community_paid_scope',
              'Confirm whether this exact requester wants paid contractor help, and clarify the deliverables and buyer budget; a forum help request does not establish paid work.', now)
        assessment.action_readiness = 'verification_needed'
        assessment.next_action = 'verify'
        assessment.verification_tasks.sort(key=lambda task: 0 if task['missing_fact'].startswith('location_scope_conflict:') else 1 if task['missing_fact']=='community_paid_scope' else 2)
        assessment.next_step = assessment.verification_tasks[0]['next_step']
    if thread is not None and thread.schema_version == 'community-thread/v2' and thread.terms_state in {
        'scope_changed', 'budget_changed', 'ambiguous', 'incomplete'
    } and assessment.next_action in {'apply', 'verify'}:
        assessment.action_readiness, assessment.next_action = 'verification_needed', 'verify'
        assessment.verification_tasks.sort(key=lambda task: 0 if task['missing_fact'].startswith('location_scope_conflict:')
            else 1 if task['missing_fact']=='community_paid_scope'
            else 2 if task['missing_fact']=='community_scope_revision' else 3)
        if assessment.verification_tasks:
            assessment.next_step = assessment.verification_tasks[0]['next_step']
    if assessment.lifecycle != 'active':
        assessment.action_readiness = 'watch'
        if assessment.lifecycle_reason == 'historical_buyer_request':
            assessment.next_action = 'await_change'
            assessment.next_step = ('High-fit historical lead: wait for substantive new buyer activity '
                                    'before treating this request as a current daily action.')
        elif assessment.lifecycle_reason in {'application_already_submitted', 'assessment_already_completed'}:
            assessment.next_action = 'await_response'
            assessment.next_step = 'Wait for the recorded application or assessment outcome; do not repeat completed steps.'
        elif not assessment.verification_tasks:
            assessment.next_action = 'await_change'
            assessment.next_step = 'Wait for new task availability, an account update or the next scheduled recheck; do not repeat completed steps.'

    # Time-to-cash is supported only by an explicit referenced estimate plus a
    # currently allocated task and completed access/payment prerequisites.
    timing = state.get('time_to_cash_days')
    timing_reference = state.get('time_to_cash_evidence_reference') or state.get('evidence_reference')
    if (
        isinstance(timing, (int, float)) and timing >= 0 and timing_reference
        and task_allocated and allocation_current and access_confirmed
        and payout_confirmed and paid_terms and not account_block
    ):
        assessment.time_to_cash_days = float(timing)
    if canonical.outcome_projection is not None:
        apply_reviewed_outcome_action(canonical, assessment, now)
    if canonical.outcome_binding_hold:
        _task(assessment, canonical, 'active_outcome_attempt',
              'Select exactly one active application attempt for this exact role; retain the other attempts as history before choosing the next action.', now)
        assessment.unresolved.append('outcome_binding:' + canonical.outcome_binding_hold)
        assessment.action_readiness = 'verification_needed'
        assessment.next_action = 'verify'
        assessment.next_step = next(t['next_step'] for t in assessment.verification_tasks if t['missing_fact']=='active_outcome_attempt')
    from .work_actions import existing_work_action_allowed
    if closed and assessment.lifecycle != 'closed' and not existing_work_action_allowed(canonical, assessment):
        # A freshness/watch rule cannot reopen the same public request. Scoped
        # application/account facts remain recorded separately in account_state.
        assessment.lifecycle, assessment.lifecycle_reason = 'closed', 'matching_source_explicitly_closed'
        assessment.action_readiness = 'watch'
        assessment.verification_tasks = []
    from .action_constraints import apply_action_constraints
    apply_action_constraints(canonical,assessment,now,thread=thread)
    finalize_rejected_action(assessment, canonical)
    from .work_actions import finalize_work_action_view
    finalize_work_action_view(assessment)


def finalize_rejected_action(assessment: Assessment, canonical: CanonicalJob | None = None) -> None:
    """A public policy veto removes action authority while retaining evidence."""
    if not assessment.blockers:
        return
    from .work_actions import existing_work_action_allowed
    if existing_work_action_allowed(canonical, assessment):
        # Reading an existing payment record leaves public qualification failed.
        if assessment.account_state['_reviewed_work_action']['read_only']:
            assessment.eligibility = EligibilityStatus.FAILED
        return
    assessment.eligibility = EligibilityStatus.FAILED
    assessment.action_readiness = 'closed' if assessment.lifecycle == 'closed' else 'blocked'
    assessment.next_action = 'skip'
    assessment.next_step = 'Do not apply based on this record: ' + '; '.join(assessment.blockers[:3])


def apply_reviewed_outcome_action(canonical: CanonicalJob, assessment: Assessment,
                                  now: datetime) -> None:
    """First offline outcome slice, inside the existing action/decision path.

    Explicit projected state may refine an action but never silently override a
    conflicting legacy overlay or a suitability blocker. Delivery is separate.
    """
    from .outcomes import ActionChecks, AssessmentIdentity
    from ..config import CONFIG
    projection = canonical.outcome_projection
    if projection is None or not projection.events:
        return
    from .work_actions import apply_reviewed_work_action
    if apply_reviewed_work_action(canonical, assessment, now):
        return
    conflicts = sorted(set(projection.action_conflicts() +
        assessment.account_state.get('_outcome_legacy_conflicts', [])))
    if conflicts:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'outcome_conflict'
        assessment.action_readiness, assessment.next_action = 'watch', 'verify'
        assessment.next_step = 'Resolve conflicting scoped evidence for: ' + ', '.join(conflicts)
        for predicate in conflicts:
            _task(assessment, canonical, 'outcome_conflict:' + predicate,
                  'Review the dated facts for this exact application; do not repeat or reverse a step yet.', now)
        return
    value = projection.value
    selected_step = value('assessment_step')
    step = selected_step if isinstance(selected_step, AssessmentIdentity) else None
    has_scoped_assessment = any(e.assessment is not None for e in projection.events)

    def step_value(predicate):
        if step is None and has_scoped_assessment:
            return None
        return projection.value(predicate, assessment=step)

    if value('application_state') in {'rejected', 'withdrawn'}:
        assessment.lifecycle, assessment.lifecycle_reason = 'closed', 'exact_application_closed'
        assessment.action_readiness, assessment.next_action = 'watch', 'await_change'
        assessment.next_step = 'This application is closed. Do not repeat its assessment; a distinct role or application remains separate.'
        assessment.verification_tasks = []
        return
    if value('project_access') == 'blocked':
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'known_account_block'
        assessment.action_readiness, assessment.next_action = 'watch', 'verify'
        assessment.next_step = 'Wait for exact project-access restoration or resolve this recorded access blocker; do not repeat onboarding.'
        return
    if step_value('assessment_state') in {'completed', 'passed'}:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'assessment_already_completed'
        assessment.action_readiness, assessment.next_action = 'watch', 'await_response'
        assessment.next_step = ('The existing assessment is complete; wait for its outcome or allocation, not another test.'
            if step is None else f'Assessment {step.step_id}, revision {step.revision}, is complete; wait for its outcome or allocation, not another test.')
        assessment.verification_tasks = []
        return
    if assessment.blockers or assessment.account_state.get('applicable_block'):
        return
    if assessment.lifecycle_reason == 'known_account_block':
        return

    if step is None and has_scoped_assessment:
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'assessment_step_not_selected'
        assessment.action_readiness, assessment.next_action = 'verification_needed', 'verify'
        assessment.next_step = 'Confirm the provider-selected assessment step and terms revision for this exact application before using any route or prior approval.'
        _task(assessment, canonical, 'current_assessment_step', assessment.next_step, now)
        return
    if step is not None and step_value('assessment_state') is None:
        assessment.lifecycle, assessment.lifecycle_reason = 'active', 'assessment_step_state_unknown'
        assessment.action_readiness, assessment.next_action = 'verification_needed', 'verify'
        assessment.next_step = f'Confirm the invitation or completion state for assessment {step.step_id}, revision {step.revision}; another step does not establish it.'
        _task(assessment, canonical, 'assessment_step_state', assessment.next_step, now)
        return

    def current(predicate, *, assessment_step=None):
        facts = projection.facts(predicate, assessment=assessment_step)
        return bool(facts) and any(
            fact.source_kind != 'reviewed_seed' and fact.event_at is not None
            and fact.event_at <= now
            and (now - fact.event_at).total_seconds() <= CONFIG.v55.verification_ttl_hours * 3600
            and (fact.expires_at is None or now < fact.expires_at)
            for fact in facts
        )

    if value('buyer_reply') is not None:
        if not current('buyer_reply'):
            assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'buyer_update_needs_recheck'
            assessment.action_readiness, assessment.next_action = 'watch', 'await_change'
            assessment.next_step = 'Recheck the existing buyer thread when due; this dated reply is not a new lead.'
            return
        # Clarification does not claim that an old quote is a contract for new scope.
        revision = value('scope_revision')
        changed = value('buyer_reply') == 'scope_changed' or (
            revision is not None and revision != value('agreed_scope_revision'))
        assessment.lifecycle, assessment.lifecycle_reason = 'active', 'current_buyer_reply'
        assessment.action_readiness, assessment.next_action = 'captured_next_step', 'clarify_scope'
        assessment.next_step = ('Reconfirm the changed deliverable, hosting/support responsibilities and price in the existing buyer thread; no work or payment is established.'
            if changed else 'Review the substantive buyer reply and clarify remaining scope and payment terms in that existing thread.')
        assessment.verification_tasks = [t for t in assessment.verification_tasks if
            t['missing_fact'] not in {'current_open_status', 'application_route'}]
        return
    if step_value('assessment_state') == 'invited':
        checks = step_value('action_checks')
        ready_checks = isinstance(checks, ActionChecks) and all(
            getattr(checks, k) is True for k in ('privacy', 'schedule', 'equipment', 'cost'))
        route = step_value('assessment_route')
        fresh = all(current(k, assessment_step=step) for k in ('assessment_state', 'action_checks', 'assessment_route'))
        if step is not None:
            fresh = fresh and current('assessment_step')
        assessment.lifecycle, assessment.lifecycle_reason = 'active', 'existing_assessment_invitation'
        remaining = [t for t in assessment.verification_tasks if
                     t['missing_fact'] not in {'current_open_status', 'application_route'}]
        if assessment.eligibility != EligibilityStatus.PASSED or remaining:
            assessment.action_readiness, assessment.next_action = 'verification_needed', 'verify'
            assessment.next_step = (remaining[0]['next_step'] if remaining else
                'Confirm the exact assessment requirements against documented eligibility before starting it.')
        elif not ready_checks or not route or not fresh:
            assessment.action_readiness, assessment.next_action = 'verification_needed', 'verify'
            assessment.next_step = ('Confirm current assessment access and explicit privacy, schedule, equipment and cost compatibility before taking the existing test.'
                if step is None else f'Confirm current access and explicit privacy, schedule, equipment and cost compatibility for assessment {step.step_id}, revision {step.revision}; approvals for another step or revision do not apply.')
            _task(assessment, canonical, 'assessment_prerequisites', assessment.next_step, now)
        else:
            assessment.action_readiness, assessment.next_action = 'captured_next_step', 'complete_known_step'
            assessment.next_step = (f'Complete the existing compatible assessment through {route}; invitation is not selection or paid allocation.'
                if step is None else f'Complete assessment {step.step_id}, revision {step.revision}, through {route}; this invitation is not selection or paid allocation.')
            assessment.verification_tasks = [t for t in assessment.verification_tasks if
                t['missing_fact'] not in {'current_open_status', 'application_route'}]
        return
    if value('project_access') == 'accessible' and not current('project_access'):
        assessment.lifecycle, assessment.lifecycle_reason = 'watch', 'access_evidence_stale'
        assessment.action_readiness, assessment.next_action = 'watch', 'verify'
        assessment.next_step = 'Recheck this dated access-restoration evidence before further onboarding.'
        _task(assessment, canonical, 'project_access', assessment.next_step, now)
