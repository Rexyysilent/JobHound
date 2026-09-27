"""Email digest delivery (Gmail SMTP) — the channel that actually works here:
api.telegram.org is ISP-blocked on this network, smtp.gmail.com is not.

Needs EMAIL_SMTP_USER + EMAIL_APP_PASSWORD in .env (a Gmail *app password*,
not the account password — requires 2-Step Verification, generated at
myaccount.google.com/apppasswords). No-ops with a warning when unset, same as
telegram. Sent as plain text; the digest is already terminal-shaped.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import hashlib
from email.message import EmailMessage
from email.headerregistry import Address

from ..settings import settings
from .base import Notifier
from .receipt import DeliveryReceipt

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

    @property
    def destination(self):
        from ..delivery_outbox import Destination
        from ..review_audit import canonical_json
        for address in (self.user, self.to):
            if not address or str(Address(addr_spec=address)) != address or '@' not in address:
                raise ValueError('one explicit mailbox address required')
        return Destination.from_address('email', canonical_json([_HOST, _PORT, self.user, self.to]))

    def _send_receipt_sync(self, text: str, delivery_key: str) -> DeliveryReceipt:
        self.destination
        key = hashlib.sha256(delivery_key.encode()).hexdigest()
        msg = build_message(text, self.user, self.to)
        msg['Message-ID'] = f'<jobhound.{key}@jobhound.local>'
        smtp = None
        sending = False
        try:
            smtp = smtplib.SMTP_SSL(
                _HOST, _PORT, timeout=30, context=ssl.create_default_context()
            )
            smtp.login(self.user, self.password)
            sending = True
            refused = smtp.send_message(msg, from_addr=self.user, to_addrs=[self.to])
            if refused:
                codes = [value[0] for value in refused.values()]
                outcome = ('not_sent' if codes and all(400 <= code < 500 for code in codes)
                           else 'permanent_failure')
                return DeliveryReceipt(outcome, 'smtp_recipient_refused')
            return DeliveryReceipt('accepted', 'smtp_accepted:' + key)
        except smtplib.SMTPAuthenticationError:
            return DeliveryReceipt('permanent_failure', 'smtp_auth_rejected')
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [value[0] for value in exc.recipients.values()]
            outcome = ('not_sent' if codes and all(400 <= code < 500 for code in codes)
                       else 'permanent_failure')
            return DeliveryReceipt(outcome, 'smtp_recipient_refused')
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            outcome = 'not_sent' if 400 <= exc.smtp_code < 500 else 'permanent_failure'
            return DeliveryReceipt(outcome, 'smtp_explicit_rejection')
        except (smtplib.SMTPException, OSError):
            return DeliveryReceipt(
                'uncertain' if sending else 'not_sent',
                'smtp_ack_unknown' if sending else 'smtp_pre_send_failure',
            )
        finally:
            if smtp is not None:
                try:
                    smtp.close()
                except Exception:
                    pass

    async def send_receipt(self, text: str, *, delivery_key: str) -> DeliveryReceipt:
        from ..run_context import deny_review_side_effect
        deny_review_side_effect('email delivery')
        if not self.ready:
            return DeliveryReceipt('permanent_failure', 'email_not_configured')
        return await asyncio.to_thread(self._send_receipt_sync, text, delivery_key)

    def _send_sync(self, msg: EmailMessage) -> None:
        with smtplib.SMTP_SSL(_HOST, _PORT, timeout=30) as smtp:
            smtp.login(self.user, self.password)
            smtp.send_message(msg)

    async def send(self, text: str) -> bool:
        from ..run_context import deny_review_side_effect
        deny_review_side_effect('email delivery')
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
