"""The window re-read: how edits and deletions the update stream dropped
are found (spec 8.3; Plan 2 Task 10).

The history poll's forward read (`Listener.read_history`) fetches only ids
*above* the per-channel pointer. An edited or deleted message is at or
below it, so the forward read never sees either -- and `MessageEdited` /
`MessageDeleted` are as prone to Telethon's channel blackouts as
`NewMessage` is. An edited stop we never see is a live position with the
wrong stop; a deletion we never see is a retracted trade, still held. So
after each channel's forward read the poll calls `reconcile_window()`,
which:

1. takes that channel's history lock (`Listener.history_lock`), so it
   never interleaves with a history read of the same channel;
2. picks the **window**: the `window` newest stored messages of the
   channel (`edits_repo.window_versions`, one indexed read on the reader
   connection) -- leaving out those a `MessageDeleted` event deleted,
   keeping those an earlier window re-read only *inferred* deleted. That
   includes messages stored live *above* the pointer (final review F03):
   the forward read meets an edited one's current version anyway when it
   passes it, but it never passes a *deleted* one, so without them the
   deletion of a channel's newest signal went unseen until the channel
   posted again. Every message in the window is stored, so an absence
   means as much above the pointer as below it;
3. fetches them by id in one bounded RPC -- `get_messages(channel,
   ids=[...])` under `asyncio.wait_for(..., rpc_timeout)` -- which
   returns each message's *current* version, or `None` for one that is
   gone;
4. hands every message that came back changed -- a newer `edit_date`, or
   the same `edit_date` with different text (two edits in one second) --
   to `Listener.ingest_edit` (`detected_by="window"`), and every message
   an earlier read had inferred deleted, which proves that inference
   wrong (`edits_repo` clears the mark and raises CRITICAL
   `DELETION_REVERSED`);
5. infers deletions from **absence** -- guarded, below -- and hands them
   to `Listener.ingest_deletion`.

What an edit or deletion then *does* is `store/edits_repo.py`'s policy,
decided in one writer job against the stored row, so a change the event
already delivered is a no-op here, and vice versa.

**Absence is evidence, not proof** (Task 10 review). Telethon turns a
`MessageIdsEmptyError` into `None` for *every* id (telethon/client/
messages.py:309-318), Telegram may leave ids out altogether
(:324-327), and a single stray `None` is possible. Treating each as a
deletion voided every unexecuted signal in the window at once. So:

- **Circuit breaker.** Count the missing ids among those the window
  expected to be present (not already marked deleted). If *all* of them
  are missing, or at least `SUSPECT_MIN_ABSENT` (3) are and they are more
  than half, the answer is suspect: no deletion is inferred from it at
  all, one CRITICAL `WINDOW_READ_SUSPECT` alert is raised for the channel
  (once per run of suspect reads, not every round), and the edits of the
  messages that *did* come back are still applied. A consequence worth
  knowing: in a window of one or two messages, deleting all of them trips
  the breaker too; the alert says so, and the `MessageDeleted` event is
  still acted on.
- **Two reads, not one.** An id missing from a read that passed the
  breaker becomes *pending*. It is treated as deleted only if the next
  read that passes the breaker is missing it too; the moment any read
  returns it, it is no longer pending. Chosen over an immediate
  single-id re-read because a fault that swallowed a message a moment
  ago will very likely swallow it again a millisecond later -- the
  reviewer's own probe injects exactly such a fault -- while a read one
  poll interval (5 s) later is a far more independent witness, and it
  costs no extra RPC. The price is one more round (about 5 s) before an
  absence-only deletion acts; the `MessageDeleted` event, which names its
  ids explicitly, still acts at once. Pending state lives in memory
  (`AbsenceTracker`, one per poll run); a restart simply starts over.
- A window re-read that voids any signal raises one CRITICAL
  `WINDOW_VOIDED_SIGNALS` alert with the count and the signal ids, on top
  of each signal's own WARNING -- a mass void must not read as noise. A
  void that promoted a standing repost of the call in its place is left
  out: that call is still queued (G3L-2).

**It never moves the pointer.** It writes only through `ingest_edit`,
`ingest_deletion` and `record_alert`, none of which touches
`channel_state`; it reads the pointer only to report it. The pointer is
`read_history`'s alone: a read that advanced it past an id it had not
ingested would lose that message for good (the reviewer's D1) -- and a
stored message above the pointer, re-read here, is still met again by
the forward read, which finds it stored.

**Matched by id, never by position.** Because Telegram may leave an id
out of its answer (telethon/client/messages.py:324-327), the list need not
zip one-to-one with the ids asked for. A message counts as present only
if a returned message carries its id.

**Failures.** The RPC's errors -- a flood wait, the `rpc_timeout`, a
dropped connection -- propagate to the poll, which handles them as it
handles the forward read's (per channel, retried next round; a flood wait
on this read defers only this step). One message whose job fails is
recorded in `handler_errors` (`source="window"`) and the rest of the
window still runs; it is retried next round (a failed deletion stays
pending). `WriterDeadError` always propagates: with no writer nothing can
be stored, and the listener must stop.

No `sqlite3` here and no `MetaTrader5` (tests enforce both).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.listener import IncomingMessage, Listener, normalise_message
from xaucopier.store import db, edits_repo
from xaucopier.store.edits_repo import DeletionResult, EditResult, StoredVersion

logger = logging.getLogger(__name__)

#: How many recent stored messages per channel the poll re-reads by id
#: each round.
DEFAULT_EDIT_WINDOW = 50
#: The most a window may hold: Telethon sends one `GetMessagesRequest`
#: per 100 ids (`_MAX_CHUNK_SIZE`, telethon/client/messages.py:10) and
#: waits 10 s between requests past 300, so up to 100 keeps each re-read
#: to exactly one RPC.
MAX_EDIT_WINDOW = 100
#: The circuit breaker: a read missing every expected id, or at least
#: this many and more than half of them, is suspect (module docstring).
SUSPECT_MIN_ABSENT = 3

#: `handler_errors` source, and `edits_repo` detector, for this path.
WINDOW_SOURCE = edits_repo.WINDOW_DETECTOR


class AbsenceTracker:
    """What the window re-read remembers between rounds, per channel: the
    ids missing from the last read that passed the circuit breaker (each
    becomes a deletion only if the next such read misses it too), and
    whether the channel's reads are currently suspect (so the CRITICAL
    alert is raised once per run of suspect reads, not every round).

    Mutable, in memory, one per poll run -- deliberately not persisted: a
    restart re-observes, which only ever delays an absence-only deletion.
    """

    __slots__ = ("_pending", "_suspect")

    def __init__(self) -> None:
        self._pending: dict[int, frozenset[int]] = {}
        self._suspect: set[int] = set()

    def pending(self, channel_id: int) -> frozenset[int]:
        return self._pending.get(channel_id, frozenset())

    def seen(self, channel_id: int, present: Iterable[int]) -> None:
        """Ids a read returned: none of them is pending any more."""
        self._pending[channel_id] = self.pending(channel_id) - frozenset(present)

    def settle(self, channel_id: int, absent: Iterable[int]) -> frozenset[int]:
        """A read that passed the breaker missed `absent`. Returns those
        missed by the previous such read too -- confirmed deletions -- and
        keeps the rest pending."""
        absent = frozenset(absent)
        confirmed = absent & self.pending(channel_id)
        self._pending[channel_id] = absent - confirmed
        return confirmed

    def keep_pending(self, channel_id: int, message_ids: Iterable[int]) -> None:
        """A confirmed deletion whose job failed: it stays pending, so the
        next read that misses it confirms it again."""
        self._pending[channel_id] = self.pending(channel_id) | frozenset(message_ids)

    def enter_suspect(self, channel_id: int) -> bool:
        """True the first time in a run of suspect reads."""
        if channel_id in self._suspect:
            return False
        self._suspect.add(channel_id)
        return True

    def leave_suspect(self, channel_id: int) -> bool:
        """True if the channel's reads had been suspect until now."""
        if channel_id not in self._suspect:
            return False
        self._suspect.discard(channel_id)
        return True


