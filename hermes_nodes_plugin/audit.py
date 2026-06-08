"""Append-only JSONL audit log for hermes-nodes-plugin calls (Task 2.9).

Every ``node_exec`` / ``node_read`` / ``node_write`` call (successful,
errored, timed out, or refused because the node is offline) is recorded
as one JSON object terminated by ``'\n'``. The on-disk format mirrors the
node-side audit log (Go ``internal/audit``) so the two streams can be
joined on ``request_id`` for an end-to-end trail of any call.

Wire-level fields (FR-5.1 + PROTOCOL §7)
---------------------------------------

Every entry carries:

  * ``ts``           — ISO-8601 / RFC3339 UTC, e.g. ``"2026-06-05T12:00:00.123456+00:00"``
  * ``node``         — node name as paired (e.g. ``"work-laptop"``)
  * ``action``       — one of ``"exec"``, ``"read"``, ``"write"``
  * ``request_id``   — UUIDv4 the *server* generated; matches the ``id``
                       field on the wire request and (when the node
                       faithfully copies it) the node-side audit row
  * ``duration_ms``  — wall-clock milliseconds from "we built the
                       payload" to "we have a final outcome"
  * ``exit_code``    — process exit code for ``exec``; ``0`` for
                       ``read``/``write`` success, ``-1`` on protocol
                       error / not-connected / timeout
  * ``status``       — ``"ok"`` | ``"error"`` | ``"timeout"`` |
                       ``"not_connected"`` (the last is server-side
                       only — the node never sees a not-connected call
                       because we never dispatch one)
  * ``error``        — human-readable error string; present only when
                       ``status != "ok"``

Field naming follows the task spec (``node``, not ``target``) and
PROTOCOL §7. The node-side Go code currently names its node-identity
field ``target`` (used there for the command/path string, not the
node name); bringing that into alignment is a separate node-side
task. The server-side schema is the one we own here, and it must
match PROTOCOL §7.

Disk layout
-----------

The active log lives at ``~/.hermes/logs/nodes-audit.log`` (override
via ``NodeServerConfig.audit_log_path``). The parent directory is
created with mode ``0700``; the file is opened with mode ``0600``
because audit entries can carry sensitive operational data
(commands, paths, exit codes) and should not be world-readable on
a multi-user host.

When the active log exceeds ``max_bytes`` (default 50 MiB, matching
the node side), it rotates: ``audit.log.1`` is renamed to
``.2``, ``audit.log`` to ``.1``, and a fresh ``audit.log`` is opened.
At most ``keep`` (default 5) rotated files are retained, so disk
usage is bounded by ``(keep + 1) * max_bytes``.

Retention (FR-5.4) is a separate concern from rotation. The default
retention is 1 year (server-side, per the resolved-decisions table in
REQUIREMENTS.md §5.3). :meth:`AuditWriter.purge_expired_rotations`
walks ``audit.log.*`` and deletes any whose mtime is older than
``retention_days``. The plugin's lifecycle hook calls the purge at
session start; tests call it directly.

Concurrency
-----------

:meth:`AuditWriter.record` is safe to call from multiple threads /
tasks. The implementation holds a ``threading.Lock`` around the
open/marshal/rotate/write/close sequence so two concurrent records
cannot interleave a partial line. Because writes are short and
synchronous, no async surface is needed; callers in async code call
``record()`` directly (the lock is cheap to acquire).

Failure mode
------------

Audit writes are best-effort. If the log path is unwritable, the
:class:`AuditWriter.record` call logs a WARNING via the
``hermes_nodes_plugin`` logger and returns ``False`` rather than
raising — losing an audit row must not break a real ``node_exec``
call. The 2.9 acceptance criterion (``After 5 mixed calls, ``tail -f``
shows 5 entries``) is met as long as the path is writable, which
matches the operator's reasonable expectation.

Public surface
--------------

* :class:`AuditWriter` — the writer. Construct with
  :func:`default_audit_writer` for production use; construct directly
  in tests for isolation.
* :func:`default_audit_writer` — module-level singleton accessor (lazily
  built from :class:`NodeServerConfig`). Tests call
  :func:`reset_default_audit_writer` to clear the singleton.
* :func:`reset_default_audit_writer` — clears the singleton (tests only).

Examples
--------

Production wiring (handled by the plugin lifecycle)::

    writer = default_audit_writer()
    writer.record(action="exec", node="laptop", request_id=rid,
                  duration_ms=123, exit_code=0, status="ok")

Tests::

    w = AuditWriter(path=tmp_path / "audit.log", max_bytes=100, keep=2)
    w.record(action="exec", node="x", request_id="r1", duration_ms=1,
             exit_code=0, status="ok")
    assert json.loads(tmp_path.joinpath("audit.log").read_text().strip())
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — keep aligned with the Go side (internal/audit/audit.go) so the
# two streams have a similar shape and disk budget on each end.
# ---------------------------------------------------------------------------

#: Default rotation threshold (bytes). Matches the Go side.
DEFAULT_MAX_BYTES: Final[int] = 50 * 1024 * 1024  # 50 MiB

#: Default rotated-file count (active + ``keep`` rotations).
DEFAULT_KEEP: Final[int] = 5

#: Default retention for rotated files. FR-5.4: 1 year server-side.
DEFAULT_RETENTION_DAYS: Final[int] = 365

#: Env var name for the retention override (FR-5.4).
RETENTION_ENV_VAR: Final[str] = "HERMES_NODES_AUDIT_RETENTION_DAYS"

#: Env var name for the audit log path override.
LOG_PATH_ENV_VAR: Final[str] = "HERMES_NODES_AUDIT_LOG_PATH"

#: Default audit log path. Matches REQUIREMENTS.md FR-5.1 verbatim.
DEFAULT_AUDIT_LOG_PATH: Final[Path] = Path("~/.hermes/logs/nodes-audit.log").expanduser()

#: Status values, kept as a literal set for typo-safety at the call site.
STATUS_OK: Final[str] = "ok"
STATUS_ERROR: Final[str] = "error"
STATUS_TIMEOUT: Final[str] = "timeout"
STATUS_NOT_CONNECTED: Final[str] = "not_connected"
_VALID_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_OK, STATUS_ERROR, STATUS_TIMEOUT, STATUS_NOT_CONNECTED}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuditError(RuntimeError):
    """Raised when the audit log cannot be created or rotated.

    Caught at the plugin boundary (:meth:`AuditWriter.record`) and
    downgraded to a WARNING log line so a broken audit log does not
    take down the host. Surfaced to the caller as a ``False`` return
    value from :meth:`record`.
    """


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditConfig:
    """Resolved audit-writer settings (a subset of :class:`NodeServerConfig`).

    Held as its own dataclass so the audit module does not need to
    import the full config tree to build a writer (and so tests can
    construct one in two lines). Mirrors the relevant ``NodeServerConfig``
    fields: ``audit_log_path`` and ``audit_retention_days``.
    """

    path: Path
    retention_days: int = DEFAULT_RETENTION_DAYS

    def __post_init__(self) -> None:
        if self.retention_days <= 0:
            raise ValueError(
                f"retention_days must be > 0, got {self.retention_days!r}"
            )


class AuditWriter:
    """Append-only JSONL audit logger with size-based rotation + time-based retention.

    The active log is opened with ``O_APPEND | O_CREAT`` and mode ``0600``
    so multiple processes (or a crash + restart) cannot interleave
    partial records. :meth:`record` serialises one :class:`Entry`-shaped
    mapping to a single JSON line terminated by ``'\n'``, then writes
    and ``fsync``s the result.

    The writer is goroutine-safe via a :class:`threading.Lock`; the
    critical section spans rotate + write + fsync so a rotation that
    races a record does not lose or duplicate a line.

    Construction is cheap and does not open the file; :meth:`record`
    opens lazily. Call :meth:`close` to release the file handle (the
    plugin's session-end hook does this; tests do it in
    ``addfinalizer``).
    """

    def __init__(
        self,
        *,
        path: Path | str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be > 0, got {max_bytes!r}")
        if keep < 0:
            raise ValueError(f"keep must be >= 0, got {keep!r}")
        if retention_days <= 0:
            raise ValueError(
                f"retention_days must be > 0, got {retention_days!r}"
            )
        self._path = Path(path).expanduser()
        self._max_bytes = int(max_bytes)
        self._keep = int(keep)
        self._retention_days = int(retention_days)
        self._lock = threading.Lock()
        self._file: Any = None  # opened lazily; never across instances
        # ``_closed`` is the "intentionally shut down" flag. It is
        # distinct from ``_file is None`` because rotation also
        # closes + reopens the file internally, and we do not
        # want a record to reopen a writer the user explicitly
        # closed (that would silently re-create the audit log
        # the user thought they had shut down).
        self._closed = False

    # -- introspection (tests + the runner) --------------------------------

    @property
    def path(self) -> Path:
        """Absolute path to the active log file."""
        return self._path

    @property
    def max_bytes(self) -> int:
        """Active log rotation threshold in bytes."""
        return self._max_bytes

    @property
    def keep(self) -> int:
        """Number of rotated files retained alongside the active log."""
        return self._keep

    @property
    def retention_days(self) -> int:
        """Retention window for rotated files, in days."""
        return self._retention_days

    # -- public API --------------------------------------------------------

    def record(
        self,
        *,
        action: str,
        node: str,
        request_id: str,
        duration_ms: int,
        status: str,
        exit_code: int = 0,
        error: str | None = None,
        ts: datetime | None = None,
    ) -> bool:
        """Append one audit entry to the active log.

        Args:
            action: ``"exec"`` / ``"read"`` / ``"write"``. Other
                values are accepted as-is so a future tool addition
                does not need a code change here — the row is still
                valid JSONL.
            node: The node name as paired (the :class:`NodeEnvironment`
                target). The empty string is rejected because a row
                without a node identifier is unfindable in the log.
            request_id: UUIDv4 the server generated for the call.
                Required for correlation with the node-side log.
            duration_ms: Wall-clock milliseconds from "we built the
                payload" to "we have a final outcome". Must be ``>= 0``.
            status: One of ``STATUS_OK`` / ``STATUS_ERROR`` /
                ``STATUS_TIMEOUT`` / ``STATUS_NOT_CONNECTED``. An
                unknown status is rejected (typo guard) because the
                node-side log uses the same vocabulary and a typo
                here would silently break correlation.
            exit_code: Process exit code for ``exec``; ``0`` for
                ``read``/``write`` success, ``-1`` on protocol
                error / not-connected / timeout. Defaults to ``0``.
            error: Human-readable error string; only meaningful when
                ``status != STATUS_OK``. ``None`` (the default) means
                "no error" and the field is omitted from the JSON
                row (avoids noisy ``"error": null`` in success rows).
            ts: Timestamp to record. Defaults to "now" (UTC). Tests
                inject a fixed value to make assertions
                deterministic.

        Returns:
            ``True`` on a successful write; ``False`` if the write
            failed for any reason (path unwritable, marshal error,
            rotation failure, etc.). The plugin never raises from
            this method — a broken audit log is bad but losing a
            node call is worse. Rotation failures are logged at
            WARNING and swallowed so a transient filesystem hiccup
            does not abort the calling pipeline (Issue #11).

        Raises:
            ValueError: ``node`` empty, ``request_id`` empty, or
                ``status`` not in the known set. These are programmer
                errors caught at the call site, not I/O failures.
        """
        if not node:
            raise ValueError("AuditWriter.record: node must be a non-empty string")
        if not request_id:
            raise ValueError(
                "AuditWriter.record: request_id must be a non-empty string"
            )
        if duration_ms < 0:
            raise ValueError(
                f"AuditWriter.record: duration_ms must be >= 0, got {duration_ms!r}"
            )
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"AuditWriter.record: status must be one of "
                f"{sorted(_VALID_STATUSES)}, got {status!r}"
            )

        # Build the row. ``ts`` first so it lines up with the Go
        # struct ordering and with what a grep user sees. ISO-8601
        # with microsecond precision + explicit UTC offset; ``+00:00``
        # is identical to ``Z`` semantically and easier to read in
        # tooling that doesn't know the ``Z`` alias.
        when = ts if ts is not None else datetime.now(timezone.utc)
        row: dict[str, Any] = {
            "ts": when.isoformat(),
            "node": node,
            "action": action,
            "request_id": request_id,
            "duration_ms": int(duration_ms),
            "exit_code": int(exit_code),
            "status": status,
        }
        if error:
            row["error"] = error

        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"

        try:
            with self._lock:
                if self._closed:
                    # Defensive: a closed writer must not
                    # silently re-open. Log a warning and skip
                    # the write rather than losing the row
                    # silently or breaking the call site.
                    logger.warning(
                        "hermes-nodes audit: record() called on a closed writer "
                        "(node=%r, action=%r, status=%r) — row dropped",
                        node,
                        action,
                        status,
                    )
                    return False
                self._ensure_open_locked()
                if self._should_rotate_locked():
                    self._rotate_locked()
                if self._file is None:
                    # Rotation didn't reopen — defensive.
                    raise AuditError("audit file is not open after rotate")
                self._file.write(line)
                self._file.flush()
                # ``os.fsync`` is the same Sync the Go side does.
                # It costs ~one disk rotation on HDD; the audit
                # log's correctness guarantee (no lost rows on
                # crash) is worth it. Skip only when the file is
                # a non-block device (e.g. /dev/null in tests).
                if self._file.fileno() >= 0:
                    try:
                        os.fsync(self._file.fileno())
                    except OSError:
                        # Some filesystems (FUSE, /dev/null) don't
                        # support fsync. The data is in the buffer
                        # and the next write would have flushed
                        # anyway, so swallow.
                        pass
        except (OSError, ValueError, AuditError) as exc:
            # I/O, marshal, or rotation failure. Don't raise —
            # losing the audit row is bad, but losing the user's
            # call is worse. The WARNING surfaces in ``hermes
            # logs`` for operator triage. AuditError covers
            # rotation errors that would otherwise violate the
            # ``record()`` never-raises contract (Issue #11).
            logger.warning(
                "hermes-nodes audit: failed to write entry "
                "(node=%r, action=%r, status=%r): %s",
                node,
                action,
                status,
                exc,
            )
            return False
        return True

    def close(self) -> None:
        """Flush + close the underlying file. Idempotent; safe on a closed writer."""
        with self._lock:
            self._closed = True
            if self._file is None:
                return
            try:
                self._file.flush()
            except OSError:
                pass
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def purge_expired_rotations(self, *, now: float | None = None) -> int:
        """Delete ``audit.log.N`` files older than ``retention_days``.

        Called by the plugin lifecycle at session start so the disk
        footprint of long-lived installations stays bounded. Pure
        side effect — returns the count of files deleted so tests
        can assert it.

        The active log (``audit.log``) is never purged; only the
        rotated siblings are eligible. This matches the Go side's
        behavior (rotation keeps the active log; retention is
        implicit because the Go side ships with the same 1-year
        default on the server).

        Args:
            now: Override the "now" reference time (epoch seconds).
                Defaults to :func:`time.time`. Tests inject a fixed
                value to make age-based assertions deterministic.

        Returns:
            Number of files deleted. Zero is a valid return.
        """
        if now is None:
            now = time.time()
        cutoff = now - (self._retention_days * 86400)
        deleted = 0
        parent = self._path.parent
        if not parent.exists():
            return 0
        # Walk every rotation slot. We don't depend on the file
        # naming being numeric — anything that starts with the
        # active path + "." is treated as a rotation. (The Go
        # side uses ``.1`` / ``.2`` / ...; this is forward-compat
        # with a future ``.YYYYMMDD`` scheme.)
        prefix = self._path.name + "."
        try:
            entries = sorted(parent.iterdir())
        except OSError as exc:
            logger.warning(
                "hermes-nodes audit: cannot list %s to purge rotations: %s",
                parent,
                exc,
            )
            return 0
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            if not entry.is_file():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            try:
                entry.unlink()
                deleted += 1
            except OSError as exc:
                # Don't fail the whole sweep on one unreadable file.
                logger.warning(
                    "hermes-nodes audit: cannot delete expired rotation %s: %s",
                    entry,
                    exc,
                )
        return deleted

    # -- internals (caller holds self._lock) -------------------------------

    def _ensure_open_locked(self) -> None:
        """Open the active log if it isn't already open.

        Creates the parent directory with mode ``0700`` and the
        file with mode ``0600``. Mirrors the Go side's
        ``MkdirAll(... 0o700)`` / ``OpenFile(... 0o600)`` choices.
        """
        if self._file is not None:
            return
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            # ``exist_ok=True`` is the common path; on first run the
            # directory does not exist and we create it. Tighten the
            # mode in case ``mkdir`` raced a less-strict parent.
            try:
                os.chmod(parent, 0o700)
            except OSError:
                # Best-effort: /var/log or similar may not allow
                # chmod. The 0600 on the file still holds.
                pass
            fd = os.open(
                str(self._path),
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
        except OSError as exc:
            raise AuditError(
                f"audit: cannot open {self._path}: {exc}"
            ) from exc
        self._file = os.fdopen(fd, "a", encoding="utf-8")

    def _should_rotate_locked(self) -> bool:
        if self._file is None:
            return False
        try:
            size = os.fstat(self._file.fileno()).st_size
        except OSError:
            return False
        return size >= self._max_bytes

    def _rotate_locked(self) -> None:
        """Shift ``audit.log`` -> ``.1`` -> ``.2`` -> ... and open a fresh active.

        Mirrors the Go side's rotateLocked. The free slot at ``.keep``
        is freed by deleting whatever sits there, then the chain is
        shifted up by one (newer → older), and finally the active log
        is renamed to ``.1`` and a fresh file is opened.

        Any individual rename that hits a non-existent source is
        treated as success (rotations past the current ``keep`` count
        have no source file). The same tolerance appears in the Go
        implementation (``os.ErrNotExist`` checks).
        """
        if self._file is None:
            return
        # Close the active file so the rename is portable across
        # operating systems (Windows refuses to rename an open file).
        try:
            self._file.flush()
        except OSError:
            pass
        try:
            self._file.close()
        except OSError:
            pass
        self._file = None

        # Drop the oldest rotation if it exists.
        oldest = self._path.with_name(self._path.name + f".{self._keep}")
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AuditError(f"audit: cannot remove {oldest}: {exc}") from exc

        # Shift .(N-1) → .N, .(N-2) → .(N-1), ..., .1 → .2.
        for i in range(self._keep - 1, 0, -1):
            src = self._path.with_name(self._path.name + f".{i}")
            dst = self._path.with_name(self._path.name + f".{i + 1}")
            if not src.exists():
                continue
            try:
                os.replace(src, dst)
            except OSError as exc:
                raise AuditError(
                    f"audit: cannot rename {src} -> {dst}: {exc}"
                ) from exc

        # Move the active log to .1.
        rotated = self._path.with_name(self._path.name + ".1")
        if self._path.exists():
            try:
                os.replace(self._path, rotated)
            except OSError as exc:
                raise AuditError(
                    f"audit: cannot rename {self._path} -> {rotated}: {exc}"
                ) from exc

        # Reopen a fresh active log. ``_ensure_open_locked`` is
        # safe to call while we hold the lock because it doesn't
        # try to re-acquire it.
        self._ensure_open_locked()

    # -- dunder ------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"AuditWriter(path={self._path!s}, "
            f"max_bytes={self._max_bytes}, keep={self._keep}, "
            f"retention_days={self._retention_days})"
        )


# ---------------------------------------------------------------------------
# Singleton — the plugin lifecycle uses a process-wide default writer so
# every call site resolves to the same log path. Tests clear the
# singleton with :func:`reset_default_audit_writer` in their fixtures.
# ---------------------------------------------------------------------------


_audit_writer: AuditWriter | None = None
_audit_writer_lock = threading.Lock()


def _resolve_audit_config(env: Mapping[str, str] | None = None) -> AuditConfig:
    """Resolve the audit settings from env vars + the active server config.

    Precedence (highest to lowest):

      1. ``HERMES_NODES_AUDIT_LOG_PATH`` / ``HERMES_NODES_AUDIT_RETENTION_DAYS``
         env vars (per FR-5.4 + the env-var contract).
      2. :class:`NodeServerConfig` fields (``audit_log_path``,
         ``audit_retention_days``).
      3. Built-in defaults.
    """
    if env is None:
        env = os.environ
    # Pull the server config without forcing a token-store load —
    # ``load_config`` does not touch the token store, so it is safe
    # at audit-writer construction time. The import is local to
    # avoid a circular import: ``config`` reads the constants
    # below back from this module (``audit``), so the top-level
    # ``from config import ...`` would deadlock.
    from hermes_nodes_plugin.config import NodeServerConfig, load_config

    try:
        server_cfg = load_config(env=env)
    except Exception:
        # A broken YAML or a partial TLS config should not block
        # audit logging — fall back to defaults so the operator at
        # least has the audit trail. The server's own startup will
        # surface the config error in the appropriate log channel.
        server_cfg = NodeServerConfig()

    log_path_raw = env.get(LOG_PATH_ENV_VAR) or server_cfg.audit_log_path
    log_path = Path(log_path_raw).expanduser()

    retention_raw = env.get(RETENTION_ENV_VAR) or str(server_cfg.audit_retention_days)
    try:
        retention = int(retention_raw)
    except (TypeError, ValueError):
        logger.warning(
            "hermes-nodes audit: invalid %s=%r; using default %d",
            RETENTION_ENV_VAR,
            retention_raw,
            DEFAULT_RETENTION_DAYS,
        )
        retention = DEFAULT_RETENTION_DAYS
    if retention <= 0:
        retention = DEFAULT_RETENTION_DAYS

    return AuditConfig(path=log_path, retention_days=retention)


def default_audit_writer() -> AuditWriter:
    """Return the process-wide :class:`AuditWriter`, building it on first call.

    The first call resolves the config from env + the server config
    and constructs a writer. Subsequent calls return the same object,
    so concurrent :meth:`AuditWriter.record` calls share a single
    file handle and rotation state. Tests call
    :func:`reset_default_audit_writer` to clear the singleton.
    """
    global _audit_writer
    if _audit_writer is None:
        with _audit_writer_lock:
            if _audit_writer is None:
                cfg = _resolve_audit_config()
                _audit_writer = AuditWriter(
                    path=cfg.path,
                    retention_days=cfg.retention_days,
                )
    return _audit_writer


def reset_default_audit_writer() -> None:
    """Clear the singleton (tests only). Closes the file handle if open."""
    global _audit_writer
    with _audit_writer_lock:
        if _audit_writer is not None:
            _audit_writer.close()
        _audit_writer = None


__all__ = [
    "AuditConfig",
    "AuditError",
    "AuditWriter",
    "DEFAULT_AUDIT_LOG_PATH",
    "DEFAULT_KEEP",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_RETENTION_DAYS",
    "LOG_PATH_ENV_VAR",
    "RETENTION_ENV_VAR",
    "STATUS_ERROR",
    "STATUS_NOT_CONNECTED",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "default_audit_writer",
    "reset_default_audit_writer",
]
