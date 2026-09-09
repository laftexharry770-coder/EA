from datetime import datetime, UTC

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.followup_planner import LegView, plan_followup
from xaucopier.domain.intent import CancelIntent, CloseIntent, ModifyIntent
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

def sell_legs(n=1, price_open=4494.0):
    return tuple(
        LegView(position_ticket=3000 + i, leg_index=i, side=Side.SELL, volume=0.01,
                price_open=price_open, sl=4506.0, tp=4480.0 - 5 * i, is_pending=False)
        for i in range(n)
    )

def fu(op, price_hint=None, tp_index_hint=None):
    return FollowUp(channel_id=-1, message_id=9, op=op, posted_at_utc=NOW,
                    price_hint=price_hint, tp_index_hint=tp_index_hint)

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

def test_close_half_picks_furthest_tps_first_on_a_sell():
    # C1: "furthest" is the LOWEST tp for a sell. Sorting by raw tp descending
    # closes the two legs nearest to paying out and keeps the long-shots --
    # the exact inverse of spec 8.2.
    sells = tuple(
        LegView(position_ticket=4000 + i, leg_index=i, side=Side.SELL, volume=0.01,
                price_open=4480.0, sl=4495.0, tp=tp, is_pending=False)
        for i, tp in enumerate((4470.0, 4465.0, 4460.0, 4455.0))
    )
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), sells, SPEC, SNAP, POLICY)
    assert len(plan.intents) == 2
    assert {i.position_ticket for i in plan.intents} == {4002, 4003}

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
    # Item 1 fix: pending legs now yield CancelIntent, whose ticket field is
    # order_ticket (not position_ticket) — this line necessarily changes
    # along with the field rename the brief mandates; the assertion's
    # substance (only the pending leg's ticket, 2000, is touched) is
    # unchanged.
    assert [i.order_ticket for i in plan.intents] == [2000]

def test_no_legs_is_a_noop_not_an_exception():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), (), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is not None


# --- Item 1: CancelIntent for pending legs -------------------------------

def test_cancel_pending_emits_cancel_intent_not_close_intent():
    mixed = legs(2) + (
        LegView(position_ticket=2000, leg_index=2, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
    )
    plan = plan_followup(fu(FollowUpOp.CANCEL_PENDING), mixed, SPEC, SNAP, POLICY)
    assert len(plan.intents) == 1
    assert isinstance(plan.intents[0], CancelIntent)
    assert not isinstance(plan.intents[0], CloseIntent)
    assert plan.intents[0].order_ticket == 2000

def test_close_all_with_open_and_pending_legs():
    mixed = legs(2) + (
        LegView(position_ticket=2000, leg_index=2, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
    )
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), mixed, SPEC, SNAP, POLICY)
    assert len(plan.intents) == 3
    assert sum(isinstance(i, CloseIntent) for i in plan.intents) == 2
    assert sum(isinstance(i, CancelIntent) for i in plan.intents) == 1
    cancels = [i for i in plan.intents if isinstance(i, CancelIntent)]
    assert cancels[0].order_ticket == 2000

def test_close_all_with_only_open_legs_has_no_cancel_intents():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), legs(3), SPEC, SNAP, POLICY)
    assert all(isinstance(i, CloseIntent) for i in plan.intents)
    assert not any(isinstance(i, CancelIntent) for i in plan.intents)


# --- Item 2: AMEND_TP geometry validation ---------------------------------
#
# C2 note: every case here now carries tp_index_hint=1, naming leg_index 0 --
# the leg these single-leg fixtures always produced. The substance of each
# assertion (geometry is validated against the leg being amended) is
# unchanged; a hintless TP amend is now its own reject, covered below.

def test_amend_tp_buy_below_sl_rejected():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4465.0, tp_index_hint=1),
                         legs(1), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT

def test_amend_tp_buy_below_ask_rejected():
    # 4485.0 is above the leg's sl (4470.0) but still below the live ask
    # (4488.10) -- isolates the ask-side check from the sl-side check.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4485.0, tp_index_hint=1),
                         legs(1), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT

def test_amend_tp_buy_comfortably_above_ask_and_sl_accepted():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4495.0, tp_index_hint=1),
                         legs(1), SPEC, SNAP, POLICY)
    assert plan.reason is None
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert isinstance(intent, ModifyIntent)
    assert intent.tp == 4495.0
    assert intent.sl == 4470.0  # leg's existing sl preserved

def test_amend_tp_sell_mirror_accepted():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4480.0, tp_index_hint=1),
                         sell_legs(1), SPEC, SNAP, POLICY)
    assert plan.reason is None
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.tp == 4480.0
    assert intent.sl == 4506.0  # leg's existing sl preserved


# --- C2: AMEND_TP must honour tp_index_hint, never fan one price across the
# whole ladder. ------------------------------------------------------------

