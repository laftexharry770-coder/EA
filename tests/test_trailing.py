"""The trailing stop ladder, like the user's Target Trend EA (Plan 1b Task 6,
spec amendment 2026-10-21).

Per position, on the signal's own TPs: step 1, TP1 reached, moves the stop
to breakeven; step k, TPk reached, moves it to TP(k-1). The highest reached
step the broker can take wins; one it cannot take falls through to the next
lower one. Breakeven is the position's own fill + be_buffer_pips x $0.10,
never past TP1, with no spread term.

The amendment's worked example is the acceptance test: BUY, SL 4470, TPs
4490/4495/4500/4505, one position (the TP4 one) filled at 4482.55, buffer
1 pip, spread 0.10 (minimum distance 3 x 0.10 = 0.30). Breakeven is
4482.55 + 1 x 0.10 = 4482.65. The SELL mirror reflects every level through
4500 (x -> 9000 - x): fill 4517.45, breakeven 4517.35, SL 4530.
"""
import dataclasses
from datetime import UTC, datetime

import pytest

from xaucopier.domain import followup_planner, trailing
from xaucopier.domain.followup_planner import LegView, plan_followup
from xaucopier.domain.market import PIP_USD, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import FollowUp, FollowUpOp, Side
from xaucopier.domain.trailing import OpenLeg, Rung, StopMove, breakeven, ladder, target_stops

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
EXNESS = SymbolSpec("XAUUSDm", 3, 0.001, 0.001, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()  # be_buffer_pips 1.0, ladder_early_pips (), trail on

BUY_TPS = (4490.0, 4495.0, 4500.0, 4505.0)
SELL_TPS = (4510.0, 4505.0, 4500.0, 4495.0)
TICKET = 7001


def buy_snap(bid, spread=0.10):
    return MarketSnapshot(bid=bid, ask=round(bid + spread, 3), tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=0.10, now_utc=NOW)


def sell_snap(ask, spread=0.10):
    return MarketSnapshot(bid=round(ask - spread, 3), ask=ask, tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=0.10, now_utc=NOW)


def buy_leg(current_sl, price_open=4482.55, tp=4505.0, ticket=TICKET):
    """By default the worked example's position: the TP4 one."""
    return OpenLeg(position_ticket=ticket, price_open=price_open, current_sl=current_sl, tp=tp)


def sell_leg(current_sl, price_open=4517.45, tp=4495.0, ticket=TICKET):
    return OpenLeg(position_ticket=ticket, price_open=price_open, current_sl=current_sl, tp=tp)


def test_a_pip_is_ten_cents():
    assert PIP_USD == 0.10


# ---------------------------------------------------------------------------
# The worked example, one test per row. Each row starts from the stop the
# row before it left in place (the first from the signal's SL), so a row
# that reaches nothing new must emit nothing at all.
# ---------------------------------------------------------------------------

# (bid, the stop in place afterwards, the step's label when it moved)
BUY_TABLE = (
    (4489.99, 4470.00, None),    # no step reached
    (4490.00, 4482.65, "BE"),    # TP1 reached: breakeven, fill + 1 pip
    (4494.99, 4482.65, None),
    (4495.00, 4490.00, "TP1"),   # TP2 reached: the stop to TP1
    (4499.99, 4490.00, None),
    (4500.00, 4495.00, "TP2"),   # TP3 reached: the stop to TP2
    (4504.99, 4495.00, None),
    (4492.00, 4495.00, None),    # the retrace: never loosens
)

# The exact mirror: ask, x -> 9000 - x.
SELL_TABLE = (
    (4510.01, 4530.00, None),
    (4510.00, 4517.35, "BE"),    # fill - 1 pip
    (4505.01, 4517.35, None),
    (4505.00, 4510.00, "TP1"),
    (4500.01, 4510.00, None),
    (4500.00, 4505.00, "TP2"),
    (4495.01, 4505.00, None),
    (4508.00, 4505.00, None),    # the retrace: never loosens
)


def _rows_with_prior(table, signal_sl):
    prior = (signal_sl,) + tuple(stop for _, stop, _ in table[:-1])
    return [(price, before, stop, label)
            for (price, stop, label), before in zip(table, prior)]


def _expected(stop, label):
    return () if label is None else (StopMove(TICKET, stop, label),)


@pytest.mark.parametrize("bid, before, stop, label", _rows_with_prior(BUY_TABLE, 4470.00),
                         ids=[f"bid {row[0]:.2f}" for row in BUY_TABLE])
def test_worked_example_buy_row(bid, before, stop, label):
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(before),), buy_snap(bid), SPEC, POLICY)
    assert moves == _expected(stop, label)


