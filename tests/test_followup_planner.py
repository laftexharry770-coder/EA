from datetime import datetime, timedelta, UTC

import pytest

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.followup_planner import LegView, plan_followup
from xaucopier.domain.intent import CancelIntent, CloseIntent, ModifyIntent
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.planner import plan_entry
from xaucopier.domain.signal import EntryZone, FollowUp, FollowUpOp, Side, Signal

NOW = datetime(2026, 9, 5, 11, 5, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()
SNAP = MarketSnapshot(bid=4488.0, ask=4488.10, tick_time_utc=NOW,
                      spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
# The take profits of the signal a follow-up resolved to, as posted
# (sig.tps), which plan_followup is given: "SL to BE" never goes past TP1.
# legs() below are a BUY on TPS, sell_legs() a SELL on SELL_TPS.
TPS = (4490.0, 4495.0, 4500.0, 4505.0)
SELL_TPS = (4480.0, 4475.0, 4470.0, 4465.0)

# One position per TP, every TP ahead at entry: position i targets TP(i+1),
# so leg_index and tp_index happen to agree here. The Plan 1b section at the
# end covers the plans where they part.
def legs(n=3, price_open=4482.10):
    return tuple(
        LegView(position_ticket=1000 + i, leg_index=i, tp_index=i, side=Side.BUY,
                volume=0.01, price_open=price_open, sl=4470.0, tp=4490.0 + 5 * i,
                is_pending=False)
        for i in range(n)
    )

def sell_legs(n=1, price_open=4494.0):
    return tuple(
        LegView(position_ticket=3000 + i, leg_index=i, tp_index=i, side=Side.SELL,
                volume=0.01, price_open=price_open, sl=4506.0, tp=4480.0 - 5 * i,
                is_pending=False)
        for i in range(n)
    )

def fu(op, price_hint=None, tp_index_hint=None):
    return FollowUp(channel_id=-1, message_id=9, op=op, posted_at_utc=NOW,
                    price_hint=price_hint, tp_index_hint=tp_index_hint)

def test_breakeven_emits_one_modify_per_leg():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, SNAP, POLICY, tps=TPS)
    assert len(plan.intents) == 3
    assert all(isinstance(i, ModifyIntent) for i in plan.intents)

def test_breakeven_uses_actual_fill_not_signal_entry():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(price_open=4483.50), SPEC, SNAP, POLICY,
                         tps=TPS)
    # fill + 1 pip (be_buffer_pips 1.0 x $0.10) = 4483.50 + 0.10; no spread term
    assert plan.intents[0].sl == 4483.60


@pytest.mark.parametrize("spread", [0.10, 0.20, 0.40])
def test_breakeven_has_no_spread_term(spread):
    # Amendment 2026-10-21: a BUY fills at the ask and its stop triggers on
    # the bid, so the fill + buffer already locks the buffer in.
    snap = MarketSnapshot(bid=4488.0, ask=round(4488.0 + spread, 2), tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=0.10, now_utc=NOW)
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(1, price_open=4483.50), SPEC, snap,
                         POLICY, tps=TPS)
    assert [i.sl for i in plan.intents] == [4483.60]


def test_breakeven_buffer_is_in_pips_of_ten_cents():
    pol = Policy(be_buffer_pips=5)   # 5 x $0.10 = $0.50
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(1, price_open=4483.50), SPEC, SNAP,
                         pol, tps=TPS)
    assert [i.sl for i in plan.intents] == [4484.00]
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), sell_legs(1, price_open=4494.0), SPEC, SNAP,
                         pol, tps=SELL_TPS)
    assert [i.sl for i in plan.intents] == [4493.50]


SNAP_PAST_TP1 = MarketSnapshot(bid=4492.0, ask=4492.10, tick_time_utc=NOW,
                               spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
SNAP_PAST_SELL_TP1 = MarketSnapshot(bid=4477.90, ask=4478.0, tick_time_utc=NOW,
                                    spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)


def test_breakeven_never_goes_past_tp1():
    # A late BUY fill at 4489.95: 4489.95 + 0.10 = 4490.05 is past TP1
    # (4490), so breakeven is TP1 itself. The SELL mirror: a fill at
    # 4480.05 on a SELL whose TP1 is 4480 gives 4480.00, not 4479.95.
    buy = LegView(position_ticket=1, leg_index=1, tp_index=1, side=Side.BUY, volume=0.01,
                  price_open=4489.95, sl=4470.0, tp=4495.0, is_pending=False)
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), (buy,), SPEC, SNAP_PAST_TP1, POLICY, tps=TPS)
    assert [i.sl for i in plan.intents] == [4490.00]
    sell = LegView(position_ticket=2, leg_index=1, tp_index=1, side=Side.SELL, volume=0.01,
                   price_open=4480.05, sl=4506.0, tp=4475.0, is_pending=False)
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), (sell,), SPEC, SNAP_PAST_SELL_TP1, POLICY,
                         tps=SELL_TPS)
    assert [i.sl for i in plan.intents] == [4480.00]


