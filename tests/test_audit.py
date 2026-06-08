"""Tests for :mod:`hermes_nodes_plugin.audit` (Task 2.9).

Coverage:

  * :class:`AuditWriter` round-trip — every status / action / exit
    code combination lands on disk in the documented JSONL shape.
  * Field shape — exactly the keys FR-5.1 names, in the order the
    Go-side audit (``hermes-nodes/internal/audit``) writes them, so
    the two streams can be ``join``-ed on ``request_id``.
  * Rotation — at ``max_bytes``, the active log is shifted to
    ``.1`` and a fresh file is opened. Old rotations are bounded
    by ``keep``.
  * Retention — :meth:`AuditWriter.purge_expired_rotations` deletes
    rotations whose mtime is older than ``retention_days``.
  * Singleton / lifecycle — :func:`default_audit_writer` resolves
    from env + server config; :func:`reset_default_audit_writer`
    closes the file handle so tests don't leak fds.
  * Integration with :class:`NodeEnvironment` — every exit path
    (``ok``, ``error``, ``timeout``, ``not_connected``) produces
    one audit row, regardless of whether the call hit the wire.

Tests construct :class:`AuditWriter` directly rather than going
through :func:`default_audit_writer` so they never touch the real
``~/.hermes/logs`` directory. The ``audit=`` constructor parameter
on :class:`NodeEnvironment` makes the integration tests fully
hermetic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_nodes_plugin.audit import (
    DEFAULT_AUDIT_LOG_PATH,
    DEFAULT_KEEP,
    DEFAULT_MAX_BYTES,
    DEFAULT_RETENTION_DAYS,
    LOG_PATH_ENV_VAR,
    RETENTION_ENV_VAR,
    STATUS_ERROR,
    STATUS_NOT_CONNECTED,
    STATUS_OK,
    AuditConfig,
    AuditError,
    AuditWriter,
    default_audit_writer,
    reset_default_audit_writer,
)
from hermes_nodes_plugin.config import NodeServerConfig, load_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_env() -> dict[str, str]:
    """A pristine env mapping with no HERMES_NODES_* vars set.

    The audit / config loaders read ``env.get(...)``, so we pass a
    fresh dict rather than ``os.environ`` to keep the tests
    hermetic.
    """
    return {}


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    """A fresh per-test path for the audit log.

    Distinct from ``tmp_path`` so tests can also pass ``tmp_path``
    for unrelated fixtures without colliding.
    """
    return tmp_path / "audit.log"


@pytest.fixture
def writer(audit_path: Path) -> AuditWriter:
    """An :class:`AuditWriter` rooted at ``audit_path`` with conservative limits.

    The default ``max_bytes`` (50 MiB) is far too large for tests;
    we use a much smaller value so a single ``record`` triggers
    rotation. Tests that want the production-size limits
    instantiate their own :class:`AuditWriter`.
    """
    w = AuditWriter(path=audit_path, max_bytes=200, keep=3)
    yield w
    w.close()


def _read_jsonl(path: Path) -> list[dict]:
    """Read ``path`` as JSONL; return one dict per non-empty line.

    Tests use this everywhere instead of re-implementing the
    scan-and-decode dance, and to keep the assertions focused on
    the row contents.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# AuditWriter: construction & argument validation
# ---------------------------------------------------------------------------


