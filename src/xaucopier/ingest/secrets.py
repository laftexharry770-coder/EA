"""DPAPI-encrypted storage for the Telegram credential.

**This module is the only place on disk the user's Telegram session may
live, encrypted.** The session string is a bearer credential equal to the
whole Telegram account -- it bypasses both the login code and the 2FA
password. `api_hash` is a one-per-phone-number secret from
my.telegram.org. Neither value is ever logged, printed, echoed, or placed
in a test fixture anywhere in this codebase; the only place they may
exist in plaintext is briefly in memory during `save`/`load`.

**Encryption: Windows DPAPI, user scope.** `win32crypt.CryptProtectData`
is called with `dwFlags=0` -- explicitly *not*
`CRYPTPROTECT_LOCAL_MACHINE` -- so the blob can only be decrypted by the
same Windows user account, on the same machine, that created it. That is
what makes a copied-elsewhere blob fail closed: `CryptUnprotectData` on
another machine (no matching per-user DPAPI master key) or under another
user raises `pywintypes.error`, which `load()` turns into a `SecretsError`
that says so explicitly, rather than a confusing native traceback.

**File ACL is defense-in-depth, not the security boundary.** After
writing the encrypted blob, `save()` best-effort restricts the file's
NTFS ACL to the current user via `icacls` (chosen over hand-building a
DACL through `win32security`, because `icacls` is a stable, documented
command for exactly this and needs no SID plumbing). If `icacls` itself
fails (missing binary, restricted shell, etc.) the blob is still safe --
DPAPI's user-scoping means another account cannot decrypt it even with
read access to the file -- so that failure is non-fatal (a stderr
warning, never an exception): it never blocks or corrupts a save. But
"non-fatal" is not "silent" -- a failure that nobody can see is hardening
nobody can audit, so it is always printed, never just swallowed. Losing
DPAPI's user-scope would be a real security regression; losing the ACL
restriction on top of it is not, provided the operator actually finds
out.

**`repr`/`str` never expose `api_hash` or `session_string`.** Both
fields are declared `field(repr=False)`, so `repr(blob)`, `str(blob)`,
an f-string, `"%s" % blob`, or a `logging.debug("...", blob)` call all
print only `api_id` and a placeholder for the rest. This does *not*
protect `dataclasses.asdict(blob)` or direct attribute access
(`blob.api_hash`) -- both still return the raw values, which is exactly
what `save()`/`load()` need. The property this buys is narrower and
still real: nothing that merely logs or prints "the blob" as a whole
-- an exception handler, a debug trace, an unhandled-task dump -- can
accidentally write the credential to a log file that is neither
DPAPI-protected nor gitignored.

**`save()` writes atomically.** A crash, power loss, or full disk
mid-write must never destroy a previously-working session: `save()`
writes the new ciphertext to a sibling `*.tmp` file, `fsync`s it,
restricts its ACL, and only then `os.replace()`s it onto `path` --
an atomic rename on NTFS. A failure at any point before the replace
leaves the original file (if any) completely untouched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pywintypes
import win32crypt

_DESCRIPTION = "xaucopier telegram secrets"

# .../src/xaucopier/ingest/secrets.py -> project root is four parents up.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_SECRETS_PATH = _PROJECT_ROOT / "config" / "secrets" / "session.dpapi"


class SecretsError(Exception):
    """Raised when the secrets blob cannot be found, read, or decrypted."""


@dataclass(frozen=True, slots=True)
class SecretsBlob:
    api_id: int
    # repr=False: see the module docstring. These two are the bearer
    # credential; they must never come out of a bare repr()/str()/f-string
    # or a `logging.debug("%s", blob)` call, even by accident once Task 11
    # holds one of these in a long-lived listener.
    api_hash: str = field(repr=False)
    session_string: str = field(repr=False)


def _icacls_path() -> str:
    """Resolve `icacls.exe` under `%SystemRoot%\\System32` instead of
    trusting `PATH` to resolve the real one."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "icacls.exe"
    return str(candidate) if candidate.exists() else "icacls"


