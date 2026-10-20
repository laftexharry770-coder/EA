"""The basket trailing stop (Plan 1b, Task 3).

The worked example in the approved design is the acceptance test: a real
BUY signal, zone 4480-4485, SL 4470, TPs 4490/4495/4500/4505, one position
filled at 4482.55, spread 0.10, be_buffer_usd 0.10, gap 2.00. Breakeven is
4482.55 + 0.10 + 0.10 = 4482.75. The SELL mirror reflects every level
through 4500 (x -> 9000 - x).
"""
from datetime import UTC, datetime

import pytest

from xaucopier.domain.followup_planner import LegView, plan_followup
from xaucopier.domain.market import MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import FollowUp, FollowUpOp, Side
from xaucopier.domain.trailing import OpenLeg, Rung, StopMove, ladder, target_stops

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
EXNESS = SymbolSpec("XAUUSDm", 3, 0.001, 0.001, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()  # be_buffer_usd 0.10, trail_gap_usd 2.00, trail on

BUY_TPS = (4490.0, 4495.0, 4500.0, 4505.0)
SELL_TPS = (4510.0, 4505.0, 4500.0, 4495.0)
TICKET = 7001


def buy_snap(bid, spread=0.10):
    return MarketSnapshot(bid=bid, ask=round(bid + spread, 3), tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=0.10, now_utc=NOW)


def sell_snap(ask, spread=0.10):
    return MarketSnapshot(bid=round(ask - spread, 3), ask=ask, tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=0.10, now_utc=NOW)


def buy_leg(current_sl, price_open=4482.55, ticket=TICKET):
    return OpenLeg(position_ticket=ticket, price_open=price_open, current_sl=current_sl)


def sell_leg(current_sl, price_open=4517.45, ticket=TICKET):
    return OpenLeg(position_ticket=ticket, price_open=price_open, current_sl=current_sl)


# ---------------------------------------------------------------------------
# The worked example, one test per row. Each row starts from the stop the
# row before it left in place (the first from the signal's SL), so a row
# that earns nothing new must emit nothing at all.
# ---------------------------------------------------------------------------

# (bid, the stop in place afterwards, the rung that put it there)
BUY_TABLE = (
    (4489.99, 4470.00, None),    # no rung earned
    (4490.00, 4482.75, "BE"),    # breakeven: fill + spread + buffer
    (4491.99, 4482.75, None),
    (4492.00, 4490.00, "TP1"),   # TP1 + gap
    (4497.49, 4490.00, None),
    (4497.50, 4495.00, "TP2"),   # midpoint(TP2, TP3)
    (4501.99, 4495.00, None),
    (4502.00, 4500.00, "TP3"),   # TP3 + gap
    (4495.00, 4500.00, None),    # the retrace: never loosens
)

# The exact mirror: ask, x -> 9000 - x. SL 4530, fill 4517.45, BE 4517.25.
SELL_TABLE = (
    (4510.01, 4530.00, None),
    (4510.00, 4517.25, "BE"),
    (4508.01, 4517.25, None),
    (4508.00, 4510.00, "TP1"),
    (4502.51, 4510.00, None),
    (4502.50, 4505.00, "TP2"),
    (4498.01, 4505.00, None),
    (4498.00, 4500.00, "TP3"),
    (4505.00, 4500.00, None),    # the retrace: never loosens
)


def _rows_with_prior(table, signal_sl):
    prior = (signal_sl,) + tuple(stop for _, stop, _ in table[:-1])
    return [(price, before, stop, label)
            for (price, stop, label), before in zip(table, prior)]


def _expected(before, stop, label):
    return () if label is None else (StopMove(TICKET, stop, label),)


@pytest.mark.parametrize("bid, before, stop, label", _rows_with_prior(BUY_TABLE, 4470.00),
                         ids=[f"bid {row[0]:.2f}" for row in BUY_TABLE])
def test_worked_example_buy_row(bid, before, stop, label):
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(before),), buy_snap(bid), SPEC, POLICY)
    assert moves == _expected(before, stop, label)


@pytest.mark.parametrize("ask, before, stop, label", _rows_with_prior(SELL_TABLE, 4530.00),
                         ids=[f"ask {row[0]:.2f}" for row in SELL_TABLE])
def test_worked_example_sell_mirror_row(ask, before, stop, label):
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(before),), sell_snap(ask), SPEC, POLICY)
    assert moves == _expected(before, stop, label)


