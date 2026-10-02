"""Mock SMTP/HTTP only. Actual notifiers run behind a network-denying harness."""
import asyncio
import smtplib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from jobhound.delivery_outbox import DeliveryOutbox, Destination, transaction
from jobhound.delivery_transport import DigestTransport, dispatch_envelope
from jobhound.notify.receipt import DeliveryReceipt
from jobhound.notify.email import EmailNotifier
from jobhound.notify.telegram import TelegramNotifier, _chunk
from jobhound.v41.store import V41Store
from test_v6_delivery_recovery import store, release, stage, EMAIL, TELEGRAM, count
from test_v6_continuity import run


def prepared(store, target=EMAIL):
    stage(store,targets=(target,))
    transport = DigestTransport(store.delivery)
    envelope = transport.prepare(target,[r['id'] for r in store.delivery.inspect()],now=101)
    return transport,envelope


class Sender:
    def __init__(self, target=EMAIL, receipts=None):
        self.destination=target
        self.receipts=list(receipts or [DeliveryReceipt('accepted','test_accepted')]*32)
        self.calls=[]

    async def send_receipt(self, body, *, delivery_key):
        self.calls.append((body,delivery_key))
        result=self.receipts.pop(0)
        if isinstance(result,BaseException):
            raise result
        return result


def send(transport,envelope,sender=None,now=102,**kwargs):
    preview=transport.inspect(envelope)
    return asyncio.run(dispatch_envelope(transport,envelope,sender or Sender(),
        confirm_target=preview['destination'],confirm_body=preview['body_hash'],
        now=lambda:now,**kwargs))


def test_one_email_one_body_and_baseline_only_after_acceptance(store):
    transport,envelope=prepared(store)
    sender=Sender()
    assert 'Pay:' in transport.inspect(envelope)['body']
    assert count(store,'delivery_baselines')==0
    assert send(transport,envelope,sender)=='accepted'
    assert len(sender.calls)==1 and count(store,'delivery_baselines')==1
    assert send(transport,envelope,sender)=='accepted' and len(sender.calls)==1
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("DELETE FROM delivery_part_events")
    store.conn.rollback()


def test_email_many_cards_remains_one_message(store):
    from test_v6_continuity import raw, NOW
    from jobhound.v41.engine import evaluate_raw
    rows=[raw('Pay USD 20 per hour.',identity=str(i)) for i in range(3)]
    for i,row in enumerate(rows):
        row['raw']['jobUrl']=f'https://jobs.ashbyhq.com/company{i}/unique-job-{i}'
        row['raw']['_company']=f'Company{i}'
    result=evaluate_raw(rows,as_of=NOW)
    stage(store,result)
    transport=DigestTransport(store.delivery)
    ids=[r['id'] for r in store.delivery.inspect()]
    assert len(ids)==3
    envelope=transport.prepare(EMAIL,ids,now=101)
    sender=Sender()
    assert send(transport,envelope,sender)=='accepted'
    assert len(sender.calls)==1


@pytest.mark.parametrize('bad', [True,False,None,('accepted','fake'),RuntimeError('raw-secret')])
def test_non_receipt_or_exception_is_uncertain(store,bad):
    transport,envelope=prepared(store)
    sender=Sender(receipts=[bad])
    assert send(transport,envelope,sender)=='uncertain'
    assert send(transport,envelope,sender,now=10000)=='uncertain'
    assert len(sender.calls)==1 and count(store,'delivery_baselines')==0
    assert 'raw-secret' not in str([dict(r) for r in store.conn.execute('SELECT * FROM delivery_part_events')])


def test_destination_and_body_confirmation_prevent_sending(store):
    transport,envelope=prepared(store)
    sender=Sender(Destination.from_address('email','other@example.test'))
    with pytest.raises(ValueError,match='differs'):
        send(transport,envelope,sender)
    with pytest.raises(ValueError,match='hashes'):
        asyncio.run(dispatch_envelope(transport,envelope,Sender(),confirm_target=EMAIL.key,confirm_body='wrong'))
    assert not sender.calls and transport.inspect(envelope)['status']=='pending'


