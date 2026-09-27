"""Delivery outcomes describe provider acceptance, not inbox arrival or reading."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class DeliveryReceipt:
    outcome: str
    evidence: str
    retry_after: float = 0

    def __post_init__(self):
        if self.outcome not in {'accepted','not_sent','uncertain','permanent_failure'}:
            raise ValueError('invalid delivery outcome')
        if not self.evidence or len(self.evidence)>256 or not all(c.isalnum() or c in ':_-.|' for c in self.evidence):
            raise ValueError('opaque receipt code required; no raw provider errors')
        if type(self.retry_after) not in (int,float) or not math.isfinite(self.retry_after) or not 0<=self.retry_after<=86400:
            raise ValueError('invalid bounded retry delay')
