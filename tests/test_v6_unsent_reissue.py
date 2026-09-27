import sqlite3
import pytest
from jobhound.delivery_outbox import DeliveryOutbox
from jobhound.delivery_transport import DigestTransport
from jobhound.delivery_migration import migrate_unsent_generations
from jobhound.notify.receipt import DeliveryReceipt
from jobhound.v41.store import V41Store
from test_v6_delivery_recovery import store, stage, release, EMAIL, TELEGRAM, count
from test_v6_transport import prepared, Sender, send, telegram_prepared
from test_v6_continuity import run


def reissue(transport,envelope,ids=None,**kwargs):
    return transport.reissue_unsent([envelope],ids or transport.inspect(envelope)['intents'],
        reviewed=True,evidence='reviewed_fixture',now=104,**kwargs)


@pytest.mark.parametrize('abandoned',[False,True])
def test_unsent_reissue_preserves_revision_payload_history_and_sends_once(store,abandoned):
    transport,old=prepared(store)
    original=transport.inspect(old)
    old_id=original['intents'][0]
    old_intent=store.delivery._owned(old_id)
    if abandoned:
        transport.abandon(old,reviewed=True,evidence='held_fixture',now=102)
    ids=reissue(transport,old)
    new=store.delivery._owned(ids[0])
    assert new['revision']==old_intent['revision'] and new['payload']==old_intent['payload']
    assert new['generation']==1 and new['replaces_intent']==old_id
    assert store.delivery._owned(old_id)['status']=='superseded'
    assert count(store,'delivery_revisions')==1
    assert count(store,'delivery_baselines')==0
    assert transport.inspect(old)['body']==original['body']
    assert transport.inspect(old)['intents']==original['intents']
    fresh=transport.prepare(EMAIL,ids,now=105)
    sender=Sender()
    assert send(transport,old,sender,now=106)=='abandoned'
    assert not sender.calls
    assert send(transport,fresh,sender,now=107)=='accepted'
    assert send(transport,fresh,sender,now=108)=='accepted'
    assert len(sender.calls)==1
    assert count(store,'delivery_baselines')==1
    assert store.delivery._owned(old_id)['status']=='superseded'
    with pytest.raises(ValueError): reissue(transport,old)
    assert count(store,'delivery_intents')==2


@pytest.mark.parametrize('outcome',['accepted','not_sent','uncertain','permanent_failure'])
def test_any_provider_attempt_forbids_recomposition(store,outcome):
    transport,envelope=prepared(store)
    send(transport,envelope,Sender(receipts=[DeliveryReceipt(outcome,'fixture')]))
    with pytest.raises(ValueError): reissue(transport,envelope)
    assert count(store,'delivery_intents')==1


def test_active_lease_cannot_be_reissued(store):
    transport,envelope=prepared(store)
    assert transport.claim(envelope,EMAIL,now=102)
    with pytest.raises(ValueError): reissue(transport,envelope)


def test_existing_acceptance_baseline_blocks_unsent_generation(store):
    transport,envelope=prepared(store)
    intent=store.delivery._owned(transport.inspect(envelope)['intents'][0])
    store.conn.execute('INSERT INTO delivery_baselines VALUES(?,?,?,?,?,?)',
        (store.delivery.workspace,store.delivery.profile,intent['subject'],
         intent['channel'],intent['destination'],intent['revision']))
    store.conn.commit()
    with pytest.raises(ValueError): reissue(transport,envelope)
    assert transport.inspect(envelope)['status']=='pending'
    assert count(store,'delivery_intents')==1


def test_multiple_reviewed_unsent_generations_keep_complete_lineage(store):
    transport,first=prepared(store)
    first_id=transport.inspect(first)['intents'][0]
    second_id=reissue(transport,first)[0]
    second=transport.prepare(EMAIL,[second_id],now=105)
    third_id=transport.reissue_unsent([second],[second_id],reviewed=True,
                                    evidence='second_review',now=106)[0]
    third=store.delivery._owned(third_id)
    assert third['generation']==2 and third['replaces_intent']==second_id
    assert store.delivery._owned(second_id)['replaces_intent']==first_id
    assert count(store,'delivery_revisions')==1
    sender=Sender()
    for old in (first,second):
        assert send(transport,old,sender,now=107)=='abandoned'
    assert not sender.calls
    final=transport.prepare(EMAIL,[third_id],now=108)
    assert send(transport,final,sender,now=109)=='accepted'
    assert len(sender.calls)==1 and count(store,'delivery_baselines')==1


