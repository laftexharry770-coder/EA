# src/xaucopier/domain/planner.py
"""Turn one Signal into the set of orders that should exist.

The entire money-losing surface of the system is this function. It is pure,
so a year of recorded messages replays through it offline.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from xaucopier.domain import gates
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.intent import OrderIntent, OrderKind, Plan
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price, usd_to_points
from xaucopier.domain.signal import EntryZone, Side, Signal

# chase_gate is deliberately absent: it applies to the market path only, and
# is run below once the entry kind is known. Chasing is a market-order
# problem -- a limit fills at a fixed price however far price has run, so
# rejecting a signal on overshoot killed the pending path outright. On the
# real sample the budget is max(1.00, 0.25 x (4490 - 4485)) = 1.25 USD, a
# distance gold covers in seconds. Under layered entry it never rejects:
# it only decides whether the NOW positions go at market or as limits.
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
    Position i targets `surviving[i mod T]`, like the "Split over enabled
    TPs" of the user's EA (amendment 2026-10-21, which replaced "extras on
    the last TP"):

    - `None` (positions not set): one position per surviving TP, unchanged.
    - `N < T`: the FIRST N TPs (the user's choice of 2026-09-23).
    - `N >= T`: one per TP, then the extras start again from the first:
      6 positions on 4 TPs are TP1, TP2, TP3, TP4, TP1, TP2.

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
    return tuple(surviving[i % len(surviving)] for i in range(n_positions))


def split_now_pending(
    targets: tuple[tuple[int, float], ...],
) -> tuple[tuple[bool, int, float], ...]:
    """Mark each of distribute()'s targets NOW or PENDING, in TP order.

    One `(is_now, tp_index, tp)` per position. The user's layered entry
    (2026-09-25): position ordinals 0, 2, 4... open NOW and 1, 3, 5... rest
    as PENDING limit orders, so ceil(N/2) open now (an odd N's extra one
    too, and N=1's only one) and floor(N/2) are pending. With 4 positions
    on 4 TPs that is NOW TP1, TP3 and PENDING TP2, TP4.
    """
    return tuple(
        (ordinal % 2 == 0, tp_index, tp)
        for ordinal, (tp_index, tp) in enumerate(targets)
    )


def pending_levels(
    side: Side,
    zone: EntryZone,
    top: float,
    k: int,
    spec: SymbolSpec,
) -> tuple[float, ...]:
    """The prices of k pending limit orders, from `top` to the better edge.

    k >= 2 spaces them evenly from `top` to the zone's better edge (BUY:
    zone.lo, SELL: zone.hi), both inclusive; k = 1 is the middle of that
    range; k = 0 is none. `top` is the pending order nearest to price --
    the caller keeps it the broker's minimum distance away from it.

    `spec` is there because every level goes through round_price, the only
    price rounding, and the tick grid is the feed's own (2 or 3 digits).
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError(f"the number of pending levels must be a whole number, got {k!r}")
    better = zone.favourable_edge(side)
    if k == 0:
        return ()
    if k == 1:
        return (round_price((top + better) / 2.0, spec),)
    return tuple(round_price(top + (better - top) * i / (k - 1), spec) for i in range(k))


def pending_should_cancel(
    side: Side,
    tps: tuple[float, ...],
    bid: float,
    ask: float,
    now: datetime,
    expires: datetime,
) -> bool:
    """Whether an unfilled pending entry order should be cancelled now.

    True once price has reached TP1 -- the bid for a BUY, the ask for a
    SELL, the price the position would close at -- or once `now` has
    reached `expires`. `tps` are the signal's take profits in the poster's
    order, so TP1 is `tps[0]`. Plan 3's worker asks this on every tick.
    """
    if now >= expires:
        return True
    if side is Side.BUY:
        return bid >= tps[0]
    return ask <= tps[0]


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

    # One (kind, entry price, expiry) per position, in leg order.
    if account.layered_entry:
        entries = _layered_entries(sig, spec, snap, pol, targets)
    else:
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
        entries = ((kind, entry_price, expires),) * len(targets)

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
        for leg_index, ((tp_index, tp), (kind, entry_price, expires))
        in enumerate(zip(targets, entries, strict=True))
    )
    notes = (f"{len(sig.tps) - len(surviving_indexed)} take profit(s) already passed",) \
        if len(surviving_indexed) != len(sig.tps) else ()
    return Plan.accept(legs, notes)


def _layered_entries(
    sig: Signal,
    spec: SymbolSpec,
    snap: MarketSnapshot,
    pol: Policy,
    targets: tuple[tuple[int, float], ...],
) -> tuple[tuple[OrderKind, float, datetime | None], ...]:
    """Layered entry: one (kind, entry price, expiry) per target, in leg order.

    The user's amendment of 2026-09-25, for a BUY. A SELL mirrors it: the
    bid is its price, zone.hi its better edge, and "past" is below the zone.

    - NOW positions (see split_now_pending) go at market, unless the ask is
      past the zone by more than the chase budget -- the chase gate's own
      formula -- when they rest as limits at the near edge, zone.hi. The
      plan is never rejected for chasing.
    - PENDING positions rest as limits from `top` down to zone.lo, both
      inclusive (pending_levels). Inside the zone `top` is the ask minus
      the broker's minimum distance; past the zone it is the near edge, so
      they spread over the whole zone.
    - A resting BUY limit must sit at least that minimum distance below the
      ask (gates.min_stop_distance_usd, the one formula), so neither `top`
      nor a NOW limit at the near edge is ever nearer the ask than that.
    - When that leaves no room above zone.lo (top <= zone.lo), every
      position takes the NOW entry. Inside the zone that is market: the
      "better than the zone" case, which an ask below zone.lo also falls
      into, since the nearest valid limit is then below zone.lo too.

    Every limit expires pending_max_age_hours after the post. pending_at
    plays no part here; it belongs to the single rule (_entry_for).
    """
    buying = sig.side is Side.BUY
    price = snap.ask if buying else snap.bid
    expires = sig.posted_at_utc + timedelta(hours=pol.pending_max_age_hours)
    min_dist = gates.min_stop_distance_usd(spec, snap, pol)
    nearest_limit = round_price(price - sig.side.sign * min_dist, spec)
    near = sig.zone.near_edge(sig.side)
    better = sig.zone.favourable_edge(sig.side)
    # The resting limit nearest to price: the near edge, unless the broker's
    # minimum distance pushes it further towards the better edge.
    top = min(near, nearest_limit) if buying else max(near, nearest_limit)

    if gates.chase_gate(sig, spec, snap, pol).ok:
        now = (OrderKind.MARKET, price, None)
    else:
        now = (OrderKind.LIMIT, top, expires)

    room = top > better if buying else top < better
    if not room:
        return (now,) * len(targets)

    split = split_now_pending(targets)
    k = sum(1 for is_now, _, _ in split if not is_now)
    levels = iter(pending_levels(sig.side, sig.zone, top, k, spec))
    return tuple(
        now if is_now else (OrderKind.LIMIT, next(levels), expires)
        for is_now, _, _ in split
    )


def _entry_for(sig: Signal, snap: MarketSnapshot, pol: Policy, price: float):
    """The single rule (layered_entry = false): market inside or better than
    the zone; a limit otherwise.

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