def _walk(side, tps, leg, snap_at, table, spec=SPEC):
    """Feed each move back in as the new current stop, the way the worker
    will, and return the stop in place after every step."""
    stops = []
    for price, _, _ in table:
        moves = target_stops(side, tps, (leg,), snap_at(price), spec, POLICY)
        for move in moves:
            assert move.position_ticket == leg.position_ticket
            leg = OpenLeg(leg.position_ticket, leg.price_open, move.new_sl)
        stops.append(leg.current_sl)
    return stops


def test_walking_the_buy_path_reproduces_the_table():
    stops = _walk(Side.BUY, BUY_TPS, buy_leg(4470.00), buy_snap, BUY_TABLE)
    assert stops == [stop for _, stop, _ in BUY_TABLE]


def test_walking_the_sell_path_reproduces_the_mirror():
    stops = _walk(Side.SELL, SELL_TPS, sell_leg(4530.00), sell_snap, SELL_TABLE)
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


# ---------------------------------------------------------------------------
# Breakeven is per position.
# ---------------------------------------------------------------------------

def test_two_legs_get_their_own_breakeven():
    legs = (buy_leg(4470.0, price_open=4482.55, ticket=1),
            buy_leg(4470.0, price_open=4483.10, ticket=2))
    moves = target_stops(Side.BUY, BUY_TPS, legs, buy_snap(4490.00), SPEC, POLICY)
    assert moves == (StopMove(1, 4482.75, "BE"), StopMove(2, 4483.30, "BE"))


def test_two_sell_legs_get_their_own_breakeven():
    legs = (sell_leg(4530.0, price_open=4517.45, ticket=1),
            sell_leg(4530.0, price_open=4516.90, ticket=2))
    moves = target_stops(Side.SELL, SELL_TPS, legs, sell_snap(4510.00), SPEC, POLICY)
    assert moves == (StopMove(1, 4517.25, "BE"), StopMove(2, 4516.70, "BE"))


def test_one_leg_already_at_target_is_left_alone_while_the_other_moves():
    legs = (buy_leg(4482.75, price_open=4482.55, ticket=1),
            buy_leg(4470.00, price_open=4483.10, ticket=2))
    moves = target_stops(Side.BUY, BUY_TPS, legs, buy_snap(4490.00), SPEC, POLICY)
    assert moves == (StopMove(2, 4483.30, "BE"),)


@pytest.mark.parametrize("side, price_open, snap", [
    (Side.BUY, 4482.55, buy_snap(4490.00)),
    (Side.SELL, 4517.45, sell_snap(4510.00)),
])
def test_breakeven_matches_the_channels_sl_to_be(side, price_open, snap):
    # Two modules compute breakeven; pin them together so neither drifts.
    view = LegView(position_ticket=TICKET, leg_index=0, tp_index=0, side=side, volume=0.01,
                   price_open=price_open, sl=4470.0 if side is Side.BUY else 4530.0,
                   tp=4505.0 if side is Side.BUY else 4495.0, is_pending=False)
    fu = FollowUp(channel_id=-1, message_id=1, op=FollowUpOp.SL_TO_BE, posted_at_utc=NOW)
    channel_be = plan_followup(fu, (view,), SPEC, snap, POLICY).intents[0].sl
    tps = BUY_TPS if side is Side.BUY else SELL_TPS
    leg = OpenLeg(TICKET, price_open, view.sl)
    (move,) = target_stops(side, tps, (leg,), snap, SPEC, POLICY)
    assert move.rung == "BE"
    assert move.new_sl == channel_be


# ---------------------------------------------------------------------------
# The broker's minimum distance: gates.min_stop_distance_usd.
# ---------------------------------------------------------------------------

def test_a_rung_inside_min_distance_falls_back_to_the_next_tightest():
    # spread 1.00 -> min distance 3 x 1.00 = 3.00. TP1 (4490) is only 2.00
    # below bid 4492, so the broker would refuse it; breakeven
    # (4482.55 + 1.00 + 0.10 = 4483.65) is 8.35 below and is taken instead.
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),),
                         buy_snap(4492.00, spread=1.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4483.65, "BE"),)


def test_a_sell_rung_inside_min_distance_falls_back_to_the_next_tightest():
    # Mirror: TP1 (4510) is 2.00 above ask 4508; BE = 4517.45 - 1.10.
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(4530.0),),
                         sell_snap(4508.00, spread=1.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4516.35, "BE"),)


def test_no_move_when_every_earned_rung_is_inside_min_distance():
    # spread 3.00 -> min distance 9.00; BE (4485.65) is 4.35 below bid.
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),),
                         buy_snap(4490.00, spread=3.00), SPEC, POLICY)
    assert moves == ()


