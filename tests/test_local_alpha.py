"""Actual local workflow isolation and recovery; no provider or production state."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime

import pytest
import yaml

from jobhound.local_paths import local_scope,current_local,LocalPaths
from jobhound import local_state as state
from jobhound.local_demo import demonstrate


def initialize(tmp_path,name='english'):
    root=tmp_path/'workspace';state.initialize(root,name)
    # Explicit synthetic inputs, separate from the qualification-free starter.
    examples=Path(__file__).resolve().parents[1]/'examples/local'
    for example,target in [('config.example.yaml','config.yaml'),('profile.example.yaml','profile.yaml')]:
        (root/'profiles'/name/target).write_text((examples/example).read_text(encoding='utf-8'),encoding='utf-8')
    return root


def contents(root):return {p.relative_to(root).as_posix():state.sha(p) for p in root.rglob('*') if p.is_file()}


def test_demo_uses_native_interpreter_without_enabling_starter_or_providers(tmp_path):
    root=initialize(tmp_path)
    file=root/'profiles/english/config.yaml'
    policy=yaml.safe_load(file.read_text(encoding='utf-8'))
    policy['v55']['enabled']=False
    file.write_text(yaml.safe_dump(policy),encoding='utf-8')
    before=file.read_bytes()
    result=demonstrate(root,'english')
    assert result['accounting_ok'] and result['decisions']['discovery']=='apply'
    assert result['calls']==dict(network=0,provider=0)
    assert file.read_bytes()==before


def test_full_engine_profiles_doctor_and_closed_recovery_preserve_accepted_history(tmp_path):
    from test_handoff_preview_lifecycle import open_review
    from test_v6_transport import Sender,send
    from jobhound.handoff_review import DESTINATIONS
    root=initialize(tmp_path);state.add_profile(root,'french')
    (root/'profiles/english/reviews/empty-reserved-output').mkdir()
    french=root/'profiles/french/profile.yaml';policy=yaml.safe_load(french.read_text())
    policy['languages']=policy['working_languages']=['French'];french.write_text(yaml.safe_dump(policy),encoding='utf-8')
    english=demonstrate(root,'english');other=demonstrate(root,'french')
    assert english['decisions']==dict(scope='clarify_scope',assessment='complete_known_step',
        discovery='apply',access='apply',rejection='await_change')
    assert english['synthetic_reissued_generations']==2 and not any(english['calls'].values())
    assert other['decisions']['discovery']=='skip'
    assert current_local() is None
    # A fake accepted receipt adds actual immutable delivery history before backup.
    db,transport=open_review(Path(english['output'])/'updated')
    envelope=db.conn.execute("SELECT id FROM delivery_envelopes WHERE status='pending' AND channel='email' ORDER BY id").fetchone()[0]
    sender=Sender(DESTINATIONS[0]);assert send(transport,envelope,sender,now=datetime.fromisoformat(english['as_of']).timestamp()+1)=='accepted'
    baseline=db.conn.execute('SELECT count(*) FROM delivery_baselines').fetchone()[0]
    assert baseline>0 and sender.calls;db.close()
    before=contents(root);doctor=state.doctor(root,'english')
    assert doctor['ok'] and doctor['writes']==0 and len(doctor['databases'])==4
    assert contents(root)==before
    backup=tmp_path/'backup';receipt=state.backup(root,backup)
    assert receipt['all_bytes_verified'] and contents(root)==before
    restored=tmp_path/'restored';receipt=state.restore(backup,restored)
    assert receipt['all_bytes_verified'] and contents(restored)==before
    assert (restored/'profiles/english/reviews/empty-reserved-output').is_dir()
    assert state.doctor(restored,'french')['ok']
    with sqlite3.connect(restored/'profiles/english/reviews/demo/updated/delivery.sqlite') as copied:
        assert copied.execute('SELECT count(*) FROM delivery_baselines').fetchone()[0]==baseline
        assert copied.execute("SELECT count(*) FROM delivery_attempt_events WHERE event='accepted'").fetchone()[0]>0


@pytest.mark.parametrize('bad',['../escape','UPPER','con','nul','com1','', 'first/second'])
def test_invalid_profile_does_not_create_any_workspace(tmp_path,bad):
    with pytest.raises(ValueError):state.initialize(tmp_path/'new-parent'/'target',bad)
    assert not (tmp_path/'new-parent').exists()


def test_init_and_restore_refuse_existing_even_empty_destinations(tmp_path):
    root=initialize(tmp_path);empty=tmp_path/'empty';empty.mkdir()
    with pytest.raises(ValueError,match='must_be_new'):state.initialize(empty)
    backup=tmp_path/'backup';state.backup(root,backup)
    with pytest.raises(ValueError,match='must_be_new'):state.restore(backup,empty)
    assert list(empty.iterdir())==[]


def test_corrupted_backup_is_rejected_before_creating_parent_or_destination(tmp_path):
    root=initialize(tmp_path);backup=tmp_path/'backup';state.backup(root,backup)
    (backup/'files/profiles/english/profile.yaml').write_text('corrupted fixture',encoding='utf-8')
    with pytest.raises(ValueError,match='checksum_mismatch'):state.restore(backup,tmp_path/'new-parent'/'target')
    assert not (tmp_path/'new-parent').exists()


@pytest.mark.parametrize('case',['traversal','absolute','windows','duplicate','boolean_bytes','extra_field','missing_policy'])
def test_invalid_manifest_cannot_restore_an_incomplete_or_foreign_file_set(tmp_path,case):
    root=initialize(tmp_path);backup=tmp_path/'backup';state.backup(root,backup)
    manifest_path=backup/'manifest.json';manifest=json.loads(manifest_path.read_text())
    if case=='traversal':manifest['files'][0]['path']='../outside'
    if case=='absolute':manifest['files'][0]['path']='/outside'
    if case=='windows':manifest['files'][0]['path']='C:\\outside'
    if case=='duplicate':manifest['files'].append(dict(manifest['files'][0]))
    if case=='boolean_bytes':manifest['files'][0]['bytes']=True
    if case=='extra_field':manifest['files'][0]['unexpected']='value'
    if case=='missing_policy':
        victim=next(r for r in manifest['files'] if r['path'].endswith('/profile.yaml'))
        manifest['files'].remove(victim);(backup/'files'/victim['path']).unlink()
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError):state.restore(backup,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()


@pytest.mark.parametrize('suffix',['-wal','-shm','-journal'])
def test_active_database_sidecars_are_unknown_to_doctor_and_not_backed_up(tmp_path,suffix):
    root=initialize(tmp_path);db=root/'profiles/english/state/local.sqlite'
    with sqlite3.connect(db) as conn:conn.execute('CREATE TABLE example(id INTEGER)')
    Path(str(db)+suffix).write_bytes(b'active synthetic sidecar')
    assert not state.doctor(root)['ok']
    with pytest.raises(ValueError,match='must_be_closed'):state.backup(root,tmp_path/'backup')
    assert not (tmp_path/'backup').exists()


@pytest.mark.parametrize('field',['v55','delivery','email_ingest','incidents','scam','sources'])
def test_local_policy_enabling_external_effects_fails_diagnostics_and_demo(tmp_path,field):
    root=initialize(tmp_path);file=root/'profiles/english/config.yaml';config=yaml.safe_load(file.read_text())
    if field=='v55':config[field]['production_approved']=True
    elif field=='scam':config[field]['llm']['enabled']=True
    elif field=='sources':config[field]['remotive']['enabled']=True
    else:config[field]['enabled']=True
    file.write_text(yaml.safe_dump(config),encoding='utf-8')
    assert not state.doctor(root)['ok']
    with pytest.raises(ValueError,match='enables_side_effects'):demonstrate(root)
    assert not (root/'profiles/english/reviews/demo').exists()


def test_env_and_foreign_files_are_rejected_without_reading_contents(tmp_path,monkeypatch):
    root=initialize(tmp_path);secret=root/'profiles/english/reviews/.ENV';secret.write_bytes(b'must remain unread')
    original=state.sha
    def spy(path):
        assert Path(path)!=secret,'env content was read'
        return original(path)
    monkeypatch.setattr(state,'sha',spy)
    with pytest.raises(ValueError,match='unexpected'):state.backup(root,tmp_path/'backup')
    assert not (tmp_path/'backup').exists()


def test_backup_inside_workspace_does_not_create_parent_or_change_source(tmp_path):
    root=initialize(tmp_path);before=contents(root)
    with pytest.raises(ValueError,match='outside_workspace'):state.backup(root,root/'reviews'/'nested'/'backup')
    assert not (root/'reviews').exists() and contents(root)==before


def test_copy_failure_and_source_change_never_publish_a_partial_backup(tmp_path,monkeypatch):
    root=initialize(tmp_path);original=state.shutil.copyfile
    def mutate(source,target):
        result=original(source,target)
        if Path(source).name=='profile.yaml':Path(source).write_text('changed during backup',encoding='utf-8')
        return result
    monkeypatch.setattr(state.shutil,'copyfile',mutate)
    with pytest.raises(ValueError,match='source_changed'):state.backup(root,tmp_path/'backup')
    assert not (tmp_path/'backup').exists() and not list(tmp_path.glob('.jobhound-local-*'))


def test_local_review_permission_never_covers_other_stores_attached_databases_or_dispatch(tmp_path):
    from jobhound.run_context import RunContext,run_scope,deny_review_side_effect
    from jobhound.v41.store import V41Store
    from jobhound.store import Store
    from jobhound.delivery_outbox import transaction
    root=initialize(tmp_path);local=state.paths(root)
    config,profile,registry=state.load_policies(local)
    target=local.reviews_dir/'guard';target.mkdir()
    (target/'.jobhound-signal-review').write_text('disposable-handoff-review/v1\n',encoding='utf-8')
    context=RunContext.capture(config=config,profile=profile,registry=registry,workspace=target)
    with local_scope(local),run_scope(context):
        with pytest.raises(RuntimeError):Store(tmp_path/'legacy.sqlite')
        with pytest.raises(RuntimeError):V41Store(tmp_path/'foreign.sqlite')
        with pytest.raises(RuntimeError):V41Store()
        with pytest.raises(RuntimeError):deny_review_side_effect('delivery dispatch')
        db=V41Store(target/'delivery.sqlite')
        with transaction(db.conn):db.conn.execute('CREATE TABLE local_example(id INTEGER)')
        db.conn.execute("ATTACH DATABASE ':memory:' AS other_database")
        with pytest.raises(RuntimeError):
            with transaction(db.conn):db.conn.execute('INSERT INTO local_example VALUES(1)')
        db.close()
    assert not (tmp_path/'foreign.sqlite').exists() and not (tmp_path/'legacy.sqlite').exists()


def test_actual_run_entrypoint_never_opens_operator_policies_dotenv_or_provider_sockets(tmp_path):
    root=initialize(tmp_path);repo=Path(__file__).resolve().parents[1]
    guard=tmp_path/'guard.py'
    guard.write_text('''import json,pathlib,runpy,socket,sys
repo=pathlib.Path(sys.argv[1]);root=sys.argv[2]
blocked={str(repo/name).casefold() for name in ('.env','config.yaml','profile.yaml','platform_registry.yaml')}
def audit(event,args):
    if event=='open' and isinstance(args[0],(str,bytes)):
        if str(pathlib.Path(args[0]).absolute()).casefold() in blocked:raise AssertionError('operator file read')
sys.addaudithook(audit)
def network(*a,**k):raise AssertionError('provider network call')
socket.socket.connect=socket.create_connection=network
sys.path.insert(0,str(repo));sys.argv=['run.py','local','demo','--data-dir',root]
runpy.run_path(str(repo/'run.py'),run_name='__main__')
from jobhound.settings import settings
assert not any(settings.model_dump().values())
''',encoding='utf-8')
    import os
    environment=dict(os.environ,TELEGRAM_BOT_TOKEN='synthetic-inherited-token',EMAIL_APP_PASSWORD='synthetic-inherited-password',
        PYTHONDONTWRITEBYTECODE='1')
    run=subprocess.run([sys.executable,'-B',str(guard),str(repo),str(root)],capture_output=True,text=True,env=environment)
    assert run.returncode==0,run.stderr
    assert 'synthetic-inherited-token' not in run.stdout+run.stderr
    assert 'synthetic-inherited-password' not in run.stdout+run.stderr
    assert json.loads(run.stdout)['calls']==dict(network=0,provider=0)


def test_actual_local_reviewed_files_use_selected_profile_and_reject_foreign_delivery_state(tmp_path):
    from jobhound.local_cli import main
    root=initialize(tmp_path);first=demonstrate(root)
    demo=Path(first['output']);snapshot=next((demo/'inputs').glob('*.jsonl'))
    common=['review','outcomes',str(snapshot),'--data-dir',str(root),'--events',str(demo/'events.input.jsonl'),
        '--bindings',str(demo/'bindings.input.jsonl'),'--as-of',first['as_of']]
    assert main(common+['--name','reviewed','--handoff-signal'])==0
    report=json.loads((root/'profiles/english/reviews/reviewed/signal-receipt.json').read_text())
    assert report['provider_calls']==0 and report['database_integrity']=='ok'
    state.add_profile(root,'second')
    assert main(common+['--name','foreign','--profile','second','--handoff-signal',
        '--delivery-state',str(demo/'updated/delivery.sqlite')])==1
    assert not (root/'profiles/second/reviews/foreign').exists()
    assert main(['review','replay',str(snapshot),'--data-dir',str(root),'--name','replayed'])==0
    assert json.loads((root/'profiles/english/reviews/replayed/audit.json').read_text())['accounting_ok']
