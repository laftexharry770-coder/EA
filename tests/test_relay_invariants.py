"""Relay invariants, checked on random scenarios with fixed seeds (ruling
R5-6).

Each round of relay fixes pinned the scenarios its review found, and each
round's rule change opened a new edge somewhere else. These tests check
the properties every rule must keep, on many generated scenarios -- one
channel and two channels, hand copies, forwards (a forward of a forward
names the original post, as Telegram does), double posts, deletions one
at a time or together, edits into chatter, re-aims, broken text, stop
moves, executed entries and replies:

- **INV-1, retraction.** Once every post of a call, on every channel, is
  withdrawn -- deleted, edited away or re-aimed -- nothing of that call is
  claimable, and every signal of it that was executed is flagged (marked
  withdrawn, or INVALID/VOIDED) with a CRITICAL alert naming it.
- **INV-2, order independence.** The VIP's call and a relay's copy of it
  (by hand or a forward), stored VIP-first or relay-first, followed by the
  same events, give the same claimable entries at every step, and every
  follow-up acts on the same signal (`relay_repo.signal_for_followup`,
  compared by what that signal is: the call's live signal, or which
  closed one), whichever post holds the signal.
- **INV-3, no double trade.** At every step, no account has more than one
  entry of the call executed, in flight or claimable.

Every scenario uses one call (the same BUY), so "the call" is every
signal the scenario creates. The seeds are fixed: the suite is
deterministic. `XAUCOPIER_FUZZ_SEEDS` sets how many seeds each family
runs (default below; the review runs used 5000 per family, about two
minutes). A failure prints the family, the seed and the events, shrunk
to the fewest that still fail -- and for INV-2, which storage order broke
an invariant, or the step where the two orders part. Never weaken an
invariant to make a seed pass: fix the rule, and pin the seed's scenario
as its own test (tests/test_relay_echo.py, "what the invariant tests
found").

Seeds: the retraction families use 1000 * channels + n, the order
family 5000 + n, for n in range(XAUCOPIER_FUZZ_SEEDS).

**Retraction by an added "CANCELLED"** (ledger 161, group-3 leftover
G3L-3). The cancel families add two more events -- a post by hand edited
to append "CANCELLED", which withdraws it though its levels are the
same, and a post by hand that shows no call edited straight into the
call with "CANCELLED" (which never shows it) -- and let the final
withdrawal cancel a post too. They are families
of their own (seeds 20000 + 1000 * channels + n and 26000 + n), so the
scenarios the families above generate are exactly what they were.

**Time that crosses the relay window** (final fix FF12). The families
above tick 20 s per event, so no post is ever outside the 900 s window of
another, and a promotion making a second live signal for a call another
signal already carried -- a repost 890 s after the first post, a third
post 910 s after it, the first post then withdrawn -- could not happen
in them. The window-crossing family (seeds 90000 + 1000 * channels + n)
ticks a random 20-500 s per event, and its worker takes only entries
younger than Plan 1's 120 s age gate, as Plan 3 will. Two posts of the
same trade more than 900 s apart are two calls by design (ruling
d4c7d7a), so its no-double-trade check is per call:

- **INV-3w.** No account holds (executed, in flight, or claimable while
  younger than the age gate) entries of two signals of the same trade
  whose own messages were posted within 900 s of each other.

The window-crossing family reaches that scenario only rarely (seed 93195
first, beyond the default seeds), so a pattern family (seeds 95000 +
1000 * channels + n) builds it on purpose: the first post unexecuted,
posts of the call on either side of the 900 s mark (by hand or forwards,
on either channel, in a random order, some taken by the worker), then the
first post deleted (event or window) or edited away (chatter, re-aimed,
broken, cancelled), then every post withdrawn: INV-3w after every step,
INV-1 at the end.
"""
from __future__ import annotations

import os
import random
import sqlite3
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from test_edits import CHATTER_TEXT, DIRECTION_EDIT, SL_EDIT, UNPARSEABLE_EDIT
from test_relay import ACCOUNTS, CLOSE_HALF, POSTED, RELAY, SIGNAL_TEXT, VIP
from xaucopier.store import edits_repo, relay_repo
from xaucopier.store.migrations import migrate
from xaucopier.store.messages_repo import ingest

#: Seeds per family in the normal suite (about 20 s in all);
#: `XAUCOPIER_FUZZ_SEEDS` overrides, e.g. 5000 for a thorough run.
DEFAULT_SEEDS = 1000
SEEDS = int(os.environ.get("XAUCOPIER_FUZZ_SEEDS", DEFAULT_SEEDS))
#: Seeds per test item (a failing block names its seeds).
BLOCK = 50

