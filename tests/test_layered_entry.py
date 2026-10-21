# tests/test_layered_entry.py
"""Plan 1b Task 2b: layered entry (the user's amendment of 2026-09-25).

Of the N positions, ceil(N/2) open NOW and floor(N/2) rest as PENDING limit
orders. The N targets come from distribute() as before; in TP order,
position ordinals 0, 2, 4... are NOW and 1, 3, 5... are PENDING.

For a BUY (a SELL mirrors it, with the bid as its price):
- inside the zone: NOW at market; PENDING limits spread from the current
  ask minus the broker's minimum distance down to zone.lo, both inclusive
  (one pending order goes to the middle). No room there: all open now.
- above the zone: NOW at market within the chase budget, else limits at
  the near edge (zone.hi); PENDING spread over the whole zone.
- below the zone: all N open now at market.
Every limit expires pending_max_age_hours after the post. A pending order
still unfilled is cancelled once price reaches TP1 or once it expires.

`layered_entry = false` keeps today's single rule exactly, and it is what
the first section of this file pins down.
"""
from datetime import datetime, timedelta, UTC

import pytest

from xaucopier.domain import gates
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.intent import OrderIntent, OrderKind, Plan
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price, usd_to_points
from xaucopier.domain.planner import (
    distribute,
    pending_levels,
    pending_should_cancel,
    plan_entry,
    split_now_pending,
)
from xaucopier.domain.signal import EntryZone, Side, Signal

