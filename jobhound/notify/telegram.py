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

log = logging.getLogger("jobhound.notify")

_MAX_LEN = 4000  # Telegram hard limit is 4096; leave headroom for the chunk prefix


def _chunk(text: str) -> list[str]:
    if len(text) <= _MAX_LEN:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > _MAX_LEN:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


class TelegramNotifier(Notifier):
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id

    @property
    def ready(self) -> bool:
        return bool(self.token and self.chat_id and self.chat_id != "0")

    async def send(self, text: str) -> bool:
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
