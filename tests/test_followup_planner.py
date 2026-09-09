from datetime import datetime, UTC

from xaucopier.domain.followup_planner import LegView, plan_followup
from xaucopier.domain.intent import CloseIntent, ModifyIntent
from xaucopier.domain.market import MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import FollowUp, FollowUpOp, Side

NOW = datetime(2026, 9, 5, 11, 5, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()
SNAP = MarketSnapshot(bid=4488.0, ask=4488.10, tick_time_utc=NOW,
                      spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)

def legs(n=3, price_open=4482.10):
    return tuple(
        LegView(position_ticket=1000 + i, leg_index=i, side=Side.BUY, volume=0.01,
                price_open=price_open, sl=4470.0, tp=4490.0 + 5 * i, is_pending=False)
        for i in range(n)
    )

def fu(op):
    return FollowUp(channel_id=-1, message_id=9, op=op, posted_at_utc=NOW)

def test_breakeven_emits_one_modify_per_leg():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, SNAP, POLICY)
    assert len(plan.intents) == 3
    assert all(isinstance(i, ModifyIntent) for i in plan.intents)

def test_breakeven_uses_actual_fill_not_signal_entry():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(price_open=4483.50), SPEC, SNAP, POLICY)
    # fill + spread + buffer = 4483.50 + 0.10 + 0.10
    assert plan.intents[0].sl == 4483.70

def test_breakeven_preserves_each_legs_own_tp():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, SNAP, POLICY)
    assert [i.tp for i in plan.intents] == [4490.0, 4495.0, 4500.0]

def test_breakeven_skipped_when_price_has_not_cleared_it():
    snap = MarketSnapshot(bid=4482.0, ask=4482.10, tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, snap, POLICY)
    assert plan.intents == ()

def test_close_half_closes_ceil_of_open_legs():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY)
    assert len(plan.intents) == 2

def test_close_half_picks_furthest_tps_first():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY)
    assert {i.position_ticket for i in plan.intents} == {1002, 1001}

def test_close_half_closes_whole_legs_never_fractions():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY)
    assert all(i.volume == 0.01 for i in plan.intents)

def test_close_all_covers_every_leg():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), legs(4), SPEC, SNAP, POLICY)
    assert len(plan.intents) == 4
    assert all(isinstance(i, CloseIntent) for i in plan.intents)

def test_cancel_pending_only_touches_pending_legs():
    mixed = legs(2) + (
        LegView(position_ticket=2000, leg_index=2, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
    )
    plan = plan_followup(fu(FollowUpOp.CANCEL_PENDING), mixed, SPEC, SNAP, POLICY)
    assert [i.position_ticket for i in plan.intents] == [2000]

def test_no_legs_is_a_noop_not_an_exception():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), (), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is not None
