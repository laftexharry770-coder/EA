"""Pre-trade predicates. Pure: no I/O, no clock, no randomness.

`now` always arrives inside the MarketSnapshot so a test can pin it.
"""
from __future__ import annotations

from dataclasses import dataclass

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import Side, Signal


@dataclass(frozen=True, slots=True)
class GateResult:
    ok: bool
    reason: RejectReason | None = None
    detail: str = ""


_OK = GateResult(True)


def _no(reason: RejectReason, detail: str) -> GateResult:
    return GateResult(False, reason, detail)


def age_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """Age is measured from the message's own timestamp, never from how it
    was discovered. Gating backfilled messages separately makes the history
    poll catch nothing while logging that it works."""
    age = (snap.now_utc - sig.posted_at_utc).total_seconds()
    if age > pol.max_signal_age_seconds:
        return _no(RejectReason.STALE, f"{age:.0f}s old")
    return _OK


def tick_freshness_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    age = (snap.now_utc - snap.tick_time_utc).total_seconds()
    if age > pol.tick_freshness_seconds:
        return _no(RejectReason.TICK_STALE, f"tick {age:.0f}s old")
    return _OK


def spread_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    if snap.spread_usd > pol.max_spread_usd:
        return _no(RejectReason.SPREAD_WIDE, f"{snap.spread_usd:.3f} > {pol.max_spread_usd}")
    ceiling = snap.median_spread_usd * pol.spread_median_multiple
    if snap.median_spread_usd > 0 and snap.spread_usd > ceiling:
        return _no(RejectReason.SPREAD_WIDE, f"{snap.spread_usd:.3f} > {ceiling:.3f} (median x{pol.spread_median_multiple})")
    return _OK


def geometry_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """Derive the side from the level layout and cross-check the keyword.

    A poster who writes BUY above sell levels must produce a hard reject,
    never a coin flip.
    """
    mid_zone = (sig.zone.lo + sig.zone.hi) / 2.0
    tps_above = all(tp > sig.zone.hi for tp in sig.tps)
    tps_below = all(tp < sig.zone.lo for tp in sig.tps)
    if not (tps_above or tps_below):
        return _no(RejectReason.GEOMETRY_CONFLICT, "take profits straddle the entry zone")
    derived = Side.BUY if tps_above else Side.SELL
    if derived is not sig.side:
        return _no(RejectReason.GEOMETRY_CONFLICT,
                   f"levels imply {derived.value}, message says {sig.side.value}")
    if derived is Side.BUY and sig.sl >= mid_zone:
        return _no(RejectReason.GEOMETRY_CONFLICT, "buy stop is not below entry")
    if derived is Side.SELL and sig.sl <= mid_zone:
        return _no(RejectReason.GEOMETRY_CONFLICT, "sell stop is not above entry")
    return _OK


def magnitude_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """Every level must sit within a band of the live mid. Catches a parse
    that produced 447 or 44700 instead of 4470."""
    band = snap.mid * pol.magnitude_band_pct / 100.0
    for label, level in [("zone.lo", sig.zone.lo), ("zone.hi", sig.zone.hi),
                         ("sl", sig.sl), *[(f"tp{i+1}", tp) for i, tp in enumerate(sig.tps)]]:
        if abs(level - snap.mid) > band:
            return _no(RejectReason.MAGNITUDE_BAND, f"{label}={level} vs mid {snap.mid:.2f}")
    return _OK


def invalidation_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    price = snap.ask if sig.side is Side.BUY else snap.bid
    if sig.side is Side.BUY and price <= sig.sl:
        return _no(RejectReason.SL_BREACHED, f"ask {price} at/below sl {sig.sl}")
    if sig.side is Side.SELL and price >= sig.sl:
        return _no(RejectReason.SL_BREACHED, f"bid {price} at/above sl {sig.sl}")
    if not surviving_tps(sig, snap):
        return _no(RejectReason.ALL_TPS_BEHIND, "price is past every take profit")
    return _OK


def surviving_tps(sig: Signal, snap: MarketSnapshot) -> tuple[float, ...]:
    """Take profits still ahead of price. A leg for a passed TP is pointless."""
    price = snap.ask if sig.side is Side.BUY else snap.bid
    if sig.side is Side.BUY:
        return tuple(tp for tp in sig.tps if tp > price)
    return tuple(tp for tp in sig.tps if tp < price)


def chase_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """How far past the zone price has already run.

    Budget scales with the trade: a fraction of the distance from the zone
    to TP1, floored so tight scalps are not rejected outright.
    """
    price = snap.ask if sig.side is Side.BUY else snap.bid
    near = sig.zone.near_edge(sig.side)
    overshoot = (price - near) if sig.side is Side.BUY else (near - price)
    if overshoot <= 0:
        return _OK
    budget = max(pol.chase_floor_usd, pol.chase_tp1_fraction * abs(sig.tps[0] - near))
    if overshoot > budget:
        return _no(RejectReason.CHASE_EXCEEDED, f"{overshoot:.2f} past zone, budget {budget:.2f}")
    return _OK


def stops_distance_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """Reject a stop the broker will not accept. Never widen it.

    Under fixed-lot sizing a wider stop is a strictly larger dollar loss
    with no volume compensation, so clamping an SL outward silently changes
    the trade. Take profits may be clamped; the stop may not.
    """
    price = snap.ask if sig.side is Side.BUY else snap.bid
    min_dist = max(spec.stops_level_usd, pol.stops_distance_spread_multiple * snap.spread_usd)
    if abs(price - sig.sl) < min_dist:
        return _no(RejectReason.STOPS_TOO_TIGHT,
                   f"sl {abs(price - sig.sl):.2f} from price, min {min_dist:.2f}")
    return _OK


def volume_gate(account: AccountSpec, spec: SymbolSpec) -> GateResult:
    if account.fixed_lot < spec.volume_min:
        return _no(RejectReason.INVALID_VOLUME,
                   f"fixed_lot {account.fixed_lot} below min {spec.volume_min}")
    if account.fixed_lot > spec.volume_max:
        return _no(RejectReason.INVALID_VOLUME,
                   f"fixed_lot {account.fixed_lot} above max {spec.volume_max}")
    steps = account.fixed_lot / spec.volume_step
    if abs(steps - round(steps)) > 1e-9:
        return _no(RejectReason.INVALID_VOLUME,
                   f"fixed_lot {account.fixed_lot} is not a multiple of {spec.volume_step}")
    return _OK
