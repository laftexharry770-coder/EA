"""Reusable AST-walking helpers for asserting a layer never touches
sqlite3 directly.

`test_purity.py` enforces purity for `domain/` and `parsing/` with
inline helpers. This module is the equivalent tool for a different
boundary: `store/db.py` (Task 4) is the *only* place allowed to call
`sqlite3.connect()` -- everything above it (the asyncio listener in
`ingest/`, Task 8) must hand writes to `store.db.Writer` and reads to
`store.db.connect(role="reader")` instead of opening sqlite3 itself.

`ingest/` does not exist yet, so nothing here is wired up against it --
Task 8 imports `sqlite3_offences` and asserts the result is empty. This
module only proves the detector itself is correct, against synthetic
source snippets and a throwaway directory.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _tree_uses_sqlite3(tree: ast.AST) -> bool:
    """True if `tree` imports sqlite3 (any form) or calls
    `sqlite3.connect(...)` via a `sqlite3.` attribute access.

    Deliberately narrow on the call side: it flags `sqlite3.connect(...)`
    specifically (attribute access off a name literally called
    `sqlite3`), not every `.connect()` call -- otherwise `redis.connect()`
    or similar would be a false positive. The import check is broad on
    purpose: importing the module at all is already the violation this
    guards against, regardless of whether `connect` is ever called.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3" or alias.name.startswith("sqlite3."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "sqlite3" or (
                node.module is not None and node.module.startswith("sqlite3.")
            ):
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id == "sqlite3"
            ):
                return True
    return False


def source_uses_sqlite3(source: str) -> bool:
    """Same detection as `sqlite3_offences`, but against a source string
    directly -- lets the detector itself be unit-tested without touching
    the filesystem."""
    tree = ast.parse(source, filename="<purity_helpers>")
    return _tree_uses_sqlite3(tree)


def sqlite3_offences(directory: Path) -> set[Path]:
    """Return the set of `.py` files under `directory` that import
    sqlite3 (in any form) or call `sqlite3.connect(...)` directly.

    An empty result means the directory is clean. Task 8 asserts
    `sqlite3_offences(ROOT / "ingest") == set()`.
    """
    offenders: set[Path] = set()
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _tree_uses_sqlite3(tree):
            offenders.add(path)
    return offenders