def test_plan_followup_needs_the_signals_take_profits():
    with pytest.raises(TypeError):
        plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, SNAP, POLICY)  # no tps
    with pytest.raises(ValueError, match="take profits"):
        plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, SNAP, POLICY, tps=())


def test_breakeven_preserves_each_legs_own_tp():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, SNAP, POLICY, tps=TPS)
    assert [i.tp for i in plan.intents] == [4490.0, 4495.0, 4500.0]

def test_breakeven_skipped_when_price_has_not_cleared_it():
    snap = MarketSnapshot(bid=4482.0, ask=4482.10, tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, snap, POLICY, tps=TPS)
    assert plan.intents == ()

def test_close_half_closes_ceil_of_open_legs():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY, tps=TPS)
    assert len(plan.intents) == 2

def test_close_half_picks_furthest_tps_first():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY, tps=TPS)
    assert {i.position_ticket for i in plan.intents} == {1002, 1001}

def test_close_half_picks_furthest_tps_first_on_a_sell():
    # C1: "furthest" is the LOWEST tp for a sell. Sorting by raw tp descending
    # closes the two legs nearest to paying out and keeps the long-shots --
    # the exact inverse of spec 8.2.
    sells = tuple(
        LegView(position_ticket=4000 + i, leg_index=i, tp_index=i, side=Side.SELL,
                volume=0.01, price_open=4480.0, sl=4495.0, tp=tp, is_pending=False)
        for i, tp in enumerate((4470.0, 4465.0, 4460.0, 4455.0))
    )
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), sells, SPEC, SNAP, POLICY,
                         tps=(4470.0, 4465.0, 4460.0, 4455.0))
    assert len(plan.intents) == 2
    assert {i.position_ticket for i in plan.intents} == {4002, 4003}

def test_close_half_closes_whole_legs_never_fractions():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY, tps=TPS)
    assert all(i.volume == 0.01 for i in plan.intents)

def test_close_all_covers_every_leg():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), legs(4), SPEC, SNAP, POLICY, tps=TPS)
    assert len(plan.intents) == 4
    assert all(isinstance(i, CloseIntent) for i in plan.intents)

def test_cancel_pending_only_touches_pending_legs():
    mixed = legs(2) + (
        LegView(position_ticket=2000, leg_index=2, tp_index=2, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
    )
    plan = plan_followup(fu(FollowUpOp.CANCEL_PENDING), mixed, SPEC, SNAP, POLICY, tps=TPS)
    # Item 1 fix: pending legs now yield CancelIntent, whose ticket field is
    # order_ticket (not position_ticket) — this line necessarily changes
    # along with the field rename the brief mandates; the assertion's
    # substance (only the pending leg's ticket, 2000, is touched) is
    # unchanged.
    assert [i.order_ticket for i in plan.intents] == [2000]

def test_no_legs_is_a_noop_not_an_exception():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), (), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is not None


# --- Item 1: CancelIntent for pending legs -------------------------------

def test_cancel_pending_emits_cancel_intent_not_close_intent():
    mixed = legs(2) + (
        LegView(position_ticket=2000, leg_index=2, tp_index=2, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
    )
    plan = plan_followup(fu(FollowUpOp.CANCEL_PENDING), mixed, SPEC, SNAP, POLICY, tps=TPS)
    assert len(plan.intents) == 1
    assert isinstance(plan.intents[0], CancelIntent)
    assert not isinstance(plan.intents[0], CloseIntent)
    assert plan.intents[0].order_ticket == 2000

def test_close_all_with_open_and_pending_legs():
    mixed = legs(2) + (
        LegView(position_ticket=2000, leg_index=2, tp_index=2, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
    )
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), mixed, SPEC, SNAP, POLICY, tps=TPS)
    assert len(plan.intents) == 3
    assert sum(isinstance(i, CloseIntent) for i in plan.intents) == 2
    assert sum(isinstance(i, CancelIntent) for i in plan.intents) == 1
    cancels = [i for i in plan.intents if isinstance(i, CancelIntent)]
    assert cancels[0].order_ticket == 2000

