"""xaucopier command-line entry point.

The commands, in the order the user runs them (docs/RUNBOOK.md):

- `setup-login` (Task 7) -- the one-time interactive Telegram login that
  produces the DPAPI-encrypted session every other Telegram command uses.
- `resolve-channels` (`cli/resolve.py`) -- look up each placeholder
  channel's numeric id once and write it into `config/channels.toml`.
- `channels` (`cli/channels.py`) -- list the channels signals are taken
  from; switch one on or off (`channels on N` / `channels off N`), or add
  a new one (`channels add`).
- `watch` (`cli/watch.py`) -- the deliverable: record every signal,
  place no order. Nothing reachable from here imports `MetaTrader5`.
- `status` (`cli/status.py`) -- a read-only report on what `watch` has
  recorded and whether it is healthy.
- `ack` (`cli/ack.py`) -- list the alerts `status` shows, and clear them
  once dealt with.
- `export-fixtures` (`cli/fixtures.py`) -- write every recorded message
  out as a replay fixture for the Plan 1 harness.

Every exit code any of them returns is in `cli/exit_codes.py`, and is
printed at the bottom of `--help`.

**`setup-login` never shows a traceback.** The user is not a developer:
every way a login can fail -- a rejected or published api_id (exit 5),
a banned phone number or account (3), a mistyped phone number, three
wrong codes or passwords, too many attempts, no network (1), Ctrl+C
(130) -- ends in one plain sentence saying what happened and what to do
(`login_failure()`), and nothing is saved.

**The login is interactive and cannot be automated.** `setup-login`
prompts for `api_id`/`api_hash`/phone directly in the console via
`input()`/`getpass.getpass()`, and hands Telethon's own `code_callback`
and `password` callbacks straight to the same primitives so the login
code and 2FA password are typed by the human, once, and never touch a
variable that outlives this function call. None of `api_id`, `api_hash`,
the phone number, the login code, or the 2FA password are ever printed,
logged, or written anywhere except into the encrypted blob via
`secrets.save()`.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import io
import sys
from pathlib import Path

from telethon import errors

from xaucopier.cli import exit_codes, paths
from xaucopier.cli.ack import cmd_ack
from xaucopier.cli.channels import cmd_channels
from xaucopier.cli.fixtures import cmd_export_fixtures
from xaucopier.cli.resolve import cmd_resolve_channels, connect_one_shot
from xaucopier.cli.status import cmd_status
from xaucopier.cli.watch import cmd_watch
from xaucopier.ingest.client import (
    API_CREDENTIAL_ERRORS,
    TERMINAL_AUTH_ERRORS,
    TelegramUnreachableError,
    build_client,
)
from xaucopier.ingest.secrets import DEFAULT_SECRETS_PATH, SecretsBlob, save

#: How the user runs setup-login again, from PowerShell in the project folder.
_AGAIN = f"run `{paths.USER_COMMAND} setup-login` again"
_SUPPORT = "No program can fix this; only Telegram support can (https://telegram.org/support)."
#: Telethon's own words when the login code was wrong `max_attempts` (3)
#: times in a row (telethon/client/auth.py, `_start`).
_THREE_WRONG_CODES = "consecutive sign-in attempts failed"
#: Said whenever Telegram accepted the login but no session was saved (the
#: save failed, or something failed right after the acceptance -- final
#: review F34): Telegram made a XAUCOPIER-TRADEBOX session this program has
#: no copy of, and running setup-login again makes a second one.
_END_EXTRA_SESSION = (
    "IMPORTANT: Telegram had already accepted this login, so a 'XAUCOPIER-TRADEBOX' "
    "session now exists on your Telegram account, and this program has no copy of it. "
    "Open Telegram -> Settings -> Devices and end that extra XAUCOPIER-TRADEBOX "
    f"session by hand, then {_AGAIN}."
)


def _prompt_login_code() -> str:
    return input("Enter the login code Telegram just sent you: ").strip()


#: How many times a blank answer at the 2FA prompt is asked again.
_PASSWORD_TRIES = 3


class BlankPasswordError(Exception):
    """The two-step verification password was left blank `_PASSWORD_TRIES`
    times in a row."""


def _prompt_2fa_password() -> str:
    """Ask for the two-step verification password.

    Telethon only calls this when the account HAS two-step verification
    on, so a blank answer is never right here -- and it is dangerous:
    Telethon takes an empty password as "no password", asks Telegram to
    send a new login code instead of checking one, and returns from
    `start()` as if the login had worked. So a blank is refused and asked
    again, and after `_PASSWORD_TRIES` blanks the login gives up
    (`BlankPasswordError`)."""
    for _ in range(_PASSWORD_TRIES):
        password = getpass.getpass(
            "This Telegram account has two-step verification turned on. "
            "Enter its password (it will not be shown): "
        )
        if password:
            return password
        print(
            "The password cannot be blank: this account has two-step verification "
            "on, so Telegram needs it.",
            file=sys.stderr,
        )
    raise BlankPasswordError


def login_failure(exc: BaseException) -> tuple[int, str]:
    """What a failed login means to the user: an exit code and one plain
    sentence saying what happened and what to do. Never the phone number
    or any secret -- only fixed text, and Telegram's own fixed error text."""
    name = type(exc).__name__
    if isinstance(exc, API_CREDENTIAL_ERRORS):
        return exit_codes.EXIT_CONFIG_ERROR, (
            f"Telegram rejected this api_id/api_hash ({name}): it is wrong, or it was "
            "published somewhere (for example copied from a tutorial) and is now "
            "banned. Get your OWN pair at https://my.telegram.org (API development "
            f"tools), then {_AGAIN}."
        )
    if isinstance(exc, errors.PhoneNumberBannedError):
        return exit_codes.EXIT_TERMINAL_AUTH, (
            f"Telegram has banned this phone number, so it cannot log in. {_SUPPORT}"
        )
    if isinstance(exc, (errors.UserDeactivatedError, errors.UserDeactivatedBanError)):
        return exit_codes.EXIT_TERMINAL_AUTH, (
            f"Telegram says this account is deactivated or banned ({name}), so it "
            f"cannot log in. {_SUPPORT}"
        )
    if isinstance(exc, errors.FrozenMethodInvalidError):
        return exit_codes.EXIT_TERMINAL_AUTH, (
            f"Telegram has frozen this account, so it cannot log in. {_SUPPORT}"
        )
    if isinstance(exc, TERMINAL_AUTH_ERRORS):
        return exit_codes.EXIT_TERMINAL_AUTH, (
            f"Telegram refused the login ({name}). Wait a minute and {_AGAIN}; if it "
            "happens again, close any other program using this Telegram login first."
        )
    if isinstance(exc, errors.PhoneNumberInvalidError):
        return exit_codes.EXIT_FAILED, (
            "Telegram says that phone number is not valid. Type it starting with + "
            f"and your country code, with no spaces, and {_AGAIN}."
        )
    if isinstance(exc, errors.PhoneNumberUnoccupiedError):
        return exit_codes.EXIT_FAILED, (
            f"No Telegram account uses that phone number. Check it and {_AGAIN}."
        )
    if isinstance(exc, RuntimeError) and _THREE_WRONG_CODES in str(exc):
        return exit_codes.EXIT_FAILED, (
            "The login code was wrong 3 times, so Telegram stopped this attempt. "
            f"{_AGAIN[0].upper()}{_AGAIN[1:]} and type the newest code Telegram sends to "
            "your Telegram app."
        )
    if isinstance(exc, BlankPasswordError):
        return exit_codes.EXIT_FAILED, (
            "This account has two-step verification turned on, so Telegram needs its "
            f"password, and it was left blank {_PASSWORD_TRIES} times. Find it (or reset "
            "it) in Telegram -> Settings -> Privacy and Security -> Two-Step "
            f"Verification, then {_AGAIN}."
        )
    if isinstance(exc, errors.PasswordHashInvalidError):
        return exit_codes.EXIT_FAILED, (
            "The 2FA password was wrong 3 times. "
            f"{_AGAIN[0].upper()}{_AGAIN[1:]}; if you have forgotten it, reset it in "
            "Telegram -> Settings -> Privacy and Security -> Two-Step Verification."
        )
    if isinstance(
        exc,
        (
            errors.FloodError,
            errors.PhoneNumberFloodError,
            errors.PhonePasswordFloodError,
            errors.SendCodeUnavailableError,
        ),
    ):
        seconds = getattr(exc, "seconds", None)
        wait = (
            f"about {max(1, round(seconds / 60))} minute(s)"
            if isinstance(seconds, int | float)
            else "a while (an hour is safe)"
        )
        return exit_codes.EXIT_FAILED, (
            f"Telegram says there have been too many login attempts. Wait {wait}, "
            f"then {_AGAIN}."
        )
    if isinstance(exc, TelegramUnreachableError):  # the bounded connect (F24)
        return exit_codes.EXIT_FAILED, f"{exc} Then {_AGAIN}."
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return exit_codes.EXIT_FAILED, (
            f"Could not reach Telegram ({name}). Check the internet connection, then "
            f"{_AGAIN}."
        )
    if isinstance(exc, EOFError):
        return exit_codes.EXIT_FAILED, (
            "No answer could be read: the input was closed. Run "
            f"`{paths.USER_COMMAND} setup-login` in an ordinary PowerShell window and "
            "type the answers there."
        )
    # Telegram's own error text is fixed wording; anything else might hold
    # something the user typed, so only its type is named.
    detail = f"{name}: {exc}" if isinstance(exc, errors.RPCError) else name
    return exit_codes.EXIT_FAILED, (
        f"The login failed ({detail}). {_AGAIN[0].upper()}{_AGAIN[1:]}; if it keeps "
        "failing, send this line to whoever looks after xaucopier."
    )


