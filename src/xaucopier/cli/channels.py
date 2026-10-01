"""`xaucopier channels` -- choose which Telegram channels signals come from.

- `channels` lists every channel in `config/channels.toml`: its number,
  name, ON or OFF, whether its numeric id has been looked up, its parser,
  when its newest recorded message was posted and how many signals it
  produced. **Read-only**: the store is opened with `db.connect_readonly()`
  (as `status` does), `channels.toml` is only read, and Telegram is never
  contacted.
- `channels on N` / `channels off N` switch channel number N on or off.
  Neither contacts Telegram. `on` refuses a channel whose id has not been
  looked up (`channel_id = 0`) and one whose parser xaucopier does not
  have; `off` of the last channel still on is allowed, with a warning that
  `watch` will then refuse to start.
- **OFF means not read at all** (ruling F15): not its new posts, and not
  the edits, deletions and follow-ups of the signals it already posted.
  `off` says how many of its signals are still waiting or already traded
  (`report.signals_in_play`); `watch` cancels the waiting entries at its
  next start. **And posts made while it was off are never read** (ruling
  M12, F28): `on` of a channel read before writes a seed cutoff of now
  (`reseed_from_now`), so the next `watch` starts reading it from there
  instead of catching up on everything posted meanwhile. `channels add`
  does the same for a channel read before, removed and added back
  (G2L-3).
- `channels add` adds a NEW channel: the only command here that contacts
  Telegram, once, to look the channel up -- by the same paste-a-link or
  pick-from-the-channels-you-joined flow `resolve-channels` uses. It takes
  the session mutex first, exactly as `resolve-channels` does, so it
  refuses to run while `watch` holds the session. The new block gets the
  channel's numeric id and title, `parser = "mmh"`, the next free number
  (one more than the highest ordinal -- never derived from how many
  channels there are) and `enabled = true`. A channel already in the file
  is never added twice.

**Every write goes through `resolve.write_config_atomically()`**, the one
writer `resolve-channels` also uses: the edited text is written to a
temporary file, flushed to disk, parsed and compared with the original
(only the intended change may differ), loaded through the real loader,
checked against the file on disk once more, and only then `os.replace()`d
over `channels.toml`. The file is always exactly the old one or the fully
checked new one, with every other channel, comment and line ending kept.

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
import asyncio
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from xaucopier.cli import exit_codes, paths
from xaucopier.cli.resolve import (
    PLACEHOLDER_ID,
    TELEGRAM_TROUBLE,
    ChangedMeanwhileError,
    ResolveError,
    _ask,
    _choose,
    accept_source,
    build_one_shot_client,
    connect_one_shot,
    found_line,
    logged_in,
    report_already_running,
    report_not_logged_in,
    report_permanent_error,
    report_telegram_trouble,
    save_added,
    save_failure_code,
    save_switch,
)
from xaucopier.cli.status import ago, database_problem, when
from xaucopier.cli.watch import SetupError, load_channels_config, load_session
from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.client import (
    PERMANENT_TELEGRAM_ERRORS,
    AlreadyRunningError,
    acquire_single_instance_lock,
    session_in_use,
)
from xaucopier.parsing.registry import parser_for
from xaucopier.store import channel_state, db, report

_CMD = paths.USER_COMMAND
#: What each registered parser reads, in the user's words.
PARSER_LAYOUTS: dict[str, str] = {"mmh": "the MARKET MASTERY HUB message layout"}
#: Told whenever a channel is added: what the one parser reads, and what
#: happens to everything else it posts -- exactly what ingest does (final
#: review F07, group-4 leftover G4L-1): a signal in another layout is
#: recorded, never traded; an instruction for an open trade is recorded as
#: one, whatever the layout; unreadable signals in a row raise an alert.
FORMAT_CAVEAT = (
    "The only parser built so far, mmh, only reads trade signals written in the MARKET "
    "MASTERY HUB layout. From this channel, a signal written any other way is recorded "
    "but NOT traded, until a parser is built for it from 5-10 of its real messages. A "
    "message that reads like an instruction for a trade already open (for example "
    "'close all' or 'move SL to entry') is recorded as an instruction, whatever its "
    f"layout. When 3 messages in a row look like signals but cannot be read, `{_CMD} "
    "status` shows a TEMPLATE_CHANGED_SUSPECTED alert: the channel changed its layout, or "
    "it writes some calls in a layout this program does not read."
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
        try:
            activity = report.channel_activity(conn)
            signals = dict(report.signals_per_channel(conn))
        finally:
            conn.close()
    except FileNotFoundError:
        return {}, f"Nothing has been recorded yet (there is no database at {db_path})."
    except sqlite3.DatabaseError as exc:
        return {}, f"Recorded messages are not shown. {database_problem(db_path, exc)}"
    except sqlite3.Error as exc:
        return {}, f"Recorded messages are not shown: {db_path} could not be read ({exc})."
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
            # One Telegram id is never listed twice: the loader refuses such a
            # file (final review F40), so there is no "same channel as" here.
            lines.append(f"     Telegram id: {channel.channel_id} (looked up)")
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


def parser_is_built(name: object) -> bool:
    """Whether `name` is a parser xaucopier has (`parsing/registry.py`)."""
    if not isinstance(name, str):
        return False
    try:
        parser_for(name)
    except KeyError:
        return False
    return True


def _raw_channel(text: str, number: int) -> dict[str, Any] | None:
    """Channel `number`'s own table, read leniently from the text -- so `on`
    can say exactly what is wrong with that channel even where the loader
    would reject the file as a whole. None if it cannot be found."""
    try:
        entries = tomllib.loads(text).get("channels")
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ordinal = entry.get("ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal == number:
            return entry
    return None


def reseed_from_now(db_path: Path, channel_id: int, now: datetime) -> bool:
    """Ask the next `watch` to read channel `channel_id` again from `now`
    -- whole seconds, UTC -- instead of from where it stopped, if it was
    read before (a `channel_state` row with a pointer or the seeded
    marker): `channel_state.request_reseed`, in one short transaction.
    For `channels on` (F28, ruling M12) and `channels add` of a channel
    read before (G2L-3): posts made while it was off, or not in the list,
    are never read. Returns whether a cutoff was written.

    No database yet, or a channel never read: nothing to write, since its
    first read seeds it at that watch's start anyway. A database another
    program holds past SQLite's wait (10 s), one from a newer version, or
    a file that is not a database raises `sqlite3.DatabaseError`, and
    nothing is written -- the caller then changes nothing either."""
    if not db_path.is_file():
        return False
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            state = channel_state.read(conn, channel_id)
            if not (state.seeded or state.pointer > 0):
                conn.rollback()
                return False
            channel_state.request_reseed(conn, channel_id=channel_id, cutoff_utc=now)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return True
    finally:
        conn.close()


def _off_lines(db_path: Path, cfg: ChannelCfg) -> list[str]:
    """What `channels off` does to the channel and to the signals it
    already posted, in plain words (final review F15)."""
    lines = [
        "While it is off, this program does not read it at all: what it posts then is "
        "not recorded, and not read later either, even after you switch it back on."
    ]
    try:
        if db_path.is_file():
            conn = db.connect_readonly(db_path)
            try:
                waiting, traded = report.signals_in_play(conn, cfg.channel_id)
            finally:
                conn.close()
        else:
            waiting = traded = 0
    except sqlite3.DatabaseError as exc:
        lines.append(f"Its signals could not be counted. {database_problem(db_path, exc)}")
        return lines
    if not waiting and not traded:
        lines.append("None of its signals is waiting to be traded or already traded.")
        return lines
    lines.append(
        f"{waiting} of its signals {'is' if waiting == 1 else 'are'} still waiting to be "
        f"traded, and {traded} {'has' if traded == 1 else 'have'} already been traded. "
        "Their edits, deletions and follow-ups (like 'close all') are no longer read. "
        f"The waiting ones are cancelled the next time `{_CMD} watch` starts; a trade "
        "already open keeps its stop loss and take profit."
    )
    return lines


def _takes_effect() -> str:
    if session_in_use():
        return WATCH_RUNNING_NOTE
    return (
        "watch reads this list when it starts, so this takes effect the next time "
        f"you run `{_CMD} watch`."
    )


def cmd_switch(args: argparse.Namespace, *, enabled: bool) -> int:
    """`channels on N` (`enabled`) or `channels off N`. Never contacts Telegram."""
    number: int = args.number
    path: Path = args.config_dir / paths.CHANNELS_FILE
    word = "ON" if enabled else "OFF"
    try:
        original = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return _config_problem(SetupError(f"{path} does not exist."))
    except (OSError, UnicodeDecodeError) as exc:
        return _config_problem(SetupError(f"{path} could not be read: {exc}"))

    if enabled:
        raw = _raw_channel(original, number)
        if raw is not None and not parser_is_built(raw.get("parser")):
            complain(
                f"Channel {number} ({_printable(str(raw.get('name')))}) was NOT switched on: "
                f"it is set to parser {raw.get('parser')!r}, which xaucopier does not have. "
                "Nothing was changed.",
                f"The only parser built so far is mmh ({PARSER_LAYOUTS['mmh']}). Set that "
                f'channel\'s parser back to "mmh" in {path}, or remove the channel; until '
                f"then `{_CMD} watch` will refuse to start.",
            )
            return exit_codes.EXIT_FAILED

    try:
        channels = load_channels_config(args.config_dir)
    except SetupError as exc:
        return _config_problem(exc)
    found = [(index, cfg) for index, cfg in enumerate(channels) if cfg.ordinal == number]
    if not found:
        complain(
            f"There is no channel number {number}. Nothing was changed. See the numbers "
            f"with `{_CMD} channels`."
        )
        return exit_codes.EXIT_FAILED
    index, cfg = found[0]
    name = _printable(cfg.name)

    if cfg.enabled == enabled:
        say(f"Channel {number} ({name}) is already {word}. Nothing was changed.")
        return exit_codes.EXIT_OK
    if enabled and cfg.channel_id == PLACEHOLDER_ID:
        complain(
            f"Channel {number} ({name}) was NOT switched on: its Telegram id has not been "
            "looked up yet (channel_id is still 0). Nothing was changed.",
            f"Run `{_CMD} resolve-channels` to look it up (that also switches it on), or "
            f"add the channel with `{_CMD} channels add`.",
        )
        return exit_codes.EXIT_FAILED
    # No second block with this Telegram id can be ON already: the loader
    # refuses a file that lists one id twice (final review F40).
    reseeded = False
    if enabled:
        # The cutoff first, then the switch: if the save below fails, the
        # channel stays OFF -- never read -- so the cutoff does nothing and
        # the next `channels on` writes its own. The other order could
        # leave it ON with no cutoff, and the next watch would read
        # everything posted while it was off as new (F28).
        try:
            reseeded = reseed_from_now(args.db, cfg.channel_id, _utcnow())
        except sqlite3.DatabaseError as exc:
            complain(
                database_problem(args.db, exc),
                f"Nothing was changed: channel {number} is still OFF.",
            )
            return exit_codes.EXIT_FAILED

    try:
        save_switch(
            path,
            original=original,
            index=index,
            ordinal=number,
            enabled=enabled,
            today=_utcnow().date().isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 -- the writer leaves the file untouched on any failure
        code = save_failure_code(exc)
        if isinstance(exc, ChangedMeanwhileError):
            outcome = (
                f"Nothing was written by this command. See the channels as they are now "
                f"with `{_CMD} channels`, then run the command again."
            )
        else:
            outcome = (
                f"The file was not changed: channel {number} is still "
                f"{'OFF' if enabled else 'ON'}."
                + (" Run the command again." if code == exit_codes.EXIT_FAILED else "")
            )
        complain(f"Could not save the change to {path}: {exc}", outcome)
        return code

    say(f"Channel {number} ({name}) is now {word}.")
    if reseeded:
        say(
            "Messages posted while it was off are not read: the next watch reads it "
            "from now on."
        )
    if not enabled:
        say(*_off_lines(args.db, cfg))
    if not enabled and not any(other.enabled for other in channels if other is not cfg):
        say(
            "WARNING: that was the last channel switched on. With no channel ON, "
            f"`{_CMD} watch` will refuse to start (exit code {exit_codes.EXIT_CONFIG_ERROR}) "
            f"until you switch one on with `{_CMD} channels on NUMBER`."
        )
    say(_takes_effect())
    return exit_codes.EXIT_OK


#: What `channels add` says when `watch` (or another command) holds the session.
ADD_WHILE_WATCH_RUNS = (
    "watch is running (or another xaucopier command is using the Telegram login). "
    "Only one program may use the Telegram session at a time, so a channel cannot "
    "be added while it runs. Nothing was changed.\n"
    "Stop watch first -- click in its window, press Ctrl+C and answer Y -- then run "
    f"`{_CMD} channels add` again, and start `{_CMD} watch` again afterwards."
)


def _next_number(channels: tuple[ChannelCfg, ...]) -> int:
    """The number (ordinal) a new channel gets: one more than the highest in
    use -- never how many channels there are, which could hand out a
    number another channel already has."""
    return max((channel.ordinal for channel in channels), default=0) + 1


async def _add(args: argparse.Namespace, client: Any, path: Path) -> int:
    """Look the channel up on Telegram, then -- offline -- check it and
    append it. The Telegram connection is closed before the file is read."""
    try:
        # Bounded: with no network Telethon's connect never gives up (F24).
        await connect_one_shot(client)
        if not await logged_in(client):
            return report_not_logged_in()
        try:
            resolved = await _choose(client, "the channel to add", args.link, again="channels add")
        except ResolveError as exc:
            complain(f"Could not find the channel: {exc}. Nothing was added.")
            return exit_codes.EXIT_FAILED
    except PERMANENT_TELEGRAM_ERRORS as exc:
        return report_permanent_error(exc)
    except TELEGRAM_TROUBLE as exc:  # no connection, a wait, Telegram's trouble (F23)
        return report_telegram_trouble(exc, again="channels add", nothing="Nothing was added.")
    finally:
        await client.disconnect()
    if resolved is None:
        say("Cancelled. Nothing was added.")
        return exit_codes.EXIT_FAILED
    title = _printable(resolved.title)
    say(found_line(resolved))

    # Read the file now, not before the lookup: the user may have taken a
    # while to choose. The writer also refuses if it changes after this.
    try:
        original = path.read_bytes().decode("utf-8")
        channels = load_channels_config(args.config_dir)
    except SetupError as exc:
        return _config_problem(exc)
    except (OSError, UnicodeDecodeError) as exc:
        return _config_problem(SetupError(f"{path} could not be read: {exc}"))

    existing = next(
        (channel for channel in by_number(channels) if channel.channel_id == resolved.channel_id),
        None,
    )
    if existing is not None:
        state = "ON" if existing.enabled else "OFF"
        complain(
            f"{title} is already in channels.toml as channel {existing.ordinal} "
            f"({_printable(existing.name)}), and it is {state}. Nothing was added."
        )
        if not existing.enabled:
            complain(f"To switch it on: `{_CMD} channels on {existing.ordinal}`")
        return exit_codes.EXIT_FAILED
    waiting = next(
        (
            channel
            for channel in by_number(channels)
            if channel.channel_id == PLACEHOLDER_ID
            and channel.name.strip().casefold() == resolved.title.strip().casefold()
        ),
        None,
    )
    if waiting is not None:
        complain(
            f"{title} is already in channels.toml as channel {waiting.ordinal}, waiting "
            f"for its id. Nothing was added: run `{_CMD} resolve-channels` to look up "
            "its id and switch it on."
        )
        return exit_codes.EXIT_FAILED

    if not await accept_source(resolved, allow_group=args.allow_group):
        say("Not added. Nothing was changed.")
        return exit_codes.EXIT_FAILED
    number = _next_number(channels)
    say("", f"PLEASE NOTE: {FORMAT_CAVEAT}", "")
    if not args.yes:
        answer = await _ask(
            f"Add {title} as channel {number} and switch it on? Type yes to confirm: "
        )
        if answer.lower() not in ("y", "yes"):
            say("Not added. Nothing was changed.")
            return exit_codes.EXIT_FAILED
    # A channel read before, removed and now added back, is read from now
    # on -- as `channels on` does it, and in the same order (G2L-3).
    try:
        reseed_from_now(args.db, resolved.channel_id, _utcnow())
    except sqlite3.DatabaseError as exc:
        complain(database_problem(args.db, exc), "Nothing was added.")
        return exit_codes.EXIT_FAILED
    try:
        save_added(
            path,
            original=original,
            resolved=resolved,
            ordinal=number,
            today=_utcnow().date().isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 -- the writer leaves the file untouched on any failure
        code = save_failure_code(exc)
        again = f"run `{_CMD} channels add` again."
        if isinstance(exc, ChangedMeanwhileError):
            outcome = f"Nothing was added by this command. Check with `{_CMD} channels`, then {again}"
        else:
            outcome = "The file was not changed: nothing was added." + (
                f" Please {again}" if code == exit_codes.EXIT_FAILED else ""
            )
        complain(f"Could not save to {path}: {exc}", outcome)
        return code
    say(
        f"Added: {title} is now channel {number}, switched ON (parser mmh).",
        f"It takes effect the next time you run `{_CMD} watch`. See every channel with "
        f"`{_CMD} channels`; switch this one off with `{_CMD} channels off {number}`.",
    )
    return exit_codes.EXIT_OK


def cmd_add(args: argparse.Namespace) -> int:
    """`channels add`: the one `channels` command that contacts Telegram."""
    path: Path = args.config_dir / paths.CHANNELS_FILE
    try:
        load_channels_config(args.config_dir)  # usable, before anything connects
    except SetupError as exc:
        return _config_problem(exc)
    try:
        blob = load_session(args.secrets)
    except SetupError as exc:
        complain(f"channels add cannot start: {exc}", "Nothing was changed.")
        return exit_codes.EXIT_CONFIG_ERROR
    try:
        lock = acquire_single_instance_lock()
    except AlreadyRunningError as exc:
        return report_already_running(ADD_WHILE_WATCH_RUNS, exc)
    try:
        client = build_one_shot_client(blob)
        return asyncio.run(_add(args, client, path))
    except KeyboardInterrupt:
        complain(
            "",
            "Cancelled (Ctrl+C). Nothing was added unless it said Added above; check with "
            f"`{_CMD} channels`.",
        )
        return exit_codes.EXIT_INTERRUPTED
    except EOFError:
        complain(
            "No answer could be read: the input was closed. Run "
            f"`{_CMD} channels add` in an ordinary PowerShell window and type the answers "
            "there. Nothing was added."
        )
        return exit_codes.EXIT_FAILED
    finally:
        lock.release()


_ADD_ONLY = f"--link, --yes and --allow-group only go with `{_CMD} channels add`."

_ON_OFF_USAGE = (
    "Say which channel, by the number `{cmd} channels` shows: "
    "`{cmd} channels on NUMBER` or `{cmd} channels off NUMBER`."
).format(cmd=_CMD)


def cmd_channels(args: argparse.Namespace) -> int:
    """`channels`, `channels on N`, `channels off N` or `channels add`."""
    if args.action in ("on", "off"):
        if args.number is None:
            complain(_ON_OFF_USAGE)
            return exit_codes.EXIT_USAGE
        if args.link is not None or args.yes or args.allow_group:
            complain(_ADD_ONLY)
            return exit_codes.EXIT_USAGE
        return cmd_switch(args, enabled=args.action == "on")
    if args.number is not None:
        complain(
            f"A number only goes with on or off: `{_CMD} channels on {args.number}` or "
            f"`{_CMD} channels off {args.number}`."
        )
        return exit_codes.EXIT_USAGE
    if args.action == "add":
        return cmd_add(args)
    if args.link is not None or args.yes or args.allow_group:
        complain(_ADD_ONLY)
        return exit_codes.EXIT_USAGE
    return cmd_list(args)
