"""Explicit local workspaces; no environment, policy or database reads here."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import re

_LOCAL = ContextVar('jobhound_local_workspace', default=None)
_DEVICES = {'con','prn','aux','nul',*[f'com{i}' for i in range(1,10)],*[f'lpt{i}' for i in range(1,10)]}


def profile_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,47}',value) or value in _DEVICES:
        raise ValueError('invalid_local_profile_name')
    return value


def no_link(path):
    path=Path(path)
    if (path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction())
            or (path.exists() and getattr(path.lstat(),'st_file_attributes',0)&0x400)):
        raise ValueError('local_link_not_allowed')


@dataclass(frozen=True)
class LocalPaths:
    root: Path
    profile: str

    @property
    def policy_dir(self):return self.root/'profiles'/self.profile
    @property
    def state_dir(self):return self.policy_dir/'state'
    @property
    def reviews_dir(self):return self.policy_dir/'reviews'

    def policy_path(self,value):
        path=Path(value)
        path=path if path.is_absolute() else self.policy_dir/path
        if path.resolve()!=self.policy_dir and self.policy_dir not in path.resolve().parents:
            raise ValueError('local_policy_path_outside_profile')
        return path


def current_local():return _LOCAL.get()


def local_review_store_allowed(path):
    """Only a marked explicit delivery copy inside this profile's review run."""
    if path is None or current_local() is None:return False
    from .run_context import current_run
    context=current_run()
    if context is None or context.network_allowed:return False
    candidate=Path(path).resolve();local=current_local()
    workspace=Path(context.workspace).resolve()
    if (candidate.name!='delivery.sqlite' or local.reviews_dir not in candidate.parents
            or workspace not in candidate.parents):return False
    no_link(path);no_link(candidate.parent)
    marker=candidate.parent/'.jobhound-signal-review'
    no_link(marker)
    return marker.is_file() and marker.stat().st_size<64 and marker.read_text(encoding='utf-8')=='disposable-handoff-review/v1\n'


def local_review_connection_allowed(connection):
    if current_local() is None:return False
    databases=list(connection.execute('PRAGMA database_list'))
    # An attached database cannot inherit the local copy's write permission.
    if any(row[1] not in {'main','temp'} for row in databases):return False
    main=next((row[2] for row in databases if row[1]=='main'),None)
    return bool(main) and local_review_store_allowed(main)


@contextmanager
def local_scope(paths):
    token=_LOCAL.set(paths)
    try:yield paths
    finally:_LOCAL.reset(token)