def test_close_all_with_only_open_legs_has_no_cancel_intents():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), legs(3), SPEC, SNAP, POLICY, tps=TPS)
    assert all(isinstance(i, CloseIntent) for i in plan.intents)
    assert not any(isinstance(i, CancelIntent) for i in plan.intents)


# --- Item 2: AMEND_TP geometry validation ---------------------------------
#
# C2 note: every case here now carries tp_index_hint=1, naming tp_index 0 --
# the leg these single-leg fixtures always produced. The substance of each
# assertion (geometry is validated against the leg being amended) is
# unchanged; a hintless TP amend is now its own reject, covered below.

def test_amend_tp_buy_below_sl_rejected():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4465.0, tp_index_hint=1),
                         legs(1), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT

def test_amend_tp_buy_below_ask_rejected():
    # 4485.0 is above the leg's sl (4470.0) but still below the live ask
    # (4488.10) -- isolates the ask-side check from the sl-side check.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4485.0, tp_index_hint=1),
                         legs(1), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT

def test_amend_tp_buy_comfortably_above_ask_and_sl_accepted():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4495.0, tp_index_hint=1),
                         legs(1), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.reason is None
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert isinstance(intent, ModifyIntent)
    assert intent.tp == 4495.0
    assert intent.sl == 4470.0  # leg's existing sl preserved

def test_amend_tp_sell_mirror_accepted():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4480.0, tp_index_hint=1),
                         sell_legs(1), SPEC, SNAP, POLICY, tps=SELL_TPS)
    assert plan.reason is None
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.tp == 4480.0
    assert intent.sl == 4506.0  # leg's existing sl preserved


# --- C2: AMEND_TP must honour tp_index_hint, never fan one price across the
# whole ladder. ------------------------------------------------------------

def test_amend_tp_with_index_hint_touches_only_that_leg():
    # "TP2 to 4550" on a 4-leg ladder: exactly one ModifyIntent, for the leg
    # whose tp_index is 1 (the hint is 1-based, tp_index is 0-based). On this
    # one-per-TP fixture that leg is also ordinal 1.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=2),
                         legs(4), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.reason is None
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.leg_index == 1
    assert intent.position_ticket == 1001
    assert intent.tp == 4550.0
    assert intent.sl == 4470.0

def test_amend_tp_leaves_the_other_legs_targets_untouched():
    # The defect this pins: the other three legs must not be rewritten to
    # 4550 as a side effect. No intent means no modification is sent.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=2),
                         legs(4), SPEC, SNAP, POLICY, tps=TPS)
    touched = {i.position_ticket for i in plan.intents}
    assert touched == {1001}
    assert {1000, 1002, 1003} & touched == set()

def test_amend_tp_out_of_range_index_is_ambiguous():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=9),
                         legs(4), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.AMBIGUOUS_TARGET
    assert any("9" in note for note in plan.notes)

def test_amend_tp_without_an_index_is_ambiguous():
    # A bare "tp to 4500": fanning one price across a ladder is never what
    # the poster meant, so this fails loudly rather than acting wrongly.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4500.0),
                         legs(4), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.AMBIGUOUS_TARGET
    assert plan.notes

def test_amend_tp_geometry_still_applies_to_the_targeted_leg():
    # Index resolution must not bypass the geometry check: 4485.0 is below
    # the live ask, so even a correctly-targeted leg is refused.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4485.0, tp_index_hint=3),
                         legs(4), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT

def test_amend_sl_still_touches_every_open_leg():
    # The index hint is a TP concept only -- one signal has one shared stop,
    # so an SL amend must still reach all four legs.
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0),
                         legs(4), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.reason is None
    assert len(plan.intents) == 4
    assert all(i.sl == 4480.0 for i in plan.intents)


# --- Item 3: minimum-stop-distance check on AMEND_SL / SL_TO_BE -----------

def test_amend_sl_buy_valid_new_stop_accepted():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0), legs(1), SPEC, SNAP, POLICY,
                         tps=TPS)
    assert plan.reason is None
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.sl == 4480.0
    assert intent.tp == 4490.0  # leg's existing tp preserved

