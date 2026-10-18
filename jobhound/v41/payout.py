"""Amount-specific payout constraints from the selected pay observation.

This checks a stated minimum, never account readiness or actual settlement.
Unrecognized terms remain unknown; no provider policy or FX rate lives here.
"""
from decimal import Decimal
import re

from .models import Evidence

_METHOD = re.compile(r'\b(?:wire(?: transfer)?|bank transfer|PayPal|Payoneer|Wise)\b', re.I)
_CONTEXT = re.compile(r'\b(?:payment|payout|paid|payable|minimum|threshold|amount|available)\b', re.I)
_MINIMUM = re.compile(
    r'(?P<op>above|over|more than|greater than|at least|minimum(?: payout)?(?: of)?|>=|>)\s*'
    r'(?P<currency>USD|INR|EUR|GBP|CAD|AUD|\$|€|£)?\s*'
    r'(?P<amount>\d+(?:\.\d+)?)(?![\d,.])\b', re.I)
_CURRENCIES = {'$': 'USD', '€': 'EUR', '£': 'GBP'}


def apply_payout_constraints(canonical, assessment, now):
    """Keep terms bound to the observation supplying this project's pay."""
    selected = assessment.selected_pay
    source_id = selected.observation_id if selected else canonical.best_observation.observation_id
    observation = next((o for o in canonical.observations if o.observation_id == source_id), None)
    if observation is None or observation.job is None or observation.origin == 'account_state':
        return
    amount = None
    if (selected is not None and not assessment.pay_conflict and not selected.estimated
            and not selected.up_to and selected.actual_unit == 'fixed_project'
            and selected.amount_low is not None and selected.amount_low == selected.amount_high):
        amount = Decimal(str(selected.amount_low))
    currency = selected.currency if selected is not None else None
    routes = []
    # Deliberately narrow: a method and its condition must share one clause.
    # Commas separate alternate rails; decimal punctuation stays intact.
    for clause in re.split(r'[;\n]|,(?!\d)|\.(?=\s|$)', observation.job.description):
        methods = list(_METHOD.finditer(clause))
        if not methods or not _CONTEXT.search(clause):
            continue
        for method in methods:
            route = dict(method=method.group().casefold(), state='unknown',
                         observation_id=source_id, source_field='description', span=clause.strip())
            minimum = _MINIMUM.search(clause)
            if re.search(r'\b(?:fees?|waiv\w*|tiers?|discounts?|rebates?|bonus|cashback)\b', clause, re.I):
                route['reason'] = 'non_minimum_condition'
            elif re.search(r'\b(?:not|unavailable|unsupported|disabled)\b', clause, re.I):
                route['reason'] = 'applicability_unresolved'
            elif len(methods) != 1:
                route['reason'] = 'method_condition_binding_unknown'
            elif re.search(r'\bno minimum(?: payout)?\b', clause, re.I):
                route.update(state='amount_compatible' if amount is not None else 'unknown',
                             reason='explicit_no_minimum')
            elif minimum and re.search(r'\b(?:requires?|only|minimum|threshold|eligible|available|payable)\b', clause, re.I):
                raw_currency = minimum['currency'] or ''
                threshold_currency = _CURRENCIES.get(raw_currency, raw_currency.upper()) or None
                strict = minimum['op'].casefold() in {'above', 'over', 'more than', 'greater than', '>'}
                threshold = Decimal(minimum['amount'])
                route.update(minimum=minimum['amount'], currency=threshold_currency,
                             comparison='>' if strict else '>=')
                if amount is None:
                    route['reason'] = 'single_project_amount_unknown'
                elif not currency or not threshold_currency:
                    route['reason'] = 'currency_unknown'
                elif currency != threshold_currency:
                    route['reason'] = 'currency_mismatch'
                else:
                    compatible = amount > threshold if strict else amount >= threshold
                    route.update(state='amount_compatible' if compatible else 'single_amount_infeasible',
                                 reason='minimum_satisfied' if compatible else 'minimum_not_satisfied')
            else:
                route['reason'] = 'threshold_unknown'
            routes.append(route)
            assessment.evidence.append(Evidence(dimension='payout', code='amount_specific_payout',
                value=route, source_field='description', observation_id=source_id,
                span=clause.strip(), source_kind=observation.source_kind))
    if not routes:
        return
    # Conflicting terms for one method cannot establish a compatible alternative.
    grouped = {}
    for route in routes:
        key = 'wire' if route['method'].startswith('wire') else route['method']
        grouped.setdefault(key, []).append(route)
    compatible = any(all(r['state'] == 'amount_compatible' for r in group) for group in grouped.values())
    state = ('amount_compatible' if compatible else 'single_amount_infeasible'
             if all(r['state'] == 'single_amount_infeasible' for r in routes) else 'unknown')
    assessment.payout_feasibility = dict(state=state, amount=str(amount) if amount is not None else None,
        currency=currency, routes=routes, caveats=[
            'Compatibility checks the stated amount only; account applicability, fees and settlement remain unconfirmed.',
            'No aggregation balance or alternative payout rail is inferred.'])
    if compatible:
        return
    assessment.time_to_cash_days = None
    assessment.unresolved.append('payout_feasibility:' + state)
    # Closed vacancies and existing invoice/payment bookkeeping retain their action.
    if (assessment.lifecycle == 'closed' or assessment.blockers or assessment.next_action not in {
            'apply', 'verify', 'complete_known_step', 'start_allocated_task'}):
        return
    from .action_policy import _task
    step = ('The stated payout minimum cannot be met by this single project amount. '
            'Confirm an evidenced applicable alternative or supported aggregation before starting work.'
            if state == 'single_amount_infeasible' else
            'Confirm the payout threshold, currency and account applicability for this exact project amount before starting work.')
    _task(assessment, canonical, 'payout_feasibility', step, now)
    assessment.action_readiness = 'verification_needed'
    assessment.next_action, assessment.next_step = 'verify', step
    plan = assessment.account_state.get('_reviewed_work_action')
    if isinstance(plan, dict):
        plan['prerequisites'].append('payout_feasibility')
        from .work_actions import finalize_work_action_view
        finalize_work_action_view(assessment)
