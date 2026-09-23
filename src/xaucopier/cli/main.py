"""xaucopier command-line entry point.

The commands, in the order the user runs them (docs/RUNBOOK.md):

- `setup-login` (Task 7) -- the one-time interactive Telegram login that
  produces the DPAPI-encrypted session every other Telegram command uses.
- `resolve-channels` (`cli/resolve.py`) -- look up each placeholder
  channel's numeric id once and write it into `config/channels.toml`.
- `watch` (`cli/watch.py`) -- the deliverable: record every signal,
  place no order. Nothing reachable from here imports `MetaTrader5`.
- `status` (`cli/status.py`) -- a read-only report on what `watch` has
  recorded and whether it is healthy.
- `export-fixtures` (`cli/fixtures.py`) -- write every recorded message
  out as a replay fixture for the Plan 1 harness.

Every exit code any of them returns is in `cli/exit_codes.py`, and is
printed at the bottom of `--help`.

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
import getpass
import sys
from pathlib import Path

from xaucopier.cli import exit_codes, paths
from xaucopier.cli.watch import cmd_watch
from xaucopier.ingest.client import EXIT_TERMINAL_AUTH, TERMINAL_AUTH_ERRORS, build_client
from xaucopier.ingest.secrets import DEFAULT_SECRETS_PATH, SecretsBlob, save


def _prompt_login_code() -> str:
    return input("Enter the login code Telegram just sent you: ").strip()


def _prompt_2fa_password() -> str:
    return getpass.getpass(
        "Enter your Telegram 2FA (cloud) password, or leave blank if you "
        "don't have one: "
    )


async def _run_setup_login(path: Path) -> int:
    print("xaucopier setup-login")
    print(
        "Get api_id/api_hash from https://my.telegram.org (API development "
        "tools) -- these are specific to your phone number. Do not reuse a "
        "sample value from a tutorial; it will get the client permanently "
        "flood-blocked."
    )

    api_id_raw = input("api_id: ").strip()
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("api_id must be an integer.", file=sys.stderr)
        return 1

    api_hash = getpass.getpass("api_hash (hidden): ").strip()
    phone = input("Phone number, with country code (e.g. +15551234567): ").strip()

    # A blank StringSession -- build_client() only needs api_id/api_hash
    # at this point; client.start() below is what actually produces a
    # session string, via client.session.save() once login succeeds.
    client = build_client(SecretsBlob(api_id=api_id, api_hash=api_hash, session_string=""))

    session_string: str | None = None
    try:
        try:
            await client.start(
                phone=phone,
                code_callback=_prompt_login_code,
                password=_prompt_2fa_password,
            )
        except TERMINAL_AUTH_ERRORS as exc:
            print(
                f"Login failed with a terminal error ({type(exc).__name__}): "
                "this cannot be retried. Check the account is not banned or "
                "restricted, then run `setup-login` again.",
                file=sys.stderr,
            )
            return EXIT_TERMINAL_AUTH

        session_string = client.session.save()
    finally:
        # Always disconnect -- the terminal-error branch above used to
        # return without this, leaking a connection and producing a
        # noisy "Task was destroyed but it is pending" warning on exit.
        await client.disconnect()

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
            f"to {path} failed: {exc}\n"
            "IMPORTANT: a live 'XAUCOPIER-TRADEBOX' session now exists on "
            "your Telegram account with no local copy of its credential. "
            "Go to Telegram -> Settings -> Devices and terminate that "
            "session by hand, then run `setup-login` again.",
            file=sys.stderr,
        )
        return 1

    print(f"Login succeeded. Session saved (DPAPI-encrypted, this user only) to: {path}")
    print(
        "On your phone, check Telegram -> Settings -> Devices: you should "
        "see a session named 'XAUCOPIER-TRADEBOX'. If you don't see it, "
        "something is wrong -- do not proceed to run the listener."
    )
    print("Next: run `xaucopier resolve-channels`.")
    return 0


def _cmd_setup_login(args: argparse.Namespace) -> int:
    path = args.path if args.path is not None else DEFAULT_SECRETS_PATH
    return asyncio.run(_run_setup_login(path))


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
