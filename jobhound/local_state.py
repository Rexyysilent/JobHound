"""Closed local workspace setup, diagnostics and byte-verified recovery."""
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import tempfile

from .local_paths import LocalPaths, local_scope, no_link, profile_name

MARKER='.jobhound-local.json'
POLICIES=('config.yaml','profile.yaml','platform_registry.yaml')
EXAMPLES=('config.example.yaml','profile.example.yaml','platform_registry.example.yaml')
ROOT=Path(__file__).resolve().parent.parent
DEPENDENCIES={'httpx':(0,27),'pydantic':(2,7),'pydantic-settings':(2,3),
    'python-dotenv':(1,0),'rapidfuzz':(3,9),'ftfy':(6,3),'PyYAML':(6,0)}


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def _object(pairs):
    row={}
    for key,value in pairs:
        if key in row:raise ValueError('duplicate_local_json_key')
        row[key]=value
    return row


def _json(path,limit=2_000_000):
    no_link(path)
    if Path(path).stat().st_size>limit:raise ValueError('local_manifest_too_large')
    try:return json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=_object)
    except (ValueError,UnicodeError):raise ValueError('invalid_local_manifest') from None


def _marker(root):
    no_link(root);row=_json(root/MARKER,8192)
    if (not isinstance(row,dict) or set(row)!={'schema','profiles','default_profile','created_at'}
            or row['schema']!='jobhound-local/v1' or not isinstance(row['profiles'],list)
            or not 1<=len(row['profiles'])<=16):raise ValueError('invalid_local_workspace_marker')
    names=[profile_name(name) for name in row['profiles']]
    if len(names)!=len(set(names)) or row['default_profile'] not in names:
        raise ValueError('invalid_local_workspace_profiles')
    return row


def paths(data_dir,profile=None):
    root=Path(data_dir).expanduser().absolute();no_link(root);root=root.resolve()
    row=_marker(root);name=profile_name(profile) if profile else row['default_profile']
    if name not in row['profiles']:raise ValueError('local_profile_not_initialized')
    result=LocalPaths(root,name)
    for part in (root/'profiles',result.policy_dir,result.state_dir,result.reviews_dir):
        no_link(part)
        if not part.is_dir():raise ValueError('incomplete_local_profile')
    return result