def test_amend_sl_buy_wrong_side_rejected():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4489.0), legs(1), SPEC, SNAP, POLICY,
                         tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT

def test_amend_sl_buy_right_side_but_inside_min_dist_rejected():
    # bid=4488.0, min_dist = max(0, 3.0 * 0.10) = 0.30; 4487.85 is only
    # 0.15 from price -- right side, but too tight.
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4487.85), legs(1), SPEC, SNAP, POLICY,
                         tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.STOPS_TOO_TIGHT

def test_sl_to_be_all_legs_inside_min_dist_is_not_applicable():
    # Construct a snapshot (not the shared fixture) where the breakeven
    # price sits on the right side of the market but within min_dist of it.
    snap = MarketSnapshot(bid=4482.35, ask=4482.45, tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
    # be = 4482.10 (fill) + 1 pip x 0.10 (buffer) = 4482.20
    # min_dist = max(0, 3.0 * 0.10) = 0.30; distance to bid = 0.15 < 0.30
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(1), SPEC, snap, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE
    assert plan.notes


# --- Item 4: NOT_APPLICABLE for empty open_legs / all-skipped -------------

_ALL_PENDING = (
    LegView(position_ticket=5000, leg_index=0, tp_index=2, side=Side.BUY, volume=0.01,
            price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
)

# I8 note: these two cases are pending-only, so they now report
# PENDING_ONLY rather than NOT_APPLICABLE. The substance is unchanged --
# still no intents, still a note -- but the reason no longer reads as
# "nothing to do" for a case where a human must look. The genuinely-empty
# case below keeps NOT_APPLICABLE.

def test_amend_sl_against_pending_only_legs_is_pending_only():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0), _ALL_PENDING,
                         SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.PENDING_ONLY
    assert plan.notes
    assert "1" in plan.notes[0]

def test_amend_tp_against_pending_only_legs_is_pending_only():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4500.0, tp_index_hint=1),
                         _ALL_PENDING, SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.PENDING_ONLY
    assert plan.notes

def test_amend_note_counts_every_pending_leg_carrying_the_stale_stop():
    four_pending = tuple(
        LegView(position_ticket=5000 + i, leg_index=i, tp_index=i, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4490.0 + 5 * i, is_pending=True)
        for i in range(4)
    )
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0),
                         four_pending, SPEC, SNAP, POLICY, tps=TPS)
    assert plan.reason is RejectReason.PENDING_ONLY
    assert "4" in plan.notes[0]

def test_amend_with_no_legs_at_all_is_still_not_applicable():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0), (), SPEC, SNAP, POLICY,
                         tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE

def test_sl_to_be_all_legs_pending_is_not_applicable():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), _ALL_PENDING, SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE
    assert plan.notes


# --- Plan 1b: a TP amend names the take profit, never the position ---------
#
# Since Plan 1b a leg's leg_index is the position's ordinal (0..N-1) and its
# tp_index the index into sig.tps of the TP it targets (see OrderIntent).
# The two part whenever a TP had passed at entry, or N != T. "TPk to X"
# names tp_index k-1 and moves EVERY open position on that TP -- never the
# position whose ordinal is k-1.

LADDER = (4490.0, 4495.0, 4500.0, 4505.0)
SELL_LADDER = (4475.0, 4470.0, 4465.0, 4460.0)


def staggered(tp_indices, side=Side.BUY):
    # Open legs numbered the way plan_entry numbers them: ordinal i targets
    # tp_indices[i].
    if side is Side.BUY:
        ladder, sl, price_open = LADDER, 4470.0, 4482.10
    else:
        ladder, sl, price_open = SELL_LADDER, 4495.0, 4482.0
    return tuple(
        LegView(position_ticket=6000 + i, leg_index=i, tp_index=k, side=side,
                volume=0.01, price_open=price_open, sl=sl, tp=ladder[k],
                is_pending=False)
        for i, k in enumerate(tp_indices)
    )


def test_amend_tp2_after_tp1_passed_moves_the_tp2_position():
    # TP1 was behind price at entry, so ordinal 0 targets TP2. Matching on
    # leg_index would move ordinal 1, the TP3 position.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=2),
                         staggered((1, 2, 3)), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.reason is None
    assert [(i.position_ticket, i.leg_index, i.sl, i.tp) for i in plan.intents] == [
        (6000, 0, 4470.0, 4550.0)]


