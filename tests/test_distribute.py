# tests/test_distribute.py
"""Plan 1b Task 2: N positions spread over the surviving take profits.

The user's rule (2026-09-23): each position carries its own TP and closes
there. N == T is one per TP; N > T puts the extras on the LAST TP; N < T
takes the FIRST N TPs. The distribution is over the SURVIVING TPs, never
all of them: a position aimed at a TP price has already passed would close
at once or be rejected.
"""
from datetime import datetime, timedelta, UTC

import pytest

from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.planner import distribute, plan_entry
from xaucopier.domain.signal import EntryZone, Side, Signal

NOW = datetime(2026, 9, 5, 10, 45, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()
TPS = (4490.0, 4495.0, 4500.0, 4505.0)
ALL4 = ((0, 4490.0), (1, 4495.0), (2, 4500.0), (3, 4505.0))
TP1_PASSED = ((1, 4495.0), (2, 4500.0), (3, 4505.0))
ONLY_TP4 = ((3, 4505.0),)

# message_id 4001 is "335" in base36 (3 x 1296 + 3 x 36 + 5).
B36_4001 = "335"


def _b36(n):
    # An independent base36 encoder, so the comment tests do not trust the
    # planner's own.
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
        if n == 0:
            return out


def acct(positions, lot=0.05):
    # A lot other than the 0.01 default, so "every leg carries fixed_lot"
    # cannot pass by coincidence.
    return AccountSpec("ic_demo", "XAUUSD", fixed_lot=lot, magic_base=77001,
                       positions=positions)


def snap(bid, ask, spread=0.10):
    return MarketSnapshot(bid=bid, ask=ask, tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=spread, now_utc=NOW)


def buy(message_id=4001, tps=TPS):
    return Signal(channel_id=-1, message_id=message_id, side=Side.BUY,
                  symbol_alias="XAUUSD", zone=EntryZone(4480.0, 4485.0),
                  sl=4470.0, tps=tps, posted_at_utc=NOW - timedelta(seconds=5),
                  content_hash="h")


def sell(message_id=4002, tps=(4475.0, 4470.0, 4465.0, 4460.0)):
    return Signal(channel_id=-1, message_id=message_id, side=Side.SELL,
                  symbol_alias="XAUUSD", zone=EntryZone(4480.0, 4485.0),
                  sl=4495.0, tps=tps, posted_at_utc=NOW - timedelta(seconds=5),
                  content_hash="h")


INSIDE = snap(4482.0, 4482.10)        # every TP ahead
PAST_TP1 = snap(4491.0, 4491.10)      # TP1 (4490) is behind the ask
PAST_TP3 = snap(4501.0, 4501.10)      # only TP4 (4505) is ahead


# --- distribute(): the pure rule -------------------------------------------

@pytest.mark.parametrize(("n", "surviving", "expected"), [
    (None, ALL4, ALL4),
    (4, ALL4, ALL4),
    (6, ALL4, ALL4 + ((3, 4505.0), (3, 4505.0))),
    (2, ALL4, ((0, 4490.0), (1, 4495.0))),
    (1, ALL4, ((0, 4490.0),)),
    (3, TP1_PASSED, TP1_PASSED),
    (5, ONLY_TP4, ((3, 4505.0),) * 5),
], ids=["none-unchanged", "n4-of-4", "n6-extras-on-last", "n2-first-two",
        "n1-first", "n3-tp1-passed", "n5-only-tp4"])
def test_distribute_table(n, surviving, expected):
    assert distribute(n, surviving) == expected


def test_distribute_returns_a_tuple():
    assert isinstance(distribute(6, ALL4), tuple)
    assert isinstance(distribute(None, ALL4), tuple)
    assert isinstance(distribute(2, ALL4), tuple)


def test_extras_go_on_the_last_surviving_tp_after_one_per_tp():
    # TP1 passed, five positions: one each on TP2..TP4, the two extras on TP4.
    assert distribute(5, TP1_PASSED) == TP1_PASSED + ((3, 4505.0), (3, 4505.0))


def test_fewer_positions_than_surviving_tps_take_the_first_ones():
    # Not the last N, not "first and last": the user chose the first N.
    assert distribute(2, TP1_PASSED) == ((1, 4495.0), (2, 4500.0))
    assert distribute(3, ALL4) == ((0, 4490.0), (1, 4495.0), (2, 4500.0))


def test_no_surviving_tp_means_no_position():
    assert distribute(None, ()) == ()
    assert distribute(3, ()) == ()


@pytest.mark.parametrize("bad", [0, -1, True, False, 2.0, "4"])
def test_distribute_refuses_a_count_that_is_not_a_positive_whole_number(bad):
    # The account loader already refuses these; this is the last line of
    # defence, because AccountSpec itself does not validate positions. A
    # zero would plan an accepted signal with no orders, and True would
    # silently mean one.
    with pytest.raises(ValueError):
        distribute(bad, ALL4)


# --- plan_entry: one leg per distributed position --------------------------

@pytest.mark.parametrize(("n", "market", "expected_tps"), [
    (None, INSIDE, [4490.0, 4495.0, 4500.0, 4505.0]),
    (4, INSIDE, [4490.0, 4495.0, 4500.0, 4505.0]),
    (6, INSIDE, [4490.0, 4495.0, 4500.0, 4505.0, 4505.0, 4505.0]),
    (2, INSIDE, [4490.0, 4495.0]),
    (1, INSIDE, [4490.0]),
    (3, PAST_TP1, [4495.0, 4500.0, 4505.0]),
    (5, PAST_TP3, [4505.0] * 5),
], ids=["none-unchanged", "n4-of-4", "n6-extras-on-last", "n2-first-two",
        "n1-first", "n3-tp1-passed", "n5-only-tp4"])
def test_plan_entry_legs_follow_the_table(n, market, expected_tps):
    plan = plan_entry(buy(), acct(n), SPEC, market, POLICY)
    assert plan.accepted
    assert [leg.tp for leg in plan.legs] == expected_tps
    assert [leg.leg_index for leg in plan.legs] == list(range(len(expected_tps)))


@pytest.mark.parametrize(("n", "market", "expected"), [
    (None, INSIDE, [0, 1, 2, 3]),
    (6, INSIDE, [0, 1, 2, 3, 3, 3]),
    (2, INSIDE, [0, 1]),
    (3, PAST_TP1, [1, 2, 3]),
    (4, PAST_TP1, [1, 2, 3, 3]),
    (5, PAST_TP3, [3, 3, 3, 3, 3]),
])
def test_tp_index_is_the_index_into_the_original_sig_tps(n, market, expected):
    s = buy()
    plan = plan_entry(s, acct(n), SPEC, market, POLICY)
    assert [leg.tp_index for leg in plan.legs] == expected
    assert all(leg.tp == s.tps[leg.tp_index] for leg in plan.legs)


def test_a_passed_take_profit_never_gets_a_position():
    # Four positions with TP1 behind price: the one that would have gone to
    # TP1 becomes an extra on the last surviving TP.
    plan = plan_entry(buy(), acct(4), SPEC, PAST_TP1, POLICY)
    assert [leg.tp for leg in plan.legs] == [4495.0, 4500.0, 4505.0, 4505.0]
    assert 4490.0 not in {leg.tp for leg in plan.legs}


def test_comment_names_the_leg_and_its_take_profit():
    plan = plan_entry(buy(), acct(6), SPEC, INSIDE, POLICY)
    assert [leg.comment for leg in plan.legs] == [
        f"{B36_4001}-0t0", f"{B36_4001}-1t1", f"{B36_4001}-2t2",
        f"{B36_4001}-3t3", f"{B36_4001}-4t3", f"{B36_4001}-5t3",
    ]


def test_comment_after_a_passed_tp_keeps_both_numbers_apart():
    plan = plan_entry(buy(), acct(3), SPEC, PAST_TP1, POLICY)
    assert [leg.comment for leg in plan.legs] == [
        f"{B36_4001}-0t1", f"{B36_4001}-1t2", f"{B36_4001}-2t3",
    ]


@pytest.mark.parametrize("n", [None, 1, 4, 6, 11, 20])
def test_every_comment_is_unique_and_fits_the_broker_limit(n):
    plan = plan_entry(buy(), acct(n), SPEC, INSIDE, POLICY)
    comments = [leg.comment for leg in plan.legs]
    assert len(set(comments)) == len(comments)
    assert all(len(c) <= 20 for c in comments)


@pytest.mark.parametrize("message_id", [36 ** 25, 36 ** 19, 36 ** 15 - 1, 36 ** 14])
@pytest.mark.parametrize("n", [4, 6, 20])
def test_a_huge_message_id_truncates_the_prefix_never_the_suffix(message_id, n):
    plan = plan_entry(buy(message_id=message_id), acct(n), SPEC, INSIDE, POLICY)
    assert plan.accepted
    comments = [leg.comment for leg in plan.legs]
    for leg in plan.legs:
        suffix = f"-{leg.leg_index}t{leg.tp_index}"
        assert leg.comment.endswith(suffix)
        assert len(leg.comment) <= 20
        # The prefix keeps its leading digits and fills the rest of the
        # budget: only the prefix's tail is ever cut.
        assert leg.comment == _b36(message_id)[:20 - len(suffix)] + suffix
    assert len(set(comments)) == len(comments)


def test_a_huge_message_id_keeps_the_leading_base36_digits():
    # 36**25 is "1" followed by 25 zeros in base36; a two-digit leg index
    # ("-19t3", five characters) leaves fifteen for the prefix.
    plan = plan_entry(buy(message_id=36 ** 25), acct(20), SPEC, INSIDE, POLICY)
    assert plan.legs[19].comment == "1" + "0" * 14 + "-19t3"
    assert plan.legs[0].comment == "1" + "0" * 15 + "-0t0"


def test_every_leg_carries_fixed_lot_shared_sl_and_magic():
    plan = plan_entry(buy(), acct(6, lot=0.05), SPEC, INSIDE, POLICY)
    assert len(plan.legs) == 6
    assert {leg.volume for leg in plan.legs} == {0.05}
    assert {leg.sl for leg in plan.legs} == {4470.0}
    assert {leg.magic for leg in plan.legs} == {77001}


def test_under_the_single_rule_every_leg_shares_one_entry():
    # Layered entry (the default, Task 2b) gives the legs of one plan
    # different kinds and prices; layered_entry = false keeps one entry.
    single = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.05, magic_base=77001,
                         positions=6, layered_entry=False)
    plan = plan_entry(buy(), single, SPEC, INSIDE, POLICY)
    assert len(plan.legs) == 6
    assert {leg.kind for leg in plan.legs} == {plan.legs[0].kind}
    assert {leg.price for leg in plan.legs} == {4482.10}


