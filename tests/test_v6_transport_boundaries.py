"""Challenge the public trial workflow, crash boundaries, and immutable previews."""
import asyncio
import hashlib
import json
import shutil
import sqlite3

import pytest

from jobhound.delivery_transport import DigestTransport, dispatch_envelope
from jobhound.delivery_trial import initialize, open_trial, stage_snapshot, main, DB
from jobhound.notify.receipt import DeliveryReceipt
from jobhound.notify.email import EmailNotifier
from jobhound.run_context import RunContext, run_scope
from jobhound.config import CONFIG
from jobhound.v41.replay import write_snapshot
from test_v6_delivery_recovery import store, release, stage, EMAIL, TELEGRAM, count
from test_v6_continuity import run, raw
from test_v6_transport import prepared, send, Sender, fake_telegram


def fixture_snapshot(tmp_path):
    directory=tmp_path/'snapshots'; directory.mkdir()
    return write_snapshot([raw('Pay USD 20 per hour.')],run('Pay USD 20 per hour.'),directory=directory)


def test_public_init_stage_preview_send_end_to_end_offline(tmp_path,monkeypatch,capsys):
    root=tmp_path/'trial'
    initialize(root,workspace='example',profile='person')
    sender=Sender()
    monkeypatch.setattr('jobhound.delivery_trial.notifier',lambda _:sender)
    snapshot=fixture_snapshot(tmp_path)
    first=stage_snapshot(root,snapshot,'email',now=100)
    second=stage_snapshot(root,snapshot,'email',now=101)
    assert first['destinations'][0]['counts']['selected_cards']==1
    assert second['destinations'][0]['counts']['already_recorded']==1
    main(['list','--root',str(root)])
    listing=json.loads(capsys.readouterr().out)
    assert len(listing['intents'])==1 and not sender.calls
    intent=listing['intents'][0]['id']
    main(['prepare','--root',str(root),'--channel','email','--intents',str(intent)])
    envelope=capsys.readouterr().out.strip()
    before=hashlib.sha256((root/DB).read_bytes()).hexdigest()
    main(['show','--root',str(root),'--envelope',envelope])
    preview=json.loads(capsys.readouterr().out)
    assert hashlib.sha256((root/DB).read_bytes()).hexdigest()==before
    args=['send','--root',str(root),'--envelope',envelope,
          '--confirm-target',preview['destination'],'--confirm-body',preview['body_hash']]
    with pytest.raises(SystemExit,match='No send'):
        main(args)
    assert not sender.calls
    main([*args,'--live'])
    assert len(sender.calls)==1 and 'accepted' in capsys.readouterr().out
    main([*args,'--live'])
    assert len(sender.calls)==1


def test_init_never_opens_existing_dir_or_production_named_namespace(tmp_path):
    existing=tmp_path/'existing'; existing.mkdir()
    for root in (existing,tmp_path/'data'/'trial',tmp_path/'state'/'trial'):
        with pytest.raises(ValueError):
            initialize(root,workspace='x',profile='y')
    assert not list(existing.iterdir())


def test_copy_or_moved_trial_fails_closed(tmp_path):
    root=tmp_path/'one'; initialize(root,workspace='x',profile='y')
    other=tmp_path/'two'; shutil.copytree(root,other)
    with pytest.raises(ValueError,match='moved/copied'):
        open_trial(other)