@dataclass(frozen=True, slots=True)
class WindowResult:
    """What one window re-read of one channel found."""

    channel_id: int
    #: The channel's pointer when the window was taken (read, never
    #: written; the window is not bounded by it).
    pointer: int
    #: The ids asked for, oldest first; empty if there was nothing to ask.
    requested: tuple[int, ...]
    edits: tuple[EditResult, ...]
    deletions: tuple[DeletionResult, ...]
    #: Missing ids waiting for a second read before they count as deleted.
    pending: tuple[int, ...]
    #: The read tripped the circuit breaker: no deletion was inferred.
    suspect: bool
    #: Ids whose job failed (recorded in `handler_errors`; retried next round).
    failed: tuple[int, ...]

    @property
    def changed(self) -> bool:
        return any(e.changed for e in self.edits) or any(
            d.outcome not in ("unknown", "already_deleted") for d in self.deletions
        )


def validate_window(window: object, *, name: str = "edit_window") -> int:
    """`window` as a positive int no larger than `MAX_EDIT_WINDOW`, or
    `ValueError` naming `name`."""
    if (
        isinstance(window, bool)
        or not isinstance(window, int)
        or not 1 <= window <= MAX_EDIT_WINDOW
    ):
        raise ValueError(
            f"{name} must be an int from 1 to {MAX_EDIT_WINDOW}, got {window!r}"
        )
    return window