def _print_failure(exc: BaseException, *, accepted: bool = False) -> int:
    """`login_failure()`'s sentence, then -- when Telegram had already
    accepted the login (`accepted`) -- the session to end by hand."""
    code, message = login_failure(exc)
    print(message, file=sys.stderr)
    if accepted:
        print(_END_EXTRA_SESSION, file=sys.stderr)
    print("Nothing was saved.", file=sys.stderr)
    return code


def _login_accepted(client: object) -> bool:
    """Whether Telegram accepted the code (or the 2FA password) before the
    failure (final review F34). Telethon marks its client authorized
    (`_authorized = True`, in `_on_login`) the moment Telegram answers the
    sign-in, before its own next requests -- which can still fail, for
    example with Telegram's own trouble. Read without asking Telegram
    anything: the connection may be what failed."""
    return getattr(client, "_authorized", None) is True


async def _start_quietly(client: object, *, phone: str) -> None:
    """`client.start()` with what Telethon itself prints to stdout kept off
    the screen -- "Signed in successfully as ...; remember to not break the
    ToS or you will risk an account ban!" (final review F29) -- so only
    xaucopier's own sentences show. The login code's prompt still shows:
    its callback puts the real stdout back while it asks (input() writes
    its prompt there). The 2FA password's needs nothing: getpass writes
    its prompt to the console itself, or to stderr, never to stdout.
    Telethon's "Invalid code. Please try again." goes to stderr and stays."""
    shown = sys.stdout

    def code() -> str:
        with contextlib.redirect_stdout(shown):
            return _prompt_login_code()

    with contextlib.redirect_stdout(io.StringIO()):
        await client.start(  # type: ignore[attr-defined]
            phone=phone, code_callback=code, password=_prompt_2fa_password
        )


