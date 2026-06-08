"""Encrypted token store for the hermes-nodes plugin.

Persists node→token bindings to a JSON file encrypted at rest with
:class:`cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256). The
encryption key is loaded from the env var named in
:attr:`hermes_nodes_plugin.config.NodeServerConfig.token_encryption_key_env`
(defaults to ``HERMES_NODES_TOKEN_KEY``).

Design choices, with rationale:

  * **One file, all tokens.** A single ``tokens.json`` per host keeps
    pairing setup trivial: ``hermes node pair`` creates it, ``hermes
    node list`` reads it, ``hermes node revoke`` mutates it. Multi-file
    stores (one per node) would force operators to clean up on revoke
    and complicate the "list all known nodes" query. Trade-off: file
    must be re-encrypted on every mutation. For ≤ a few hundred nodes
    the file is small and re-encryption is sub-millisecond.

  * **Revoked entries are kept on disk, not deleted.** Setting
    ``revoked=true`` rather than ``del`` preserves the audit trail (a
    node can't reconnect with a stale token even if the file is
    restored from backup) and keeps the JSON append-friendly. A
    separate prune pass can drop old revoked entries later.

  * **File-level write lock via ``fcntl``.** Two concurrent
    ``hermes node pair`` invocations on the same host would otherwise
    race on read-modify-write. Cross-process locking matters because
    the store can be touched by both the CLI and the long-running
    server (the server needs to read tokens for inbound auth). We use
    POSIX ``fcntl.flock`` so the lock auto-releases on process death.

  * **Constant-time token comparison.** Comparing the presented token
    to the stored token with ``==`` leaks timing info that lets an
    attacker byte-by-byte brute-force the secret. NFR-1.1 requires
    :func:`hmac.compare_digest`.

  * **At-rest encryption only.** The plaintext token is held in memory
    while a process has a handle to the store, which is unavoidable
    since the server needs to compare incoming tokens. A future
    hardening pass could use a HSM-backed key, but that's out of scope
    for v1 (REQUIREMENTS §3 "Explicitly out of scope").
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

from hermes_nodes_plugin.errors import TokenStoreError

# Token is 32 random bytes (256 bits) encoded base64url, matching
# REQUIREMENTS FR-1.1. 32 bytes matches the entropy floor recommended
# by NIST SP 800-63B for "memorable secrets" (we don't need memorable,
# we just need strong).
_TOKEN_BYTES = 32


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenRecord:
    """Public view of a stored token entry.

    The raw token is *not* a field — once ``create()`` has returned it
    to the operator, we keep only a hash on disk. The store can prove a
    presented token matches a record (via :meth:`TokenStore.validate`),
    but it cannot recover the token from the record alone.

    Attributes:
        name: The node name this token is bound to.
        created_at: ISO-8601 UTC timestamp of when the token was created.
        revoked: True if the token has been revoked via ``revoke()``.
            Revoked tokens fail validation even if the operator
            presents the original secret.
        last_used_at: ISO-8601 UTC timestamp of the most recent
            successful validation, or ``None`` if never used.
    """

    name: str
    created_at: str
    revoked: bool
    last_used_at: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Shape safe to return from ``hermes node list`` CLI.

        Excludes the token hash and the secret. Stable field order so
        the CLI output doesn't shuffle between rows.
        """
        return {
            "name": self.name,
            "created_at": self.created_at,
            "revoked": self.revoked,
            "last_used_at": self.last_used_at,
        }


# ---------------------------------------------------------------------------
# Internal on-disk record (hashes the secret)
# ---------------------------------------------------------------------------