def test_concurrent_claim_and_reissue_cannot_both_win(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    transport,envelope=prepared(store)
    ids=transport.inspect(envelope)['intents']
    db_path=store.conn.execute('PRAGMA database_list').fetchone()[2]
    barrier=Barrier(2)
    def worker(action):
        conn=sqlite3.connect(db_path,timeout=10)
        conn.row_factory=sqlite3.Row
        try:
            box=DeliveryOutbox(conn,store.delivery.workspace,store.delivery.profile)
            tx=DigestTransport(box)
            barrier.wait(timeout=10)
            try:
                if action=='claim':
                    return action,bool(tx.claim(envelope,EMAIL,now=104))
                return action,bool(tx.reissue_unsent([envelope],ids,reviewed=True,
                                                   evidence='concurrent_review',now=104))
            except ValueError:
                return action,False
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=dict(pool.map(worker,['claim','reissue']))
    assert sum(results.values())==1
    assert transport.inspect(envelope)['status']==('leased' if results['claim'] else 'abandoned')
    assert count(store,'delivery_intents')==(1 if results['claim'] else 2)
    assert count(store,'delivery_baselines')==0


def test_stale_revision_cannot_be_reissued(store):
    transport,envelope=prepared(store)
    stage(store,run('Pay USD 35 per hour.',minute=1))
    with pytest.raises(ValueError): reissue(transport,envelope)
    assert transport.inspect(envelope)['status']=='pending'


def test_mixed_batch_refuses_all_if_one_draft_was_attempted(store):
    transport,email=prepared(store,EMAIL)
    stage(store,run('Pay USD 20 per hour.',minute=1),targets=(TELEGRAM,))
    ids=[r['id'] for r in store.delivery.inspect() if r['channel']=='telegram']
    telegram=transport.prepare(TELEGRAM,ids,now=101)
    send(transport,email)
    with pytest.raises(ValueError):
        transport.reissue_unsent([telegram,email],ids,reviewed=True,evidence='fixture',now=104)
    assert transport.inspect(telegram)['status']=='pending'
    assert count(store,'delivery_intents')==2


@pytest.mark.parametrize('approved',[False,1,'yes',None])
def test_reissue_needs_literal_review_approval(store,approved):
    transport,envelope=prepared(store)
    with pytest.raises(ValueError):
        transport.reissue_unsent([envelope],transport.inspect(envelope)['intents'],
                                reviewed=approved,evidence='fixture',now=104)
    assert transport.inspect(envelope)['status']=='pending'


def test_other_owner_cannot_reissue_a_draft(store):
    transport,envelope=prepared(store)
    other=DigestTransport(DeliveryOutbox(store.conn,'other-workspace','person'))
    with pytest.raises(ValueError): reissue(other,envelope,[1])


def test_reissue_rolls_back_everything_if_insert_fails(store):
    transport,envelope=prepared(store)
    store.conn.execute("CREATE TRIGGER fail_generation BEFORE INSERT ON delivery_intents WHEN NEW.generation>0 BEGIN SELECT RAISE(ABORT,'test'); END;")
    with pytest.raises(sqlite3.IntegrityError): reissue(transport,envelope)
    assert transport.inspect(envelope)['status']=='pending'
    assert store.delivery.inspect()[0]['status']=='pending'
    assert count(store,'delivery_intents')==1
    assert count(store,'delivery_part_events')==0


def legacy_store(tmp_path,monkeypatch):
    import jobhound.delivery_outbox as module
    legacy=module._SCHEMA.replace(' generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),\n','').replace(' replaces_intent INTEGER REFERENCES delivery_intents(id),\n','').replace('UNIQUE(revision,channel,destination,generation)','UNIQUE(revision,channel,destination)')
    with monkeypatch.context() as patch:
        patch.setattr(module,'_SCHEMA',legacy)
        patch.setattr(module,'SCHEMA_VERSION','1')
        db=V41Store(tmp_path/'legacy.sqlite')
        db.enable_delivery_review(review_root=tmp_path,workspace_id='fixture',profile_id='person')
        DigestTransport(db.delivery)
    return db


def test_explicit_v1_migration_preserves_all_references_and_evidence(tmp_path,monkeypatch):
    db=legacy_store(tmp_path,monkeypatch)
    try:
        transport,envelope=prepared(db)
        old_intents=[dict(r) for r in db.conn.execute('SELECT * FROM delivery_intents')]
        original=transport.inspect(envelope)
        with pytest.raises(ValueError,match='migration'): reissue(transport,envelope)
        assert migrate_unsent_generations(db.conn)
        assert not migrate_unsent_generations(db.conn)
        assert not db.conn.execute('PRAGMA foreign_key_check').fetchall()
        assert db.conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        new=[dict(r) for r in db.conn.execute('SELECT * FROM delivery_intents')]
        for before,after in zip(old_intents,new):
            assert before=={k:after[k] for k in before}
            assert after['generation']==0 and after['replaces_intent'] is None
        assert original==transport.inspect(envelope)
        ids=reissue(transport,envelope)
        assert transport.prepare(EMAIL,ids,now=105)
        assert not db.conn.execute('PRAGMA foreign_key_check').fetchall()
        with pytest.raises(sqlite3.IntegrityError): db.conn.execute('DELETE FROM delivery_part_events')
    finally: db.close()


def test_failed_migration_restores_v1_and_pragmas(tmp_path,monkeypatch):
    db=legacy_store(tmp_path,monkeypatch)
    transport,envelope=prepared(db)
    db.close()
    class FailCopy(sqlite3.Connection):
        def execute(self,sql,*args):
            if sql.startswith('INSERT INTO delivery_intents('):
                raise sqlite3.OperationalError('injected migration failure')
            return super().execute(sql,*args)
    conn=sqlite3.connect(tmp_path/'legacy.sqlite',factory=FailCopy)
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with pytest.raises(sqlite3.OperationalError): migrate_unsent_generations(conn)
        assert conn.execute("SELECT value FROM delivery_meta WHERE key='version'").fetchone()[0]=='1'
        assert 'generation' not in [r[1] for r in conn.execute('PRAGMA table_info(delivery_intents)')]
        assert conn.execute('SELECT count(*) FROM delivery_intents').fetchone()[0]==1
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert conn.execute('PRAGMA legacy_alter_table').fetchone()[0]==0
    finally: conn.close()