def test_the_brokers_stops_level_counts_too():
    # stops_level 300 points on a 2-digit feed = 3.00 USD, wider than
    # 3 x spread (0.30): TP1 at 2.00 from bid is refused, BE is taken.
    wide = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 300, 0)
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4470.0),), buy_snap(4492.00), wide, POLICY)
    assert moves == (StopMove(TICKET, 4482.75, "BE"),)


def test_a_stop_at_or_past_price_is_never_proposed():
    # With no stops level and no spread the minimum distance is zero; a
    # stop at the bid itself is still on the wrong side for the broker.
    snap = MarketSnapshot(bid=4490.00, ask=4490.00, tick_time_utc=NOW,
                          spread_usd=0.0, median_spread_usd=0.10, now_utc=NOW)
    leg = buy_leg(4470.0, price_open=4489.90)  # BE = 4489.90 + 0.10 = 4490.00
    assert target_stops(Side.BUY, BUY_TPS, (leg,), snap, SPEC, POLICY) == ()


# ---------------------------------------------------------------------------
# The stop only tightens.
# ---------------------------------------------------------------------------

def test_an_earned_rung_looser_than_the_current_stop_is_not_sent():
    # A poster moved the stop to 4488 by hand; BE (4482.75) is looser.
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(4488.0),), buy_snap(4490.00), SPEC, POLICY)
    assert moves == ()


def test_a_sell_rung_looser_than_the_current_stop_is_not_sent():
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(4512.0),), sell_snap(4510.00), SPEC, POLICY)
    assert moves == ()


def test_a_sell_with_no_stop_at_all_still_gets_one():
    # MT5 reports a missing stop as 0.0. Numerically that is "below" every
    # sell stop, but no stop is the loosest stop there is.
    moves = target_stops(Side.SELL, SELL_TPS, (sell_leg(0.0),), sell_snap(4510.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4517.25, "BE"),)


def test_a_buy_with_no_stop_at_all_still_gets_one():
    moves = target_stops(Side.BUY, BUY_TPS, (buy_leg(0.0),), buy_snap(4490.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4482.75, "BE"),)


# ---------------------------------------------------------------------------
# The ladder itself.
# ---------------------------------------------------------------------------

def test_the_four_tp_buy_ladder():
    assert ladder(Side.BUY, BUY_TPS, 2.00) == (
        Rung(4490.0, None, "BE"),
        Rung(4492.0, 4490.0, "TP1"),   # odd: gap past
        Rung(4497.5, 4495.0, "TP2"),   # even: halfway to TP3
        Rung(4502.0, 4500.0, "TP3"),   # odd: gap past; no rung for TP4
    )


def test_the_four_tp_sell_ladder():
    assert ladder(Side.SELL, SELL_TPS, 2.00) == (
        Rung(4510.0, None, "BE"),
        Rung(4508.0, 4510.0, "TP1"),
        Rung(4502.5, 4505.0, "TP2"),
        Rung(4498.0, 4500.0, "TP3"),
    )


def test_a_one_tp_signal_has_only_breakeven():
    assert ladder(Side.BUY, (4490.0,), 2.00) == (Rung(4490.0, None, "BE"),)


def test_a_two_tp_signal_has_breakeven_and_tp1():
    assert ladder(Side.BUY, (4490.0, 4495.0), 2.00) == (
        Rung(4490.0, None, "BE"),
        Rung(4492.0, 4490.0, "TP1"),
    )
    assert ladder(Side.SELL, (4510.0, 4505.0), 2.00) == (
        Rung(4510.0, None, "BE"),
        Rung(4508.0, 4510.0, "TP1"),
    )


def test_a_five_tp_signal_alternates():
    assert ladder(Side.BUY, (4490.0, 4495.0, 4500.0, 4505.0, 4510.0), 2.00) == (
        Rung(4490.0, None, "BE"),
        Rung(4492.0, 4490.0, "TP1"),
        Rung(4497.5, 4495.0, "TP2"),
        Rung(4502.0, 4500.0, "TP3"),
        Rung(4507.5, 4505.0, "TP4"),   # TP4 even: halfway to TP5
    )


def test_a_six_tp_sell_keeps_alternating():
    assert ladder(Side.SELL, (4510.0, 4505.0, 4500.0, 4495.0, 4490.0, 4485.0), 2.00) == (
        Rung(4510.0, None, "BE"),
        Rung(4508.0, 4510.0, "TP1"),
        Rung(4502.5, 4505.0, "TP2"),
        Rung(4498.0, 4500.0, "TP3"),
        Rung(4492.5, 4495.0, "TP4"),   # even: halfway to TP5
        Rung(4488.0, 4490.0, "TP5"),   # odd: gap past
    )


