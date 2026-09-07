# src/xaucopier/domain/planner.py
"""Turn one Signal into the set of orders that should exist.

The entire money-losing surface of the system is this function. It is pure,
so a year of recorded messages replays through it offline.
"""
from __future__ import annotations

from datetime import timedelta

from xaucopier.domain import gates
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.intent import OrderIntent, OrderKind, Plan
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price, usd_to_points
from xaucopier.domain.signal import Side, Signal

_SIGNAL_GATES = (
    gates.age_gate,
    gates.tick_freshness_gate,
    gates.spread_gate,
    gates.geometry_gate,
    gates.magnitude_gate,
    gates.invalidation_gate,
    gates.chase_gate,
    gates.stops_distance_gate,
)


def _comment(sig: Signal, leg_index: int) -> str:
    """base36(message_id)-leg. The leg index is mandatory: without it the
    legs of one signal are indistinguishable at the broker, and reconciliation
    cannot tell which position is which."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    n, out = sig.message_id, ""
    while True:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
        if n == 0:
            break
    return f"{out}-{leg_index}"[:20]


def plan_entry(
    sig: Signal,
    account: AccountSpec,
    spec: SymbolSpec,
    snap: MarketSnapshot,
    pol: Policy,
) -> Plan:
    volume = gates.volume_gate(account, spec)
    if not volume.ok:
        return Plan.rejected(volume.reason, (volume.detail,))

    for gate in _SIGNAL_GATES:
        result = gate(sig, spec, snap, pol)
        if not result.ok:
            return Plan.rejected(result.reason, (result.detail,))

    surviving = gates.surviving_tps(sig, snap)
    if not surviving:
        return Plan.rejected(RejectReason.ALL_TPS_BEHIND, ("no take profit ahead of price",))

    # gates.surviving_tps is the single source of truth for which targets
    # survive; here we only re-pair each survivor with its original position
    # in sig.tps, so leg_index (and the comment) names the real TP, not its
    # position in the filtered list.
    survived = set(surviving)
    surviving_indexed = tuple((i, tp) for i, tp in enumerate(sig.tps) if tp in survived)

    price = snap.ask if sig.side is Side.BUY else snap.bid
    kind, entry_price, expires = _entry_for(sig, snap, pol, price)

    legs = tuple(
        OrderIntent(
            account_id=account.account_id,
            symbol=account.symbol,
            side=sig.side,
            kind=kind,
            volume=account.fixed_lot,
            price=round_price(entry_price, spec),
            sl=round_price(sig.sl, spec),
            tp=round_price(tp, spec),
            leg_index=index,
            magic=account.magic_base,
            comment=_comment(sig, index),
            deviation_points=usd_to_points(pol.max_slippage_usd, spec),
            expires_at_utc=expires,
        )
        for index, tp in surviving_indexed
    )
    notes = (f"{len(sig.tps) - len(surviving_indexed)} take profit(s) already passed",) \
        if len(surviving_indexed) != len(sig.tps) else ()
    return Plan.accept(legs, notes)


def _entry_for(sig: Signal, snap: MarketSnapshot, pol: Policy, price: float):
    """Market inside or better than the zone; a limit otherwise.

    Where you fill inside the zone materially changes R: on the observed
    4480-4485 / SL 4470 signal, filling at 4480 makes TP1 a 1.0R target and
    filling at 4485 makes it 0.33R. The default places the limit at the
    favourable edge, trading fill rate for R.
    """
    inside_or_better = (
        price <= sig.zone.hi if sig.side is Side.BUY else price >= sig.zone.lo
    )
    if inside_or_better:
        return OrderKind.MARKET, price, None
    edge = (sig.zone.favourable_edge(sig.side)
            if pol.pending_at == "favourable"
            else sig.zone.near_edge(sig.side))
    expires = sig.posted_at_utc + timedelta(hours=pol.pending_max_age_hours)
    return OrderKind.LIMIT, edge, expires
