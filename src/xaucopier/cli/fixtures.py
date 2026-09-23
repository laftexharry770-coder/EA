"""`xaucopier export-fixtures` -- every recorded message, as a replay fixture.

The Plan 1 replay harness (`replay/harness.py`) is the only regression
net the parser and planner have, and until now it held one hand-checked
message. This writes every message `watch` has stored into
`tests/fixtures/messages/`, one JSON file each, in exactly the shape the
harness reads -- `channel_id`, `message_id`, `posted_at_utc`, `text` --
so real traffic, above all the first real follow-ups, can be replayed.

- **Never overwrites anything.** Each file is written to a temporary name
  and then hard-linked into place, which fails if the name exists; a
  file already there -- a hand-checked fixture, or one a developer edited
  after an earlier export -- is left exactly as it is.
- **Skips a message an existing fixture already covers**, whatever that
  file is called (the hand-checked `mmh_buy_4tp.json` among them), so an
  export never adds a second copy of the same `(channel_id, message_id)`.
- **No `expect` key.** An exported message has not been checked by a
  human; `expect` is the harness's assertion, and only a person may add it.
- The text is the version stored now -- the latest edit, if it was edited.

Reads through `db.connect_readonly()`: exporting never changes the store.

New fixtures change the replay output, so the golden file must be
regenerated (`python -m xaucopier.replay.harness --write-golden`) and the
diff reviewed -- a developer step, which the command's output says.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from xaucopier.cli import exit_codes, paths
from xaucopier.store import db

#: Exported files are named `captured_<channel_id>_<message_id, 10 digits>.json`,
#: so a channel's fixtures sort (and replay) in posting order.
PREFIX = "captured_"


@dataclass(frozen=True, slots=True)
class ExportResult:
    written: tuple[Path, ...]
    #: Messages an existing fixture already covers.
    already_covered: int
    #: Messages whose file name was already taken (left untouched).
    name_taken: int
    #: Existing `*.json` files that could not be read (left untouched).
    unreadable: tuple[Path, ...]


def fixture_name(channel_id: int, message_id: int) -> str:
    return f"{PREFIX}{channel_id}_{message_id:010d}.json"


def _covered(out_dir: Path) -> tuple[set[tuple[int, int]], tuple[Path, ...]]:
    covered: set[tuple[int, int]] = set()
    unreadable: list[Path] = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            covered.add((int(data["channel_id"]), int(data["message_id"])))
        except (OSError, ValueError, KeyError, TypeError):
            unreadable.append(path)
    return covered, tuple(unreadable)


def _write_new(path: Path, payload: bytes) -> bool:
    """Create `path` holding `payload`, complete, or not at all. False (and
    nothing touched) if `path` already exists."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    try:
        with open(tmp, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)  # fails if `path` exists: never an overwrite
        except FileExistsError:
            return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


def export(conn: object, out_dir: Path) -> ExportResult:
    """Write a fixture for every stored message not already covered."""
    covered, unreadable = _covered(out_dir)
    rows = conn.execute(  # type: ignore[attr-defined]
        "SELECT channel_id, message_id, posted_at_utc, raw_text FROM messages "
        "ORDER BY channel_id, message_id"
    ).fetchall()
    written: list[Path] = []
    already = taken = 0
    for channel_id, message_id, posted_at_utc, raw_text in rows:
        if (channel_id, message_id) in covered:
            already += 1
            continue
        fixture = {
            "channel_id": channel_id,
            "message_id": message_id,
            "posted_at_utc": posted_at_utc,
            "text": raw_text,
        }
        payload = (json.dumps(fixture, indent=2, ensure_ascii=True) + "\n").encode("ascii")
        path = out_dir / fixture_name(channel_id, message_id)
        if _write_new(path, payload):
            written.append(path)
            covered.add((channel_id, message_id))
        else:
            taken += 1
    return ExportResult(
        written=tuple(written), already_covered=already, name_taken=taken, unreadable=unreadable
    )


def cmd_export_fixtures(args: argparse.Namespace) -> int:
    out_dir: Path = args.out
    if not out_dir.is_dir():
        print(f"The fixture folder {out_dir} does not exist.", file=sys.stderr)
        return exit_codes.EXIT_FAILED
    try:
        conn = db.connect_readonly(args.db)
    except FileNotFoundError:
        print(f"No recordings yet: there is no database at {args.db}.", file=sys.stderr)
        return exit_codes.EXIT_FAILED
    except db.SchemaVersionError as exc:
        print(
            f"{exc}. Run `{paths.USER_COMMAND} watch` once to bring it up to date.",
            file=sys.stderr,
        )
        return exit_codes.EXIT_FAILED
    try:
        result = export(conn, out_dir)
    finally:
        conn.close()

    print(f"Wrote {len(result.written)} new fixture file(s) to {out_dir}.")
    if result.already_covered:
        print(f"Skipped {result.already_covered} message(s) an existing fixture already covers.")
    if result.name_taken:
        print(f"Skipped {result.name_taken} message(s) whose file name was already taken.")
    for path in result.unreadable:
        print(f"warning: could not read {path}; left it untouched.", file=sys.stderr)
    if result.written:
        print(
            "The replay golden file is now out of date. A developer should run\n"
            "  python -m xaucopier.replay.harness --write-golden\n"
            "and review the diff before committing the new fixtures."
        )
    return exit_codes.EXIT_OK