def test_amend_tp4_after_tp1_passed_moves_the_last_position():
    # Three positions, so there is no ordinal 3: matching on leg_index would
    # find nothing for TP4.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=4),
                         staggered((1, 2, 3)), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.reason is None
    assert [(i.position_ticket, i.leg_index, i.tp) for i in plan.intents] == [
        (6002, 2, 4550.0)]


def test_amend_tp1_after_tp1_passed_names_no_position():
    # No position was placed on a TP that had already passed. Ordinal 0 is
    # the TP2 position and must not move.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=1),
                         staggered((1, 2, 3)), SPEC, SNAP, POLICY, tps=TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.AMBIGUOUS_TARGET
    assert plan.notes == ("no open leg for TP1 (tp_index 0)",)


def test_amend_tp_moves_every_position_sharing_a_tp():
    # Six positions over four TPs cycle (amendment 2026-10-21): TP1 carries
    # ordinals 0 and 4, TP2 ordinals 1 and 5. "TP2 to 4550" moves both,
    # each keeping its own stop; the other positions are untouched.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=2),
                         staggered((0, 1, 2, 3, 0, 1)), SPEC, SNAP, POLICY, tps=LADDER)
    assert plan.reason is None
    assert [(i.position_ticket, i.leg_index, i.sl, i.tp) for i in plan.intents] == [
        (6001, 1, 4470.0, 4550.0),
        (6005, 5, 4470.0, 4550.0),
    ]


def test_amend_tp5_with_six_positions_on_four_tps_names_no_position():
    # There is no TP5. Matching on leg_index would move ordinal 4, a TP1
    # position.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=5),
                         staggered((0, 1, 2, 3, 0, 1)), SPEC, SNAP, POLICY, tps=LADDER)
    assert plan.intents == ()
    assert plan.reason is RejectReason.AMBIGUOUS_TARGET


def test_amend_tp_moves_every_position_sharing_a_tp_on_a_sell():
    # SELL mirror, TP1 passed, five positions cycle over TP2..TP4: TP2
    # carries ordinals 0 and 3.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4440.0, tp_index_hint=2),
                         staggered((1, 2, 3, 1, 2), side=Side.SELL), SPEC, SNAP, POLICY,
                         tps=SELL_LADDER)
    assert plan.reason is None
    assert [(i.position_ticket, i.leg_index, i.sl, i.tp) for i in plan.intents] == [
        (6000, 0, 4495.0, 4440.0),
        (6003, 3, 4495.0, 4440.0),
    ]


def _entry_market(bid):
    return MarketSnapshot(bid=bid, ask=bid + 0.10, tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)


def _legs_read_back_from_comments(plan):
    # What a broker-side LegView producer does: read both numbers back from
    # the comment "{base36}-{leg}t{tp}". Every leg is treated as filled.
    out = []
    for ticket, order in enumerate(plan.legs, start=7000):
        leg_part, tp_part = order.comment.rsplit("-", 1)[1].split("t")
        out.append(LegView(position_ticket=ticket, leg_index=int(leg_part),
                           tp_index=int(tp_part), side=order.side, volume=order.volume,
                           price_open=order.price, sl=order.sl, tp=order.tp,
                           is_pending=False))
    return tuple(out)


@pytest.mark.parametrize(("positions", "bid"), [
    (None, 4482.0), (4, 4482.0), (6, 4482.0), (2, 4482.0),
    (3, 4491.0), (5, 4491.0),
], ids=["none", "n4", "n6", "n2", "n3-tp1-passed", "n5-tp1-passed"])
def test_amend_tp_moves_exactly_the_positions_the_planner_aimed_at_that_tp(positions, bid):
    sig = Signal(channel_id=-1, message_id=4001, side=Side.BUY, symbol_alias="XAUUSD",
                 zone=EntryZone(4480.0, 4485.0), sl=4470.0, tps=LADDER,
                 posted_at_utc=NOW - timedelta(seconds=5), content_hash="h")
    account = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001,
                          positions=positions)
    plan = plan_entry(sig, account, SPEC, _entry_market(bid), POLICY)
    assert plan.accepted
    live = _legs_read_back_from_comments(plan)
    # The comment carries the planner's own two numbers.
    assert [(v.leg_index, v.tp_index) for v in live] == [
        (o.leg_index, o.tp_index) for o in plan.legs]
    for hint in range(1, len(LADDER) + 1):
        aimed = {v.position_ticket for v, o in zip(live, plan.legs)
                 if o.tp_index == hint - 1}
        result = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=hint),
                               live, SPEC, SNAP, POLICY, tps=sig.tps)
        if aimed:
            assert result.reason is None, hint
            assert {i.position_ticket for i in result.intents} == aimed, hint
            assert all(i.tp == 4550.0 for i in result.intents)
        else:
            assert result.intents == (), hint
            assert result.reason is RejectReason.AMBIGUOUS_TARGET, hint