NOW = datetime(2026, 9, 5, 10, 45, tzinfo=UTC)
POSTED = NOW - timedelta(seconds=5)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
EXNESS = SymbolSpec("XAUUSDm", 3, 0.001, 0.001, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()
ZONE = EntryZone(4480.0, 4485.0)
BUY_TPS = (4490.0, 4495.0, 4500.0, 4505.0)
SELL_TPS = (4475.0, 4470.0, 4465.0, 4460.0)


def _b36(n):
    # An independent base36 encoder, so these tests do not trust the
    # planner's own.
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
        if n == 0:
            return out


def acct(positions=4, lot=0.01, layered=True):
    return AccountSpec("ic_demo", "XAUUSD", fixed_lot=lot, magic_base=77001,
                       positions=positions, layered_entry=layered)


def buy(message_id=4001, tps=BUY_TPS):
    return Signal(channel_id=-1, message_id=message_id, side=Side.BUY,
                  symbol_alias="XAUUSD", zone=ZONE, sl=4470.0, tps=tps,
                  posted_at_utc=POSTED, content_hash="h")


def sell(message_id=4002, tps=SELL_TPS):
    return Signal(channel_id=-1, message_id=message_id, side=Side.SELL,
                  symbol_alias="XAUUSD", zone=ZONE, sl=4495.0, tps=tps,
                  posted_at_utc=POSTED, content_hash="h")


def at_ask(ask, spread=0.10, median=None):
    """A BUY's market: the ask is the price that matters."""
    return MarketSnapshot(bid=round(ask - spread, 3), ask=ask, tick_time_utc=NOW,
                          spread_usd=spread,
                          median_spread_usd=spread if median is None else median,
                          now_utc=NOW)


def at_bid(bid, spread=0.10, median=None):
    """A SELL's market: the bid is the price that matters."""
    return MarketSnapshot(bid=bid, ask=round(bid + spread, 3), tick_time_utc=NOW,
                          spread_usd=spread,
                          median_spread_usd=spread if median is None else median,
                          now_utc=NOW)


# ---------------------------------------------------------------------------
# layered_entry = false: today's single rule, exactly (ruling P2).
#
# The oracle below is the rule as the planner had it before Task 2b, written
# out on its own: market when price is inside or better than the zone, else
# every leg one limit at the favourable (or, by policy, the near) edge,
# expiring pending_max_age_hours after the post. It was run against the
# unchanged planner first, so it is today's behaviour, not a guess at it.
# ---------------------------------------------------------------------------

def _single_rule_plan(sig, account, spec, snap, pol):
    buying = sig.side is Side.BUY
    price = snap.ask if buying else snap.bid
    survivors = tuple((i, tp) for i, tp in enumerate(sig.tps)
                      if (tp > price if buying else tp < price))
    targets = distribute(account.positions, survivors)
    if (price <= sig.zone.hi) if buying else (price >= sig.zone.lo):
        kind, entry, expires = OrderKind.MARKET, price, None
    else:
        favourable = sig.zone.lo if buying else sig.zone.hi
        near = sig.zone.hi if buying else sig.zone.lo
        entry = favourable if pol.pending_at == "favourable" else near
        kind = OrderKind.LIMIT
        expires = sig.posted_at_utc + timedelta(hours=pol.pending_max_age_hours)
    legs = tuple(
        OrderIntent(
            account_id=account.account_id, symbol=account.symbol, side=sig.side,
            kind=kind, volume=account.fixed_lot, price=round_price(entry, spec),
            sl=round_price(sig.sl, spec), tp=round_price(tp, spec),
            leg_index=leg, tp_index=tp_index, magic=account.magic_base,
            comment=f"{_b36(sig.message_id)}-{leg}t{tp_index}",
            deviation_points=usd_to_points(pol.max_slippage_usd, spec),
            expires_at_utc=expires,
        )
        for leg, (tp_index, tp) in enumerate(targets)
    )
    passed = len(sig.tps) - len(survivors)
    notes = (f"{passed} take profit(s) already passed",) if passed else ()
    return Plan.accept(legs, notes)


# BUY asks from better than the zone, through it, to past TP2; each SELL bid
# is the mirror image about the zone's middle (4482.50).
_BUY_ASKS = (4478.0, 4480.0, 4482.10, 4483.0, 4485.0, 4485.10, 4486.0,
             4486.25, 4487.50, 4491.0, 4496.0)
_SELL_BIDS = tuple(round(8965.0 - a, 2) for a in _BUY_ASKS)
_SINGLE_RULE_CASES = (
    [("buy", a) for a in _BUY_ASKS] + [("sell", b) for b in _SELL_BIDS]
)


@pytest.mark.parametrize("spec", [SPEC, EXNESS], ids=["2-digit", "3-digit"])
@pytest.mark.parametrize("pending_at", ["favourable", "near"])
@pytest.mark.parametrize("positions", [None, 1, 2, 3, 4, 6])
@pytest.mark.parametrize(("side", "price"), _SINGLE_RULE_CASES)
def test_layered_entry_false_reproduces_the_single_rule_exactly(side, price, positions,
                                                                pending_at, spec):
    sig = buy() if side == "buy" else sell()
    snap = at_ask(price) if side == "buy" else at_bid(price)
    pol = Policy(pending_at=pending_at)
    account = acct(positions, layered=False)
    plan = plan_entry(sig, account, spec, snap, pol)
    assert plan.accepted
    # Plan and OrderIntent are frozen dataclasses: == compares every field
    # of every leg -- kind, price, sl, tp, both indexes, lot, magic,
    # comment, deviation and expiry -- and the notes.
    assert plan == _single_rule_plan(sig, account, spec, snap, pol)


# ---------------------------------------------------------------------------
# Helpers for the layered tests.
# ---------------------------------------------------------------------------

M, L = "market", "limit"
EXPIRES = POSTED + timedelta(hours=4)   # pending_max_age_hours = 4 (default)


def shape(plan):
    """Each leg, in leg order, as (kind, entry price, tp_index)."""
    return [(leg.kind.value, leg.price, leg.tp_index) for leg in plan.legs]


def assert_every_leg_is_well_formed(plan, sig, lot=0.01, expires=EXPIRES):
    """What every leg carries whatever its kind: the shared SL, its own TP,
    fixed_lot, the magic, a unique ordinal leg_index and comment, and an
    expiry on a limit only."""
    assert plan.accepted and plan.reason is None
    assert [leg.leg_index for leg in plan.legs] == list(range(len(plan.legs)))
    for leg in plan.legs:
        assert leg.sl == sig.sl
        assert leg.tp == sig.tps[leg.tp_index]
        assert leg.volume == lot
        assert leg.magic == 77001
        assert leg.side is sig.side
        assert leg.comment == f"{_b36(sig.message_id)}-{leg.leg_index}t{leg.tp_index}"
        if leg.kind is OrderKind.LIMIT:
            assert leg.expires_at_utc == expires
        else:
            assert leg.expires_at_utc is None


# ---------------------------------------------------------------------------
# AccountSpec.layered_entry: on by default, a real bool.
# ---------------------------------------------------------------------------

def test_account_spec_layered_entry_defaults_to_true():
    assert AccountSpec("a", "XAUUSD", 0.01, 1).layered_entry is True
    assert AccountSpec("a", "XAUUSD", 0.01, 1, positions=4).layered_entry is True
    assert AccountSpec("a", "XAUUSD", 0.01, 1, layered_entry=False).layered_entry is False


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_account_spec_layered_entry_must_be_a_real_bool(value):
    # A quoted "false" is truthy: it would leave layered entry on.
    with pytest.raises(ValueError, match="layered_entry must be true or false"):
        AccountSpec("a", "XAUUSD", 0.01, 1, layered_entry=value)


# ---------------------------------------------------------------------------
# split_now_pending: alternate over distribute()'s output in TP order.
# ---------------------------------------------------------------------------

ALL4 = ((0, 4490.0), (1, 4495.0), (2, 4500.0), (3, 4505.0))


@pytest.mark.parametrize(("n", "expected"), [
    (1, ((True, 0, 4490.0),)),
    (2, ((True, 0, 4490.0), (False, 1, 4495.0))),
    (3, ((True, 0, 4490.0), (False, 1, 4495.0), (True, 2, 4500.0))),
    (4, ((True, 0, 4490.0), (False, 1, 4495.0), (True, 2, 4500.0), (False, 3, 4505.0))),
    (5, ((True, 0, 4490.0), (False, 1, 4495.0), (True, 2, 4500.0), (False, 3, 4505.0),
         (True, 0, 4490.0))),
    # The amendment's N=6 row (2026-10-21, the TPs cycle): NOW TP1, TP3,
    # TP1; PENDING TP2, TP4, TP2.
    (6, ((True, 0, 4490.0), (False, 1, 4495.0), (True, 2, 4500.0), (False, 3, 4505.0),
         (True, 0, 4490.0), (False, 1, 4495.0))),
])
def test_split_now_pending_alternates_in_tp_order(n, expected):
    assert split_now_pending(distribute(n, ALL4)) == expected


@pytest.mark.parametrize("n", range(1, 21))
def test_split_now_pending_opens_the_extra_one_now(n):
    split = split_now_pending(distribute(n, ALL4))
    assert isinstance(split, tuple)
    assert sum(1 for is_now, _, _ in split if is_now) == (n + 1) // 2      # ceil(N/2)
    assert sum(1 for is_now, _, _ in split if not is_now) == n // 2        # floor(N/2)
    # The targets themselves are distribute()'s, untouched and in order.
    assert tuple((i, tp) for _, i, tp in split) == distribute(n, ALL4)


def test_split_now_pending_of_nothing_is_nothing():
    assert split_now_pending(()) == ()


# ---------------------------------------------------------------------------
# pending_levels: k levels from top to the better edge, both inclusive; one
# level goes to the middle. Every level is round_price'd.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("side", "top", "k", "expected"), [
    (Side.BUY, 4482.70, 2, (4482.70, 4480.0)),
    (Side.BUY, 4485.0, 2, (4485.0, 4480.0)),
    (Side.BUY, 4485.0, 3, (4485.0, 4482.50, 4480.0)),
    (Side.BUY, 4485.0, 4, (4485.0, 4483.33, 4481.67, 4480.0)),
    (Side.BUY, 4482.70, 3, (4482.70, 4481.35, 4480.0)),
    (Side.BUY, 4482.70, 1, (4481.35,)),     # the middle of 4482.70..4480
    (Side.BUY, 4485.0, 1, (4482.50,)),      # the middle of the whole zone
    (Side.SELL, 4482.30, 2, (4482.30, 4485.0)),
    (Side.SELL, 4480.0, 2, (4480.0, 4485.0)),
    (Side.SELL, 4480.0, 3, (4480.0, 4482.50, 4485.0)),
    (Side.SELL, 4480.0, 4, (4480.0, 4481.67, 4483.33, 4485.0)),
    (Side.SELL, 4482.30, 1, (4483.65,)),
    (Side.SELL, 4480.0, 1, (4482.50,)),
    (Side.BUY, 4485.0, 0, ()),
    (Side.SELL, 4480.0, 0, ()),
])
def test_pending_levels(side, top, k, expected):
    assert pending_levels(side, ZONE, top, k, SPEC) == expected


