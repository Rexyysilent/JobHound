"""Push-only Telegram delivery. jobhound never polls, so it only calls
sendMessage — safe to share a bot token with an interactive bot. Sent as plain
text (no parse_mode) so digest punctuation never trips Markdown escaping; URLs
auto-link in Telegram clients anyway. Chunking ported from telegram-ai-bot.
"""
from __future__ import annotations

import logging

import httpx

from ..settings import settings
from .base import Notifier
from .receipt import DeliveryReceipt

log = logging.getLogger("jobhound.notify")

_MAX_LEN = 4000  # Telegram hard limit is 4096; leave headroom for the chunk prefix


def _chunk(text: str) -> list[str]:
    chunks, current, units = [], [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > _MAX_LEN:
            chunks.append(''.join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current or not chunks:
        chunks.append(''.join(current))
    return chunks


class TelegramNotifier(Notifier):
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id

    @property
    def ready(self) -> bool:
        return bool(self.token and self.chat_id and self.chat_id != "0")

    @property
    def destination(self):
        from ..delivery_outbox import Destination
        from ..review_audit import canonical_json
        bot_id, separator, secret = self.token.partition(':')
        if not separator or not bot_id.isdigit() or not secret or not self.ready:
            raise ValueError('configured bot identity and chat required')
        return Destination.from_address('telegram', canonical_json([bot_id, self.chat_id]))

    async def send_receipt(self, text: str, *, delivery_key: str) -> DeliveryReceipt:
        from ..run_context import deny_review_side_effect
        deny_review_side_effect('telegram delivery')
        if not self.ready:
            return DeliveryReceipt('permanent_failure', 'telegram_not_configured')
        self.destination
        if len(text.encode('utf-16-le')) // 2 > 4096:
            return DeliveryReceipt('permanent_failure', 'telegram_chunk_too_large')
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.post(
                    f'https://api.telegram.org/bot{self.token}/sendMessage',
                    json={'chat_id': self.chat_id, 'text': text,
                          'disable_web_page_preview': True},
                )
            try:
                body = response.json()
            except (ValueError, TypeError):
                body = {}
            if not isinstance(body, dict):
                body = {}
            if response.status_code == 200 and body.get('ok') is True:
                result = body.get('result')
                message_id = result.get('message_id') if isinstance(result, dict) else None
                if type(message_id) is int and message_id > 0:
                    return DeliveryReceipt('accepted', f'telegram_message:{message_id}')
                return DeliveryReceipt('uncertain', 'telegram_receipt_missing')
            if 400 <= response.status_code < 500 and body.get('ok') is False:
                code = body.get('error_code')
                if type(code) is int and code == 429 and response.status_code == 429:
                    parameters = body.get('parameters')
                    delay = parameters.get('retry_after', 60) if isinstance(parameters, dict) else 60
                    if type(delay) not in (int, float) or not 0 <= delay <= 86400:
                        delay = 86400
                    return DeliveryReceipt('not_sent', 'telegram_rate_limited', delay)
                if type(code) is int and code == response.status_code:
                    return DeliveryReceipt('permanent_failure', 'telegram_explicit_rejection')
            return DeliveryReceipt('uncertain', 'telegram_ack_unknown')
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return DeliveryReceipt('not_sent', 'telegram_pre_send_failure')
        except httpx.HTTPError:
            return DeliveryReceipt('uncertain', 'telegram_transport_unknown')

    async def send(self, text: str) -> bool:
        from ..run_context import deny_review_side_effect
        deny_review_side_effect('telegram delivery')
        if not self.ready:
            log.warning("telegram not configured (token/chat_id missing) — skipping send")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        chunks = _chunk(text)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                for i, chunk in enumerate(chunks):
                    prefix = f"({i + 1}/{len(chunks)})\n" if len(chunks) > 1 else ""
                    resp = await client.post(url, json={
                        "chat_id": self.chat_id,
                        "text": prefix + chunk,
                        "disable_web_page_preview": True,
                    })
                    if resp.status_code != 200:
                        log.error("telegram send failed (%s): %s", resp.status_code, resp.text[:200])
                        return False
        except httpx.HTTPError as e:
            # Network failure (e.g. Telegram blocked on this network) must not crash
            # the run — the digest file is already written either way.
            log.error("telegram unreachable: %s", e)
            return False
        log.info("telegram digest sent (%d chunk(s))", len(chunks))
        return True
