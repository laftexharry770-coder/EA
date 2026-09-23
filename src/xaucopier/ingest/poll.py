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
   stops the listener with exit 4 like any other `WriterDeadError`.
2. **Read each enabled channel's history, one channel after another** --
   `Listener.read_history(channel, source="poll", limit=..., rpc_timeout=
   ...)`. That call is the only thing that moves the per-channel pointer:
   it reads everything after the pointer oldest-first, ingests it, and
   advances the pointer inside each ingest's own transaction. The poll
   never writes the pointer itself and never calls `get_messages()`
   itself (Telethon's `get_messages()` defaults to *one* message, which
   is exactly the kind of detail `read_history` exists to own).
3. **Count what the update stream missed.** Every message a poll read
   ingests as `"new"` is one the update stream had not delivered, and is
   added to the durable `metrics(day, 'missed_by_eventstream')` counter
   -- the system's real reliability metric, which `status` must show.
4. **Re-read that channel's recent window by id** (Task 10,
   `ingest/edits.py`'s `reconcile_window`), right after its forward
   read: the `edit_window` (50) newest stored messages at or below the
   pointer, fetched with `get_messages(ids=...)`. The forward read only
   ever sees ids above the pointer, so this is the only history read
   that can find an edit the `MessageEdited` stream dropped. It runs in
   the same per-channel step, under the same `rpc_timeout` and the same
   error handling below, and it never moves the pointer. Edits it finds
   are counted in `edit_missed_by_eventstream`.

Why 5 s and not 30: spec section 9 G6 gates a signal on its own
timestamp, and the Plan 1 review found that a slow poll behind a tight
age gate "recovers" only messages that are already too old to trade --
a safety net that catches nothing while logging that it works.

**What stops the poll, and what does not.** The listener supervises the
poll task: a poll that raises, returns or is cancelled stops the whole
listener with exit 4. So a channel's trouble is caught inside the round
and never escapes it:

- `FloodWaitError` (any `FloodError` carrying `seconds`): *that channel*
  is not read again until the requested time has passed, and the poll
  keeps its interval rather than sleeping the whole wait. In practice
  every channel ends up waiting: Telethon records a flood wait per
  *request type* (`_flood_waited_requests[CONSTRUCTOR_ID]`), so once
  one channel's `GetHistory` is told to wait, every other channel's
  `GetHistory` raises the same `FloodWaitError` locally, without being
  sent, and is deferred in turn. The real gain is that the round -- and
  with it the writer probe -- keeps running every interval instead of
  going dark for as long as Telegram asked. (Telethon itself already
  sleeps through any wait up to the client's `flood_sleep_threshold`,
  120 s, so what reaches here is the long kind.)
- `TimeoutError` from the `rpc_timeout` bound, connection errors, other
  RPC errors, and anything else a read raises: logged, and that channel
  is read again next round.

Three things are *not* a channel's trouble, and end the poll (and so the
listener) at once:

- `WriterDeadError`: there is no writer, so nothing can be stored. Every
  write goes through the listener's writer door, which stops the
  listener on the first one, whoever meets it. The poll re-raises the
  deaths that reach it -- from the probe, a first-run seed's pointer
  write, or the metric write -- rather than carrying on with a round
  that can record nothing. A death met *inside* a read's ingest does not
  reach it: `ingest_history()` records that message as failed and ends
  the batch, and the read returns normally. The listener still stops,
  through the writer door; the poll simply finishes its round first.
- A terminal auth error (`client.TERMINAL_AUTH_ERRORS`): the session is
  dead and no retry revives it. Swallowing it would leave a process that
  looks alive and can never read anything again. The listener re-raises
  it as itself, so the process exits 3 ("do not restart"), not 4.
- `WriterUnresponsiveError`: the writer did not run the probe within
  `writer_probe_timeout` -- a hung writer thread, which stores nothing
  and would hang the next ingest forever.

**One history read per channel at a time.** Channels are read one after
another within a round, and rounds never overlap, so the poll never runs
two `read_history()` calls on one channel concurrently (which would
double-count a failure towards its skip). `read_history()` also enforces
the rule with a per-channel lock, for any other caller.

**What the counter means, exactly.** `"new"` from the poll means the poll
stored the message before the update stream did. Almost always that is a
message the stream dropped; occasionally the poll simply got there first
(a message posted moments before a round, its update not yet
dispatched). That race can only over-count, never hide a miss.

No `sqlite3` here (the `ingest/` package never touches it), and no
`MetaTrader5`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NoReturn

from telethon import errors

