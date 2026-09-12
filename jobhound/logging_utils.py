"""Logging helpers that keep credentials out of console and cron output."""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"

_TELEGRAM_BOT_URL = re.compile(
    r"(?i)(https?://api\.telegram\.org/bot)[^/\s?#]+"
)
_SENSITIVE_QUERY_PARAM = re.compile(
    r"(?i)([?&](?:app_?id|app_?key|api_?key|key|token|access_?token|auth|authorization)=)"
    r"([^&#\s\"']+)"
)
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*(?:(?:bearer|basic)\s+)?)([^,;\s\"']+)"
)
_API_KEY_HEADER = re.compile(
    r"(?i)(\b(?:x-rapidapi-key|x-goog-api-key|x-api-key|api-key)\s*[:=]\s*)"
    r"([^,;\s\"']+)"
)


def _replace_value(match: re.Match[str]) -> str:
    return f"{match.group(1)}{REDACTED}"


def redact_sensitive(text: object, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with credential-shaped values replaced.

    Pattern-based redaction protects known URL/header formats. Exact configured
    secret replacement is the final safety net for exception bodies or future
    providers that use a different transport shape.
    """
    clean = str(text)
    clean = _TELEGRAM_BOT_URL.sub(rf"\1{REDACTED}", clean)
    clean = _SENSITIVE_QUERY_PARAM.sub(_replace_value, clean)
    clean = _AUTHORIZATION_HEADER.sub(_replace_value, clean)
    clean = _API_KEY_HEADER.sub(_replace_value, clean)

    variants: set[str] = set()
    for secret in secrets:
        if not secret or len(secret) < 6:
            continue
        variants.add(secret)
        variants.add(quote(secret, safe=""))
        variants.add(quote_plus(secret, safe=""))
    for secret in sorted(variants, key=len, reverse=True):
        clean = clean.replace(secret, REDACTED)
    return clean


class RedactingFormatter(logging.Formatter):
    """Format first, then redact the complete message and traceback."""

    def __init__(self, *args, secrets: Iterable[str] = (), **kwargs):
        super().__init__(*args, **kwargs)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive(super().format(record), self._secrets)