async def _disconnect_quietly(client: object) -> None:
    try:
        await client.disconnect()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 -- the login's own outcome is what matters
        pass


async def _run_setup_login(path: Path) -> int:
    print("xaucopier setup-login")
    print(
        "Get api_id/api_hash from https://my.telegram.org (API development "
        "tools) -- these are specific to your phone number. Do not reuse a "
        "sample value from a tutorial: Telegram blocks those for good, and this "
        "program with them."
    )

    api_id_raw = input("api_id: ").strip()
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print(
            "api_id must be a whole number -- the api_id shown at "
            f"https://my.telegram.org. Nothing was saved; {_AGAIN}.",
            file=sys.stderr,
        )
        return exit_codes.EXIT_FAILED

    api_hash = getpass.getpass("api_hash (hidden): ").strip()
    if not api_hash:
        print(
            "The api_hash was empty. Paste the api_hash shown at https://my.telegram.org "
            f"(nothing appears while you paste; that is normal). Nothing was saved; {_AGAIN}.",
            file=sys.stderr,
        )
        return exit_codes.EXIT_FAILED
    phone = input("Phone number, with country code (e.g. +15551234567): ").strip()
    if not phone:
        print(
            "The phone number was empty. Enter it starting with + and your country "
            f"code, for example +2547..., with no spaces. Nothing was saved; {_AGAIN}.",
            file=sys.stderr,
        )
        return exit_codes.EXIT_FAILED

    # A blank StringSession -- build_client() only needs api_id/api_hash
    # at this point; client.start() below is what actually produces a
    # session string, via client.session.save() once login succeeds.
    client = build_client(SecretsBlob(api_id=api_id, api_hash=api_hash, session_string=""))

    session_string: str | None = None
    try:
        try:
            # Only the connect is bounded (F24), never the time the user
            # takes to type the code or the password inside start().
            if not client.is_connected():
                await connect_one_shot(client)
            await _start_quietly(client, phone=phone)
            # Defence in depth: `start()` returning is not proof of a login
            # (a blank 2FA password made it return without one). Never save
            # a session Telegram does not recognise as logged in.
            authorized = await client.is_user_authorized()
        except Exception as exc:  # noqa: BLE001 -- every failure is explained, none escapes
            return _print_failure(exc, accepted=_login_accepted(client))
        if not authorized:
            print(
                "Telegram did not log this program in, although no error was reported. "
                f"Nothing was saved; {_AGAIN}.",
                file=sys.stderr,
            )
            return exit_codes.EXIT_FAILED

        session_string = client.session.save()
    finally:
        # Always disconnect -- the terminal-error branch above used to
        # return without this, leaking a connection and producing a
        # noisy "Task was destroyed but it is pending" warning on exit.
        await _disconnect_quietly(client)

    blob = SecretsBlob(api_id=api_id, api_hash=api_hash, session_string=session_string)
    try:
        save(blob, path)
    except Exception as exc:
        # Telegram-side login already succeeded at this point: a live
        # 'XAUCOPIER-TRADEBOX' device now exists on the account with no
        # local copy of its credential. Say so plainly rather than
        # emitting a raw traceback -- the user needs to go revoke it by
        # hand, not decode a stack trace to figure that out. `exc` is
        # save()'s own error message, which (per secrets.py) only ever
        # references the file path, never the secret values.
        print(
            f"Login to Telegram succeeded, but saving the encrypted session "
            f"to {path} failed: {exc}\n{_END_EXTRA_SESSION}",
            file=sys.stderr,
        )
        return exit_codes.EXIT_FAILED

    print(f"Login succeeded. Session saved (DPAPI-encrypted, this user only) to: {path}")
    print(
        "On your phone, check Telegram -> Settings -> Devices: you should "
        "see a session named 'XAUCOPIER-TRADEBOX'. If you do not see it, "
        f"something went wrong: do not start `{paths.USER_COMMAND} watch`, and tell "
        "whoever looks after xaucopier."
    )
    print(f"Next: run `{paths.USER_COMMAND} resolve-channels`.")
    return exit_codes.EXIT_OK