from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.edits import DEFAULT_EDIT_WINDOW, reconcile_window, validate_window
from xaucopier.ingest.listener import DEFAULT_RPC_TIMEOUT, HistoryIngest, Listener
from xaucopier.store import db

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
#: out a 10 s `busy_timeout`. Only a hung writer takes a full minute.
DEFAULT_WRITER_PROBE_TIMEOUT = 60.0

#: `messages.source` for everything the poll ingests.
POLL_SOURCE = "poll"
#: `metrics.name` for messages the poll ingested before the update stream.
MISSED_BY_EVENTSTREAM_METRIC = "missed_by_eventstream"


class WriterUnresponsiveError(RuntimeError):
    """The writer thread did not run the poll's no-op probe within
    `writer_probe_timeout`. It is alive but not serving jobs, so nothing
    is being stored; the poll stops, and with it the listener, for the
    supervisor to restart."""


def count_missed(results: tuple[HistoryIngest, ...]) -> int:
    """How many messages in one history read were `"new"` -- stored by
    this read, not by the update stream first."""
    return sum(
        1 for outcome in results if outcome.result is not None and outcome.result.outcome == "new"
    )


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
        from xaucopier.ingest.client import TERMINAL_AUTH_ERRORS

        # channel_id -> clock() reading before which that channel is not
        # read again (a flood wait). Local: it belongs to this run.
        flood_until: dict[int, float] = {}
        logger.info(
            "history poll started: every %gs, up to %d messages per channel per round",
            self.interval,
            self.limit,
        )
        while True:
            await self.sleep(self.interval)
            await self._probe_writer(listener)
            for channel in listener.enabled_channels:
                await self._poll_channel(listener, channel, flood_until, TERMINAL_AUTH_ERRORS)

    async def _probe_writer(self, listener: Listener) -> None:
        try:
            await asyncio.wait_for(listener.probe_writer(), timeout=self.writer_probe_timeout)
        except TimeoutError as exc:
            raise WriterUnresponsiveError(
                f"the writer thread did not run a no-op within {self.writer_probe_timeout:g}s; "
                "it is not storing anything, so the poll is stopping the listener"
            ) from exc

    async def _poll_channel(
        self,
        listener: Listener,
        channel: ChannelCfg,
        flood_until: dict[int, float],
        terminal_errors: tuple[type[Exception], ...],
    ) -> None:
        """One channel's step: its forward history read, then its window
        re-read. Never raises, except for the three stop conditions (and
        cancellation). If the forward read fails, the window waits for the
        next round, like the forward read itself."""
        channel_id = channel.channel_id
        resume_at = flood_until.get(channel_id)
        if resume_at is not None:
            if self.clock() < resume_at:
                return
            del flood_until[channel_id]

        try:
            results = await listener.read_history(
                channel, source=POLL_SOURCE, limit=self.limit, rpc_timeout=self.rpc_timeout
            )
            missed = count_missed(results)
            if missed:
                await listener.record_metric(MISSED_BY_EVENTSTREAM_METRIC, missed)
                logger.warning(
                    "poll recovered %d message(s) on channel %s (%s) that the update "
                    "stream never delivered: %s",
                    missed,
                    channel_id,
                    channel.name,
                    [o.message_id for o in results if o.result is not None and o.result.outcome == "new"],
                )
            await reconcile_window(
                listener, channel, window=self.edit_window, rpc_timeout=self.rpc_timeout
            )
        except db.WriterDeadError:
            raise
        except terminal_errors:
            raise
        except errors.FloodError as exc:
            seconds = getattr(exc, "seconds", None)
            if not isinstance(seconds, int | float):
                logger.exception(
                    "poll of channel %s (%s) hit a flood error with no wait time; "
                    "retrying next round",
                    channel_id,
                    channel.name,
                )
                return
            flood_until[channel_id] = self.clock() + seconds
            logger.error(
                "poll of channel %s (%s) must wait %ss (%s); it is NOT read again until "
                "then. Telethon applies a flood wait to every channel's history read, so "
                "the others will likely report the same; the writer probe keeps running",
                channel_id,
                channel.name,
                seconds,
                type(exc).__name__,
            )
        except TimeoutError:
            logger.warning(
                "poll of channel %s (%s) timed out after %gs; retrying next round (if "
                "this repeats, Telethon may be sleeping through a flood wait of up to "
                "its 120 s flood_sleep_threshold)",
                channel_id,
                channel.name,
                self.rpc_timeout,
            )
        except Exception:  # noqa: BLE001 -- one channel's failure must not stop the poll
            logger.exception(
                "poll of channel %s (%s) failed; retrying next round", channel_id, channel.name
            )