def test_queue_workers_cannot_steal_envelope_or_reconcile_one_card(store):
    transport,envelope=prepared(store)
    intent=transport.inspect(envelope)['intents'][0]
    assert store.delivery.claim_next(EMAIL,now=102) is None
    token=transport.claim(envelope,EMAIL,now=102)
    for func,args,kwargs in [
        (store.delivery.begin_send,(intent,token),{'now':103}),
        (store.delivery.finish,(intent,token,'accepted'),{'now':103,'evidence':'bad'}),
        (store.delivery.reconcile,(intent,'accepted'),{'now':103,'evidence':'bad','reviewed':True})]:
        with pytest.raises(ValueError,match='envelope owns'):
            func(*args,**kwargs)
    assert store.delivery.claim_next(EMAIL,now=10000) is None
    assert transport.inspect(envelope)['status']=='leased'


def test_claim_before_send_can_expire_and_retry(store):
    transport,envelope=prepared(store)
    transport.claim(envelope,EMAIL,now=102,lease_seconds=1)
    assert send(transport,envelope,now=104)=='accepted'


def test_lost_ack_blocks_and_explicit_reconciliation_advances(store):
    transport,envelope=prepared(store)
    token=transport.claim(envelope,EMAIL,now=102,lease_seconds=1)
    part=transport.begin_part(envelope,token,now=102)
    with pytest.raises(ValueError,match='stale receipt'):
        transport.finish_part(envelope,token,part['ordinal'],DeliveryReceipt('accepted','late'),now=104)
    assert transport.inspect(envelope)['status']=='uncertain'
    for reviewed in (False,1,'true'):
        with pytest.raises(ValueError):
            transport.reconcile_part(envelope,0,'accepted',reviewed=reviewed,evidence='provider_log',now=105)
    transport.reconcile_part(envelope,0,'accepted',reviewed=True,evidence='provider_log',now=105)
    assert transport.inspect(envelope)['status']=='accepted'
    assert count(store,'delivery_baselines')==1