@pytest.mark.parametrize("ask, before, stop, label", _rows_with_prior(SELL_TABLE, 4530.00),
                         ids=[f"ask {row[0]:.2f}" for row in SELL_TABLE])
def test_worked_example_sell_mirror_row(ask, before, stop, label):
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(before),), sell_snap(ask), SPEC, POLICY)
    assert moves == _expected(stop, label)


def _walk(side, tps, leg, snap_at, prices, spec=SPEC, pol=POLICY):
    """Feed each move back in as the new current stop, the way the worker
    will, and return the stop in place after every step."""
    stops = []
    for price in prices:
        for move in target_stops(side, tps, (leg,), snap_at(price), spec, pol):
            assert move.position_ticket == leg.position_ticket
            leg = dataclasses.replace(leg, current_sl=move.new_sl)
        stops.append(leg.current_sl)
    return stops


def test_walking_the_buy_path_reproduces_the_table():
    stops = _walk(Side.BUY, BUY_TPS, buy_leg(4470.00), buy_snap, [p for p, _, _ in BUY_TABLE])
    assert stops == [stop for _, stop, _ in BUY_TABLE]


def test_walking_the_sell_path_reproduces_the_mirror():
    stops = _walk(Side.SELL, SELL_TPS, sell_leg(4530.00), sell_snap, [p for p, _, _ in SELL_TABLE])
    assert stops == [stop for _, stop, _ in SELL_TABLE]


def test_a_buy_trails_on_bid_not_ask():
    # bid 4489.95 has not reached TP1; ask 4490.05 has. A BUY exits at bid.
    snap = MarketSnapshot(bid=4489.95, ask=4490.05, tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),), snap, SPEC, POLICY) == ()


def test_a_sell_trails_on_ask_not_bid():
    snap = MarketSnapshot(bid=4509.95, ask=4510.05, tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
    assert target_stops(Side.SELL, SELL_TPS, (sell_leg(4530.0),), snap, SPEC, POLICY) == ()


def test_a_fast_move_takes_the_best_step_at_once():
    # From the signal's stop straight to TP3: one move, to TP2, never BE
    # first ("best placeable step first").
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),), buy_snap(4500.00),
                        SPEC, POLICY) == (StopMove(TICKET, 4495.0, "TP2"),)
    assert target_stops(Side.SELL, SELL_TPS, (sell_leg(4530.0),), sell_snap(4500.00),
                        SPEC, POLICY) == (StopMove(TICKET, 4505.0, "TP2"),)


# ---------------------------------------------------------------------------
# The ladder itself: step 1 -> BE at TP1, step k -> TP(k-1) at TPk. The old
# "$2 past TPn" and "halfway to TP(n+1)" rungs are gone.
# ---------------------------------------------------------------------------

def test_the_four_tp_buy_ladder():
    assert ladder(Side.BUY, BUY_TPS) == (
        Rung(4490.0, None, "BE"),
        Rung(4495.0, 4490.0, "TP1"),
        Rung(4500.0, 4495.0, "TP2"),
        Rung(4505.0, 4500.0, "TP3"),
    )


def test_the_four_tp_sell_ladder():
    assert ladder(Side.SELL, SELL_TPS) == (
        Rung(4510.0, None, "BE"),
        Rung(4505.0, 4510.0, "TP1"),
        Rung(4500.0, 4505.0, "TP2"),
        Rung(4495.0, 4500.0, "TP3"),
    )


def test_a_one_tp_signal_has_only_breakeven():
    assert ladder(Side.BUY, (4490.0,)) == (Rung(4490.0, None, "BE"),)
    assert ladder(Side.SELL, (4510.0,)) == (Rung(4510.0, None, "BE"),)


def test_a_two_tp_signal_has_breakeven_and_tp1():
    assert ladder(Side.BUY, (4490.0, 4495.0)) == (
        Rung(4490.0, None, "BE"),
        Rung(4495.0, 4490.0, "TP1"),
    )
    assert ladder(Side.SELL, (4510.0, 4505.0)) == (
        Rung(4510.0, None, "BE"),
        Rung(4505.0, 4510.0, "TP1"),
    )


