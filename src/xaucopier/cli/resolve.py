"""`xaucopier resolve-channels` -- look up each channel's numeric id, once.

`config/channels.toml` ships with the channel as a placeholder
(`channel_id = 0`, `enabled = false`). This command connects to Telegram
once, finds the channel -- from a t.me link or @username the user pastes
(`client.get_entity`), or by the user choosing it from the channels the
account has joined (`client.get_dialogs`), which is the only way for a
private channel whose subscribers see no link -- and writes the numeric
id into `channels.toml`, switching the channel on. **This is the only
place `get_entity` is ever called**; `watch` routes by the numeric id
alone and never resolves anything at runtime.

**The file is never left enabled with id 0.** The Task 1 loader rejects
an enabled channel whose `channel_id` is 0, so the id must land before
the flag flips. `set_id_then_enable()` makes the two edits in that order
and checks each intermediate text against the loader's rule, so not even
an in-memory step produces that state. `write_config_atomically()` then
writes the result to a temporary file, flushes it to disk, loads it with
the real loader and compares it with the original -- only the one
channel's `channel_id` and `enabled` may differ -- and only then
`os.replace()`s it over `channels.toml`. The file on disk is therefore
always either exactly the old one or the fully checked new one; a
failure at any step leaves it untouched and removes the temporary file.

**Two commands writing at once cannot both think they won** (final review
F33). Each write has its own temporary file, and the write -- temporary
file, checks, "is the file still what this command read?", replace -- is
one step no other xaucopier command can enter (`config_write_lock()`, a
short-lived named mutex of its own, never the session's). The later of
two commands therefore finds the file changed and writes nothing, and
says so (`ChangedMeanwhileError`, exit 1: run it again). A failure that
running the command again can fix -- that, a write that had to wait too
long, or the disk refusing the write or the rename -- is exit 1; a file
the edit or the loader cannot accept is exit 5 (`save_failure_code()`,
final review F30).

**`channels on` / `channels off` / `channels add` (`cli/channels.py`) write
through the same path.** Their text edits (`set_enabled()`,
`append_channel()`) live here beside `set_id_then_enable()`, and every one
of them is saved by `write_config_atomically()`: there is one writer.

**Comments and layout are kept.** Python ships a TOML reader but no
writer, and no round-tripping TOML library is installed, so the edit is
done on the text: inside the channel's own `[[channels]]` block, the
`channel_id = 0` line and the `enabled = false` line are rewritten in
place (their now-stale trailing comments replaced); every other byte of
the file is kept. A layout this cannot edit safely (the key missing,
repeated, or written some other way) is refused with instructions to
edit the file by hand -- never guessed at.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import os
import re
import sqlite3
import sys
import time
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pywintypes
import win32api
import win32event
import winerror
from telethon import errors, utils
from telethon.tl import types

from xaucopier.cli import channel_reading, exit_codes, paths
from xaucopier.cli.status import database_problem
from xaucopier.cli.watch import SetupError, load_channels_config, load_session
from xaucopier.config.channels import ChannelCfg, load_channels
from xaucopier.ingest.client import (
    API_CREDENTIAL_ERRORS,
    PERMANENT_TELEGRAM_ERRORS,
    AlreadyRunningError,
    SourceKind,
    TelegramUnreachableError,
    acquire_single_instance_lock,
    build_client,
    connect_within,
    source_kind,
)

#: `channel_id` of a channel whose id has not been looked up yet.
PLACEHOLDER_ID = 0

#: How long `setup-login`, `channels add` and `resolve-channels` wait for
#: the connection to Telegram before they give up with exit 1 (final
#: review F24; ruling F06). Telethon's own connect never gives up, so with
#: no network these commands ran for ever, holding the session.
CONNECT_TIMEOUT_SECONDS = 30.0

#: Telegram failures that are neither permanent (`PERMANENT_TELEGRAM_ERRORS`)
#: nor a lookup that found nothing: no connection, a wait Telegram asks
#: for, Telegram's own trouble. `report_telegram_trouble()` says each.
TELEGRAM_TROUBLE: tuple[type[BaseException], ...] = (
    TelegramUnreachableError,
    errors.RPCError,
    ConnectionError,
    TimeoutError,
    OSError,
)

#: What a lookup by link must never call "not found" (final review F23): a
#: wait Telegram asks for, Telegram's own trouble -- a 500 or a -500
#: (`ServerError`), a -503 timeout (`TimedOutError`, not a `ServerError`)
#: -- and no connection.
NOT_A_LOOKUP_FAILURE: tuple[type[BaseException], ...] = (
    errors.FloodError,
    errors.ServerError,
    errors.TimedOutError,
    TelegramUnreachableError,
    ConnectionError,
    TimeoutError,
)

_HEADER = re.compile(r"^\s*\[")
_CHANNELS_HEADER = re.compile(r"^\s*\[\[\s*channels\s*\]\]\s*(#.*)?$")
_ID_LINE = re.compile(r"^(?P<lead>\s*channel_id\s*=\s*)(?P<value>0)(?P<gap>\s*)(?:#.*)?$")
_ENABLED_LINE = re.compile(
    r"^(?P<lead>\s*enabled\s*=\s*)(?P<value>false)(?P<gap>\s*)(?:#.*)?$"
)
#: The switch, either way (`channels on` / `channels off`).
_SWITCH_LINE = re.compile(
    r"^(?P<lead>\s*enabled\s*=\s*)(?P<value>true|false)(?P<gap>\s*)(?:#.*)?$"
)
#: A pasted post link (t.me/name/123) resolves as its channel (t.me/name).
_POST_LINK = re.compile(
    r"^(?P<channel>(?:https?://)?(?:www\.)?t(?:elegram)?\.me/(?!joinchat/|c/)[A-Za-z0-9_]+)/\d+$"
)
#: A link to a post in a *private* channel (t.me/c/<id>/<post>) names no
#: channel Telegram can look up by link.
_PRIVATE_POST_LINK = re.compile(r"(?:^|/)t(?:elegram)?\.me/c/\d+")


class ResolveError(Exception):
    """This channel could not be resolved or saved; the message says why."""


class ChangedMeanwhileError(ResolveError):
    """`channels.toml` is no longer the text this command read: something
    else -- another xaucopier command, or a person editing it -- changed it
    meanwhile. Nothing was written; running the command again is the fix."""


class ConfigBusyError(ResolveError):
    """Another xaucopier command was writing `channels.toml` for longer than
    `CONFIG_LOCK_TIMEOUT_SECONDS`. Nothing was written."""


#: How long a write of `channels.toml` waits for another xaucopier command
#: that is writing it (each write holds it for milliseconds).
CONFIG_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_RETRY_SECONDS = 0.05


def config_lock_name(path: Path) -> str:
    """The name of the write lock for the file at `path`: one per file,
    however the path is spelled."""
    key = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()
    return f"Local\\xaucopier_config_{key[:40]}"


@contextlib.contextmanager
def config_write_lock(path: Path) -> Iterator[None]:
    """Hold the write lock for the file at `path` -- a named mutex of its
    own, never the session's `Global\\xaucopier_listener`, so `channels on`
    and `off` still run while `watch` does. Waits up to
    `CONFIG_LOCK_TIMEOUT_SECONDS`, then `ConfigBusyError`. A mutex this
    window may not open (held by a command in an administrator window) is
    waited for the same way. Windows releases it if its holder dies."""
    name = config_lock_name(path)
    deadline = time.monotonic() + CONFIG_LOCK_TIMEOUT_SECONDS
    busy = ConfigBusyError(
        f"another xaucopier command is changing {path.name} right now; nothing was written"
    )
    while True:
        try:
            handle = win32event.CreateMutex(None, False, name)
            break
        except pywintypes.error as exc:
            if exc.winerror != winerror.ERROR_ACCESS_DENIED:
                raise
            if time.monotonic() >= deadline:
                raise busy from None
            time.sleep(_LOCK_RETRY_SECONDS)
    try:
        wait_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if win32event.WaitForSingleObject(handle, wait_ms) not in (
            win32event.WAIT_OBJECT_0,
            win32event.WAIT_ABANDONED,  # its holder died: the lock is ours
        ):
            raise busy
        try:
            yield
        finally:
            win32event.ReleaseMutex(handle)
    finally:
        win32api.CloseHandle(handle)


def save_failure_code(exc: BaseException) -> int:
    """The exit code for a failed save of `channels.toml` (final review
    F30): 1 when running the command again can fix it -- the file changed
    meanwhile, another command held the write too long, or the disk refused
    the write or the rename (another program had the file open) -- and 5
    for a file the edit or the loader cannot accept, which only a fix to
    the file can."""
    if isinstance(exc, (ChangedMeanwhileError, ConfigBusyError, OSError)):
        return exit_codes.EXIT_FAILED
    return exit_codes.EXIT_CONFIG_ERROR


@dataclass(frozen=True, slots=True)
class Resolved:
    channel_id: int
    title: str
    #: What Telegram says it is (`client.source_kind`): "channel" (only its
    #: admins post), "group" (anyone in it posts) or "unknown" (final
    #: review F05).
    kind: SourceKind = "unknown"
    #: This account has not joined it (a public channel found by link).
    left: bool = False


# -- the file edit --------------------------------------------------------------


def _line_body(line: str) -> tuple[str, str]:
    """`line` split into its text and its line ending (kept as it was)."""
    stripped = line.rstrip("\r\n")
    return stripped, line[len(stripped):]


def _channel_block(lines: list[str], index: int) -> tuple[int, int]:
    """The line range of the `index`-th `[[channels]]` table: its header
    line (exclusive) to the next table header or the end of the file."""
    seen = -1
    for number, line in enumerate(lines):
        if _CHANNELS_HEADER.match(_line_body(line)[0]):
            seen += 1
            if seen == index:
                end = next(
                    (n for n in range(number + 1, len(lines)) if _HEADER.match(lines[n])),
                    len(lines),
                )
                return number + 1, end
    raise ResolveError(f"channels.toml has no [[channels]] table number {index + 1}")


def _rewrite_one(
    lines: list[str],
    block: tuple[int, int],
    pattern: re.Pattern[str],
    value: str,
    comment: str,
    key: str,
) -> list[str]:
    """`lines` with the one line in `block` that `pattern` matches set to
    `value` (replacing the pattern's `value` group), its trailing comment
    replaced by `comment`. Anything but exactly one such line is refused."""
    matches = [
        number for number in range(*block) if pattern.match(_line_body(lines[number])[0])
    ]
    if len(matches) != 1:
        raise ResolveError(
            f"channels.toml is laid out in a way xaucopier cannot edit safely "
            f"(found {len(matches)} plain `{key} = ...` lines it could change in that "
            f"channel's section, expected exactly one)"
        )
    number = matches[0]
    body, ending = _line_body(lines[number])
    match = pattern.match(body)
    # Keep the trailing comment in its column when the new value fits.
    gap = " " * max(1, len(match["gap"]) - (len(value) - len(match["value"])))
    updated = list(lines)
    updated[number] = f"{match['lead']}{value}{gap}# {comment}{ending}"
    return updated


def _check_no_enabled_placeholder(text: str) -> dict[str, Any]:
    """The Task 1 loader's rule, applied to an intermediate text: it must
    parse, and no enabled channel may have `channel_id = 0`."""
    data = tomllib.loads(text)
    for entry in data.get("channels", []):
        if entry.get("enabled") and entry.get("channel_id") == PLACEHOLDER_ID:
            raise ResolveError(
                f"internal error: an edit step left channel {entry.get('name')!r} "
                "switched on with channel_id = 0; nothing was written"
            )
    return data


def _safe_comment(text: str) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in text)


def set_id_then_enable(text: str, *, index: int, channel_id: int, title: str, today: str) -> str:
    """`text` with the `index`-th channel's `channel_id = 0` set to
    `channel_id`, and then its `enabled = false` set to true. The id first:
    each step is checked against the loader's rule, so a step that
    enabled a channel still at id 0 raises instead of returning."""
    if channel_id == PLACEHOLDER_ID:
        raise ResolveError("refusing to write channel_id = 0")
    lines = text.splitlines(keepends=True)
    block = _channel_block(lines, index)
    lines = _rewrite_one(
        lines,
        block,
        _ID_LINE,
        str(channel_id),
        f"{_safe_comment(title)} -- looked up by resolve-channels on {today}",
        "channel_id",
    )
    _check_no_enabled_placeholder("".join(lines))
    lines = _rewrite_one(
        lines,
        block,
        _ENABLED_LINE,
        "true",
        "switched on by resolve-channels",
        "enabled",
    )
    new_text = "".join(lines)
    _check_no_enabled_placeholder(new_text)
    return new_text


#: The parser a channel added by `channels add` is given: the only one built.
NEW_CHANNEL_PARSER = "mmh"


def _toml_string(value: str) -> str:
    """`value` as a TOML basic string: quotes, backslashes and control
    characters escaped; everything else (an emoji in a channel title)
    written as it is."""
    out = []
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def append_channel(text: str, *, channel_id: int, name: str, ordinal: int, today: str) -> str:
    """`text` with a new `[[channels]]` block added at the end (for
    `channels add`): `channel_id`, `name`, `parser = "mmh"`, `ordinal` and
    `enabled = true`. Every byte of `text` is kept; the block uses the
    file's own line ending. Checked against the loader's rule."""
    if channel_id == PLACEHOLDER_ID:
        raise ResolveError("refusing to write channel_id = 0")
    ending = "\r\n" if "\r\n" in text else "\n"
    lead = text if not text or text.endswith("\n") else text + ending
    block = (
        "",
        f"# {_safe_comment(name)} -- added by channels add on {today}",
        "[[channels]]",
        f"channel_id = {channel_id}",
        f"name       = {_toml_string(name)}",
        f'parser     = "{NEW_CHANNEL_PARSER}"      # reads only the MARKET MASTERY HUB layout',
        f"ordinal    = {ordinal}",
        "enabled    = true",
    )
    new_text = lead + ending.join(block) + ending
    _check_no_enabled_placeholder(new_text)
    return new_text


def set_enabled(text: str, *, index: int, enabled: bool, comment: str) -> str:
    """`text` with the `index`-th channel's `enabled = ...` line set to
    `enabled` (for `channels on` / `channels off`), its trailing comment
    replaced by `comment`. Checked against the loader's rule: switching on
    a channel still at `channel_id = 0` raises instead of returning."""
    lines = text.splitlines(keepends=True)
    block = _channel_block(lines, index)
    lines = _rewrite_one(
        lines,
        block,
        _SWITCH_LINE,
        "true" if enabled else "false",
        _safe_comment(comment),
        "enabled",
    )
    new_text = "".join(lines)
    _check_no_enabled_placeholder(new_text)
    return new_text


def _fsync_write(path: Path, data: bytes) -> None:
    with open(path, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_config_atomically(
    path: Path,
    *,
    original: str,
    new_text: str,
    expect: Callable[[dict[str, Any]], None],
    confirm: Callable[[tuple[ChannelCfg, ...]], bool],
    change: str,
) -> None:
    """Replace `path` (whose text was `original`) with `new_text` -- all of
    it or none of it. The one way `channels.toml` is ever written.

    `expect` makes the intended change -- and only it -- to a copy of
    `original` as parsed; `change` says in words what that change is.
    `new_text` goes to a temporary file beside `path`, flushed to disk,
    and is read back: parsed, it must equal that copy exactly, so nothing
    else in the file changed. It is then loaded through the real loader
    (`load_channels`) and `confirm` must accept what loaded. Last, `path`
    must still hold `original` -- nothing else changed it meanwhile
    (`ChangedMeanwhileError`). Only then is the temporary file
    `os.replace()`d over `path`, which is atomic on NTFS. The temporary
    file is this call's own, and everything from writing it to the replace
    runs under `config_write_lock()`, so no other xaucopier command can
    pass the same check in between. On any failure the temporary file is
    removed and `path` is exactly as it was."""
    expected = copy.deepcopy(tomllib.loads(original))
    expect(expected)

    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with config_write_lock(path):
        try:
            _fsync_write(tmp, new_text.encode("utf-8"))
            written = tomllib.loads(tmp.read_bytes().decode("utf-8"))
            if written != expected:
                raise ResolveError(
                    "internal error: the edited channels.toml differs from the original in "
                    f"more than {change}; nothing was written"
                )
            loaded = load_channels(tmp)
            if not confirm(loaded):
                raise ResolveError("internal error: the edited channel did not load as expected")
            if path.read_bytes() != original.encode("utf-8"):
                raise ChangedMeanwhileError(
                    "channels.toml was changed by something else while this command ran"
                )
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def save_resolved(path: Path, *, index: int, resolved: Resolved, today: str) -> None:
    original = path.read_bytes().decode("utf-8")
    new_text = set_id_then_enable(
        original, index=index, channel_id=resolved.channel_id, title=resolved.title, today=today
    )

    def expect(data: dict[str, Any]) -> None:
        data["channels"][index]["channel_id"] = resolved.channel_id
        data["channels"][index]["enabled"] = True

    write_config_atomically(
        path,
        original=original,
        new_text=new_text,
        expect=expect,
        confirm=lambda loaded: (
            loaded[index].channel_id == resolved.channel_id and loaded[index].enabled
        ),
        change="the one channel's id and switch",
    )


def save_switch(
    path: Path, *, original: str, index: int, ordinal: int, enabled: bool, today: str
) -> None:
    """Switch the `index`-th channel -- number `ordinal` -- on or off in
    `path`, whose text was `original`, through `write_config_atomically`."""
    word = "on" if enabled else "off"
    new_text = set_enabled(
        original,
        index=index,
        enabled=enabled,
        comment=f"switched {word} on {today} (channels {word} {ordinal})",
    )

    def expect(data: dict[str, Any]) -> None:
        data["channels"][index]["enabled"] = enabled

    write_config_atomically(
        path,
        original=original,
        new_text=new_text,
        expect=expect,
        confirm=lambda loaded: (
            loaded[index].ordinal == ordinal and loaded[index].enabled is enabled
        ),
        change=f"channel {ordinal}'s switch",
    )


def save_added(path: Path, *, original: str, resolved: Resolved, ordinal: int, today: str) -> None:
    """Add `resolved` to `path` (whose text was `original`) as channel
    number `ordinal`, switched on, through `write_config_atomically`. The
    new channel must load as the last one, and as the only one with its id."""
    new_text = append_channel(
        original,
        channel_id=resolved.channel_id,
        name=resolved.title,
        ordinal=ordinal,
        today=today,
    )
    entry = {
        "channel_id": resolved.channel_id,
        "name": resolved.title,
        "parser": NEW_CHANNEL_PARSER,
        "ordinal": ordinal,
        "enabled": True,
    }

    def expect(data: dict[str, Any]) -> None:
        data.setdefault("channels", []).append(dict(entry))

    def confirm(loaded: tuple[ChannelCfg, ...]) -> bool:
        last = loaded[-1] if loaded else None
        return (
            last is not None
            and (last.channel_id, last.ordinal, last.parser, last.enabled)
            == (resolved.channel_id, ordinal, NEW_CHANNEL_PARSER, True)
            and sum(1 for channel in loaded if channel.channel_id == resolved.channel_id) == 1
        )

    write_config_atomically(
        path,
        original=original,
        new_text=new_text,
        expect=expect,
        confirm=confirm,
        change="the one new channel",
    )


# -- Telegram ---------------------------------------------------------------------


def _as_channel(entity: object, *, asked: str) -> Resolved:
    if not isinstance(entity, types.Channel):
        raise ResolveError(
            f"{asked!r} is not a channel (it is a {type(entity).__name__}); paste the "
            "channel's own link"
        )
    return _resolved(entity)


def _resolved(entity: types.Channel) -> Resolved:
    return Resolved(
        channel_id=utils.get_peer_id(entity),
        title=entity.title,
        kind=source_kind(entity),
        left=bool(getattr(entity, "left", False)),
    )


#: What each kind is called in the picker and in "Found: ...".
KIND_WORDS: dict[str, str] = {"channel": "channel", "group": "group", "unknown": "kind unknown"}

#: Said, and asked about, before a group is used (final review F05, ruling
#: C10): nothing filters who posted, so every member's message is read.
GROUP_WARNING = (
    "This is a group: anyone in it can post, and their messages will be read as trading "
    "instructions."
)
#: The same, for a source Telegram describes as neither.
UNKNOWN_KIND_WARNING = (
    "Telegram does not say whether this is a channel or a group. If it is a group, anyone "
    "in it can post, and their messages will be read as trading instructions."
)
#: The one answer that uses a group anyway.
GROUP_QUESTION = "Type yes to use it anyway: "


def found_line(resolved: Resolved) -> str:
    """"Found: <title> -- a channel, Telegram id -100..." (F05, F27)."""
    kind = resolved.kind if resolved.kind in ("channel", "group") else "source of unknown kind"
    return (
        f"Found: {_safe_comment(resolved.title)} -- a {kind}, Telegram id "
        f"{resolved.channel_id}"
    )


async def accept_source(resolved: Resolved, *, allow_group: bool) -> bool:
    """Warn about what `resolved` is, and for anything but a channel ask
    once more -- apart from, and before, the ordinary confirmation, which
    `--yes` skips: only the answer `yes`, or `--allow-group` on the
    command line, uses a group (final review F05). A closed input is
    EOFError, which the command reports: nothing is saved. Also warns when
    this account has not joined it. False if the user said no."""
    if resolved.left:
        print(
            f"NOTE: This Telegram account has not joined {_safe_comment(resolved.title)}. Join "
            "it in the Telegram app first: until then, watch sees its posts only through "
            "the 5-second check, not the moment they are posted."
        )
    if resolved.kind == "channel":
        return True
    print()
    print(f"WARNING: {GROUP_WARNING if resolved.kind == 'group' else UNKNOWN_KIND_WARNING}")
    if allow_group:
        print("--allow-group was given, so it is used anyway.")
        return True
    return (await _ask(GROUP_QUESTION)).lower() == "yes"


def normalise_link(text: str) -> str:
    """Trim what the user pasted: whitespace and, for a t.me link, a
    `?query`, a trailing slash and a post number (t.me/name/123)."""
    text = text.strip()
    if "t.me/" in text or "telegram.me/" in text:
        text = text.split("?", 1)[0].rstrip("/")
        match = _POST_LINK.match(text)
        if match:
            text = match["channel"]
    return text


async def resolve_link(client: Any, text: str, *, again: str = "resolve-channels") -> Resolved:
    """A t.me link, an invite link (t.me/+..., t.me/joinchat/...) or an
    @username -> the channel. One `get_entity` call. `again` is the
    command the user is told to run again."""
    link = normalise_link(text)
    if _PRIVATE_POST_LINK.search(link):
        raise ResolveError(
            "that is a link to a post in a private channel, which cannot be looked up "
            f"by link; run `{paths.USER_COMMAND} {again}` again and just press "
            "Enter to choose the channel from the list"
        )
    if link.lstrip("+-").isdigit():
        raise ResolveError(
            "that is a number; paste the channel's t.me link or @username instead "
            "(Telegram would read a bare number as a phone number)"
        )
    try:
        entity = await client.get_entity(link)
    except PERMANENT_TELEGRAM_ERRORS:
        raise
    except NOT_A_LOOKUP_FAILURE:
        raise  # a wait, Telegram's own trouble or no connection: not "not found" (F23)
    except Exception as exc:  # noqa: BLE001 -- Telethon raises ValueError or an RPCError
        raise ResolveError(f"Telegram could not find {link!r}: {exc}") from None
    return _as_channel(entity, asked=link)


async def joined_channels(client: Any) -> tuple[Resolved, ...]:
    """Every channel (and supergroup) the account has joined, in the order
    Telegram lists them."""
    dialogs = await client.get_dialogs()
    return tuple(
        _resolved(d.entity)
        for d in dialogs
        if isinstance(getattr(d, "entity", None), types.Channel)
    )


async def _ask(prompt: str) -> str:
    # In a thread, so a slow typist never blocks Telethon's event loop.
    return (await asyncio.to_thread(input, prompt)).strip()


async def _choose(
    client: Any, wanted: str, link: str | None, *, again: str = "resolve-channels"
) -> Resolved | None:
    """The channel the user means: from `link`, else from what they paste,
    else picked from the channels the account has joined. None if they
    cancel. `wanted` names it in the prompt; `again` is the command."""
    answer = link if link is not None else await _ask(
        "Paste the channel's t.me link or @username -- or just press Enter to choose it "
        "from the channels you have joined (do that for a private channel): "
    )
    if answer:
        return await resolve_link(client, answer, again=again)
    channels = await joined_channels(client)
    if not channels:
        raise ResolveError("this Telegram account has not joined any channel")
    print("The channels and groups this account has joined:")
    for number, channel in enumerate(channels, start=1):
        print(f"  {number:>3}. {channel.title} ({KIND_WORDS[channel.kind]})")
    choice = await _ask(f"Type the number of {wanted}, or press Enter to cancel: ")
    if not choice:
        return None
    if not choice.isdigit() or not 1 <= int(choice) <= len(channels):
        raise ResolveError(f"{choice!r} is not one of the numbers listed")
    return channels[int(choice) - 1]


def report_not_logged_in() -> int:
    """Say the session is not logged in; the exit code for it (3)."""
    print(
        "This Telegram session is not logged in (it may have been logged out). "
        f"Run `{paths.USER_COMMAND} setup-login` again.",
        file=sys.stderr,
    )
    return exit_codes.EXIT_TERMINAL_AUTH


def build_one_shot_client(blob: Any) -> Any:
    """The client `channels add` and `resolve-channels` use: `build_client`'s,
    except that Telegram's own trouble that outlasts Telethon's retries is
    raised as Telegram's last error -- which `report_telegram_trouble()`
    says plainly -- instead of a bare `ValueError` that escaped as a
    traceback, or was called "not found" for a link (final review F23)."""
    return build_client(blob, raise_last_call_error=True)


async def logged_in(client: Any) -> bool:
    """Whether Telegram still knows this login. Asks who this account is
    (`get_me()`), which answers None only when Telegram says the login is
    not authorised (a 401). Any other error -- a wait Telegram asks for,
    Telegram's own trouble -- is raised, for `report_telegram_trouble()`.
    Telethon's `is_user_authorized()` read every one of those as "not
    logged in", and the user was sent to run setup-login again, which makes
    a second XAUCOPIER-TRADEBOX session and cures nothing (final review
    F23)."""
    return await client.get_me() is not None


async def connect_one_shot(client: Any) -> None:
    """Connect a one-shot command (`setup-login`, `channels add`,
    `resolve-channels`) to Telegram, waiting at most
    `CONNECT_TIMEOUT_SECONDS`; past that, `TelegramUnreachableError`
    (final review F24). Only the connect is bounded -- never the time the
    user takes to type an answer."""
    await connect_within(client, CONNECT_TIMEOUT_SECONDS)


def report_telegram_trouble(exc: BaseException, *, again: str, nothing: str) -> int:
    """One plain sentence for a `TELEGRAM_TROUBLE` failure of a one-shot
    command, and its exit code, 1 (final review F23/F24): Telegram out of
    reach within `CONNECT_TIMEOUT_SECONDS`, a wait Telegram asks for (in
    minutes), or any other Telegram error (named by its number). `again`
    is the command to run again; `nothing` says what was not done."""
    command = f"`{paths.USER_COMMAND} {again}`"
    if isinstance(exc, TelegramUnreachableError):
        text = f"{exc} Then run {command} again. {nothing}"
    elif isinstance(exc, errors.FloodError):
        seconds = getattr(exc, "seconds", None)
        if isinstance(seconds, int | float) and not isinstance(seconds, bool):
            minutes = max(1, round(seconds / 60))
            wait = f"about {minutes} minute{'' if minutes == 1 else 's'}"
        else:
            wait = "a while (an hour is safe)"
        text = f"Telegram asks to wait {wait}, then run it again ({command}). {nothing}"
    elif isinstance(exc, errors.RPCError):
        # Telegram sends some as negative (-500 "no workers", -503 timeout).
        number = abs(exc.code) if isinstance(exc.code, int) else exc.code
        text = (
            f"Telegram could not do this just now (Telegram error {number}). Wait a "
            f"minute, then run {command} again. {nothing}"
        )
    else:
        text = (
            f"Could not reach Telegram ({type(exc).__name__}). Check the internet "
            f"connection, then run {command} again. {nothing}"
        )
    print(text, file=sys.stderr)
    return exit_codes.EXIT_FAILED


def report_already_running(message: str, exc: AlreadyRunningError) -> int:
    """`message`, then the lock's own hint about where the other program
    probably is, if it has one (G2L-1); exit code 6."""
    print(message, file=sys.stderr)
    if exc.hint:
        print(exc.hint, file=sys.stderr)
    return exit_codes.EXIT_ALREADY_RUNNING


def report_permanent_error(exc: BaseException) -> int:
    """One plain sentence for a Telegram error no retry can fix, and its
    exit code: 5 for a rejected api_id/api_hash, 3 for a dead session or
    account. Shared by `resolve-channels` and `channels add`."""
    if isinstance(exc, API_CREDENTIAL_ERRORS):
        print(
            f"Telegram rejects this program's api_id/api_hash ({type(exc).__name__}). "
            "Get a NEW pair at https://my.telegram.org, then run "
            "`.\\xaucopier setup-login` again.",
            file=sys.stderr,
        )
        return exit_codes.EXIT_CONFIG_ERROR
    print(
        f"Telegram refuses this login ({type(exc).__name__}): it was logged out, "
        "used from two places at once, or the account is banned. Run "
        "`.\\xaucopier setup-login` again.",
        file=sys.stderr,
    )
    return exit_codes.EXIT_TERMINAL_AUTH


#: Said when a channel read before is put back (G2L-3; fix round 2).
READ_FROM_NOW = (
    "It was read before, so the next watch reads it from now on: what it posted since it "
    "was last read is skipped."
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _resolve_all(args: argparse.Namespace, client: Any, channels: tuple[ChannelCfg, ...]) -> int:
    channels_path: Path = args.config_dir / paths.CHANNELS_FILE
    pending = [(index, cfg) for index, cfg in enumerate(channels) if cfg.channel_id == PLACEHOLDER_ID]
    if args.link is not None and len(pending) != 1:
        print("--link can only be used when exactly one channel needs resolving.", file=sys.stderr)
        return exit_codes.EXIT_USAGE

    saved = 0

    def nothing() -> str:
        if not saved:
            return "Nothing was changed."
        return f"The {saved} channel(s) saved above are kept; nothing else was changed."

    try:
        # Inside the try: a bad api_id is reported by the connect itself
        # (final review F37); bounded, so no network is not a hang (F24).
        await connect_one_shot(client)
        if not await logged_in(client):
            return report_not_logged_in()
        for index, cfg in pending:
            print(f"\nChannel {cfg.name!r} does not have its Telegram id yet.")
            try:
                resolved = await _choose(client, repr(cfg.name), args.link)
            except ResolveError as exc:
                print(f"Could not resolve {cfg.name!r}: {exc}. {nothing()}", file=sys.stderr)
                return exit_codes.EXIT_FAILED
            if resolved is None:
                print(f"Cancelled. {nothing()}")
                return exit_codes.EXIT_FAILED
            clash = next(
                (c for c in channels if c.channel_id == resolved.channel_id and c is not cfg), None
            )
            if clash is not None:
                print(
                    f"{resolved.title!r} (id {resolved.channel_id}) is already in channels.toml "
                    f"as {clash.name!r}. Nothing was changed.",
                    file=sys.stderr,
                )
                return exit_codes.EXIT_FAILED
            print(found_line(resolved))
            if not await accept_source(resolved, allow_group=args.allow_group):
                print("Not saved. Nothing was changed.")
                return exit_codes.EXIT_FAILED
            if not args.yes:
                confirm = await _ask(
                    f"Save this id for {cfg.name!r} and switch the channel on? Type yes to confirm: "
                )
                if confirm.lower() not in ("y", "yes"):
                    print("Not saved. Nothing was changed.")
                    return exit_codes.EXIT_FAILED
            # A channel read before -- removed, then put back as a placeholder
            # -- is read from now on, the database first, as `channels add`
            # does it (G2L-3; fix round 2). This command holds the session,
            # so no watch is seeding it: a first read pending moves too.
            try:
                reread = channel_reading.reseed_from_now(
                    args.db, resolved.channel_id, _utcnow(), may_replace_first_cutoff=True
                )
            except sqlite3.DatabaseError as exc:
                print(f"{database_problem(args.db, exc)} {nothing()}", file=sys.stderr)
                return exit_codes.EXIT_FAILED
            try:
                save_resolved(
                    channels_path,
                    index=index,
                    resolved=resolved,
                    today=_utcnow().date().isoformat(),
                )
            except Exception as exc:  # noqa: BLE001 -- the writer leaves the file untouched on any failure
                code = save_failure_code(exc)
                if isinstance(exc, ChangedMeanwhileError):
                    outcome = "Nothing was written by this command."
                else:
                    outcome = "The file was not changed."
                again = (
                    f" Run `{paths.USER_COMMAND} resolve-channels` again, or, to"
                    if code == exit_codes.EXIT_FAILED
                    else " To"
                )
                print(
                    f"Could not save to {channels_path}: {exc}\n{outcome}{again} do it by "
                    f"hand, set channel_id = {resolved.channel_id} and then enabled = true "
                    f"in the {cfg.name!r} section.",
                    file=sys.stderr,
                )
                return code
            saved += 1
            # "Channel N" is only ever the number `channels` shows (F27).
            print(
                f"Saved: {cfg.name!r} -- its Telegram id {resolved.channel_id} -- and "
                f"switched it on. It is channel {cfg.ordinal} in "
                f"`{paths.USER_COMMAND} channels`."
            )
            if reread == "reseeded":
                print(READ_FROM_NOW)
    except PERMANENT_TELEGRAM_ERRORS as exc:
        return report_permanent_error(exc)
    except TELEGRAM_TROUBLE as exc:
        return report_telegram_trouble(exc, again="resolve-channels", nothing=nothing())
    finally:
        await client.disconnect()

    print(f"\nDone: {saved} channel(s) resolved. Next: run `{paths.USER_COMMAND} watch`.")
    return exit_codes.EXIT_OK


def cmd_resolve_channels(args: argparse.Namespace) -> int:
    try:
        channels = load_channels_config(args.config_dir)
    except SetupError as exc:
        print(f"resolve-channels cannot start: {exc}", file=sys.stderr)
        return exit_codes.EXIT_CONFIG_ERROR
    if not any(cfg.channel_id == PLACEHOLDER_ID for cfg in channels):
        print("Nothing to do: every channel in channels.toml already has its Telegram id.")
        return exit_codes.EXIT_OK
    try:
        blob = load_session(args.secrets)
    except SetupError as exc:
        print(f"resolve-channels cannot start: {exc}", file=sys.stderr)
        return exit_codes.EXIT_CONFIG_ERROR
    try:
        lock = acquire_single_instance_lock()
    except AlreadyRunningError as exc:
        return report_already_running(
            "xaucopier watch is running (or another xaucopier command is using the "
            "Telegram login). Stop it first -- click in its window, press Ctrl+C and "
            f"answer Y -- then run `{paths.USER_COMMAND} resolve-channels` again: two "
            "programs must never use the Telegram session at once.",
            exc,
        )
    try:
        client = build_one_shot_client(blob)
        return asyncio.run(_resolve_all(args, client, channels))
    except KeyboardInterrupt:
        print()
        print("Cancelled (Ctrl+C). channels.toml was not changed by the channel in progress.")
        return exit_codes.EXIT_INTERRUPTED
    except EOFError:
        print(
            "No answer could be read: the input was closed. Run "
            f"`{paths.USER_COMMAND} resolve-channels` in an ordinary PowerShell window and "
            "type the answers there. The channel it was asking about was not saved.",
            file=sys.stderr,
        )
        return exit_codes.EXIT_FAILED
    finally:
        lock.release()
