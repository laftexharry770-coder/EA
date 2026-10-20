# tests/test_planner.py
from datetime import datetime, timedelta, UTC

import pytest

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
# Plan 1b ruling P2: layered entry is the default. The entry-kind tests
# below describe the single rule (market inside or better than the zone,
# one limit otherwise), which layered_entry = false keeps exactly; the
# layered rule has its own tests in test_layered_entry.py.
SINGLE_RULE = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001,
                          layered_entry=False)
POLICY = Policy()

# I2: the chase gate no longer runs on the pending path, so price far past
# the zone is a limit order at the favourable edge, not a rejection. That
# makes these TP-dropping cases reachable under the real default policy --
# the permissive-chase escape hatch they used to need is gone.

def snap(bid, ask, spread=0.10, median=0.10):
    return MarketSnapshot(bid=bid, ask=ask, tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=median, now_utc=NOW)

def sig(tps=(4490.0, 4495.0, 4500.0, 4505.0)):
    return Signal(channel_id=-1, message_id=4001, side=Side.BUY,
                  symbol_alias="XAUUSD", zone=EntryZone(4480.0, 4485.0),
                  sl=4470.0, tps=tps, posted_at_utc=NOW - timedelta(seconds=5),
                  content_hash="h")

def sell_sig(tps=(4470.0, 4465.0)):
    return Signal(channel_id=-1, message_id=4002, side=Side.SELL,
                  symbol_alias="XAUUSD", zone=EntryZone(4480.0, 4485.0),
                  sl=4495.0, tps=tps, posted_at_utc=NOW - timedelta(seconds=5),
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
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4482.0, 4482.10), POLICY)
    assert all(leg.kind is OrderKind.MARKET for leg in plan.legs)
    assert plan.legs[0].price == 4482.10

def test_market_when_price_better_than_zone():
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4478.0, 4478.10), POLICY)
    assert plan.legs[0].kind is OrderKind.MARKET

def test_limit_at_favourable_edge_when_price_above_zone():
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4485.5, 4485.60), POLICY)
    assert all(leg.kind is OrderKind.LIMIT for leg in plan.legs)
    assert plan.legs[0].price == 4480.0  # favourable edge, not 4485

def test_limit_at_near_edge_when_policy_says_so():
    pol = Policy(pending_at="near")
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4485.5, 4485.60), pol)
    assert plan.legs[0].price == 4485.0

def test_pending_legs_carry_an_expiry():
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4485.5, 4485.60), POLICY)
    assert plan.legs[0].expires_at_utc is not None

def test_market_legs_carry_no_expiry():
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4482.0, 4482.10), POLICY)
    assert plan.legs[0].expires_at_utc is None

def test_passed_tps_are_dropped_as_legs():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4496.0, 4496.10), POLICY)
    assert [leg.tp for leg in plan.legs] == [4500.0, 4505.0]

def test_rejected_when_no_tp_survives():
    plan = plan_entry(sig(tps=(4490.0,)), ACCT, SPEC, snap(4499.0, 4499.10), POLICY)
    assert not plan.accepted
    assert plan.reason is RejectReason.ALL_TPS_BEHIND

def test_a_runaway_becomes_a_limit_at_the_favourable_edge():
    # Replaces test_chase_gate_still_rejects_a_runaway_under_default_policy.
    # A limit fills at a fixed price no matter how far price has run, so
    # there is nothing to chase: rejecting here killed the pending path that
    # spec 7.1 commits to. Price 11 USD past the zone must still rest an
    # order at 4480, not refuse the signal.
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4496.0, 4496.10), POLICY)
    assert plan.accepted
    assert plan.reason is None
    assert all(leg.kind is OrderKind.LIMIT for leg in plan.legs)
    assert {leg.price for leg in plan.legs} == {4480.0}

def test_a_runaway_sell_becomes_a_limit_at_the_favourable_edge():
    plan = plan_entry(sell_sig(), SINGLE_RULE, SPEC, snap(4469.0, 4469.10), POLICY)
    assert plan.accepted
    assert all(leg.kind is OrderKind.LIMIT for leg in plan.legs)
    assert {leg.price for leg in plan.legs} == {4485.0}

def test_a_runaway_limit_still_carries_an_expiry():
    # The pending path's staleness bound is the expiry, not the chase gate.
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4496.0, 4496.10), POLICY)
    assert all(leg.expires_at_utc is not None for leg in plan.legs)

def test_a_runaway_past_every_tp_is_still_rejected():
    # invalidation_gate, not chase_gate, is what bounds a stale limit: once
    # price is past every target the signal is dead however it would fill.
    plan = plan_entry(sig(), ACCT, SPEC, snap(4510.0, 4510.10), POLICY)
    assert not plan.accepted
    assert plan.reason is RejectReason.ALL_TPS_BEHIND

def test_a_runaway_through_the_stop_is_still_rejected():
    plan = plan_entry(sell_sig(), ACCT, SPEC, snap(4496.0, 4496.10), POLICY)
    assert not plan.accepted
    assert plan.reason is RejectReason.SL_BREACHED

def test_market_entries_are_unaffected_by_the_move():
    plan = plan_entry(sig(), SINGLE_RULE, SPEC, snap(4482.0, 4482.10), POLICY)
    assert plan.accepted
    assert all(leg.kind is OrderKind.MARKET for leg in plan.legs)

