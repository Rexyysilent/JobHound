"""Notifier interface — lets a WhatsApp channel be swapped in later (HANDOFF §11)
without touching the pipeline."""
from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    @abstractmethod
    async def send(self, text: str) -> bool:
        """Deliver the digest text. Return True on success."""