class TestAuditWriterConstruction:
    def test_rejects_invalid_max_bytes(self, audit_path: Path) -> None:
        with pytest.raises(ValueError, match="max_bytes must be > 0"):
            AuditWriter(path=audit_path, max_bytes=0)

    def test_rejects_negative_max_bytes(self, audit_path: Path) -> None:
        with pytest.raises(ValueError, match="max_bytes must be > 0"):
            AuditWriter(path=audit_path, max_bytes=-1)

    def test_rejects_negative_keep(self, audit_path: Path) -> None:
        with pytest.raises(ValueError, match="keep must be >= 0"):
            AuditWriter(path=audit_path, keep=-1)

    def test_rejects_zero_retention_days(self, audit_path: Path) -> None:
        with pytest.raises(ValueError, match="retention_days must be > 0"):
            AuditWriter(path=audit_path, retention_days=0)

    def test_default_max_bytes_matches_node_side(self) -> None:
        """The server-side default must equal the Go side's.

        FR-5.4 + the design note: the Go node side rotates at
        50 MiB / 5 files, and the server side keeps the same
        defaults so a paired laptop and VPS have comparable disk
        budgets for their respective logs.
        """
        assert DEFAULT_MAX_BYTES == 50 * 1024 * 1024
        assert DEFAULT_KEEP == 5

    def test_default_audit_path_matches_fr51(self) -> None:
        """FR-5.1 names ``~/.hermes/logs/nodes-audit.log`` as the default.

        The constant is constructed via ``Path("~/.hermes/...").expanduser()``
        so its string form is the operator's home expansion, not
        the literal ``~`` — the assertion below uses the home
        expansion so the test passes regardless of which user
        runs it.
        """
        home = Path("~").expanduser()
        assert str(DEFAULT_AUDIT_LOG_PATH) == str(
            home / ".hermes" / "logs" / "nodes-audit.log"
        )

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        deep = tmp_path / "nested" / "logs" / "audit.log"
        w = AuditWriter(path=deep, max_bytes=1024, keep=2)
        try:
            w.record(
                action="exec",
                node="laptop",
                request_id="r1",
                duration_ms=1,
                status=STATUS_OK,
            )
        finally:
            w.close()
        assert deep.exists()
        # The parent directory must be ``0700`` per the Go-side
        # convention. ``stat`` in octal; on a tmpfs the umask
        # might mask the top bit so we check the lower 6.
        mode = deep.parent.stat().st_mode & 0o777
        assert mode == 0o700, f"parent dir mode is {oct(mode)}, want 0o700"

    def test_creates_file_with_mode_0600(self, audit_path: Path) -> None:
        w = AuditWriter(path=audit_path, max_bytes=1024, keep=2)
        try:
            w.record(
                action="exec",
                node="laptop",
                request_id="r1",
                duration_ms=1,
                status=STATUS_OK,
            )
        finally:
            w.close()
        mode = audit_path.stat().st_mode & 0o777
        assert mode == 0o600, f"file mode is {oct(mode)}, want 0o600"


# ---------------------------------------------------------------------------
# AuditWriter.record: row shape & validation
# ---------------------------------------------------------------------------


