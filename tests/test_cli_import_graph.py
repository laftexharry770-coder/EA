"""No module reachable from `xaucopier watch` imports `MetaTrader5`.

Plan 2 is a watch-only recorder: it must not place an order, and must not
be *able* to. `MetaTrader5` is the only way this codebase can reach a
trading terminal (spec 4: the worker is the only place that imports it),
so the property is: nothing on the `watch` path imports it.

Two checks, because neither alone is enough:

1. **A static walk of the real import graph** from `cli/main.py` (the
   entry point, which imports every command including `watch`), following
   every import in every first-party module it reaches -- including ones
   inside functions, which is how a forbidden dependency would slip in
   unnoticed (`ingest/listener.py` already imports lazily). A grep would
   not know which modules are reachable; an import-time check would not
   see a lazy import.
2. **A clean subprocess** that imports every module the walk reached,
   with an import hook that refuses and records any attempt to import
   `MetaTrader5` -- covering third-party code the static walk does not
   enter, including an attempt swallowed by a `try/except ImportError`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from import_graph import module_imports, walk

SRC = Path(__file__).resolve().parents[1] / "src"
FORBIDDEN = "MetaTrader5"
ROOT = "xaucopier.cli.main"


@pytest.mark.parametrize("root", [ROOT, "xaucopier.cli.watch"])
def test_nothing_reachable_from_the_cli_imports_metatrader5(root: str) -> None:
    graph = walk(SRC, root, package="xaucopier")

    assert graph.chains_to(FORBIDDEN) == []

    # The walk really covered the watch path -- a walker that stopped at
    # the first module would pass the assertion above vacuously.
    for module in (
        "xaucopier.cli.watch",
        "xaucopier.ingest.listener",
        "xaucopier.ingest.poll",
        "xaucopier.ingest.edits",
        "xaucopier.ingest.client",
        "xaucopier.store.db",
        "xaucopier.store.messages_repo",
        "xaucopier.store.edits_repo",
        "xaucopier.store.outbox",
        "xaucopier.parsing.router",
        "xaucopier.parsing.channel_mmh",
        "xaucopier.domain.signal",
        "xaucopier.config.channels",
    ):
        assert module in graph.reached, f"the walk from {root} never reached {module}"


def test_importing_the_whole_watch_path_never_attempts_metatrader5() -> None:
    graph = walk(SRC, ROOT, package="xaucopier")
    script = textwrap.dedent(
        """
        import importlib, json, sys

        class Refuse:
            attempts = []

            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] == "MetaTrader5":
                    Refuse.attempts.append(name)
                    raise ImportError("MetaTrader5 is forbidden on the watch path")
                return None

        sys.meta_path.insert(0, Refuse())
        for name in sys.argv[1:]:
            importlib.import_module(name)
        print(json.dumps({
            "attempts": Refuse.attempts,
            "loaded": sorted(m for m in sys.modules if m.split(".")[0] == "MetaTrader5"),
            "imported": sorted(m for m in sys.argv[1:] if m in sys.modules),
        }))
        """
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    result = subprocess.run(
        [sys.executable, "-c", script, *sorted(graph.reached)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=120,
    )
    outcome = json.loads(result.stdout.strip().splitlines()[-1])

    assert outcome["attempts"] == []
    assert outcome["loaded"] == []
    assert outcome["imported"] == sorted(graph.reached)


# -- the walker itself bites -------------------------------------------------------------


def _package(root: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")


def test_the_walker_finds_a_forbidden_import_hidden_inside_a_function(tmp_path: Path) -> None:
    """Through a relative import, a `from package import submodule`, and a
    lazy import in a function body: the shape the real mutation would take."""
    _package(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/cli.py": "from . import runner\n",
            "pkg/runner.py": "from pkg.deep import engine\n",
            "pkg/deep/__init__.py": "",
            "pkg/deep/engine.py": """
                def place():
                    import MetaTrader5 as mt5
                    return mt5
            """,
            "pkg/unreachable.py": "import MetaTrader5\n",
        },
    )

    graph = walk(tmp_path, "pkg.cli", package="pkg")

    assert graph.chains_to(FORBIDDEN) == [
        ("pkg.cli", "pkg.runner", "pkg.deep.engine", "MetaTrader5")
    ]
    assert "pkg.unreachable" not in graph.reached


def test_the_walker_sees_every_import_form() -> None:
    source = """
        import a.b.c
        from d import e
        from .sibling import thing
        from .. import parent_mod
        import importlib
        def f():
            importlib.import_module("MetaTrader5")
            __import__("lazy.mod")
        try:
            from g import h
        except ImportError:
            pass
    """
    names = module_imports(textwrap.dedent(source), module="x.y.z", is_package=False)

    assert {"a", "a.b", "a.b.c", "d", "d.e", "x.y.sibling", "x.y.sibling.thing",
            "x", "x.parent_mod", "MetaTrader5", "lazy", "lazy.mod", "g", "g.h"} <= names