def test_pending_levels_round_to_the_feeds_own_digits():
    # The 10x trap: the same USD levels on a 3-digit feed, to 3 decimals.
    assert pending_levels(Side.BUY, ZONE, 4485.0, 4, EXNESS) == (4485.0, 4483.333, 4481.667, 4480.0)
    assert pending_levels(Side.BUY, ZONE, 4485.0, 4, SPEC) == (4485.0, 4483.33, 4481.67, 4480.0)


def test_pending_levels_snap_float_noise_to_the_tick_grid():
    top = 4480.40 - 3 * 0.10         # 4480.099999999999 in binary floating point
    assert top != 4480.10
    assert pending_levels(Side.BUY, ZONE, top, 2, SPEC) == (4480.10, 4480.0)
    assert pending_levels(Side.BUY, ZONE, top, 1, SPEC) == (4480.05,)


@pytest.mark.parametrize("k", [-1, True, False, 2.0, "2"])
def test_pending_levels_refuses_a_count_that_is_not_a_whole_number(k):
    with pytest.raises(ValueError):
        pending_levels(Side.BUY, ZONE, 4485.0, k, SPEC)


# ---------------------------------------------------------------------------
# The amendment's worked table -- the acceptance test. BUY zone 4480-4485,
# SL 4470, TPs 4490/4495/4500/4505, positions 4, lot 0.01, minimum distance
# 0.30 (spread 0.10 x 3, stops level 0: gates.min_stop_distance_usd).
# ---------------------------------------------------------------------------