FIVE_TPS = (4490.0, 4495.0, 4500.0, 4505.0, 4510.0)


def test_a_five_tp_signal_no_longer_alternates():
    # Step 5 moves the stop to TP4 at TP5 itself, not halfway to it.
    assert ladder(Side.BUY, FIVE_TPS) == (
        Rung(4490.0, None, "BE"),
        Rung(4495.0, 4490.0, "TP1"),
        Rung(4500.0, 4495.0, "TP2"),
        Rung(4505.0, 4500.0, "TP3"),
        Rung(4510.0, 4505.0, "TP4"),
    )
    leg = buy_leg(4500.0, tp=4510.0)   # the TP5 position, stop at TP3
    # 4507.50 was the old "halfway from TP4 to TP5" rung.
    assert target_stops(Side.BUY, FIVE_TPS, (leg,), buy_snap(4507.50), SPEC, POLICY) == ()
    assert target_stops(Side.BUY, FIVE_TPS, (leg,), buy_snap(4509.99), SPEC, POLICY) == ()
    assert target_stops(Side.BUY, FIVE_TPS, (leg,), buy_snap(4510.00), SPEC, POLICY) == (
        StopMove(TICKET, 4505.0, "TP4"),)


def test_a_six_tp_sell_steps_one_tp_behind_every_time():
    tps = (4510.0, 4505.0, 4500.0, 4495.0, 4490.0, 4485.0)
    assert ladder(Side.SELL, tps) == (
        Rung(4510.0, None, "BE"),
        Rung(4505.0, 4510.0, "TP1"),
        Rung(4500.0, 4505.0, "TP2"),
        Rung(4495.0, 4500.0, "TP3"),
        Rung(4490.0, 4495.0, "TP4"),
        Rung(4485.0, 4490.0, "TP5"),
    )


def test_the_old_gap_and_halfway_rungs_are_gone():
    # bid 4492 was "$2 past TP1" (stop to TP1), 4497.50 "halfway from TP2
    # to TP3" (stop to TP2), 4502 "$2 past TP3" (stop to TP3).
    at_be, at_tp1, at_tp2 = buy_leg(4482.65), buy_leg(4490.0), buy_leg(4495.0)
    assert target_stops(Side.BUY, BUY_TPS, (at_be,), buy_snap(4492.00), SPEC, POLICY) == ()
    assert target_stops(Side.BUY, BUY_TPS, (at_tp1,), buy_snap(4497.50), SPEC, POLICY) == ()
    assert target_stops(Side.BUY, BUY_TPS, (at_tp2,), buy_snap(4502.00), SPEC, POLICY) == ()


def test_the_early_distances_move_the_triggers_in_usd():
    assert [r.trigger for r in ladder(Side.BUY, BUY_TPS, (0.0, 0.0, 2.0))] == [
        4490.0, 4495.0, 4498.0, 4505.0]
    # A SELL's step is reached that far ABOVE its TP.
    assert [r.trigger for r in ladder(Side.SELL, SELL_TPS, (0.0, 0.0, 2.0))] == [
        4510.0, 4505.0, 4502.0, 4495.0]


@pytest.mark.parametrize("side, tps", [
    (Side.BUY, (4490.0, 4500.0, 4495.0, 4505.0)),   # out of order
    (Side.BUY, (4490.0, 4490.0, 4495.0)),           # not strictly increasing
    (Side.BUY, (4505.0, 4500.0, 4495.0, 4490.0)),   # a sell's order on a buy
    (Side.SELL, (4495.0, 4500.0, 4505.0, 4510.0)),  # a buy's order on a sell
    (Side.SELL, (4510.0, 4500.0, 4505.0, 4495.0)),
    (Side.BUY, ()),
])
def test_non_monotonic_tps_mean_no_trailing(side, tps):
    assert ladder(side, tps) == ()
    # A price far enough past every level to reach any step, either way,
    # and a position with no take profit, so only the TPs' order stops it.
    snap = buy_snap(4600.00) if side is Side.BUY else sell_snap(4400.00)
    leg = buy_leg(4470.0, tp=0.0) if side is Side.BUY else sell_leg(4530.0, tp=0.0)
    assert target_stops(side, tps, (leg,), snap, SPEC, POLICY) == ()


