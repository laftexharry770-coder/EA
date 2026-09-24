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


# ---------------------------------------------------------------------
# Final review F35: load() never lets a bare OSError or TypeError out.
# The realistic trigger is setup-login run under another Windows user
# (e.g. "Run as administrator" with a separate admin account): the file's
# ACL then refuses this user before DPAPI's own "wrong user" message can.
# ---------------------------------------------------------------------

def _encrypt(payload: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptProtectData(payload, "test", None, None, None, 0)


def test_a_folder_at_the_secrets_path_is_a_secrets_error(tmp_path: Path) -> None:
    folder = tmp_path / "session.dpapi"
    folder.mkdir()
    with pytest.raises(SecretsError, match="no secrets file") as caught:
        load(folder)
    assert "setup-login" in str(caught.value)


def test_a_file_this_user_cannot_read_is_a_secrets_error_naming_the_other_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "session.dpapi"
    save(_FAKE_BLOB, path)

    def denied(self: Path) -> bytes:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(SecretsError) as caught:
        load(path)
    message = str(caught.value)
    assert "another Windows user" in message
    assert "setup-login" in message and "this Windows account" in message
    assert isinstance(caught.value.__cause__, PermissionError)


@pytest.mark.parametrize(
    "payload",
    [
        b"[1, 2, 3]",
        b'"just a string"',
        b"42",
        b"null",
        b'{"api_id": "12345", "api_hash": "0", "session_string": "x"}',
        b'{"api_id": true, "api_hash": "0", "session_string": "x"}',
        b'{"api_id": 12345, "api_hash": 0, "session_string": "x"}',
        b'{"api_id": 12345, "api_hash": "0", "session_string": null}',
        b'{"api_id": 12345, "api_hash": "0"}',
    ],
    ids=["array", "string", "number", "null", "api_id-str", "api_id-bool",
         "api_hash-int", "session-null", "missing-key"],
)
def test_a_malformed_payload_is_a_secrets_error(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "session.dpapi"
    path.write_bytes(_encrypt(payload))
    with pytest.raises(SecretsError, match="malformed"):
        load(path)


# ---------------------------------------------------------------------
# Final review F38: DPAPI *user* scope is pinned. A same-user round trip
# decrypts a CRYPTPROTECT_LOCAL_MACHINE blob too, so no round-trip test
# can tell -- but any local account could then decrypt the session.
# ---------------------------------------------------------------------

_CRYPTPROTECT_LOCAL_MACHINE = 0x4
#: Offset of dwFlags in a DPAPI blob (after dwVersion, guidProvider,
#: dwMasterKeyVersion, guidMasterKey).
_DPAPI_FLAGS_OFFSET = 40


def test_save_asks_dpapi_for_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    real = secrets_module.win32crypt.CryptProtectData

    def spy(*args: object) -> bytes:
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(secrets_module.win32crypt, "CryptProtectData", spy)
    save(_FAKE_BLOB, tmp_path / "session.dpapi")
    (args,) = calls
    assert args[5] == 0, f"dwFlags {args[5]!r}: the blob must be for this Windows user only"


def test_the_saved_blob_is_user_scoped_not_machine_scoped(tmp_path: Path) -> None:
    import struct

    path = tmp_path / "session.dpapi"
    save(_FAKE_BLOB, path)
    (flags,) = struct.unpack_from("<I", path.read_bytes(), _DPAPI_FLAGS_OFFSET)
    assert flags & _CRYPTPROTECT_LOCAL_MACHINE == 0
    # The check can tell: a machine-scoped blob carries the flag there.
    import win32crypt

    machine = win32crypt.CryptProtectData(
        b"{}", "test", None, None, None, _CRYPTPROTECT_LOCAL_MACHINE
    )
    (machine_flags,) = struct.unpack_from("<I", machine, _DPAPI_FLAGS_OFFSET)
    assert machine_flags & _CRYPTPROTECT_LOCAL_MACHINE
