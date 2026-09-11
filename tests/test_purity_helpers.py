"""Tests for the sqlite3-purity AST helper (`tests/purity_helpers.py`).

The Telegram listener (`ingest/`, Task 8) doesn't exist yet, so the
assertion "the listener never calls sqlite3.connect directly" can't be
wired up against a real directory -- there is nothing to walk. Per the
controller ruling for this task, that assertion moves to Task 8, which
will import `sqlite3_offences` from `purity_helpers` and run it against
`ingest/` once that package exists. This file only proves the detector
itself is correct -- against synthetic source snippets and a throwaway
directory -- so Task 8 can trust the tool it inherits.
"""
from __future__ import annotations

from pathlib import Path

from purity_helpers import sqlite3_offences, source_uses_sqlite3

# -- source_uses_sqlite3(): detector correctness on snippets ---------------

TRUE_CASES = [
    "import sqlite3",
    "import sqlite3 as db",
    "from sqlite3 import connect",
    "from sqlite3 import connect as c",
    "import sqlite3.dbapi2",
    "sqlite3.connect('store.db')",
    "def f():\n    import sqlite3\n    return sqlite3.connect(':memory:')",
]

FALSE_CASES = [
    "from xaucopier.store.db import connect\nconnect(path, role='reader')",
    "conn.execute('SELECT 1')",
    "import threading",
    "redis.connect()",  # a .connect() call, but not on a name called sqlite3
    "def connect(): pass",
    "x = sqlite3_offences",  # the identifier appears, but nothing is imported or called
]


def test_detector_flags_every_form_of_sqlite3_use():
    for source in TRUE_CASES:
        assert source_uses_sqlite3(source), f"expected a flag for: {source!r}"


def test_detector_has_no_false_positives():
    for source in FALSE_CASES:
        assert not source_uses_sqlite3(source), f"unexpected flag for: {source!r}"


# -- sqlite3_offences(): walks an actual directory --------------------------


def test_sqlite3_offences_walks_a_directory(tmp_path: Path):
    clean = tmp_path / "clean.py"
    clean.write_text(
        "from xaucopier.store.db import connect\n"
        "conn = connect(path, role='reader')\n",
        encoding="utf-8",
    )
    dirty = tmp_path / "dirty.py"
    dirty.write_text("import sqlite3\nconn = sqlite3.connect('x.db')\n", encoding="utf-8")

    offenders = sqlite3_offences(tmp_path)

    assert offenders == {dirty}


def test_sqlite3_offences_empty_for_a_clean_directory(tmp_path: Path):
    (tmp_path / "a.py").write_text("import asyncio\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from xaucopier.store.db import Writer\nWriter(path)\n", encoding="utf-8"
    )

    assert sqlite3_offences(tmp_path) == set()
