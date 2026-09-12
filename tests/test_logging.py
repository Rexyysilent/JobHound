"""Security regressions for console/cron credential redaction."""
from __future__ import annotations

import logging
import sys

from jobhound import cli
from jobhound.logging_utils import REDACTED, RedactingFormatter, redact_sensitive


def _record(message: str, *, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="jobhound.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )


def test_telegram_token_is_redacted_from_exception_url():
    token = "123456789:AAFakeTelegramToken_123456"
    message = (
        "request failed for "
        f"https://api.telegram.org/bot{token}/sendMessage"
    )

    rendered = RedactingFormatter("%(message)s").format(_record(message))

    assert token not in rendered
    assert f"https://api.telegram.org/bot{REDACTED}/sendMessage" in rendered


def test_adzuna_credentials_are_redacted_but_safe_query_is_preserved():
    url = (
        "https://api.adzuna.com/v1/api/jobs/in/search/1"
        "?app_id=fake-app-id&app_key=fake-app-key"
        "&results_per_page=50&what=ai+training"
    )

    rendered = redact_sensitive(url)

    assert "fake-app-id" not in rendered
    assert "fake-app-key" not in rendered
    assert f"app_id={REDACTED}" in rendered
    assert f"app_key={REDACTED}" in rendered
    assert "results_per_page=50" in rendered
    assert "what=ai+training" in rendered


def test_generic_query_and_header_credentials_are_redacted():
    message = (
        "https://example.test/run?api_key=query-secret&access_token=access-secret "
        "Authorization: Bearer bearer-secret "
        "X-RapidAPI-Key: header-secret"
    )

    rendered = redact_sensitive(message)

    for secret in ("query-secret", "access-secret", "bearer-secret", "header-secret"):
        assert secret not in rendered
    assert rendered.count(REDACTED) == 4


def test_exact_secret_and_traceback_text_are_redacted():
    secret = "provider-secret-value"
    formatter = RedactingFormatter("%(levelname)s: %(message)s", secrets=[secret])
    try:
        raise RuntimeError(f"provider rejected {secret}")
    except RuntimeError:
        rendered = formatter.format(_record("delivery failed", exc_info=sys.exc_info()))

    assert secret not in rendered
    assert REDACTED in rendered
    assert "RuntimeError" in rendered


def test_harmless_messages_and_query_parameters_are_unchanged():
    message = (
        "source remotive: 42 raw items; "
        "https://example.test/jobs?what=python&results_per_page=50"
    )
    assert redact_sensitive(message) == message


def test_logging_setup_is_quiet_for_http_clients_and_keeps_jobhound_info(
    monkeypatch,
):
    configured: dict = {}
    levels: dict[str, int] = {}

    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: configured.update(kwargs))
    for name in ("httpx", "httpcore", "jobhound"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(
            logger,
            "setLevel",
            lambda level, logger_name=name: levels.__setitem__(logger_name, level),
        )
    for setting_name in (
        "telegram_bot_token",
        "email_app_password",
        "imap_app_password",
        "adzuna_app_id",
        "adzuna_app_key",
        "rapidapi_key",
        "serper_api_key",
        "gemini_api_key",
    ):
        monkeypatch.setattr(cli.settings, setting_name, "")
    monkeypatch.setattr(cli.settings, "telegram_bot_token", "fake-telegram-secret")
    monkeypatch.setattr(cli.settings, "adzuna_app_key", "fake-adzuna-secret")

    cli._setup_logging()

    assert configured["level"] == logging.INFO
    assert configured["force"] is True
    assert len(configured["handlers"]) == 1
    formatter = configured["handlers"][0].formatter
    assert isinstance(formatter, RedactingFormatter)
    rendered = formatter.format(_record(
        "fake-telegram-secret and fake-adzuna-secret"
    ))
    assert "fake-telegram-secret" not in rendered
    assert "fake-adzuna-secret" not in rendered
    assert levels == {
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
        "jobhound": logging.INFO,
    }