def test_leg_index_is_in_the_comment():
    # Plan 1b: the suffix is "-{leg_index}t{tp_index}", so a position names
    # both itself and its take profit at the broker.
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    comments = [leg.comment for leg in plan.legs]
    assert len(set(comments)) == 4
    assert comments == ["335-0t0", "335-1t1", "335-2t2", "335-3t3"]

def test_comment_fits_broker_limit():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert all(len(leg.comment) <= 20 for leg in plan.legs)

def test_leg_index_survives_an_absurdly_large_message_id():
    # A base36 prefix this long (36**25 has 26 base36 digits) plus "-{leg}"
    # overflows the 20-char broker limit on its own. The leg index is the
    # one mandatory field -- truncation must eat the prefix, not the leg
    # index, or the comment stops disambiguating which leg is which.
    huge = Signal(channel_id=-1, message_id=36 ** 25, side=Side.BUY,
                  symbol_alias="XAUUSD", zone=EntryZone(4480.0, 4485.0),
                  sl=4470.0, tps=(4490.0, 4495.0, 4500.0, 4505.0),
                  posted_at_utc=NOW - timedelta(seconds=5), content_hash="h")
    plan = plan_entry(huge, ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert plan.accepted
    for leg in plan.legs:
        assert leg.comment.endswith(f"-{leg.leg_index}t{leg.tp_index}")
        assert len(leg.comment) <= 20

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


# --- Item 1: a leg must name its real position in the original sig.tps, not
# its position in the filtered/surviving list. Plan 1b moved that job from
# leg_index (now the position's ordinal, 0..N-1) to tp_index, and the
# comment carries both. ---

def test_tp_index_names_the_original_tp_when_earlier_tps_have_passed():
    # Price has run past TP1 and TP2; only TP3 and TP4 survive. Those legs
    # target TP3 and TP4 (indexes 2 and 3 in sig.tps), not 0 and 1 (their
    # position in the surviving subset) -- reconciliation reads the TP from
    # the comment's "t" part. leg_index is the position's own number.
    plan = plan_entry(sig(), ACCT, SPEC, snap(4496.0, 4496.10), POLICY)
    assert [leg.tp for leg in plan.legs] == [4500.0, 4505.0]
    assert [leg.leg_index for leg in plan.legs] == [0, 1]
    assert [leg.tp_index for leg in plan.legs] == [2, 3]
    assert plan.legs[0].comment.endswith("-0t2")
    assert plan.legs[1].comment.endswith("-1t3")

def test_leg_index_is_unchanged_when_no_tp_has_passed():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert [leg.leg_index for leg in plan.legs] == [0, 1, 2, 3]


# --- Item 2: Policy.pending_at must be validated, not left as a free string
# that silently falls through to the near edge on a typo. ---

def test_pending_at_rejects_a_typo():
    with pytest.raises(ValueError):
        Policy(pending_at="favorable")

def test_pending_at_rejects_wrong_case():
    with pytest.raises(ValueError):
        Policy(pending_at="Favourable")

def test_pending_at_accepts_the_two_known_values():
    Policy(pending_at="near")
    Policy(pending_at="favourable")

def test_policy_default_still_constructs():
    Policy()


# --- Item 3: SELL coverage. Take profits below the zone, stop above it, so
# the geometry gate's independently-derived side agrees with the keyword. ---

def test_sell_all_legs_market_when_bid_inside_zone():
    plan = plan_entry(sell_sig(), SINGLE_RULE, SPEC, snap(4482.0, 4482.10), POLICY)
    assert plan.accepted
    assert len(plan.legs) == 2
    assert all(leg.kind is OrderKind.MARKET for leg in plan.legs)
    assert {leg.sl for leg in plan.legs} == {4495.0}

def test_sell_limit_at_favourable_high_edge_when_bid_below_zone():
    plan = plan_entry(sell_sig(), SINGLE_RULE, SPEC, snap(4479.5, 4479.60), POLICY)
    assert plan.accepted
    assert all(leg.kind is OrderKind.LIMIT for leg in plan.legs)
    assert plan.legs[0].price == 4485.0  # favourable edge for a sell is the high side

def test_sell_market_when_bid_above_zone():
    plan = plan_entry(sell_sig(), SINGLE_RULE, SPEC, snap(4490.0, 4490.10), POLICY)
    assert plan.accepted
    assert all(leg.kind is OrderKind.MARKET for leg in plan.legs)

def test_sell_limit_at_near_edge_when_policy_says_so():
    pol = Policy(pending_at="near")
    plan = plan_entry(sell_sig(), SINGLE_RULE, SPEC, snap(4479.5, 4479.60), pol)
    assert plan.accepted
    assert plan.legs[0].price == 4480.0  # near edge for a sell is the low side


# --- Plan 1b Task 1: a leg names the take profit it targets by tp_index,
# the index into sig.tps, apart from its own leg_index. ---

def test_every_leg_names_its_take_profit_by_index_into_sig_tps():
    s = sig()
    plan = plan_entry(s, ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert [leg.tp_index for leg in plan.legs] == [0, 1, 2, 3]
    assert all(leg.tp == s.tps[leg.tp_index] for leg in plan.legs)

def test_tp_index_is_the_original_position_when_earlier_tps_have_passed():
    s = sig()
    plan = plan_entry(s, ACCT, SPEC, snap(4496.0, 4496.10), POLICY)
    assert [leg.tp_index for leg in plan.legs] == [2, 3]
    assert all(leg.tp == s.tps[leg.tp_index] for leg in plan.legs)