# ---------------------------------------------------------------------------
# Early triggers: ladder_early_pips, entry k-1 for step k, in pips of $0.10.
# Missing entries are 0: on hit.
# ---------------------------------------------------------------------------

EARLY_TP3 = Policy(ladder_early_pips=(0, 0, 20))


def test_twenty_pips_early_moves_the_stop_to_tp2_two_dollars_before_tp3():
    # The amendment's row: with (0, 0, 20), at bid 4498.00 the stop moves to 4495.00.
    leg = buy_leg(4490.0)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4497.99), SPEC, EARLY_TP3) == ()
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4498.00), SPEC, EARLY_TP3) == (
        StopMove(TICKET, 4495.0, "TP2"),)


def test_twenty_pips_early_on_a_sell():
    leg = sell_leg(4510.0)
    assert target_stops(Side.SELL, SELL_TPS, (leg,), sell_snap(4502.01), SPEC, EARLY_TP3) == ()
    assert target_stops(Side.SELL, SELL_TPS, (leg,), sell_snap(4502.00), SPEC, EARLY_TP3) == (
        StopMove(TICKET, 4505.0, "TP2"),)


def test_steps_without_an_early_entry_fire_on_hit():
    # (0, 0, 20) says nothing about step 4: TP4 itself, 4505, is needed.
    leg = buy_leg(4495.0, tp=0.0)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4504.99), SPEC, EARLY_TP3) == ()
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4505.00), SPEC, EARLY_TP3) == (
        StopMove(TICKET, 4500.0, "TP3"),)
    # Nor do its zeros move steps 1 and 2.
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),), buy_snap(4489.99),
                        SPEC, EARLY_TP3) == ()


def test_the_first_entry_is_the_breakeven_steps():
    pol = Policy(ladder_early_pips=(20,))
    leg = buy_leg(4470.0)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4487.99), SPEC, pol) == ()
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4488.00), SPEC, pol) == (
        StopMove(TICKET, 4482.65, "BE"),)


def test_an_early_step_whose_stop_is_not_behind_price_yet_falls_through():
    # 60 pips early, step 3 is reached at 4494.00 -- but its stop, TP2 4495,
    # is above the bid there. The step is skipped, so the stop never stalls
    # on it: TP2 itself still moves it to TP1, and step 3 takes it to TP2
    # once 4495 is far enough below the bid.
    pol = Policy(ladder_early_pips=(0, 0, 60))
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4482.65),), buy_snap(4494.00),
                        SPEC, pol) == ()
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4482.65),), buy_snap(4495.00),
                        SPEC, pol) == (StopMove(TICKET, 4490.0, "TP1"),)
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4490.0),), buy_snap(4495.50),
                        SPEC, pol) == (StopMove(TICKET, 4495.0, "TP2"),)


# ---------------------------------------------------------------------------
# Breakeven: the position's own fill +/- be_buffer_pips x $0.10, never past
# TP1, with no spread term. One formula, shared with the channel's "SL to BE".
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side, fill, tp1, pol, expected", [
    (Side.BUY, 4482.55, 4490.0, POLICY, 4482.65),
    (Side.SELL, 4517.45, 4510.0, POLICY, 4517.35),
    (Side.BUY, 4482.55, 4490.0, Policy(be_buffer_pips=0), 4482.55),
    (Side.BUY, 4482.55, 4490.0, Policy(be_buffer_pips=2.5), 4482.80),
    (Side.SELL, 4517.45, 4510.0, Policy(be_buffer_pips=2.5), 4517.20),
    # Capped at TP1: a fill 5 cents short of it gets TP1 itself, not 5 past.
    (Side.BUY, 4489.95, 4490.0, POLICY, 4490.00),
    (Side.SELL, 4510.05, 4510.0, POLICY, 4510.00),
    # 100 pips is $10.00: 4482.55 + 10 = 4492.55, past TP1, so TP1.
    (Side.BUY, 4482.55, 4490.0, Policy(be_buffer_pips=100), 4490.00),
    (Side.SELL, 4517.45, 4510.0, Policy(be_buffer_pips=100), 4510.00),
], ids=["buy", "sell", "no buffer", "2.5 pips", "sell 2.5 pips", "buy capped",
        "sell capped", "buy big buffer capped", "sell big buffer capped"])