CHANNELS = (VIP, RELAY)
FIRST_ID = {0: 4001, 1: 7001}
WINDOW = 900
#: What a hand post may be edited into. SIGNAL_TEXT and SL_EDIT show the
#: call (a stop move); the rest take the post out of it.
EDITS = {
    "BUY": SIGNAL_TEXT,
    "SL": SL_EDIT,
    "SELL": DIRECTION_EDIT,
    "CHAT": CHATTER_TEXT,
    "BROKEN": UNPARSEABLE_EDIT,
}
#: What `_edit` draws from (the cancel families add CANCEL on their own).
EDIT_LABELS = ("BUY", "SL", "SELL", "CHAT", "BROKEN")
#: Appended by a cancel edit: the post's levels unchanged, the call
#: withdrawn (ledger 161).
CANCEL_SUFFIX = "\n\u274c CANCELLED"
SHOWS_CALL = frozenset({SIGNAL_TEXT, SL_EDIT})
READS_AS_SIGNAL = frozenset({SIGNAL_TEXT, SL_EDIT, DIRECTION_EDIT})


# -- the store ---------------------------------------------------------------


_TEMPLATE: sqlite3.Connection | None = None


def _fresh() -> sqlite3.Connection:
    """An empty, migrated store in memory (copied from one template)."""
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = sqlite3.connect(":memory:")
        migrate(_TEMPLATE)
    conn = sqlite3.connect(":memory:")
    _TEMPLATE.backup(conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# -- a scenario --------------------------------------------------------------


@dataclass
class _Post:
    channel: int  # index into CHANNELS
    message_id: int
    text: str
    posted: datetime
    #: The original post a forward names (a forward of a forward names
    #: the first), or None for a post by hand.
    forwarded: tuple[int, int] | None = None
    #: A follow-up replying to this message id of its channel.
    reply_to: int | None = None
    #: How its deletion was found: "event" (final) or "window" (inferred,
    #: and reversed if the window reads the post back); None: not deleted.
    deleted_by: str | None = None
    edited_at: datetime | None = None

    @property
    def deleted(self) -> bool:
        return self.deleted_by is not None

    @property
    def reply(self) -> bool:
        return self.reply_to is not None


@dataclass
class _World:
    """One scenario's store and the test's own model of the channels."""

    conn: sqlite3.Connection
    posts: dict[tuple[int, int], _Post] = field(default_factory=dict)
    next_id: dict[int, int] = field(default_factory=lambda: dict(FIRST_ID))
    clock: datetime = POSTED
    log: list[str] = field(default_factory=list)
    #: Seconds an event advances the clock: one value, or (the
    #: window-crossing family) a random choice among several.
    ticks: tuple[int, ...] = (20,)
    tick_rng: random.Random | None = None

    def tick(self) -> datetime:
        if len(self.ticks) == 1:
            step = self.ticks[0]
        else:
            step = self.tick_rng.choice(self.ticks)
        self.clock += timedelta(seconds=step)
        return self.clock

    def name(self, key: tuple[int, int]) -> str:
        return f"{CHANNELS[key[0]].name}/{key[1]}"

    def store(self, post: _Post) -> None:
        fwd = None
        if post.forwarded is not None:
            fwd = (CHANNELS[post.forwarded[0]].channel_id, post.forwarded[1])
        ingest(
            self.conn,
            channel_cfg=CHANNELS[post.channel],
            message_id=post.message_id,
            text=post.text,
            posted_at_utc=post.posted,
            edit_date_utc=None,
            reply_to_message_id=post.reply_to,
            source="live",
            now_utc=post.posted + timedelta(seconds=1),
            accounts=ACCOUNTS,
            relay_window_seconds=WINDOW,
            fwd_from_channel_id=None if fwd is None else fwd[0],
            fwd_from_post_id=None if fwd is None else fwd[1],
            fwd_from_date_utc=None if fwd is None else POSTED,
        )
        self.conn.commit()
        self.posts[(post.channel, post.message_id)] = post

    def new_id(self, channel: int) -> int:
        message_id = self.next_id[channel]
        self.next_id[channel] += 1
        return message_id

    def standing(self) -> list[tuple[int, int]]:
        return sorted(k for k, p in self.posts.items() if not p.deleted and not p.reply)

    def delete(self, keys: list[tuple[int, int]], *, by: str = "event") -> None:
        at = self.tick()
        together = frozenset(k[1] for k in keys)
        for key in keys:
            edits_repo.apply_deletion(
                self.conn, channel_cfg=CHANNELS[key[0]], message_id=key[1], now_utc=at,
                accounts=ACCOUNTS, detected_by=by,
                also_deleted=together if len(keys) > 1 else (),
            )
            self.conn.commit()
            self.posts[key].deleted_by = by
        self.log.append(f"delete {' + '.join(self.name(k) for k in keys)} ({by})")

    def edit(self, key: tuple[int, int], label: str, *, by: str = "event") -> None:
        post = self.posts[key]
        at = self.tick()
        if label == "CANCEL":
            text = post.text + CANCEL_SUFFIX
        elif label == "CALL+CANCEL":
            text = SIGNAL_TEXT + CANCEL_SUFFIX
        else:
            text = EDITS[label]
        edits_repo.apply_edit(
            self.conn, channel_cfg=CHANNELS[key[0]], message_id=key[1], text=text,
            posted_at_utc=post.posted, edit_date_utc=at, reply_to_message_id=None,
            now_utc=at, accounts=ACCOUNTS, detected_by=by,
        )
        self.conn.commit()
        post.text, post.edited_at = text, at
        self.log.append(f"edit {self.name(key)} -> {label} ({by})")

    def read_back(self, key: tuple[int, int]) -> None:
        """The window reads back a post it had inferred deleted: as it is."""
        post = self.posts[key]
        at = self.tick()
        edits_repo.apply_edit(
            self.conn, channel_cfg=CHANNELS[key[0]], message_id=key[1], text=post.text,
            posted_at_utc=post.posted, edit_date_utc=post.edited_at,
            reply_to_message_id=post.reply_to, now_utc=at, accounts=ACCOUNTS,
            detected_by="window",
        )
        self.conn.commit()
        post.deleted_by = None
        self.log.append(f"read back {self.name(key)} (window)")

    def execute(self) -> None:
        """acct-1's worker takes every entry it can claim."""
        self.conn.execute(
            "UPDATE outbox SET state = 'DONE' WHERE kind = 'ENTRY' AND account_id = 'acct-1' "
            "AND state = 'QUEUED' AND signal_id IN "
            "(SELECT signal_id FROM signals WHERE state = 'NEW' AND withdrawn_at_utc IS NULL)"
        )
        self.conn.commit()
        self.log.append("acct-1 takes its entries")


def _q(conn, sql: str, *params) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


# -- the events --------------------------------------------------------------


def _post(world: _World, rng: random.Random, channels: int) -> None:
    channel = rng.randrange(channels)
    post = _Post(channel, world.new_id(channel), SIGNAL_TEXT, world.tick())
    world.store(post)
    world.log.append(f"post {world.name((channel, post.message_id))}")


def _forward(world: _World, rng: random.Random, channels: int, *, cross_only: bool) -> None:
    sources = [k for k in world.standing() if world.posts[k].text in READS_AS_SIGNAL]
    if not sources:
        return
    source = rng.choice(sources)
    root = world.posts[source].forwarded or source
    channel = 1 - root[0] if cross_only else rng.randrange(channels)
    post = _Post(channel, world.new_id(channel), world.posts[source].text, world.tick(), root)
    world.store(post)
    world.log.append(f"forward {world.name((channel, post.message_id))} of {world.name(root)}")


def _delete(world: _World, rng: random.Random) -> None:
    standing = world.standing()
    if not standing:
        return
    first = rng.choice(standing)
    keys = [first]
    same = [k for k in standing if k[0] == first[0] and k != first]
    if same and rng.random() < 0.25:
        keys.append(rng.choice(same))  # two posts in one report
    world.delete(keys, by=rng.choice(["event", "event", "window"]))


def _edit(world: _World, rng: random.Random) -> None:
    hands = [k for k in world.standing() if world.posts[k].forwarded is None]
    if not hands:
        return
    key = rng.choice(hands)
    label = rng.choice(EDIT_LABELS)
    if EDITS[label] == world.posts[key].text:
        return
    world.edit(key, label, by=rng.choice(["event", "event", "window"]))


def _read_back(world: _World, rng: random.Random) -> None:
    inferred = sorted(k for k, p in world.posts.items() if p.deleted_by == "window")
    if inferred:
        world.read_back(rng.choice(inferred))


def _reply(world: _World, rng: random.Random) -> None:
    targets = sorted(k for k, p in world.posts.items() if not p.reply)
    if not targets:
        return
    target = rng.choice(targets)
    post = _Post(
        target[0], world.new_id(target[0]), CLOSE_HALF, world.tick(), reply_to=target[1]
    )
    world.store(post)
    world.log.append(f"reply {world.name((target[0], post.message_id))} -> {world.name(target)}")


def _cancel(world: _World, rng: random.Random) -> None:
    """A post by hand that shows the call gets "CANCELLED" appended."""
    hands = [
        k for k in world.standing()
        if world.posts[k].forwarded is None and world.posts[k].text in SHOWS_CALL
    ]
    if hands:
        world.edit(rng.choice(hands), "CANCEL", by=rng.choice(["event", "event", "window"]))


def _cancelled_call(world: _World, rng: random.Random) -> None:
    """A post by hand that shows NO call (chatter, a broken call, another
    call) is edited straight into the call with "CANCELLED" appended: it
    adds the word, so it never shows the call (group 4 review, round 1)."""
    hands = [
        k for k in world.standing()
        if world.posts[k].forwarded is None and world.posts[k].text not in SHOWS_CALL
        and CANCEL_SUFFIX not in world.posts[k].text
    ]
    if hands:
        world.edit(rng.choice(hands), "CALL+CANCEL", by=rng.choice(["event", "event", "window"]))


def _withdraw_everything(world: _World, rng: random.Random, *, cancel: bool = False) -> None:
    """Every post still showing the call is withdrawn -- deleted, or (a
    post by hand) edited into chatter or re-aimed, or with `cancel` given
    "CANCELLED" -- one at a time, in a random order; the rest are deleted
    or left as they are."""
    rest = world.standing()
    rng.shuffle(rest)
    for key in rest:
        post = world.posts[key]
        if post.text not in SHOWS_CALL and rng.random() < 0.5:
            continue  # shows no call: may stay
        if post.forwarded is None and post.text in SHOWS_CALL and rng.random() < 0.4:
            world.edit(key, rng.choice(["CHAT", "SELL", "CANCEL"] if cancel else ["CHAT", "SELL"]))
        else:
            world.delete([key], by=rng.choice(["event", "event", "window"]))


# -- the invariants ----------------------------------------------------------


def _claimable(conn) -> list[tuple]:
    """(account, signal) of every ENTRY `outbox.claim()` would hand out."""
    return _q(
        conn,
        "SELECT o.account_id, o.signal_id FROM outbox o JOIN signals s "
        "ON s.signal_id = o.signal_id WHERE o.kind = 'ENTRY' AND o.state = 'QUEUED' "
        "AND s.state = 'NEW' AND s.withdrawn_at_utc IS NULL ORDER BY 1, 2",
    )


def _no_double_trade(conn) -> list[str]:
    """INV-3: per account, entries of the call executed, in flight or
    claimable -- at most one."""
    taken = _q(
        conn,
        "SELECT account_id, signal_id FROM outbox WHERE kind = 'ENTRY' "
        "AND state IN ('DONE', 'CLAIMED')",
    )
    problems = []
    for account in ACCOUNTS:
        mine = [s for a, s in taken + _claimable(conn) if a == account.account_id]
        if len(mine) > 1:
            problems.append(f"INV-3: {account.account_id} holds entries of signals {mine}")
    return problems


#: The window-crossing family's clock steps, and Plan 1's age gate.
CROSSING_TICKS = (20, 20, 60, 200, 400, 500)
AGE_GATE = 120


def _held_twice(world: _World) -> list[str]:
    """INV-3w: per account, no two signals of the same trade (first trade
    hash) whose own messages were posted within the relay window of each
    other are both held -- executed, in flight, or claimable while younger
    than the age gate."""
    young = world.clock - timedelta(seconds=AGE_GATE)
    held: dict[tuple[str, str], set[tuple[int, datetime]]] = {}
    for row in world.conn.execute(
        "SELECT o.account_id, o.state, s.signal_id, s.first_trade_hash, "
        "  s.state = 'NEW' AND s.withdrawn_at_utc IS NULL AS live, m.posted_at_utc "
        "FROM outbox o JOIN signals s ON s.signal_id = o.signal_id "
        "JOIN messages m ON m.channel_id = s.channel_id AND m.message_id = s.message_id "
        "WHERE o.kind = 'ENTRY'"
    ).fetchall():
        posted = datetime.fromisoformat(row["posted_at_utc"])
        claimable = row["state"] == "QUEUED" and row["live"] and posted >= young
        if row["state"] in ("DONE", "CLAIMED") or claimable:
            held.setdefault((row["account_id"], row["first_trade_hash"]), set()).add(
                (row["signal_id"], posted)
            )
    problems = []
    for (account, _), signals in sorted(held.items()):
        ordered = sorted(signals, key=lambda signal: signal[1])
        for index, (first, first_at) in enumerate(ordered):
            for second, second_at in ordered[index + 1:]:
                if (second_at - first_at).total_seconds() <= WINDOW:
                    problems.append(
                        f"INV-3w: {account} holds signals {first} and {second} of one trade, "
                        f"posted {(second_at - first_at).total_seconds():.0f} s apart"
                    )
    return problems


def _take_young(world: _World) -> None:
    """acct-1's worker takes every entry it can claim that is younger than
    the age gate (Plan 3 skips the rest)."""
    world.conn.execute(
        "UPDATE outbox SET state = 'DONE' WHERE kind = 'ENTRY' AND account_id = 'acct-1' "
        "AND state = 'QUEUED' AND signal_id IN (SELECT s.signal_id FROM signals s "
        "  JOIN messages m ON m.channel_id = s.channel_id AND m.message_id = s.message_id "
        "  WHERE s.state = 'NEW' AND s.withdrawn_at_utc IS NULL AND m.posted_at_utc >= ?)",
        ((world.clock - timedelta(seconds=AGE_GATE)).isoformat(),),
    )
    world.conn.commit()
    world.log.append("acct-1 takes its entries younger than the age gate")


def _retracted(conn) -> list[str]:
    """INV-1, once every post is withdrawn."""
    problems = []
    if claimable := _claimable(conn):
        problems.append(f"INV-1: still claimable {claimable}")
    for (signal_id,) in _q(
        conn,
        "SELECT DISTINCT signal_id FROM outbox WHERE kind = 'ENTRY' AND state = 'DONE'",
    ):
        (state, withdrawn) = _q(
            conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = ?",
            signal_id,
        )[0]
        critical = _q(
            conn, "SELECT code FROM alerts WHERE level = 'CRITICAL' AND signal_id = ?", signal_id
        )
        if not (withdrawn or state in ("INVALID", "VOIDED")) or not critical:
            problems.append(
                f"INV-1: executed signal {signal_id} not flagged (state {state}, "
                f"withdrawn {bool(withdrawn)}, CRITICAL alerts {critical})"
            )
    return problems


def _outcome(world: _World) -> tuple:
    """For INV-2: the claimable entries per account, and what each
    follow-up acts on -- the same whichever post holds the signal, so
    signals are named by what they are, not by their ids."""
    conn = world.conn
    claimable = tuple(
        sum(1 for a, _ in _claimable(conn) if a == account.account_id) for account in ACCOUNTS
    )
    live = {row[0] for row in _q(
        conn, "SELECT signal_id FROM signals WHERE state = 'NEW' AND withdrawn_at_utc IS NULL"
    )}
    acts_on = []
    for key, post in sorted(world.posts.items()):
        if not post.reply:
            continue
        (row,) = _q(
            conn,
            "SELECT f.followup_id, m.relay_of_followup_id FROM messages m LEFT JOIN followups f "
            "ON f.channel_id = m.channel_id AND f.message_id = m.message_id AND f.edit_seq = 0 "
            "WHERE m.channel_id = ? AND m.message_id = ?",
            CHANNELS[key[0]].channel_id, key[1],
        )
        followup_id = row[0] if row[0] is not None else row[1]
        signal_id = (
            None if followup_id is None
            else relay_repo.signal_for_followup(conn, followup_id=followup_id)
        )
        acts_on.append((world.name(key), _what(conn, signal_id, live)))
    return claimable, tuple(acts_on)


def _what(conn, signal_id: int | None, live: set[int]) -> str | None:
    if signal_id is None:
        return None
    if signal_id in live:
        return "the live call"
    (state, withdrawn) = _q(
        conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = ?",
        signal_id,
    )[0]
    newest = relay_repo.live_signal_for(conn, signal_id) == signal_id
    return f"{state}{', withdrawn' if withdrawn else ''}{'' if newest else ', replaced'}"


# -- the families ------------------------------------------------------------


def _retraction_run(
    seed: int, events: list[tuple], channels: int, *, cancel: bool = False
) -> tuple[list[str], list[str]]:
    """One retraction scenario: the VIP's call, the events, then every
    post withdrawn. Returns (problems, log)."""
    world = _World(_fresh())
    try:
        world.store(_Post(0, world.new_id(0), SIGNAL_TEXT, world.clock))
        world.log.append("post VIP/4001")
        for op, event_seed in events:
            rng = random.Random(event_seed)
            if op == "post":
                _post(world, rng, channels)
            elif op == "forward":
                _forward(world, rng, channels, cross_only=False)
            elif op == "delete":
                _delete(world, rng)
            elif op == "edit":
                _edit(world, rng)
            elif op == "execute":
                world.execute()
            elif op == "read back":
                _read_back(world, rng)
            elif op == "cancel":
                _cancel(world, rng)
            elif op == "cancelled call":
                _cancelled_call(world, rng)
            if problems := _no_double_trade(world.conn):
                return problems, world.log
        _withdraw_everything(world, random.Random(seed), cancel=cancel)
        return _no_double_trade(world.conn) + _retracted(world.conn), world.log
    except Exception as exc:  # noqa: BLE001 -- reported with the seed and the events
        return [_crash(exc)], world.log
    finally:
        world.conn.close()


def _crossing_run(seed: int, events: list[tuple], channels: int) -> tuple[list[str], list[str]]:
    """One window-crossing scenario: the VIP's call, the events -- each
    advancing the clock 20-500 s -- then every post withdrawn. INV-3w after
    every step, INV-1 at the end. Returns (problems, log)."""
    world = _World(_fresh(), ticks=CROSSING_TICKS, tick_rng=random.Random(seed * 7 + 1))
    try:
        world.store(_Post(0, world.new_id(0), SIGNAL_TEXT, world.clock))
        world.log.append("post VIP/4001")
        for op, event_seed in events:
            rng = random.Random(event_seed)
            if op == "post":
                _post(world, rng, channels)
            elif op == "forward":
                _forward(world, rng, channels, cross_only=False)
            elif op == "delete":
                _delete(world, rng)
            elif op == "edit":
                _edit(world, rng)
            elif op == "execute":
                _take_young(world)
            elif op == "read back":
                _read_back(world, rng)
            if problems := _held_twice(world):
                return problems, [*world.log, f"(clock at +{_age(world)} s)"]
        _withdraw_everything(world, random.Random(seed))
        return _held_twice(world) + _retracted(world.conn), world.log
    except Exception as exc:  # noqa: BLE001 -- reported with the seed and the events
        return [_crash(exc)], world.log
    finally:
        world.conn.close()


def _pattern_events(rng: random.Random, channels: int) -> list[tuple]:
    """For the pattern family: posts of the call around the 900 s mark,
    some worker passes, then the first post withdrawn."""
    times = [rng.randint(850, 899), rng.randint(901, 950)]
    times += [rng.randint(1, 1000) for _ in range(rng.randint(0, 2))]
    events: list[tuple] = []
    for at in sorted(times):
        events.append(("post", at, rng.randrange(channels), rng.random() < 0.3))
        if rng.random() < 0.25:
            events.append(("execute", at + 1))
    end = max(times) + rng.randint(1, 30)
    events.append(("withdraw", end, rng.choice(["event", "window", "CHAT", "SELL", "BROKEN",
                                                 "CANCEL"])))
    if rng.random() < 0.5:
        events.append(("execute", end + 1))
    return events


def _pattern_run(seed: int, events: list[tuple]) -> tuple[list[str], list[str]]:
    """One pattern scenario (`_pattern_events`) after the VIP's call at
    +0 s, then every post withdrawn. Returns (problems, log)."""
    world = _World(_fresh())
    try:
        world.store(_Post(0, world.new_id(0), SIGNAL_TEXT, world.clock))
        world.log.append("post VIP/4001 at +0 s")
        for event in events:
            world.clock = POSTED + timedelta(seconds=event[1])
            if event[0] == "post":
                _, at, channel, forwarded = event
                post = _Post(
                    channel, world.new_id(channel), SIGNAL_TEXT, world.clock,
                    (0, 4001) if forwarded else None,
                )
                world.store(post)
                what = "forward of VIP/4001" if forwarded else "post"
                world.log.append(f"{what} {world.name((channel, post.message_id))} at +{at} s")
            elif event[0] == "execute":
                _take_young(world)
            else:
                _, at, how = event
                if (0, 4001) not in world.standing():
                    continue
                if how in ("event", "window"):
                    world.delete([(0, 4001)], by=how)
                else:
                    world.edit((0, 4001), how)
            if problems := _held_twice(world):
                return problems, world.log
        _withdraw_everything(world, random.Random(seed))
        return _held_twice(world) + _retracted(world.conn), world.log
    except Exception as exc:  # noqa: BLE001 -- reported with the seed and the events
        return [_crash(exc)], world.log
    finally:
        world.conn.close()


def _age(world: _World) -> int:
    return int((world.clock - POSTED).total_seconds())


def _retraction_events(rng: random.Random, *, cancel: bool = False) -> list[tuple]:
    ops = ["post", "post", "forward", "forward", "forward", "delete", "delete", "edit",
           "execute", "read back"] + (["cancel", "cancel", "cancelled call"] if cancel else [])
    return [(rng.choice(ops), rng.randrange(2**32)) for _ in range(rng.randint(2, 9))]


def _order_run(
    order: str, forwards: bool, events: list[tuple], seed: int, *, cancel: bool = False
) -> tuple[list, list]:
    """One order scenario: V/4001 and the relay's R/7001 (a forward of
    it, or by hand) stored in `order`, then the events, then every post
    withdrawn. Returns (the outcome after each step, log); an outcome is
    a list of problems instead when an invariant broke."""
    world = _World(_fresh())
    try:
        vip = _Post(0, world.new_id(0), SIGNAL_TEXT, POSTED)
        relay = _Post(
            1, world.new_id(1), SIGNAL_TEXT, POSTED + timedelta(seconds=30),
            (0, 4001) if forwards else None,
        )
        for post in ([vip, relay] if order == "vip-first" else [relay, vip]):
            world.store(post)
        world.clock = POSTED + timedelta(seconds=40)
        world.log.append(f"VIP/4001 and Relay/7001 ({'a forward' if forwards else 'by hand'})")
        outcomes = [_outcome(world)]
        for op, event_seed in events:
            rng = random.Random(event_seed)
            if op == "post":
                _post(world, rng, 2)
            elif op == "forward":
                _forward(world, rng, 2, cross_only=True)
            elif op == "delete":
                _delete(world, rng)
            elif op == "edit":
                _edit(world, rng)
            elif op == "execute":
                world.execute()
            elif op == "reply":
                _reply(world, rng)
            elif op == "read back":
                _read_back(world, rng)
            elif op == "cancel":
                _cancel(world, rng)
            elif op == "cancelled call":
                _cancelled_call(world, rng)
            if problems := _no_double_trade(world.conn):
                return [problems], world.log
            outcomes.append(_outcome(world))
        _withdraw_everything(world, random.Random(seed), cancel=cancel)
        if problems := _no_double_trade(world.conn) + _retracted(world.conn):
            return [problems], world.log
        outcomes.append(_outcome(world))
        return outcomes, world.log
    except Exception as exc:  # noqa: BLE001 -- reported with the seed and the events
        return [[_crash(exc)]], world.log
    finally:
        world.conn.close()


def _order_events(rng: random.Random, *, cancel: bool = False) -> tuple[bool, list[tuple]]:
    ops = ["post", "forward", "delete", "delete", "delete", "edit", "edit", "execute", "reply",
           "reply", "read back"] + (["cancel", "cancel", "cancelled call"] if cancel else [])
    return rng.random() < 0.5, [
        (rng.choice(ops), rng.randrange(2**32)) for _ in range(rng.randint(2, 8))
    ]


def _order_problems(
    seed: int, forwards: bool, events: list[tuple], *, cancel: bool = False
) -> tuple[list[str], list[str]]:
    first, log = _order_run("vip-first", forwards, events, seed, cancel=cancel)
    second, _ = _order_run("relay-first", forwards, events, seed, cancel=cancel)
    for order, outcomes in (("vip-first", first), ("relay-first", second)):
        if outcomes and isinstance(outcomes[-1], list):
            return [f"{order}: {problem}" for problem in outcomes[-1]], log
    for step, (a, b) in enumerate(zip(first, second)):
        if a != b:
            return [f"INV-2 after step {step}: VIP-first {a}, relay-first {b}"], log
    return [], log


def _crash(exc: Exception) -> str:
    """A scenario that raised: the error and where, as a problem."""
    where = traceback.extract_tb(exc.__traceback__)[-1]
    return (
        f"raised {type(exc).__name__}: {exc} "
        f"(at {os.path.basename(where.filename)}:{where.lineno})"
    )


def _shrink(fails, events: list[tuple]) -> list[tuple]:
    """The fewest events (dropping one at a time) that still fail."""
    changed = True
    while changed:
        changed = False
        for index in range(len(events)):
            trial = events[:index] + events[index + 1:]
            if fails(trial):
                events, changed = trial, True
                break
    return events


def _report(family: str, seed: int, problems: list[str], log: list[str]) -> str:
    return "\n".join(
        [f"{family} seed {seed}:", *(f"  {p}" for p in problems), "  events:",
         *(f"    {line}" for line in log)]
    )


def _blocks() -> list[range]:
    return [range(start, min(start + BLOCK, SEEDS)) for start in range(0, SEEDS, BLOCK)]


@pytest.mark.parametrize("seeds", _blocks(), ids=lambda r: f"seeds{r.start}-{r.stop - 1}")
@pytest.mark.parametrize("channels", [1, 2], ids=["one-channel", "two-channels"])
def test_a_call_every_post_of_which_is_withdrawn_is_never_traded(channels, seeds):
    """INV-1 and INV-3 on random scenarios: one channel (double posts,
    forwards of its own posts) or two (forwards both ways)."""
    failures = []
    for seed in seeds:
        base = 1000 * channels + seed
        events = _retraction_events(random.Random(base))
        problems, log = _retraction_run(base, events, channels)
        if problems:
            small = _shrink(lambda e: bool(_retraction_run(base, e, channels)[0]), events)
            problems, log = _retraction_run(base, small, channels)
            failures.append(_report(f"retraction ({channels} channel(s))", base, problems, log))
    assert not failures, "\n\n".join(failures)


@pytest.mark.parametrize("seeds", _blocks(), ids=lambda r: f"seeds{r.start}-{r.stop - 1}")
def test_which_post_is_stored_first_never_changes_what_is_traded(seeds):
    """INV-2 (and INV-1, INV-3) on random scenarios: the VIP's call and a
    relay's copy stored in either order, then the same events."""
    failures = []
    for seed in seeds:
        base = 5000 + seed
        forwards, events = _order_events(random.Random(base))
        problems, log = _order_problems(base, forwards, events)
        if problems:
            small = _shrink(lambda e: bool(_order_problems(base, forwards, e)[0]), events)
            problems, log = _order_problems(base, forwards, small)
            failures.append(_report("order", base, problems, log))
    assert not failures, "\n\n".join(failures)


@pytest.mark.parametrize("seeds", _blocks(), ids=lambda r: f"seeds{r.start}-{r.stop - 1}")
@pytest.mark.parametrize("channels", [1, 2], ids=["one-channel", "two-channels"])
def test_a_call_whose_posts_are_cancelled_or_withdrawn_is_never_traded(channels, seeds):
    """INV-1 and INV-3 with "CANCELLED" appended to posts by hand, as an
    event and as a way the final withdrawal takes a post (ledger 161,
    G3L-3): a cancelled post never keeps a call standing."""
    failures = []
    for seed in seeds:
        base = 20000 + 1000 * channels + seed
        events = _retraction_events(random.Random(base), cancel=True)
        problems, log = _retraction_run(base, events, channels, cancel=True)
        if problems:
            small = _shrink(
                lambda e: bool(_retraction_run(base, e, channels, cancel=True)[0]), events
            )
            problems, log = _retraction_run(base, small, channels, cancel=True)
            failures.append(_report(f"cancel ({channels} channel(s))", base, problems, log))
    assert not failures, "\n\n".join(failures)


@pytest.mark.parametrize("seeds", _blocks(), ids=lambda r: f"seeds{r.start}-{r.stop - 1}")
def test_which_post_is_stored_first_never_changes_what_a_cancel_does(seeds):
    """INV-2 (and INV-1, INV-3) with cancel edits among the events."""
    failures = []
    for seed in seeds:
        base = 26000 + seed
        forwards, events = _order_events(random.Random(base), cancel=True)
        problems, log = _order_problems(base, forwards, events, cancel=True)
        if problems:
            small = _shrink(
                lambda e: bool(_order_problems(base, forwards, e, cancel=True)[0]), events
            )
            problems, log = _order_problems(base, forwards, small, cancel=True)
            failures.append(_report("order with cancels", base, problems, log))
    assert not failures, "\n\n".join(failures)


@pytest.mark.parametrize("seeds", _blocks(), ids=lambda r: f"seeds{r.start}-{r.stop - 1}")
@pytest.mark.parametrize("channels", [1, 2], ids=["one-channel", "two-channels"])
def test_a_call_posted_across_the_relay_window_is_never_traded_twice(channels, seeds):
    """INV-3w and INV-1 with time crossing the 900 s relay window (final
    fix FF12): posts of one call on either side of the window, the first
    one unexecuted and then withdrawn, never give one account two entries
    of the same call."""
    failures = []
    for seed in seeds:
        base = 90000 + 1000 * channels + seed
        events = _retraction_events(random.Random(base))
        problems, log = _crossing_run(base, events, channels)
        if problems:
            small = _shrink(lambda e: bool(_crossing_run(base, e, channels)[0]), events)
            problems, log = _crossing_run(base, small, channels)
            failures.append(_report(f"window crossing ({channels} channel(s))", base, problems, log))
    assert not failures, "\n\n".join(failures)


@pytest.mark.parametrize("seeds", _blocks(), ids=lambda r: f"seeds{r.start}-{r.stop - 1}")
@pytest.mark.parametrize("channels", [1, 2], ids=["one-channel", "two-channels"])
def test_posts_on_either_side_of_the_window_then_the_first_withdrawn_trade_once(channels, seeds):
    """INV-3w and INV-1 on the pattern of final fix FF12, built on purpose:
    the first post unexecuted, posts of the call on either side of the
    900 s mark, then the first post withdrawn."""
    failures = []
    for seed in seeds:
        base = 95000 + 1000 * channels + seed
        events = _pattern_events(random.Random(base), channels)
        problems, log = _pattern_run(base, events)
        if problems:
            small = _shrink(lambda e: bool(_pattern_run(base, e)[0]), events)
            problems, log = _pattern_run(base, small)
            failures.append(_report(f"window pattern ({channels} channel(s))", base, problems, log))
    assert not failures, "\n\n".join(failures)