# --- Plan 1b Task 4: a follow-up may tighten the stop, never loosen it -----
#
# The worked example's basket, after the trail (amendment 2026-10-21): BUY,
# TPs 4490/4495/4500/4505, every position filled at 4482.55 (breakeven
# 4482.55 + 1 pip = 4482.65). Price reached TP2, so the TP1 and TP2
# positions closed and the trail moved the stop of the other two to TP1,
# 4490.00. The SELL basket is its mirror: TPs 4510/4505/4500/4495, fills
# 4517.45 (breakeven 4517.35), trailed to TP1 = 4510.00 once the ask reached
# TP2. SPEC is 2-digit and min_dist = 3 x 0.10 = 0.30.

BUY_BASKET_TPS = (4490.0, 4495.0, 4500.0, 4505.0)   # the signal's, as posted
SELL_BASKET_TPS = (4510.0, 4505.0, 4500.0, 4495.0)
BUY_TRAILED_TPS = (4500.0, 4505.0)       # TP3, TP4, still open
SELL_TRAILED_TPS = (4500.0, 4495.0)


def basket_tps(side):
    return BUY_BASKET_TPS if side is Side.BUY else SELL_BASKET_TPS


def trailed(side=Side.BUY, sl=None, price_open=None):
    """The two positions left open once the trail moved the stop to TP1."""
    if side is Side.BUY:
        tps, sl, price_open = BUY_TRAILED_TPS, sl or 4490.0, price_open or 4482.55
    else:
        tps, sl, price_open = SELL_TRAILED_TPS, sl or 4510.0, price_open or 4517.45
    return tuple(
        LegView(position_ticket=8001 + k, leg_index=k + 2, tp_index=k + 2, side=side,
                volume=0.01, price_open=price_open, sl=sl, tp=tp, is_pending=False)
        for k, tp in enumerate(tps)
    )


def one_leg(ticket, side, price_open, sl, tp, tp_index):
    return LegView(position_ticket=ticket, leg_index=tp_index, tp_index=tp_index, side=side,
                   volume=0.01, price_open=price_open, sl=sl, tp=tp, is_pending=False)


def market(bid):
    return MarketSnapshot(bid=bid, ask=round(bid + 0.10, 2), tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)


BUY_PAST_TP1 = market(4492.50)     # bid past TP1, short of TP2: the stop is at BE
SELL_PAST_TP1 = market(4507.40)    # ask 4507.50: past TP1 (4510), short of TP2 (4505)
BUY_PAST_TP2 = market(4496.00)     # bid past TP2, short of TP3: the stop is at TP1
SELL_PAST_TP2 = market(4503.90)    # ask 4504.00: past TP2 (4505), short of TP3 (4500)


def test_would_loosen_is_its_own_reject_reason():
    # Persisted with the follow-up's outcome, so the string is pinned.
    assert RejectReason.WOULD_LOOSEN.value == "would_loosen"


def test_amend_sl_below_a_buy_basket_trailed_to_tp1_would_loosen():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4485.0),
                         trailed(), SPEC, BUY_PAST_TP2, POLICY, tps=BUY_BASKET_TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.WOULD_LOOSEN
    (note,) = plan.notes
    assert "4485.0" in note and "4490.0" in note


def test_amend_sl_above_a_buy_basket_trailed_to_tp1_is_applied():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4491.50),
                         trailed(), SPEC, BUY_PAST_TP2, POLICY, tps=BUY_BASKET_TPS)
    assert plan.reason is None
    assert [(i.position_ticket, i.sl, i.tp) for i in plan.intents] == [
        (8001, 4491.5, 4500.0), (8002, 4491.5, 4505.0)]