def _write_json(path,row):
    Path(path).write_text(json.dumps(row,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')


def _new_target(path):
    target=Path(path).expanduser().absolute();no_link(target);target=target.resolve()
    if target.exists():raise ValueError('local_destination_must_be_new')
    if target==ROOT or target in ROOT.parents:raise ValueError('local_destination_is_repository')
    return target


def _discard_owned_stage(stage,parent):
    # Only the concrete mkdtemp directory allocated by this operation is removed.
    no_link(stage)
    resolved=stage.resolve()
    if resolved.parent!=parent.resolve() or not resolved.name.startswith('.jobhound-local-'):
        raise ValueError('invalid_local_staging_cleanup')
    shutil.rmtree(resolved)


def _publish(target,writer):
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.jobhound-local-',dir=target.parent))
    try:
        writer(stage)
        # rename never replaces an existing nonempty workspace; check even empty roots.
        if target.exists():raise ValueError('local_destination_must_be_new')
        # Atomic reservation also refuses an empty directory created concurrently.
        target.mkdir(exist_ok=False)
        for child in sorted(stage.iterdir(),key=lambda path:path.name in {MARKER,'manifest.json'}):
            child.rename(target/child.name)
    finally:
        if stage.exists():_discard_owned_stage(stage,target.parent)


def _profile_files(root,name):
    profile=LocalPaths(root,name)
    profile.policy_dir.mkdir(parents=True,exist_ok=False)
    profile.state_dir.mkdir();profile.reviews_dir.mkdir()
    for source,target in zip(EXAMPLES,POLICIES):
        shutil.copyfile(ROOT/source,profile.policy_dir/target)


def initialize(data_dir,profile='local'):
    name=profile_name(profile);target=_new_target(data_dir)
    def write(stage):
        _profile_files(stage,name)
        _write_json(stage/MARKER,dict(schema='jobhound-local/v1',profiles=[name],default_profile=name,
            created_at=datetime.now(timezone.utc).isoformat()))
        load_policies(LocalPaths(stage,name))
    _publish(target,write)
    return dict(schema='jobhound-local-init/v1',data_dir=str(target),profile=name,network_calls=0,provider_calls=0)


def add_profile(data_dir,name):
    name=profile_name(name);current=paths(data_dir);marker=_marker(current.root)
    if name in marker['profiles'] or len(marker['profiles'])>=16:raise ValueError('local_profile_exists_or_limit')
    target=current.root/'profiles'/name
    if target.exists():raise ValueError('local_profile_exists_or_limit')
    # Keep the new directory undiscoverable until policy validation succeeds.
    stage=Path(tempfile.mkdtemp(prefix='.jobhound-local-',dir=current.root/'profiles'))
    try:
        for source,destination in zip(EXAMPLES,POLICIES):shutil.copyfile(ROOT/source,stage/destination)
        (stage/'state').mkdir();(stage/'reviews').mkdir()
        stage.rename(target)
        try:load_policies(LocalPaths(current.root,name))
        except Exception:
            # Preserve a failed profile rather than deleting a user-visible path.
            target.rename(stage);raise
        marker['profiles'].append(name)
        temporary=current.root/(MARKER+'.new')
        with temporary.open('x',encoding='utf-8') as handle:json.dump(marker,handle,indent=2)
        os.replace(temporary,current.root/MARKER)
    finally:
        if stage.exists():_discard_owned_stage(stage,current.root/'profiles')
    return dict(profile=name,data_dir=str(current.root),provider_calls=0)


def load_policies(local):
    with local_scope(local):
        import yaml
        from .config import load_config
        from .filters.eligibility import Profile
        from .trust.registry import load_registry
        for name in POLICIES:no_link(local.policy_dir/name)
        try:
            config=load_config(local.policy_dir/'config.yaml')
            profile=Profile.from_config(yaml.safe_load((local.policy_dir/'profile.yaml').read_text(encoding='utf-8')) or {})
            registry=load_registry(local.policy_dir/'platform_registry.yaml',cfg=config.trust)
        except Exception:raise ValueError('invalid_local_policy') from None
        if (config.v55.production_approved or config.delivery.enabled or config.delivery.production_approved
                or config.email_ingest.enabled or config.incidents.enabled or config.scam.llm.enabled
                or any(getattr(config.sources,name).enabled for name in type(config.sources).model_fields)):
            raise ValueError('local_policy_enables_side_effects')
        for value in (config.v41.sidecar_db,config.v41.snapshot_dir):
            if local.state_dir not in local.policy_path(value).resolve().parents and local.policy_path(value).resolve()!=local.state_dir:
                raise ValueError('local_state_path_outside_state_directory')
        return config,profile,registry


def _relative(value):
    if not isinstance(value,str) or '\\' in value or ':' in value:
        raise ValueError('invalid_local_backup_path')
    parts=PurePosixPath(value).parts
    if not parts or PurePosixPath(value).is_absolute() or any(part in ('.','..') for part in value.split('/')):
        raise ValueError('invalid_local_backup_path')
    reserved={'con','prn','aux','nul',*[f'com{i}' for i in range(1,10)],*[f'lpt{i}' for i in range(1,10)]}
    if any(any(ch in '<>"|?*' for ch in part) or part.endswith((' ','.'))
            or part.split('.')[0].casefold() in reserved for part in parts):
        raise ValueError('nonportable_local_backup_path')
    return parts


def _files(root):
    marker=_marker(root);rows=[];seen=set()
    for path in sorted(root.rglob('*')):
        no_link(path)
        value=path.relative_to(root).as_posix();parts=_relative(value)
        if value.casefold() in seen:raise ValueError('local_case_collision')
        seen.add(value.casefold())
        allowed=(value==MARKER or value=='profiles' or (len(parts)>=2 and parts[0]=='profiles' and parts[1] in marker['profiles']
            and (len(parts)==2 or parts[2] in (*POLICIES,'state','reviews'))))
        if not allowed or any(part.casefold()=='.env' or part.casefold().startswith('.env.') for part in parts):
            raise ValueError('unexpected_local_workspace_file')
        if path.is_file():
            if path.name.endswith(('-wal','-shm','-journal')):raise ValueError('local_database_must_be_closed')
            size=path.stat().st_size
            if size>4*1024**3:raise ValueError('local_backup_file_limit')
            rows.append(dict(path=value,bytes=size,sha256=sha(path)))
    if len(rows)>5000 or sum(r['bytes'] for r in rows)>16*1024**3:raise ValueError('local_backup_limit')
    names={row['path'] for row in rows}
    if any(f'profiles/{name}/{policy}' not in names for name in marker['profiles'] for policy in POLICIES):
        raise ValueError('incomplete_local_profile_policies')
    return sorted(rows,key=lambda row:row['path'])


def _database(path):
    if path.suffix.lower() not in {'.db','.sqlite','.sqlite3'}:return None
    for suffix in ('-wal','-shm','-journal'):
        if Path(str(path)+suffix).exists():raise ValueError('local_database_must_be_closed')
    # Closed state only: immutable prevents SQLite from creating journals/sidecars.
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('local_database_integrity_failed')
        schema=list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
        return dict(integrity='ok',foreign_key_errors=0,schema_sha256=hashlib.sha256(json.dumps(schema).encode()).hexdigest())
    finally:db.close()


def doctor(data_dir=None,profile=None):
    checks={};versions={}
    checks['python_3_12_or_newer']=sys.version_info>=(3,12)
    for name,minimum in DEPENDENCIES.items():
        try:
            version=importlib.metadata.version(name);versions[name]=version
            checks['dependency:'+name]=tuple(int(p) for p in version.split('.')[:2])>=minimum
        except (importlib.metadata.PackageNotFoundError,ValueError):checks['dependency:'+name]=False
    if data_dir is None:
        return dict(schema='jobhound-local-doctor/v1',ok=all(checks.values()),checks=checks,versions=versions,
            python=sys.version.split()[0],sqlite=sqlite3.sqlite_version,workspace_checked=False,
            network_calls=0,provider_calls=0,writes=0)
    local=paths(data_dir,profile)
    if all(checks.values()):
        try:load_policies(local);checks['local_policy']=True
        except ValueError:checks['local_policy']=False
    else:checks['local_policy']=False
    databases=[]
    try:
        for row in _files(local.root):
            info=_database(local.root/row['path'])
            if info:databases.append(info)
        checks['closed_state_integrity']=True
    except (ValueError,sqlite3.Error):checks['closed_state_integrity']=False
    return dict(schema='jobhound-local-doctor/v1',ok=all(checks.values()),checks=checks,
        versions=versions,python=sys.version.split()[0],sqlite=sqlite3.sqlite_version,
        profile=local.profile,initialized_profiles=_marker(local.root)['profiles'],workspace_checked=True,
        databases=databases,network_calls=0,provider_calls=0,writes=0)


def backup(data_dir,output):
    local=paths(data_dir);rows=_files(local.root)
    directories=_directories(local.root)
    databases={row['path']:info for row in rows if (info:=_database(local.root/row['path'])) is not None}
    target=_new_target(output)
    if local.root in target.parents or target in local.root.parents:raise ValueError('local_backup_must_be_outside_workspace')
    if _free_space(target.parent)<sum(row['bytes'] for row in rows)+16*1024**2:
        raise ValueError('insufficient_local_backup_space')
    def write(stage):
        for directory in directories:(stage/'files'/directory).mkdir(parents=True,exist_ok=True)
        for row in rows:
            source=local.root/row['path'];destination=stage/'files'/row['path']
            destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,destination)
            if sha(source)!=row['sha256'] or sha(destination)!=row['sha256']:raise ValueError('local_backup_source_changed')
        if _files(local.root)!=rows or _directories(local.root)!=directories:raise ValueError('local_backup_source_changed')
        _write_json(stage/'manifest.json',dict(schema='jobhound-local-backup/v1',files=rows,databases=databases,
            directories=directories,created_at=datetime.now(timezone.utc).isoformat(),profiles=_marker(local.root)['profiles']))
    _publish(target,write)
    return dict(schema='jobhound-local-backup-receipt/v1',output=str(target),files=len(rows),
        databases=len(databases),all_bytes_verified=True,provider_calls=0)


def restore(backup_dir,data_dir):
    source=Path(backup_dir).expanduser().absolute();no_link(source);source=source.resolve()
    manifest=_json(source/'manifest.json')
    if (not isinstance(manifest,dict) or set(manifest)!={'schema','files','directories','databases','created_at','profiles'}
            or manifest['schema']!='jobhound-local-backup/v1' or not isinstance(manifest['files'],list)
            or not 1<=len(manifest['files'])<=5000 or not isinstance(manifest['databases'],dict)
            or not isinstance(manifest['directories'],list) or len(manifest['directories'])>5000):
        raise ValueError('invalid_local_backup_manifest')
    rows=manifest['files'];seen=set()
    for row in rows:
        if (not isinstance(row,dict) or set(row)!={'path','bytes','sha256'} or type(row['bytes']) is not int
                or not 0<=row['bytes']<=4*1024**3 or not isinstance(row['sha256'],str)
                or len(row['sha256'])!=64 or any(ch not in '0123456789abcdef' for ch in row['sha256'])):
            raise ValueError('invalid_local_backup_manifest')
        _relative(row['path'])
        if row['path'].casefold() in seen:raise ValueError('duplicate_local_backup_path')
        seen.add(row['path'].casefold());file=source/'files'/row['path']
        for part in (file,*file.parents):
            if part==source.parent:break
            no_link(part)
        if file.stat().st_size!=row['bytes'] or sha(file)!=row['sha256']:raise ValueError('local_backup_checksum_mismatch')
    if sum(row['bytes'] for row in rows)>16*1024**3:raise ValueError('local_backup_limit')
    observed={p.relative_to(source/'files').as_posix() for p in (source/'files').rglob('*') if p.is_file()}
    if observed!={row['path'] for row in rows}:raise ValueError('local_backup_has_unlisted_files')
    if MARKER not in {row['path'] for row in rows}:raise ValueError('local_backup_marker_missing')
    # Validate complete ownership and every SQLite file before creating a destination.
    if _files(source/'files')!=sorted(rows,key=lambda row:row['path']):raise ValueError('local_backup_manifest_incomplete')
    for directory in manifest['directories']:_relative(directory)
    if manifest['directories']!=_directories(source/'files'):raise ValueError('local_backup_directory_proof_mismatch')
    if manifest['profiles']!=_marker(source/'files')['profiles']:raise ValueError('local_backup_profile_mismatch')
    actual={row['path']:info for row in rows if (info:=_database(source/'files'/row['path'])) is not None}
    if actual!=manifest['databases']:raise ValueError('local_backup_database_proof_mismatch')
    target=_new_target(data_dir)
    if source in target.parents or target in source.parents:raise ValueError('local_restore_must_be_outside_backup')
    if _free_space(target.parent)<sum(row['bytes'] for row in rows)+16*1024**2:raise ValueError('insufficient_local_restore_space')
    def write(stage):
        for directory in manifest['directories']:(stage/directory).mkdir(parents=True,exist_ok=True)
        for row in rows:
            destination=stage/row['path'];destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/'files'/row['path'],destination)
            if sha(destination)!=row['sha256']:raise ValueError('local_backup_source_changed')
        marker=_marker(stage)
        for name in marker['profiles']:
            local=LocalPaths(stage,name);local.state_dir.mkdir(exist_ok=True);local.reviews_dir.mkdir(exist_ok=True)
        if (_files(stage)!=sorted(rows,key=lambda row:row['path']) or _directories(stage)!=manifest['directories']):
            raise ValueError('local_restore_verification_failed')
    _publish(target,write)
    return dict(schema='jobhound-local-restore-receipt/v1',data_dir=str(target),files=len(rows),
        profiles=manifest['profiles'],all_bytes_verified=True,provider_calls=0)


def _free_space(path):
    while not path.exists():path=path.parent
    return shutil.disk_usage(path).free


def _directories(root):
    result=[]
    for path in root.rglob('*'):
        no_link(path)
        if path.is_dir():result.append(path.relative_to(root).as_posix())
    return sorted(result)