def _warn_acl_not_restricted(path: Path, reason: str) -> None:
    print(
        f"warning: could not restrict permissions on {path} ({reason}). "
        "The file is still DPAPI-encrypted for this Windows user only, "
        "but other local accounts can see the (undecryptable) file.",
        file=sys.stderr,
    )


def _restrict_acl(path: Path) -> None:
    """Best-effort: restrict `path`'s NTFS ACL to the current Windows user.

    Uses `icacls` via `subprocess` rather than `win32security`, per the
    brief's choice of "either is fine" -- `icacls /inheritance:r
    /grant:r <user>:F` is a documented one-liner that needs no manual DACL
    or SID construction. Failure here is intentionally non-fatal (see the
    module docstring): it never raises, so a save can never be blocked --
    or silently corrupted -- by an environment where `icacls` is
    unavailable. It is, however, always reported to stderr so the failure
    is auditable rather than invisible.
    """
    username = os.environ.get("USERNAME")
    if not username:
        _warn_acl_not_restricted(path, "no USERNAME in environment")
        return
    domain = os.environ.get("USERDOMAIN")
    userspec = f"{domain}\\{username}" if domain else username
    try:
        result = subprocess.run(
            [_icacls_path(), str(path), "/inheritance:r", "/grant:r", f"{userspec}:F"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        _warn_acl_not_restricted(path, str(exc))
        return
    if result.returncode != 0:
        _warn_acl_not_restricted(path, f"icacls exited {result.returncode}")


def save(blob: SecretsBlob, path: Path) -> None:
    """Serialise `blob` to JSON, DPAPI-encrypt it (user scope), and
    atomically write the result to `path`. Creates `path`'s parent
    directory if needed.

    Atomic: writes to `path.with_suffix(".tmp")`, `fsync`s it, restricts
    its ACL, then `os.replace()`s it onto `path`. A failure at any point
    up to and including the write leaves whatever was previously at
    `path` (if anything) untouched and still loadable -- the half-written
    temp file is cleaned up, not left behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "api_id": blob.api_id,
            "api_hash": blob.api_hash,
            "session_string": blob.session_string,
        }
    ).encode("utf-8")
    # flags=0: current-user scope. Never CRYPTPROTECT_LOCAL_MACHINE (0x4),
    # which would let any user on this machine decrypt the blob.
    encrypted = win32crypt.CryptProtectData(payload, _DESCRIPTION, None, None, None, 0)

    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_acl(tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def load(path: Path) -> SecretsBlob:
    """Read and decrypt the blob at `path`.

    Raises `SecretsError` (never a bare `pywintypes.error` or `OSError`)
    when the file is missing, malformed, or was encrypted under a
    different Windows user/machine -- each with a message that tells the
    user what to do about it.
    """
    if not path.exists():
        raise SecretsError(
            f"no secrets file at {path}. Run `.\\xaucopier setup-login` first "
            "to log in and create it."
        )

    encrypted = path.read_bytes()

    try:
        _description, payload = win32crypt.CryptUnprotectData(
            encrypted, None, None, None, 0
        )
    except pywintypes.error as exc:
        raise SecretsError(
            f"failed to decrypt secrets file at {path}. DPAPI blobs are "
            "tied to the Windows user account (and machine) that created "
            "them, so this usually means the file was copied from another "
            "machine or user, or has been corrupted. Run `.\\xaucopier "
            "setup-login` again on this machine, under this account."
        ) from exc

    try:
        data = json.loads(payload.decode("utf-8"))
        return SecretsBlob(
            api_id=data["api_id"],
            api_hash=data["api_hash"],
            session_string=data["session_string"],
        )
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
        raise SecretsError(
            f"secrets file at {path} decrypted but its contents are "
            "malformed. Run `.\\xaucopier setup-login` again to recreate it."
        ) from exc