def test_amend_sl_above_a_sell_basket_trailed_to_tp1_would_loosen():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4515.0),
                         trailed(Side.SELL), SPEC, SELL_PAST_TP2, POLICY, tps=SELL_BASKET_TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.WOULD_LOOSEN
    (note,) = plan.notes
    assert "4515.0" in note and "4510.0" in note


def test_amend_sl_below_a_sell_basket_trailed_to_tp1_is_applied():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4508.50),
                         trailed(Side.SELL), SPEC, SELL_PAST_TP2, POLICY, tps=SELL_BASKET_TPS)
    assert plan.reason is None
    assert [(i.position_ticket, i.sl, i.tp) for i in plan.intents] == [
        (8001, 4508.5, 4500.0), (8002, 4508.5, 4495.0)]


def test_amend_sl_moves_only_the_buy_legs_it_tightens():
    # Breakeven is per position: a market fill at 4482.55 sits at 4482.65,
    # a pending fill at 4483.30 at 4483.40. "SL to 4483" tightens the first
    # and would loosen the second, which keeps its stop.
    legs_ = (one_leg(8101, Side.BUY, 4482.55, 4482.65, 4490.0, 0),
             one_leg(8102, Side.BUY, 4483.30, 4483.40, 4495.0, 1))
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4483.0),
                         legs_, SPEC, market(4491.0), POLICY, tps=BUY_BASKET_TPS)
    assert plan.reason is None
    assert [(i.position_ticket, i.sl, i.tp) for i in plan.intents] == [(8101, 4483.0, 4490.0)]
    (note,) = plan.notes
    assert "4483.0" in note and "4483.4" in note


def test_amend_sl_moves_only_the_sell_legs_it_tightens():
    # SELL mirror: fills 4517.45 and 4516.70, breakevens 4517.35 and 4516.60.
    # "SL to 4517" is lower than 4517.35 (tighter) and higher than 4516.60
    # (looser).
    legs_ = (one_leg(8201, Side.SELL, 4517.45, 4517.35, 4510.0, 0),
             one_leg(8202, Side.SELL, 4516.70, 4516.60, 4505.0, 1))
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4517.0),
                         legs_, SPEC, market(4509.0), POLICY, tps=SELL_BASKET_TPS)
    assert plan.reason is None
    assert [(i.position_ticket, i.sl, i.tp) for i in plan.intents] == [(8201, 4517.0, 4510.0)]
    (note,) = plan.notes
    assert "4517.0" in note and "4516.6" in note


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_amend_sl_names_every_distinct_stop_it_would_loosen(side):
    if side is Side.BUY:
        legs_ = (one_leg(8301, side, 4482.55, 4482.65, 4490.0, 0),
                 one_leg(8302, side, 4483.30, 4483.40, 4495.0, 1))
        new, snap = 4482.0, market(4491.0)
    else:
        legs_ = (one_leg(8301, side, 4517.45, 4517.35, 4510.0, 0),
                 one_leg(8302, side, 4516.70, 4516.60, 4505.0, 1))
        new, snap = 4518.0, market(4509.0)
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=new), legs_, SPEC, snap, POLICY,
                         tps=basket_tps(side))
    assert plan.intents == ()
    assert plan.reason is RejectReason.WOULD_LOOSEN
    (note,) = plan.notes
    assert all(str(p) in note for p in (new, legs_[0].sl, legs_[1].sl))


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
@pytest.mark.parametrize("noise", [0.0, 1e-7, -1e-7], ids=["exact", "above", "below"])
def test_amend_sl_to_the_stop_already_in_place_is_not_applicable(side, noise):
    # Nothing to move, nothing refused. The broker reports the stop as a
    # float, so it is compared on the symbol's tick grid: a hair off the
    # grid neither loosens nor tightens it.
    stop = 4490.0 if side is Side.BUY else 4510.0
    snap = BUY_PAST_TP2 if side is Side.BUY else SELL_PAST_TP2
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=stop),
                         trailed(side, sl=stop + noise), SPEC, snap, POLICY, tps=basket_tps(side))
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE
    assert plan.notes


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_amend_sl_level_with_one_leg_and_looser_than_another_would_loosen(side):
    # Nothing tightens: one leg is already there, the other is refused.
    if side is Side.BUY:
        legs_ = (one_leg(8401, side, 4482.55, 4482.65, 4490.0, 0),
                 one_leg(8402, side, 4483.30, 4483.40, 4495.0, 1))
        new, snap = 4482.65, market(4491.0)
    else:
        legs_ = (one_leg(8401, side, 4517.45, 4517.35, 4510.0, 0),
                 one_leg(8402, side, 4516.70, 4516.60, 4505.0, 1))
        new, snap = 4517.35, market(4509.0)
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=new), legs_, SPEC, snap, POLICY,
                         tps=basket_tps(side))
    assert plan.intents == ()
    assert plan.reason is RejectReason.WOULD_LOOSEN


