"""Scope-bound due times and substantive update evidence; no execution authority."""
from datetime import timezone
import math

from ..config import CONFIG
from .outcomes import ActionDeadline,AssessmentIdentity,WorkIdentity

RESERVED_SIGNAL_ORDER='_reviewed_signal_order'
URGENT_WINDOW_HOURS=24


def _current(facts,now):
    return any(e.source_kind!='reviewed_seed' and e.event_at is not None and e.event_at<=now
        and (now-e.event_at).total_seconds()<=CONFIG.v55.verification_ttl_hours*3600
        and (e.expires_at is None or now<e.expires_at) for e in facts)


def apply_action_constraints(canonical,assessment,now,*,thread=None):
    """Unknown feasibility prevents urgency; an expired due time never closes a vacancy."""
    from .action_policy import _task
    if canonical.outcome_binding_hold or assessment.lifecycle=='closed':
        return
    projection=canonical.outcome_projection
    ordering=None
    if thread is not None and thread.schema_version=='community-thread/v2' and thread.complete and thread.intent=='buyer':
        revision=next((r for r in thread.revisions if r.revision_id==thread.current_terms_revision_id),None)
        if (revision is not None and revision.kind in {'scope_changed','budget_changed'}
                and revision.actor_id==thread.buyer_actor_id and revision.created_at is not None
                and revision.created_at<=now and (now-revision.created_at).total_seconds()<=CONFIG.v55.verification_ttl_hours*3600
                and thread.terms_state not in {'ambiguous','incomplete'}):
            ordering=dict(authority='reviewed_signal_order_v1',substantive_buyer_update=1,
                          buyer_revision=revision.revision_id,buyer_post_id=revision.post_id,
                          update_at=revision.created_at.isoformat())
    if projection is not None and assessment.next_action=='clarify_scope' and _current(projection.facts('buyer_reply'),now):
        ordering=dict(authority='reviewed_signal_order_v1',substantive_buyer_update=1,
                      attempt_scope=projection.scope.model_dump(),basis='current_exact_buyer_reply')
    if ordering is not None:
        assessment.account_state[RESERVED_SIGNAL_ORDER]=ordering
    if projection is None or assessment.next_action in {'await_response','await_change','reconcile_payment_status'}:
        return
    work=projection.value('work_selection');step=projection.value('assessment_step')
    if isinstance(work,WorkIdentity):
        scope=dict(work=work);predicate,cost_predicate='work_deadline','work_cost'
    elif isinstance(step,AssessmentIdentity):
        scope=dict(assessment=step);predicate,cost_predicate='assessment_deadline','action_cost'
    else:
        return
    facts=projection.facts(predicate,**scope)
    if not facts:
        return
    deadline=projection.value(predicate,**scope)
    cost=projection.value(cost_predicate,**scope)
    view=dict(authority='reviewed_signal_order_v1',attempt_scope=projection.scope.model_dump(),
              target=(work if 'work' in scope else step).model_dump(),predicate=predicate,
              deadline=deadline.model_dump(mode='json') if isinstance(deadline,ActionDeadline) else None,
              status='unresolved',supported_urgency=0,supported_deadline_order=0)
    assessment.account_state[RESERVED_SIGNAL_ORDER]=view
    def check(code,instruction,*,expired=False):
        _task(assessment,canonical,code,instruction,now)
        assessment.lifecycle='watch' if expired else 'active'
        assessment.lifecycle_reason=code
        assessment.action_readiness='verification_needed'
        assessment.next_action='verify';assessment.next_step=instruction
        plan=assessment.account_state.get('_reviewed_work_action')
        if isinstance(plan,dict) and code not in plan['prerequisites']:plan['prerequisites'].append(code)
    if not isinstance(deadline,ActionDeadline) or not _current(facts,now):
        check('action_deadline_evidence','Confirm the dated, non-conflicting counterparty due time for this exact step/allocation; a reminder or seed is not an extension.')
        return
    remaining=(deadline.due_at-now).total_seconds()/60
    if remaining<=0:
        view['status']='elapsed'
        check('action_deadline_elapsed','Confirm an explicit extension for this exact step/allocation before repeating or starting it; retain the original due-time evidence.',expired=True)
        return
    minutes=getattr(cost,'minutes',None)
    if (type(minutes) not in (int,float) or not math.isfinite(minutes)
            or not _current(projection.facts(cost_predicate,**scope),now)):
        check('action_deadline_duration','Review a current time estimate for this exact step/allocation against its stated due time; unknown duration is not zero effort.')
        return
    required=max(minutes,deadline.minimum_lead_minutes or 0)
    view.update(estimated_minutes=minutes,required_lead_minutes=required)
    if remaining<required:
        view['status']='estimate_exceeds_window'
        check('action_deadline_infeasible','The reviewed time estimate or stated minimum lead exceeds the remaining due-time window. Confirm feasibility or an explicit extension before starting.')
        return
    view['status']='estimated_feasible'
    if (assessment.next_action in {'complete_known_step','start_allocated_task'}
            and assessment.lifecycle=='active' and not assessment.verification_tasks
            and not projection.action_conflicts() and remaining<=URGENT_WINDOW_HOURS*60):
        view.update(supported_urgency=1,supported_deadline_order=-int(deadline.due_at.astimezone(timezone.utc).timestamp()))


def priority_fields(canonical,assessment):
    """Reserved metadata is generated from retained facts, never a captured overlay."""
    view=assessment.account_state.get(RESERVED_SIGNAL_ORDER) or {}
    if view.get('authority')!='reviewed_signal_order_v1' or canonical.outcome_binding_hold:
        return {}
    ready=(assessment.lifecycle=='active' and not assessment.verification_tasks
           and assessment.next_action in {'complete_known_step','start_allocated_task'})
    substantive=int(assessment.lifecycle=='active' and assessment.next_action in {'verify','clarify_scope'}
                    and bool(view.get('substantive_buyer_update')))
    return dict(supported_urgency=int(ready and view.get('supported_urgency')==1),
                supported_deadline_order=view.get('supported_deadline_order',0) if ready else 0,
                substantive_buyer_update=substantive)