def test_breakeven_is_the_fill_plus_the_buffer_never_past_tp1(side, fill, tp1, pol, expected):
    assert breakeven(side, fill, tp1, SPEC, pol) == expected


def test_the_buffer_is_ten_cents_on_a_two_and_a_three_digit_feed():
    # The 10x trap: a pip read from the feed's point would be $0.01 on Exness.
    assert breakeven(Side.BUY, 4482.55, 4490.0, SPEC, POLICY) == 4482.65
    assert breakeven(Side.BUY, 4482.55, 4490.0, EXNESS, POLICY) == 4482.65
    assert breakeven(Side.BUY, 4482.553, 4490.0, EXNESS, POLICY) == 4482.653
    assert breakeven(Side.SELL, 4517.447, 4510.0, EXNESS, POLICY) == 4517.347


def test_a_capped_breakeven_is_what_the_trail_sends():
    # A late fill at 4489.95: fill + 1 pip would be 4490.05, past TP1.
    leg = buy_leg(4470.0, price_open=4489.95)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4492.00), SPEC, POLICY) == (
        StopMove(TICKET, 4490.00, "BE"),)
    leg = sell_leg(4530.0, price_open=4510.05)
    assert target_stops(Side.SELL, SELL_TPS, (leg,), sell_snap(4508.00), SPEC, POLICY) == (
        StopMove(TICKET, 4510.00, "BE"),)
    big = Policy(be_buffer_pips=100)
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),), buy_snap(4494.99), SPEC, big) == (
        StopMove(TICKET, 4490.00, "BE"),)


@pytest.mark.parametrize("spread", [0.10, 0.40, 1.00])
def test_breakeven_has_no_spread_term(spread):
    # At bid 4492 only step 1 is reached; whatever the spread, BE is
    # 4482.55 + 0.10. (A spread of 1.00 makes the minimum distance 3.00,
    # and BE is 9.35 below the bid.)
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),), buy_snap(4492.00, spread),
                         SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4482.65, "BE"),)
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(4530.0),), sell_snap(4508.00, spread),
                         SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4517.35, "BE"),)


def test_two_legs_get_their_own_breakeven():
    legs = (buy_leg(4470.0, price_open=4482.55, ticket=1),
            buy_leg(4470.0, price_open=4483.10, ticket=2))
    moves = target_stops(Side.BUY, BUY_TPS, legs, buy_snap(4490.00), SPEC, POLICY)
    assert moves == (StopMove(1, 4482.65, "BE"), StopMove(2, 4483.20, "BE"))


def test_two_sell_legs_get_their_own_breakeven():
    legs = (sell_leg(4530.0, price_open=4517.45, ticket=1),
            sell_leg(4530.0, price_open=4516.90, ticket=2))
    moves = target_stops(Side.SELL, SELL_TPS, legs, sell_snap(4510.00), SPEC, POLICY)
    assert moves == (StopMove(1, 4517.35, "BE"), StopMove(2, 4516.80, "BE"))


def test_one_leg_already_at_target_is_left_alone_while_the_other_moves():
    legs = (buy_leg(4482.65, price_open=4482.55, ticket=1),
            buy_leg(4470.00, price_open=4483.10, ticket=2))
    moves = target_stops(Side.BUY, BUY_TPS, legs, buy_snap(4490.00), SPEC, POLICY)
    assert moves == (StopMove(2, 4483.20, "BE"),)


def test_the_follow_up_uses_the_same_breakeven_function():
    # One formula: the channel's "SL to BE" calls trailing.breakeven itself.
    assert followup_planner.breakeven is trailing.breakeven
    assert not hasattr(followup_planner, "_breakeven_price")


