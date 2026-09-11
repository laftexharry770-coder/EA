"""Tests for the DPAPI-encrypted secrets store.

Every value below is an obviously-fake placeholder (per the Task 7 brief's
hard rule): `api_id=12345`, `api_hash="0"*32`, and a session string that is
clearly not a real Telethon session. No real credential, of any kind, is
ever written into this file or into a fixture.

Exercises the real `pywin32` DPAPI path -- `win32crypt` is not mocked, per
the brief ("pywin32 is installed in the venv on this machine").
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xaucopier.ingest.secrets import SecretsBlob, SecretsError, load, save

_FAKE_BLOB = SecretsBlob(
    api_id=12345,
    api_hash="0" * 32,
    session_string="NOT_A_REAL_SESSION_" + "x" * 40,
)


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "session.dpapi"
    save(_FAKE_BLOB, path)
    assert load(path) == _FAKE_BLOB


def test_blob_on_disk_is_not_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "session.dpapi"
    save(_FAKE_BLOB, path)
    raw = path.read_bytes()
    # Neither secret value may appear byte-for-byte in the encrypted blob.
    assert _FAKE_BLOB.api_hash.encode("utf-8") not in raw
    assert _FAKE_BLOB.session_string.encode("utf-8") not in raw


def test_missing_file_raises_with_setup_login_hint(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist" / "session.dpapi"
    with pytest.raises(SecretsError, match="setup-login"):
        load(path)


def test_corrupted_blob_raises_secrets_error(tmp_path: Path) -> None:
    path = tmp_path / "session.dpapi"
    save(_FAKE_BLOB, path)
    raw = bytearray(path.read_bytes())
    raw[-5] ^= 0xFF  # flip one byte -- DPAPI's integrity check must reject this
    path.write_bytes(bytes(raw))
    with pytest.raises(SecretsError):
        load(path)


def test_save_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "session.dpapi"
    assert not path.parent.exists()
    save(_FAKE_BLOB, path)
    assert load(path) == _FAKE_BLOB


def test_secrets_blob_is_frozen() -> None:
    with pytest.raises(AttributeError):
        _FAKE_BLOB.api_id = 1  # type: ignore[misc]


def test_truncated_file_raises_secrets_error(tmp_path: Path) -> None:
    # Not just a bit-flip: a file that isn't a DPAPI blob at all (e.g. an
    # empty file, or one copied from another machine and truncated in
    # transit) must fail the same clear way, not with a raw pywintypes
    # traceback.
    path = tmp_path / "session.dpapi"
    path.write_bytes(b"not a dpapi blob")
    with pytest.raises(SecretsError):
        load(path)
