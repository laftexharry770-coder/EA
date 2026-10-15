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

# chase_gate is deliberately absent: it applies to the market path only, and
# is run below once the entry kind is known. Chasing is a market-order
# problem -- a limit fills at a fixed price however far price has run, so
# rejecting a signal on overshoot killed the pending path outright. On the
# real sample the budget is max(1.00, 0.25 x (4490 - 4485)) = 1.25 USD, a
# distance gold covers in seconds.
_SIGNAL_GATES = (
    gates.age_gate,
    gates.tick_freshness_gate,
    gates.spread_gate,
    gates.geometry_gate,
    gates.magnitude_gate,
    gates.invalidation_gate,
    gates.stops_distance_gate,
)


def distribute(
    n_positions: int | None,
    surviving: tuple[tuple[int, float], ...],
) -> tuple[tuple[int, float], ...]:
    """Spread N positions over the surviving take profits, one entry each.

    `surviving` is `(tp_index, tp)` in TP order, where tp_index is the
    index into `sig.tps`; the result has one `(tp_index, tp)` per position.
    The user's staggered rule (2026-09-23):

    - `None` (positions not set): one position per surviving TP, unchanged.
    - `N >= T`: one per TP, and the `N - T` extras on the LAST TP.
    - `N < T`: the FIRST N TPs.

    It is given the SURVIVING TPs, never all of them: a position aimed at a
    TP price has already passed would close at once or be rejected. With
    nothing surviving there is nothing to aim at, so there is no position
    (plan_entry rejects that case with ALL_TPS_BEHIND before it gets here).
    """
    if n_positions is None:
        return tuple(surviving)
    # The account loader already refuses these. AccountSpec does not, so
    # this is the last line of defence: zero would plan an accepted signal
    # with no orders, and a bool is an int that silently means 0 or 1.
    if isinstance(n_positions, bool) or not isinstance(n_positions, int) or n_positions < 1:
        raise ValueError(f"positions must be a whole number of at least 1, got {n_positions!r}")
    if not surviving:
        return ()
    if n_positions < len(surviving):
        return tuple(surviving[:n_positions])
    return tuple(surviving) + (surviving[-1],) * (n_positions - len(surviving))


def _comment(sig: Signal, leg_index: int, tp_index: int) -> str:
    """base36(message_id)-{leg}t{tp}, capped to the broker's 20-char limit.

    The suffix is mandatory. The leg index tells the legs of one signal
    apart at the broker -- without it reconciliation cannot say which
    position is which -- and the TP index names the take profit the
    position targets, so a position is self-describing even if its database
    row is lost. So the base36 prefix is what gets truncated when the two
    together would overflow 20 chars -- never the suffix, or a large enough
    message_id would silently drop the fields that must survive.
    """
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    n, out = sig.message_id, ""
    while True:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
        if n == 0:
            break
    suffix = f"-{leg_index}t{tp_index}"
    prefix_budget = max(0, 20 - len(suffix))
    return f"{out[:prefix_budget]}{suffix}"[:20]


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
    # in sig.tps, so tp_index (and the comment) names the real TP, not its
    # position in the filtered list.
    survived = set(surviving)
    surviving_indexed = tuple((i, tp) for i, tp in enumerate(sig.tps) if tp in survived)
    # One entry per position. leg_index is the position's ordinal (0..N-1),
    # unique per leg; several positions may share a tp_index.
    targets = distribute(account.positions, surviving_indexed)

    price = snap.ask if sig.side is Side.BUY else snap.bid
    kind, entry_price, expires = _entry_for(sig, snap, pol, price)

    # Chase only applies to a market fill, so it runs after the entry kind is
    # known. Be honest about the consequence: _entry_for picks MARKET only
    # when price is already at-or-better than the zone, which is exactly when
    # chase_gate's overshoot is <= 0, so as the rule stands today this gate
    # cannot reject anything. It is kept as defence in depth for a future
    # change to the entry-kind rule (e.g. a market fill some distance past
    # the zone), where it would immediately have teeth again.
    #
    # The limit path is not left unbounded: its staleness risk is carried by
    # pending_max_age_hours (the order expires) and by invalidation_gate
    # (SL breached, or every take profit already behind price).
    if kind is OrderKind.MARKET:
        chase = gates.chase_gate(sig, spec, snap, pol)
        if not chase.ok:
            return Plan.rejected(chase.reason, (chase.detail,))

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
            leg_index=leg_index,
            tp_index=tp_index,
            magic=account.magic_base,
            comment=_comment(sig, leg_index, tp_index),
            deviation_points=usd_to_points(pol.max_slippage_usd, spec),
            expires_at_utc=expires,
        )
        for leg_index, (tp_index, tp) in enumerate(targets)
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
