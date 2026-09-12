"""Thin release-contract adapter: inputs -> real engine -> output projection.

Assertions are deliberately never passed to the fixture builders. Missing
diagnostics remain missing; the independent supplied evaluator reports failures.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from public_test_context import install
install()
from jobhound.config import CONFIG
from jobhound.filters.eligibility import default_profile
from jobhound.normalize import normalize, remote_signal
from jobhound.v41.models import ListingObservation,SourceKind
from jobhound.v41.engine import evaluate_observations,decision_fingerprint
from jobhound.v41.digest import build_digest
from jobhound.v41.compensation import parse_compensation
from jobhound.v41.requirements import requirement_outcomes
from jobhound.v41.roles import classify_role_semantics
from jobhound.v41.store import V41Store
from v55_contract_retrieval import project_retrieval_case


LANG={'bn':'bengali','hi':'hindi','en':'english','nl':'dutch','de':'german'}
ISO={value:key for key,value in LANG.items()}


def stamp(value):
    if not value:return None
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def make_profile(data):
    profile=copy.deepcopy(default_profile())
    values=dict(data.get('profile') or {})
    values.update(data.get('profile_patch') or {})
    if 'working_languages' in values:
        profile.languages={LANG.get(str(v).casefold(),str(v).casefold()) for v in values['working_languages']}
        profile.working_languages=set(profile.languages)
    profile.credential_domains=set(values.get('declared_capability_domains') or [])
    profile.capability_domains=set(profile.credential_domains)
    credentials=values.get('formal_credentials') or []
    profile.formal_credentials={key for key,value in credentials.items() if value is True} if isinstance(credentials,dict) else set(credentials)
    profile.absent_formal_credentials=set(values.get('absent_formal_credentials') or [])
    if isinstance(credentials,dict):
        profile.absent_formal_credentials.update(key for key,value in credentials.items() if value is False)
    profile.absent_formal_credentials.update(key for key in ['medical_licence','tax_professional','university_degree'] if values.get(key) is False)
    profile.documented_professional_years=dict(values.get('documented_professional_years') or {})
    profile.demonstrated_skills=set(values.get('demonstrated_skills') or [])
    return profile


def make_observations(data,key='fixture'):
    now=stamp(data['as_of'])
    description=str(data.get('description_html') or data.get('description') or '')+'\n'+str(data.get('append_text') or '')
    url=data.get('opportunity_url') or f'https://example.invalid/jobs/{key}'
    raw={'title':data.get('title') or '', 'descriptionHtml':description,
         'jobUrl':url,'_company':data.get('company','Example'),
         'location':data.get('location'),'isRemote':remote_signal(description),
         'publishedAt':data.get('posted_at')}
    job=normalize({'source':'ashby','raw':raw})
    job.pay_raw=data.get('pay_raw')
    job.platform_key=data.get('platform')
    state=data.get('content_state','unknown')
    kind=data.get('source_kind','unknown')
    row=ListingObservation(observation_id=key,source='community' if kind=='community' else 'fixture',job=job,
        raw_payload=raw,normalized_url=url,original_url=url,
        source_kind=SourceKind.UNKNOWN if kind=='community' else SourceKind(kind),
        content_state={'role_complete':'complete','snippet':'snippet_only'}.get(state,state),
        identity_state=data.get('identity_match','uncertain'),
        vacancy_state={'closed':'explicitly_closed'}.get(data.get('vacancy_state'),data.get('vacancy_state','unknown')),
        application_route_state={'public_observed':'observed_public_route'}.get(data.get('application_route'),data.get('application_route','account_unknown')),
        verified_open_at=stamp(data.get('checked_open_at')),captured_at=now)
    account=dict(data.get('account_state') or {})
    account.setdefault('observed_at',now.isoformat())
    account['action_cost']=data.get('action_cost') or {}
    account['verification_attempts']=data.get('verification_attempts',0)
    account['new_material_evidence']=data.get('new_evidence',False)
    account_row=ListingObservation(observation_id=key+':account',source='account_state',origin='account_state',
        parent_observation_ids=[key],captured_at=now,raw_payload=account)
    return [row,account_row]


def project(data):
    profile=make_profile(data)
    now=stamp(data['as_of'])
    old=CONFIG.v55.enabled
    CONFIG.v55.enabled=True
    try:
        with patch('jobhound.v41.engine.default_profile',return_value=profile):
            rows=[]
            if data.get('observations'):
                for observation in data['observations']:
                    observed=copy.deepcopy(data)
                    observed['source_kind']=observation['source_kind']
                    observed['pay_raw']=observation.get('pay')
                    if observation['source_kind']=='aggregator_unresolved':
                        observed['content_state']='snippet'
                    rows.extend(make_observations(observed,observation['id']))
            else:
                rows=make_observations(data)
            retrieval=project_retrieval_case(data)
            for encoded in retrieval.pop('engine_input',{}).get('hydrated_observations',[]):
                rows.append(ListingObservation.model_validate(encoded))
            result=evaluate_observations(rows,as_of=now)
            item=result.evaluated[0]
            assessment=item.assessment
            req=assessment.requirements_assessment
            outcomes=requirement_outcomes(req,profile)
            document=assessment.document_type
            role=classify_role_semantics(item.job.title,item.job.description,item.job.url,profile)
            selected=assessment.selected_pay
            parsed=parse_compensation(selected.raw if selected else str(data.get('pay_raw') or ''),
                labor_hours_per_unit=(data.get('labor_assumption') or {}).get('working_hours_per_recorded_hour'))
            digest=build_digest(result,include_all=True)
            mandatory=[c for c in req.languages if c.required and c.evidence_status!='weak_copy']
            actual={
                'decision':item.decision.model_dump(mode='json'),
                'source':{'hydration_state':'role_complete' if rows[0].content_state=='complete' else rows[0].content_state},
                'requirements':{
                    'mandatory_languages':sorted({ISO.get(str(v),str(v)) for c in mandatory for v in (c.value if isinstance(c.value,list) else [c.value])}),
                    'language_logic':'any' if any(c.match_mode=='any' for c in mandatory) else 'all',
                    'credential_tags':req.credentials_required,
                    'mandatory_degree':any(c.required for c in req.degrees),
                    'required_years':{c.scope:float(c.value) for c in req.experience if c.required},
                    'alternative_satisfied':bool(outcomes.alternatives_satisfied),
                    'experience_evidence_fabricated':any(c.scope not in profile.documented_professional_years and c.evidence_status=='fulfilled' for c in req.experience),
                    'coverage_sufficient':req.completeness=='complete',
                    'seniority':assessment.seniority,
                },
                'location':{'worldwide_confirmed':any('worldwide' in c.value and c.polarity=='positive' for c in req.location_scope)},
                'document':{'type':{'individual_job':'individual_vacancy'}.get(document,document)},
                'role':asdict(role),
                'pay':{
                    **asdict(parsed), 'status':'undisclosed' if not assessment.pay_candidates else assessment.pay_assessment.state.value,
                    'material_conflict':assessment.pay_conflict,
                    'buyer_budget':selected.fixed_amount_usd if selected and assessment.buyer_intent!='seller' else None,
                    'modeled_calendar_hour_rate':item.job.effective_hourly_usd,
                    'first_payment_within_seven_days_confirmed':assessment.time_to_cash_days is not None and assessment.time_to_cash_days<=7,
                    'claim_is_employer_confirmed':bool(selected and selected.observation_source_kind in {SourceKind.ORIGINAL_ATS,SourceKind.ORIGINAL_EMPLOYER}),
                    'candidate_observation_ids':[p.observation_id for p in assessment.pay_candidates],
                },
                'access':{'project_access':assessment.account_state.get('project_access','unknown'),
                          'tasks_allocated':assessment.account_state.get('tasks_allocated') is True or assessment.account_state.get('task_allocation')=='allocated'},
                'lifecycle':{'state':item.decision.lifecycle,'vacancy_state':rows[0].vacancy_state},
                'freshness':{'original_posted_date':item.job.posted_at.date().isoformat() if item.job.posted_at else None,
                    'open_check_current':not any(t['missing_fact']=='current_open_status' for t in item.decision.verification_tasks),
                    'used_minimum_floor':assessment.freshness==0.1,
                    'is_new_by_scrape_only':item.job.posted_at==now and data.get('posted_at') is None},
                'verification':item.decision.verification_tasks[0] if item.decision.verification_tasks else {},
                'presentation':{'daily_action_allowed':item.decision.lifecycle=='active' and item.decision.action_band.value!='reject',
                    'displayed_primary':digest.display_counts['displayed_primary'],
                    'displayed_verify':digest.display_counts['displayed_verify'],
                    'padding_count':len(digest.displayed)-len({r.canonical.canonical_id for r in digest.displayed}),
                    'audit_record_count':len(result.evaluated)},
                'ledgers':{'presentation_balanced':digest.accounting_ok},
                'ranking':{'rendered_key_matches_actual':all(f'readiness {r.decision.priority_key.action_readiness}' in digest.text for r in digest.displayed)},
            }
            if data.get('freshness_test'):
                from jobhound.enrich.trust import freshness
                f=data['freshness_test']
                actual['freshness']['test_decay']=freshness(now-timedelta(days=f['age_days']),now=now,half_life_days=f['half_life_days'])
            pair=data.get('pair_fixture') or {}
            mutation=data.get('mutation_fixture') or {}
            if mutation.get('append_boilerplate'):
                changed=copy.deepcopy(data)
                changed['append_text']=str(data.get('append_text') or '')+'\n'+mutation['append_boilerplate']
                changed_result=evaluate_observations(make_observations(changed),as_of=now)
                actual['ranking']['fit_unchanged']=item.decision.priority_key.match_strength==changed_result.evaluated[0].decision.priority_key.match_strength
            if pair.get('type') in {'same_platform_different_postings','same_employer_different_languages'}:
                twins=[]
                for index,key in enumerate(pair['posting_ids']):
                    d=copy.deepcopy(data)
                    d['opportunity_url']=f'https://example.invalid/jobs/{key}'
                    if pair.get('platform'):
                        d['platform']=pair['platform']
                        d['company']=f'Buyer {key}'
                    if pair.get('languages'):
                        language=LANG[pair['languages'][index]]
                        d['title']=d['title'].replace('Bengali',language.title())
                        d['description_html']=d['description_html'].replace('Bengali',language.title())
                    twins.extend(make_observations(d,key))
                actual['identity']={'canonical_count':len(evaluate_observations(twins,as_of=now).evaluated)}
            if pair.get('type')=='allocated_work_vs_pool':
                twins=[]
                for key in ['allocated_work','pool']:
                    d=copy.deepcopy(data)
                    d['opportunity_url']=f'https://example.invalid/jobs/{key}'
                    if key=='allocated_work':
                        d['pay_raw']=f"USD {pair['allocated_rate_usd']} per working hour"
                        d['account_state'].update({'tasks_allocated':True,'project_access':'verified','assessment':'passed','payout_setup':'verified'})
                    else:
                        d['title']='Bengali AI evaluator talent pool'
                        d['append_text']='Join our talent pool for possible future projects. No tasks are guaranteed.'
                        d['pay_raw']=f"Up to USD {pair['pool_headline_rate_usd']} per hour"
                    twins.extend(make_observations(d,key))
                ranked=evaluate_observations(twins,as_of=now).evaluated
                actual['ranking']['first']=next(o.observation_id for o in ranked[0].canonical.observations if o.origin!='account_state')
            if pair.get('run_with_reversed_input'):
                twins=[]
                for key in pair.get('ids',['a','b']):
                    d=copy.deepcopy(data); d['opportunity_url']=f'https://example.invalid/jobs/{key}'
                    twins.extend(make_observations(d,key))
                left=evaluate_observations(twins,as_of=now)
                right=evaluate_observations(list(reversed(twins)),as_of=now)
                actual['ranking']['same_after_shuffle']=decision_fingerprint(left)==decision_fingerprint(right)
            if data.get('presentation_fixture'):
                fixture=data['presentation_fixture']; many=[]
                for band in ['primary','verify']:
                    for i in range(fixture.get(band+'_count',0)):
                        d=copy.deepcopy(data); key=f'{band}-{i}';d['opportunity_url']=f'https://example.invalid/jobs/{key}'
                        d['content_state']='role_complete' if band=='primary' else 'snippet'
                        many.extend(make_observations(d,key))
                multi=evaluate_observations(many,as_of=now)
                preview=build_digest(multi,include_all=True)
                actual['presentation'].update({'audit_record_count':len(multi.evaluated),
                    'displayed_primary':preview.display_counts['displayed_primary'],
                    'displayed_verify':preview.display_counts['displayed_verify']})
                actual['ledgers']['presentation_balanced']=preview.accounting_ok
            with TemporaryDirectory(prefix='jobhound-contract-') as directory:
                store=V41Store(Path(directory)/'state.db')
                first=store.annotate_transitions(result)
                actual['notifications']={'send_new_opportunity':bool(first)}
                if data.get('notification_fixture'):
                    fixture=data['notification_fixture']
                    store.record_run(result)
                    store.mark_notified(result.metadata.run_id,first,'test',fixture.get('previous_delivery')=='succeeded')
                    second=store.annotate_transitions(result)
                    actual['notifications'].update({'retry_delivery_allowed':bool(second),'send_duplicate':bool(second)})
                store.close()
            for section,values in retrieval.items():
                actual.setdefault(section,{}).update(values)
            return actual
    finally:
        CONFIG.v55.enabled=old


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--cases',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    outputs=[]
    for line in args.cases.read_text(encoding='utf-8').splitlines():
        case=json.loads(line)
        try: actual=project(case['input'])
        except Exception as exc: actual={'adapter_error':f'{type(exc).__name__}: {exc}'}
        outputs.append({'case_id':case['id'],'actual':actual})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(''.join(json.dumps(row,ensure_ascii=False,default=str)+'\n' for row in outputs),encoding='utf-8')
    print(json.dumps({'outputs':len(outputs),'adapter_errors':sum('adapter_error' in row['actual'] for row in outputs)}))
