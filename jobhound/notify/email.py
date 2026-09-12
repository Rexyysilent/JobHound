"""Optional email digest delivery (Gmail SMTP).
SMTP delivery is an optional alternative to Telegram.

Needs EMAIL_SMTP_USER + EMAIL_APP_PASSWORD in .env (a Gmail *app password*,
not the account password — requires 2-Step Verification, generated at
myaccount.google.com/apppasswords). No-ops with a warning when unset, same as
telegram. Sent as plain text; the digest is already terminal-shaped.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from ..settings import settings
from .base import Notifier

log = logging.getLogger("jobhound.notify")

_HOST = "smtp.gmail.com"
_PORT = 465  # implicit TLS; STARTTLS on 587 is the fallback if 465 is filtered


def build_message(text: str, sender: str, to: str) -> EmailMessage:
    """First digest line becomes the subject; the whole text is the body."""
    msg = EmailMessage()
    first_line = text.strip().split("\n", 1)[0]
    msg["Subject"] = first_line[:120] or "jobhound digest"
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(text)
    return msg


class EmailNotifier(Notifier):
    def __init__(self, user: str | None = None, password: str | None = None,
                 to: str | None = None):
        self.user = user or settings.email_smtp_user
        self.password = password or settings.email_app_password
        self.to = to or settings.email_to or self.user

    @property
    def ready(self) -> bool:
        return bool(self.user and self.password)

    def _send_sync(self, msg: EmailMessage) -> None:
        with smtplib.SMTP_SSL(_HOST, _PORT, timeout=30) as smtp:
            smtp.login(self.user, self.password)
            smtp.send_message(msg)

    async def send(self, text: str) -> bool:
        if not self.ready:
            log.warning("email not configured (EMAIL_SMTP_USER/EMAIL_APP_PASSWORD) — skipping send")
            return False
        msg = build_message(text, sender=self.user, to=self.to)
        try:
            await asyncio.to_thread(self._send_sync, msg)
        except (smtplib.SMTPException, OSError) as e:
            # Same contract as telegram: delivery failure never crashes the run —
            # the digest file is already on disk.
            log.error("email send failed: %s", e)
            return False
        log.info("email digest sent to %s", self.to)
        return True
