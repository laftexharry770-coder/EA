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
    # Amendment 2026-10-21: the buffer is 1 pip, and every ladder step fires
    # on its own take profit (no early pips).
    p = Policy()
    assert p.trail_enabled is True
    assert p.be_buffer_pips == 1.0
    assert p.ladder_early_pips == ()


def test_the_old_ladder_gap_and_usd_buffer_are_gone():
    names = {f.name for f in dataclasses.fields(Policy)}
    assert "trail_gap_usd" not in names
    assert "be_buffer_usd" not in names


def test_a_pip_is_a_fixed_ten_cents():
    # Never derived from a broker's digits or point size: gold is quoted at
    # 2 digits on some feeds and 3 on others, and a pip read from the
    # point would be $0.01 on a 3-digit feed (the 10x trap).
    from xaucopier.domain import market
    assert market.PIP_USD == 0.10


def test_policy_from_dict_applies_the_trail_keys():
    p = Policy.from_dict({"trail_enabled": False, "be_buffer_pips": 2.5,
                          "ladder_early_pips": [0, 0, 20]})
    assert p.trail_enabled is False
    assert p.be_buffer_pips == 2.5
    # A TOML list becomes a tuple: the Policy is frozen through and through.
    assert p.ladder_early_pips == (0, 0, 20)
    assert isinstance(p.ladder_early_pips, tuple)


@pytest.mark.parametrize("pips", [0, 0.0, 1, 1.0, 2.5, 30])
def test_be_buffer_pips_accepts_a_distance_of_zero_or_more(pips):
    assert Policy(be_buffer_pips=pips).be_buffer_pips == pips


@pytest.mark.parametrize("pips", [-0.1, -1, float("nan"), float("inf"), float("-inf"),
                                  True, False, "1.0", None])
def test_be_buffer_pips_must_be_a_finite_number_of_zero_or_more(pips):
    with pytest.raises(ValueError, match="be_buffer_pips"):
        Policy(be_buffer_pips=pips)


@pytest.mark.parametrize(("given", "stored"), [
    ((), ()), ([], ()), ((0, 0, 20), (0, 0, 20)), ([0, 0, 20], (0, 0, 20)),
    ((0.5,), (0.5,)), ([0, 0, 0, 0, 0, 0], (0, 0, 0, 0, 0, 0)),
])
def test_ladder_early_pips_accepts_distances_of_zero_or_more(given, stored):
    early = Policy(ladder_early_pips=given).ladder_early_pips
    assert early == stored
    assert isinstance(early, tuple)


@pytest.mark.parametrize("early", [
    (-1,), (0, -0.5), (float("nan"),), (float("inf"),), (True,), (0, False),
    ("20",), (None,), "20", 20, 20.0, None, {"step": 20},
])
def test_ladder_early_pips_must_be_a_list_of_finite_numbers_of_zero_or_more(early):
    with pytest.raises(ValueError, match="ladder_early_pips"):
        Policy(ladder_early_pips=early)


def test_the_retired_be_buffer_usd_key_is_refused_and_names_its_replacement():
    # Silently ignoring it would change the user's buffer.
    with pytest.raises(ValueError, match="be_buffer_usd") as refused:
        Policy.from_dict({"be_buffer_usd": 0.10})
    assert "be_buffer_pips" in str(refused.value)
    assert "$0.10" in str(refused.value)


def test_the_retired_trail_gap_usd_key_is_refused_and_says_the_gap_is_gone():
    with pytest.raises(ValueError, match="trail_gap_usd") as refused:
        Policy.from_dict({"trail_gap_usd": 2.00})
    message = str(refused.value)
    assert "gap is gone" in message
    assert "be_buffer_pips" in message
    assert "ladder_early_pips" in message


def test_both_retired_keys_are_named_at_once():
    with pytest.raises(ValueError) as refused:
        Policy.from_dict({"be_buffer_usd": 0.10, "trail_gap_usd": 2.00, "max_spread_usd": 0.5})
    assert "be_buffer_usd" in str(refused.value)
    assert "trail_gap_usd" in str(refused.value)


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
    # N > T cycles (amendment 2026-10-21): with 6 positions on 4 TPs,
    # positions 0 and 4 share TP1's tp_index; each keeps its own leg_index.
    a, b = _intent(0, 0, 4490.0), _intent(4, 0, 4490.0)
    assert a.tp_index == b.tp_index == 0
    assert a.leg_index != b.leg_index
    assert a != b


def test_order_intent_tp_index_is_required():
    from xaucopier.domain.intent import OrderIntent
    field = {f.name: f for f in dataclasses.fields(OrderIntent)}["tp_index"]
    assert field.default is dataclasses.MISSING