@pytest.mark.parametrize("side, price_open, snap", [
    (Side.BUY, 4482.55, buy_snap(4490.00)),
    (Side.SELL, 4517.45, sell_snap(4510.00)),
    (Side.BUY, 4489.95, buy_snap(4492.00)),    # capped at TP1
    (Side.SELL, 4510.05, sell_snap(4508.00)),
])
def test_breakeven_matches_the_channels_sl_to_be(side, price_open, snap):
    tps = BUY_TPS if side is Side.BUY else SELL_TPS
    view = LegView(position_ticket=TICKET, leg_index=0, tp_index=3, side=side, volume=0.01,
                   price_open=price_open, sl=4470.0 if side is Side.BUY else 4530.0,
                   tp=tps[3], is_pending=False)
    fu = FollowUp(channel_id=-1, message_id=1, op=FollowUpOp.SL_TO_BE, posted_at_utc=NOW)
    channel_be = plan_followup(fu, (view,), SPEC, snap, POLICY, tps=tps).intents[0].sl
    leg = OpenLeg(TICKET, price_open, view.sl, view.tp)
    (move,) = target_stops(side, tps, (leg,), snap, SPEC, POLICY)
    assert move.rung == "BE"
    assert move.new_sl == channel_be


# ---------------------------------------------------------------------------
# A spread spike causes no repeated modify: breakeven no longer depends on
# the spread, so once placed it is never "tighter" again.
# ---------------------------------------------------------------------------

SPREAD_SPIKE = (0.10, 0.11, 0.12, 0.20, 0.50, 1.00, 2.00, 0.10)


def _hold_price_through_the_spike(side, tps, leg, snap_at, price):
    """Every tick at one price while the spread spikes and settles, feeding
    each move back in as the current stop. Returns each tick's moves."""
    per_tick = []
    for spread in SPREAD_SPIKE:
        moves = target_stops(side, tps, (leg,), snap_at(price, spread=spread), SPEC, POLICY)
        for move in moves:
            leg = dataclasses.replace(leg, current_sl=move.new_sl)
        per_tick.append(moves)
    return per_tick


@pytest.mark.parametrize("side, tps, leg, snap_at, price, first", [
    (Side.BUY, BUY_TPS, buy_leg(4470.0), buy_snap, 4491.50, StopMove(TICKET, 4482.65, "BE")),
    (Side.SELL, SELL_TPS, sell_leg(4530.0), sell_snap, 4508.50, StopMove(TICKET, 4517.35, "BE")),
    (Side.BUY, BUY_TPS, buy_leg(4470.0), buy_snap, 4497.00, StopMove(TICKET, 4490.0, "TP1")),
    (Side.SELL, SELL_TPS, sell_leg(4530.0), sell_snap, 4503.00, StopMove(TICKET, 4510.0, "TP1")),
], ids=["buy at BE", "sell at BE", "buy at TP1", "sell at TP1"])
def test_a_spread_spike_sends_one_modify_and_no_more(side, tps, leg, snap_at, price, first):
    per_tick = _hold_price_through_the_spike(side, tps, leg, snap_at, price)
    assert per_tick == [(first,)] + [()] * (len(SPREAD_SPIKE) - 1)


# ---------------------------------------------------------------------------
# Placeable: the broker's minimum distance (gates.min_stop_distance_usd),
# strictly tighter, and strictly behind the position's own TP. A step that
# is not placeable falls through to the next lower one.
# ---------------------------------------------------------------------------

def test_a_step_inside_min_distance_falls_through_to_the_next_lower_step():
    # spread 2.00 -> min distance 3 x 2.00 = 6.00. At bid 4495 step 2's
    # stop, TP1 4490, is only 5.00 below: the broker would refuse it.
    # Step 1's breakeven, 4482.65, is 12.35 below and is taken instead.
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),),
                         buy_snap(4495.00, spread=2.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4482.65, "BE"),)


def test_a_sell_step_inside_min_distance_falls_through():
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(4530.0),),
                         sell_snap(4505.00, spread=2.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4517.35, "BE"),)


def test_a_fall_through_to_a_step_already_in_place_moves_nothing():
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4482.65),),
                         buy_snap(4495.00, spread=2.00), SPEC, POLICY)
    assert moves == ()


def test_a_fall_through_skips_every_step_it_cannot_place():
    # At bid 4500 with spread 2.00 (min 6.00): step 3's TP2 (5.00 below)
    # is refused, step 2's TP1 (10.00 below) is taken.
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),),
                         buy_snap(4500.00, spread=2.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4490.0, "TP1"),)


def test_no_move_when_every_reached_step_is_inside_min_distance():
    # spread 3.00 -> min distance 9.00; BE (4482.65) is 7.35 below bid.
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),),
                         buy_snap(4490.00, spread=3.00), SPEC, POLICY)
    assert moves == ()