WORKED_BUY = [
    # ask 4483 (inside): NOW market -> TP1, TP3; PENDING 4482.70, 4480 -> TP2, TP4
    (4483.0, [(M, 4483.0, 0), (L, 4482.70, 1), (M, 4483.0, 2), (L, 4480.0, 3)]),
    # ask 4486 (1.00 past, within the 1.25 budget): NOW market; PENDING 4485, 4480
    (4486.0, [(M, 4486.0, 0), (L, 4485.0, 1), (M, 4486.0, 2), (L, 4480.0, 3)]),
    # ask 4487.50 (2.50 past > 1.25): NOW limits at the near edge 4485
    (4487.50, [(L, 4485.0, 0), (L, 4485.0, 1), (L, 4485.0, 2), (L, 4480.0, 3)]),
    # ask 4479 (better than the zone): all four open now at market
    (4479.0, [(M, 4479.0, 0), (M, 4479.0, 1), (M, 4479.0, 2), (M, 4479.0, 3)]),
]

# The mirror image: SELL zone 4480-4485, SL 4495, TPs 4475/4470/4465/4460.
# The bid is the price; zone.hi is the better edge; "past" is below the zone.
WORKED_SELL = [
    (4482.0, [(M, 4482.0, 0), (L, 4482.30, 1), (M, 4482.0, 2), (L, 4485.0, 3)]),
    (4479.0, [(M, 4479.0, 0), (L, 4480.0, 1), (M, 4479.0, 2), (L, 4485.0, 3)]),
    (4477.50, [(L, 4480.0, 0), (L, 4480.0, 1), (L, 4480.0, 2), (L, 4485.0, 3)]),
    (4486.0, [(M, 4486.0, 0), (M, 4486.0, 1), (M, 4486.0, 2), (M, 4486.0, 3)]),
]


def test_the_worked_table_uses_the_shared_minimum_distance_of_0_30():
    # Ruling P4: one formula, max(stops_level_usd, 3 x spread).
    assert gates.min_stop_distance_usd(SPEC, at_ask(4483.0), POLICY) == pytest.approx(0.30)


@pytest.mark.parametrize("spec", [SPEC, EXNESS], ids=["2-digit", "3-digit"])
@pytest.mark.parametrize(("ask", "expected"), WORKED_BUY, ids=[str(r[0]) for r in WORKED_BUY])
def test_worked_table_buy(ask, expected, spec):
    sig = buy()
    plan = plan_entry(sig, acct(4), spec, at_ask(ask), POLICY)
    assert shape(plan) == expected
    assert_every_leg_is_well_formed(plan, sig)


@pytest.mark.parametrize("spec", [SPEC, EXNESS], ids=["2-digit", "3-digit"])
@pytest.mark.parametrize(("bid", "expected"), WORKED_SELL, ids=[str(r[0]) for r in WORKED_SELL])
def test_worked_table_sell_mirror(bid, expected, spec):
    sig = sell()
    plan = plan_entry(sig, acct(4), spec, at_bid(bid), POLICY)
    assert shape(plan) == expected
    assert_every_leg_is_well_formed(plan, sig)


def test_layered_entry_is_the_default_even_with_positions_unset():
    # Ruling P2: N is the number of surviving TPs (here 4), and the plan is
    # the worked table's first row.
    sig = buy()
    account = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)
    plan = plan_entry(sig, account, SPEC, at_ask(4483.0), POLICY)
    assert shape(plan) == WORKED_BUY[0][1]


def test_positions_unset_with_tp1_passed_layers_the_three_survivors():
    # ask 4491: TP1 is behind price, so N = 3 (TP2, TP3, TP4). 6.00 past the
    # zone is beyond the budget: NOW limits at 4485; the one PENDING order
    # goes to the middle of the whole zone.
    sig = buy()
    account = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)
    plan = plan_entry(sig, account, SPEC, at_ask(4491.0), POLICY)
    assert shape(plan) == [(L, 4485.0, 1), (L, 4482.50, 2), (L, 4485.0, 3)]
    assert_every_leg_is_well_formed(plan, sig)


# ---------------------------------------------------------------------------
# N = 1, 2, 3, 5 and 6, with the amendment's alternation.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("n", "expected"), [
    (1, [(M, 4483.0, 0)]),                                   # one position, now
    (2, [(M, 4483.0, 0), (L, 4481.35, 1)]),                  # one pending: the middle
    (3, [(M, 4483.0, 0), (L, 4481.35, 1), (M, 4483.0, 2)]),
    (5, [(M, 4483.0, 0), (L, 4482.70, 1), (M, 4483.0, 2), (L, 4480.0, 3),
         (M, 4483.0, 0)]),
    (6, [(M, 4483.0, 0), (L, 4482.70, 1), (M, 4483.0, 2), (L, 4481.35, 3),
         (M, 4483.0, 0), (L, 4480.0, 1)]),
])
def test_position_counts_inside_the_zone(n, expected):
    sig = buy()
    plan = plan_entry(sig, acct(n), SPEC, at_ask(4483.0), POLICY)
    assert shape(plan) == expected
    assert_every_leg_is_well_formed(plan, sig)


