"""A static walk of the real import graph, for "nothing reachable from X
imports Y" tests.

Grepping the source for `MetaTrader5` would not do: it cannot tell a
comment from an import, and it cannot say whether the module that holds
an import is *reachable* at all. This parses each first-party module and
follows every import it makes -- at module level, inside functions and
`try`/`if` blocks (lazy imports are exactly how a forbidden dependency
would slip onto a path unnoticed), relative imports, and
`importlib.import_module("...")` / `__import__("...")` with a literal
name -- from the root module outwards, module by module. Nothing is
executed.

First-party modules (under `package`) are walked; anything else is
recorded by name but not entered: third-party code cannot be statically
followed in general, so the test that uses this also imports the whole
reached set in a clean subprocess and inspects `sys.modules`.
"""
from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ImportGraph:
    #: First-party module -> the chain of modules that first reached it.
    reached: dict[str, tuple[str, ...]]
    #: First-party module -> every module name it imports, anywhere in it.
    imports: dict[str, frozenset[str]]

    def chains_to(self, top_level: str) -> list[tuple[str, ...]]:
        """For each walked module that imports `top_level` (or a submodule
        of it), the chain from the root to that module, then the import."""
        return [
            (*self.reached[module], name)
            for module in sorted(self.imports)
            for name in sorted(self.imports[module])
            if name.split(".")[0] == top_level
        ]


def _with_parents(name: str) -> set[str]:
    parts = name.split(".")
    return {".".join(parts[: i + 1]) for i in range(len(parts))}


def _dynamic_target(node: ast.Call) -> str | None:
    func = node.func
    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if called not in ("import_module", "__import__"):
        return None
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def module_imports(source: str, *, module: str, is_package: bool) -> frozenset[str]:
    """Every module name `source` (the text of `module`) imports anywhere,
    resolved to absolute names. `from a import b` yields `a` and `a.b`:
    whether `a.b` is a module or an attribute is for the caller to decide."""
    tree = ast.parse(source, filename=module)
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found |= _with_parents(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                if node.level > 1:
                    base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts)
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found |= _with_parents(target)
            found |= {f"{target}.{alias.name}" for alias in node.names if alias.name != "*"}
        elif isinstance(node, ast.Call):
            target = _dynamic_target(node)
            if target:
                found |= _with_parents(target)
    return frozenset(found)


def _source_file(src_root: Path, module: str) -> tuple[Path, bool] | None:
    base = src_root.joinpath(*module.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py", True
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py"), False
    return None


def walk(src_root: Path, root: str, *, package: str) -> ImportGraph:
    """Walk the import graph of `root` (a module under `src_root`),
    entering every reachable module of `package`."""
    reached: dict[str, tuple[str, ...]] = {root: (root,)}
    by_module: dict[str, frozenset[str]] = {}
    queue: deque[str] = deque([root])
    while queue:
        module = queue.popleft()
        located = _source_file(src_root, module)
        if located is None:
            continue
        path, is_package = located
        names = module_imports(path.read_text(encoding="utf-8"), module=module, is_package=is_package)
        by_module[module] = names
        for name in sorted(names):
            if name.split(".")[0] != package or name in reached:
                continue
            if _source_file(src_root, name) is None:
                continue  # `from pkg.mod import function`: an attribute, not a module
            reached[name] = (*reached[module], name)
            queue.append(name)
    return ImportGraph(
        reached={name: chain for name, chain in reached.items() if name in by_module},
        imports=by_module,
    )
