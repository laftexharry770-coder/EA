"""The `xaucopier.cmd` launcher in the project folder.

The package is not pip-installed, so this is how the user (and later a
supervisor) runs every command: it points Python at `src/` and runs the
CLI with the project's own interpreter. What matters is that the CLI's
exit code comes out of the launcher unchanged -- a supervisor that saw
every failure as "1" could not tell exit 5 (do not restart) from exit 4
(restart).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "xaucopier.cmd"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="a Windows batch file")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(LAUNCHER), *args], capture_output=True, text=True, timeout=120, check=False
    )


def test_the_launcher_runs_the_cli() -> None:
    result = _run("--help")
    assert result.returncode == 0
    assert "WATCH-ONLY" in result.stdout


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("watch", "--config-dir", "{tmp}"), 5),  # no channels.toml: a setup error
        (("status", "--db", "{tmp}/none.db"), 1),
        (("no-such-command",), 2),
    ],
)
def test_the_launcher_passes_the_exit_code_through(tmp_path: Path, args: tuple, code: int) -> None:
    result = _run(*(arg.format(tmp=tmp_path) for arg in args))
    assert result.returncode == code, result.stderr