@pytest.mark.parametrize(("n", "expected"), [
    (1, [(M, 4486.0, 0)]),
    (2, [(M, 4486.0, 0), (L, 4482.50, 1)]),                  # the middle of the zone
    (3, [(M, 4486.0, 0), (L, 4482.50, 1), (M, 4486.0, 2)]),
    (5, [(M, 4486.0, 0), (L, 4485.0, 1), (M, 4486.0, 2), (L, 4480.0, 3),
         (M, 4486.0, 0)]),
    (6, [(M, 4486.0, 0), (L, 4485.0, 1), (M, 4486.0, 2), (L, 4482.50, 3),
         (M, 4486.0, 0), (L, 4480.0, 1)]),
])
def test_position_counts_above_the_zone_within_the_budget(n, expected):
    sig = buy()
    plan = plan_entry(sig, acct(n), SPEC, at_ask(4486.0), POLICY)
    assert shape(plan) == expected
    assert_every_leg_is_well_formed(plan, sig)


@pytest.mark.parametrize(("n", "expected"), [
    (1, [(M, 4482.0, 0)]),
    (2, [(M, 4482.0, 0), (L, 4483.65, 1)]),
    (3, [(M, 4482.0, 0), (L, 4483.65, 1), (M, 4482.0, 2)]),
    (5, [(M, 4482.0, 0), (L, 4482.30, 1), (M, 4482.0, 2), (L, 4485.0, 3),
         (M, 4482.0, 0)]),
    (6, [(M, 4482.0, 0), (L, 4482.30, 1), (M, 4482.0, 2), (L, 4483.65, 3),
         (M, 4482.0, 0), (L, 4485.0, 1)]),
])
def test_position_counts_inside_the_zone_sell(n, expected):
    sig = sell()
    plan = plan_entry(sig, acct(n), SPEC, at_bid(4482.0), POLICY)
    assert shape(plan) == expected
    assert_every_leg_is_well_formed(plan, sig)


# ---------------------------------------------------------------------------
# The minimum distance: a pending BUY limit sits at least
# gates.min_stop_distance_usd below the ask (a SELL limit that far above
# the bid). No room between that and the better edge: all open now.
# ---------------------------------------------------------------------------

def test_a_pending_level_inside_the_minimum_distance_of_the_near_edge_moves_down():
    # ask 4485.10 is 0.10 past the zone, within the budget. The near edge
    # 4485 is only 0.10 below the ask, inside the 0.30 minimum: the top
    # pending level is 4484.80, not 4485.
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(4485.10), POLICY)
    assert shape(plan) == [(M, 4485.10, 0), (L, 4484.80, 1), (M, 4485.10, 2), (L, 4480.0, 3)]


def test_a_pending_level_inside_the_minimum_distance_sell():
    plan = plan_entry(sell(), acct(4), SPEC, at_bid(4479.90), POLICY)
    assert shape(plan) == [(M, 4479.90, 0), (L, 4480.20, 1), (M, 4479.90, 2), (L, 4485.0, 3)]


@pytest.mark.parametrize("ask", [4480.0, 4480.20, 4480.30])
def test_no_room_above_the_better_edge_opens_everything_now(ask):
    # ask - 0.30 is at or below zone.lo: "better than the zone", all now.
    # 4480.30 is the boundary: the top would be exactly 4480.00.
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(ask), POLICY)
    assert shape(plan) == [(M, ask, 0), (M, ask, 1), (M, ask, 2), (M, ask, 3)]


def test_just_enough_room_above_the_better_edge_rests_the_pending_half():
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(4480.40), POLICY)
    assert shape(plan) == [(M, 4480.40, 0), (L, 4480.10, 1), (M, 4480.40, 2), (L, 4480.0, 3)]


@pytest.mark.parametrize("bid", [4485.0, 4484.80, 4484.70])
def test_no_room_below_the_better_edge_opens_everything_now_sell(bid):
    plan = plan_entry(sell(), acct(4), SPEC, at_bid(bid), POLICY)
    assert shape(plan) == [(M, bid, 0), (M, bid, 1), (M, bid, 2), (M, bid, 3)]


def test_just_enough_room_below_the_better_edge_sell():
    plan = plan_entry(sell(), acct(4), SPEC, at_bid(4484.60), POLICY)
    assert shape(plan) == [(M, 4484.60, 0), (L, 4484.90, 1), (M, 4484.60, 2), (L, 4485.0, 3)]


def test_the_minimum_distance_includes_the_broker_stops_level():
    # stops level 50 points = 0.50 USD beats 3 x spread (0.30).
    deep = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 50, 0)
    plan = plan_entry(buy(), acct(4), deep, at_ask(4483.0), POLICY)
    assert shape(plan) == [(M, 4483.0, 0), (L, 4482.50, 1), (M, 4483.0, 2), (L, 4480.0, 3)]