def is_suspect(absent: int, expected: int) -> bool:
    """The circuit breaker: `absent` of the `expected` ids came back
    missing. Every one missing, or at least `SUSPECT_MIN_ABSENT` and more
    than half, is not believed."""
    if expected == 0 or absent == 0:
        return False
    return absent == expected or (absent >= SUSPECT_MIN_ABSENT and absent * 2 > expected)


async def reconcile_window(
    listener: Listener,
    channel_cfg: ChannelCfg,
    *,
    window: int,
    rpc_timeout: float,
    absences: AbsenceTracker,
) -> WindowResult:
    """Re-read `channel_cfg`'s recent window by id and apply every edit
    found, and every deletion the guards confirm (see the module
    docstring). `rpc_timeout` is required and bounds the one RPC; its
    `TimeoutError`, like any RPC error, propagates to the caller.
    `absences` carries pending absences from one call to the next."""
    validate_window(window)
    if (
        isinstance(rpc_timeout, bool)
        or not isinstance(rpc_timeout, int | float)
        or rpc_timeout <= 0
    ):
        raise ValueError(f"rpc_timeout must be a positive number, got {rpc_timeout!r}")
    channel_id = channel_cfg.channel_id

    async with listener.history_lock(channel_id):
        pointer = listener.read_last_processed_message_id(channel_id)
        stored = edits_repo.window_versions(
            listener.reader, channel_id=channel_id, limit=window
        )
        requested = tuple(version.message_id for version in stored)
        if not requested:
            return WindowResult(channel_id, pointer, (), (), (), (), False, ())

        fetched = await asyncio.wait_for(
            listener.client.get_messages(channel_id, ids=list(requested)), rpc_timeout
        )
        wanted = set(requested)
        current = {
            message.id: message
            for message in fetched
            if message is not None and message.id in wanted
        }
        expected = [v for v in stored if not v.deleted_by_window]
        absent = tuple(v.message_id for v in expected if v.message_id not in current)
        suspect = is_suspect(len(absent), len(expected))
        absences.seen(channel_id, current)

        edits: list[EditResult] = []
        failed: list[int] = []

        # Whatever came back -- even in a suspect read -- is a real version.
        for version in stored:
            message = current.get(version.message_id)
            if message is None:
                continue
            try:
                incoming = normalise_message(message, channel_id=channel_id)
                if not _worth_a_job(incoming, version):
                    continue  # unchanged since it was stored: no job at all
                edits.append(
                    await listener.ingest_edit(
                        incoming, channel_cfg=channel_cfg, detected_by=WINDOW_SOURCE
                    )
                )
            except db.WriterDeadError:
                raise
            except Exception as exc:  # noqa: BLE001 -- recorded; the rest of the window runs
                listener.record_handler_error(
                    channel_id, version.message_id, exc, source=WINDOW_SOURCE
                )
                failed.append(version.message_id)

        deletions: list[DeletionResult] = []
        if suspect:
            await _suspect_read(listener, channel_cfg, absences, absent, len(expected))
        else:
            if absences.leave_suspect(channel_id):
                logger.warning(
                    "window re-read of channel %s (%s) looks normal again", channel_id, channel_cfg.name
                )
            confirmed = absences.settle(channel_id, absent)
            reported_at = listener.now_utc()  # one report, one moment (R6-3)
            for message_id in sorted(confirmed):
                try:
                    deletions.append(
                        await listener.ingest_deletion(
                            channel_cfg,
                            message_id,
                            detected_by=WINDOW_SOURCE,
                            also_deleted=confirmed,
                            reported_at_utc=reported_at,
                        )
                    )
                except db.WriterDeadError:
                    raise
                except Exception as exc:  # noqa: BLE001 -- recorded; stays pending
                    listener.record_handler_error(channel_id, message_id, exc, source=WINDOW_SOURCE)
                    absences.keep_pending(channel_id, (message_id,))
                    failed.append(message_id)
            await _report_voids(listener, channel_cfg, deletions)
            newly_pending = sorted(absences.pending(channel_id))
            if newly_pending:
                logger.info(
                    "window re-read of channel %s (%s): %s missing; each is treated as deleted "
                    "only if the next read misses it too",
                    channel_id,
                    channel_cfg.name,
                    newly_pending,
                )

    result = WindowResult(
        channel_id=channel_id,
        pointer=pointer,
        requested=requested,
        edits=tuple(edits),
        deletions=tuple(deletions),
        pending=tuple(sorted(absences.pending(channel_id))),
        suspect=suspect,
        failed=tuple(failed),
    )
    if result.changed:
        logger.info(
            "window re-read of channel %s (%s), pointer %s: %d edit(s), %d deletion(s) "
            "applied",
            channel_id,
            channel_cfg.name,
            pointer,
            sum(1 for e in result.edits if e.changed),
            sum(1 for d in result.deletions if d.outcome not in ("unknown", "already_deleted")),
        )
    return result