def _cmd_setup_login(args: argparse.Namespace) -> int:
    path = args.path if args.path is not None else DEFAULT_SECRETS_PATH
    try:
        return asyncio.run(_run_setup_login(path))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(
            "\nCancelled with Ctrl+C. Nothing was saved. If you had already typed the "
            "login code, open Telegram -> Settings -> Devices and, if "
            f"XAUCOPIER-TRADEBOX is listed, terminate it before you {_AGAIN}.",
            file=sys.stderr,
        )
        return exit_codes.EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 -- the last line of defence: never a traceback
        return _print_failure(exc)


def _add_config_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=paths.DEFAULT_CONFIG_DIR,
        help="Folder holding channels.toml and accounts.toml (default: %(default)s).",
    )


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=paths.DEFAULT_DB_PATH,
        help="The recordings database (default: %(default)s).",
    )


def _add_secrets(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--secrets",
        type=Path,
        default=DEFAULT_SECRETS_PATH,
        help="The encrypted Telegram session written by setup-login (default: %(default)s).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xaucopier",
        description=(
            "Records XAUUSD signals from Telegram. This version is WATCH-ONLY: "
            "it places no orders. Start with docs/RUNBOOK.md."
        ),
        epilog=exit_codes.help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_login = subparsers.add_parser(
        "setup-login",
        help="One-time interactive Telegram login; writes the DPAPI-encrypted session.",
    )
    setup_login.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Override the secrets file path (default: config/secrets/session.dpapi).",
    )
    setup_login.set_defaults(func=_cmd_setup_login)

    resolve = subparsers.add_parser(
        "resolve-channels",
        help="One-time: look up each channel's Telegram id and switch it on in channels.toml.",
        description=(
            "For every channel in channels.toml whose channel_id is still 0, look "
            "up its Telegram id -- from a t.me link or @username you paste, or by "
            "choosing it from the channels you have joined -- and write it into "
            "channels.toml, switching the channel on. Connects to Telegram once; "
            "watch never looks channels up by name."
        ),
        epilog=exit_codes.help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_config_dir(resolve)
    _add_secrets(resolve)
    resolve.add_argument(
        "--link",
        default=None,
        help="The channel's t.me link or @username, instead of being asked "
        "(only when exactly one channel needs resolving).",
    )
    resolve.add_argument(
        "--yes",
        action="store_true",
        help="Save what was found without asking for confirmation (a group is still "
        "asked about).",
    )
    resolve.add_argument(
        "--allow-group",
        action="store_true",
        help="Use a group without asking. In a group anyone can post, and their "
        "messages are read as trading instructions.",
    )
    resolve.set_defaults(func=cmd_resolve_channels)

    channels = subparsers.add_parser(
        "channels",
        help="Choose the channels signals come from: list, add, or switch one on or off.",
        description=(
            "With nothing after it, lists every channel in channels.toml: its "
            "number, name, ON or OFF, whether its id has been looked up, its parser, "
            "its newest recorded message and how many signals it produced (this only "
            "reads). `channels on NUMBER` / `channels off NUMBER` switch that channel "
            "on or off; NUMBER is the one the list shows. None of these contact "
            "Telegram. `channels add` adds a new channel: it looks the channel up on "
            "Telegram once (paste its link, or pick it from the channels you have "
            "joined), so watch must be stopped first. watch reads the list when it "
            "starts."
        ),
        epilog=exit_codes.help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    channels.add_argument(
        "action",
        nargs="?",
        choices=("add", "on", "off"),
        default=None,
        help="add: add a new channel. on / off: switch channel NUMBER on or off. "
        "Leave out to list the channels.",
    )
    channels.add_argument(
        "number",
        nargs="?",
        type=int,
        default=None,
        help="The channel's number, as the list shows it.",
    )
    _add_config_dir(channels)
    _add_db(channels)
    _add_secrets(channels)
    channels.add_argument(
        "--link",
        default=None,
        help="For add: the channel's t.me link or @username, instead of being asked.",
    )
    channels.add_argument(
        "--yes",
        action="store_true",
        help="For add: add what was found without asking for confirmation (a group is "
        "still asked about).",
    )
    channels.add_argument(
        "--allow-group",
        action="store_true",
        help="For add: add a group without asking. In a group anyone can post, and their "
        "messages are read as trading instructions.",
    )
    channels.set_defaults(func=cmd_channels)

    watch = subparsers.add_parser(
        "watch",
        help="Record every signal from the switched-on channels. Places NO orders.",
        description=(
            "Connects to Telegram and records every message from the channels "
            "switched on in channels.toml, checking the channel's history every 5 "
            "seconds for anything the live feed drops. WATCH-ONLY: it places no "
            "orders and contains no code that could. Runs until stopped (Ctrl+C)."
        ),
        epilog=exit_codes.help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_config_dir(watch)
    _add_db(watch)
    _add_secrets(watch)
    watch.set_defaults(func=cmd_watch)

    status = subparsers.add_parser(
        "status",
        help="Show what watch has recorded, any alerts, and whether it is healthy. Read-only.",
        description=(
            "Reports what watch has recorded and whether it is healthy. It only "
            "reads: the database is opened read-only and nothing is changed."
        ),
        epilog=exit_codes.help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_config_dir(status)
    _add_db(status)
    _add_secrets(status)
    status.set_defaults(func=cmd_status)

    ack = subparsers.add_parser(
        "ack",
        help="List the alerts status shows; clear one (ack NUMBER) or all (ack --all).",
        description=(
            "With no arguments, lists every uncleared alert. `ack NUMBER` clears the "
            "alert with that number (the one after # in the list); `ack --all` clears "
            "them all. Clear an alert once you have dealt with it: TELEGRAM_... alerts "
            "clear themselves once watch reads the channel again."
        ),
        epilog=exit_codes.help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_db(ack)
    which = ack.add_mutually_exclusive_group()
    which.add_argument(
        "alert_id", nargs="?", type=int, default=None, help="The alert's number, from the list."
    )
    which.add_argument("--all", action="store_true", help="Clear every uncleared alert.")
    ack.set_defaults(func=cmd_ack)

    export = subparsers.add_parser(
        "export-fixtures",
        help="Write every recorded message out as a replay-test fixture.",
        description=(
            "Writes every recorded message into the replay harness's fixture folder "
            "as one JSON file each. Never overwrites a file, and skips a message "
            "any existing fixture already covers."
        ),
        epilog=exit_codes.help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_db(export)
    export.add_argument(
        "--out",
        type=Path,
        default=paths.DEFAULT_FIXTURES_DIR,
        help="Folder to write the fixtures into (default: %(default)s).",
    )
    export.set_defaults(func=cmd_export_fixtures)

    return parser


def _replace_what_cannot_be_shown() -> None:
    """Make stdout and stderr replace a character their encoding cannot
    show (an emoji in a channel title, written to a file or a pipe, which
    Python encodes as cp1252 on Windows) with "?", instead of raising
    UnicodeEncodeError -- a traceback in the middle of `watch`'s banner
    (final review F32). One guard for every command. A stream that cannot
    be reconfigured is left as it is."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # e.g. a stream already closed
            pass


def main(argv: list[str] | None = None) -> int:
    """Run one command. A command that does not finish normally ends with
    "(exit code N)" -- the number the runbook's table explains (final
    review F26) -- except `watch`, which says its code itself on every way
    it stops ("watch stopped (exit code 4: ...)"), so it is said once."""
    _replace_what_cannot_be_shown()
    parser = build_parser()
    args = parser.parse_args(argv)
    code = args.func(args)
    if code != exit_codes.EXIT_OK and args.func is not cmd_watch:
        print(f"(exit code {code})", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
