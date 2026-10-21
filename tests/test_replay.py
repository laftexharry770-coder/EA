import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path

from xaucopier.domain.intent import OrderIntent, OrderKind
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.planner import plan_entry
from xaucopier.domain.signal import Side, Signal
from xaucopier.parsing.channel_mmh import parse
from xaucopier.replay.harness import replay, Decision

FIXDIR = Path(__file__).parent / "fixtures/messages"
GOLDEN = Path(__file__).parent / "fixtures/expected_decisions.json"
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
ACCT = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)

def _snap_for(signal: Signal) -> MarketSnapshot:
    """Deterministic synthetic market derived from the signal's own entry
    zone (mid), not a hard-coded price -- must stay consistent with the
    harness's own snapshot_for (see Item I6)."""
    spread = 0.10
    mid = (signal.zone.lo + signal.zone.hi) / 2.0
    return MarketSnapshot(bid=mid - spread / 2, ask=mid + spread / 2,
                          tick_time_utc=signal.posted_at_utc,
                          spread_usd=spread, median_spread_usd=spread,
                          now_utc=signal.posted_at_utc + timedelta(seconds=5))

def _fixtures():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXDIR.glob("*.json"))]

def test_every_fixture_produces_a_decision():
    decisions = replay(_fixtures(), ACCT, SPEC, Policy(), _snap_for)
    assert len(decisions) == len(_fixtures())

def test_decisions_match_the_golden_file():
    decisions = replay(_fixtures(), ACCT, SPEC, Policy(), _snap_for)
    actual = [d.as_dict() for d in decisions]
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual == expected, (
        "A parser or planner change altered a historical decision.\n"
        "If this change is intended, regenerate with:\n"
        "  python -m xaucopier.replay.harness --write-golden"
    )

def test_the_real_message_yields_four_legs():
    decisions = replay(_fixtures(), ACCT, SPEC, Policy(), _snap_for)
    mmh = next(d for d in decisions if d.message_id == 4001)
    assert mmh.accepted and mmh.leg_count == 4


# ---------------------------------------------------------------------------
# Plan 1b changed the real message's decision in exactly two ways (ruling
# P2): the broker comment gained a "t{tp_index}" suffix, and layered entry
# (on by default) rests two of the four positions as limit orders. These
# pin both against Plan 2's decision, so a golden file regenerated blindly
# cannot hide a third change.
# ---------------------------------------------------------------------------

#: The real message's decision as Plan 2 shipped it: the golden file at
#: 289c117, before it recorded comments, kinds or entry prices. Four market
#: legs at the snapshot's ask, 4482.55, one per TP.
PLAN_2_DECISION = {
    "message_id": 4001, "accepted": True, "reason": None, "leg_count": 4,
    "entry_kind": "market", "entry_price": 4482.55, "sl": 4470.0,
    "leg_indices": [0, 1, 2, 3], "prices": [4490.0, 4495.0, 4500.0, 4505.0],
}
#: Plan 2's broker comments, "{base36(4001)}-{leg}"; 4001 is "335" in base 36.
PLAN_2_COMMENTS = ["335-0", "335-1", "335-2", "335-3"]
#: The fields the golden file has gained since Plan 2.
ADDED_FIELDS = {"comments", "kinds", "entry_prices"}


def _real_decision(account: AccountSpec) -> dict:
    decisions = replay(_fixtures(), account, SPEC, Policy(), _snap_for)
    return next(d.as_dict() for d in decisions if d.message_id == 4001)


def test_the_single_entry_rule_gives_plan_2s_legs_with_only_the_comment_changed():
    decision = _real_decision(dataclasses.replace(ACCT, layered_entry=False))
    assert set(decision) == set(PLAN_2_DECISION) | ADDED_FIELDS
    assert {key: decision[key] for key in PLAN_2_DECISION} == PLAN_2_DECISION
    # Every leg is Plan 2's one entry, market at 4482.55...
    assert decision["kinds"] == ["market"] * 4
    assert decision["entry_prices"] == [4482.55] * 4
    # ...and the comment is Plan 2's with the TP index appended: 335-0t0 ...
    assert decision["comments"] == [f"{old}t{tp}" for tp, old in enumerate(PLAN_2_COMMENTS)]


def test_the_single_entry_rule_gives_plan_2s_orders_field_for_field():
    """The golden file records only some of an order's fields. Plan 2's
    planner (289c117) gave each leg: market at the ask, the signal's SL,
    its own TP, fixed_lot, magic_base, max_slippage_usd 0.20 as 20 points,
    and no expiry. Plan 1b changes only the comment, and adds tp_index,
    which here equals leg_index (one position per TP)."""
    fixture = next(f for f in _fixtures() if f["message_id"] == 4001)
    signal = parse(fixture["text"], channel_id=fixture["channel_id"], message_id=4001,
                   posted_at_utc=datetime.fromisoformat(fixture["posted_at_utc"])).signal
    single = dataclasses.replace(ACCT, layered_entry=False)
    plan = plan_entry(signal, single, SPEC, _snap_for(signal), Policy())
    assert plan.accepted and plan.notes == ()
    assert plan.legs == tuple(
        OrderIntent(account_id="ic_demo", symbol="XAUUSD", side=Side.BUY,
                    kind=OrderKind.MARKET, volume=0.01, price=4482.55, sl=4470.0, tp=tp,
                    leg_index=leg, tp_index=leg, magic=77001,
                    comment=f"{PLAN_2_COMMENTS[leg]}t{leg}", deviation_points=20,
                    expires_at_utc=None)
        for leg, tp in enumerate(PLAN_2_DECISION["prices"])
    )


def test_layered_entry_changes_only_the_entry_of_the_pending_legs():
    layered = _real_decision(ACCT)   # layered_entry is on by default
    single = _real_decision(dataclasses.replace(ACCT, layered_entry=False))
    assert ACCT.layered_entry is True
    assert {key for key in layered if layered[key] != single[key]} == {"kinds", "entry_prices"}
    # The ask, 4482.55, is inside the zone 4480-4485. NOW positions 0 and 2
    # (TP1, TP3) go at market; PENDING positions 1 and 3 (TP2, TP4) rest as
    # limits from the ask less the minimum distance, 4482.55 - 3 x 0.10 =
    # 4482.25, down to the better edge, 4480, both inclusive.
    assert layered["kinds"] == ["market", "limit", "market", "limit"]
    assert layered["entry_prices"] == [4482.55, 4482.25, 4482.55, 4480.0]
    assert layered["prices"] == [4490.0, 4495.0, 4500.0, 4505.0]
