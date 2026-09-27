import json
from jobhound.v41.digest import build_digest, compact_coverage
from jobhound.delivery_transport import DigestTransport
from test_v6_delivery_recovery import store, stage, EMAIL, release
from test_v6_continuity import run


def result():
    value=run()
    value.source_health=[{'source':f'source-{i}', 'status':'failed', 'error_type':'ReadError'}
                         for i in range(12)]
    return value


def test_preview_summary_preserves_full_operational_evidence():
    value=result()
    digest=build_digest(value,include_all=True)
    assert '+ 7 additional source-status updates' in digest.text
    assert len(digest.operational_alerts)==12
    assert len(value.source_health)==12
    assert 'not evidence of job closure' in digest.text
    assert digest.accounting_ok


def test_frozen_email_summary_does_not_drop_queue_evidence(store):
    stage(store,result(),card_cap=0,status_cap=20)
    transport=DigestTransport(store.delivery)
    intents=store.delivery.inspect()
    assert len(intents)==12
    envelope=transport.prepare(EMAIL,[r['id'] for r in intents],now=101)
    body=transport.inspect(envelope)['body']
    assert '+ 7 additional source-status updates' in body
    assert body.count('Not a claim that jobs closed.')==5
    assert {json.loads(row['payload'])['health']['source'] for row in intents}=={
        f'source-{i}' for i in range(12)}


def test_duplicate_summary_lines_do_not_inflate_overflow():
    assert compact_coverage(['same']*20)==['same']


def test_stage_cap_overflow_is_frozen_without_claiming_delivery(store):
    value=result()
    stage(store,value,card_cap=0,status_cap=5)
    transport=DigestTransport(store.delivery)
    ids=[r['id'] for r in store.delivery.inspect()]
    envelope=transport.prepare(EMAIL,ids,now=101,summary_run_id=value.metadata.run_id)
    body=transport.inspect(envelope)['body']
    assert '12 source scopes' in body and '7 deferred by the status cap' in body
    assert 'not individually delivered here' in body
    assert value.metadata.run_id in body
    assert len(ids)==5


def test_invalid_run_summary_cannot_freeze_a_partial_envelope(store):
    import pytest
    from test_v6_delivery_recovery import count
    stage(store,result(),card_cap=0)
    transport=DigestTransport(store.delivery)
    ids=[r['id'] for r in store.delivery.inspect()]
    with pytest.raises(ValueError,match='staged run'):
        transport.prepare(EMAIL,ids,now=101,summary_run_id='not-this-workspace')
    assert count(store,'delivery_envelopes')==0
