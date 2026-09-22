"""Tests for the DPAPI-encrypted secrets store.

Every value below is an obviously-fake placeholder (per the Task 7 brief's
hard rule): `api_id=12345`, `api_hash="0"*32`, and a session string that is
clearly not a real Telethon session. No real credential, of any kind, is
ever written into this file or into a fixture.

Exercises the real `pywin32` DPAPI path -- `win32crypt` is not mocked, per
the brief ("pywin32 is installed in the venv on this machine").
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

import xaucopier.ingest.secrets as secrets_module
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


# ---------------------------------------------------------------------
# Security-review follow-up: repr/str must never expose the credential.
# ---------------------------------------------------------------------

def test_repr_never_exposes_secret_fields(caplog: pytest.LogCaptureFixture) -> None:
    marker_hash = "MARKER_HASH_" + "a" * 40
    marker_session = "MARKER_SESSION_" + "b" * 40
    blob = SecretsBlob(api_id=99999, api_hash=marker_hash, session_string=marker_session)

    assert marker_hash not in repr(blob)
    assert marker_session not in repr(blob)
    assert marker_hash not in str(blob)
    assert marker_session not in str(blob)
    assert marker_hash not in f"{blob}"
    assert marker_session not in f"{blob}"
    assert marker_hash not in ("%s" % (blob,))
    assert marker_session not in ("%s" % (blob,))

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("xaucopier.test").debug("loaded %s", blob)
    assert marker_hash not in caplog.text
    assert marker_session not in caplog.text

    # api_id is not secret and is fine to appear.
    assert "99999" in repr(blob)


# ---------------------------------------------------------------------
# Security-review follow-up: save() is an atomic replace, not a bare
# truncate-in-place -- a failure partway through must never destroy a
# previously-working session.
# ---------------------------------------------------------------------

def test_failed_save_leaves_prior_blob_intact_and_loadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.dpapi"
    save(_FAKE_BLOB, path)
    original_bytes = path.read_bytes()

    other_blob = SecretsBlob(
        api_id=99999,
        api_hash="1" * 32,
        session_string="A_DIFFERENT_FAKE_SESSION_" + "z" * 40,
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(secrets_module.os, "fsync", _boom)

    with pytest.raises(OSError):
        save(other_blob, path)

    # The target file was never touched -- os.replace() never ran.
    assert path.read_bytes() == original_bytes
    assert load(path) == _FAKE_BLOB

    # The half-written temp file is cleaned up, not left behind.
    assert not path.with_suffix(".tmp").exists()


def test_save_overwrite_replaces_atomically(tmp_path: Path) -> None:
    path = tmp_path / "session.dpapi"
    save(_FAKE_BLOB, path)

    other_blob = SecretsBlob(
        api_id=54321,
        api_hash="2" * 32,
        session_string="ANOTHER_FAKE_SESSION_" + "w" * 40,
    )
    save(other_blob, path)

    assert load(path) == other_blob
    assert not path.with_suffix(".tmp").exists()
