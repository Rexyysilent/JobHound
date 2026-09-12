"""Privacy checks use synthetic values and cannot access real accounts."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publication_check", ROOT / "tools/check_publication.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_public_profile_contains_no_operator_declarations():
    import yaml
    data = yaml.safe_load((ROOT / "profile.example.yaml").read_text(encoding="utf-8"))
    for key in ("languages", "credential_domains", "anchors", "working_languages", "capability_domains",
                "demonstrated_skills", "formal_credentials", "absent_formal_credentials",
                "documented_professional_years", "target_lanes"):
        assert not data[key]
    assert data["eligibility"]["known_languages"]
    assert data["eligibility"]["gated_role_patterns"]


def test_public_starter_disables_network_sources_and_delivery():
    from jobhound.config import load_config
    config = load_config(ROOT / "config.example.yaml")
    assert not any(getattr(config.sources, field).enabled for field in type(config.sources).model_fields)
    assert not config.email_ingest.enabled and not config.digest.channels
    assert not config.scam.llm.enabled and not config.incidents.enabled
    assert not config.v55.enabled and not config.v55.account_states


def test_config_example_fallback_preserves_explicit_local_precedence(tmp_path, monkeypatch):
    from jobhound import config
    path = tmp_path / "config.yaml"
    example = tmp_path / "config.example.yaml"
    example.write_text("digest: {channels: []}", encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_PATH", path)
    assert config.load_config().digest.channels == []
    path.write_text("digest: {channels: [email]}", encoding="utf-8")
    assert config.load_config().digest.channels == ["email"]


def test_profile_example_fallback_does_not_override_a_local_profile(tmp_path, monkeypatch):
    from jobhound.filters import eligibility
    path = tmp_path / "profile.yaml"
    path.with_name("profile.example.yaml").write_text("languages: []", encoding="utf-8")
    monkeypatch.setattr(eligibility, "_PROFILE_PATH", path)
    eligibility.default_profile.cache_clear()
    try:
        assert not eligibility.default_profile().languages
        path.write_text("languages: [english]", encoding="utf-8")
        eligibility.default_profile.cache_clear()
        assert eligibility.default_profile().languages == {"english"}
    finally:
        eligibility.default_profile.cache_clear()


@pytest.mark.parametrize("name", [".env", ".env.backup", "profile.yaml", "data/state.db",
                                 "roadmap/review.json", "private/memo.md", "keys/service.pem"])
def test_private_paths_cannot_pass_publication(name):
    assert checker.private_path(name)


def test_exact_value_beats_a_reviewed_test_exception():
    line = '"https://example.invalid/?token=synthetic-sensitive-value"'
    item = {"path": "test.py", "kind": "credential_query", "sha256": checker.line_hash(line)}
    findings, _ = checker.scan_text("test.py", line, ["synthetic-sensitive-value"], [item])
    assert any(row["kind"] == "configured_private_value" for row in findings)
    assert "synthetic-sensitive-value" not in json.dumps(findings)


def test_exception_is_bound_to_content_and_path():
    line = '"https://example.invalid/?token=synthetic-placeholder"'
    item = {"path": "test.py", "kind": "credential_query", "sha256": checker.line_hash(line)}
    assert not checker.scan_text("test.py", line, [], [item])[0]
    assert checker.scan_text("other.py", line, [], [item])[0]
    assert checker.scan_text("test.py", line + " changed", [], [item])[0]


def test_personal_email_and_home_path_are_reported_without_echoing_values():
    email = "synthetic" + "@" + "mail-provider.test-domain.com"
    path = "C:" + "/Users/" + "SyntheticPerson/project"
    findings, _ = checker.scan_text("sample.md", email + " " + path, [], [])
    assert {row["kind"] for row in findings} == {"email", "home_path"}
    assert email not in json.dumps(findings) and path not in json.dumps(findings)


def test_clean_examples_do_not_trigger_personal_email_detection():
    assert not checker.scan_text("sample.md", "person@example.com", [], [])[0]


def test_private_settings_are_not_read_before_synthetic_bootstrap(tmp_path):
    import os
    import shutil
    import subprocess
    import sys
    shutil.copytree(ROOT / "jobhound", tmp_path / "jobhound")
    shutil.copytree(ROOT / "tests/fixtures", tmp_path / "tests/fixtures")
    shutil.copy(ROOT / "tests/public_test_context.py", tmp_path / "tests/public_test_context.py")
    (tmp_path / "config.yaml").write_text("pay: {floor_usd: PRIVATE_TEST_MARKER}", encoding="utf-8")
    (tmp_path / ".env").write_text("EMAIL_TO=fixture-private-value", encoding="utf-8")
    script = (
        "import sys; sys.path.insert(0, 'tests'); "
        "from public_test_context import install; install(); "
        "from jobhound.settings import settings; "
        "from jobhound.config import CONFIG; "
        "assert not settings.email_to; assert CONFIG.pay.floor_usd == 4.5"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
                            env={**os.environ, "EMAIL_TO": "inherited-private-value"},
                            capture_output=True, text=True)
    assert result.returncode == 0, "Synthetic bootstrap failed"
    assert "PRIVATE_TEST_MARKER" not in result.stderr


def init_git(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("Git is needed for index-boundary tests")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    return subprocess


def test_index_scanner_refuses_nested_scope(tmp_path):
    sp = init_git(tmp_path)
    nested = tmp_path / "release"
    (nested / "tools").mkdir(parents=True)
    (nested / "tools/publication_exceptions.json").write_text("[]", encoding="utf-8")
    (tmp_path / ".env").write_text("UNSAFE=synthetic-value", encoding="utf-8")
    sp.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    with pytest.raises(ValueError, match="repository root"):
        checker.check(nested, index=True)


def test_index_scan_uses_staged_bytes_not_clean_worktree(tmp_path):
    sp = init_git(tmp_path)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/publication_exceptions.json").write_text("[]", encoding="utf-8")
    path = tmp_path / "notes.txt"
    value = "synthetic" + "@" + "mail-provider.test-domain.com"
    path.write_text(value, encoding="utf-8")
    sp.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    path.write_text("clean replacement", encoding="utf-8")
    assert not checker.check(tmp_path, index=True)["passed"]
    assert checker.check(tmp_path)["passed"]
    sp.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    path.write_text(value, encoding="utf-8")
    assert checker.check(tmp_path, index=True)["passed"]
    assert not checker.check(tmp_path)["passed"]


def test_launcher_creates_log_directory_before_python(tmp_path):
    import os
    import shutil
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows batch launcher")
    launcher = tmp_path / "jobhound_daily.cmd"
    shutil.copy(ROOT / "jobhound_daily.cmd", launcher)
    # No Python installation here: test only that shell redirection can open
    # the log before the interpreter would start. No scraping can occur.
    subprocess.run(["cmd", "/d", "/c", str(launcher)], cwd=tmp_path, capture_output=True)
    assert (tmp_path / "data/cron.log").is_file()


def test_metadata_hashes_effective_example_files(tmp_path, monkeypatch):
    from jobhound.v41 import engine
    monkeypatch.setattr(engine, "_ROOT", tmp_path)
    monkeypatch.setattr(engine.CONFIG.v55, "enabled", False)
    profile = tmp_path / "profile.example.yaml"
    config = tmp_path / "config.example.yaml"
    profile.write_text("languages: []", encoding="utf-8")
    config.write_text("digest: {channels: []}", encoding="utf-8")
    first = engine._metadata()
    profile.write_text("languages: [english]", encoding="utf-8")
    config.write_text("digest: {channels: [email]}", encoding="utf-8")
    second = engine._metadata()
    assert first.profile_hash != second.profile_hash
    assert first.config_hash != second.config_hash
    local = tmp_path / "profile.yaml"
    local.write_text("languages: [hindi]", encoding="utf-8")
    assert engine._metadata().profile_hash == engine._file_hash(local)