def test_read_only_view_cannot_initialize_or_write(tmp_path):
    root=tmp_path/'one'; initialize(root,workspace='x',profile='y')
    transport=open_trial(root,readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            transport.conn.execute("UPDATE delivery_meta SET value='2'")
    finally: transport.conn.close()


def test_review_context_rejects_initialization_staging_dispatch_and_open(tmp_path,store):
    root=tmp_path/'trial'; initialize(root,workspace='x',profile='y')
    transport,envelope=prepared(store)
    with run_scope(RunContext.capture(workspace=tmp_path)):
        for call in [lambda:initialize(tmp_path/'other',workspace='x',profile='y'),
                     lambda:open_trial(root),
                     lambda:stage_snapshot(root,'absent','email'),
                     lambda:send(transport,envelope),
                     lambda:asyncio.run(EmailNotifier('sender@example.test','fake','to@example.test').send_receipt('hi',delivery_key='test'))]:
            with pytest.raises(RuntimeError,match='review context'):
                call()


@pytest.mark.parametrize('sql',[
    "UPDATE delivery_envelopes SET body='changed'",
    "UPDATE delivery_envelopes SET destination='changed'",
    "UPDATE delivery_envelopes SET body_hash='changed'",
    "UPDATE delivery_parts SET body='changed'",
    "UPDATE delivery_envelope_items SET envelope='changed'",
    "DELETE FROM delivery_envelope_items"])
def test_frozen_preview_and_membership_cannot_mutate(store,sql):
    transport,envelope=prepared(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(sql)
    store.conn.rollback()
    assert send(transport,envelope)=='accepted'


def test_stale_preview_hash_rejected_even_if_db_trigger_removed(store):
    transport,envelope=prepared(store)
    with store.conn:
        store.conn.execute('DROP TRIGGER delivery_part_frozen')
        store.conn.execute("UPDATE delivery_parts SET body='changed'")
    sender=Sender()
    with pytest.raises(ValueError,match='integrity'):
        send(transport,envelope,sender)
    assert not sender.calls


def test_acceptance_commit_failure_does_not_create_retry(store,monkeypatch):
    transport,envelope=prepared(store)
    original=transport.box._accept
    def crash(row):
        original(row)
        raise RuntimeError('crash_on_commit')
    monkeypatch.setattr(transport.box,'_accept',crash)
    sender=Sender()
    with pytest.raises(RuntimeError,match='crash_on_commit'):
        send(transport,envelope,sender)
    assert count(store,'delivery_baselines')==0
    assert transport.inspect(envelope)['parts'][0]['status']=='sending'
    monkeypatch.setattr(transport.box,'_accept',original)
    assert send(transport,envelope,sender,now=10000)=='uncertain'
    assert len(sender.calls)==1


def test_not_sent_reconciliation_retries_only_after_backoff(store):
    transport,envelope=prepared(store)
    sender=Sender(receipts=[DeliveryReceipt('uncertain','lost_ack'),DeliveryReceipt('accepted','next_ack')])
    assert send(transport,envelope,sender)=='uncertain'
    transport.reconcile_part(envelope,0,'not_sent',reviewed=True,evidence='checked_provider',now=103)
    assert send(transport,envelope,sender,now=104)=='pending' and len(sender.calls)==1
    assert send(transport,envelope,sender,now=300)=='accepted'


@pytest.mark.parametrize('ids',[[],[1,1],[True],[-1],list(range(1,102))])
def test_exact_bounded_intent_selection(store,ids):
    stage(store)
    transport=DigestTransport(store.delivery)
    with pytest.raises(ValueError):
        transport.prepare(EMAIL,ids,now=101)
    assert count(store,'delivery_envelopes')==0


def test_account_or_recipient_change_cannot_reroute_pending_email():
    base=EmailNotifier('a@example.test','fake','b@example.test')
    assert base.destination==EmailNotifier('a@example.test','rotated','b@example.test').destination
    assert base.destination!=EmailNotifier('other@example.test','fake','b@example.test').destination
    assert base.destination!=EmailNotifier('a@example.test','fake','other@example.test').destination


@pytest.mark.parametrize('status,body',[
    (503,{'ok':False,'error_code':429}),
    (200,{'ok':False,'error_code':403}),
    (429,{'ok':False,'error_code':429.0}),
    (403,{'ok':False,'error_code':400})])
def test_telegram_inconsistent_error_body_is_uncertain(monkeypatch,status,body):
    from jobhound.notify.telegram import TelegramNotifier
    fake_telegram(monkeypatch,status,body)
    receipt=asyncio.run(TelegramNotifier('123:fake','456').send_receipt('hi',delivery_key='test'))
    assert receipt.outcome=='uncertain'


def test_permanent_failure_does_not_drain_or_advance_baseline(store):
    transport,envelope=prepared(store)
    sender=Sender(receipts=[DeliveryReceipt('permanent_failure','auth_rejected')])
    assert send(transport,envelope,sender)=='failed'
    assert send(transport,envelope,sender,now=100000)=='failed' and len(sender.calls)==1
    assert count(store,'delivery_baselines')==0


def test_telegram_actual_notifier_queue_integration(store,monkeypatch):
    from jobhound.notify.telegram import TelegramNotifier
    calls=fake_telegram(monkeypatch,200,{'ok':True,'result':{'message_id':1234}})
    sender=TelegramNotifier('123:fake','456')
    transport,envelope=prepared(store,sender.destination)
    assert send(transport,envelope,sender,max_parts=32)=='accepted'
    assert len(calls)==len(transport.inspect(envelope)['parts'])
    assert all(call['json']['chat_id']=='456' for call in calls)


def test_email_and_telegram_acceptance_remain_independent(store):
    stage(store,targets=(EMAIL,TELEGRAM))
    transport=DigestTransport(store.delivery)
    envelopes={}
    for target in (EMAIL,TELEGRAM):
        ids=[r['id'] for r in store.delivery.inspect() if r['channel']==target.channel]
        envelopes[target.channel]=transport.prepare(target,ids,now=101)
    assert send(transport,envelopes['email'],Sender(receipts=[DeliveryReceipt('uncertain','lost')]))=='uncertain'
    assert send(transport,envelopes['telegram'],Sender(TELEGRAM),max_parts=32)=='accepted'
    baselines=store.conn.execute('SELECT channel FROM delivery_baselines').fetchall()
    assert [r[0] for r in baselines]==['telegram']
