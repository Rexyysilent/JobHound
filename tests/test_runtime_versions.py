"""All decision code and declared installed dependencies bind replay identity."""
from pathlib import Path

from jobhound.versions import runtime_versions
from jobhound.run_context import RunContext
from jobhound.v41.models import RunMetadata
from datetime import datetime, timezone


def test_a_normalizer_change_changes_the_code_manifest(tmp_path):
    module = tmp_path / 'jobhound' / 'normalize.py'
    module.parent.mkdir()
    module.write_text('old', encoding='utf-8')
    before = runtime_versions(tmp_path)
    module.write_text('new', encoding='utf-8')
    after = runtime_versions(tmp_path)
    assert before['code_sha256'] != after['code_sha256']
    assert before['dependencies_sha256'] == after['dependencies_sha256']


def test_context_fingerprint_changes_with_bound_versions():
    from dataclasses import replace
    context = RunContext.capture()
    assert replace(context, versions_json='{"code_sha256":"new"}').fingerprint() != context.fingerprint()
    assert 'jobhound/normalize.py' in runtime_versions()['files']
    assert all(not name.endswith(('.env', '.yaml')) for name in runtime_versions()['files'])


def test_old_metadata_remains_readable():
    now = datetime.now(timezone.utc)
    old = RunMetadata(run_id='old', started_at=now, as_of=now, ruleset_hash='old',
        profile_hash='old', trust_registry_hash='old', config_hash='old')
    assert old.runtime_versions == {} and old.input_sha256 is None