def test_cancellation_does_not_turn_into_retry(store):
    transport,envelope=prepared(store)
    sender=Sender(receipts=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        send(transport,envelope,sender)
    assert send(transport,envelope,sender,now=10000)=='uncertain'
    assert len(sender.calls)==1


def test_backoff_retry_limit_and_permanent_failure(store):
    transport,envelope=prepared(store)
    sender=Sender(receipts=[DeliveryReceipt('not_sent','busy',300)]*3)
    assert send(transport,envelope,sender)=='pending'
    assert send(transport,envelope,sender,now=103)=='pending' and len(sender.calls)==1
    assert send(transport,envelope,sender,now=403)=='pending'
    assert send(transport,envelope,sender,now=800)=='failed'
    assert send(transport,envelope,sender,now=2000)=='failed' and len(sender.calls)==3


def test_ready_envelopes_retries_only_safe_pending_work(store):
    transport,envelope=prepared(store)
    assert transport.ready_envelopes(EMAIL,now=102)==[envelope]
    sender=Sender(receipts=[DeliveryReceipt('not_sent','busy',300)])
    assert send(transport,envelope,sender,now=102)=='pending'
    assert transport.ready_envelopes(EMAIL,now=401)==[]
    assert transport.ready_envelopes(EMAIL,now=402)==[envelope]


def test_ready_envelopes_never_returns_uncertain_work(store):
    transport,envelope=prepared(store)
    assert send(transport,envelope,Sender(receipts=[DeliveryReceipt('uncertain','lost')]))=='uncertain'
    assert transport.ready_envelopes(EMAIL,now=10000)==[]


def test_reopen_and_two_workers_one_claim(store):
    transport,envelope=prepared(store)
    def worker(_):
        other=V41Store(store.path)
        box=other.enable_delivery_review(review_root=store.path.parent,workspace_id='fixture',profile_id='person')
        transport2=DigestTransport(box)
        token=transport2.claim(envelope,EMAIL,now=102)
        other.close()
        return token
    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens=list(pool.map(worker,range(2)))
    assert sum(t is not None for t in tokens)==1


def test_atomic_prepare_on_renderer_failure(store,monkeypatch):
    stage(store)
    transport=DigestTransport(store.delivery)
    monkeypatch.setattr('jobhound.delivery_transport.render_envelope',lambda _, **kw: (_ for _ in ()).throw(ValueError('bad')))
    with pytest.raises(ValueError):
        transport.prepare(EMAIL,[store.delivery.inspect()[0]['id']],now=101)
    assert count(store,'delivery_envelopes')==count(store,'delivery_envelope_items')==0


def test_owner_and_future_schema_fail_closed(store):
    transport,envelope=prepared(store)
    other=DigestTransport(DeliveryOutbox(store.conn,'other','person'))
    with pytest.raises(ValueError,match='outside owner'):
        other.inspect(envelope)
    with store.conn:
        store.conn.execute("UPDATE delivery_meta SET value='999' WHERE key='transport_version'")
    with pytest.raises(ValueError,match='schema'):
        DigestTransport(store.delivery)


def test_new_revision_cancels_unsent_envelope_and_cannot_bypass_reservation(store):
    transport,envelope=prepared(store)
    stage(store,run('Pay USD 35 per hour.',minute=1))
    newer=store.delivery.inspect()[-1]['id']
    assert store.delivery.claim_next(EMAIL,now=102) is None
    with pytest.raises(ValueError):
        transport.prepare(EMAIL,[newer],now=102)
    sender=Sender()
    assert send(transport,envelope,sender)=='cancelled' and not sender.calls
    transport.abandon(envelope,reviewed=True,evidence='review_obsolete',now=103)
    fresh=transport.prepare(EMAIL,[newer],now=104)
    assert send(transport,fresh,now=105)=='accepted'


def telegram_prepared(store,monkeypatch):
    import jobhound.delivery_transport as module
    renderer=module.render_envelope
    monkeypatch.setattr(module,'render_envelope',lambda rows, **kw:renderer(rows, **kw)+'\n'+'x'*9000)
    return prepared(store,TELEGRAM)


def test_telegram_partial_success_does_not_resend_accepted_parts(store,monkeypatch):
    transport,envelope=telegram_prepared(store,monkeypatch)
    count_parts=len(transport.inspect(envelope)['parts'])
    sender=Sender(TELEGRAM,[DeliveryReceipt('accepted','part1'),DeliveryReceipt('not_sent','rate_limit',500)]
        +[DeliveryReceipt('accepted','other')]*32)
    assert send(transport,envelope,sender,max_parts=32)=='pending'
    assert len(sender.calls)==2 and count(store,'delivery_baselines')==0
    assert send(transport,envelope,sender,now=700,max_parts=32)=='accepted'
    assert len(sender.calls)==count_parts+1
    assert sum(key.endswith(':0') for _,key in sender.calls)==1
    assert count(store,'delivery_baselines')==1


def test_partial_stale_digest_holds_for_explicit_abandonment(store,monkeypatch):
    transport,envelope=telegram_prepared(store,monkeypatch)
    sender=Sender(TELEGRAM)
    assert send(transport,envelope,sender)=='pending'
    stage(store,run('Pay USD 35 per hour.',minute=1),targets=(TELEGRAM,))
    assert send(transport,envelope,sender)=='uncertain' and len(sender.calls)==1
    assert count(store,'delivery_baselines')==0
    transport.abandon(envelope,reviewed=True,evidence='review_partial_obsolete',now=103)
    assert transport.inspect(envelope)['status']=='abandoned'
    assert transport.inspect(envelope)['parts'][0]['status']=='accepted'


@pytest.mark.parametrize('text',['x'*15000,'😀'*7000,'a\n'*5000,''],ids=['giant_line','emoji','newlines','empty'])
def test_telegram_chunk_boundaries(text):
    chunks=_chunk(text)
    assert ''.join(chunks)==text
    assert all(len(p.encode('utf-16-le'))//2<=4000 for p in chunks)


@pytest.fixture
def smtp(monkeypatch):
    server=SimpleNamespace(login=lambda *_:None,send_message=lambda *a,**kw:{},close=lambda:None)
    monkeypatch.setattr('jobhound.notify.email.smtplib.SMTP_SSL',lambda *a,**kw:server)
    return server


def email():
    return EmailNotifier(user='sender@example.test',password='fake-app-password',to='receiver@example.test')


def raise_error(error):
    def fail(*args,**kwargs):
        raise error
    return fail


@pytest.mark.parametrize('stage_name,error,outcome',[
    ('login',smtplib.SMTPAuthenticationError(535,b'secret'),'permanent_failure'),
    ('login',OSError('secret'),'not_sent'),
    ('send_message',OSError('secret'),'uncertain'),
    ('send_message',smtplib.SMTPDataError(451,b'secret'),'not_sent'),
    ('send_message',smtplib.SMTPDataError(550,b'secret'),'permanent_failure'),
    ('send_message',smtplib.SMTPRecipientsRefused({'receiver@example.test':(450,b'secret')}),'not_sent'),
    ('send_message',smtplib.SMTPRecipientsRefused({'receiver@example.test':(550,b'secret')}),'permanent_failure'),
    ('close',OSError('secret'),'accepted')])
def test_smtp_typed_receipts(smtp,stage_name,error,outcome):
    setattr(smtp,stage_name,raise_error(error))
    receipt=asyncio.run(email().send_receipt('Test digest',delivery_key='envelope:0'))
    assert receipt.outcome==outcome and 'secret' not in receipt.evidence


def test_smtp_stable_message_id_exact_recipient_and_queue_integration(store,smtp):
    calls=[]
    smtp.send_message=lambda msg,**kw:calls.append((msg,kw)) or {}
    notifier=email()
    transport,envelope=prepared(store,notifier.destination)
    assert send(transport,envelope,notifier)=='accepted'
    msg,kw=calls[0]
    assert kw=={'from_addr':'sender@example.test','to_addrs':['receiver@example.test']}
    assert msg['Message-ID'].startswith('<jobhound.')
    again=asyncio.run(notifier.send_receipt('Same',delivery_key=envelope+':0'))
    assert calls[1][0]['Message-ID']==msg['Message-ID']


@pytest.mark.parametrize('address',['a@example.test,b@example.test','Name <a@example.test>','bad\nBcc:evil@example.test','no-host'])
def test_smtp_rejects_ambiguous_destination_before_network(address):
    notifier=email(); notifier.to=address
    with pytest.raises(ValueError):
        notifier.destination


def fake_telegram(monkeypatch,status,body=None,error=None):
    calls=[]
    class Client:
        def __init__(self,**kw):
            assert kw['follow_redirects'] is False
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        async def post(self,url,**kw):
            calls.append(kw)
            if error: raise error
            return httpx.Response(status,json=body)
    monkeypatch.setattr('jobhound.notify.telegram.httpx.AsyncClient',Client)
    return calls


@pytest.mark.parametrize('status,body,outcome',[
    (200,{'ok':True,'result':{'message_id':42}},'accepted'),
    (200,{'ok':True},'uncertain'),(200,{'ok':True,'result':{'message_id':True}},'uncertain'),
    (200,{},'uncertain'),(302,{},'uncertain'),(503,{},'uncertain'),
    (429,{'ok':False,'error_code':429,'parameters':{'retry_after':25}},'not_sent'),
    (403,{'ok':False,'error_code':403},'permanent_failure')])
def test_telegram_typed_receipts(monkeypatch,status,body,outcome):
    fake_telegram(monkeypatch,status,body)
    receipt=asyncio.run(TelegramNotifier(token='123:fake-token',chat_id='456').send_receipt('Hi',delivery_key='test'))
    assert receipt.outcome==outcome


@pytest.mark.parametrize('error,outcome',[
    (httpx.ConnectTimeout('secret'),'not_sent'),(httpx.ReadTimeout('secret'),'uncertain'),
    (httpx.WriteError('secret'),'uncertain')])
def test_telegram_transport_failures(monkeypatch,error,outcome):
    fake_telegram(monkeypatch,200,error=error)
    receipt=asyncio.run(TelegramNotifier(token='123:fake-token',chat_id='456').send_receipt('Hi',delivery_key='test'))
    assert receipt.outcome==outcome and 'secret' not in receipt.evidence


def test_telegram_token_rotation_preserves_target():
    assert TelegramNotifier('123:old','456').destination==TelegramNotifier('123:new','456').destination
    assert TelegramNotifier('124:new','456').destination!=TelegramNotifier('123:new','456').destination