@dataclass
class _StoredRecord:
    """What's actually serialised to disk.

    Differs from :class:`TokenRecord` in two ways: it carries the SHA-256
    hex digest of the token (so we can verify a presented token without
    storing the secret), and it always uses dict-of-dicts shape so the
    on-disk JSON is stable across schema bumps.
    """

    name: str
    token_hash: str
    created_at: str
    revoked: bool
    last_used_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "token_hash": self.token_hash,
            "created_at": self.created_at,
            "revoked": self.revoked,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> _StoredRecord:
        # Tolerate missing optional fields so old files from a previous
        # schema revision still load. Missing required fields raise
        # KeyError, which the store surfaces as TokenStoreError.
        return cls(
            name=data["name"],
            token_hash=data["token_hash"],
            created_at=data["created_at"],
            revoked=bool(data.get("revoked", False)),
            last_used_at=data.get("last_used_at"),
        )

    def to_public(self) -> TokenRecord:
        return TokenRecord(
            name=self.name,
            created_at=self.created_at,
            revoked=self.revoked,
            last_used_at=self.last_used_at,
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class TokenStore:
    """Fernet-encrypted node token store.

    Args:
        path: Location of the encrypted JSON file. The file is created
            on first ``create()`` if it doesn't exist; passing a
            nonexistent file to :meth:`list` returns an empty mapping
            (a freshly-paired host has no nodes yet).
        key: The Fernet key (urlsafe-base64 32-byte key, as returned by
            :func:`cryptography.fernet.Fernet.generate_key`). Must be
            non-empty. This is the same key the operator exports as
            ``HERMES_NODES_TOKEN_KEY`` per REQUIREMENTS FR-4.1.
        key_env_name: Name of the env var the key came from, used only
            for error messages. Never logged itself.

    Raises:
        TokenStoreError: Bad key, missing key, or the file exists but
            can't be decrypted with the supplied key.
    """

    path: Path
    key: str
    key_env_name: str = "HERMES_NODES_TOKEN_KEY"
    # Per-instance lock for in-process serialisation. Cross-process
    # serialisation is handled by the fcntl file lock inside _mutate.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- construction helpers ---------------------------------------------

    def __post_init__(self) -> None:
        if not self.key:
            raise TokenStoreError(
                f"Fernet key is empty (env var {self.key_env_name!r} is unset). "
                f"Generate one with: python3 -c 'from cryptography.fernet "
                f"import Fernet; print(Fernet.generate_key().decode())'"
            )
        # Cheap sanity check: Fernet keys are 44 urlsafe-base64 chars.
        # We don't validate the bytes — Fernet does that at first use —
        # but a wrong length is a common misconfiguration worth catching
        # with a friendly message.
        if len(self.key) != 44:
            raise TokenStoreError(
                f"Fernet key from {self.key_env_name!r} has wrong length: "
                f"expected 44 chars (urlsafe-base64-encoded 32 bytes), got "
                f"{len(self.key)}. Generate a fresh key with: "
                f"python3 -c 'from cryptography.fernet import Fernet; "
                f"print(Fernet.generate_key().decode())'"
            )
        # Eagerly construct Fernet so a malformed key fails at __init__
        # time, not on the first create()/list() call.
        try:
            self._fernet: Fernet = Fernet(self.key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise TokenStoreError(
                f"Fernet key from {self.key_env_name!r} is malformed: {exc}. "
                f"Generate a fresh key with: python3 -c 'from cryptography.fernet "
                f"import Fernet; print(Fernet.generate_key().decode())'"
            ) from exc

    # -- public API --------------------------------------------------------

    def create(self, name: str) -> str:
        """Generate a new token bound to ``name``, persist it, return the token.

        The returned token is the only time the plaintext is ever
        visible — the caller (``hermes node pair`` CLI) is responsible
        for printing it to the operator exactly once.

        Args:
            name: Node name to bind. Must be unique among non-revoked
                tokens (REQUIREMENTS FR-1.5). ``create`` on a revoked
                name is allowed and produces a fresh token — the
                operator is explicitly re-pairing.

        Returns:
            The new plaintext token (44-char base64url string).

        Raises:
            TokenStoreError: ``name`` is non-unique against an
                un-revoked record, or the store can't be written.
        """
        if not name or not name.strip():
            raise TokenStoreError("node name must be a non-empty string")
        name = name.strip()

        token = _generate_token()
        token_hash = _hash_token(token)
        now = _now_iso()

        def _write(records: list[_StoredRecord]) -> list[_StoredRecord]:
            for rec in records:
                if rec.name == name and not rec.revoked:
                    raise TokenStoreError(
                        f"node {name!r} is already paired; pass --force or "
                        f"revoke the existing token before re-pairing"
                    )
            records.append(
                _StoredRecord(
                    name=name,
                    token_hash=token_hash,
                    created_at=now,
                    revoked=False,
                )
            )
            return records

        self._mutate(_write)
        return token

    def list(self) -> list[TokenRecord]:
        """Return public records for every node (including revoked).

        Order is creation order, oldest first. Stable across reads
        because we sort by ``created_at`` on the way out.
        """
        records = self._read()
        records.sort(key=lambda r: r.created_at)
        return [r.to_public() for r in records]

    def revoke(self, name: str) -> None:
        """Mark the token bound to ``name`` as revoked.

        After revoke, ``validate(name, token)`` returns ``False`` for
        the original token. Revoking an already-revoked or unknown
        name is a no-op (idempotent) — CLI scripts that retry on
        failure shouldn't blow up.

        Args:
            name: Node name to revoke.

        Raises:
            TokenStoreError: Name is empty, or the store can't be written.
        """
        if not name or not name.strip():
            raise TokenStoreError("node name must be a non-empty string")
        name = name.strip()

        def _write(records: list[_StoredRecord]) -> list[_StoredRecord]:
            found = False
            for rec in records:
                if rec.name == name:
                    found = True
                    rec.revoked = True
            # Silently no-op on unknown names. This matches the
            # REQUIREMENTS FR-1.4 "drops any active connection" only
            # at the server-connection level; at the store level, a
            # revoke of a name we don't know is a safe no-op.
            _ = found
            return records

        self._mutate(_write)

    def validate(self, name: str, presented_token: str) -> bool:
        """Return True iff ``presented_token`` is the live token for ``name``.

        "Live" means: a record exists for ``name``, the record is not
        revoked, and the SHA-256 of ``presented_token`` matches the
        record's stored hash. Token comparison goes through
        :func:`hmac.compare_digest` for constant-time semantics
        (REQUIREMENTS NFR-1.1).

        On success, the record's ``last_used_at`` is updated and
        persisted. We do this best-effort: if the write fails (disk
        full, permissions), the validation result is still True — the
        caller authenticated, the audit update is a nice-to-have.

        Args:
            name: Node name the presenter claims.
            presented_token: Token the presenter offered. The caller
                is responsible for the wire format (we just compare
                bytes).
        """
        if not name or not presented_token:
            return False

        records = self._read()
        match: _StoredRecord | None = None
        for rec in records:
            if rec.name == name and not rec.revoked:
                match = rec
                break
        if match is None:
            return False

        # Constant-time hash compare. We compare hex digests (strings)
        # rather than raw bytes because that's what we store, and
        # hmac.compare_digest requires equal-length inputs.
        candidate = _hash_token(presented_token)
        if not hmac.compare_digest(candidate, match.token_hash):
            return False

        # Successful auth → update last_used_at. Best-effort; ignore
        # write errors so a disk hiccup doesn't lock the node out.
        # We catch ``Exception`` (not just ``TokenStoreError``) because
        # :meth:`_write_all` can raise low-level :class:`OSError` from
        # ``os.replace`` / ``os.fdopen`` / ``tempfile.mkstemp`` / the
        # ``mkdir`` call on a read-only directory, plus a ``TypeError``
        # if the parent path can't be coerced to ``str``. The docstring
        # above ("if the write fails ... the validation result is still
        # True") promises best-effort semantics, so we honour the
        # contract by swallowing *all* write-time errors and returning
        # ``True`` — the auth already succeeded. The previous narrower
        # ``except TokenStoreError`` was dead code: nothing in the
        # current write path raises it.
        now = _now_iso()
        match.last_used_at = now
        try:
            self._write_all(records)
        except Exception:
            # Intentionally broad: a transient disk/permission failure
            # must not convert a successful auth into a server crash.
            # The auth result is the source of truth; the audit update
            # is a nice-to-have and will be retried on the next
            # successful validate.
            pass
        return True

    # -- internals ---------------------------------------------------------

    def _read(self) -> list[_StoredRecord]:
        """Read and decrypt the store. Missing file → empty list."""
        if not self.path.exists():
            return []
        ciphertext = self.path.read_bytes()
        if not ciphertext:
            # Treat an empty file as "no tokens" rather than an error.
            # Crashes during write could leave a zero-byte file behind;
            # refusing to start in that state would block recovery.
            return []
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise TokenStoreError(
                f"failed to decrypt token store at {self.path}: the Fernet "
                f"key from {self.key_env_name!r} does not match. Either the "
                f"file was encrypted with a different key, or the file is "
                f"corrupt. To recover: re-pair all nodes with `hermes node "
                f"pair` after exporting the correct key."
            ) from exc
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TokenStoreError(
                f"token store at {self.path} decrypted but the plaintext is "
                f"not valid JSON: {exc}. The file is corrupt. To recover: "
                f"re-pair all nodes with `hermes node pair`."
            ) from exc
        if not isinstance(data, dict) or "records" not in data:
            raise TokenStoreError(
                f"token store at {self.path} has unexpected shape: expected "
                f"a top-level object with a 'records' array, got "
                f"{type(data).__name__}."
            )
        raw_records = data["records"]
        if not isinstance(raw_records, list):
            raise TokenStoreError(
                f"token store at {self.path}: 'records' must be a list, got "
                f"{type(raw_records).__name__}."
            )
        return [_StoredRecord.from_dict(r) for r in raw_records]

    def _write_all(self, records: list[_StoredRecord]) -> None:
        """Encrypt and write the full record list (atomic via temp+rename).

        Crash-safety recipe (issue #16):

        1. Write the ciphertext to a temp file in the same directory.
        2. ``fsync`` the temp file so its data is on disk before the
           rename. Without this, a power loss between the rename and
           the next page-cache flush can leave the new file's inode
           pointing at zero bytes or stale data.
        3. ``os.replace`` the temp file onto the target. Same-directory
           rename is atomic on POSIX and Windows (since Python 3.3).
        4. ``fsync`` the parent directory so the directory entry
           update (the rename) is durable too. Without this, a crash
           can leave the directory pointing at the *old* inode even
           though ``os.replace`` returned successfully.

        Both ``fsync`` calls are wrapped in
        :func:`contextlib.suppress` because some filesystems (FUSE,
        ``/dev/null``, network mounts) raise ``OSError`` on fsync; we
        do not want a transient fsync failure to convert a successful
        write into a crash — the underlying write either succeeded or
        raised, and the fsync is a durability best-effort.
        """
        payload = {"version": 1, "records": [r.to_dict() for r in records]}
        plaintext = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        ciphertext = self._fernet.encrypt(plaintext)
        # Atomic write: write to a temp file in the same directory, then
        # rename. Same-directory rename is atomic on POSIX and Windows
        # (since Python 3.3, via os.replace). A crash mid-write leaves
        # the old file intact rather than a half-written new one.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".tokens.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(ciphertext)
                f.flush()
                # Durability: make sure the temp file's data is on
                # disk before we rename. If the rename succeeds and
                # the kernel crashes before flushing, the new file
                # could exist with zero/garbage bytes.
                with contextlib.suppress(OSError):
                    os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
            # Durability: make sure the directory entry update from
            # the rename is on disk. Without this, a power loss can
            # leave the directory pointing at the *old* inode even
            # though ``os.replace`` already returned.
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                with contextlib.suppress(OSError):
                    os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise

    def _mutate(self, fn) -> None:
        """Read-modify-write under both an in-process lock and a file lock.

        ``fn`` receives the current record list and returns the new
        record list. It may mutate the list in place (revoke does) or
        return a new list (create does); we persist whatever it
        returns.

        The file lock (``fcntl.flock``) serialises against *other
        processes* touching the same file — the CLI's ``hermes node
        pair`` racing with the long-running server's read-modify-write
        is the realistic case. The threading lock serialises against
        *this process* calling two mutations on the same store, which
        can't happen in normal usage but is easy to accidentally do in
        tests.
        """
        with self._lock:
            with _file_lock(self.path):
                records = self._read()
                new_records = fn(records)
                self._write_all(new_records)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    """32 cryptographically-random bytes encoded as urlsafe-base64 (no padding)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of the token. We never store the token itself.

    SHA-256 (not HMAC) is fine here because the token already has
    256 bits of entropy from :func:`secrets.token_urlsafe` — the
    attacker can't precompute a rainbow table. SHA-256 is faster than
    HMAC-SHA256, and we only call it on the auth path (latency-
    sensitive).
    """
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """UTC timestamp in ISO-8601 with a ``Z`` suffix, second precision.

    We use ``Z`` instead of ``+00:00`` for visual compactness; both
    are RFC 3339 and most parsers (including stdlib
    :func:`datetime.fromisoformat` since 3.11) accept both.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def _file_lock(path: Path):
    """Acquire an exclusive ``fcntl.flock`` on ``path``.

    If the file doesn't exist yet (first create), we open ``path`` for
    read so the lock applies to a stable inode; the lock is released
    when the context exits and the FD is closed.

    No-op on platforms without :mod:`fcntl` (Windows). On Windows the
    single-process serialisation is provided by the threading.Lock;
    cross-process races rely on the atomic-rename write and are
    best-effort there. The CI target is Linux per REQUIREMENTS
    NFR-4.2.
    """
    try:
        import fcntl as _fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        yield
        return

    # Open the file (creating empty if missing) so flock has an inode
    # to attach to. ``a+`` creates if needed, doesn't truncate, and
    # allows subsequent reads.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        yield
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Convenience: build a store from the plugin's loaded config
# ---------------------------------------------------------------------------


def token_store_from_config(config: Any) -> TokenStore:
    """Construct a :class:`TokenStore` from a :class:`NodeServerConfig`.

    Reads the Fernet key from the env var named by
    ``config.token_encryption_key_env``. Raises :class:`TokenStoreError`
    with a clear message if the env var is unset (REQUIREMENTS FR-4.2:
    "refuses to start with a clear error message").
    """
    key = config.token_encryption_key()
    if not key:
        raise TokenStoreError(
            f"Fernet key env var {config.token_encryption_key_env!r} is unset. "
            f"Generate one with: python3 -c 'from cryptography.fernet import "
            f"Fernet; print(Fernet.generate_key().decode())' — then export "
            f"it as {config.token_encryption_key_env}=<key> in your shell "
            f"or in ~/.hermes/.env (sourced by Hermes)."
        )
    return TokenStore(
        path=Path(config.token_store_path),
        key=key,
        key_env_name=config.token_encryption_key_env,
    )


__all__ = [
    "TokenRecord",
    "TokenStore",
    "TokenStoreError",
    "token_store_from_config",
]
