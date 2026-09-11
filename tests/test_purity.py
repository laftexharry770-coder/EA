import ast
from pathlib import Path
import pytest

# gates.py's own docstring states the contract: "Pure: no I/O, no clock, no
# randomness." All three thirds are enforced here.
FORBIDDEN = {
    # I/O and concurrency
    "telethon", "MetaTrader5", "sqlite3", "requests", "socket",
    "urllib", "http", "subprocess", "threading", "asyncio",
    # filesystem and process
    "os", "pathlib", "io", "shutil", "tempfile", "multiprocessing",
    # clock and randomness -- these do not raise, they just make a replay
    # non-reproducible, which is far harder to notice than a crash
    "time", "random", "secrets",
}

# Attribute calls that read a wall clock. `datetime` is imported by every
# domain module, so `datetime.now(UTC)` is one word away at all times.
CLOCK_ATTRS = {"now", "utcnow", "today", "monotonic", "perf_counter"}

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

def _clock_call_offences(source: str) -> list[str]:
    """Return a description of every wall-clock read in `source`.

    Flags a *call* whose callee is an attribute in CLOCK_ATTRS --
    `datetime.now(...)`, `datetime.utcnow()`, `date.today()`,
    `time.monotonic()`, `time.perf_counter()`.

    Deliberately does not flag attribute *access*: `snap.now_utc` is how
    every pinned time legitimately arrives, and flagging it would make the
    guard unusable. A bare `now()` call is likewise left alone -- reaching a
    clock without an attribute needs an import from a module the FORBIDDEN
    list already blocks.
    """
    tree = ast.parse(source, filename="<test>")
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in CLOCK_ATTRS:
                offences.append(f".{node.func.attr}() clock call")
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

def test_no_clock_calls_in_pure_core():
    for path in _pure_files():
        source = path.read_text(encoding="utf-8")
        offences = _clock_call_offences(source)
        assert not offences, f"{path.name} reads a clock: {offences}"

def test_clock_detection_catches_the_one_word_mistake():
    """`datetime` is imported by every domain module, so `datetime.now(UTC)`
    inside a gate is one word away at all times -- and it would destroy the
    replay determinism that is this logic's only regression net."""
    cases = [
        # True positives: a clock read
        ('datetime.now(UTC)', True),
        ('from datetime import datetime\nx = datetime.now()', True),
        ('datetime.utcnow()', True),
        ('date.today()', True),
        ('time.monotonic()', True),
        ('time.perf_counter()', True),
        ('def f(): return datetime.now(UTC) - sig.posted_at_utc', True),
        # False-positive controls: none of these read a clock
        ('age = snap.now_utc - sig.posted_at_utc', False),   # attribute access
        ('now = snap.now_utc', False),                        # local named now
        ('x = leg.tp', False),                                # ordinary attribute
        ('return self.now_utc', False),
        ('def now_for(snap): return snap.now_utc', False),
        ('cfg.today_only = True', False),                     # assignment, no call
        ('nowhere()', False),                                 # substring, not the name
        ('d["now"]', False),
    ]
    for source, should_flag in cases:
        offences = _clock_call_offences(source)
        assert (len(offences) > 0) == should_flag, (
            f"Source: {source!r}\n"
            f"Expected flag={should_flag}, got {offences}"
        )

@pytest.mark.parametrize("name", ["os", "time", "random", "secrets", "pathlib",
                                  "io", "multiprocessing", "shutil", "tempfile"])
def test_clock_and_randomness_modules_are_forbidden(name):
    # The docstring contract in gates.py is "no I/O, no clock, no
    # randomness"; the blocklist only ever encoded the first third.
    assert name in FORBIDDEN

def test_pure_core_has_files_to_check():
    # Guards against the parametrize silently collecting nothing.
    assert len(list(_pure_files())) >= 8


# `ingest/` and `cli/` are not part of the pure core -- they do real I/O
# (Telethon, DPAPI, argparse/input()) -- but they must never import the
# MetaTrader5 SDK directly. Execution (Plan 3) owns that boundary; a
# listener or CLI command reaching straight into MT5 would let a signal
# get acted on outside the store/outbox path this design relies on for
# idempotency and reconciliation.
INGEST_CLI_DIRS = ["ingest", "cli"]

def _ingest_cli_files():
    for name in INGEST_CLI_DIRS:
        directory = ROOT / name
        if directory.exists():
            yield from sorted(directory.rglob("*.py"))

@pytest.mark.parametrize("path", list(_ingest_cli_files()), ids=lambda p: p.name)
def test_ingest_and_cli_never_import_mt5(path: Path):
    offending = _imported_roots(path) & {"MetaTrader5"}
    assert not offending, (
        f"{path.name} imports MetaTrader5; ingest/ and cli/ must never "
        "touch the MT5 SDK directly"
    )

def test_ingest_and_cli_have_files_to_check():
    # Guards against the parametrize silently collecting nothing.
    assert len(list(_ingest_cli_files())) >= 2
