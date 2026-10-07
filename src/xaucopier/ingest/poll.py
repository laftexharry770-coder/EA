"""The history poll: the safety net under the Telegram update stream.

Telethon has documented, unresolved channel *blackouts* (issue #4361):
for a minute or two the connection is healthy, the client believes it is
caught up, and `NewMessage` simply never fires. The posts are still in
the channel's history. A missed post is a missed trade, silently, so the
update stream is treated as a low-latency hint and **this poll is the
source of truth**.

Every `interval` (5 s) the poll runs one *round*:

1. **Probe the writer** (`Listener.probe_writer()`, a `SELECT 1` on the
   writer thread). A dead writer is otherwise noticed only by the next
   write, and a quiet channel makes none; the probe surfaces a death
   within one interval, through the listener's own writer door, so it
   stops the listener with exit 4 like any other `WriterDeadError`. A
   probe still queued at its deadline is not proof of a hung writer: if
   the writer started or finished any job meanwhile it is busy, not
   hung (another program holding the database's write lock makes every
   queued job wait out the busy timeout), and the round carries on.
2. **Read each enabled channel's history, one channel after another** --
   `Listener.read_history(channel, source="poll", limit=..., rpc_timeout=
   ...)`. That call is the only thing that moves the per-channel pointer:
   it reads everything after the pointer oldest-first, ingests it, and
   advances the pointer inside each ingest's own transaction. The poll
   never writes the pointer itself and never calls `get_messages()`
   itself (Telethon's `get_messages()` defaults to *one* message, which
   is exactly the kind of detail `read_history` exists to own).
3. **Count what the update stream missed.** Every message a poll read
   ingests as `"new"` is one the update stream had not delivered. If it
   was posted while this watch ran -- dated at or after its start
   (`Listener.started_at`) less the 60 s seed margin -- it is added to
   the durable `metrics(day, 'missed_by_eventstream')` counter, the
   system's real reliability metric, which `status` must show. One dated
   before that is backlog from while watch was stopped (a long downtime
   past backfill's 500, or a channel switched back on), which the stream
   could not have delivered: it is counted apart, in
   `backlog_recovered` (final review F17), so it cannot pass for dozens
   of drops a day.
4. **Re-read that channel's recent window by id** (Task 10,
   `ingest/edits.py`'s `reconcile_window`), right after its forward
   read: the `edit_window` (50) newest stored messages, above the
   pointer too (final review F03), fetched with `get_messages(ids=...)`.
   The forward read only ever sees ids above the pointer, and never one
   that was deleted, so this is the only history read that can find an
   edit below it, or -- by a message's absence -- a deletion anywhere,
   that the `MessageEdited`/`MessageDeleted` stream dropped. It runs in
   the same per-channel step, only once that channel's forward read has
   succeeded this round, under the same `rpc_timeout` and the same error
   handling below, and it never moves the pointer. Edits and deletions
   it finds are counted in `edit_missed_by_eventstream` /
   `delete_missed_by_eventstream`.
5. **Stamp the round** (Task 11): one writer job records
   `heartbeats.POLL_ROUND` ("N of M channel(s) read") and, for each
   channel whose forward read succeeded this round, its
   `channel_read:<id>` stamp. If any read succeeded, the same job
   acknowledges the session alerts (`alerts.SESSION_ALERT_CODES`: a dead
   session, a rejected api_id) -- Telegram has just answered, so the
   condition they report is gone. `xaucopier status` runs in another process
   and reads these to say whether `watch` is alive and whether Telegram
   is still answering. A stamp that fails to write is logged and the
   poll carries on -- a missing stamp makes `status` look stale, never
   makes a message go missing -- except a `WriterDeadError`, which stops
   the poll like any other.

Why 5 s and not 30: spec section 9 G6 gates a signal on its own
timestamp, and the Plan 1 review found that a slow poll behind a tight
age gate "recovers" only messages that are already too old to trade --
a safety net that catches nothing while logging that it works.

**What stops the poll, and what does not.** The listener supervises the
poll task: a poll that raises, returns or is cancelled stops the whole
listener with exit 4. So a channel's trouble is caught inside the round
and never escapes it:

- `FloodWaitError` (any `FloodError` carrying `seconds`): *that step of
  that channel* -- its forward read, or its window re-read -- is not run
  again until the requested time has passed, and the poll keeps its
  interval rather than sleeping the whole wait. In practice every
  channel ends up waiting: Telethon records a flood wait per *request
  type* (`_flood_waited_requests[CONSTRUCTOR_ID]`), so once one
  channel's `GetHistory` is told to wait, every other channel's
  `GetHistory` raises the same `FloodWaitError` locally, without being
  sent, and is deferred in turn. The real gain is that the round -- and
  with it the writer probe -- keeps running every interval instead of
  going dark for as long as Telegram asked. (Telethon itself already
  sleeps through any wait up to the client's `flood_sleep_threshold`,
  120 s, so what reaches here is the long kind.) The two steps are
  deferred separately for the same reason: the window re-read is a
  different request type (`channels.GetMessages`), and a flood wait on
  it must never hold back the forward read, which is how a message the
  update stream dropped -- a trade -- is recovered.
- `TimeoutError` from the `rpc_timeout` bound, connection errors, other
  RPC errors, and anything else a read raises: logged, and that channel
  is read again next round.

Three things are *not* a channel's trouble, and end the poll (and so the
listener) at once:

- `WriterDeadError`: there is no writer, so nothing can be stored. Every
  write goes through the listener's writer door, which stops the
  listener on the first one, whoever meets it. The poll re-raises the
  deaths that reach it -- from the probe, a first-run seed's pointer
  write, the metric write, or the round stamp -- rather than carrying on
  with a round that can record nothing. A death met *inside* a read's ingest does not
  reach it: `ingest_history()` records that message as failed and ends
  the batch, and the read returns normally. The listener still stops,
  through the writer door; the poll simply finishes its round first.
- A permanent Telegram error (`client.PERMANENT_TELEGRAM_ERRORS`): the
  session or account is dead (`TERMINAL_AUTH_ERRORS`, exit 3), or
  Telegram rejects the api_id/api_hash (`API_CREDENTIAL_ERRORS`, exit 5).
  No retry revives either. Swallowing one would leave a process that
  looks alive -- `status` saying RUNNING -- and can never read anything
  again. The listener re-raises it as itself, so the process exits 3 or
  5 ("do not restart"), not 4. (`FrozenMethodInvalidError` is a
  `FloodError`; it is matched here first, never deferred as a flood.)
- `WriterUnresponsiveError`: the writer neither ran the probe nor
  started or finished any other job within `writer_probe_timeout` -- a
  hung writer thread, which stores nothing and would hang the next
  ingest forever.

**One history read per channel at a time.** Channels are read one after
another within a round, and rounds never overlap, so the poll never runs
two `read_history()` calls on one channel concurrently (which would
double-count a failure towards its skip). `read_history()` also enforces
the rule with a per-channel lock, for any other caller.

**What the counter means, exactly.** `"new"` from the poll means the poll
stored the message before the update stream did. Almost always that is a
message the stream dropped; occasionally the poll simply got there first
(a message posted moments before a round, its update not yet
dispatched). That race can only over-count, never hide a miss. A "new"
message posted before this watch started (less the 60 s margin) is not
counted as missed at all: it is backlog, `backlog_recovered` (F17).
Posts from the margin itself still count as missed -- the margin exists
because the two clocks may disagree by that much -- so that side, too,
can only over-count.

No `sqlite3` here (the `ingest/` package never touches it), and no
`MetaTrader5`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from telethon import errors

from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.edits import (
    DEFAULT_EDIT_WINDOW,
    AbsenceTracker,
    reconcile_window,
    validate_window,
)
from xaucopier.ingest.listener import (
    DEFAULT_RPC_TIMEOUT,
    FIRST_RUN_SEED_MARGIN,
    HistoryIngest,
    Listener,
    wait_words,
)
from xaucopier.store import alerts, db, heartbeats

logger = logging.getLogger(__name__)

#: Seconds between rounds. See the module docstring for why it is 5.
DEFAULT_POLL_INTERVAL = 5.0
#: Messages fetched per channel per round. A longer gap is fine: the
#: pointer stops at the last id read and the next round continues.
DEFAULT_POLL_LIMIT = 100
#: The hard bound on each `get_messages()` RPC is `DEFAULT_RPC_TIMEOUT`,
#: imported from the listener above: backfill uses the same bound.
#: Hard bound on the per-round writer probe. Generous: the probe queues
#: behind whatever the writer is doing, and each job may legitimately wait
#: out a 10 s `busy_timeout`. Past it, the writer still counts as alive if
#: it started or finished any job meanwhile; only a hung writer does not.
DEFAULT_WRITER_PROBE_TIMEOUT = 60.0

#: `messages.source` for everything the poll ingests.
POLL_SOURCE = "poll"
#: `metrics.name` for messages the poll ingested before the update stream.
MISSED_BY_EVENTSTREAM_METRIC = "missed_by_eventstream"
#: `metrics.name` for messages the poll ingested that were posted before
#: this watch started -- backlog from while it was stopped (final review
#: F17), which the update stream could not have delivered.
BACKLOG_RECOVERED_METRIC = "backlog_recovered"
#: The two steps of a channel's turn in a round, each flood-deferred on
#: its own: the forward history read, then the window re-read.
HISTORY_STEP = "history"
WINDOW_STEP = "window"

#: What a step's trouble is, for the window's one line when it starts and
#: for the round stamp `status` reads (final fix FF20).
TroubleKind = str
_WAIT = "wait"
_UNREADABLE = "unreadable"
_TIMEOUT = "timeout"
_FAILED = "failed"


def _doing(step: str, channel: ChannelCfg) -> str:
    if step == HISTORY_STEP:
        return f"reading {channel.name}"
    return f"checking the recent messages of {channel.name} for edits and deletions"


def _unreadable_errors() -> tuple[type[Exception], ...]:
    """What Telegram answers when this account cannot read a channel: it
    left it, was removed from it, or the channel is gone."""
    return (errors.ChannelPrivateError, errors.ChannelInvalidError)


class WriterUnresponsiveError(RuntimeError):
    """The writer thread did not run the poll's no-op probe within
    `writer_probe_timeout`. It is alive but not serving jobs, so nothing
    is being stored; the poll stops, and with it the listener, for the
    supervisor to restart. `plain` says it in the user's words, for the
    watch window; the message is the technical detail, for the log file
    (group-2 review leftover G2L-2)."""

    def __init__(self, message: str, *, plain: str) -> None:
        super().__init__(message)
        self.plain = plain


def _retrieve_outcome(task: asyncio.Future[object]) -> None:
    """Mark a probe's outcome as seen, so a probe left to finish in the
    queue never logs "exception was never retrieved". A writer death it
    meets has already been reported through the listener's writer door."""
    if not task.cancelled():
        task.exception()


