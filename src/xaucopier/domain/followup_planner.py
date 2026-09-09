"""Turn a follow-up command into intents against the legs that actually exist.

LegView is read from the broker, never from our own database — the DB can be
stale, the broker cannot. Ambiguity always resolves to doing nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.intent import CloseIntent, ModifyIntent
from xaucopier.domain.market import MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price
from xaucopier.domain.signal import FollowUp, FollowUpOp, Side


@dataclass(frozen=True, slots=True)
class LegView:
    """Live broker truth for one leg."""
    position_ticket: int
    leg_index: int
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    is_pending: bool


@dataclass(frozen=True, slots=True)
class FollowUpPlan:
    intents: tuple
    reason: RejectReason | None = None
    notes: tuple[str, ...] = ()


def _breakeven_price(leg: LegView, snap: MarketSnapshot, pol: Policy, spec: SymbolSpec) -> float:
    """Breakeven is the leg's OWN fill plus costs, never the signal's quoted
    entry — each broker filled somewhere different."""
    offset = snap.spread_usd + pol.be_buffer_usd
    raw = leg.price_open + offset if leg.side is Side.BUY else leg.price_open - offset
    return round_price(raw, spec)


def plan_followup(
    fu: FollowUp,
    legs: tuple[LegView, ...],
    spec: SymbolSpec,
    snap: MarketSnapshot,
    pol: Policy,
) -> FollowUpPlan:
    open_legs = tuple(leg for leg in legs if not leg.is_pending)
    pending_legs = tuple(leg for leg in legs if leg.is_pending)

    if not legs:
        return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("no legs for this signal",))

    if fu.op is FollowUpOp.SL_TO_BE:
        intents = []
        for leg in open_legs:
            be = _breakeven_price(leg, snap, pol, spec)
            price = snap.bid if leg.side is Side.BUY else snap.ask
            # Refuse a stop that is already on the wrong side of the market.
            if leg.side is Side.BUY and be >= price:
                continue
            if leg.side is Side.SELL and be <= price:
                continue
            intents.append(ModifyIntent(account_id="", position_ticket=leg.position_ticket,
                                        sl=be, tp=leg.tp, leg_index=leg.leg_index))
        return FollowUpPlan(tuple(intents))

    if fu.op is FollowUpOp.CLOSE_HALF:
        if not open_legs:
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("nothing open",))
        count = math.ceil(len(open_legs) / 2)
        # Furthest take profits first: keep the legs nearest to paying out.
        ordered = sorted(open_legs, key=lambda leg: leg.tp, reverse=True)
        chosen = ordered[:count]
        return FollowUpPlan(tuple(
            CloseIntent(account_id="", position_ticket=leg.position_ticket,
                        volume=leg.volume, leg_index=leg.leg_index)
            for leg in chosen
        ))

    if fu.op is FollowUpOp.CLOSE_ALL:
        return FollowUpPlan(tuple(
            CloseIntent(account_id="", position_ticket=leg.position_ticket,
                        volume=leg.volume, leg_index=leg.leg_index)
            for leg in legs
        ))

    if fu.op is FollowUpOp.CANCEL_PENDING:
        if not pending_legs:
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("nothing pending",))
        return FollowUpPlan(tuple(
            CloseIntent(account_id="", position_ticket=leg.position_ticket,
                        volume=leg.volume, leg_index=leg.leg_index)
            for leg in pending_legs
        ))

    if fu.op in (FollowUpOp.AMEND_SL, FollowUpOp.AMEND_TP):
        if fu.price_hint is None:
            return FollowUpPlan((), RejectReason.AMBIGUOUS_TARGET, ("amend without a price",))
        new = round_price(fu.price_hint, spec)
        price = snap.bid if open_legs and open_legs[0].side is Side.BUY else snap.ask
        intents = []
        for leg in open_legs:
            if fu.op is FollowUpOp.AMEND_SL:
                if leg.side is Side.BUY and new >= price:
                    return FollowUpPlan((), RejectReason.GEOMETRY_CONFLICT,
                                        (f"amended buy stop {new} is at/above price {price}",))
                if leg.side is Side.SELL and new <= price:
                    return FollowUpPlan((), RejectReason.GEOMETRY_CONFLICT,
                                        (f"amended sell stop {new} is at/below price {price}",))
                intents.append(ModifyIntent("", leg.position_ticket, new, leg.tp, leg.leg_index))
            else:
                intents.append(ModifyIntent("", leg.position_ticket, leg.sl, new, leg.leg_index))
        return FollowUpPlan(tuple(intents))

    return FollowUpPlan((), RejectReason.NOT_APPLICABLE, (f"unhandled op {fu.op}",))
