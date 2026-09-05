"""The parsed vocabulary of a channel message. Frozen, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL. Lets geometry checks stay side-agnostic."""
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True, slots=True)
class EntryZone:
    lo: float
    hi: float

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            lo, hi = self.hi, self.lo
            object.__setattr__(self, "lo", lo)
            object.__setattr__(self, "hi", hi)

    def favourable_edge(self, side: Side) -> float:
        """The edge that gives the better fill: low for a buy, high for a sell."""
        return self.lo if side is Side.BUY else self.hi

    def near_edge(self, side: Side) -> float:
        return self.hi if side is Side.BUY else self.lo

    def contains(self, price: float) -> bool:
        return self.lo <= price <= self.hi


class FollowUpOp(Enum):
    SL_TO_BE = "sl_to_be"
    CLOSE_HALF = "close_half"
    CLOSE_ALL = "close_all"
    CANCEL_PENDING = "cancel_pending"
    AMEND_SL = "amend_sl"
    AMEND_TP = "amend_tp"


@dataclass(frozen=True, slots=True)
class Signal:
    channel_id: int
    message_id: int
    side: Side
    symbol_alias: str
    zone: EntryZone
    sl: float
    tps: tuple[float, ...]
    posted_at_utc: datetime
    content_hash: str
    source: str = "live"

    def __post_init__(self) -> None:
        if self.posted_at_utc.tzinfo is None:
            raise ValueError("posted_at_utc must be timezone-aware UTC")
        if not self.tps:
            raise ValueError("a signal needs at least one take profit")


@dataclass(frozen=True, slots=True)
class FollowUp:
    channel_id: int
    message_id: int
    op: FollowUpOp
    posted_at_utc: datetime
    reply_to_message_id: int | None = None
    price_hint: float | None = None
    tp_index_hint: int | None = None
    raw: str = ""

    def __post_init__(self) -> None:
        if self.posted_at_utc.tzinfo is None:
            raise ValueError("posted_at_utc must be timezone-aware UTC")
