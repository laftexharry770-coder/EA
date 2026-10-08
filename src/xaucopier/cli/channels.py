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
  next start. **And what it posts while it is off is never read** (ruling
  M12, F28; fix round 2): `off` records when it was switched off and `on`
  when it came back (`cli/channel_reading.py`), and the next `watch`
  reads what it posted before the switch-off, skips the OFF period, and
  reads on from the switch-on. `channels add` and `resolve-channels` of a
  channel read before, removed and added back, have it read from now on
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

from xaucopier.cli import channel_reading, exit_codes, paths
from xaucopier.cli.resolve import (
    PLACEHOLDER_ID,
    READ_FROM_NOW,
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
from xaucopier.cli.database_words import after_colon
from xaucopier.cli.status import ago, database_problem, when
from xaucopier.cli.watch import (
    ConfigReadBusyError,
    SetupError,
    load_channels_config,
    load_session,
)
from xaucopier.config.loader import read_while_replaced
from xaucopier.config.channels import ChannelCfg
from xaucopier.config.loader import ConfigError, load_policy_dict
from xaucopier.domain.market import Policy
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
    if isinstance(exc, ConfigReadBusyError):
        # Not the file's fault: another command was saving it (FF9).
        complain(f"{exc}.", f"Nothing was changed. Run the command again. {exc.KEEPS_HAPPENING}")
        return exit_codes.EXIT_FAILED
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


def _utc_text(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _on_lines(result: channel_state.SwitchOn | None) -> list[str]:
    """What `channels on` did to where the next watch reads the channel
    (fix round 2 of F28), in plain words. Nothing to say for a channel
    never read, or with no database yet: its first read starts at that
    watch's start, as for any new channel."""
    if result is None or result.outcome == "never_read":
        return []
    if result.outcome in ("period_closed", "period_extended"):
        off = f" ({_utc_text(result.off_at_utc)} UTC)" if result.off_at_utc is not None else ""
        return [
            f"The next `{_CMD} watch` reads what it posted until you switched it off{off} "
            "that watch had not read yet, skips what it posted while it was off, and reads "
            "everything it posts from now on."
        ]
    if result.outcome == "reseeded":
        return [
            "When it was switched off was not recorded, so the next watch reads it from now "
            "on: everything it posted since watch last read it is skipped, not only what it "
            "posted while it was off."
        ]
    # "first_read_kept"
    return [
        "When it was switched off was not recorded, and watch is running and has not "
        "finished its first read of this channel, so what it posted while it was off may "
        "still be read."
    ]


def max_signal_age_seconds(config_dir: Path) -> int:
    """`max_signal_age_seconds` from `policy.toml` beside channels.toml: how
    old a signal may be and still be traded. The built-in default (120 s,
    `domain.market.Policy`) when the file is missing, unreadable, or does
    not hold a whole number above 0 there."""
    try:
        value = load_policy_dict(config_dir / paths.POLICY_FILE).get("max_signal_age_seconds")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ConfigError):
        value = None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return Policy().max_signal_age_seconds


def _is_are(count: int) -> str:
    return "is" if count == 1 else "are"


def _off_lines(
    db_path: Path,
    cfg: ChannelCfg,
    *,
    max_age: int,
    recorded: bool,
    problem: str | None,
) -> list[str]:
    """What `channels off` does to the channel and to the signals it
    already posted, in plain words (final review F15). A waiting entry
    whose signal is older than `max_age` seconds is told apart: it can
    never be traded. `recorded`: when it was switched off was saved, so
    what it posted before is still read once it is back on; `problem`:
    that it could not be saved, and why (fix round 2 of F28;
    `_off_not_saved`)."""
    if session_in_use():
        # watch read the list when it started: it keeps reading this
        # channel, and recording what it posts, until restarted (FF24).
        lines = [
            "watch is running and keeps reading this channel until you restart it, and "
            "records what it posts until then. From the restart until you switch it back "
            "on, this program does not read it at all: what it posts then is skipped for "
            "good."
        ]
    elif recorded:
        lines = [
            "While it is off, this program does not read it at all.",
            "What it posts from now until you switch it back on is never read, not even "
            "later. What it posted before now that watch has not read yet is still read "
            "once it is back on.",
        ]
    else:
        lines = [
            "While it is off, this program does not read it at all.",
            "What it posts while it is off is never read, not even later.",
        ]
    if problem is not None:
        lines.append(problem)
    try:
        if db_path.is_file():
            conn = db.connect_readonly(db_path)
            try:
                counts = report.signals_in_play(
                    conn, cfg.channel_id, now_utc=_utcnow(), max_age_seconds=max_age
                )
            finally:
                conn.close()
        else:
            counts = report.SignalsInPlay(waiting=0, too_old=0, traded=0)
    except sqlite3.DatabaseError as exc:
        lines.append(
            "Its signals could not be counted either."
            if problem is not None
            else f"Its signals could not be counted. {database_problem(db_path, exc)}"
        )
        return lines
    waiting, too_old, traded = counts.waiting, counts.too_old, counts.traded
    if not waiting and not too_old and not traded:
        lines.append("None of its signals is waiting to be traded or already traded.")
        return lines
    old = (
        f"{too_old} {_is_are(too_old)} too old to be traded (posted more than {max_age} "
        "seconds ago), "
        if too_old
        else ""
    )
    lines.append(
        f"{waiting} of its signals {_is_are(waiting)} still waiting to be traded, {old}"
        f"and {traded} {'has' if traded == 1 else 'have'} already been traded. "
        "Their edits, deletions and follow-ups (like 'close all') are no longer read. "
        f"The waiting ones are cancelled the next time `{_CMD} watch` starts -- the same "
        "call posted by a channel that is switched on is still traded; a trade already "
        "open keeps its stop loss and take profit."
    )
    return lines


def _off_not_saved(db_path: Path, exc: sqlite3.DatabaseError, number: int) -> str:
    """Why `channels off NUMBER` could not save when the channel was
    switched off, and what that costs. A busy database is worth running
    the command again for -- `off` on a channel already OFF saves a
    switch-off never saved (final fix FF11) -- so it says exactly that
    command; for anything else it would not help."""
    cost = "everything it posted since watch last read it is skipped too"
    if db.is_lock_error(exc):
        return (
            f"When it was switched off could not be saved: the database is busy -- watch "
            f"is probably writing to it ({db_path}). Run `{_CMD} channels off {number}` "
            f"again in a moment to save it; until then, when you switch it back on, {cost}."
        )
    problem = after_colon(database_problem(db_path, exc))
    return (
        f"When it was switched off could not be saved: {problem} "
        f"So when you switch it back on, {cost}."
    )


def _already_off(args: argparse.Namespace, cfg: ChannelCfg, number: int, name: str) -> int:
    """`channels off NUMBER` for a channel already OFF: nothing changes in
    channels.toml, but a switch-off whose time was never saved is saved now
    (final fix FF11), and said."""
    try:
        saved = channel_reading.record_missing_switch_off(args.db, cfg.channel_id, _utcnow())
    except sqlite3.DatabaseError as exc:
        say(
            f"Channel {number} ({name}) is already OFF. Nothing was changed.",
            _off_not_saved(args.db, exc, number).replace(
                "When it was switched off could not be saved",
                "Whether its switch-off was saved could not be checked",
                1,
            ),
        )
        return exit_codes.EXIT_OK
    if not saved:
        say(f"Channel {number} ({name}) is already OFF. Nothing was changed.")
        return exit_codes.EXIT_OK
    say(
        f"Channel {number} ({name}) is already OFF.",
        "When it was switched off had not been saved; it is saved now, as now. So when you "
        "switch it back on, what it posted before now that watch has not read yet is still "
        "read, and only what it posts from now until then is skipped.",
    )
    return exit_codes.EXIT_OK


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
        original = read_while_replaced(path).decode("utf-8")
    except FileNotFoundError:
        return _config_problem(SetupError(f"{path} does not exist."))
    except PermissionError:
        return _config_problem(ConfigReadBusyError(path))
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
        if not enabled:
            return _already_off(args, cfg, number, name)
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
    #
    # The database first, then the switch, both ways (fix round 2 of F28).
    # `on`: if the save below fails, the channel stays OFF -- never read --
    # with its OFF period closed; the next `channels on` finds no open one
    # and extends that one to its own time, so nothing is lost. The other
    # order could leave it ON with its period still open, which no read
    # skips: everything posted while it was off would be read as new.
    # `off`: if the save fails, the channel stays ON with an open period,
    # which no read skips and the next `channels off` replaces. An `off`
    # whose time cannot be saved is still done -- switching a channel off
    # must always work -- and says what that costs.
    switched_on: channel_state.SwitchOn | None = None
    off_recorded = False
    off_problem: str | None = None
    try:
        if enabled:
            switched_on = channel_reading.record_switch_on(
                args.db,
                cfg.channel_id,
                _utcnow(),
                # A running watch may be seeding the channel at its first
                # cutoff; only with none may that be replaced.
                may_replace_first_cutoff=not session_in_use(),
            )
        else:
            off_recorded = bool(
                channel_reading.record_switch_off(args.db, cfg.channel_id, _utcnow())
            )
    except sqlite3.DatabaseError as exc:
        if enabled:
            complain(
                database_problem(args.db, exc),
                f"Nothing was changed: channel {number} is still OFF.",
            )
            return exit_codes.EXIT_FAILED
        off_problem = _off_not_saved(args.db, exc, number)

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
    if enabled:
        for line in _on_lines(switched_on):
            say(line)
    else:
        say(
            *_off_lines(
                args.db,
                cfg,
                max_age=max_signal_age_seconds(args.config_dir),
                recorded=off_recorded,
                problem=off_problem,
            )
        )
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
        original = read_while_replaced(path).decode("utf-8")
        channels = load_channels_config(args.config_dir)
    except SetupError as exc:
        return _config_problem(exc)
    except PermissionError:
        return _config_problem(ConfigReadBusyError(path))
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
    # on, the database first, as `channels on` does it (G2L-3). This
    # command holds the session, so no watch is seeding it: a first read
    # still pending is moved to now too.
    try:
        reread = channel_reading.reseed_from_now(
            args.db, resolved.channel_id, _utcnow(), may_replace_first_cutoff=True
        )
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
    if reread == "reseeded":
        say(READ_FROM_NOW)
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