def test_the_brokers_stops_level_counts_too():
    # stops_level 600 points on a 2-digit feed = 6.00 USD, wider than
    # 3 x spread (0.30): TP1 at 5.00 from bid is refused, BE is taken.
    wide = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 600, 0)
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),), buy_snap(4495.00), wide, POLICY)
    assert moves == (StopMove(TICKET, 4482.65, "BE"),)


def test_a_stop_at_or_past_price_is_never_proposed():
    # With no stops level and no spread the minimum distance is zero; a
    # stop at the bid itself is still on the wrong side for the broker.
    snap = MarketSnapshot(bid=4490.00, ask=4490.00, tick_time_utc=NOW,
                          spread_usd=0.0, median_spread_usd=0.10, now_utc=NOW)
    leg = buy_leg(4470.0, price_open=4489.90)  # BE = 4489.90 + 0.10 = 4490.00
    assert target_stops(Side.BUY, BUY_TPS, (leg,), snap, SPEC, POLICY) == ()


def test_a_step_at_the_positions_own_tp_falls_through():
    # The TP2 position, still reported open at bid 4500: step 3's stop,
    # TP2 4495, is its own TP. A stop there is never set; step 2's TP1 is.
    leg = buy_leg(4482.65, tp=4495.0)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4500.00), SPEC, POLICY) == (
        StopMove(TICKET, 4490.0, "TP1"),)
    leg = sell_leg(4517.35, tp=4505.0)
    assert target_stops(Side.SELL, SELL_TPS, (leg,), sell_snap(4500.00), SPEC, POLICY) == (
        StopMove(TICKET, 4510.0, "TP1"),)


def test_a_step_past_the_positions_own_tp_falls_through():
    # A position whose TP was amended to 4489: TP1 4490 is past it, so
    # breakeven, below it, is the step taken.
    leg = buy_leg(4470.0, tp=4489.0)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4495.00), SPEC, POLICY) == (
        StopMove(TICKET, 4482.65, "BE"),)
    leg = sell_leg(4530.0, tp=4511.0)
    assert target_stops(Side.SELL, SELL_TPS, (leg,), sell_snap(4505.00), SPEC, POLICY) == (
        StopMove(TICKET, 4517.35, "BE"),)


def test_a_position_with_no_take_profit_has_no_tp_limit():
    # MT5 reports a missing take profit as 0.0, like a missing stop.
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4482.65, tp=0.0),), buy_snap(4500.00),
                        SPEC, POLICY) == (StopMove(TICKET, 4495.0, "TP2"),)
    assert target_stops(Side.SELL, SELL_TPS, (sell_leg(4517.35, tp=0.0),), sell_snap(4500.00),
                        SPEC, POLICY) == (StopMove(TICKET, 4505.0, "TP2"),)


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
@pytest.mark.parametrize("own_tp_index", [0, 1, 2, 3])
def test_a_stop_is_never_set_at_or_past_the_positions_own_tp(side, own_tp_index):
    # Every position of the basket, walked a quarter-dollar at a time from
    # before TP1 to past TP4 and back, with the stop fed back in each time.
    tps = BUY_TPS if side is Side.BUY else SELL_TPS
    own_tp = tps[own_tp_index]
    leg = buy_leg(4470.0, tp=own_tp) if side is Side.BUY else sell_leg(4530.0, tp=own_tp)
    up = [4488.00 + 0.25 * i for i in range(97)]                # 4488 .. 4512
    prices = up + up[::-1] if side is Side.BUY else [9000.0 - p for p in up + up[::-1]]
    snap_at = buy_snap if side is Side.BUY else sell_snap
    moved = 0
    for price in prices:
        for move in target_stops(side, tps, (leg,), snap_at(price), SPEC, POLICY):
            assert (own_tp - move.new_sl) * side.sign > 0, (price, move)
            assert (move.new_sl - leg.current_sl) * side.sign > 0, (price, move)
            leg = dataclasses.replace(leg, current_sl=move.new_sl)
            moved += 1
    # It still trails: BE, then one step per TP before its own.
    assert moved == own_tp_index + 1


# ---------------------------------------------------------------------------
# The stop only tightens.
# ---------------------------------------------------------------------------

