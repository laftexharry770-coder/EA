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

def _open_call_offences(source: str) -> list[str]:
    """Return a description of every open()-like call in `source`.

    Detects:
    - Bare open() calls
    - io.open(), builtins.open(), Path.open(), etc. (attribute access)
    - Aliased imports like: from builtins import open as o
    """
    tree = ast.parse(source, filename="<test>")
    offences = []

    # Track aliases: name -> original (e.g., {"o": "open"})
    open_aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        # Track import aliases
        if isinstance(node, ast.ImportFrom):
            if node.module in ("builtins", "io"):
                for alias in node.names:
                    if alias.name == "open":
                        # alias.asname is the local name, or alias.name if no 'as'
                        local_name = alias.asname or alias.name
                        open_aliases[local_name] = "open"

        # Detect open() calls
        if isinstance(node, ast.Call):
            # Bare open() call
            if isinstance(node.func, ast.Name):
                if node.func.id == "open":
                    offences.append(f"bare open() call")
                # Check for aliased open
                elif node.func.id in open_aliases:
                    offences.append(f"aliased open() call (as {node.func.id})")
            # Attribute call like io.open(), Path.open(), etc.
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr == "open":
                    offences.append(f"{node.func.attr}() attribute call")

    return offences

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
        source = path.read_text(encoding="utf-8")
        offences = _open_call_offences(source)
        assert not offences, f"{path.name} calls open(): {offences}"

def test_open_detection_catches_indirect_forms():
    """Verify the detection catches all forms of open() and avoids false positives."""
    # Test cases: (source, should_flag)
    cases = [
        # True positives: should detect open()
        ('open("f")', True),
        ('io.open("f")', True),
        ('builtins.open("f")', True),
        ('Path(x).open()', True),
        ('from builtins import open\nopen("f")', True),
        ('from io import open\nopen("f")', True),
        ('from builtins import open as o\no("f")', True),
        ('from io import open as alias\nalias("f")', True),
        # False positives: should NOT detect
        ('d.open_orders()', False),
        ('x = opened_file', False),
        ('sock.close()', False),
        ('def open_connection(): pass', False),
    ]

    for source, should_flag in cases:
        offences = _open_call_offences(source)
        has_offences = len(offences) > 0
        assert has_offences == should_flag, (
            f"Source: {source!r}\n"
            f"Expected flag={should_flag}, got flag={has_offences}\n"
            f"Offences: {offences}"
        )

def test_pure_core_has_files_to_check():
    # Guards against the parametrize silently collecting nothing.
    assert len(list(_pure_files())) >= 8
