"""Offline reviewed source-value/cadence evidence. No source collection or stores."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from jobhound.source_policy import SourceCheck,SourcePlan,SourcePolicyDecision,source_value_report
from jobhound.v41.outcome_review import read_jsonl
from jobhound.versions import runtime_versions

def run(plan_path,checks_path,output_dir,*,as_of,decisions_path=None):
    target=Path(output_dir).resolve();root=Path(__file__).resolve().parent.parent
    if target.exists() or target==root or any(p.casefold() in {'data','state'} for p in target.parts):
        raise ValueError('source_review_requires_new_nonproduction_directory')
    plan_bytes=Path(plan_path).read_bytes()
    if len(plan_bytes)>2_000_000:raise ValueError('source_plan_too_large')
    try:
        plan=SourcePlan.model_validate_json(plan_bytes)
        checks=[SourceCheck.model_validate(r) for r in read_jsonl(checks_path)]
        decisions=[SourcePolicyDecision.model_validate(r) for r in read_jsonl(decisions_path)] if decisions_path else []
        report=source_value_report(plan,checks,decisions,as_of=as_of)
    except (ValueError,TypeError):
        raise ValueError('invalid_source_policy_input') from None
    target.mkdir(parents=True,exist_ok=False)
    (target/'source_value.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    (target/'source_plan.json').write_text(plan.model_dump_json(indent=2),encoding='utf-8')
    (target/'checks.jsonl').write_text(''.join(c.model_dump_json()+'\n' for c in checks),encoding='utf-8')
    manifest=dict(mode='offline_source_policy_review/v1',as_of=as_of.isoformat(),
        plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),checks_sha256=hashlib.sha256(Path(checks_path).read_bytes()).hexdigest(),
        decisions_sha256=hashlib.sha256(Path(decisions_path).read_bytes()).hexdigest() if decisions_path else None,
        runtime_versions=runtime_versions(),input_checks=len(checks),cohorts=len(report['cohorts']),
        network_called=False,production_store_called=False,policy_automatically_changed=False)
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return target

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--checks',type=Path,required=True)
    parser.add_argument('--decisions',type=Path)
    parser.add_argument('--as-of',type=datetime.fromisoformat,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    path=run(args.plan,args.checks,args.output_dir,as_of=args.as_of,decisions_path=args.decisions)
    print('Offline source policy report: '+str(path))

if __name__=='__main__':main()
