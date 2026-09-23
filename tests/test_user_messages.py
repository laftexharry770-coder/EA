"""Every command a message tells the user to run is written the way the
user must type it: `.\\xaucopier watch`, not `xaucopier watch`.

The package is not installed; the user runs the `xaucopier.cmd` launcher
from PowerShell in the project folder, and PowerShell only runs a script
in the current folder as `.\\xaucopier`. A message saying "run `xaucopier
setup-login`" is an instruction that does not work.

Walks every string constant in `src/` (docstrings excepted -- those are
for developers) and fails on a backticked `xaucopier <command>` or a
"...: xaucopier <command>" instruction.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "xaucopier"
COMMANDS = "setup-login|resolve-channels|watch|status|ack|export-fixtures"
WRONG = re.compile(rf"(`|with: |run )xaucopier ({COMMANDS})\b")
#: A bare "run setup-login" -- no `.\xaucopier` at all -- is the same mistake.
BARE = re.compile(rf"\b[Rr]un `?({COMMANDS})\b")


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _offences() -> list[str]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and (WRONG.search(node.value) or BARE.search(node.value))
            ):
                found.append(f"{path.relative_to(SRC)}:{node.lineno}: {node.value.strip()[:70]}")
    return found


def test_every_command_in_a_message_is_written_as_the_user_types_it() -> None:
    assert _offences() == []


def test_the_check_bites() -> None:
    assert WRONG.search("Run `xaucopier setup-login` again")
    assert WRONG.search("Check on it with: xaucopier status")
    assert not WRONG.search("Run `.\\xaucopier setup-login` again")
    assert not WRONG.search("xaucopier watch cannot start: ...")  # a title, not an instruction
    assert BARE.search("Run setup-login in an ordinary PowerShell window")
    assert BARE.search("then run resolve-channels again")
    assert not BARE.search("Run `.\\xaucopier setup-login` again")