def test_the_minimum_distance_widens_with_the_spread():
    # spread 0.20: 3 x 0.20 = 0.60 below the ask.
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(4483.0, spread=0.20), POLICY)
    assert shape(plan) == [(M, 4483.0, 0), (L, 4482.40, 1), (M, 4483.0, 2), (L, 4480.0, 3)]


def test_a_now_limit_at_the_edge_also_keeps_the_minimum_distance():
    # spread 0.45: the minimum is 1.35. ask 4486.30 is 1.30 past the zone,
    # beyond the 1.25 budget, so the NOW half are limits -- but 4485 is only
    # 1.30 below the ask, so they rest at 4484.95, as does the top pending.
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(4486.30, spread=0.45), POLICY)
    assert shape(plan) == [(L, 4484.95, 0), (L, 4484.95, 1), (L, 4484.95, 2), (L, 4480.0, 3)]


def _narrow(lo):
    return Signal(channel_id=-1, message_id=4001, side=Side.BUY, symbol_alias="XAUUSD",
                  zone=EntryZone(lo, 4485.0), sl=4470.0, tps=BUY_TPS,
                  posted_at_utc=POSTED, content_hash="h")


def test_no_room_past_a_narrow_zone_within_the_budget_opens_everything_at_market():
    # Zone 4484.80-4485: from ask 4485.10 the nearest valid limit is 4484.80,
    # the better edge itself, so there is no room for a pending order.
    plan = plan_entry(_narrow(4484.80), acct(4), SPEC, at_ask(4485.10), POLICY)
    assert shape(plan) == [(M, 4485.10, 0), (M, 4485.10, 1), (M, 4485.10, 2), (M, 4485.10, 3)]


def test_no_room_past_a_narrow_zone_beyond_the_budget_rests_everything_at_one_limit():
    # Zone 4484.95-4485, spread 0.45 (minimum 1.35), ask 4486.30: beyond the
    # 1.25 budget, and the one valid limit price, 4484.95, is the better
    # edge. Every position takes the NOW entry: a limit there.
    sig = _narrow(4484.95)
    plan = plan_entry(sig, acct(4), SPEC, at_ask(4486.30, spread=0.45), POLICY)
    assert shape(plan) == [(L, 4484.95, 0), (L, 4484.95, 1), (L, 4484.95, 2), (L, 4484.95, 3)]
    assert_every_leg_is_well_formed(plan, sig)


@pytest.mark.parametrize("n", [1, 2, 4, 6, 9])
@pytest.mark.parametrize("spread", [0.10, 0.25, 0.45])
@pytest.mark.parametrize("price", [4478.0, 4480.0, 4480.25, 4481.0, 4483.0, 4484.9, 4485.0,
                                   4485.2, 4486.0, 4486.4, 4487.5, 4491.0, 4496.0])
def test_no_limit_ever_sits_inside_the_minimum_distance(price, spread, n):
    for sig, snap in ((buy(), at_ask(price, spread=spread)),
                      (sell(), at_bid(round(8965.0 - price, 2), spread=spread))):
        plan = plan_entry(sig, acct(n), SPEC, snap, POLICY)
        assert plan.accepted
        min_dist = gates.min_stop_distance_usd(SPEC, snap, POLICY)
        ref = snap.ask if sig.side is Side.BUY else snap.bid
        for leg in plan.legs:
            if leg.kind is OrderKind.MARKET:
                assert leg.price == ref
                continue
            # On the better side of price by at least the minimum, and
            # inside the zone.
            assert sig.side.sign * (ref - leg.price) >= min_dist - 1e-9
            assert ZONE.lo - 1e-9 <= leg.price <= ZONE.hi + 1e-9


# ---------------------------------------------------------------------------
# The chase budget: the existing chase gate's formula,
# max(chase_floor_usd, chase_tp1_fraction x |TP1 - near edge|). Beyond it
# the NOW half become limits at the near edge; the plan is NOT rejected.
# ---------------------------------------------------------------------------

def test_exactly_at_the_budget_the_now_half_is_market():
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(4486.25), POLICY)
    assert [kind for kind, _, _ in shape(plan)] == [M, L, M, L]


def test_just_past_the_budget_the_now_half_are_limits_at_the_near_edge():
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(4486.26), POLICY)
    assert plan.accepted and plan.reason is None
    assert shape(plan) == [(L, 4485.0, 0), (L, 4485.0, 1), (L, 4485.0, 2), (L, 4480.0, 3)]


def test_the_budget_scales_with_the_distance_to_tp1():
    # TP1 4505 is 20 past the zone: budget 0.25 x 20 = 5.00. Two TPs, two
    # positions: the one PENDING order goes to the middle of the zone.
    far = buy(tps=(4505.0, 4510.0))
    within = plan_entry(far, acct(None), SPEC, at_ask(4489.0), POLICY)
    assert shape(within) == [(M, 4489.0, 0), (L, 4482.50, 1)]
    beyond = plan_entry(far, acct(None), SPEC, at_ask(4490.01), POLICY)
    assert shape(beyond) == [(L, 4485.0, 0), (L, 4482.50, 1)]


