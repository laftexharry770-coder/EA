import dataclasses
import pytest
from datetime import datetime, UTC
from xaucopier.domain.signal import Side, EntryZone, Signal
from xaucopier.domain.intent import Plan, OrderKind
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.market import Policy


def _sig(**kw):
    base = dict(
        channel_id=-1001234, message_id=42, side=Side.BUY, symbol_alias="XAUUSD",
        zone=EntryZone(4480.0, 4485.0), sl=4470.0, tps=(4490.0, 4495.0),
        posted_at_utc=datetime(2026, 9, 5, 10, 45, tzinfo=UTC),
        content_hash="abc", source="live",
    )
    base.update(kw)
    return Signal(**base)


def test_signal_is_frozen():
    s = _sig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.sl = 1.0


def test_entry_zone_orders_its_bounds():
    assert EntryZone(4485.0, 4480.0) == EntryZone(4480.0, 4485.0)


def test_zone_favourable_edge_is_low_for_buy():
    assert EntryZone(4480.0, 4485.0).favourable_edge(Side.BUY) == 4480.0
    assert EntryZone(4480.0, 4485.0).favourable_edge(Side.SELL) == 4485.0


def test_signal_requires_aware_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        _sig(posted_at_utc=datetime(2026, 9, 5, 10, 45))


def test_signal_requires_at_least_one_tp():
    with pytest.raises(ValueError, match="at least one"):
        _sig(tps=())


def test_plan_rejected_carries_reason_and_no_legs():
    p = Plan.rejected(RejectReason.STALE)
    assert p.accepted is False
    assert p.reason is RejectReason.STALE
    assert p.legs == ()


def test_order_kind_members():
    assert {k.name for k in OrderKind} == {"MARKET", "LIMIT"}


def test_policy_from_dict_applies_known_keys():
    p = Policy.from_dict({"max_spread_usd": 0.25, "pending_at": "near"})
    assert p.max_spread_usd == 0.25
    assert p.pending_at == "near"


def test_policy_from_dict_ignores_unknown_keys():
    p = Policy.from_dict({"max_spread_usd": 0.25, "future_setting": True})
    assert p.max_spread_usd == 0.25
    assert p.max_signal_age_seconds == 120  # default preserved


# --- Plan 1b Task 1: position count, trailing-stop policy, leg vs TP. ---

def test_account_spec_positions_defaults_to_none():
    from xaucopier.domain.market import AccountSpec
    acct = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)
    assert acct.positions is None
    assert AccountSpec("a", "XAUUSD", 0.01, 1, positions=4).positions == 4


def test_policy_trailing_defaults():
    p = Policy()
    assert p.trail_enabled is True
    assert p.trail_gap_usd == 2.00


def test_policy_from_dict_applies_the_trail_keys():
    p = Policy.from_dict({"trail_enabled": False, "trail_gap_usd": 1.5})
    assert p.trail_enabled is False
    assert p.trail_gap_usd == 1.5


@pytest.mark.parametrize("gap", [0, 0.0, -0.01, -2.0, float("nan"), float("inf")])
def test_trail_gap_must_be_a_positive_usd_distance(gap):
    with pytest.raises(ValueError, match="trail_gap_usd"):
        Policy(trail_gap_usd=gap)


@pytest.mark.parametrize("gap", [True, False, "2.00", None])
def test_trail_gap_must_be_a_number_not_a_bool_or_string(gap):
    with pytest.raises(ValueError, match="trail_gap_usd"):
        Policy(trail_gap_usd=gap)


@pytest.mark.parametrize("gap", [0.01, 2, 2.00, 5.0])
def test_trail_gap_accepts_a_positive_distance(gap):
    assert Policy(trail_gap_usd=gap).trail_gap_usd == gap


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_trail_enabled_must_be_a_real_boolean(value):
    # A quoted "false" is a non-empty string, so it is truthy: loaded as-is
    # it would switch the trail ON when the user wrote it off.
    with pytest.raises(ValueError, match="trail_enabled"):
        Policy(trail_enabled=value)


def _intent(leg_index, tp_index, tp):
    from xaucopier.domain.intent import OrderIntent
    return OrderIntent(
        account_id="a", symbol="XAUUSD", side=Side.BUY, kind=OrderKind.MARKET,
        volume=0.01, price=4482.10, sl=4470.0, tp=tp, leg_index=leg_index,
        tp_index=tp_index, magic=77001, comment=f"x-{leg_index}t{tp_index}",
        deviation_points=20,
    )


def test_order_intent_carries_tp_index_apart_from_leg_index():
    # N > T: two positions on the last TP share its tp_index; each keeps
    # its own leg_index.
    a, b = _intent(3, 3, 4505.0), _intent(4, 3, 4505.0)
    assert a.tp_index == b.tp_index == 3
    assert a.leg_index != b.leg_index
    assert a != b


def test_order_intent_tp_index_is_required():
    from xaucopier.domain.intent import OrderIntent
    field = {f.name: f for f in dataclasses.fields(OrderIntent)}["tp_index"]
    assert field.default is dataclasses.MISSING
