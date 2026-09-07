# tests/test_planner.py
from datetime import datetime, timedelta, UTC

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.intent import OrderKind
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.planner import plan_entry
from xaucopier.domain.signal import EntryZone, Side, Signal

NOW = datetime(2026, 9, 5, 10, 45, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
EXNESS = SymbolSpec("XAUUSDm", 3, 0.001, 0.001, 0.01, 100.0, 0.01, 0, 0)
ACCT = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)
POLICY = Policy()

# Price cannot run past a take profit without also running far past the entry
# zone, so under the default policy the chase gate fires first and masks the
# TP-dropping path. The two behaviours are independent; a permissive chase
# budget is the only way to exercise the second one in isolation.
PERMISSIVE = Policy(chase_floor_usd=50.0)

def snap(bid, ask, spread=0.10, median=0.10):
    return MarketSnapshot(bid=bid, ask=ask, tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=median, now_utc=NOW)

def sig(tps=(4490.0, 4495.0, 4500.0, 4505.0)):
    return Signal(channel_id=-1, message_id=4001, side=Side.BUY,
                  symbol_alias="XAUUSD", zone=EntryZone(4480.0, 4485.0),
                  sl=4470.0, tps=tps, posted_at_utc=NOW - timedelta(seconds=5),
                  content_hash="h")

def test_one_leg_per_tp():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert plan.accepted
    assert len(plan.legs) == 4
    assert [leg.tp for leg in plan.legs] == [4490.0, 4495.0, 4500.0, 4505.0]

def test_every_leg_carries_the_shared_sl_and_fixed_lot():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert {leg.sl for leg in plan.legs} == {4470.0}
    assert {leg.volume for leg in plan.legs} == {0.01}

def test_market_when_price_inside_zone():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert all(leg.kind is OrderKind.MARKET for leg in plan.legs)
    assert plan.legs[0].price == 4482.10

def test_market_when_price_better_than_zone():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4478.0, 4478.10), POLICY)
    assert plan.legs[0].kind is OrderKind.MARKET

def test_limit_at_favourable_edge_when_price_above_zone():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4485.5, 4485.60), POLICY)
    assert all(leg.kind is OrderKind.LIMIT for leg in plan.legs)
    assert plan.legs[0].price == 4480.0  # favourable edge, not 4485

def test_limit_at_near_edge_when_policy_says_so():
    pol = Policy(pending_at="near")
    plan = plan_entry(sig(), ACCT, SPEC, snap(4485.5, 4485.60), pol)
    assert plan.legs[0].price == 4485.0

def test_pending_legs_carry_an_expiry():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4485.5, 4485.60), POLICY)
    assert plan.legs[0].expires_at_utc is not None

def test_market_legs_carry_no_expiry():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert plan.legs[0].expires_at_utc is None

def test_passed_tps_are_dropped_as_legs():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4496.0, 4496.10), PERMISSIVE)
    assert [leg.tp for leg in plan.legs] == [4500.0, 4505.0]

def test_rejected_when_no_tp_survives():
    plan = plan_entry(sig(tps=(4490.0,)), ACCT, SPEC, snap(4499.0, 4499.10), PERMISSIVE)
    assert not plan.accepted
    assert plan.reason is RejectReason.ALL_TPS_BEHIND

def test_chase_gate_still_rejects_a_runaway_under_default_policy():
    # The companion to the two tests above: with the real policy, price this
    # far past the zone must be refused outright, not traded on fewer legs.
    plan = plan_entry(sig(), ACCT, SPEC, snap(4496.0, 4496.10), POLICY)
    assert not plan.accepted
    assert plan.reason is RejectReason.CHASE_EXCEEDED

def test_leg_index_is_in_the_comment():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    comments = [leg.comment for leg in plan.legs]
    assert len(set(comments)) == 4
    assert comments[0].endswith("-0")

def test_comment_fits_broker_limit():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert all(len(leg.comment) <= 20 for leg in plan.legs)

def test_magic_is_constant_across_legs():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert {leg.magic for leg in plan.legs} == {77001}

def test_deviation_is_digit_aware():
    two = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    three = plan_entry(sig(), ACCT, EXNESS, snap(4482.0, 4482.10), POLICY)
    assert two.legs[0].deviation_points == 20    # 0.20 USD at point 0.01
    assert three.legs[0].deviation_points == 200  # same USD, 3-digit feed

def test_stale_signal_is_rejected_before_planning():
    old = Signal(channel_id=-1, message_id=1, side=Side.BUY, symbol_alias="XAUUSD",
                 zone=EntryZone(4480.0, 4485.0), sl=4470.0, tps=(4490.0,),
                 posted_at_utc=NOW - timedelta(seconds=900), content_hash="h")
    plan = plan_entry(old, ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert not plan.accepted and plan.reason is RejectReason.STALE

def test_wide_spread_is_rejected():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4483.0, spread=1.0), POLICY)
    assert not plan.accepted and plan.reason is RejectReason.SPREAD_WIDE
