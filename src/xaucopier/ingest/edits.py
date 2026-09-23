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
2. reads the pointer and picks the **window**: the `window` newest stored,
   not-yet-deleted messages at or below it (`edits_repo.window_versions`,
   one indexed read on the reader connection). Messages above the
   pointer belong to the forward read, which meets their current version
   anyway when it passes them;
3. fetches them by id in one bounded RPC -- `get_messages(channel,
   ids=[...])` under `asyncio.wait_for(..., rpc_timeout)` -- which
   returns each message's *current* version, or `None` for one that is
   gone;
4. hands each message whose `edit_date` is newer than the stored one to
   `Listener.ingest_edit` (`detected_by="window"`), and each id that came
   back missing -- a deletion found by **absence** -- to
   `Listener.ingest_deletion`.

What an edit or deletion then *does* is `store/edits_repo.py`'s policy,
decided in one writer job against the stored row, so a change the event
already delivered is a no-op here, and vice versa.

**It never moves the pointer.** It writes only through `ingest_edit` /
`ingest_deletion`, neither of which touches `channel_state`, and it only
reads ids at or below the pointer. The pointer is `read_history`'s alone:
a read that advanced it past an id it had not ingested would lose that
message for good (the reviewer's D1).

**Matched by id, never by position.** Telethon documents that Telegram
may leave an invalid id out of its answer altogether instead of sending
`MessageEmpty` for it (telethon/client/messages.py:324-327), so the list
need not zip one-to-one with the ids asked for. A message counts as
present only if a returned message carries its id.

**Failures.** The RPC's errors -- a flood wait, the `rpc_timeout`, a
dropped connection -- propagate to the poll, which handles them exactly
as it handles the forward read's (per channel, retried next round). One
message whose job fails is recorded in `handler_errors` (`source=
"window"`) and the rest of the window still runs; it is retried next
round. `WriterDeadError` always propagates: with no writer nothing can be
stored, and the listener must stop.

No `sqlite3` here and no `MetaTrader5` (tests enforce both).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.listener import Listener, normalise_message
from xaucopier.store import db, edits_repo
from xaucopier.store.edits_repo import DeletionResult, EditResult

logger = logging.getLogger(__name__)

#: How many recent stored messages per channel the poll re-reads by id
#: each round.
DEFAULT_EDIT_WINDOW = 50
#: The most a window may hold: Telethon sends one `GetMessagesRequest`
#: per 100 ids (`_MAX_CHUNK_SIZE`, telethon/client/messages.py:10) and
#: waits 10 s between requests past 300, so up to 100 keeps each re-read
#: to exactly one RPC.
MAX_EDIT_WINDOW = 100

#: `handler_errors` source, and `edits_repo` detector, for this path.
WINDOW_SOURCE = edits_repo.WINDOW_DETECTOR


@dataclass(frozen=True, slots=True)
class WindowResult:
    """What one window re-read of one channel found."""

    channel_id: int
    #: The pointer the window was taken below (read, never written).
    pointer: int
    #: The ids asked for, oldest first; empty if there was nothing to ask.
    requested: tuple[int, ...]
    edits: tuple[EditResult, ...]
    deletions: tuple[DeletionResult, ...]
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


async def reconcile_window(
    listener: Listener,
    channel_cfg: ChannelCfg,
    *,
    window: int,
    rpc_timeout: float,
) -> WindowResult:
    """Re-read `channel_cfg`'s recent window by id and apply every edit
    and deletion found (see the module docstring). `rpc_timeout` is
    required and bounds the one RPC; its `TimeoutError`, like any RPC
    error, propagates to the caller."""
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
            listener.reader, channel_id=channel_id, at_or_below=pointer, limit=window
        )
        requested = tuple(version.message_id for version in stored)
        if not requested:
            return WindowResult(channel_id, pointer, (), (), (), ())

        fetched = await asyncio.wait_for(
            listener.client.get_messages(channel_id, ids=list(requested)), rpc_timeout
        )
        wanted = set(requested)
        current = {
            message.id: message
            for message in fetched
            if message is not None and message.id in wanted
        }

        edits: list[EditResult] = []
        deletions: list[DeletionResult] = []
        failed: list[int] = []
        for version in stored:
            message = current.get(version.message_id)
            try:
                if message is None:
                    deletions.append(
                        await listener.ingest_deletion(
                            channel_cfg, version.message_id, detected_by=WINDOW_SOURCE
                        )
                    )
                    continue
                incoming = normalise_message(message, channel_id=channel_id)
                if not _newer(incoming.edit_date_utc, version.edit_date_utc):
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

    result = WindowResult(
        channel_id=channel_id,
        pointer=pointer,
        requested=requested,
        edits=tuple(edits),
        deletions=tuple(deletions),
        failed=tuple(failed),
    )
    if result.changed:
        logger.info(
            "window re-read of channel %s (%s) at or below %s: %d edit(s), %d deletion(s) "
            "applied",
            channel_id,
            channel_cfg.name,
            pointer,
            sum(1 for e in result.edits if e.changed),
            sum(1 for d in result.deletions if d.outcome not in ("unknown", "already_deleted")),
        )
    return result


def _newer(fetched: datetime | None, stored: datetime | None) -> bool:
    """Whether the fetched version is newer than the stored one. Only an
    `edit_date` can make it so (a message's content changes only by an
    edit). This is a filter to spare the writer a job per message per
    round; `edits_repo` makes the real decision inside the job."""
    if fetched is None:
        return False
    return stored is None or fetched > stored
