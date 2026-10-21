"""The end-to-end acceptance test of Plan 1b (Tasks 5 and 6): the real
signal, planned, filled, and walked up the trailing ladder.

The real fixture (tests/fixtures/messages/mmh_buy_4tp.json) is a BUY, zone
4480-4485, SL 4470, TPs 4490 / 4495 / 4500 / 4505. The spread is 0.10 at
every step unless a step says otherwise, so the broker's minimum stop
distance is 3 x 0.10 = 0.30.

The ladder is the user's EA's (spec amendment 2026-10-21): TP1 reached
moves a position's stop to its breakeven, its OWN fill + 1 pip ($0.10);
TPk reached moves it to TP(k-1). With positions = 4 the TPs cycle, so every
TP has one position, and as price climbs each one closes at its own TP
while the stop of the rest steps up one TP behind the price: breakeven,
then 4490, then 4495. No position ever holds the step-4 stop (TP3, 4500) on
the default policy: step 4 is reached at TP4 itself, where the last
position closes.

It runs with layered_entry false and true (ruling P3):

- layered_entry = false: every position fills at market at 4482.55, the
  worked example's fill, so every breakeven is 4482.65; the walk reproduces
  the amendment's worked example row for row.
- layered_entry = true, planned at the amendment's ask 4483: two positions
  open at market and two rest as limits, at 4482.70 and 4480. A limit fills
  at its own price once the ask comes down to it, so its breakeven differs;
  one still unfilled when the bid reaches TP1 is cancelled.

The walk is what Plan 3's worker and the broker will do between them, for a
BUY, at each price on the path (see _walk). The trail and the cancel rule
are both given the signal's own TPs, all four in the poster's numbering
(ruling T3-cv2), never only the ones that still have a position.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from xaucopier.domain.intent import OrderKind, Plan
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.planner import pending_should_cancel, plan_entry
from xaucopier.domain.signal import Side, Signal
from xaucopier.domain.trailing import OpenLeg, target_stops
from xaucopier.parsing.channel_mmh import parse

FIXTURE = Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json"
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()   # be_buffer_pips 1.0, every step on hit, trail on
SPREAD = 0.10       # so the minimum stop distance is 3 x 0.10 = 0.30
TICKET_BASE = 9000  # a position's ticket is TICKET_BASE + its leg_index


def _real_signal() -> Signal:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = parse(fixture["text"], channel_id=fixture["channel_id"],
                   message_id=fixture["message_id"],
                   posted_at_utc=datetime.fromisoformat(fixture["posted_at_utc"]))
    assert result.ok, result
    return result.signal


SIGNAL = _real_signal()


def _market(bid: float, at: datetime, spread: float = SPREAD) -> MarketSnapshot:
    return MarketSnapshot(bid=bid, ask=round(bid + spread, 2), tick_time_utc=at,
                          spread_usd=spread, median_spread_usd=SPREAD, now_utc=at)


def _tick(step: float | tuple[float, float]) -> tuple[float, float]:
    """A step of a path: a bid at the usual spread, or (bid, spread)."""
    return step if isinstance(step, tuple) else (step, SPREAD)


def _plan(layered: bool, ask: float, positions: int = 4) -> Plan:
    account = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001,
                          positions=positions, layered_entry=layered)
    at = SIGNAL.posted_at_utc + timedelta(seconds=5)
    plan = plan_entry(SIGNAL, account, SPEC, _market(round(ask - SPREAD, 2), at), POLICY)
    assert plan.accepted, plan
    return plan


def _walk(plan: Plan, path: tuple[float | tuple[float, float], ...], policy: Policy = POLICY):
    """Walk the bid along `path`, one minute a step, and return one
    `(step, {leg_index: stop} of the open positions, leg_indices of the
    limits still resting)` per step, and how each leg ended. A step is a
    bid at the usual spread, or `(bid, spread)` (see _tick).

    The market legs fill at the planning tick, at their planned price. At
    each step, in this order:

    1. the broker fills a resting buy limit at its own price once the ask
       has come down to it;
    2. the worker cancels a resting limit once pending_should_cancel says
       so (price at TP1, or the order's expiry);
    3. the broker closes a position at its TP once the bid has reached it;
    4. the trail moves the stops target_stops returns -- and every move
       must tighten the stop and stay strictly below the position's own
       TP, which this asserts;
    5. the broker closes a position at its stop once the bid has fallen to
       it.

    Stop-outs come after the trail so that a retrace is the worked
    example's last row: the worker sees that tick, maybe before it learns
    the broker has closed the position, and must not loosen the stop. On a
    rising step no stop can be hit, so the order changes nothing else.
    """
    assert SIGNAL.side is Side.BUY
    legs = {leg.leg_index: leg for leg in plan.legs}
    resting = {i for i, leg in legs.items() if leg.kind is OrderKind.LIMIT}
    open_ = {i: OpenLeg(TICKET_BASE + i, leg.price, leg.sl, leg.tp)
             for i, leg in legs.items() if leg.kind is OrderKind.MARKET}
    ended: dict[int, tuple[str, float | None]] = {}
    steps = []
    for minute, step in enumerate(path, start=1):
        bid, spread = _tick(step)
        snap = _market(bid, SIGNAL.posted_at_utc + timedelta(minutes=minute), spread)
        for i in sorted(resting):
            if snap.ask <= legs[i].price:
                resting.discard(i)
                open_[i] = OpenLeg(TICKET_BASE + i, legs[i].price, legs[i].sl, legs[i].tp)
        for i in sorted(resting):
            if pending_should_cancel(SIGNAL.side, SIGNAL.tps, snap.bid, snap.ask,
                                     snap.now_utc, legs[i].expires_at_utc):
                resting.discard(i)
                ended[i] = ("cancelled", None)
        for i in sorted(open_):
            if snap.bid >= legs[i].tp:
                del open_[i]
                ended[i] = ("tp", legs[i].tp)
        for move in target_stops(SIGNAL.side, SIGNAL.tps, tuple(open_.values()),
                                 snap, SPEC, policy):
            i = move.position_ticket - TICKET_BASE
            assert move.new_sl > open_[i].current_sl, (
                f"bid {bid}: leg {i}'s stop would loosen from "
                f"{open_[i].current_sl} to {move.new_sl}")
            assert move.new_sl < legs[i].tp, (
                f"bid {bid}: leg {i}'s stop {move.new_sl} is not below its TP {legs[i].tp}")
            open_[i] = dataclasses.replace(open_[i], current_sl=move.new_sl)
        for i in sorted(open_):
            if snap.bid <= open_[i].current_sl:
                ended[i] = ("stop", open_[i].current_sl)
                del open_[i]
        steps.append((step, {i: leg.current_sl for i, leg in sorted(open_.items())},
                      tuple(sorted(resting))))
    return steps, ended


def _check_walk(plan, table, ended, policy=POLICY):
    got_steps, got_ended = _walk(plan, tuple(step for step, _, _ in table), policy)
    for got, want in zip(got_steps, table, strict=True):
        assert got == want
    assert got_ended == ended


def _open_counts(table):
    return [len(open_) for _, open_, _ in table]


# ---------------------------------------------------------------------------
# layered_entry = false, positions = 4: one position per TP, all four bought
# at 4482.55, so every breakeven is 4482.55 + 1 pip = 4482.65.
# ---------------------------------------------------------------------------

BE_4482_55 = 4482.65   # 4482.55 + be_buffer_pips 1.0 x $0.10; no spread term

# The brief's path, 4488 -> 4490 -> 4495 -> 4500 -> 4505, then a retrace.
# (bid, open positions afterwards: leg -> its stop, limits still resting)
UP_THE_LADDER = (
    (4488.00, {0: 4470.00, 1: 4470.00, 2: 4470.00, 3: 4470.00}, ()),
    # TP1: leg 0 closes there; step 1 moves the rest to breakeven.
    (4490.00, {1: BE_4482_55, 2: BE_4482_55, 3: BE_4482_55}, ()),
    # TP2: leg 1 closes there; step 2 moves the rest to TP1.
    (4495.00, {2: 4490.00, 3: 4490.00}, ()),
    # TP3: leg 2 closes there; step 3 moves the last one to TP2.
    (4500.00, {3: 4495.00}, ()),
    # TP4: the last position closes at its own TP, before any step-4 stop.
    (4505.00, {}, ()),
    # The retrace finds nothing left open.
    (4495.00, {}, ()),
)
UP_THE_LADDER_ENDED = {0: ("tp", 4490.0), 1: ("tp", 4495.0), 2: ("tp", 4500.0),
                       3: ("tp", 4505.0)}

# The amendment's worked example, row for row, for the TP4 position (leg 3)
# while the other three close at their TPs: 4489.99 nothing, 4490 BE,
# 4494.99 BE, 4495 TP1, 4499.99 TP1, 4500 TP2, 4504.99 TP2, then the retrace
# to 4492 leaves it at TP2 -- and the broker closes it there.
WORKED_EXAMPLE = (
    (4488.00, {0: 4470.00, 1: 4470.00, 2: 4470.00, 3: 4470.00}, ()),
    (4489.99, {0: 4470.00, 1: 4470.00, 2: 4470.00, 3: 4470.00}, ()),
    (4490.00, {1: BE_4482_55, 2: BE_4482_55, 3: BE_4482_55}, ()),
    (4494.99, {1: BE_4482_55, 2: BE_4482_55, 3: BE_4482_55}, ()),
    (4495.00, {2: 4490.00, 3: 4490.00}, ()),
    (4499.99, {2: 4490.00, 3: 4490.00}, ()),
    (4500.00, {3: 4495.00}, ()),
    (4504.99, {3: 4495.00}, ()),
    # The retrace: the trail leaves 4495 alone (it never loosens), and the
    # broker then closes the last position at that stop, TP2 locked in.
    (4492.00, {}, ()),
)
WORKED_EXAMPLE_ENDED = {0: ("tp", 4490.0), 1: ("tp", 4495.0), 2: ("tp", 4500.0),
                        3: ("stop", 4495.0)}

def test_the_single_entry_plan_is_four_market_positions_at_the_worked_example_fill():
    plan = _plan(layered=False, ask=4482.55)
    assert [leg.leg_index for leg in plan.legs] == [0, 1, 2, 3]
    assert {leg.kind for leg in plan.legs} == {OrderKind.MARKET}
    assert {leg.price for leg in plan.legs} == {4482.55}
    assert {leg.sl for leg in plan.legs} == {4470.0}
    # Four positions on four TPs: the cycle gives one each.
    assert [leg.tp for leg in plan.legs] == [4490.0, 4495.0, 4500.0, 4505.0]
    assert {leg.volume for leg in plan.legs} == {0.01}   # fixed_lot, per position


def test_the_single_entry_walk_up_the_ladder():
    _check_walk(_plan(layered=False, ask=4482.55), UP_THE_LADDER, UP_THE_LADDER_ENDED)


def test_the_single_entry_walk_goes_4_3_2_1_0_open_with_the_stop_one_tp_behind():
    steps, _ = _walk(_plan(layered=False, ask=4482.55),
                     tuple(bid for bid, _, _ in UP_THE_LADDER))
    assert [len(open_) for _, open_, _ in steps] == [4, 3, 2, 1, 0, 0]
    # The last position's stop at every step: SL, BE, TP1, TP2, then closed
    # at TP4.
    assert [open_.get(3) for _, open_, _ in steps] == [
        4470.00, BE_4482_55, 4490.00, 4495.00, None, None]


def test_the_single_entry_walk_reproduces_the_worked_example():
    _check_walk(_plan(layered=False, ask=4482.55), WORKED_EXAMPLE, WORKED_EXAMPLE_ENDED)
    assert _open_counts(WORKED_EXAMPLE) == [4, 4, 3, 3, 2, 2, 1, 1, 0]


# A spread spike at the TP2 step. At spread 2.00 the broker's minimum stop
# distance is 3 x 2.00 = 6.00, so TP1, 5.00 below the bid, is not a stop the
# broker accepts. The step falls through to breakeven, which is already in
# place (it has no spread term, so it is not re-sent either), and the stop
# waits there until the spread settles.
SPREAD_SPIKE_WALK = (
    (4490.00, {1: BE_4482_55, 2: BE_4482_55, 3: BE_4482_55}, ()),
    ((4495.00, 2.00), {2: BE_4482_55, 3: BE_4482_55}, ()),   # leg 1 closes at TP2
    (4495.00, {2: 4490.00, 3: 4490.00}, ()),                  # spread 0.10 again
)


def test_a_spread_spike_holds_the_stop_where_the_broker_accepts_it():
    _check_walk(_plan(layered=False, ask=4482.55), SPREAD_SPIKE_WALK,
                {0: ("tp", 4490.0), 1: ("tp", 4495.0)})


# ---------------------------------------------------------------------------
# layered_entry = true, from the amendment's ask 4483: NOW legs 0 and 2 at
# market 4483 (TP1, TP3), PENDING leg 1 at 4482.70 (TP2) and leg 3 at 4480
# (TP4). Breakeven, each its own fill + 1 pip: 4483.10 for legs 0 and 2,
# 4482.80 for leg 1, 4480.10 for leg 3.
# ---------------------------------------------------------------------------

# Both walks start at the planning price, ask 4483: the market legs are
# open, and neither limit is reached.
AT_THE_PLAN = (4482.90, {0: 4470.00, 2: 4470.00}, (1, 3))

# Price dips through both limits first, so all four positions are open
# when the walk starts up the ladder: 4488 -> 4490 -> 4495 -> 4500 -> 4505,
# then a retrace.
BOTH_LIMITS_FILL = (
    AT_THE_PLAN,
    (4482.60, {0: 4470.00, 1: 4470.00, 2: 4470.00}, (3,)),   # ask 4482.70: leg 1 fills
    (4479.90, {0: 4470.00, 1: 4470.00, 2: 4470.00, 3: 4470.00}, ()),  # ask 4480: leg 3
    (4488.00, {0: 4470.00, 1: 4470.00, 2: 4470.00, 3: 4470.00}, ()),
    # Leg 0 closes at TP1; each of the rest goes to its OWN breakeven.
    (4490.00, {1: 4482.80, 2: 4483.10, 3: 4480.10}, ()),
    (4495.00, {2: 4490.00, 3: 4490.00}, ()),                 # leg 1 closes at TP2
    (4500.00, {3: 4495.00}, ()),                             # leg 2 closes at TP3
    (4505.00, {}, ()),                                       # leg 3 closes at TP4
    (4495.00, {}, ()),
)
BOTH_LIMITS_FILL_ENDED = {0: ("tp", 4490.0), 1: ("tp", 4495.0), 2: ("tp", 4500.0),
                          3: ("tp", 4505.0)}

# Price dips only to the first limit; the second is still resting when the
# bid reaches TP1, and is cancelled there -- not one tick before. Then a
# retrace short of TP3 leaves leg 2's stop at TP1, where it is stopped out.
ONE_LIMIT_FILLS = (
    AT_THE_PLAN,
    (4482.60, {0: 4470.00, 1: 4470.00, 2: 4470.00}, (3,)),   # ask 4482.70: leg 1 fills
    (4488.00, {0: 4470.00, 1: 4470.00, 2: 4470.00}, (3,)),
    (4489.99, {0: 4470.00, 1: 4470.00, 2: 4470.00}, (3,)),   # TP1 - 0.01: still resting
    (4490.00, {1: 4482.80, 2: 4483.10}, ()),                 # leg 3 cancelled, leg 0 at TP1
    (4495.00, {2: 4490.00}, ()),                             # leg 1 closes at TP2
    (4499.99, {2: 4490.00}, ()),
    (4492.00, {2: 4490.00}, ()),                             # the retrace: never loosens
    (4489.00, {}, ()),                                       # stopped out at 4490
)
ONE_LIMIT_FILLS_ENDED = {0: ("tp", 4490.0), 1: ("tp", 4495.0), 2: ("stop", 4490.0),
                         3: ("cancelled", None)}


def test_the_layered_plan_is_the_amendments_row_at_ask_4483():
    plan = _plan(layered=True, ask=4483.00)
    assert [(leg.leg_index, leg.kind, leg.price, leg.tp) for leg in plan.legs] == [
        (0, OrderKind.MARKET, 4483.00, 4490.0),
        (1, OrderKind.LIMIT, 4482.70, 4495.0),
        (2, OrderKind.MARKET, 4483.00, 4500.0),
        (3, OrderKind.LIMIT, 4480.00, 4505.0),
    ]
    assert {leg.sl for leg in plan.legs} == {4470.0}
    # Both limits expire 4 hours after the post, long after this walk.
    assert {leg.expires_at_utc for leg in plan.legs if leg.kind is OrderKind.LIMIT} == {
        SIGNAL.posted_at_utc + timedelta(hours=4)}


@pytest.mark.parametrize("table, ended", [
    pytest.param(BOTH_LIMITS_FILL, BOTH_LIMITS_FILL_ENDED, id="both limits fill"),
    pytest.param(ONE_LIMIT_FILLS, ONE_LIMIT_FILLS_ENDED, id="one fills, one is cancelled"),
])
def test_the_layered_walk(table, ended):
    _check_walk(_plan(layered=True, ask=4483.00), table, ended)


def test_once_every_limit_fills_the_layered_walk_goes_4_3_2_1_0_open():
    assert _open_counts(BOTH_LIMITS_FILL)[2:] == [4, 4, 3, 2, 1, 0, 0]
