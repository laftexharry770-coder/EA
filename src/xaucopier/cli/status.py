"""`xaucopier status` -- what `watch` has recorded, and whether it is healthy.

**Read-only.** The store is opened with `db.connect_readonly()` (SQLite's
`mode=ro` plus `query_only`), so nothing here can write -- not a
migration, not a checkpoint, not an acknowledgement. A missing database
is reported, never created.

The report (`store/report.py` gathers it) is written for the user, not a
developer: plain words first, the internal name in brackets after, so a
developer reading a pasted report can still find the counter. Alerts come
first, CRITICAL before WARNING, because they are the one thing on the
page that needs a human.

`status` runs in a different process from `watch`, so "is watch alive?"
is answered from the stamps `watch` and its poll leave in the store
(`store/heartbeats.py`): a poll round finishes every few seconds while
all is well, so a stale one means `watch` is stuck, was closed without
Ctrl+C, or the computer slept.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xaucopier.cli import exit_codes, paths
from xaucopier.cli.watch import SetupError, load_channels_config
from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.client import EXIT_CONFIG_ERROR, EXIT_TERMINAL_AUTH
from xaucopier.ingest.listener import HISTORY_SKIPPED_METRIC
from xaucopier.ingest.poll import BACKLOG_RECOVERED_METRIC, MISSED_BY_EVENTSTREAM_METRIC
from xaucopier.store import alerts, db, heartbeats, metrics, report
from xaucopier.store.edits_repo import (
    DELETE_MISSED_BY_EVENTSTREAM_METRIC,
    EDIT_MISSED_BY_EVENTSTREAM_METRIC,
)
from xaucopier.store.outbox import (
    DEAD_ENTRY_SKIPPED_METRIC,
    LEASE_RECLAIMED_METRIC,
    NO_ACCOUNTS_METRIC,
)
from xaucopier.store.relay_repo import (
    RELAY_DUPLICATE_FOLLOWUPS_METRIC,
    RELAY_DUPLICATES_METRIC,
)

#: A poll round finishes every ~5 s; one whose channel reads all time out
#: takes up to ~45 s. Older than this, `watch` is not healthy.
STALE_AFTER = timedelta(minutes=2)

#: The system's reliability metrics: what the live feed dropped.
MISSED_METRICS: tuple[tuple[str, str], ...] = (
    (MISSED_BY_EVENTSTREAM_METRIC, "new posts"),
    (EDIT_MISSED_BY_EVENTSTREAM_METRIC, "edits"),
    (DELETE_MISSED_BY_EVENTSTREAM_METRIC, "deletions"),
)
#: Counters that should stay at zero; each non-zero one is a problem.
ZERO_METRICS: tuple[tuple[str, str], ...] = (
    (HISTORY_SKIPPED_METRIC, "posts given up on after 3 failed reads -- NOT recorded"),
    (NO_ACCOUNTS_METRIC, "signals recorded with no trading account switched on"),
    (LEASE_RECLAIMED_METRIC, "queue items taken back from a trading worker that died"),
    (DEAD_ENTRY_SKIPPED_METRIC, "queued entries dropped because their signal was withdrawn"),
)
#: Counters that are information only: nothing went wrong, nothing to do
#: (final fix wave). Posts made while watch was stopped are caught up when
#: it runs again -- not dropped by the live feed (F17) -- and a copy of a
#: call already recorded (a relay, a forward, a double post) is stored but
#: never traded or acted on twice (F02, F08, F09).
INFO_METRICS: tuple[tuple[str, str], ...] = (
    (BACKLOG_RECOVERED_METRIC, "posts made while watch was stopped, caught up after it started"),
    (RELAY_DUPLICATES_METRIC, "copies of a signal already recorded -- not traded twice"),
    (
        RELAY_DUPLICATE_FOLLOWUPS_METRIC,
        "copies of an instruction already recorded -- not acted on twice",
    ),
)

_KINDS: tuple[tuple[str, str], ...] = (
    ("signal", "signals"),
    ("followup", "follow-ups"),
    ("skip", "chatter (not a signal)"),
    ("parse_failed", "looked like a signal but could not be read"),
)
_SOURCES: tuple[tuple[str, str], ...] = (
    ("live", "live feed"),
    ("poll", "5-second check"),
    ("backfill", "catch-up at start"),
)
_STATES: tuple[tuple[str, str], ...] = (
    ("NEW", "valid (NEW)"),
    ("INVALID", "INVALID -- re-aimed or broken by an edit"),
    ("VOIDED", "VOIDED -- deleted or withdrawn before any trade"),
    ("SUPERSEDED", "SUPERSEDED"),
)
_OUTBOX_STATES = ("QUEUED", "CLAIMED", "DONE", "SKIPPED")

#: The poll's round stamp detail: "N of M channel(s) read" (poll.py).
_ROUND_DETAIL = re.compile(r"^(\d+) of (\d+) channel")

_CMD = paths.USER_COMMAND
#: What to do about a watch that is stuck or was closed. A stuck watch
#: still holds the session, so a second one would exit 6: stop it first.
_RESTART_ADVICE = (
    "If the watch window is still open, click in it, press Ctrl+C and answer Y; "
    f"then start it again with `{_CMD} watch`."
)


def database_problem(path: Path, exc: sqlite3.DatabaseError) -> str:
    """What is wrong with the database file at `path`, in plain words, for
    every command that reads it (`status`, `ack`, `channels`,
    `export-fixtures`): one written by a newer version of xaucopier
    (G1L-5: running `watch` cannot bring *that* up to date), one from an
    older version, one that is busy, or a file that is not xaucopier's
    database at all (final review F25). The caller says what was, or was
    not, done."""
    if isinstance(exc, db.SchemaVersionError):
        if exc.newer:
            return (
                f"The database at {path} was written by a newer version of xaucopier; "
                "use that version. This version does not read or change it."
            )
        return (
            f"The database at {path} is from an older version of xaucopier. Run "
            f"`{_CMD} watch` once to bring it up to date, then try again."
        )
    if db.is_lock_error(exc):
        return (
            f"The database is busy -- watch is probably writing to it ({path}). Try "
            "again in a moment."
        )
    if isinstance(exc, sqlite3.OperationalError):
        return f"The database at {path} could not be used ({exc})."
    return (
        f"The file at {path} is not xaucopier's database, or it is damaged ({exc}). "
        "Check that --db names the right file."
    )


def round_counts(detail: str | None) -> tuple[int, int] | None:
    """(channels read, channels tried) from a round stamp's detail, or
    None if it does not say."""
    match = _ROUND_DETAIL.match(detail or "")
    return (int(match[1]), int(match[2])) if match else None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def when(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def ago(now: datetime, value: datetime) -> str:
    seconds = int((now - value).total_seconds())
    if seconds < 0:
        return "in the future (is this computer's clock right?)"
    if seconds < 120:
        return f"{seconds} s ago"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h {minutes % 60} min ago"
    return f"{hours // 24} days ago"


def critical_first(items: tuple[alerts.Alert, ...]) -> tuple[alerts.Alert, ...]:
    """CRITICAL before WARNING; each level oldest first."""
    return tuple(sorted(items, key=lambda alert: (alert.level != "CRITICAL", alert.alert_id)))


def alert_lines(items: tuple[alerts.Alert, ...]) -> list[str]:
    """Each alert as two lines -- level, number, time, code, where; then
    what it says. Shared by `status` and `ack`."""
    lines: list[str] = []
    for alert in items:
        where = ", ".join(
            f"{label} {value}"
            for label, value in (
                ("channel", alert.channel_id),
                ("message", alert.message_id),
                ("signal", alert.signal_id),
            )
            if value is not None
        )
        lines.append(
            f"  [{alert.level}] #{alert.alert_id} {when(alert.created_at_utc)}  {alert.code}"
            + (f"  ({where})" if where else "")
        )
        lines.append(f"      {alert.detail}")
    return lines


def _alert_lines(rep: report.StatusReport) -> list[str]:
    if not rep.alerts:
        return ["ALERTS: none"]
    critical = sum(1 for alert in rep.alerts if alert.level == "CRITICAL")
    lines = [
        "!" * 72,
        f"ALERTS: {len(rep.alerts)} unacknowledged ({critical} CRITICAL, "
        f"{len(rep.alerts) - critical} WARNING)",
    ]
    lines.extend(alert_lines(rep.alerts))
    lines.append(
        "  An alert is something xaucopier could not settle safely on its own. This "
        "version places no orders, so nothing is at risk right now. Do what a "
        "TELEGRAM_... alert says (it clears itself once watch reads the channel "
        "again); send any other to whoever looks after xaucopier. Then clear it with "
        f"`{_CMD} ack --all` (or `{_CMD} ack NUMBER` for one)."
    )
    lines.append("!" * 72)
    return lines


def _watch_lines(rep: report.StatusReport, now: datetime, secrets_path: Path) -> list[str]:
    started = rep.heartbeat(heartbeats.WATCH_STARTED)
    stopped = rep.heartbeat(heartbeats.WATCH_STOPPED)
    last_round = rep.heartbeat(heartbeats.POLL_ROUND)
    lines = ["WATCH"]
    if started is None:
        lines.append("  watch has never run against this database.")
    elif stopped is None or stopped.at_utc < started.at_utc:
        since = f"started {when(started.at_utc)} ({ago(now, started.at_utc)})"
        if last_round is None or last_round.at_utc < started.at_utc:
            if now - started.at_utc > STALE_AFTER:
                lines.append(
                    f"  NOT HEALTHY: {since}, but no check of the channel has finished "
                    "since. It may be stuck connecting, or its window was closed. "
                    + _RESTART_ADVICE
                )
            else:
                lines.append(f"  STARTING: {since}; the first check has not finished yet.")
        elif now - last_round.at_utc > STALE_AFTER:
            lines.append(
                f"  NOT HEALTHY: the last check of the channel finished "
                f"{ago(now, last_round.at_utc)} ({when(last_round.at_utc)}). watch was "
                "probably closed without Ctrl+C, or the computer slept, or it is stuck. "
                + _RESTART_ADVICE
            )
        elif (counts := round_counts(last_round.detail)) is not None and counts[0] < counts[1]:
            # A round that finished but read fewer channels than it tried is
            # not health: nothing is being recorded from the rest.
            lines.append(
                f"  NOT HEALTHY: {since}, and still checking -- but the last check "
                f"({ago(now, last_round.at_utc)}) could read only {counts[0]} of "
                f"{counts[1]} channel(s): Telegram is not answering for the rest (see "
                f"CHANNELS below, and {paths.LOG_FILE_NAME}), so nothing new is being "
                "recorded from them. If this lasts more than a few minutes: "
                + _RESTART_ADVICE
            )
        else:
            lines.append(
                f"  RUNNING: {since}; last check of the channel {ago(now, last_round.at_utc)}"
                + (f" ({last_round.detail})" if last_round.detail else "")
            )
    else:
        try:
            code: int | None = int(stopped.detail) if stopped.detail is not None else None
        except ValueError:
            code = None
        how = (
            f"exit code {code}: {exit_codes.meaning(code)}" if code is not None else "exit code unknown"
        )
        lines.append(
            f"  NOT RUNNING: stopped {when(stopped.at_utc)} ({ago(now, stopped.at_utc)}), {how}."
        )
        if code == EXIT_TERMINAL_AUTH:
            lines.append(
                "  Telegram refuses this login (logged out, used from two places at once, "
                f"or the account is banned). Run `{_CMD} setup-login`, then `{_CMD} watch`."
            )
        elif code == EXIT_CONFIG_ERROR:
            lines.append(
                "  Telegram rejects this program's api_id/api_hash. Get a NEW pair at "
                f"https://my.telegram.org, run `{_CMD} setup-login`, then `{_CMD} watch`."
            )
    if secrets_path.is_file():
        lines.append(f"  Telegram session file: present ({secrets_path})")
    else:
        lines.append(
            f"  Telegram session file: MISSING ({secrets_path}) -- run `{_CMD} setup-login`"
        )
    return lines


def _counts_line(pairs: tuple[tuple[str, int], ...], labels: tuple[tuple[str, str], ...]) -> str:
    known = {key for key, _ in labels}
    parts = [f"{label} {report.count(pairs, key)}" for key, label in labels]
    parts.extend(f"{key} {value}" for key, value in pairs if key not in known)
    return " | ".join(parts)


def _metric_table(
    rep: report.StatusReport, title: str, rows: tuple[tuple[str, str], ...]
) -> list[str]:
    labels = [(f"  {label} ({name})", rep.metric(name)) for name, label in rows]
    width = max(len(title), *(len(text) for text, _ in labels))
    lines = [f"{title:<{width}}  {'today':>7}  {'all time':>8}"]
    for text, total in labels:
        flag = "  <-- look into this" if title.startswith("SHOULD") and total.all_time else ""
        lines.append(f"{text:<{width}}  {total.today:>7}  {total.all_time:>8}{flag}")
    return lines


def _message_lines(rep: report.StatusReport) -> list[str]:
    lines = [f"MESSAGES RECORDED: {rep.messages_total}"]
    lines.append("  " + _counts_line(rep.messages_by_kind, _KINDS))
    lines.append("  arrived via: " + _counts_line(rep.messages_by_source, _SOURCES))
    return lines


def _signal_lines(rep: report.StatusReport) -> list[str]:
    recorded = sum(value for _, value in rep.signals_by_state)
    lines = [f"SIGNALS RECORDED AS TRADES: {recorded}"]
    lines.append("  " + _counts_line(rep.signals_by_state, _STATES))
    lines.append(f"  withdrawn by the poster (deleted, or edited into chatter): {rep.withdrawn_signals}")
    if rep.deleted_live_signals:
        lines.append(
            f"  LIVE SIGNALS WHOSE MESSAGE WAS DELETED: {len(rep.deleted_live_signals)}"
        )
        lines.extend(
            f"    signal {item.signal_id} (channel {item.channel_id}, message "
            f"{item.message_id}) deleted {when(item.deleted_at_utc)}, noticed by "
            f"{item.detected_by or 'unknown'}"
            for item in rep.deleted_live_signals
        )
    else:
        lines.append("  live signals whose message was deleted: none")
    lines.append(f"  follow-up instructions recorded: {rep.followups}")
    return lines


def _outbox_lines(rep: report.StatusReport) -> list[str]:
    lines = ["TRADE QUEUE (watch-only: nothing in it is executed)"]
    lines.append(
        "  " + " | ".join(f"{state} {report.count(rep.outbox_by_state, state)}" for state in _OUTBOX_STATES)
    )
    if rep.skipped_outbox:
        lines.append(f"  skipped ({len(rep.skipped_outbox)}):")
        for row in rep.skipped_outbox:
            target = (
                f"signal {row.signal_id}" if row.signal_id is not None else f"follow-up {row.followup_id}"
            )
            lines.append(
                f"    #{row.outbox_id} {row.account_id} {row.kind} for {target}: "
                f"{row.reason or '(no reason recorded)'}"
            )
    return lines


def _channel_lines(
    rep: report.StatusReport,
    configured: tuple[ChannelCfg, ...] | None,
    now: datetime,
    watch_running: bool,
) -> list[str]:
    lines = ["CHANNELS"]
    activity = {item.channel_id: item for item in rep.channels}
    last_round = rep.heartbeat(heartbeats.POLL_ROUND)
    entries: list[tuple[int, str, bool]] = []
    if configured is not None:
        for channel in configured:
            if channel.channel_id == 0:
                entries.append(
                    (
                        0,
                        f"{channel.name} -- id not looked up yet "
                        f"(run `{_CMD} resolve-channels`)",
                        False,
                    )
                )
            else:
                state = "switched on" if channel.enabled else "switched off"
                entries.append(
                    (
                        channel.channel_id,
                        f"{channel.name} (id {channel.channel_id}) -- {state}",
                        channel.enabled,
                    )
                )
    known = {channel_id for channel_id, _, _ in entries}
    entries.extend(
        (channel_id, f"id {channel_id} (not in channels.toml)", False)
        for channel_id in activity
        if channel_id not in known
    )
    if not entries:
        return lines + ["  none"]
    for channel_id, title, watched in entries:
        lines.append(f"  {title}")
        if channel_id == 0:
            continue
        item = activity.get(channel_id)
        if item is None or item.last_message_id is None:
            lines.append("    no messages recorded yet")
        else:
            lines.append(
                f"    messages: {item.messages}; newest #{item.last_message_id} posted "
                f"{when(item.last_posted_at_utc)} ({ago(now, item.last_posted_at_utc)}): "
                f"{item.last_kind}, via {dict(_SOURCES).get(item.last_source, item.last_source)}"
            )
        read = rep.heartbeat(heartbeats.channel_read_name(channel_id))
        pointer = item.pointer if item is not None else 0
        failures = item.consecutive_parse_failures if item is not None else 0
        read_text = f"{ago(now, read.at_utc)}" if read is not None else "never"
        warn = ""
        if watched and watch_running:
            if read is None or now - read.at_utc > STALE_AFTER:
                warn = "  <-- NOT BEING READ"
            elif last_round is not None and read.at_utc < last_round.at_utc:
                warn = "  <-- could not be read in the last check"
        resumes = f"reading resumes after #{pointer}" if pointer else "not read from history yet"
        lines.append(
            f"    last read by the 5-second check: {read_text}{warn}; {resumes}; "
            f"unreadable signals in a row: {failures}"
        )
    return lines


def render(
    rep: report.StatusReport,
    *,
    now: datetime,
    db_path: Path,
    secrets_path: Path,
    configured: tuple[ChannelCfg, ...] | None,
    config_problem: str | None,
) -> str:
    """The whole report as text."""
    started = rep.heartbeat(heartbeats.WATCH_STARTED)
    stopped = rep.heartbeat(heartbeats.WATCH_STOPPED)
    watch_running = started is not None and (stopped is None or stopped.at_utc < started.at_utc)

    sections: list[list[str]] = [
        [f"xaucopier status -- {when(now)}", f"recordings: {db_path}"],
        _alert_lines(rep),
        _watch_lines(rep, now, secrets_path),
        _message_lines(rep),
        _metric_table(
            rep, "MISSED BY TELEGRAM'S LIVE FEED, RECOVERED BY THE 5-SECOND CHECK", MISSED_METRICS
        ),
        _metric_table(rep, "SHOULD STAY AT ZERO", ZERO_METRICS),
        _metric_table(rep, "FOR INFORMATION -- NOTHING TO DO", INFO_METRICS),
    ]
    known = {name for name, _ in MISSED_METRICS + ZERO_METRICS + INFO_METRICS}
    others = [total for total in rep.metrics if total.name not in known]
    if others:
        sections.append(
            ["OTHER COUNTERS (today / all time)"]
            + [f"  {total.name}: {total.today} / {total.all_time}" for total in others]
        )
    sections.extend(
        [
            _signal_lines(rep),
            _outbox_lines(rep),
            _channel_lines(rep, configured, now, watch_running),
        ]
    )
    if config_problem is not None:
        sections.append([f"(could not read the channel list: {config_problem})"])
    return "\n\n".join("\n".join(section) for section in sections) + "\n"


def cmd_status(args: argparse.Namespace) -> int:
    db_path: Path = args.db
    now = _utcnow()
    try:
        conn = db.connect_readonly(db_path)
        try:
            rep = report.collect(conn, today=metrics.day_key_for(now))
        finally:
            conn.close()
    except FileNotFoundError:
        print(
            f"No recordings yet: there is no database at {db_path}. "
            f"Run `{_CMD} watch` first.",
            file=sys.stderr,
        )
        return exit_codes.EXIT_FAILED
    except sqlite3.DatabaseError as exc:
        print(database_problem(db_path, exc), file=sys.stderr)
        return exit_codes.EXIT_FAILED

    configured: tuple[ChannelCfg, ...] | None
    config_problem: str | None = None
    try:
        configured = load_channels_config(args.config_dir)
    except SetupError as exc:
        configured, config_problem = None, str(exc)

    _write(
        render(
            rep,
            now=now,
            db_path=db_path,
            secrets_path=args.secrets,
            configured=configured,
            config_problem=config_problem,
        )
    )
    return exit_codes.EXIT_OK


def _write(text: str) -> None:
    """Print `text`, replacing what the console's encoding cannot show (a
    channel name with an emoji, redirected to a file on Windows) rather
    than crashing on it."""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding))
