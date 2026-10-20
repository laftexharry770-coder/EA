"""The basket trailing stop: the approved ladder, and each open leg's target.

Pure: no I/O, no clock, no randomness. The worker calls `target_stops` on
each tick and sends a modify only for the moves it returns; the stop in
place at the broker is the trail's only memory, which is why a move is
emitted only when it tightens that stop.

All distances are USD. The ladder never sees a point or a pip: gold is
quoted at 2 digits on some feeds and 3 on others, and a distance in points
is ten times wrong on one of them.
"""
from __future__ import annotations

from dataclasses import dataclass

from xaucopier.domain.gates import min_stop_distance_usd
from xaucopier.domain.market import MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price
from xaucopier.domain.signal import Side


@dataclass(frozen=True, slots=True)
class Rung:
    trigger: float          # price at which this rung is earned
    stop: float | None      # new stop price; None means breakeven, computed per position
    label: str              # "BE", "TP1", "TP2", ... for logs and alerts


@dataclass(frozen=True, slots=True)
class OpenLeg:
    position_ticket: int
    price_open: float       # this position's own fill
    current_sl: float       # as the broker reports it; 0.0 is MT5's "no stop"


@dataclass(frozen=True, slots=True)
class StopMove:
    position_ticket: int
    new_sl: float
    rung: str


def _strictly_monotonic(side: Side, tps: tuple[float, ...]) -> bool:
    """TP1 < TP2 < ... for a BUY, TP1 > TP2 > ... for a SELL. A NaN fails."""
    return all((later - earlier) * side.sign > 0 for earlier, later in zip(tps, tps[1:]))


def ladder(side: Side, tps: tuple[float, ...], gap_usd: float) -> tuple[Rung, ...]:
    """The user's ladder, in the poster's TP numbering.

    BE is earned at TP1. Then for n = 1 .. T-1 the stop moves to TPn once
    price is `gap_usd` past TPn (n odd) or halfway to TP(n+1) (n even). The
    last TP has no rung: the last positions close there.

    TPs that are not strictly monotonic in the trade's direction make the
    ladder ill-defined, so the answer is no ladder at all, never a guess.
    """
    if not tps or not _strictly_monotonic(side, tps):
        return ()
    rungs = [Rung(trigger=tps[0], stop=None, label="BE")]
    for n in range(1, len(tps)):
        tp_n, tp_next = tps[n - 1], tps[n]
        if n % 2 == 1:
            trigger = tp_n + side.sign * gap_usd
        else:
            trigger = (tp_n + tp_next) / 2.0
        rungs.append(Rung(trigger=trigger, stop=tp_n, label=f"TP{n}"))
    return tuple(rungs)


def _reached(side: Side, price: float, level: float) -> bool:
    return price >= level if side is Side.BUY else price <= level


def _tighter(side: Side, stop: float, than: float) -> bool:
    """A BUY's stop tightens upwards, a SELL's downwards."""
    return stop > than if side is Side.BUY else stop < than


def _tightens(side: Side, stop: float, current_sl: float, spec: SymbolSpec) -> bool:
    """Whether moving to `stop` tightens the stop in place. MT5 reports a
    missing stop as 0.0, which is numerically below every SELL stop but is
    the loosest stop there is."""
    if current_sl <= 0.0:
        return True
    return _tighter(side, stop, round_price(current_sl, spec))


def _breakeven(side: Side, price_open: float, snap: MarketSnapshot,
               spec: SymbolSpec, pol: Policy) -> float:
    """This leg's own fill plus costs, the same formula as the channel's
    "SL to BE" (followup_planner._breakeven_price; a test pins the two)."""
    offset = snap.spread_usd + pol.be_buffer_usd
    return round_price(price_open + side.sign * offset, spec)


def _at_breakeven(side: Side, leg: OpenLeg, spec: SymbolSpec, pol: Policy) -> bool:
    """Whether the leg's stop already locks in breakeven: at or past its own
    fill plus be_buffer_usd (the round-turn cost): a stop-out there loses
    nothing.

    `_breakeven` adds the live spread, so a BE recomputed on every tick
    would creep with each new spread high: one modify per leg per tick in a
    news spike, ending dollars past breakeven. BE is therefore placed once,
    with the spread at that moment, and never re-sent while this holds."""
    if leg.current_sl <= 0.0:   # MT5's "no stop"
        return False
    line = round_price(leg.price_open + side.sign * pol.be_buffer_usd, spec)
    return (round_price(leg.current_sl, spec) - line) * side.sign >= 0


def target_stops(
    side: Side, tps: tuple[float, ...], legs: tuple[OpenLeg, ...],
    snap: MarketSnapshot, spec: SymbolSpec, pol: Policy,
) -> tuple[StopMove, ...]:
    """The stop each open leg should move to now, for the legs that need one.

    The reference price is the one the position would exit at: bid for a
    BUY, ask for a SELL. Of the rungs that price has earned, each leg takes
    the tightest stop the broker will accept (on the right side of price,
    at least the minimum stop distance away), and moves only if that is
    strictly tighter than its current stop. A leg whose stop is already at
    breakeven (`_at_breakeven`) does not take the BE rung again.
    """
    if not pol.trail_enabled:
        return ()
    rungs = ladder(side, tps, pol.trail_gap_usd)
    price = snap.bid if side is Side.BUY else snap.ask
    earned = tuple(r for r in rungs if _reached(side, price, round_price(r.trigger, spec)))
    if not earned:
        return ()
    min_dist = min_stop_distance_usd(spec, snap, pol)

    moves = []
    for leg in legs:
        best: tuple[float, str] | None = None
        be_placed = _at_breakeven(side, leg, spec, pol)
        for rung in earned:
            if rung.stop is None:
                if be_placed:
                    continue
                stop = _breakeven(side, leg.price_open, snap, spec, pol)
            else:
                stop = round_price(rung.stop, spec)
            distance = (price - stop) * side.sign   # positive: the right side of price
            if distance <= 0 or distance < min_dist:
                continue
            if best is None or _tighter(side, stop, best[0]):
                best = (stop, rung.label)
        if best is not None and _tightens(side, best[0], leg.current_sl, spec):
            moves.append(StopMove(position_ticket=leg.position_ticket,
                                  new_sl=best[0], rung=best[1]))
    return tuple(moves)