#: `ingest()` outcomes that mean the message was stored for the first time.
_FIRST_STORED = frozenset({"new", "relay_duplicate"})


def split_new(
    results: tuple[HistoryIngest, ...], *, live_from: datetime | None
) -> tuple[list[int], list[int]]:
    """The ids of one history read's first-stored messages -- `"new"`, or
    a `"relay_duplicate"` (a copy of a call we already have is stored for
    the first time too; final review F02) -- as (missed by the update
    stream, backlog): a message dated at or after `live_from` was posted
    while this watch ran and the stream should have delivered it; one
    dated before it was posted while watch was stopped (final review
    F17). With no `live_from`, or no date, a message counts as missed --
    the side that can only over-count."""
    missed: list[int] = []
    backlog: list[int] = []
    for outcome in results:
        if outcome.result is None or outcome.result.outcome not in _FIRST_STORED:
            continue
        posted = outcome.posted_at_utc
        if live_from is not None and posted is not None and posted < live_from:
            backlog.append(outcome.message_id)
        else:
            missed.append(outcome.message_id)
    return missed, backlog


def _positive_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and value > 0


@dataclass(frozen=True, slots=True)
class HistoryPoll:
    """The poll, configured. An instance is the listener's `poll_hook`:
    `Listener(..., poll_hook=HistoryPoll())`.

    `sleep` and `clock` are injectable so a test can step the poll one
    round at a time without waiting real seconds. `clock` is a monotonic
    seconds counter (never a wall-clock datetime) and is only used to
    honour flood waits. `rpc_timeout` and `writer_probe_timeout` are real
    `asyncio.wait_for` bounds; `rpc_timeout` bounds the window re-read's
    by-id RPC too. `edit_window` is how many recent stored messages each
    channel's window re-read covers (1 to `edits.MAX_EDIT_WINDOW`).
    """

    interval: float = DEFAULT_POLL_INTERVAL
    limit: int = DEFAULT_POLL_LIMIT
    rpc_timeout: float = DEFAULT_RPC_TIMEOUT
    writer_probe_timeout: float = DEFAULT_WRITER_PROBE_TIMEOUT
    edit_window: int = DEFAULT_EDIT_WINDOW
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        # interval > 0 is what makes the poll unable to spin.
        for name in ("interval", "rpc_timeout", "writer_probe_timeout"):
            if not _positive_number(getattr(self, name)):
                raise ValueError(f"{name} must be a positive number, got {getattr(self, name)!r}")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise ValueError(f"limit must be a positive int, got {self.limit!r}")
        validate_window(self.edit_window, name="edit_window")

    async def __call__(self, listener: Listener) -> NoReturn:
        """Poll forever: sleep one interval, run one round, repeat. Only
        the three conditions in the module docstring (and cancellation)
        end it."""
        # Imported here, as run_listener() does, so importing this module
        # does not load pywin32.
        from xaucopier.ingest.client import PERMANENT_TELEGRAM_ERRORS

        # (step, channel_id) -> clock() reading before which that step of
        # that channel is not run again (a flood wait). Local: it belongs
        # to this run.
        flood_until: dict[tuple[str, int], float] = {}
        # (step, channel_id) -> the trouble that step is in, while it lasts:
        # said once when it starts and once when it is over (FF20). Local.
        trouble: dict[tuple[str, int], TroubleKind] = {}
        # Absences the window re-read is waiting to see confirmed, and
        # which channels' reads are suspect (ingest/edits.py). Local too.
        absences = AbsenceTracker()
        logger.info(
            "history poll started: every %gs, up to %d messages per channel per round",
            self.interval,
            self.limit,
        )
        while True:
            await self.sleep(self.interval)
            await self._probe_writer(listener)
            channels = listener.enabled_channels
            read: list[int] = []
            for channel in channels:
                if await self._poll_channel(
                    listener, channel, flood_until, PERMANENT_TELEGRAM_ERRORS, absences, trouble
                ):
                    read.append(channel.channel_id)
            unread = [c.channel_id for c in channels if c.channel_id not in read]
            await self._stamp_round(
                listener,
                read=tuple(read),
                total=len(channels),
                waiting=sum(1 for c in unread if trouble.get((HISTORY_STEP, c)) == _WAIT),
                unreadable=sum(
                    1 for c in unread if trouble.get((HISTORY_STEP, c)) == _UNREADABLE
                ),
            )

    async def _stamp_round(
        self,
        listener: Listener,
        *,
        read: tuple[int, ...],
        total: int,
        waiting: int = 0,
        unreadable: int = 0,
    ) -> None:
        """Step 5: record that this round finished, and which channels it
        read, for `xaucopier status`. One writer job. Never raises except
        `WriterDeadError` (and cancellation): a stamp is a report, and
        failing to write one must not stop the poll that recovers trades.
        Of the channels not read, how many wait because Telegram asked
        (`waiting`) and how many this account cannot read (`unreadable`)
        follow the count, so `status` can say why (FF20)."""
        detail = f"{len(read)} of {total} channel(s) read"
        if waiting:
            detail += f"; waiting: {waiting}"
        if unreadable:
            detail += f"; cannot read: {unreadable}"
        beats: dict[str, str | None] = {heartbeats.POLL_ROUND: detail}
        for channel_id in read:
            beats[heartbeats.channel_read_name(channel_id)] = None
        try:
            # A read that succeeded proves Telegram accepts this session and
            # this api_id again: the alerts saying otherwise are resolved.
            await listener.record_heartbeats(
                beats,
                resolve_alerts=alerts.SESSION_ALERT_CODES if read else (),
                readable_channels=read,
            )
        except db.WriterDeadError:
            raise
        except Exception:  # noqa: BLE001 -- a lost stamp only makes status look stale
            logger.exception("could not record the poll round for `status`; carrying on")

    async def _probe_writer(self, listener: Listener) -> None:
        """Step 1. The writer is alive if it runs the probe within
        `writer_probe_timeout` -- or, failing that, if it started or
        finished *some* job in that time (`Listener.writer_progressed_
        since`): a probe queued behind jobs that each wait out another
        program's write lock can miss any fixed deadline while the writer
        is perfectly healthy (group-1 review leftover G1L-1). The probe is
        then left to finish in the queue. Only a writer that made no
        progress at all is `WriterUnresponsiveError`."""
        queued_at = time.monotonic()
        probe = asyncio.ensure_future(listener.probe_writer())
        probe.add_done_callback(_retrieve_outcome)
        try:
            await asyncio.wait_for(asyncio.shield(probe), timeout=self.writer_probe_timeout)
        except TimeoutError as exc:
            if listener.writer_progressed_since(queued_at):
                logger.warning(
                    "the database is slow to answer (still busy after %gs); carrying on",
                    self.writer_probe_timeout,
                )
                return
            probe.cancel()
            raise WriterUnresponsiveError(
                f"the writer thread did not run a no-op within {self.writer_probe_timeout:g}s "
                "and finished no other job in that time; it is not storing anything, so "
                "the poll is stopping the listener",
                plain=(
                    f"The database stopped answering for {self.writer_probe_timeout:g} "
                    "seconds, so nothing was being recorded."
                ),
            ) from exc

    async def _poll_channel(
        self,
        listener: Listener,
        channel: ChannelCfg,
        flood_until: dict[tuple[str, int], float],
        terminal_errors: tuple[type[Exception], ...],
        absences: AbsenceTracker,
        trouble: dict[tuple[str, int], TroubleKind] | None = None,
    ) -> bool:
        """One channel's step: its forward history read, then -- only if
        that read succeeded this round -- its window re-read. Each is
        guarded on its own (`_guarded`), so a flood wait on one never
        defers the other. Returns whether the forward read succeeded.
        Never raises, except for the three stop conditions (and
        cancellation)."""
        trouble = {} if trouble is None else trouble
        read = await self._guarded(
            channel,
            flood_until,
            terminal_errors,
            step=HISTORY_STEP,
            action=lambda: self._forward_read(listener, channel),
            trouble=trouble,
            listener=listener,
        )
        if not read:
            return False
        await self._guarded(
            channel,
            flood_until,
            terminal_errors,
            trouble=trouble,
            listener=listener,
            step=WINDOW_STEP,
            action=lambda: reconcile_window(
                listener,
                channel,
                window=self.edit_window,
                rpc_timeout=self.rpc_timeout,
                absences=absences,
            ),
        )
        return True

    async def _forward_read(self, listener: Listener, channel: ChannelCfg) -> None:
        results = await listener.read_history(
            channel, source=POLL_SOURCE, limit=self.limit, rpc_timeout=self.rpc_timeout
        )
        started_at = listener.started_at
        missed, backlog = split_new(
            results,
            live_from=None if started_at is None else started_at - FIRST_RUN_SEED_MARGIN,
        )
        if missed:
            await listener.record_metric(MISSED_BY_EVENTSTREAM_METRIC, len(missed))
            logger.warning(
                "%d message(s) of %s (id %s) did not arrive as they were posted; they "
                "were read a moment later: %s",
                len(missed),
                channel.name,
                channel.channel_id,
                missed,
            )
        if backlog:
            await listener.record_metric(BACKLOG_RECOVERED_METRIC, len(backlog))
            logger.info(
                "read %d earlier message(s) on channel %s (%s), posted while watch was "
                "not running: %s",
                len(backlog),
                channel.channel_id,
                channel.name,
                backlog,
            )

    async def _guarded(
        self,
        channel: ChannelCfg,
        flood_until: dict[tuple[str, int], float],
        terminal_errors: tuple[type[Exception], ...],
        *,
        step: str,
        action: Callable[[], Awaitable[object]],
        trouble: dict[tuple[str, int], TroubleKind] | None = None,
        listener: Listener | None = None,
    ) -> bool:
        """Run one step of one channel; True if it ran and succeeded.
        False if it is deferred by an earlier flood wait, or failed (which
        is logged and handled here -- see the module docstring). Only the
        three stop conditions propagate.

        The window gets one plain line when a step's trouble starts (or
        changes kind) and one when it is over; the technical detail -- a
        traceback, the error's name -- goes to watch.log only (final fix
        FF20: a traceback every round, for ever, was the window's lot). A
        channel this account cannot read is a WARNING alert as well, once
        per spell, cleared when the channel is read again."""
        channel_id = channel.channel_id
        key = (step, channel_id)
        what = "poll" if step == HISTORY_STEP else "window re-read"
        trouble = {} if trouble is None else trouble
        resume_at = flood_until.get(key)
        if resume_at is not None:
            if self.clock() < resume_at:
                return False
            del flood_until[key]

        try:
            await action()
        except db.WriterDeadError:
            raise
        except terminal_errors:
            raise
        except errors.FloodError as exc:
            seconds = getattr(exc, "seconds", None)
            if isinstance(seconds, int | float):
                flood_until[key] = self.clock() + seconds
                logger.info(
                    "%s of channel %s (%s) must wait %ss (%s); not run again until then",
                    what, channel_id, channel.name, seconds, type(exc).__name__,
                    extra=db.LOG_FILE_ONLY,
                )
                trouble[key] = _WAIT
                logger.warning(
                    "Telegram asked watch to wait %s before %s again. watch goes on with "
                    "the rest meanwhile; new posts still arrive as they are posted.",
                    wait_words(seconds),
                    _doing(step, channel),
                )
                return False
            self._failed(channel, step, trouble, _FAILED, what)
            return False
        except _unreadable_errors() as exc:
            logger.info(
                "%s of channel %s (%s) refused: %r", what, channel_id, channel.name, exc,
                extra=db.LOG_FILE_ONLY,
            )
            if trouble.get(key) != _UNREADABLE:
                trouble[key] = _UNREADABLE
                said = (
                    f"This Telegram account can no longer read {channel.name} (id "
                    f"{channel_id}): it may have left the channel or been removed from it, or "
                    "the channel is gone. Please join it again in Telegram with this account; "
                    "until then nothing it posts is recorded."
                )
                # The alert's own line says it in the window; this one only
                # when the alert could not be recorded.
                if not await self._channel_unreadable(listener, channel, said):
                    logger.warning("%s", said)
            return False
        except TimeoutError:
            logger.info(
                "%s of channel %s (%s) timed out after %gs", what, channel_id, channel.name,
                self.rpc_timeout, extra=db.LOG_FILE_ONLY,
            )
            if trouble.get(key) != _TIMEOUT:
                trouble[key] = _TIMEOUT
                logger.warning(
                    "Telegram did not answer within %g seconds when %s; watch tries again "
                    "every few seconds.",
                    self.rpc_timeout,
                    _doing(step, channel),
                )
            return False
        except Exception:  # noqa: BLE001 -- one channel's failure must not stop the poll
            self._failed(channel, step, trouble, _FAILED, what)
            return False
        if trouble.pop(key, None) is not None:
            doing = _doing(step, channel)
            logger.warning("%s works again.", doing[0].upper() + doing[1:])
        return True

    def _failed(
        self,
        channel: ChannelCfg,
        step: str,
        trouble: dict[tuple[str, int], TroubleKind],
        kind: TroubleKind,
        what: str,
    ) -> None:
        """A step that failed for a reason nobody gave words to: one plain
        line when it starts, pointing at the log file, where the traceback
        goes (only the first of a spell's; the rest one line each)."""
        key = (step, channel.channel_id)
        if trouble.get(key) == kind:
            logger.info(
                "%s of channel %s (%s) failed again; retrying next round",
                what, channel.channel_id, channel.name, exc_info=False, extra=db.LOG_FILE_ONLY,
            )
            return
        trouble[key] = kind
        subject = (
            channel.name
            if step == HISTORY_STEP
            else f"The recent messages of {channel.name}"
        )
        verb = "read" if step == HISTORY_STEP else "checked"
        logger.warning(
            "%s could not be %s just now; watch tries again every few seconds.",
            subject,
            verb,
            extra=db.POINTS_TO_DETAILS,
        )
        logger.exception(
            "%s of channel %s (%s) failed; retrying next round",
            what, channel.channel_id, channel.name, extra=db.LOG_FILE_ONLY,
        )

    async def _channel_unreadable(
        self, listener: Listener | None, channel: ChannelCfg, said: str
    ) -> bool:
        """The WARNING alert for a channel this account can no longer read
        (FF20); whether it was recorded. A failure to record it is logged;
        the poll goes on."""
        if listener is None:
            return False
        try:
            await listener.record_alert(
                "WARNING",
                alerts.TELEGRAM_CHANNEL_UNREADABLE,
                said[0].lower() + said[1:] + " This alert clears itself once watch reads the "
                "channel again.",
                channel_id=channel.channel_id,
            )
        except db.WriterDeadError:
            raise
        except Exception:  # noqa: BLE001 -- the caller says it in the window instead
            logger.exception(
                "could not record the alert for channel %s", channel.channel_id,
                extra=db.LOG_FILE_ONLY,
            )
            return False
        return True