def test_the_budget_has_a_floor():
    # TP1 4487 is 2 past the zone: 0.25 x 2 = 0.50, floored to 1.00.
    near = buy(tps=(4487.0, 4495.0))
    within = plan_entry(near, acct(None), SPEC, at_ask(4486.0), POLICY)
    assert [kind for kind, _, _ in shape(within)] == [M, L]
    beyond = plan_entry(near, acct(None), SPEC, at_ask(4486.01), POLICY)
    assert [kind for kind, _, _ in shape(beyond)] == [L, L]


def test_the_budget_follows_the_policy():
    pol = Policy(chase_floor_usd=3.00)
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(4487.50), pol)
    assert shape(plan) == [(M, 4487.50, 0), (L, 4485.0, 1), (M, 4487.50, 2), (L, 4480.0, 3)]


def test_a_runaway_past_tp2_is_not_rejected():
    # 11 past the zone: TP3 and TP4 survive, the NOW one is a limit at 4485
    # and the PENDING one goes to the middle of the zone.
    sig = buy()
    plan = plan_entry(sig, acct(None), SPEC, at_ask(4496.0), POLICY)
    assert shape(plan) == [(L, 4485.0, 2), (L, 4482.50, 3)]
    assert_every_leg_is_well_formed(plan, sig)


def test_a_runaway_sell_is_not_rejected():
    sig = sell()
    plan = plan_entry(sig, acct(4), SPEC, at_bid(4469.0), POLICY)
    # TP1 and TP2 are behind the bid: TP3, TP4 survive and N=4 cycles over
    # them, TP3, TP4, TP3, TP4. NOW: limits at 4480; PENDING: 4480 and 4485.
    assert shape(plan) == [(L, 4480.0, 2), (L, 4480.0, 3), (L, 4480.0, 2), (L, 4485.0, 3)]
    assert_every_leg_is_well_formed(plan, sig)


# ---------------------------------------------------------------------------
# Everything else is unchanged: the other gates run first, and pending_at
# belongs to the single rule only.
# ---------------------------------------------------------------------------

def _stale_buy():
    return Signal(channel_id=-1, message_id=1, side=Side.BUY, symbol_alias="XAUUSD",
                  zone=ZONE, sl=4470.0, tps=BUY_TPS,
                  posted_at_utc=NOW - timedelta(seconds=900), content_hash="h")


@pytest.mark.parametrize(("sig", "snap", "account", "reason"), [
    (_stale_buy(), at_ask(4483.0), acct(4), RejectReason.STALE),
    (buy(), at_ask(4483.0, spread=1.0), acct(4), RejectReason.SPREAD_WIDE),
    (buy(), at_ask(4469.0), acct(4), RejectReason.SL_BREACHED),
    (buy(), at_ask(4470.20), acct(4), RejectReason.STOPS_TOO_TIGHT),
    (buy(), at_ask(4510.0), acct(4), RejectReason.ALL_TPS_BEHIND),
    (buy(), at_ask(4483.0), acct(4, lot=0.004), RejectReason.INVALID_VOLUME),
    (sell(), at_bid(4496.0), acct(4), RejectReason.SL_BREACHED),
], ids=["stale", "spread", "sl-breached", "stops-tight", "all-tps-behind",
        "volume", "sell-sl-breached"])
def test_a_gate_rejection_still_rejects_the_whole_signal(sig, snap, account, reason):
    plan = plan_entry(sig, account, SPEC, snap, POLICY)
    assert not plan.accepted
    assert plan.reason is reason
    assert plan.legs == ()
    single = AccountSpec(account.account_id, account.symbol, account.fixed_lot,
                         account.magic_base, positions=account.positions,
                         layered_entry=False)
    assert plan == plan_entry(sig, single, SPEC, snap, POLICY)


@pytest.mark.parametrize("ask", [4479.0, 4483.0, 4486.0, 4487.50, 4496.0])
def test_pending_at_does_not_change_a_layered_plan(ask):
    favourable = plan_entry(buy(), acct(4), SPEC, at_ask(ask), Policy(pending_at="favourable"))
    near = plan_entry(buy(), acct(4), SPEC, at_ask(ask), Policy(pending_at="near"))
    assert favourable == near


def test_every_limit_expires_pending_max_age_hours_after_the_post():
    sig = buy()
    plan = plan_entry(sig, acct(4, lot=0.05), SPEC, at_ask(4487.50),
                      Policy(pending_max_age_hours=2))
    assert_every_leg_is_well_formed(plan, sig, lot=0.05, expires=POSTED + timedelta(hours=2))
    assert all(leg.kind is OrderKind.LIMIT for leg in plan.legs)


