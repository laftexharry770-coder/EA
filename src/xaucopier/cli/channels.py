"""`xaucopier channels` -- choose which Telegram channels signals come from.

- `channels` lists every channel in `config/channels.toml`: its number,
  name, ON or OFF, whether its numeric id has been looked up, its parser,
  when its newest recorded message was posted and how many signals it
  produced. **Read-only**: the store is opened with `db.connect_readonly()`
  (as `status` does), `channels.toml` is only read, and Telegram is never
  contacted.

**A channel's number is its `ordinal`**, never its position in the file.
The ordinal is explicit and unique (the Task 1 loader enforces both) and
it feeds the magic numbers that identify live broker positions, so it
must never change when the file is reordered -- and the number the user
sees in the list is the number the user types.

`watch` reads `channels.toml` once, when it starts. Whether it is running
now is answered by `session_in_use()`, which looks for the session mutex
without taking it, so asking never stops a `watch` from starting.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from xaucopier.cli import exit_codes, paths
from xaucopier.cli.resolve import PLACEHOLDER_ID
from xaucopier.cli.status import ago, when
from xaucopier.cli.watch import SetupError, load_channels_config
from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.client import session_in_use
from xaucopier.store import db, report

_CMD = paths.USER_COMMAND
#: What each registered parser reads, in the user's words.
PARSER_LAYOUTS: dict[str, str] = {"mmh": "the MARKET MASTERY HUB message layout"}
#: Told whenever a channel is added or described: a new channel is only
#: recorded, not traded, unless its posts look like MARKET MASTERY HUB's.
FORMAT_CAVEAT = (
    "The only parser built so far, mmh, understands only the MARKET MASTERY HUB "
    "message layout. A channel that writes its signals differently will have its "
    "messages recorded, but nothing will be traded from it until a parser is built "
    "for it from 5-10 of its real messages."
)
#: What to say while `watch` runs: it reads the list only when it starts.
WATCH_RUNNING_NOTE = (
    "watch is running now. It read this list when it started, so a change made "
    "here takes effect the next time watch starts: click in its window, press "
    f"Ctrl+C and answer Y, then run `{_CMD} watch` again."
)


@dataclass(frozen=True, slots=True)
class ChannelTotals:
    """What the store has recorded from one channel."""

    messages: int
    signals: int
    last_posted_at_utc: datetime | None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _printable(text: str) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in text)


def _write(text: str, *, err: bool = False) -> None:
    """Print `text`, replacing what the console's encoding cannot show (a
    channel name with an emoji, redirected to a file) rather than crashing."""
    stream = sys.stderr if err else sys.stdout
    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = stream.encoding or "ascii"
        stream.write(text.encode(encoding, "replace").decode(encoding))


def say(*lines: str) -> None:
    _write("\n".join(lines) + "\n")


def complain(*lines: str) -> None:
    _write("\n".join(lines) + "\n", err=True)


def by_number(channels: tuple[ChannelCfg, ...]) -> tuple[ChannelCfg, ...]:
    """The channels in the order the user sees them: by number (ordinal)."""
    return tuple(sorted(channels, key=lambda channel: channel.ordinal))


def read_totals(db_path: Path) -> tuple[dict[int, ChannelTotals], str | None]:
    """Per channel id, what the store has recorded -- read through a
    connection that cannot write -- and, if it could not be read, why."""
    try:
        conn = db.connect_readonly(db_path)
    except FileNotFoundError:
        return {}, f"Nothing has been recorded yet (there is no database at {db_path})."
    except db.SchemaVersionError as exc:
        return {}, (
            f"Recorded messages are not shown: {exc}. Run `{_CMD} watch` once to bring "
            "it up to date."
        )
    except sqlite3.Error as exc:
        return {}, f"Recorded messages are not shown: {db_path} could not be read ({exc})."
    try:
        activity = report.channel_activity(conn)
        signals = dict(report.signals_per_channel(conn))
    except sqlite3.Error as exc:
        return {}, f"Recorded messages are not shown: {db_path} could not be read ({exc})."
    finally:
        conn.close()
    return {
        item.channel_id: ChannelTotals(
            messages=item.messages,
            signals=signals.get(item.channel_id, 0),
            last_posted_at_utc=item.last_posted_at_utc,
        )
        for item in activity
    }, None


def _parser_text(name: str) -> str:
    layout = PARSER_LAYOUTS.get(name)
    return f"{name} (reads {layout})" if layout else name


def render_list(
    channels: tuple[ChannelCfg, ...],
    totals: dict[int, ChannelTotals],
    *,
    channels_path: Path,
    db_problem: str | None,
    watch_running: bool,
    now: datetime,
) -> str:
    """The whole `channels` listing as text."""
    lines = [f"Channels signals are taken from ({channels_path}):", ""]
    if not channels:
        lines += ["  none yet", ""]
    for channel in by_number(channels):
        switch = "ON" if channel.enabled else "OFF"
        lines.append(f"  {channel.ordinal}. {_printable(channel.name)} -- {switch}")
        if channel.channel_id == PLACEHOLDER_ID:
            lines.append(
                "     id: NOT LOOKED UP YET, so it cannot be switched on -- run "
                f"`{_CMD} resolve-channels`"
            )
        else:
            same = [
                other.ordinal
                for other in channels
                if other.channel_id == channel.channel_id and other is not channel
            ]
            also = (
                f" -- the same channel as number {', '.join(map(str, same))}" if same else ""
            )
            lines.append(f"     id: {channel.channel_id} (looked up){also}")
        lines.append(f"     parser: {_parser_text(channel.parser)}")
        item = totals.get(channel.channel_id) if channel.channel_id != PLACEHOLDER_ID else None
        if item is None or item.last_posted_at_utc is None:
            lines.append("     last message recorded: none | signals: 0")
        else:
            lines.append(
                f"     last message recorded: posted {when(item.last_posted_at_utc)} "
                f"({ago(now, item.last_posted_at_utc)}) | messages: {item.messages} | "
                f"signals: {item.signals}"
            )
        lines.append("")

    on = sum(1 for channel in channels if channel.enabled)
    if db_problem is not None:
        lines.append(db_problem)
    if on:
        lines.append(f"{on} of {len(channels)} channel(s) ON.")
    elif any(channel.channel_id != PLACEHOLDER_ID for channel in channels):
        lines.append(
            f"No channel is ON, so `{_CMD} watch` will refuse to start. Switch one on "
            f"with `{_CMD} channels on NUMBER`."
        )
    else:
        lines.append(
            f"No channel is ON, so `{_CMD} watch` will refuse to start. A channel needs "
            f"its id first: run `{_CMD} resolve-channels` for one listed here, or "
            f"`{_CMD} channels add` to add a new one."
        )
    if watch_running:
        lines.append(WATCH_RUNNING_NOTE)
    lines += [
        "",
        f"To switch a channel on or off: `{_CMD} channels on NUMBER` or "
        f"`{_CMD} channels off NUMBER`",
        f"To add a channel: `{_CMD} channels add`",
    ]
    return "\n".join(lines) + "\n"


def _config_problem(exc: SetupError) -> int:
    complain(
        f"channels.toml cannot be used: {exc}",
        f"`{_CMD} watch` will refuse to start until this is fixed. Nothing was changed.",
    )
    return exit_codes.EXIT_CONFIG_ERROR


def cmd_list(args: argparse.Namespace) -> int:
    try:
        channels = load_channels_config(args.config_dir)
    except SetupError as exc:
        return _config_problem(exc)
    totals, db_problem = read_totals(args.db)
    _write(
        render_list(
            channels,
            totals,
            channels_path=args.config_dir / paths.CHANNELS_FILE,
            db_problem=db_problem,
            watch_running=session_in_use(),
            now=_utcnow(),
        )
    )
    return exit_codes.EXIT_OK


def cmd_channels(args: argparse.Namespace) -> int:
    return cmd_list(args)
