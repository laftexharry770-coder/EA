"""`xaucopier watch` -- the deliverable: a signal recorder that places no
orders, and cannot.

It runs Task 8's listener with Task 9's history poll in its slot
(`poll_hook=HistoryPoll()`, Task 10's edit window included) against the
channels and accounts in `config/`, recording into the SQLite store.
Nothing on this path imports `MetaTrader5`; `tests/test_cli_import_graph.py`
walks the real import graph from `cli/main.py` to prove it.

**Before anything connects, the setup is checked** (`check_setup`), and
every problem a restart cannot fix exits `EXIT_CONFIG_ERROR` (5) with a
plain message instead of starting: a missing or malformed config file, a
channel switched on with `channel_id = 0`, no channel switched on at all,
two channels with one id, a missing database folder, a database file that
cannot be opened, a missing or undecryptable session file. Before exit 5
existed these all surfaced as exit 4 ("restart me"), so a supervisor
would have restarted them hot, for ever. Then the single-instance lock
(`EXIT_ALREADY_RUNNING`, 6, if another process holds the session).

**Then the store is opened once on this thread, before any event loop
exists,** which migrates it if needed and stamps `watch_started`. (The
listener's "writer first" rule is about not migrating on the event-loop
thread; here there is no loop yet.) When the listener stops, `watch`
stamps `watch_stopped` with the exit code -- and, for exit 3, records a
CRITICAL `TELEGRAM_SESSION_DEAD` alert -- so `xaucopier status`, a
different process, can say why it is not running.

Exit codes are `cli/exit_codes.py`'s; `run_listener()` supplies 3 and 4.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sqlite3
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from xaucopier.cli import exit_codes, paths
from xaucopier.config.accounts import AccountCfg, load_accounts
from xaucopier.config.channels import ChannelCfg, load_channels
from xaucopier.config.loader import ConfigError
from xaucopier.ingest.client import (
    EXIT_CONFIG_ERROR,
    EXIT_TERMINAL_AUTH,
    AlreadyRunningError,
    acquire_single_instance_lock,
    build_client,
)
from xaucopier.ingest.listener import Listener, run_listener
from xaucopier.ingest.poll import HistoryPoll
from xaucopier.ingest.secrets import SecretsBlob, SecretsError
from xaucopier.ingest.secrets import load as load_secrets
from xaucopier.store import alerts, db, heartbeats

logger = logging.getLogger(__name__)

#: `alerts.code` recorded when `watch` exits 3: Telegram refuses the
#: session, and only the user can fix that (`setup-login`).
TELEGRAM_SESSION_DEAD = alerts.TELEGRAM_SESSION_DEAD
#: `alerts.code` recorded when Telegram rejects the api_id/api_hash (exit
#: 5 from the listener): only a new pair from my.telegram.org fixes it.
TELEGRAM_API_ID_REJECTED = alerts.TELEGRAM_API_ID_REJECTED

#: What the user must do, per permanent Telegram stop: the alert's text
#: and the line printed when `watch` exits.
_TELEGRAM_STOPS: dict[int, tuple[str, str]] = {
    EXIT_TERMINAL_AUTH: (
        TELEGRAM_SESSION_DEAD,
        "Telegram refuses this login: it was logged out (for example from "
        "Telegram -> Settings -> Devices), used from two places at once, or the "
        "account is deactivated, banned or frozen. watch cannot record anything "
        "until you run `.\\xaucopier setup-login` again. This alert clears "
        "itself once watch reads the channel again.",
    ),
    EXIT_CONFIG_ERROR: (
        TELEGRAM_API_ID_REJECTED,
        "Telegram rejects this program's api_id/api_hash (it is invalid, or it "
        "was published somewhere, e.g. copied from a tutorial). Restarting, or "
        "logging in again with the same pair, cannot fix it: get a NEW "
        "api_id/api_hash at https://my.telegram.org, then run "
        "`.\\xaucopier setup-login` again. This alert clears itself once "
        "watch reads the channel again.",
    ),
}

#: The log file rotates at this size, keeping this many old files.
_LOG_MAX_BYTES = 5_000_000
_LOG_BACKUPS = 5


class SetupError(Exception):
    """A problem with the setup that restarting cannot fix. `watch` prints
    it and exits `EXIT_CONFIG_ERROR` without starting anything."""


@dataclass(frozen=True, slots=True)
class WatchSetup:
    """What `check_setup` found: the loaded config and a usable store path."""

    channels: tuple[ChannelCfg, ...]
    accounts: tuple[AccountCfg, ...]
    db_path: Path

    @property
    def enabled_channels(self) -> tuple[ChannelCfg, ...]:
        return tuple(channel for channel in self.channels if channel.enabled)


def _load(loader: Callable[[Path], tuple], path: Path) -> tuple:
    try:
        return loader(path)
    except FileNotFoundError:
        raise SetupError(f"{path} does not exist.") from None
    except (ConfigError, tomllib.TOMLDecodeError) as exc:
        raise SetupError(f"{path} is not valid: {exc}") from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # A structurally odd file (e.g. `channels = "x"`) fails deep in the
        # loader; it is still the file's fault, not a reason to restart.
        raise SetupError(f"{path} could not be read: {type(exc).__name__}: {exc}") from None


def load_channels_config(config_dir: Path) -> tuple[ChannelCfg, ...]:
    """`channels.toml` from `config_dir`, through the Task 1 loader. Any
    problem is a `SetupError`."""
    return _load(load_channels, config_dir / paths.CHANNELS_FILE)


def load_config(config_dir: Path) -> tuple[tuple[ChannelCfg, ...], tuple[AccountCfg, ...]]:
    """`channels.toml` and `accounts.toml` from `config_dir`, through the
    Task 1 loaders. Any problem is a `SetupError`."""
    channels = load_channels_config(config_dir)
    accounts = _load(load_accounts, config_dir / paths.ACCOUNTS_FILE)
    return channels, accounts


def check_setup(config_dir: Path, db_path: Path) -> WatchSetup:
    """Every check that needs no network and no lock. Raises `SetupError`
    for the first permanent problem found."""
    channels, accounts = load_config(config_dir)
    channels_path = config_dir / paths.CHANNELS_FILE

    enabled = [channel for channel in channels if channel.enabled]
    if not enabled:
        raise SetupError(
            f"no channel is switched on in {channels_path}, so there is nothing to "
            f"watch. See the channels with `{paths.USER_COMMAND} channels` and switch one "
            f"on with `{paths.USER_COMMAND} channels on NUMBER` -- or, for a channel whose "
            f"id has not been looked up yet, run `{paths.USER_COMMAND} resolve-channels`."
        )
    seen: dict[int, str] = {}
    for channel in enabled:
        if channel.channel_id in seen:
            raise SetupError(
                f"{channels_path}: channels {seen[channel.channel_id]!r} and "
                f"{channel.name!r} are both switched on with channel_id "
                f"{channel.channel_id}; each channel must appear once."
            )
        seen[channel.channel_id] = channel.name

    folder = db_path.parent
    if not folder.is_dir():
        raise SetupError(
            f"the database folder {folder} does not exist. Create it, or pass --db "
            "with a file inside a folder that exists."
        )
    if db_path.exists() and not db_path.is_file():
        raise SetupError(f"{db_path} exists but is not a file.")
    return WatchSetup(channels=channels, accounts=accounts, db_path=db_path)


def load_session(secrets_path: Path) -> SecretsBlob:
    """The encrypted Telegram session, or `SetupError` saying to run
    `setup-login` (`SecretsError`'s message never contains a secret)."""
    try:
        return load_secrets(secrets_path)
    except SecretsError as exc:
        raise SetupError(str(exc)) from None


def _is_transient(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _utcnow() -> datetime:
    return datetime.now(UTC)


def stamp_start(db_path: Path) -> None:
    """Open (creating and migrating if needed) the store and stamp
    `watch_started`. A database that cannot be opened is a `SetupError`,
    except a busy one (`sqlite3.OperationalError` "locked"), which is
    re-raised: that clears up on its own, so it is exit 4, not 5."""
    try:
        conn = db.connect(db_path, role="writer")
    except sqlite3.Error as exc:
        if _is_transient(exc):
            raise
        raise SetupError(
            f"the database {db_path} cannot be used ({type(exc).__name__}: {exc})."
        ) from None
    try:
        heartbeats.beat(conn, name=heartbeats.WATCH_STARTED, now_utc=_utcnow())
        conn.commit()
    finally:
        conn.close()


def stamp_stop(db_path: Path, exit_code: int) -> None:
    """Stamp `watch_stopped` with `exit_code` and, for a permanent Telegram
    stop -- exit 3 (the session is refused) or exit 5 (the only exit 5 a
    *running* watch can return: Telegram rejected the api_id/api_hash;
    every other setup problem exits before the store is opened) --
    record the CRITICAL alert `status` shows, naming the fix. Best
    effort: it runs after the listener's writer has stopped, and a store
    that cannot take this write has already said so; failing here only
    means `status` shows an older state."""
    now_utc = _utcnow()
    try:
        conn = db.connect(db_path, role="writer")
        try:
            heartbeats.beat(
                conn, name=heartbeats.WATCH_STOPPED, now_utc=now_utc, detail=str(exit_code)
            )
            if exit_code in _TELEGRAM_STOPS:
                code, detail = _TELEGRAM_STOPS[exit_code]
                alerts.record(
                    conn, now_utc=now_utc, level="CRITICAL", code=code, detail=detail
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- a report about stopping must not mask the stop
        logger.exception("could not record that watch stopped; `status` will show an older state")


def build_poll() -> HistoryPoll:
    """The poll `watch` runs: every 5 s, with Task 10's edit window. Its
    defaults are the design (ingest/poll.py explains each); nothing here
    tunes them."""
    return HistoryPoll()


async def run_watch(client: object, setup: WatchSetup) -> int:
    """Run the listener, with the poll, until it stops; return its exit
    code (`run_listener`: 3 or 4 -- never 0)."""
    listener = Listener(
        client=client,
        channels=setup.channels,
        accounts=setup.accounts,
        writer=db.Writer(setup.db_path),
        db_path=setup.db_path,
        poll_hook=build_poll(),
    )
    return await run_listener(listener)


def start_logging(log_path: Path) -> Callable[[], None]:
    """Log INFO and above to the console and to `log_path` (rotating), in
    UTC. Returns the function that undoes it."""
    root = logging.getLogger()
    previous_level = root.level
    formatter = logging.Formatter("%(asctime)s UTC %(levelname)-8s %(name)s: %(message)s")
    formatter.converter = time.gmtime
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
            )
        )
    except OSError as exc:
        print(f"warning: cannot write the log file {log_path} ({exc}); logging to this window only",
              file=sys.stderr)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.INFO)

    def stop() -> None:
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(previous_level)

    return stop