async def _suspect_read(
    listener: Listener,
    channel_cfg: ChannelCfg,
    absences: AbsenceTracker,
    absent: tuple[int, ...],
    expected: int,
) -> None:
    channel_id = channel_cfg.channel_id
    logger.error(
        "window re-read of channel %s (%s) is missing %d of the %d messages it expected; "
        "not believed, so no deletion is inferred from it",
        channel_id,
        channel_cfg.name,
        len(absent),
        expected,
    )
    if not absences.enter_suspect(channel_id):
        return  # alerted when this run of suspect reads began
    shown = ", ".join(str(message_id) for message_id in absent[:10])
    more = f" and {len(absent) - 10} more" if len(absent) > 10 else ""
    try:
        # What is at stake if they really are gone (final fix FF13): a lone
        # signal deleted unseen trips this as surely as a bad answer.
        at_stake = edits_repo.waiting_trades(
            listener.reader, channel_id=channel_id, message_ids=absent
        )
        await listener.record_alert(
            "CRITICAL",
            edits_repo.WINDOW_READ_SUSPECT,
            f"this program re-read the newest messages of channel {channel_id} "
            f"({channel_cfg.name}), and Telegram did not return {len(absent)} of the "
            f"{expected} it expected (ids {shown}{more}). That looks like a bad answer "
            "rather than that many deletions, so none of them is taken as deleted while "
            "reads look like this; edits of the messages that did come back are still "
            "applied, and a deletion Telegram reports by itself still counts. "
            + "".join(f"{sentence} " for sentence in at_stake)
            + "If those messages really were deleted, check the channel by hand. Raised "
            "once until a read looks normal again.",
            channel_id=channel_id,
        )
    except db.WriterDeadError:
        raise
    except Exception as exc:  # noqa: BLE001 -- recorded; the error log above stands
        absences.leave_suspect(channel_id)  # so the next suspect read tries the alert again
        listener.record_handler_error(channel_id, None, exc, source=WINDOW_SOURCE)