def test_every_leg_carries_fixed_lot():
    sig = buy()
    plan = plan_entry(sig, acct(6, lot=0.05), SPEC, at_ask(4483.0), POLICY)
    assert {leg.volume for leg in plan.legs} == {0.05}
    assert {leg.kind for leg in plan.legs} == {OrderKind.MARKET, OrderKind.LIMIT}


def test_the_passed_tp_note_is_unchanged():
    plan = plan_entry(buy(), acct(4), SPEC, at_ask(4491.0), POLICY)
    assert plan.notes == ("1 take profit(s) already passed",)


# ---------------------------------------------------------------------------
# Unique leg_index values and comments across the NOW and PENDING legs.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [None, *range(1, 21)])
@pytest.mark.parametrize("ask", [4479.0, 4483.0, 4486.0, 4487.50, 4491.0])
def test_leg_index_and_comment_are_unique_across_now_and_pending(ask, n):
    sig = buy()
    plan = plan_entry(sig, acct(n), SPEC, at_ask(ask), POLICY)
    assert_every_leg_is_well_formed(plan, sig)
    comments = [leg.comment for leg in plan.legs]
    assert len(set(comments)) == len(comments)
    assert all(len(c) <= 20 for c in comments)
    assert len({leg.leg_index for leg in plan.legs}) == len(plan.legs)


@pytest.mark.parametrize("n", [2, 4, 6, 9])
def test_unique_comments_survive_a_huge_message_id(n):
    huge = buy(message_id=36 ** 25)
    plan = plan_entry(huge, acct(n), SPEC, at_ask(4483.0), POLICY)
    comments = [leg.comment for leg in plan.legs]
    assert len(set(comments)) == n
    for leg in plan.legs:
        assert leg.comment.endswith(f"-{leg.leg_index}t{leg.tp_index}")
        assert len(leg.comment) <= 20


# ---------------------------------------------------------------------------
# pending_should_cancel: true once price reaches TP1 (bid for a BUY, ask for
# a SELL -- the price the position would close at) or once now >= expires.
# ---------------------------------------------------------------------------

LATER = EXPIRES - timedelta(hours=1)      # well before the expiry


@pytest.mark.parametrize(("bid", "want"), [
    (4489.99, False),     # TP1 - 0.01
    (4490.00, True),      # at TP1
    (4490.01, True),
    (4496.00, True),
])
def test_pending_should_cancel_buy_at_tp1(bid, want):
    assert pending_should_cancel(Side.BUY, BUY_TPS, bid, bid + 0.10, LATER, EXPIRES) is want


def test_pending_should_cancel_buy_reads_the_bid_not_the_ask():
    # The ask has reached TP1 but the bid -- where a BUY closes -- has not.
    assert pending_should_cancel(Side.BUY, BUY_TPS, 4489.95, 4490.05, LATER, EXPIRES) is False


@pytest.mark.parametrize(("ask", "want"), [
    (4475.01, False),     # TP1 + 0.01
    (4475.00, True),      # at TP1
    (4474.99, True),
    (4469.00, True),
])
def test_pending_should_cancel_sell_at_tp1(ask, want):
    assert pending_should_cancel(Side.SELL, SELL_TPS, ask - 0.10, ask, LATER, EXPIRES) is want


def test_pending_should_cancel_sell_reads_the_ask_not_the_bid():
    assert pending_should_cancel(Side.SELL, SELL_TPS, 4474.95, 4475.05, LATER, EXPIRES) is False


@pytest.mark.parametrize(("now", "want"), [
    (EXPIRES - timedelta(seconds=1), False),
    (EXPIRES - timedelta(microseconds=1), False),
    (EXPIRES, True),                              # the 4 h boundary
    (EXPIRES + timedelta(seconds=1), True),
])
@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_pending_should_cancel_at_the_expiry_boundary(side, now, want):
    tps, bid, ask = (BUY_TPS, 4483.0, 4483.10) if side is Side.BUY else (SELL_TPS, 4482.0, 4482.10)
    assert pending_should_cancel(side, tps, bid, ask, now, EXPIRES) is want


def test_a_planned_pending_leg_is_cancelled_exactly_four_hours_after_the_post():
    sig = buy()
    plan = plan_entry(sig, acct(4), SPEC, at_ask(4483.0), POLICY)
    pending = [leg for leg in plan.legs if leg.kind is OrderKind.LIMIT]
    assert pending
    for leg in pending:
        assert leg.expires_at_utc == sig.posted_at_utc + timedelta(hours=4)
        before = leg.expires_at_utc - timedelta(seconds=1)
        assert not pending_should_cancel(sig.side, sig.tps, 4483.0, 4483.10, before,
                                         leg.expires_at_utc)
        assert pending_should_cancel(sig.side, sig.tps, 4483.0, 4483.10, leg.expires_at_utc,
                                     leg.expires_at_utc)