def test_amend_tp_with_index_hint_touches_only_that_leg():
    # "TP2 to 4550" on a 4-leg ladder: exactly one ModifyIntent, for the leg
    # whose leg_index is 1 (the hint is 1-based, leg_index is 0-based).
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=2),
                         legs(4), SPEC, SNAP, POLICY)
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
                         legs(4), SPEC, SNAP, POLICY)
    touched = {i.position_ticket for i in plan.intents}
    assert touched == {1001}
    assert {1000, 1002, 1003} & touched == set()

def test_amend_tp_out_of_range_index_is_ambiguous():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4550.0, tp_index_hint=9),
                         legs(4), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.AMBIGUOUS_TARGET
    assert any("9" in note for note in plan.notes)

def test_amend_tp_without_an_index_is_ambiguous():
    # A bare "tp to 4500": fanning one price across a ladder is never what
    # the poster meant, so this fails loudly rather than acting wrongly.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4500.0),
                         legs(4), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.AMBIGUOUS_TARGET
    assert plan.notes

def test_amend_tp_geometry_still_applies_to_the_targeted_leg():
    # Index resolution must not bypass the geometry check: 4485.0 is below
    # the live ask, so even a correctly-targeted leg is refused.
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4485.0, tp_index_hint=3),
                         legs(4), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT

def test_amend_sl_still_touches_every_open_leg():
    # The index hint is a TP concept only -- one signal has one shared stop,
    # so an SL amend must still reach all four legs.
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0),
                         legs(4), SPEC, SNAP, POLICY)
    assert plan.reason is None
    assert len(plan.intents) == 4
    assert all(i.sl == 4480.0 for i in plan.intents)


# --- Item 3: minimum-stop-distance check on AMEND_SL / SL_TO_BE -----------

def test_amend_sl_buy_valid_new_stop_accepted():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0), legs(1), SPEC, SNAP, POLICY)
    assert plan.reason is None
    assert len(plan.intents) == 1
    intent = plan.intents[0]
    assert intent.sl == 4480.0
    assert intent.tp == 4490.0  # leg's existing tp preserved

def test_amend_sl_buy_wrong_side_rejected():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4489.0), legs(1), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.GEOMETRY_CONFLICT

def test_amend_sl_buy_right_side_but_inside_min_dist_rejected():
    # bid=4488.0, min_dist = max(0, 3.0 * 0.10) = 0.30; 4487.85 is only
    # 0.15 from price -- right side, but too tight.
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4487.85), legs(1), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.STOPS_TOO_TIGHT

def test_sl_to_be_all_legs_inside_min_dist_is_not_applicable():
    # Construct a snapshot (not the shared fixture) where the breakeven
    # price sits on the right side of the market but within min_dist of it.
    snap = MarketSnapshot(bid=4482.35, ask=4482.45, tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
    # be = 4482.10 (fill) + 0.10 (spread) + 0.10 (buffer) = 4482.30
    # min_dist = max(0, 3.0 * 0.10) = 0.30; distance to bid = 0.05 < 0.30
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(1), SPEC, snap, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE
    assert plan.notes


# --- Item 4: NOT_APPLICABLE for empty open_legs / all-skipped -------------

_ALL_PENDING = (
    LegView(position_ticket=5000, leg_index=0, side=Side.BUY, volume=0.01,
            price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
)

# I8 note: these two cases are pending-only, so they now report
# PENDING_ONLY rather than NOT_APPLICABLE. The substance is unchanged --
# still no intents, still a note -- but the reason no longer reads as
# "nothing to do" for a case where a human must look. The genuinely-empty
# case below keeps NOT_APPLICABLE.

def test_amend_sl_against_pending_only_legs_is_pending_only():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0), _ALL_PENDING, SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.PENDING_ONLY
    assert plan.notes
    assert "1" in plan.notes[0]

def test_amend_tp_against_pending_only_legs_is_pending_only():
    plan = plan_followup(fu(FollowUpOp.AMEND_TP, price_hint=4500.0, tp_index_hint=1),
                         _ALL_PENDING, SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.PENDING_ONLY
    assert plan.notes

def test_amend_note_counts_every_pending_leg_carrying_the_stale_stop():
    four_pending = tuple(
        LegView(position_ticket=5000 + i, leg_index=i, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4490.0 + 5 * i, is_pending=True)
        for i in range(4)
    )
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0),
                         four_pending, SPEC, SNAP, POLICY)
    assert plan.reason is RejectReason.PENDING_ONLY
    assert "4" in plan.notes[0]

def test_amend_with_no_legs_at_all_is_still_not_applicable():
    plan = plan_followup(fu(FollowUpOp.AMEND_SL, price_hint=4480.0), (), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE

def test_sl_to_be_all_legs_pending_is_not_applicable():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), _ALL_PENDING, SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is RejectReason.NOT_APPLICABLE
    assert plan.notes