def _print_setup_error(exc: SetupError) -> None:
    print(f"xaucopier watch cannot start: {exc}", file=sys.stderr)
    print(
        "Nothing was started. This is a setup problem that restarting will not fix "
        f"(exit code {exit_codes.EXIT_CONFIG_ERROR}); fix it and run "
        f"`{paths.USER_COMMAND} watch` again. "
        "See docs/RUNBOOK.md.",
        file=sys.stderr,
    )


def _print_banner(setup: WatchSetup, log_path: Path) -> None:
    print("xaucopier watch -- WATCH-ONLY: records signals, places NO orders.")
    for channel in setup.enabled_channels:
        print(f"  watching: {channel.name} (id {channel.channel_id})")
    print(f"  recording to: {setup.db_path}")
    print(f"  log file: {log_path}")
    print(
        "Leave this window open. To stop: press Ctrl+C, then answer Y. "
        f"Check on it from another window with: {paths.USER_COMMAND} status"
    )


def _run(setup: WatchSetup, blob: SecretsBlob) -> int:
    try:
        client = build_client(blob)
        return asyncio.run(run_watch(client, setup))
    except KeyboardInterrupt:
        return exit_codes.EXIT_INTERRUPTED
    except Exception:  # noqa: BLE001 -- anything else is "stopped, restart me"
        logger.exception("watch stopped on an unexpected error")
        return exit_codes.EXIT_LISTENER_STOPPED


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        setup = check_setup(args.config_dir, args.db)
        blob = load_session(args.secrets)
    except SetupError as exc:
        _print_setup_error(exc)
        return exit_codes.EXIT_CONFIG_ERROR

    try:
        lock = acquire_single_instance_lock()
    except AlreadyRunningError as exc:
        print(
            "xaucopier watch is already running on this computer (or resolve-channels "
            "or channels add is). Do not start a second one: two programs using the "
            "same Telegram session at once can get it logged out for good. Find its "
            "window; if it is stuck, press Ctrl+C there and answer Y, then start watch "
            "again.",
            file=sys.stderr,
        )
        if exc.hint:  # where it probably is (G2L-1)
            print(exc.hint, file=sys.stderr)
        return exit_codes.EXIT_ALREADY_RUNNING

    log_path = setup.db_path.parent / paths.LOG_FILE_NAME
    stop_logging = start_logging(log_path)
    exit_code: int | None = None
    try:
        try:
            stamp_start(setup.db_path)
        except SetupError as exc:
            _print_setup_error(exc)
            return exit_codes.EXIT_CONFIG_ERROR
        except sqlite3.Error as exc:
            print(f"The database is busy ({exc}); try again in a minute.", file=sys.stderr)
            return exit_codes.EXIT_LISTENER_STOPPED

        _print_banner(setup, log_path)
        exit_code = _run(setup, blob)
    except KeyboardInterrupt:
        exit_code = exit_codes.EXIT_INTERRUPTED
    finally:
        try:
            if exit_code is not None:
                stamp_stop(setup.db_path, exit_code)
                print(f"watch stopped (exit code {exit_code}: {exit_codes.meaning(exit_code)}).")
                if exit_code in _TELEGRAM_STOPS:
                    print(_TELEGRAM_STOPS[exit_code][1], file=sys.stderr)
        finally:
            stop_logging()
            lock.release()
    return exit_code
