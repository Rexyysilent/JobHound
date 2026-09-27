"""Explicit isolated trial commands: python -m jobhound.delivery_trial --help.

Never invoked by the scheduled run. Initial state has no production notification
history: the user must approve the selected trial digest, not assume migration.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from .delivery_outbox import DeliveryOutbox, identifier
from .delivery_transport import DigestTransport, dispatch_envelope

MARKER = 'delivery-trial.json'
DB = 'delivery-trial.sqlite'


def notifier(channel):
    if channel == 'email':
        from .notify.email import EmailNotifier
        return EmailNotifier()
    if channel == 'telegram':
        from .notify.telegram import TelegramNotifier
        return TelegramNotifier()
    raise ValueError('unsupported channel')


def initialize(root, *, workspace, profile):
    from .run_context import deny_review_side_effect
    deny_review_side_effect('delivery trial initialization')
    root=Path(root).resolve()
    identifier(workspace); identifier(profile)
    if root.exists() or any(p.name.lower() in {'data','state','.git'} for p in (root,*root.parents)):
        raise ValueError('a NEW isolated directory outside production data/state is required')
    root.mkdir(parents=True,exist_ok=False)
    from .v41.store import V41Store
    db=V41Store(root/DB)
    try:
        box=db.enable_delivery_review(review_root=root,workspace_id=workspace,profile_id=profile)
        DigestTransport(box)
        (root/MARKER).write_text(json.dumps({'version':1,'root':str(root),
            'workspace':workspace,'profile':profile,'mode':'explicit_trial_no_production_history'},indent=2),encoding='utf-8')
    finally:
        db.close()


def open_trial(root, *, readonly=False):
    """No implicit migrations or initialization on inspect/send/recovery."""
    root=Path(root).resolve()
    marker=json.loads((root/MARKER).read_text(encoding='utf-8'))
    if (marker.get('version')!=1 or marker.get('root')!=str(root)
            or marker.get('mode')!='explicit_trial_no_production_history'):
        raise ValueError('not an initialized trial namespace, or namespace was moved/copied')
    path=(root/DB).resolve()
    if path.parent!=root or not path.is_file():
        raise ValueError('trial database missing or outside namespace')
    if not readonly:
        from .run_context import deny_review_side_effect
        deny_review_side_effect('delivery persistence')
    conn=sqlite3.connect(path.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        if readonly: conn.execute('PRAGMA query_only=ON')
        versions=dict(conn.execute('SELECT key,value FROM delivery_meta'))
        if versions.get('version') not in {'1','2'} or versions.get('transport_version')!='1':
            raise ValueError('unsupported trial schema')
        # Read/open existing schema only. Constructor migrations are initialization-only.
        box=DeliveryOutbox.__new__(DeliveryOutbox)
        box.conn,box.workspace,box.profile=conn,identifier(marker['workspace']),identifier(marker['profile'])
        transport=DigestTransport.__new__(DigestTransport)
        transport.box,transport.conn=box,conn
        return transport
    except BaseException:
        conn.close()
        raise


def stage_snapshot(root, snapshot, channel, *, now=None):
    """Offline frozen replay, then an explicit persistence boundary into trial DB."""
    from .config import CONFIG
    from .run_context import RunContext, run_scope, deny_review_side_effect
    from .v41.replay import replay, load_snapshot
    from .v41.store import V41Store
    deny_review_side_effect('delivery staging')
    root=Path(root).resolve()
    sender=notifier(channel)
    target=sender.destination
    header,_=load_snapshot(snapshot)
    config=CONFIG.model_copy(deep=True)
    config.v55.enabled=True
    for name,value in (header.get('release_policy') or {}).items():
        if name in type(config.v55).model_fields:
            setattr(config.v55,name,value)
    context=RunContext.capture(config=config,workspace=root,as_of=datetime.fromisoformat(header['as_of']))
    with run_scope(context):
        result=replay(snapshot)
    transport=open_trial(root)
    try:
        # record_delivery_run consumes only conn/delivery and the existing store
        # methods. Do not call V41Store's constructor on an operational command.
        db=V41Store.__new__(V41Store)
        db.conn,db.delivery=transport.conn,transport.box
        return db.record_delivery_run(result,[target],now=time.time() if now is None else now)
    finally:
        transport.conn.close()


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='action',required=True)
    init=sub.add_parser('init',help='create a NEW empty trial namespace; no production migration')
    init.add_argument('--root',required=True); init.add_argument('--workspace',required=True); init.add_argument('--profile',required=True)
    stage=sub.add_parser('stage',help='offline snapshot replay into the trial queue; never sends')
    stage.add_argument('--root',required=True); stage.add_argument('--snapshot',required=True)
    stage.add_argument('--channel',choices=['email','telegram'],required=True)
    listing=sub.add_parser('list',help='read-only queue metadata; no message text or secrets')
    listing.add_argument('--root',required=True)
    prepare=sub.add_parser('prepare',help='freeze exact intent IDs as one digest')
    prepare.add_argument('--root',required=True); prepare.add_argument('--channel',choices=['email','telegram'],required=True)
    prepare.add_argument('--intents',type=int,nargs='+',required=True)
    prepare.add_argument('--summary-run',help='bind coverage overflow accounting from this staged run')
    migrate=sub.add_parser('migrate-reissue',help='explicitly add reviewed unsent-intent generations; never automatic')
    migrate.add_argument('--root',required=True); migrate.add_argument('--reviewed',action='store_true')
    reissue=sub.add_parser('reissue',help='recover exact current intents from provably-unsent drafts; never sends')
    reissue.add_argument('--root',required=True); reissue.add_argument('--reviewed',action='store_true')
    reissue.add_argument('--envelopes',nargs='+',required=True)
    reissue.add_argument('--intents',type=int,nargs='+',required=True)
    reissue.add_argument('--evidence',required=True)
    for action in ('show','send','reconcile','abandon'):
        cmd=sub.add_parser(action)
        cmd.add_argument('--root',required=True); cmd.add_argument('--envelope',required=True)
        if action=='send':
            cmd.add_argument('--live',action='store_true',help='explicitly authorize this bounded provider call')
            cmd.add_argument('--confirm-target',required=True); cmd.add_argument('--confirm-body',required=True)
            cmd.add_argument('--max-parts',type=int,default=1)
        if action in {'reconcile','abandon'}:
            cmd.add_argument('--reviewed',action='store_true'); cmd.add_argument('--evidence',required=True)
        if action=='reconcile':
            cmd.add_argument('--part',type=int,required=True)
            cmd.add_argument('--outcome',choices=['accepted','not_sent'],required=True)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    from .cli import _setup_logging
    _setup_logging()
    if args.action=='send' and not args.live:
        raise SystemExit('No send: --live and exact preview hashes are required.')
    try:
        if args.action=='init':
            initialize(args.root,workspace=args.workspace,profile=args.profile)
            print('Initialized isolated delivery trial. No production history imported.')
            return
        if args.action=='stage':
            print(json.dumps(stage_snapshot(args.root,args.snapshot,args.channel),indent=2))
            return
        transport=open_trial(args.root,readonly=args.action in {'list','show'})
        try:
            if args.action=='list':
                intents=[{k:r[k] for k in ('id','subject','channel','destination','status')} for r in transport.box.inspect()]
                envelopes=[dict(r) for r in transport.conn.execute('''SELECT id,channel,destination,body_hash,status
                    FROM delivery_envelopes WHERE workspace=? AND profile=? ORDER BY created''',
                    (transport.box.workspace,transport.box.profile))]
                print(json.dumps({'intents':intents,'envelopes':envelopes},indent=2))
            elif args.action=='prepare':
                print(transport.prepare(notifier(args.channel).destination,args.intents,now=time.time(),summary_run_id=args.summary_run))
            elif args.action=='migrate-reissue':
                if args.reviewed is not True:
                    raise ValueError('explicit reviewed migration required')
                from .delivery_migration import migrate_unsent_generations
                print('Notification-generation migration applied.' if migrate_unsent_generations(transport.conn) else 'Already migrated.')
            elif args.action=='reissue':
                print(json.dumps(transport.reissue_unsent(args.envelopes,args.intents,
                    reviewed=args.reviewed,evidence=args.evidence,now=time.time())))
            elif args.action=='show':
                print(json.dumps(transport.inspect(args.envelope),ensure_ascii=False,indent=2))
            elif args.action=='send':
                channel=transport.inspect(args.envelope)['channel']
                status=asyncio.run(dispatch_envelope(transport,args.envelope,notifier(channel),
                    confirm_target=args.confirm_target,confirm_body=args.confirm_body,max_parts=args.max_parts))
                print('Provider acceptance state: '+status+' (not an inbox/read receipt).')
                if status!='accepted': raise SystemExit(2)
            elif args.action=='reconcile':
                transport.reconcile_part(args.envelope,args.part,args.outcome,reviewed=args.reviewed,evidence=args.evidence,now=time.time())
            elif args.action=='abandon':
                transport.abandon(args.envelope,reviewed=args.reviewed,evidence=args.evidence,now=time.time())
        finally:
            transport.conn.close()
    except (ValueError,OSError,sqlite3.Error,KeyError):
        raise SystemExit('Delivery trial rejected invalid inputs/state. Inspect the preview and receipt ledger; no raw provider details printed.') from None


if __name__=='__main__':
    main()
