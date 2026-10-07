"""Explicit source overlay and measured bounded-check evidence.

This policy neither replaces discovery configuration nor admits a parser from a
provider name. Missing coverage, novelty, effort and outcomes stay unknown.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

Opaque = Annotated[str, Field(min_length=1, max_length=160, pattern=r'^[\w.:-]+$')]
Count = Annotated[int, Field(strict=True, ge=0, le=100000)]


class ReviewedModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)

    @field_validator('reviewed', check_fields=False, mode='before')
    @classmethod
    def explicit_review(cls, value):
        if value is not True:
            raise ValueError('explicit_review_required')
        return value


class CategoryLimit(ReviewedModel):
    category: Opaque
    requests: Count
    inspections: Count
    searches: Count


class SourceRoute(CategoryLimit):
    source_id: Opaque
    family: Opaque
    mode: Literal['core', 'pilot', 'watch']
    parser_state: Literal['capture_only', 'admitted', 'unsupported']
    hosts: tuple[Annotated[str, Field(pattern=r'^[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$')], ...] = Field(min_length=1, max_length=8)
    task_scope_ref: Opaque
    evidence_refs: tuple[Opaque, ...] = Field(min_length=1, max_length=20)
    admission_ref: Opaque | None = None
    next_check_at: AwareDatetime | None = None
    reviewed: Literal[True]

    @model_validator(mode='after')
    def bounded_admission(self):
        if self.mode != 'watch' and self.admission_ref is None:
            raise ValueError('active_route_requires_logged_admission')
        if len(set(self.hosts)) != len(self.hosts):
            raise ValueError('duplicate_hosts')
        return self


class SourcePlan(ReviewedModel):
    schema_version: Literal[1]
    request_ceiling: Annotated[int, Field(strict=True, ge=0, le=60)] = 60
    inspection_ceiling: Annotated[int, Field(strict=True, ge=0, le=12)] = 12
    search_ceiling: Annotated[int, Field(strict=True, ge=0, le=12)] = 12
    routes: tuple[SourceRoute, ...] = Field(min_length=1, max_length=64)
    categories: tuple[CategoryLimit, ...] = Field(min_length=1, max_length=32)
    policy_decisions: tuple[SourcePolicyDecision, ...] = Field(default=(),max_length=100)
    reviewed: Literal[True]

    @field_validator('schema_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError('integer_schema_version_required')
        return value

    @model_validator(mode='after')
    def limits(self):
        if len({r.source_id for r in self.routes}) != len(self.routes):
            raise ValueError('duplicate_source_id')
        cats={c.category:c for c in self.categories}
        if len(cats)!=len(self.categories):
            raise ValueError('duplicate_category')
        if len({r.family for r in self.routes if r.mode=='pilot'})>2:
            raise ValueError('at_most_two_pilot_families')
        for row in (*self.routes,*self.categories):
            if row.requests>self.request_ceiling or row.inspections>self.inspection_ceiling or row.searches>self.search_ceiling:
                raise ValueError('route_or_category_exceeds_shared_ceiling')
        for route in self.routes:
            if route.category not in cats:
                raise ValueError('unknown_category')
            if route.mode!='watch':
                admission=next((d for d in self.policy_decisions if d.decision_id==route.admission_ref),None)
                if admission is None or admission.source_id!=route.source_id or admission.disposition not in {'continue','promote','resume','reduce_cadence'}:
                    raise ValueError('active_route_requires_matching_policy_decision')
                if admission.disposition=='reduce_cadence' and (route.next_check_at is None or route.next_check_at<=admission.observed_at):
                    raise ValueError('reduced_cadence_requires_next_check')
                if any(d.source_id==route.source_id and d.decision_id!=admission.decision_id
                       and d.observed_at>=admission.observed_at and d.disposition=='pause'
                       for d in self.policy_decisions):
                    raise ValueError('paused_route_requires_later_resume')
        if len({d.decision_id for d in self.policy_decisions})!=len(self.policy_decisions):
            raise ValueError('duplicate_policy_decision')
        return self

    def route(self, source_id):
        return next((r for r in self.routes if r.source_id==source_id),None)

    def category(self, category):
        return next(c for c in self.categories if c.category==category)


class SourceCheck(ReviewedModel):
    check_id: Opaque
    source_id: Opaque
    query_family: Opaque
    profile_hash: Annotated[str, Field(pattern=r'^[0-9a-f]{16,64}$')]
    window_start: AwareDatetime
    window_end: AwareDatetime
    observed_at: AwareDatetime
    allowed_pages: tuple[Opaque, ...] = Field(min_length=1, max_length=200)
    fetched_pages: tuple[Opaque, ...] = Field(max_length=200)
    bounded_set_complete: StrictBool
    retrieval_state: Literal['complete','partial','failed','throttled','unsupported']
    omissions: tuple[Opaque, ...] = Field(default=(),max_length=30)
    novelty_evaluated: StrictBool
    novel_action_ids: tuple[Opaque, ...] | None = Field(default=None,max_length=500)
    original_opportunity_ids: tuple[Opaque, ...] | None = Field(default=None,max_length=500)
    duplicate_observations: Count | None = None
    suitable_candidates: Count | None = None
    review_minutes: float | None = Field(default=None,strict=True,allow_inf_nan=False,ge=0,le=10000)
    external_cost: float | None = Field(default=None,strict=True,allow_inf_nan=False,ge=0,le=100000)
    cost_currency: str | None = Field(default=None,pattern=r'^[A-Z]{3}$')
    transition_refs: tuple[Opaque, ...] | None = Field(default=None,max_length=500)
    exclusion_codes: tuple[Opaque, ...] = Field(default=(),max_length=50)
    evidence_ref: Opaque
    support_sha256: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    reviewed: Literal[True]

    @model_validator(mode='after')
    def consistent_check(self):
        if self.window_end<=self.window_start or self.observed_at<self.window_end:
            raise ValueError('invalid_check_window')
        for values in (self.allowed_pages,self.fetched_pages,self.novel_action_ids,
                       self.original_opportunity_ids,self.transition_refs):
            if values is not None and len(set(values))!=len(values):
                raise ValueError('duplicate_check_identity')
        if not set(self.fetched_pages)<=set(self.allowed_pages):
            raise ValueError('fetched_page_outside_planned_set')
        if self.external_cost is not None and self.cost_currency is None:
            raise ValueError('explicit_cost_currency_required')
        if self.novel_action_ids is not None and not self.novelty_evaluated:
            raise ValueError('novelty_count_requires_review')
        return self

    @property
    def adequate(self):
        return (self.retrieval_state=='complete' and self.bounded_set_complete
                and set(self.fetched_pages)==set(self.allowed_pages) and not self.omissions
                and self.novelty_evaluated and self.novel_action_ids is not None)


class SourcePolicyDecision(ReviewedModel):
    decision_id: Opaque
    source_id: Opaque
    observed_at: AwareDatetime
    disposition: Literal['continue','reduce_cadence','pause','promote','resume']
    reason: Opaque
    evidence_refs: tuple[Opaque, ...] = Field(min_length=1,max_length=30)
    reviewed: Literal[True]


SourcePlan.model_rebuild()


def source_value_report(plan: SourcePlan, checks, decisions=(), *, as_of):
    """Reviewed incidence and cadence signals; no finder/earnings attribution."""
    if as_of.tzinfo is None:raise ValueError('source_report_requires_aware_clock')
    known={r.source_id for r in plan.routes};unique={}
    for check in checks:
        if check.observed_at>as_of:raise ValueError('future_source_check')
        if check.source_id not in known:raise ValueError('check_for_unknown_route')
        if check.check_id in unique and unique[check.check_id]!=check:
            raise ValueError('check_id_collision')
        unique[check.check_id]=check
    policy={}
    for decision in (*plan.policy_decisions,*decisions):
        if decision.observed_at>as_of:raise ValueError('future_source_policy_decision')
        if decision.source_id not in known:raise ValueError('decision_for_unknown_route')
        if decision.decision_id in policy and policy[decision.decision_id]!=decision:
            raise ValueError('policy_decision_id_collision')
        policy[decision.decision_id]=decision
    groups=defaultdict(list)
    # Unequal profiles/query families are separate cohorts, not a head-to-head.
    for check in unique.values():groups[(check.source_id,check.query_family,check.profile_hash)].append(check)
    rows=[];union_actions=set()
    for (source_id,query,profile),cohort in sorted(groups.items()):
        cohort.sort(key=lambda c:(c.window_end,bool(c.novel_action_ids),c.check_id))
        streak=0;adequate=[];actions=set();zero_end=None;zero_checks=[];overlapping=[]
        for check in cohort:
            if check.adequate:
                adequate.append(check.check_id)
            # Finding an action is positive evidence even when the remaining
            # pages were unavailable. An incomplete empty check proves no zero.
            if check.novel_action_ids:
                streak=0;zero_end=None;zero_checks=[]
            elif check.adequate:
                if zero_end is None or check.window_start>=zero_end:
                    streak+=1;zero_end=check.window_end;zero_checks.append(check.check_id)
                else:
                    overlapping.append(check.check_id)
            if check.novel_action_ids is not None:actions.update(check.novel_action_ids)
        union_actions.update(actions)
        measured_minutes=[c.review_minutes for c in cohort if c.review_minutes is not None]
        originals={identity for c in cohort for identity in (c.original_opportunity_ids or ())}
        duplicates=[c.duplicate_observations for c in cohort if c.duplicate_observations is not None]
        suitable=[c.suitable_candidates for c in cohort if c.suitable_candidates is not None]
        costs=defaultdict(float)
        for check in cohort:
            if check.external_cost is not None:costs[check.cost_currency]+=check.external_cost
        rows.append(dict(source_id=source_id,query_family=query,profile_hash=profile,
            checks=len(cohort),adequately_covered_checks=len(adequate),adequate_check_ids=adequate,
            inadequate_check_ids=[c.check_id for c in cohort if not c.adequate],
            known_unique_action_ids=sorted(actions),unknown_novelty_checks=sum(c.novel_action_ids is None for c in cohort),
            covered_zero_yield_streak=streak,cadence_review_required=streak>=5,
            covered_zero_yield_check_ids=zero_checks,overlapping_zero_check_ids=overlapping,
            measured_review_minutes=sum(measured_minutes) if measured_minutes else None,
            unmeasured_effort_checks=sum(c.review_minutes is None for c in cohort),
            known_original_opportunity_ids=sorted(originals),unknown_originals_checks=sum(c.original_opportunity_ids is None for c in cohort),
            known_duplicate_observations=sum(duplicates) if duplicates else None,
            unknown_duplicate_checks=sum(c.duplicate_observations is None for c in cohort),
            known_suitable_candidate_occurrences=sum(suitable) if suitable else None,
            unknown_suitability_checks=sum(c.suitable_candidates is None for c in cohort),
            measured_external_cost_by_currency=dict(sorted(costs.items())),
            unmeasured_cost_checks=sum(c.external_cost is None for c in cohort),
            known_transition_refs=sorted({ref for c in cohort for ref in (c.transition_refs or ())}),
            unknown_transition_checks=sum(c.transition_refs is None for c in cohort),
            critical_exclusion_codes=sorted({code for c in cohort for code in c.exclusion_codes}),
            policy_decision_ids=sorted(d.decision_id for d in policy.values() if d.source_id==source_id)))
    return dict(schema='jobhound-source-value/v1',cohorts=rows,union_known_action_ids=sorted(union_actions),
        checked_routes=sorted({c.source_id for c in unique.values()}),
        unchecked_routes=sorted(known-{c.source_id for c in unique.values()}),
        limitations=['Known action incidence is not first-discoverer attribution or a conversion rate',
                    'Unmeasured effort and inadequate coverage are not zero',
                    'Cadence review is a signal; only an explicit logged decision changes policy'])
