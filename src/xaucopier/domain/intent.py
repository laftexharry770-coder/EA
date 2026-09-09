"""What the planner emits. An intent is a decision, not an action."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.signal import Side


class OrderKind(Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    account_id: str
    symbol: str
    side: Side
    kind: OrderKind
    volume: float
    price: float
    sl: float
    tp: float
    leg_index: int
    magic: int
    comment: str
    deviation_points: int
    expires_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class ModifyIntent:
    account_id: str
    position_ticket: int
    sl: float
    tp: float
    leg_index: int


@dataclass(frozen=True, slots=True)
class CloseIntent:
    account_id: str
    position_ticket: int
    volume: float
    leg_index: int


@dataclass(frozen=True, slots=True)
class CancelIntent:
    """Remove a resting pending order. Distinct from CloseIntent: at the
    broker, closing a filled position is a deal, while removing a pending
    order is TRADE_ACTION_REMOVE — different operations, so a pending
    order's ticket is named order_ticket, not position_ticket."""
    account_id: str
    order_ticket: int
    leg_index: int


@dataclass(frozen=True, slots=True)
class Plan:
    accepted: bool
    reason: RejectReason | None
    legs: tuple[OrderIntent, ...]
    notes: tuple[str, ...] = ()

    @classmethod
    def rejected(cls, reason: RejectReason, notes: tuple[str, ...] = ()) -> "Plan":
        return cls(accepted=False, reason=reason, legs=(), notes=notes)

    @classmethod
    def accept(cls, legs: tuple[OrderIntent, ...], notes: tuple[str, ...] = ()) -> "Plan":
        return cls(accepted=True, reason=None, legs=legs, notes=notes)