def test_a_reached_step_looser_than_the_current_stop_is_not_sent():
    # A poster moved the stop to 4488 by hand; BE (4482.65) is looser.
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4488.0),), buy_snap(4490.00), SPEC, POLICY)
    assert moves == ()


def test_a_sell_step_looser_than_the_current_stop_is_not_sent():
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(4512.0),), sell_snap(4510.00), SPEC, POLICY)
    assert moves == ()


def test_a_sell_with_no_stop_at_all_still_gets_one():
    # MT5 reports a missing stop as 0.0. Numerically that is "below" every
    # sell stop, but no stop is the loosest stop there is.
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(0.0),), sell_snap(4510.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4517.35, "BE"),)


def test_a_buy_with_no_stop_at_all_still_gets_one():
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(0.0),), buy_snap(4490.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4482.65, "BE"),)


# ---------------------------------------------------------------------------
# The 10x trap: a 3-digit feed (Exness) must give the same USD levels.
# ---------------------------------------------------------------------------

def test_a_three_digit_feed_yields_the_same_levels_in_usd():
    prices = [round(p, 3) for p, _, _ in BUY_TABLE]
    stops = _walk(Side.BUY, BUY_TPS, buy_leg(4470.0), buy_snap, prices, spec=EXNESS)
    assert stops == [stop for _, stop, _ in BUY_TABLE]
    prices = [round(p, 3) for p, _, _ in SELL_TABLE]
    stops = _walk(Side.SELL, SELL_TPS, sell_leg(4530.0), sell_snap, prices, spec=EXNESS)
    assert stops == [stop for _, stop, _ in SELL_TABLE]


def test_a_three_digit_feed_rounds_breakeven_to_three_decimals():
    # 4482.553 + 0.100 = 4482.653 on Exness; a 2-digit feed would round the
    # same arithmetic to 4482.65.
    leg = buy_leg(4470.0, price_open=4482.553)
    moves = target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4490.000), EXNESS, POLICY)
    assert moves == (StopMove(TICKET, 4482.653, "BE"),)
    moves = target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4490.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4482.65, "BE"),)


def test_twenty_pips_early_is_two_dollars_on_a_three_digit_feed_too():
    # 20 points would be 0.020 USD on Exness: step 3 would wait until
    # 4499.980. It must take a full 2.00 USD.
    leg = buy_leg(4490.0)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4497.999), EXNESS, EARLY_TP3) == ()
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4498.000), EXNESS, EARLY_TP3) == (
        StopMove(TICKET, 4495.0, "TP2"),)


def test_the_stops_level_is_read_in_the_feeds_own_points():
    # 600 points is 0.600 USD on Exness (TP1 at 5.00 from bid is fine) but
    # 6.00 USD on a 2-digit feed (TP1 refused, breakeven instead).
    exness = SymbolSpec("XAUUSDm", 3, 0.001, 0.001, 0.01, 100.0, 0.01, 600, 0)
    fxpro = SymbolSpec("GOLD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 600, 0)
    leg = buy_leg(4470.0)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4495.00), exness, POLICY) == (
        StopMove(TICKET, 4490.0, "TP1"),)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4495.00), fxpro, POLICY) == (
        StopMove(TICKET, 4482.65, "BE"),)


def test_levels_are_snapped_to_the_tick_grid():
    # TPs quoted with more precision than the feed carries are rounded like
    # every other price: TP2's trigger (4495.004 raw) is 4495.00 on this
    # feed, so bid 4495.00 has reached it, and TP1 is sent as 4490.00.
    tps = (4490.004, 4495.004, 4500.0, 4505.0)
    moves = target_stops(Side.BUY, tps, (buy_leg(4482.65),), buy_snap(4495.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4490.0, "TP1"),)


# ---------------------------------------------------------------------------
# The switch.
# ---------------------------------------------------------------------------

def test_trail_disabled_means_no_moves():
    pol = Policy(trail_enabled=False)
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),), buy_snap(4502.00), SPEC, pol) == ()
    assert target_stops(Side.SELL, SELL_TPS, (sell_leg(4530.0),), sell_snap(4498.00), SPEC, pol) == ()


def test_no_legs_means_no_moves():
    assert target_stops(Side.BUY, BUY_TPS, (), buy_snap(4502.00), SPEC, POLICY) == ()
