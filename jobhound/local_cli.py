"""Keyless local entrypoints. Importing this module loads no operator settings."""
import argparse
import json
import sqlite3
import sys


def configure_parser(parser):
    sub=parser.add_subparsers(dest='local_command',required=True)
    init=sub.add_parser('init',help='create a new isolated workspace from offline examples')
    init.add_argument('--data-dir',required=True);init.add_argument('--profile',default='local')
    init.set_defaults(operation='init')
    profile=sub.add_parser('profile',help='add a distinct local profile with neutral examples')
    profile.add_argument('action',choices=['add']);profile.add_argument('--data-dir',required=True)
    profile.add_argument('--profile',required=True);profile.set_defaults(operation='profile')
    doctor=sub.add_parser('doctor',help='read-only runtime/policy/closed-state checks')
    doctor.add_argument('--data-dir');doctor.add_argument('--profile');doctor.set_defaults(operation='doctor')
    demo=sub.add_parser('demo',help='synthetic native engine/outcome preview; no providers')
    demo.add_argument('--data-dir',required=True);demo.add_argument('--profile');demo.add_argument('--name',default='demo')
    demo.set_defaults(operation='demo')
    review=sub.add_parser('review',help='existing offline replay/outcome workflow using the selected local profile')
    reviews=review.add_subparsers(dest='local_review_command',required=True)
    for command in ('replay','outcomes'):
        item=reviews.add_parser(command)
        item.add_argument('snapshot');item.add_argument('--data-dir',required=True)
        item.add_argument('--profile');item.add_argument('--name',required=True)
        item.set_defaults(operation='review')
        if command=='outcomes':
            item.add_argument('--events',required=True);item.add_argument('--bindings',required=True)
            item.add_argument('--as-of',required=True);item.add_argument('--previous-events');item.add_argument('--aliases')
            item.add_argument('--handoff-signal',action='store_true');item.add_argument('--delivery-state');item.add_argument('--reissue-plan')
    backup=sub.add_parser('backup',help='verify and copy all profiles to a NEW directory outside the workspace')
    backup.add_argument('--data-dir',required=True);backup.add_argument('--output',required=True)
    backup.set_defaults(operation='backup')
    restore=sub.add_parser('restore',help='verify backup checksums/integrity and restore to a NEW workspace')
    restore.add_argument('--backup',required=True);restore.add_argument('--data-dir',required=True)
    restore.set_defaults(operation='restore')
    parser.set_defaults(func=dispatch)
    return parser


def dispatch(args):
    from . import local_state as state
    try:
        if args.operation=='init':report=state.initialize(args.data_dir,args.profile)
        elif args.operation=='profile':report=state.add_profile(args.data_dir,args.profile)
        elif args.operation=='doctor':report=state.doctor(args.data_dir,args.profile)
        elif args.operation=='backup':report=state.backup(args.data_dir,args.output)
        elif args.operation=='restore':report=state.restore(args.backup,args.data_dir)
        elif args.operation=='review':report=review_files(args)
        else:
            from .local_demo import demonstrate
            report=demonstrate(args.data_dir,args.profile,args.name)
    except (ValueError,OSError,sqlite3.Error,ImportError,RuntimeError) as error:
        # No YAML snippets, credential fields or individual state records in errors.
        code=str(error) if type(error) in (ValueError,RuntimeError) else 'local_'+type(error).__name__.lower()
        print(json.dumps(dict(ok=False,error=code)),file=sys.stderr)
        return 1
    visible={key:value for key,value in report.items() if key!='runtime_versions'}
    if args.operation=='demo':
        visible['preview']=report['output']+'/updated/preview.md'
        visible['verification_receipt']=report['output']+'/demo-receipt.json'
    print(json.dumps(visible,indent=2,ensure_ascii=False))
    return 0 if report.get('ok',True) else 1


def review_files(args):
    from datetime import datetime,timezone
    from pathlib import Path
    from .local_state import paths,load_policies
    from .local_paths import local_scope,profile_name
    local=paths(args.data_dir,args.profile);config,operator,registry=load_policies(local)
    target=local.reviews_dir/profile_name(args.name)
    if target.exists():raise ValueError('local_review_output_must_be_new')
    if args.local_review_command=='outcomes':
        try:now=datetime.fromisoformat(args.as_of.replace('Z','+00:00'))
        except ValueError:raise ValueError('invalid_local_review_clock') from None
        if now.tzinfo is None:raise ValueError('local_review_clock_must_be_aware')
        if args.delivery_state:
            previous=Path(args.delivery_state).resolve()
            if local.reviews_dir not in previous.parents:raise ValueError('local_delivery_state_belongs_to_other_profile')
    else:now=datetime.now(timezone.utc)
    with local_scope(local):
        from .run_context import RunContext,run_scope
        context=RunContext.capture(config=config,profile=operator,registry=registry,as_of=now,
            workspace=target,network_allowed=False,side_effects_allowed=False)
        with run_scope(context):
            if args.local_review_command=='outcomes':
                from .v41.outcome_review import run_outcome_review
                run_outcome_review(args.snapshot,args.events,args.bindings,target,as_of=now,
                    previous_events=args.previous_events,aliases_path=args.aliases,handoff_signal=args.handoff_signal,
                    delivery_state=args.delivery_state,reissue_plan_path=args.reissue_plan)
            else:
                from .v41.review import replay_review
                replay_review(args.snapshot,target)
    audit=json.loads((target/'audit.json').read_text(encoding='utf-8'))
    return dict(schema='jobhound-local-review/v1',profile=local.profile,command=args.local_review_command,
        output=str(target),accounting_ok=audit['accounting_ok'],network_calls=0,provider_calls=0)


def main(argv=None):
    parser=configure_parser(argparse.ArgumentParser(prog='jobhound local',description='Offline personal local workspace'))
    return dispatch(parser.parse_args(argv))


if __name__=='__main__':raise SystemExit(main())