class TestAuditWriterRecord:
    def test_round_trip_writes_one_jsonl_line_per_record(
        self, writer: AuditWriter, audit_path: Path
    ) -> None:
        writer.record(
            action="exec",
            node="laptop",
            request_id="req-1",
            duration_ms=42,
            status=STATUS_OK,
            exit_code=0,
        )
        rows = _read_jsonl(audit_path)
        assert len(rows) == 1
        assert rows[0] == {
            "ts": rows[0]["ts"],
            "node": "laptop",
            "action": "exec",
            "request_id": "req-1",
            "duration_ms": 42,
            "exit_code": 0,
            "status": "ok",
        }

    def test_field_set_matches_fr51(self, writer: AuditWriter) -> None:
        """The wire-format field set is locked by FR-5.1.

        Adding or removing a field is a spec change, not a
        refactor — this test makes any drift a CI failure.
        """
        writer.record(
            action="exec",
            node="laptop",
            request_id="r1",
            duration_ms=1,
            status=STATUS_OK,
        )
        row = _read_jsonl(writer.path)[0]
        assert set(row.keys()) == {
            "ts",
            "node",
            "action",
            "request_id",
            "duration_ms",
            "exit_code",
            "status",
        }

    def test_error_row_includes_error_field(
        self, writer: AuditWriter, audit_path: Path
    ) -> None:
        writer.record(
            action="exec",
            node="laptop",
            request_id="r1",
            duration_ms=5,
            status=STATUS_ERROR,
            exit_code=-1,
            error="node offline",
        )
        row = _read_jsonl(audit_path)[0]
        assert row["status"] == "error"
        assert row["error"] == "node offline"

    def test_success_row_omits_error_field(
        self, writer: AuditWriter, audit_path: Path
    ) -> None:
        """Avoid noisy ``"error": null`` in success rows.

        The audit consumer is a JSONL log; greppability improves
        when optional fields are genuinely absent on success
        rather than present-with-null.
        """
        writer.record(
            action="read",
            node="laptop",
            request_id="r1",
            duration_ms=3,
            status=STATUS_OK,
        )
        row = _read_jsonl(audit_path)[0]
        assert "error" not in row

    def test_timestamp_is_iso8601_utc(
        self, writer: AuditWriter, audit_path: Path
    ) -> None:
        writer.record(
            action="exec",
            node="laptop",
            request_id="r1",
            duration_ms=1,
            status=STATUS_OK,
        )
        row = _read_jsonl(audit_path)[0]
        # Round-trip through ``datetime.fromisoformat`` to assert
        # the value is a valid ISO-8601 string. ``isoformat``
        # accepts the same string the writer emits.
        parsed = datetime.fromisoformat(row["ts"])
        assert parsed.tzinfo is not None, "ts must carry a timezone"
        assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)

    def test_injected_timestamp_is_used_verbatim(
        self, writer: AuditWriter, audit_path: Path
    ) -> None:
        """``ts`` override is honored so tests can assert deterministically."""
        when = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        writer.record(
            action="exec",
            node="laptop",
            request_id="r1",
            duration_ms=1,
            status=STATUS_OK,
            ts=when,
        )
        row = _read_jsonl(audit_path)[0]
        assert row["ts"] == when.isoformat()

    def test_appends_across_multiple_records(
        self, audit_path: Path
    ) -> None:
        # Use a fresh writer with plenty of headroom so
        # rotation does not kick in and discard earlier rows.
        w = AuditWriter(path=audit_path, max_bytes=10_000, keep=3)
        try:
            for i in range(5):
                w.record(
                    action="exec",
                    node="laptop",
                    request_id=f"r{i}",
                    duration_ms=i,
                    status=STATUS_OK,
                    exit_code=0,
                )
        finally:
            w.close()
        rows = _read_jsonl(audit_path)
        assert [r["request_id"] for r in rows] == [
            "r0",
            "r1",
            "r2",
            "r3",
            "r4",
        ]
        assert [r["duration_ms"] for r in rows] == [0, 1, 2, 3, 4]

    def test_rejects_empty_node(self, writer: AuditWriter) -> None:
        with pytest.raises(ValueError, match="node must be a non-empty string"):
            writer.record(
                action="exec",
                node="",
                request_id="r1",
                duration_ms=1,
                status=STATUS_OK,
            )

    def test_rejects_empty_request_id(self, writer: AuditWriter) -> None:
        with pytest.raises(
            ValueError, match="request_id must be a non-empty string"
        ):
            writer.record(
                action="exec",
                node="laptop",
                request_id="",
                duration_ms=1,
                status=STATUS_OK,
            )

    def test_rejects_negative_duration(self, writer: AuditWriter) -> None:
        with pytest.raises(ValueError, match="duration_ms must be >= 0"):
            writer.record(
                action="exec",
                node="laptop",
                request_id="r1",
                duration_ms=-1,
                status=STATUS_OK,
            )

    def test_rejects_unknown_status(self, writer: AuditWriter) -> None:
        """Status is a closed vocabulary — typos break correlation."""
        with pytest.raises(ValueError, match="status must be one of"):
            writer.record(
                action="exec",
                node="laptop",
                request_id="r1",
                duration_ms=1,
                status="success",  # not a valid value
            )

    def test_close_is_idempotent(self, writer: AuditWriter) -> None:
        writer.close()
        writer.close()  # second call must be a no-op

    def test_record_after_close_returns_false(
        self, audit_path: Path
    ) -> None:
        """Closing the writer is final — a post-close record is a warning + return.

        We don't raise because the plugin's call sites use a
        best-effort pattern; surfacing a hard error here would
        break the audit-on-every-exit-path invariant.
        """
        w = AuditWriter(path=audit_path, max_bytes=1024, keep=2)
        w.close()
        ok = w.record(
            action="exec",
            node="laptop",
            request_id="r1",
            duration_ms=1,
            status=STATUS_OK,
        )
        assert ok is False

    def test_rotation_failure_does_not_propagate(
        self,
        audit_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``record()`` honours its never-raises contract when rotation blows up.

        Regression for Issue #11: ``_rotate_locked`` raises
        ``AuditError`` on filesystem errors (rename failure, perm
        denied, etc.). ``record()`` must catch that, log a WARNING,
        and return ``False`` so the calling pipeline (node_exec /
        node_read / node_write) is not aborted by an audit-only
        failure.
        """
        w = AuditWriter(path=audit_path, max_bytes=50, keep=2)
        try:
            boom = AuditError("simulated rotation failure (issue #11)")

            def _raise_audit_error() -> None:
                raise boom

            monkeypatch.setattr(w, "_rotate_locked", _raise_audit_error)

            for i in range(5):
                w.record(
                    action="exec",
                    node="laptop",
                    request_id=f"pre-{i}",
                    duration_ms=1,
                    status=STATUS_OK,
                    exit_code=0,
                )

            with caplog.at_level(
                logging.WARNING, logger="hermes_nodes_plugin.audit"
            ):
                ok = w.record(
                    action="exec",
                    node="laptop",
                    request_id="post-rotation",
                    duration_ms=1,
                    status=STATUS_OK,
                    exit_code=0,
                )
            assert ok is False
            assert any(
                "simulated rotation failure (issue #11)" in rec.message
                for rec in caplog.records
            ), "rotation failure must be logged at WARNING"
        finally:
            w.close()

    def test_audit_error_outside_rotation_does_not_propagate(
        self,
        audit_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any ``AuditError`` raised inside the record critical section is swallowed.

        Guards against future refactors that might raise
        ``AuditError`` from a different point (open, fsync, etc.).
        The contract is method-wide: ``record()`` never raises
        for I/O or audit-internal failures.
        """
        w = AuditWriter(path=audit_path, max_bytes=200, keep=2)
        try:
            boom = AuditError("simulated internal failure")

            def _raise_audit_error() -> None:
                raise boom

            monkeypatch.setattr(w, "_ensure_open_locked", _raise_audit_error)
            ok = w.record(
                action="exec",
                node="laptop",
                request_id="r1",
                duration_ms=1,
                status=STATUS_OK,
            )
            assert ok is False
        finally:
            w.close()


# ---------------------------------------------------------------------------
# Rotation (size-based, mirrors the Go side)
# ---------------------------------------------------------------------------


class TestAuditWriterRotation:
    def test_rotation_creates_numbered_files(self, audit_path: Path) -> None:
        # ``max_bytes=150`` is small enough that a few rows push
        # the file over the threshold.
        w = AuditWriter(path=audit_path, max_bytes=150, keep=3)
        try:
            for i in range(5):
                w.record(
                    action="exec",
                    node="laptop",
                    request_id=f"r{i}",
                    duration_ms=i * 10,
                    status=STATUS_OK,
                    exit_code=0,
                )
        finally:
            w.close()

        # The active log holds the most recent write. The
        # rotated file (``audit.log.1``) holds earlier rows.
        assert audit_path.exists()
        assert audit_path.with_name(audit_path.name + ".1").exists()

        # Read all files in chronological order (rotation
        # files first, then the active one) and confirm the
        # request_ids appear in order.
        order = [".2", ".1", ""]
        seen: list[str] = []
        for suffix in order:
            path = audit_path.with_name(audit_path.name + suffix)
            if not path.exists():
                continue
            for row in _read_jsonl(path):
                seen.append(row["request_id"])
        assert seen == ["r0", "r1", "r2", "r3", "r4"]

    def test_rotation_drops_files_past_keep(
        self, audit_path: Path
    ) -> None:
        w = AuditWriter(path=audit_path, max_bytes=80, keep=2)
        try:
            # Each record is well over 80 bytes when serialized
            # (the long request_id padding is intentional), so
            # every write triggers a rotation. With ``keep=2``,
            # only the active log + .1 + .2 exist; .3 and
            # beyond are dropped.
            for i in range(6):
                w.record(
                    action="exec",
                    node="laptop",
                    request_id=f"req-{i:04d}-padding-padding",
                    duration_ms=1,
                    status=STATUS_OK,
                )
        finally:
            w.close()

        for allowed in ("", ".1", ".2"):
            assert (audit_path.with_name(audit_path.name + allowed)).exists(), (
                f"expected {allowed} to exist"
            )
        # ``.3`` and beyond must not exist — the rotation cap
        # is the disk-budget guard the audit log exists to
        # provide.
        assert not (audit_path.with_name(audit_path.name + ".3")).exists()

    def test_reopen_appends_not_truncates(
        self, audit_path: Path
    ) -> None:
        """A fresh writer on the same path appends to the prior content.

        Append-only is a hard requirement (FR-5.2 — "Audit log
        is append-only. There is no CLI command to delete
        entries."). The writer must NEVER truncate.
        """
        w1 = AuditWriter(path=audit_path, max_bytes=10_000, keep=2)
        w1.record(
            action="exec",
            node="laptop",
            request_id="first",
            duration_ms=1,
            status=STATUS_OK,
        )
        w1.close()

        w2 = AuditWriter(path=audit_path, max_bytes=10_000, keep=2)
        w2.record(
            action="exec",
            node="laptop",
            request_id="second",
            duration_ms=2,
            status=STATUS_OK,
        )
        w2.close()

        request_ids = [r["request_id"] for r in _read_jsonl(audit_path)]
        assert request_ids == ["first", "second"]


# ---------------------------------------------------------------------------
# Retention (time-based purge)
# ---------------------------------------------------------------------------


class TestAuditWriterRetention:
    def test_purge_removes_old_rotations(self, tmp_path: Path) -> None:
        w = AuditWriter(
            path=tmp_path / "audit.log", max_bytes=10_000, keep=5
        )
        try:
            # Create three rotation files with controlled mtimes.
            for i in range(1, 4):
                p = tmp_path / f"audit.log.{i}"
                p.write_text(f"old rotation {i}\n", encoding="utf-8")
                # Set the mtime to 400 days ago. ``utime`` uses
                # epoch seconds, so we work in seconds.
                old = time.time() - (400 * 86400)
                os.utime(p, (old, old))
        finally:
            w.close()

        purged = w.purge_expired_rotations()
        assert purged == 3
        for i in range(1, 4):
            assert not (tmp_path / f"audit.log.{i}").exists()

    def test_purge_keeps_recent_rotations(self, tmp_path: Path) -> None:
        w = AuditWriter(
            path=tmp_path / "audit.log", max_bytes=10_000, keep=5
        )
        try:
            for i in range(1, 4):
                p = tmp_path / f"audit.log.{i}"
                p.write_text(f"rotation {i}\n", encoding="utf-8")
                # Set mtime to 10 days ago — well within the
                # 365-day retention window.
                recent = time.time() - (10 * 86400)
                os.utime(p, (recent, recent))
        finally:
            w.close()

        purged = w.purge_expired_rotations()
        assert purged == 0
        for i in range(1, 4):
            assert (tmp_path / f"audit.log.{i}").exists()

    def test_purge_ignores_active_log(self, tmp_path: Path) -> None:
        """The active log is never purged, even if ancient.

        The active log is appended-to; purging it would be data
        loss for in-flight calls. Rotations only.
        """
        active = tmp_path / "audit.log"
        active.write_text("active contents\n", encoding="utf-8")
        ancient = time.time() - (400 * 86400)
        os.utime(active, (ancient, ancient))

        w = AuditWriter(path=active, max_bytes=10_000, keep=5)
        try:
            purged = w.purge_expired_rotations()
        finally:
            w.close()

        assert purged == 0
        assert active.exists()

    def test_purge_returns_count(self, tmp_path: Path) -> None:
        w = AuditWriter(
            path=tmp_path / "audit.log", max_bytes=10_000, keep=5
        )
        try:
            # Five ancient, one fresh.
            for i in range(1, 6):
                p = tmp_path / f"audit.log.{i}"
                p.write_text("x", encoding="utf-8")
                old = time.time() - (400 * 86400)
                os.utime(p, (old, old))
            fresh = tmp_path / "audit.log.6"
            fresh.write_text("x", encoding="utf-8")
        finally:
            w.close()

        purged = w.purge_expired_rotations()
        assert purged == 5
        assert fresh.exists()

    def test_purge_noop_when_log_dir_absent(self, tmp_path: Path) -> None:
        w = AuditWriter(
            path=tmp_path / "never-created" / "audit.log",
            max_bytes=10_000,
            keep=5,
        )
        # Never call record, so the parent dir is never created.
        purged = w.purge_expired_rotations()
        assert purged == 0
        w.close()

    def test_purge_uses_injected_now(self, tmp_path: Path) -> None:
        """Tests inject ``now`` so age math is deterministic.

        Without the override, the test would depend on
        ``time.time()`` and the "100 days old" file would
        actually be 100 days old — fragile in CI.
        """
        target = tmp_path / "audit.log.1"
        target.write_text("x", encoding="utf-8")
        # Set the mtime to ``now - 100 days``.
        base = 1_000_000_000.0  # arbitrary fixed instant
        os.utime(target, (base - 100 * 86400, base - 100 * 86400))

        w = AuditWriter(
            path=tmp_path / "audit.log",
            max_bytes=10_000,
            keep=5,
            retention_days=90,
        )
        # With ``now=base + 0`` the file is exactly 100 days
        # old; retention is 90 days, so it must be purged.
        purged = w.purge_expired_rotations(now=base)
        assert purged == 1
        assert not target.exists()

    def test_purge_does_not_delete_unrelated_files(
        self, tmp_path: Path
    ) -> None:
        """Purging only touches ``audit.log.N`` siblings, not other names.

        A shared log directory can hold other rotated logs
        (``agent.log.1`` etc.) — the audit purger must not
        touch them.
        """
        unrelated = tmp_path / "agent.log.1"
        unrelated.write_text("x", encoding="utf-8")
        ancient = time.time() - (400 * 86400)
        os.utime(unrelated, (ancient, ancient))

        w = AuditWriter(
            path=tmp_path / "audit.log", max_bytes=10_000, keep=5
        )
        try:
            purged = w.purge_expired_rotations()
        finally:
            w.close()
        assert purged == 0
        assert unrelated.exists()


# ---------------------------------------------------------------------------
# Singleton + default-resolution
# ---------------------------------------------------------------------------


class TestDefaultAuditWriter:
    def test_singleton_returns_same_instance(self) -> None:
        reset_default_audit_writer()
        try:
            a = default_audit_writer()
            b = default_audit_writer()
            assert a is b
        finally:
            reset_default_audit_writer()

    def test_singleton_resolves_path_from_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``HERMES_NODES_AUDIT_LOG_PATH`` overrides the default path."""
        reset_default_audit_writer()
        try:
            monkeypatch.setenv(
                LOG_PATH_ENV_VAR, str(tmp_path / "override.log")
            )
            # Block the real ``~/.hermes/hermes-nodes.yaml`` from
            # being read by an empty env. ``load_config`` falls
            # back to dataclass defaults when the file is
            # missing, so this is fine.
            monkeypatch.setenv("HOME", str(tmp_path))
            w = default_audit_writer()
            assert w.path == (tmp_path / "override.log").expanduser()
        finally:
            reset_default_audit_writer()

    def test_singleton_resolves_retention_from_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_default_audit_writer()
        try:
            monkeypatch.setenv(LOG_PATH_ENV_VAR, str(tmp_path / "a.log"))
            monkeypatch.setenv(RETENTION_ENV_VAR, "7")
            monkeypatch.setenv("HOME", str(tmp_path))
            w = default_audit_writer()
            assert w.retention_days == 7
        finally:
            reset_default_audit_writer()

    def test_invalid_retention_env_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_default_audit_writer()
        try:
            monkeypatch.setenv(LOG_PATH_ENV_VAR, str(tmp_path / "a.log"))
            monkeypatch.setenv(RETENTION_ENV_VAR, "not-a-number")
            monkeypatch.setenv("HOME", str(tmp_path))
            w = default_audit_writer()
            assert w.retention_days == DEFAULT_RETENTION_DAYS
        finally:
            reset_default_audit_writer()

    def test_reset_closes_file(self, tmp_path: Path) -> None:
        reset_default_audit_writer()
        try:
            w = default_audit_writer()
            # Force the file to open.
            w.record(
                action="exec",
                node="laptop",
                request_id="r1",
                duration_ms=1,
                status=STATUS_OK,
            )
            assert w._file is not None  # type: ignore[attr-defined]
            reset_default_audit_writer()
            assert w._file is None  # type: ignore[attr-defined]
        finally:
            reset_default_audit_writer()


# ---------------------------------------------------------------------------
# AuditConfig dataclass
# ---------------------------------------------------------------------------


class TestAuditConfig:
    def test_rejects_zero_retention(self) -> None:
        with pytest.raises(ValueError, match="retention_days must be > 0"):
            AuditConfig(path=Path("/tmp/x.log"), retention_days=0)

    def test_accepts_valid_values(self) -> None:
        cfg = AuditConfig(
            path=Path("/tmp/x.log"), retention_days=42
        )
        assert cfg.retention_days == 42
        assert cfg.path == Path("/tmp/x.log")


# ---------------------------------------------------------------------------
# Integration with NodeEnvironment — every exit path writes one row
# ---------------------------------------------------------------------------


class TestEnvironmentAuditIntegration:
    """End-to-end: drive :class:`NodeEnvironment` and assert the audit log.

    Each test passes a fresh :class:`AuditWriter` rooted at a
    ``tmp_path`` file so the writer does not touch the real
    ``~/.hermes/logs`` directory. This is the same pattern the
    test_environment suite uses for the registry override.
    """

    def _make_env(
        self, audit_path: Path, *, target: str = "laptop"
    ):
        from hermes_nodes_plugin.environment import NodeEnvironment
        from hermes_nodes_plugin.registry import NodeRegistry

        writer = AuditWriter(path=audit_path, max_bytes=10_000, keep=2)
        env = NodeEnvironment(
            target, registry=NodeRegistry(), audit=writer
        )
        return env, writer

    def test_execute_not_connected_writes_audit_row(
        self, audit_path: Path
    ) -> None:
        env, writer = self._make_env(audit_path)
        try:
            from hermes_nodes_plugin.errors import NodeNotConnectedError

            with pytest.raises(NodeNotConnectedError):
                asyncio.run(env.execute("echo hi"))
        finally:
            writer.close()

        rows = _read_jsonl(audit_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "exec"
        assert row["node"] == "laptop"
        assert row["status"] == STATUS_NOT_CONNECTED
        assert row["exit_code"] == -1
        assert "request_id" in row and row["request_id"]
        assert row["duration_ms"] >= 0
        assert "not connected" in row["error"].lower()

    def test_read_not_connected_writes_audit_row(
        self, audit_path: Path
    ) -> None:
        env, writer = self._make_env(audit_path)
        try:
            from hermes_nodes_plugin.errors import NodeNotConnectedError

            with pytest.raises(NodeNotConnectedError):
                asyncio.run(env.read("/tmp/foo"))
        finally:
            writer.close()

        rows = _read_jsonl(audit_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "read"
        assert row["node"] == "laptop"
        assert row["status"] == STATUS_NOT_CONNECTED

    def test_write_not_connected_writes_audit_row(
        self, audit_path: Path
    ) -> None:
        env, writer = self._make_env(audit_path)
        try:
            from hermes_nodes_plugin.errors import NodeNotConnectedError

            with pytest.raises(NodeNotConnectedError):
                asyncio.run(env.write("/tmp/foo", "hello"))
        finally:
            writer.close()

        rows = _read_jsonl(audit_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "write"
        assert row["node"] == "laptop"
        assert row["status"] == STATUS_NOT_CONNECTED

    def test_execute_with_real_node_writes_ok_audit_row(
        self, audit_path: Path
    ) -> None:
        """End-to-end happy path: real uvicorn + fake node + audit row.

        Reuses the same fake-node pattern as the existing
        :mod:`tests.test_environment` integration tests — the
        server runs in a daemon thread with its own event
        loop, and the test body drives the env + fake node in
        a single ``asyncio.run`` on the main thread. The audit
        log goes to ``audit_path`` and the env uses the same
        registry the server does, so the fake node's
        ``exec_result`` is delivered to the env's waiter and
        an ``ok`` row is written.
        """
        import json
        import socket
        import threading
        from contextlib import contextmanager
        from typing import Any, Iterator

        import uvicorn
        from cryptography.fernet import Fernet
        from websockets.asyncio.client import connect

        from hermes_nodes_plugin.registry import NodeRegistry
        from hermes_nodes_plugin.server import create_app
        from hermes_nodes_plugin.tokens import TokenStore

        registry = NodeRegistry()
        fernet_key = Fernet.generate_key().decode("ascii")
        store = TokenStore(
            path=audit_path.parent / "tokens.json", key=fernet_key
        )
        token = store.create("laptop")

        config = NodeServerConfig()
        app = create_app(token_store=store, registry=registry, config=config)

        def _free_port() -> int:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]

        @contextmanager
        def _running(app: Any) -> Iterator[uvicorn.Server]:
            uconfig = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=_free_port(),
                log_level="warning",
                lifespan="off",
            )
            server = uvicorn.Server(uconfig)
            thread = threading.Thread(
                target=server.run, name="test-uvicorn", daemon=True
            )
            thread.start()
            for _ in range(200):
                if server.started:
                    break
                time.sleep(0.025)
            else:  # pragma: no cover
                server.should_exit = True
                thread.join(timeout=2.0)
                raise RuntimeError("uvicorn test server failed to bind")
            try:
                yield server
            finally:
                server.should_exit = True
                thread.join(timeout=5.0)

        with _running(app) as server:
            # ``server.config`` is private but populated after
            # ``Server.run`` binds. We need the actual bound
            # port, not the value we passed in. ``server.servers``
            # holds the active asyncio server list post-bind.
            # Falling back to ``config.port`` is fine for the
            # local-loopback test setup.
            bound_port = server.config.port

            from hermes_nodes_plugin.environment import NodeEnvironment

            writer = AuditWriter(path=audit_path, max_bytes=10_000, keep=2)
            try:
                env = NodeEnvironment(
                    "laptop", registry=registry, audit=writer
                )

                async def _flow() -> dict:
                    pair_done = asyncio.Event()

                    async def _pair_then_exec() -> None:
                        ws = await connect(f"ws://127.0.0.1:{bound_port}/ws/nodes")
                        try:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "hello",
                                        "protocol_version": "0.1.0",
                                        "node_name": "laptop",
                                        "node_version": "0.1.0",
                                        "platform": "linux",
                                        "arch": "x86_64",
                                        "capabilities": [
                                            "exec",
                                            "read",
                                            "write",
                                        ],
                                    }
                                )
                            )
                            await ws.recv()  # hello_ack
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "auth",
                                        "node_name": "laptop",
                                        "token": token,
                                    }
                                )
                            )
                            await ws.recv()  # auth_ok
                            pair_done.set()
                            raw = await ws.recv()
                            msg = json.loads(raw)
                            if msg.get("type") == "exec":
                                await ws.send(
                                    json.dumps(
                                        {
                                            "type": "exec_result",
                                            "id": msg["id"],
                                            "status": "ok",
                                            "exit_code": 0,
                                            "stdout": "hello\n",
                                            "stderr": "",
                                            "truncated": False,
                                        }
                                    )
                                )
                        finally:
                            await ws.close()

                    reader = asyncio.create_task(_pair_then_exec())
                    await pair_done.wait()
                    result = await env.execute("echo hello")
                    await reader
                    return result

                result = asyncio.run(_flow())
            finally:
                writer.close()

        assert result == {"output": "hello\n", "returncode": 0}

        rows = _read_jsonl(audit_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "exec"
        assert row["node"] == "laptop"
        assert row["status"] == STATUS_OK
        assert row["exit_code"] == 0
        assert "request_id" in row and row["request_id"]


# ---------------------------------------------------------------------------
# Config — the audit_log_path / audit_retention_days keys
# ---------------------------------------------------------------------------


class TestConfigAuditFields:
    def test_defaults(self, empty_env: dict[str, str]) -> None:
        cfg = load_config(env=empty_env)
        assert cfg.audit_log_path == "~/.hermes/logs/nodes-audit.log"
        assert cfg.audit_retention_days == 365

    def test_env_overrides_defaults(
        self, empty_env: dict[str, str]
    ) -> None:
        empty_env["HERMES_NODES_AUDIT_LOG_PATH"] = "/var/log/audit.log"
        empty_env["HERMES_NODES_AUDIT_RETENTION_DAYS"] = "30"
        cfg = load_config(env=empty_env)
        assert cfg.audit_log_path == "/var/log/audit.log"
        assert cfg.audit_retention_days == 30

    def test_file_overrides_defaults(
        self, tmp_path: Path, empty_env: dict[str, str]
    ) -> None:
        cfg_file = tmp_path / "hermes-nodes.yaml"
        cfg_file.write_text(
            "audit_log_path: /var/log/from-file.log\n"
            "audit_retention_days: 90\n",
            encoding="utf-8",
        )
        cfg = load_config(env=empty_env, config_path=cfg_file)
        assert cfg.audit_log_path == "/var/log/from-file.log"
        assert cfg.audit_retention_days == 90

    def test_env_beats_file(
        self, tmp_path: Path, empty_env: dict[str, str]
    ) -> None:
        cfg_file = tmp_path / "hermes-nodes.yaml"
        cfg_file.write_text(
            "audit_log_path: /var/log/from-file.log\n"
            "audit_retention_days: 90\n",
            encoding="utf-8",
        )
        empty_env["HERMES_NODES_AUDIT_LOG_PATH"] = "/var/log/from-env.log"
        empty_env["HERMES_NODES_AUDIT_RETENTION_DAYS"] = "7"
        cfg = load_config(env=empty_env, config_path=cfg_file)
        assert cfg.audit_log_path == "/var/log/from-env.log"
        assert cfg.audit_retention_days == 7

    def test_invalid_retention_raises(
        self, empty_env: dict[str, str]
    ) -> None:
        from hermes_nodes_plugin.errors import ConfigError

        empty_env["HERMES_NODES_AUDIT_RETENTION_DAYS"] = "not-a-number"
        with pytest.raises(ConfigError, match="must be an integer"):
            load_config(env=empty_env)

    def test_zero_retention_raises(
        self, empty_env: dict[str, str]
    ) -> None:
        from hermes_nodes_plugin.errors import ConfigError

        empty_env["HERMES_NODES_AUDIT_RETENTION_DAYS"] = "0"
        with pytest.raises(ConfigError, match="must be > 0"):
            load_config(env=empty_env)

    def test_dataclass_rejects_zero_retention(self) -> None:
        """Belt-and-braces: the dataclass ``__post_init__`` also catches it."""
        from hermes_nodes_plugin.errors import ConfigError

        with pytest.raises(ConfigError, match="audit_retention_days must be > 0"):
            NodeServerConfig(audit_retention_days=0)