# --- the SELL mirror ---------------------------------------------------------

SELL_INSIDE = snap(4482.0, 4482.10)
SELL_PAST_TP1 = snap(4474.0, 4474.10)   # TP1 (4475) is behind the bid


@pytest.mark.parametrize(("n", "market", "expected_tps", "expected_tp_index"), [
    (None, SELL_INSIDE, [4475.0, 4470.0, 4465.0, 4460.0], [0, 1, 2, 3]),
    (4, SELL_INSIDE, [4475.0, 4470.0, 4465.0, 4460.0], [0, 1, 2, 3]),
    (6, SELL_INSIDE, [4475.0, 4470.0, 4465.0, 4460.0, 4460.0, 4460.0],
     [0, 1, 2, 3, 3, 3]),
    (2, SELL_INSIDE, [4475.0, 4470.0], [0, 1]),
    (1, SELL_INSIDE, [4475.0], [0]),
    (3, SELL_PAST_TP1, [4470.0, 4465.0, 4460.0], [1, 2, 3]),
    (5, SELL_PAST_TP1, [4470.0, 4465.0, 4460.0, 4460.0, 4460.0], [1, 2, 3, 3, 3]),
], ids=["none-unchanged", "n4-of-4", "n6-extras-on-last", "n2-first-two",
        "n1-first", "n3-tp1-passed", "n5-tp1-passed"])
def test_sell_mirror(n, market, expected_tps, expected_tp_index):
    s = sell()
    plan = plan_entry(s, acct(n), SPEC, market, POLICY)
    assert plan.accepted
    assert [leg.tp for leg in plan.legs] == expected_tps
    assert [leg.tp_index for leg in plan.legs] == expected_tp_index
    assert [leg.leg_index for leg in plan.legs] == list(range(len(expected_tps)))
    assert {leg.side for leg in plan.legs} == {Side.SELL}
    assert {leg.sl for leg in plan.legs} == {4495.0}
    assert {leg.volume for leg in plan.legs} == {0.05}
    # 4002 is "336" in base36, one past 4001's "335".
    assert [leg.comment for leg in plan.legs] == [
        f"336-{i}t{t}" for i, t in enumerate(expected_tp_index)
    ]