async def _report_voids(
    listener: Listener, channel_cfg: ChannelCfg, deletions: list[DeletionResult]
) -> None:
    """The CRITICAL summary of one window read's voids (module docstring).
    A void that promoted the channel's standing repost of the call in its
    place is left out (group-3 leftover G3L-2): the call never stopped --
    it is queued as the promoted signal, and its own WARNINGs say so --
    and whether such a void happens at all depends only on which post of
    a relayed call was stored first (a copy's deletion is a repeat
    removal), so "check they really were deleted" would be noise that
    comes and goes with storage order. So is a void whose standing repost
    repeats another signal that already carried the same trade (final fix
    FF12): the call is still queued, or traded, as that signal."""
    channel_id = channel_cfg.channel_id
    for d in deletions:
        if d.outcome == "voided" and d.promoted_signal_id is not None:
            logger.info(
                "window re-read of channel %s (%s): signal %s voided, its call still queued "
                "as signal %s (a repost promoted in its place); not in the void summary",
                channel_id,
                channel_cfg.name,
                d.signal_id,
                d.promoted_signal_id,
            )
        elif d.outcome == "voided" and d.carried_by_signal_id is not None:
            logger.info(
                "window re-read of channel %s (%s): signal %s voided, its call still carried "
                "by signal %s (the same trade, which its standing repost repeats); not in the "
                "void summary",
                channel_id,
                channel_cfg.name,
                d.signal_id,
                d.carried_by_signal_id,
            )
    voided = [
        d.signal_id
        for d in deletions
        if d.outcome == "voided"
        and d.promoted_signal_id is None
        and d.carried_by_signal_id is None
    ]
    if not voided:
        return
    try:
        await listener.record_alert(
            "CRITICAL",
            edits_repo.WINDOW_VOIDED_SIGNALS,
            f"one window re-read of channel {channel_id} ({channel_cfg.name}) voided "
            f"{len(voided)} signal(s): {voided}. Each had stopped coming back in two reads "
            "in a row and was never executed. Check they really were deleted.",
            channel_id=channel_id,
        )
    except db.WriterDeadError:
        raise
    except Exception as exc:  # noqa: BLE001 -- recorded; each void has its own alert
        listener.record_handler_error(channel_id, None, exc, source=WINDOW_SOURCE)


def _worth_a_job(incoming: IncomingMessage, version: StoredVersion) -> bool:
    """Whether a message the read returned needs a writer job: it had
    been inferred deleted (reading it back reverses that), or it changed
    since it was stored -- a newer `edit_date`, or the same `edit_date`
    with different text (Telegram's `edit_date` has one-second
    resolution). A filter to spare the writer a job per message per
    round; `edits_repo` makes the real decision inside the job."""
    if version.deleted_by_window:
        return True
    fetched, stored = incoming.edit_date_utc, version.edit_date_utc
    if fetched is None:
        return False
    if stored is None or fetched > stored:
        return True
    return fetched == stored and incoming.text != version.raw_text
