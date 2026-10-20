"""Turn a follow-up command into intents against the legs that actually exist.

LegView is read from the broker, never from our own database — the DB can be
stale, the broker cannot. Ambiguity always resolves to doing nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.gates import min_stop_distance_usd
from xaucopier.domain.intent import CancelIntent, CloseIntent, ModifyIntent
from xaucopier.domain.market import MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price
from xaucopier.domain.signal import FollowUp, FollowUpOp, Side


@dataclass(frozen=True, slots=True)
class LegView:
    """Live broker truth for one leg.

    `leg_index` is the position's own number within its plan, unique per
    leg. `tp_index` is the index into `sig.tps` of the take profit it
    targets; two legs may share it (see `OrderIntent`). Both are in the
    broker comment `{base36}-{leg_index}t{tp_index}`, so whoever builds a
    LegView from the broker reads the `t` part as well: a TP amend matches
    on `tp_index`, never on `leg_index`. It has no default on purpose, so
    a producer cannot forget it.
    """
    position_ticket: int
    leg_index: int
    tp_index: int
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    is_pending: bool


@dataclass(frozen=True, slots=True)
class FollowUpPlan:
    # CLOSE_ALL is the one op that mixes kinds: open legs close as
    # CloseIntent, pending legs cancel as CancelIntent. Every other op
    # emits a single-type tuple, a subset of this same union.
    intents: tuple[ModifyIntent | CloseIntent | CancelIntent, ...]
    reason: RejectReason | None = None
    notes: tuple[str, ...] = ()


def _breakeven_price(leg: LegView, snap: MarketSnapshot, pol: Policy, spec: SymbolSpec) -> float:
    """Breakeven is the leg's OWN fill plus costs, never the signal's quoted
    entry — each broker filled somewhere different."""
    # be_buffer_usd is expected to absorb round-turn commission until Plan 3
    # models commission properly from broker-supplied per-lot/contract data;
    # see the "explicitly out of scope" note in the task-9 review.
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
        if not open_legs:
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("nothing open for breakeven",))
        min_dist = min_stop_distance_usd(spec, snap, pol)
        intents = []
        skipped_wrong_side = 0
        skipped_too_tight = 0
        for leg in open_legs:
            be = _breakeven_price(leg, snap, pol, spec)
            price = snap.bid if leg.side is Side.BUY else snap.ask
            # Refuse a stop that is already on the wrong side of the market.
            if leg.side is Side.BUY and be >= price:
                skipped_wrong_side += 1
                continue
            if leg.side is Side.SELL and be <= price:
                skipped_wrong_side += 1
                continue
            # Refuse a stop the broker will reject as too close to price.
            if abs(price - be) < min_dist:
                skipped_too_tight += 1
                continue
            intents.append(ModifyIntent(account_id="", position_ticket=leg.position_ticket,
                                        sl=be, tp=leg.tp, leg_index=leg.leg_index))
        if not intents:
            total = skipped_wrong_side + skipped_too_tight
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, (
                f"{total} leg(s) skipped for breakeven "
                f"({skipped_wrong_side} wrong side, {skipped_too_tight} too tight)",
            ))
        return FollowUpPlan(tuple(intents))

    if fu.op is FollowUpOp.CLOSE_HALF:
        if not open_legs:
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("nothing open",))
        count = math.ceil(len(open_legs) / 2)
        # Furthest take profits first: keep the legs nearest to paying out.
        # "Furthest" is direction-dependent -- the highest tp for a buy, the
        # LOWEST for a sell. Multiplying by side.sign folds both into one
        # descending sort; sorting on the raw tp would close a sell's two
        # nearest legs and keep the long-shots (spec 8.2, inverted).
        ordered = sorted(open_legs, key=lambda leg: leg.tp * leg.side.sign, reverse=True)
        chosen = ordered[:count]
        return FollowUpPlan(tuple(
            CloseIntent(account_id="", position_ticket=leg.position_ticket,
                        volume=leg.volume, leg_index=leg.leg_index)
            for leg in chosen
        ))

    if fu.op is FollowUpOp.CLOSE_ALL:
        # Open legs close as positions; pending legs cancel as orders — the
        # broker needs a different operation for each (Item 1).
        close_intents = tuple(
            CloseIntent(account_id="", position_ticket=leg.position_ticket,
                        volume=leg.volume, leg_index=leg.leg_index)
            for leg in open_legs
        )
        cancel_intents = tuple(
            CancelIntent(account_id="", order_ticket=leg.position_ticket, leg_index=leg.leg_index)
            for leg in pending_legs
        )
        return FollowUpPlan(close_intents + cancel_intents)

    if fu.op is FollowUpOp.CANCEL_PENDING:
        if not pending_legs:
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("nothing pending",))
        return FollowUpPlan(tuple(
            CancelIntent(account_id="", order_ticket=leg.position_ticket, leg_index=leg.leg_index)
            for leg in pending_legs
        ))

    if fu.op in (FollowUpOp.AMEND_SL, FollowUpOp.AMEND_TP):
        if fu.price_hint is None:
            return FollowUpPlan((), RejectReason.AMBIGUOUS_TARGET, ("amend without a price",))
        if not open_legs:
            if pending_legs:
                # A distinct "modify pending order" intent belongs to Plan 3
                # (a different broker operation from modifying a position);
                # out of scope here. Until it exists, the honest move is to
                # refuse loudly rather than silently do nothing -- these
                # orders will fill carrying a stop the poster explicitly
                # moved, a directly wrong dollar risk under fixed-lot sizing.
                return FollowUpPlan((), RejectReason.PENDING_ONLY, (
                    f"{len(pending_legs)} pending leg(s) still carry the stale "
                    "stop -- amend the pending order(s) at the broker directly",
                ))
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("nothing open to amend",))
        # An SL amend reaches every open leg: one signal has one shared stop.
        # A TP amend does not -- each leg owns its own target, so the poster
        # must say which one. "TP2 to 4550" names tp_index 1 (the hint is
        # 1-based, tp_index is 0-based into sig.tps) and moves EVERY open
        # position on that TP: with more positions than TPs the extras share
        # the last one. It never names leg_index, which is only the
        # position's ordinal: once TP1 has passed at entry, ordinal 0 targets
        # TP2. A bare "tp to 4550" names nothing, and writing one price
        # across a four-leg ladder is never what was meant, so it fails
        # loudly instead.
        targets = open_legs
        if fu.op is FollowUpOp.AMEND_TP:
            if fu.tp_index_hint is None:
                return FollowUpPlan((), RejectReason.AMBIGUOUS_TARGET, (
                    "tp amend without an index: one price across a ladder "
                    "flattens every target",
                ))
            wanted = fu.tp_index_hint - 1
            targets = tuple(leg for leg in open_legs if leg.tp_index == wanted)
            if not targets:
                return FollowUpPlan((), RejectReason.AMBIGUOUS_TARGET, (
                    f"no open leg for TP{fu.tp_index_hint} (tp_index {wanted})",
                ))

        new = round_price(fu.price_hint, spec)
        min_dist = min_stop_distance_usd(spec, snap, pol)
        intents = []
        for leg in targets:
            if fu.op is FollowUpOp.AMEND_SL:
                # Exit-side reference: an SL for a BUY closes at bid, a SELL
                # at ask.
                price = snap.bid if leg.side is Side.BUY else snap.ask
                if leg.side is Side.BUY and new >= price:
                    return FollowUpPlan((), RejectReason.GEOMETRY_CONFLICT,
                                        (f"amended buy stop {new} is at/above price {price}",))
                if leg.side is Side.SELL and new <= price:
                    return FollowUpPlan((), RejectReason.GEOMETRY_CONFLICT,
                                        (f"amended sell stop {new} is at/below price {price}",))
                dist = abs(price - new)
                if dist < min_dist:
                    return FollowUpPlan((), RejectReason.STOPS_TOO_TIGHT,
                                        (f"amended stop {dist:.2f} from price, min {min_dist:.2f}",))
                intents.append(ModifyIntent("", leg.position_ticket, new, leg.tp, leg.leg_index))
            else:
                # Entry-side reference: a TP is a target price must still
                # travel to reach, same side as where a fresh entry fills.
                price = snap.ask if leg.side is Side.BUY else snap.bid
                if leg.side is Side.BUY and (new <= price or new <= leg.sl):
                    return FollowUpPlan((), RejectReason.GEOMETRY_CONFLICT,
                                        (f"amended buy tp {new} is not above "
                                         f"price {price} and sl {leg.sl}",))
                if leg.side is Side.SELL and (new >= price or new >= leg.sl):
                    return FollowUpPlan((), RejectReason.GEOMETRY_CONFLICT,
                                        (f"amended sell tp {new} is not below "
                                         f"price {price} and sl {leg.sl}",))
                intents.append(ModifyIntent("", leg.position_ticket, leg.sl, new, leg.leg_index))
        return FollowUpPlan(tuple(intents))

    return FollowUpPlan((), RejectReason.NOT_APPLICABLE, (f"unhandled op {fu.op}",))
