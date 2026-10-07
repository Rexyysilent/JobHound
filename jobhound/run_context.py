"""Immutable run inputs with task-local compatibility for existing call sites.

Serialized inputs cannot be changed through a caller-owned model. Config views
resolve at call time (including legacy default arguments). Containers returned
inside a scope are detached copies; model attribute writes are rejected.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

_ACTIVE = ContextVar('jobhound_run', default=None)
_CONFIG = ContextVar('jobhound_run_config', default=None)


def current_run():
    return _ACTIVE.get()


def deny_review_side_effect(action):
    if current_run() and not current_run().side_effects_allowed:
        raise RuntimeError('review context forbids ' + action)


def _json(value):
    # Mapping insertion order is policy-significant (ordered aliases/rules and
    # explanation/task order). Sorting keys here changes an otherwise same run.
    return json.dumps(value, default=lambda v: sorted(v) if isinstance(v, set) else str(v))


@dataclass(frozen=True)
class RunContext:
    config_json: str
    profile_json: str
    registry_json: str
    as_of: datetime
    workspace: str
    run_id: str
    network_allowed: bool = False
    side_effects_allowed: bool = False
    versions_json: str = '{}'

    @classmethod
    def capture(cls, *, config=None, profile=None, registry=None, as_of=None,
                workspace='.', run_id=None, network_allowed=False,
                side_effects_allowed=False):
        from .config import CONFIG
        from .filters.eligibility import default_profile
        from .trust.registry import default_registry
        config = config if config is not None else CONFIG
        profile = profile if profile is not None else default_profile()
        registry = registry if registry is not None else default_registry()
        now = as_of or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError('run clock must be timezone-aware')
        from .versions import runtime_versions
        return cls(_json(config.model_dump(mode='json')), _json(asdict(profile)),
                   _json({k: v.model_dump(mode='json') for k, v in registry.items()}),
                   now, str(Path(workspace).resolve()), run_id or uuid4().hex,
                   network_allowed, side_effects_allowed, _json(runtime_versions()))

    def config(self):
        from .config import Config
        return Config.model_validate_json(self.config_json)

    def profile(self):
        from .filters.eligibility import Profile
        data = json.loads(self.profile_json)
        for name, field in Profile.__dataclass_fields__.items():
            if str(field.type).startswith('set['):
                data[name] = set(data[name])
        return Profile(**data)

    def registry(self):
        from .models import PlatformTrust
        return {k: PlatformTrust.model_validate(v) for k, v in json.loads(self.registry_json).items()}

    def fingerprint(self):
        return hashlib.sha256((self.config_json+self.profile_json+self.registry_json+
                               self.as_of.isoformat()+self.versions_json).encode()).hexdigest()


@contextmanager
def run_scope(context):
    active = _ACTIVE.set(context)
    config = _CONFIG.set(context.config())
    try:
        yield context
    finally:
        _CONFIG.reset(config)
        _ACTIVE.reset(active)


class ConfigView:
    """Resolve models lazily so imported default arguments remain task-local."""
    def __init__(self, default, path=()):
        object.__setattr__(self, '_default', default)
        object.__setattr__(self, '_path', path)

    def _value(self):
        value = _CONFIG.get()
        if value is None:
            value = self._default
        for key in self._path:
            value = getattr(value, key)
        return value

    def __getattr__(self, name):
        value = getattr(self._value(), name)
        if isinstance(value, BaseModel):
            return ConfigView(self._default, (*self._path, name))
        # Model methods only return copies/serialized values in application use.
        if callable(value):
            return getattr(deepcopy(self._value()), name) if current_run() else value
        return deepcopy(value) if current_run() else value

    def __setattr__(self, name, value):
        if current_run():
            raise TypeError('immutable run configuration; create a new context')
        setattr(self._value(), name, value)

    def __deepcopy__(self, memo):
        return deepcopy(self._value(), memo)