@pytest.mark.parametrize(("side", "new"), [(Side.BUY, 4480.0), (Side.SELL, 4506.0)])
def test_amend_sl_on_a_leg_with_no_stop_is_applied(side, new):
    # MT5 reports a missing stop as 0.0. It is the loosest stop there is,
    # though numerically below every SELL stop: any amend tightens it.
    view = one_leg(8501, side, 4482.10 if side is Side.BUY else 4494.0, 0.0,
                   4490.0 if side is Side.BUY else 4480.0, 0)
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=new), (view,), SPEC, SNAP, POLICY,
                         tps=TPS if side is Side.BUY else SELL_TPS)
    assert plan.reason is None
    assert [(i.position_ticket, i.sl) for i in plan.intents] == [(8501, new)]


def test_amend_sl_that_tightens_still_meets_the_geometry_checks():
    # The guard runs before today's checks, not instead of them: 4497 is
    # tighter than 4490 but above the bid (4496).
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4497.0),
                         trailed(), SPEC, BUY_PAST_TP2, POLICY, tps=BUY_BASKET_TPS)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_sl_to_be_after_the_trail_reached_tp1_is_not_applicable(side):
    # Breakeven (4482.65 / 4517.35) is looser than TP1 (4490 / 4510): the
    # normal case once the trail has moved the stop to TP1, so no error.
    snap = BUY_PAST_TP2 if side is Side.BUY else SELL_PAST_TP2
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), trailed(side), SPEC, snap, POLICY,
                         tps=basket_tps(side))
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE
    (note,) = plan.notes
    assert note.startswith("2 leg(s) skipped for breakeven (2 already at or tighter than it")


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_sl_to_be_with_the_stop_already_at_breakeven_is_not_applicable(side):
    # Past TP1, short of TP2: the trail has placed breakeven, fill +/- 1 pip.
    if side is Side.BUY:
        view, snap = one_leg(8601, side, 4482.55, 4482.65, 4495.0, 1), BUY_PAST_TP1
    else:
        view, snap = one_leg(8601, side, 4517.45, 4517.35, 4505.0, 1), SELL_PAST_TP1
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), (view,), SPEC, snap, POLICY, tps=basket_tps(side))
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_sl_to_be_moves_only_the_legs_still_looser_than_breakeven(side):
    # One leg trailed to TP1; its sibling kept the signal's stop (the trail's
    # modify for it failed). Only the sibling moves, to its own breakeven.
    if side is Side.BUY:
        legs_ = (one_leg(8701, side, 4482.55, 4490.0, 4500.0, 2),
                 one_leg(8702, side, 4482.55, 4470.0, 4505.0, 3))
        snap, be, tp = BUY_PAST_TP2, 4482.65, 4505.0
    else:
        legs_ = (one_leg(8701, side, 4517.45, 4510.0, 4500.0, 2),
                 one_leg(8702, side, 4517.45, 4530.0, 4495.0, 3))
        snap, be, tp = SELL_PAST_TP2, 4517.35, 4495.0
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs_, SPEC, snap, POLICY, tps=basket_tps(side))
    assert plan.reason is None
    assert [(i.position_ticket, i.sl, i.tp) for i in plan.intents] == [(8702, be, tp)]


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_sl_to_be_on_a_leg_with_no_stop_moves_it_to_breakeven(side):
    # 0.0 is MT5's "no stop", never a stop tighter than breakeven:
    # 4482.10 + 0.10, or 4494.00 - 0.10.
    if side is Side.BUY:
        view, be = one_leg(8801, side, 4482.10, 0.0, 4490.0, 0), 4482.20
    else:
        view, be = one_leg(8801, side, 4494.0, 0.0, 4480.0, 0), 4493.90
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), (view,), SPEC, SNAP, POLICY,
                         tps=TPS if side is Side.BUY else SELL_TPS)
    assert plan.reason is None
    assert [(i.position_ticket, i.sl) for i in plan.intents] == [(8801, be)]