def test_the_gap_comes_from_the_argument():
    rungs = ladder(Side.BUY, BUY_TPS, 3.00)
    assert [r.trigger for r in rungs] == [4490.0, 4493.0, 4497.5, 4503.0]


def test_the_trail_uses_the_policys_gap():
    pol = Policy(trail_gap_usd=3.00)
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4482.75),), buy_snap(4492.00), SPEC, pol) == ()
    assert target_stops(Side.BUY, BUY_TPS, (buy_leg(4482.75),), buy_snap(4493.00), SPEC, pol) == (
        StopMove(TICKET, 4490.00, "TP1"),)


@pytest.mark.parametrize("side, tps", [
    (Side.BUY, (4490.0, 4500.0, 4495.0, 4505.0)),   # out of order
    (Side.BUY, (4490.0, 4490.0, 4495.0)),           # not strictly increasing
    (Side.BUY, (4505.0, 4500.0, 4495.0, 4490.0)),   # a sell's order on a buy
    (Side.SELL, (4495.0, 4500.0, 4505.0, 4510.0)),  # a buy's order on a sell
    (Side.SELL, (4510.0, 4500.0, 4505.0, 4495.0)),
    (Side.BUY, ()),
])
def test_non_monotonic_tps_mean_no_trailing(side, tps):
    assert ladder(side, tps, 2.00) == ()
    # A price far enough past every level to earn any rung, either way.
    snap = buy_snap(4600.00) if side is Side.BUY else sell_snap(4400.00)
    leg = buy_leg(4470.0) if side is Side.BUY else sell_leg(4530.0)
    assert target_stops(side, tps, (leg,), snap, SPEC, POLICY) == ()


# ---------------------------------------------------------------------------
# The 10x trap: a 3-digit feed (Exness) must give the same USD levels.
# ---------------------------------------------------------------------------

def test_a_three_digit_feed_yields_the_same_levels_in_usd():
    exness_table = tuple((round(p, 3), s, lab) for p, s, lab in BUY_TABLE)
    stops = _walk(Side.BUY, BUY_TPS, buy_leg(4470.0), buy_snap, exness_table, spec=EXNESS)
    assert stops == [stop for _, stop, _ in BUY_TABLE]


def test_a_three_digit_feed_rounds_breakeven_to_three_decimals():
    # 4482.553 + 0.10 + 0.10 = 4482.753 on Exness; a 2-digit feed would
    # round the same arithmetic to 4482.75.
    leg = buy_leg(4470.0, price_open=4482.553)
    moves = target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4490.000), EXNESS, POLICY)
    assert moves == (StopMove(TICKET, 4482.753, "BE"),)
    moves = target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4490.00), SPEC, POLICY)
    assert moves == (StopMove(TICKET, 4482.75, "BE"),)


def test_the_gap_is_two_dollars_on_a_three_digit_feed_not_two_hundred_points():
    # 200 points would be 0.200 USD on Exness: TP1 would be earned at
    # 4490.200. It must take a full 2.00 USD.
    leg = buy_leg(4482.75)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4491.999), EXNESS, POLICY) == ()
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4492.000), EXNESS, POLICY) == (
        StopMove(TICKET, 4490.0, "TP1"),)


def test_the_stops_level_is_read_in_the_feeds_own_points():
    # 300 points is 0.300 USD on Exness (TP1 at 2.00 from bid is fine) but
    # 3.00 USD on a 2-digit feed (TP1 refused, breakeven instead).
    exness = SymbolSpec("XAUUSDm", 3, 0.001, 0.001, 0.01, 100.0, 0.01, 300, 0)
    fxpro = SymbolSpec("GOLD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 300, 0)
    leg = buy_leg(4470.0)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4492.00), exness, POLICY) == (
        StopMove(TICKET, 4490.0, "TP1"),)
    assert target_stops(Side.BUY, BUY_TPS, (leg,), buy_snap(4492.00), fxpro, POLICY) == (
        StopMove(TICKET, 4482.75, "BE"),)


def test_levels_are_snapped_to_the_tick_grid():
    # A TP quoted with more precision than the feed carries is rounded like
    # every other price: the stop is sent as 4490.00, and the trigger
    # (4492.004 raw) is 4492.00 on this feed, so bid 4492.00 has earned it.
    tps = (4490.004, 4495.0, 4500.0, 4505.0)
    moves = target_stops(Side.BUY, tps, (buy_leg(4482.75),), buy_snap(4492.00), SPEC, POLICY)
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
