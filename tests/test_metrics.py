"""Tests for `store.metrics`, the one metrics upsert every counter uses."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from xaucopier.store import db, metrics


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "store.db", role="writer")
    try:
        yield connection
    finally:
        connection.close()


def _value(conn, day_key: str, name: str) -> int | None:
    row = conn.execute(
        "SELECT value FROM metrics WHERE day_key = ? AND name = ?", (day_key, name)
    ).fetchone()
    return None if row is None else row[0]


def test_bump_creates_the_row_then_accumulates(conn) -> None:
    metrics.bump(conn, day_key="2026-09-05", name="thing")
    metrics.bump(conn, day_key="2026-09-05", name="thing", value=4)
    metrics.bump(conn, day_key="2026-09-06", name="thing")

    assert _value(conn, "2026-09-05", "thing") == 5
    assert _value(conn, "2026-09-06", "thing") == 1


def test_bump_never_commits_so_the_caller_owns_the_transaction(conn) -> None:
    metrics.bump(conn, day_key="2026-09-05", name="thing")
    assert conn.in_transaction
    conn.rollback()

    assert _value(conn, "2026-09-05", "thing") is None


@pytest.mark.parametrize("value", [0, -1, True, 1.0])
def test_bump_rejects_a_value_that_is_not_a_positive_int(conn, value) -> None:
    with pytest.raises(ValueError, match="positive int"):
        metrics.bump(conn, day_key="2026-09-05", name="thing", value=value)


def test_day_key_is_the_utc_day_even_for_a_non_utc_aware_datetime() -> None:
    late_evening_new_york = datetime(2026, 9, 5, 22, 0, tzinfo=timezone(timedelta(hours=-4)))

    assert metrics.day_key_for(late_evening_new_york) == "2026-09-06"
    assert metrics.day_key_for(datetime(2026, 9, 5, 23, 59, tzinfo=UTC)) == "2026-09-05"


def test_day_key_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError, match="naive"):
        metrics.day_key_for(datetime(2026, 9, 5, 12, 0))
