"""The trailing stop ladder, like the user's Target Trend EA, and each open
leg's target stop.

Pure: no I/O, no clock, no randomness. The worker calls `target_stops` on
each tick and sends a modify only for the moves it returns; the stop in
place at the broker is the trail's only memory, which is why a move is
emitted only when it tightens that stop.

The ladder (spec amendment 2026-10-21; the EA's TV_ManageStopLadder) runs
per position, on the signal's own take profits in the poster's numbering:

- step 1: once TP1 is reached, the stop moves to breakeven;
- step k (k = 2..T): once TPk is reached, the stop moves to TP(k-1).

A step is reached at its TP, or `ladder_early_pips[k-1]` pips before it.
The highest reached step whose stop is placeable wins; one that is not
falls through to the next lower step.

Every price and distance here is USD. The only pips are the policy's two
settings, converted with the fixed PIP_USD and never with the broker's
point: gold is quoted at 2 digits on some feeds and 3 on others, and a
distance in points is ten times wrong on one of them.
"""
from __future__ import annotations

from dataclasses import dataclass

from xaucopier.domain.gates import min_stop_distance_usd
from xaucopier.domain.market import PIP_USD, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price
from xaucopier.domain.signal import Side


@dataclass(frozen=True, slots=True)
class Rung:
    trigger: float          # the price at which this step is reached
    stop: float | None      # the stop it moves to; None means breakeven, computed per position
    label: str              # "BE", "TP1", "TP2", ...: the stop's name, for logs and alerts


@dataclass(frozen=True, slots=True)
class OpenLeg:
    position_ticket: int
    price_open: float       # this position's own fill
    current_sl: float       # as the broker reports it; 0.0 is MT5's "no stop"
    tp: float               # this position's own take profit; 0.0 is MT5's "none"


@dataclass(frozen=True, slots=True)
class StopMove:
    position_ticket: int
    new_sl: float
    rung: str


def _strictly_monotonic(side: Side, tps: tuple[float, ...]) -> bool:
    """TP1 < TP2 < ... for a BUY, TP1 > TP2 > ... for a SELL. A NaN fails."""
    return all((later - earlier) * side.sign > 0 for earlier, later in zip(tps, tps[1:]))


def ladder(side: Side, tps: tuple[float, ...],
           early_usd: tuple[float, ...] = ()) -> tuple[Rung, ...]:
    """The EA's ladder, in the poster's TP numbering: step 1 moves the stop
    to breakeven at TP1, step k to TP(k-1) at TPk.

    `early_usd[k-1]` is how far before TPk step k is reached, in USD (for a
    SELL, that far above it); a missing entry is 0, on hit. target_stops
    converts the policy's pips with PIP_USD.

    TPs that are not strictly monotonic in the trade's direction make the
    ladder ill-defined, so the answer is no ladder at all, never a guess.
    """
    if not tps or not _strictly_monotonic(side, tps):
        return ()
    rungs = []
    for k, tp in enumerate(tps, start=1):
        early = early_usd[k - 1] if k <= len(early_usd) else 0.0
        stop, label = (None, "BE") if k == 1 else (tps[k - 2], f"TP{k - 1}")
        rungs.append(Rung(trigger=tp - side.sign * early, stop=stop, label=label))
    return tuple(rungs)


def breakeven(side: Side, price_open: float, tp1: float,
              spec: SymbolSpec, pol: Policy) -> float:
    """A position's breakeven, like the EA's TV_Be: its OWN fill plus
    be_buffer_pips x PIP_USD for a BUY (minus, for a SELL), never past TP1.

    The one formula for the trail and for the channel's "SL to BE"
    (followup_planner calls this). There is no spread term: a BUY fills at
    the ask and its stop triggers on the bid, so fill + buffer already locks
    the buffer in; a SELL mirrors this. Because it does not move with the
    spread, a breakeven once placed is never re-sent in a spread spike.
    Commission is not modelled; the buffer is what a stop-out there keeps.
    """
    be = price_open + side.sign * pol.be_buffer_pips * PIP_USD
    be = min(be, tp1) if side is Side.BUY else max(be, tp1)
    return round_price(be, spec)


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


def _behind_own_tp(side: Side, stop: float, tp: float, spec: SymbolSpec) -> bool:
    """Strictly behind the position's own take profit: below it for a BUY,
    above it for a SELL. A stop at or past it is never set (the EA's
    TV_StopOk). A position with no take profit (0.0) has no such limit."""
    if tp <= 0.0:
        return True
    return _tighter(side, round_price(tp, spec), stop)


def target_stops(
    side: Side, tps: tuple[float, ...], legs: tuple[OpenLeg, ...],
    snap: MarketSnapshot, spec: SymbolSpec, pol: Policy,
) -> tuple[StopMove, ...]:
    """The stop each open leg should move to now, for the legs that need one.

    `tps` are the signal's take profits as posted (sig.tps), all of them.
    The reference price is the one the position would exit at: bid for a
    BUY, ask for a SELL. For each leg the reached steps are tried from the
    highest down, and the first whose stop is placeable is taken:

    - at least the broker's minimum stop distance from that price, on the
      right side of it (gates.min_stop_distance_usd, the one formula);
    - strictly tighter than the leg's current stop, so it never loosens;
    - strictly behind the leg's own take profit.
    """
    if not pol.trail_enabled:
        return ()
    early_usd = tuple(pips * PIP_USD for pips in pol.ladder_early_pips)
    rungs = ladder(side, tps, early_usd)
    price = snap.bid if side is Side.BUY else snap.ask
    reached = tuple(r for r in rungs if _reached(side, price, round_price(r.trigger, spec)))
    if not reached:
        return ()
    min_dist = min_stop_distance_usd(spec, snap, pol)

    moves = []
    for leg in legs:
        for rung in reversed(reached):   # the best placeable step first
            if rung.stop is None:
                stop = breakeven(side, leg.price_open, tps[0], spec, pol)
            else:
                stop = round_price(rung.stop, spec)
            distance = (price - stop) * side.sign   # positive: the right side of price
            if (distance > 0 and distance >= min_dist
                    and _tightens(side, stop, leg.current_sl, spec)
                    and _behind_own_tp(side, stop, leg.tp, spec)):
                moves.append(StopMove(position_ticket=leg.position_ticket,
                                      new_sl=stop, rung=rung.label))
                break
    return tuple(moves)
