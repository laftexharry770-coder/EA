import ast
from pathlib import Path
import pytest

FORBIDDEN = {"telethon", "MetaTrader5", "sqlite3", "requests", "socket",
             "urllib", "http", "subprocess", "threading", "asyncio"}
PURE_DIRS = ["domain", "parsing"]
ROOT = Path(__file__).resolve().parents[1] / "src" / "xaucopier"

def _pure_files():
    for name in PURE_DIRS:
        yield from sorted((ROOT / name).rglob("*.py"))

def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots

@pytest.mark.parametrize("path", list(_pure_files()), ids=lambda p: p.name)
def test_no_io_imports_in_pure_core(path: Path):
    offending = _imported_roots(path) & FORBIDDEN
    assert not offending, f"{path.name} imports {sorted(offending)}; domain/ and parsing/ must stay pure"

def test_no_open_calls_in_pure_core():
    for path in _pure_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "open", f"{path.name} calls open()"

def test_pure_core_has_files_to_check():
    # Guards against the parametrize silently collecting nothing.
    assert len(list(_pure_files())) >= 8
