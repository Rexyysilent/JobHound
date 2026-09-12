"""Email notifier: message shape, readiness gating, and channel config."""
import asyncio

import pytest

from jobhound.config import CONFIG
from jobhound.notify.email import EmailNotifier, build_message
from jobhound.settings import settings


@pytest.fixture
def blank_email_env(monkeypatch):
    """Constructor args fall back to settings — pin them empty so these tests
    behave the same once real creds land in .env."""
    monkeypatch.setattr(settings, "email_smtp_user", "")
    monkeypatch.setattr(settings, "email_app_password", "")
    monkeypatch.setattr(settings, "email_to", "")


def test_build_message_subject_is_first_digest_line():
    text = "🐕 jobhound digest — 2026-07-06\nraw 995 → deduped 861\n• job one"
    msg = build_message(text, sender="sender@example.com", to="recipient@example.com")
    assert msg["Subject"] == "🐕 jobhound digest — 2026-07-06"
    assert msg["From"] == "sender@example.com" and msg["To"] == "recipient@example.com"
    assert "job one" in msg.get_content()


def test_build_message_subject_is_bounded():
    msg = build_message("x" * 500, sender="a@x", to="a@x")
    assert len(msg["Subject"]) == 120


def test_not_ready_without_credentials(blank_email_env):
    n = EmailNotifier()
    assert not n.ready
    assert asyncio.run(n.send("hello")) is False   # no-op, no network


def test_to_defaults_to_sender(blank_email_env):
    n = EmailNotifier(user="sender@example.com", password="pw")
    assert n.ready and n.to == "sender@example.com"


def test_email_is_a_configured_channel():
    assert set(CONFIG.digest.channels) == {"telegram", "email"}
