"""The IMAP sweep must verify the mail server's certificate and hostname.

imaplib.IMAP4_SSL without ssl_context falls back to an unverified context, so
anyone on the network path could impersonate the server and collect the
mailbox app password on login.
"""
from __future__ import annotations

import ssl

import jobhound.store
from jobhound.sources import email_offers


class _FakeStore:
    def close(self):
        pass


def _capture_imap(monkeypatch):
    seen = {}

    class FakeIMAP:
        def __init__(self, *args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, *_):
            seen["login_after_connect"] = True

        def select(self, *_args, **_kwargs):
            return "NO", [b""]

    monkeypatch.setattr(email_offers.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(jobhound.store, "Store", _FakeStore)
    return seen


def test_imap_connection_verifies_certificate_and_hostname(monkeypatch):
    seen = _capture_imap(monkeypatch)

    email_offers.EmailOffersSource()._fetch_sync()

    context = seen["kwargs"].get("ssl_context")
    assert isinstance(context, ssl.SSLContext), "IMAP4_SSL got no ssl_context (unverified default)"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert seen["args"][0] == email_offers.CONFIG.email_ingest.host
