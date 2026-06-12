"""Tests for :mod:`hermes_nodes_plugin.cli` (Task 2.10).

Coverage areas (matching REQUIREMENTS FR-1 + the plan §Task 2.10):

* Argparse wiring — every subcommand appears with the right
  arguments; missing args produce argparse-level errors, not
  crashes.
* End-to-end pair → list → revoke: tokens persist, list shows
  the right state transitions, revoke persists the revoked flag.
* Edge cases — duplicate name (FR-1.5), ``--force`` re-pair,
  revoke of unknown name is a no-op (idempotent), missing Fernet
  key surfaces the operator-friendly error (FR-4.2), JSON output
  is machine-parseable.
* State derivation (``_format_row``) — connected / disconnected /
  never_seen / revoked. Pulled out as a free function for
  hermetic testing, since the live-connection branch needs a
  runner that we don't want to spin up here.

The CLI module is sync; we exercise it by calling
:func:`node_command` directly with a synthetic
:class:`argparse.Namespace`. Config and env are injected via
``monkeypatch`` so tests don't touch the real ``~/.hermes/`` or
process environment.

What is NOT tested here (covered elsewhere):

* The "revoke drops the live connection" WebSocket behaviour
  needs a uvicorn server with an active WebSocket; that's an
  end-to-end test that lands in ``tests/e2e/test_full_flow.py``
  per the plan (Phase 3). The CLI's best-effort close helper
  is exercised in isolation below.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from hermes_nodes_plugin import cli as cli_mod
from hermes_nodes_plugin.cli import (
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_NEVER_SEEN,
    STATE_REVOKED,
    _format_row,
    node_command,
    setup_node_cli,
)
from hermes_nodes_plugin.errors import TokenStoreError
from hermes_nodes_plugin.tokens import TokenRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "tokens.json"


@pytest.fixture
def isolated_env(
    monkeypatch: pytest.MonkeyPatch,
    fernet_key: str,
    store_path: Path,
    tmp_path: Path,
) -> None:
    """Pin the config and Fernet key for the test.

    The CLI's :func:`load_config` reads ``~/.hermes/hermes-nodes.yaml``
    unconditionally, so we patch :func:`cli_mod.load_config` to
    return a config built from our temp YAML. That keeps the
    operator's real config and existing token store untouched.
    """
    from hermes_nodes_plugin.config import load_config as _real_load_config
    from hermes_nodes_plugin import cli as cli_module

    monkeypatch.setenv("HERMES_NODES_TOKEN_KEY", fernet_key)

    config_yaml = tmp_path / "hermes-nodes.yaml"
    config_yaml.write_text(
        f"token_store_path: {store_path}\n"
        "host: 127.0.0.1\n"
        "port: 1\n"  # low port, never bound in these tests
    )

    def _fake_load_config(*, env=None, config_path=None):
        return _real_load_config(env=env, config_path=config_yaml)

    monkeypatch.setattr(cli_module, "load_config", _fake_load_config)


def _make_parser() -> argparse.ArgumentParser:
    """Build a top-level parser that mirrors how Hermes wires it.

    Hermes allocates a ``hermes node`` subparser and hands it to
    :func:`setup_node_cli`. We replicate that structure here so
    :func:`_parse` produces a namespace indistinguishable from
    what the real CLI would see.
    """
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="top_action")
    node = sub.add_parser("node")
    setup_node_cli(node)
    return parser


def _parse(argv: list[str]) -> argparse.Namespace:
    return _make_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------


class TestArgparseWiring:
    def test_pair_requires_name(self) -> None:
        with pytest.raises(SystemExit):
            _parse(["node", "pair"])

    def test_revoke_requires_name(self) -> None:
        with pytest.raises(SystemExit):
            _parse(["node", "revoke"])

    def test_list_accepts_json_flag(self) -> None:
        args = _parse(["node", "list", "--json"])
        assert args.node_action == "list"
        assert args.as_json is True

    def test_pair_force_flag(self) -> None:
        args = _parse(["node", "pair", "--name", "x", "--force"])
        assert args.node_action == "pair"
        assert args.force is True

    def test_status_has_no_args(self) -> None:
        args = _parse(["node", "status"])
        assert args.node_action == "status"

    def test_node_with_no_subcommand_returns_2(self) -> None:
        """The fallback dispatch (action=None) returns 2.

        argparse normally exits before we get here, but a caller
        that constructs the namespace by hand would hit the
        fallback. Pin the behaviour so a future refactor doesn't
        silently change it.
        """
        args = argparse.Namespace(node_action=None, func=node_command)
        assert node_command(args) == 2

    def test_unknown_subcommand_returns_2(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Defensive: if a future subcommand slips through argparse,
        dispatch returns 2 and writes to stderr.
        """
        args = argparse.Namespace(node_action="frobnicate", func=node_command)
        assert node_command(args) == 2
        _, err = capsys.readouterr()
        assert "frobnicate" in err


# ---------------------------------------------------------------------------
# End-to-end pair → list → revoke
# ---------------------------------------------------------------------------


class TestPairListRevoke:
    def test_pair_creates_token_and_writes_to_disk(
        self, isolated_env: None, store_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = node_command(_parse(["node", "pair", "--name", "laptop1"]))
        assert rc == 0
        out, err = capsys.readouterr()
        # Token to stdout, prefixed with "token: " so scripts can grep.
        assert out.startswith("token: ")
        token = out.split("token: ", 1)[1].strip().splitlines()[0]
        assert len(token) >= 43
        # Setup hint to stderr (operator-facing, not scriptable).
        assert "hermes-node pair" in err
        assert "laptop1" in err
        assert "--name laptop1" in err
        # File is on disk and non-empty (Fernet-encrypted blob).
        assert store_path.exists()
        assert store_path.read_bytes()

    def test_pair_then_list_shows_never_seen(
        self, isolated_env: None, store_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        node_command(_parse(["node", "pair", "--name", "laptop1"]))
        capsys.readouterr()  # discard pair output

        rc = node_command(_parse(["node", "list"]))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "laptop1" in out
        # Freshly paired, no successful auth yet → never_seen.
        assert STATE_NEVER_SEEN in out

    def test_pair_then_list_json_is_machine_parseable(
        self, isolated_env: None, store_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        node_command(_parse(["node", "pair", "--name", "laptop1"]))
        capsys.readouterr()

        rc = node_command(_parse(["node", "list", "--json"]))
        assert rc == 0
        out, _ = capsys.readouterr()
        rows = [json.loads(line) for line in out.strip().splitlines()]
        assert len(rows) == 1
        assert rows[0]["name"] == "laptop1"
        assert rows[0]["state"] == STATE_NEVER_SEEN
        assert rows[0]["last_used_at"] is None
        assert rows[0]["created_at"]  # non-empty timestamp

    def test_revoke_persists_revoked_flag(
        self, isolated_env: None, store_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        node_command(_parse(["node", "pair", "--name", "laptop1"]))
        capsys.readouterr()
        rc = node_command(_parse(["node", "revoke", "--name", "laptop1"]))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "revoked: laptop1" in out

        # Listing now shows the revoked state.
        node_command(_parse(["node", "list", "--json"]))
        out, _ = capsys.readouterr()
        rows = [json.loads(line) for line in out.strip().splitlines()]
        assert len(rows) == 1
        # The state string is the public encoding of "revoked" —
        # the raw ``revoked`` field is intentionally not in the
        # JSON output (TokenStore's public shape drops it).
        assert rows[0]["state"] == STATE_REVOKED

    def test_revoke_unknown_name_is_idempotent(
        self, isolated_env: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Revoke on a name we never paired is a no-op (exit 0).

        Scripts that retry on transient failures shouldn't blow up
        if the second attempt's revoke is a no-op. This matches
        :meth:`TokenStore.revoke`'s idempotent contract.
        """
        rc = node_command(_parse(["node", "revoke", "--name", "ghost"]))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "revoked: ghost" in out

    def test_empty_list_says_so(
        self, isolated_env: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = node_command(_parse(["node", "list"]))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "no paired nodes" in out


# ---------------------------------------------------------------------------
# Force + duplicate handling (FR-1.5)
# ---------------------------------------------------------------------------


class TestForceAndDuplicates:
    def test_duplicate_name_without_force_errors(
        self, isolated_env: None, store_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        node_command(_parse(["node", "pair", "--name", "laptop1"]))
        capsys.readouterr()

        rc = node_command(_parse(["node", "pair", "--name", "laptop1"]))
        assert rc == 1
        _, err = capsys.readouterr()
        # FR-1.5: the operator message tells them how to recover.
        assert "already paired" in err
        assert "--force" in err

    def test_force_overwrites_existing_record(
        self, isolated_env: None, store_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        node_command(_parse(["node", "pair", "--name", "laptop1"]))
        first = capsys.readouterr().out.split("token: ", 1)[1].strip().splitlines()[0]

        rc = node_command(_parse(["node", "pair", "--name", "laptop1", "--force"]))
        assert rc == 0
        second = capsys.readouterr().out.split("token: ", 1)[1].strip().splitlines()[0]

        assert first != second, "force should issue a cryptographically fresh token"

        # Two records on disk: the revoked old one and the live new
        # one. TokenStore keeps revoked rows for audit history
        # (per tokens.py: "Revoked entries are kept on disk, not
        # deleted") so ``list`` surfaces both. The new record is
        # ``never_seen`` because the freshly-issued token has not
        # been validated yet — that's the same state any brand-new
        # pair shows until the laptop connects.
        node_command(_parse(["node", "list", "--json"]))
        out, _ = capsys.readouterr()
        rows = [json.loads(line) for line in out.strip().splitlines()]
        assert len(rows) == 2
        states = sorted(r["state"] for r in rows)
        assert states == [STATE_NEVER_SEEN, STATE_REVOKED]

    def test_pair_after_revoke_succeeds_without_force(
        self, isolated_env: None, store_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Revoked-on-disk records are eligible for re-pair.

        :class:`TokenStore.create` documents this and the CLI
        shouldn't add a ``--force`` requirement just to recover
        from a revoke. This is a real operator path: revoke a
        node, then immediately pair it again to issue a new
        token.
        """
        node_command(_parse(["node", "pair", "--name", "laptop1"]))
        capsys.readouterr()
        node_command(_parse(["node", "revoke", "--name", "laptop1"]))
        capsys.readouterr()

        rc = node_command(_parse(["node", "pair", "--name", "laptop1"]))
        assert rc == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_pair_empty_name_errors(
        self, isolated_env: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = node_command(_parse(["node", "pair", "--name", "   "]))
        assert rc == 1
        _, err = capsys.readouterr()
        assert "non-empty" in err

    def test_revoke_empty_name_errors(
        self, isolated_env: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = node_command(_parse(["node", "revoke", "--name", ""]))
        assert rc == 1
        _, err = capsys.readouterr()
        assert "non-empty" in err

    def test_missing_fernet_key_auto_generates_and_saves(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A fresh install with no key auto-generates one (FR-4.2).

        Pre-v0.2.0 behaviour was to surface a ``Fernet.generate_key``
        hint to stderr; v0.2.0+ auto-generates the key and writes it
        to ``~/.hermes/.env`` (or the test-injected equivalent). This
        test pins the new behaviour: no manual
        ``export HERMES_NODES_TOKEN_KEY=***`` step, the
        pair just works.

        We pin the helper to write to a ``tmp_path`` so the
        operator's real ``.env`` is never touched, and we inject
        a deterministic ``generate`` so the test doesn't depend on
        randomness.
        """
        from hermes_nodes_plugin import cli as cli_module
        from hermes_nodes_plugin.config import load_config as _real_load_config
        from hermes_nodes_plugin import env as env_mod

        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        config_yaml = tmp_path / "hermes-nodes.yaml"
        config_yaml.write_text(
            f"port: 1\ntoken_store_path: {tmp_path / 'tokens.json'}\n"
        )

        def _fake_load_config(*, env=None, config_path=None):
            return _real_load_config(env=env, config_path=config_yaml)

        monkeypatch.setattr(cli_module, "load_config", _fake_load_config)

        # Pin the env helper to a tmp_path .env + deterministic
        # key. The pair command calls
        # ``ensure_fernet_key_in_env(var_name=...)`` — we
        # monkeypatch the module-level symbol so the call
        # delegates to our controlled version.
        fake_env_path = tmp_path / "fake_hermes.env"
        injected_key = "f" * 43 + "="  # Fernet-shaped, 32 bytes b64-encoded

        def _fake_ensure(var_name, env_path=None, *, generate=None):
            return env_mod.ensure_fernet_key_in_env(
                var_name=var_name,
                env_path=fake_env_path,
                generate=lambda: injected_key,
            )

        monkeypatch.setattr(cli_module, "ensure_fernet_key_in_env", _fake_ensure)

        rc = node_command(_parse(["node", "pair", "--name", "laptop1"]))
        assert rc == 0, "auto-token should let pair succeed with no manual setup"
        out, err = capsys.readouterr()
        # The auto-generated key made it to stdout as the token.
        assert "token:" in out
        # A confirmation line tells the operator where the key landed.
        assert "generated Fernet key" in err
        assert str(fake_env_path) in err
        # The key actually got persisted to the (test) env file.
        assert injected_key in fake_env_path.read_text()
        # And the var name is in the file in KEY=VALUE form.
        assert "HERMES_NODES_TOKEN_KEY=" in fake_env_path.read_text()


# ---------------------------------------------------------------------------
# State derivation (the small, hermetic core of `list`)
# ---------------------------------------------------------------------------


class TestFormatRow:
    def test_revoked_overrides_everything(self) -> None:
        """Revoked wins over "connected" — the store is the source
        of truth for "this token is dead", not the registry.
        """
        rec = TokenRecord(
            name="x",
            created_at="2026-06-04T00:00:00Z",
            revoked=True,
            last_used_at="2026-06-04T00:01:00Z",
        )
        row = _format_row(rec, is_connected=True)
        assert row["state"] == STATE_REVOKED

    def test_connected(self) -> None:
        rec = TokenRecord(
            name="x", created_at="2026-06-04T00:00:00Z",
            revoked=False, last_used_at="2026-06-04T00:01:00Z",
        )
        row = _format_row(rec, is_connected=True)
        assert row["state"] == STATE_CONNECTED

    def test_never_seen(self) -> None:
        rec = TokenRecord(
            name="x", created_at="2026-06-04T00:00:00Z",
            revoked=False, last_used_at=None,
        )
        row = _format_row(rec, is_connected=False)
        assert row["state"] == STATE_NEVER_SEEN

    def test_disconnected(self) -> None:
        rec = TokenRecord(
            name="x", created_at="2026-06-04T00:00:00Z",
            revoked=False, last_used_at="2026-06-04T00:01:00Z",
        )
        row = _format_row(rec, is_connected=False)
        assert row["state"] == STATE_DISCONNECTED

    def test_row_keeps_raw_fields(self) -> None:
        """Downstream tooling may want the raw timestamps; the row
        passes them through unchanged.
        """
        rec = TokenRecord(
            name="x", created_at="2026-06-04T00:00:00Z",
            revoked=False, last_used_at="2026-06-04T00:01:00Z",
        )
        row = _format_row(rec, is_connected=True)
        assert row["name"] == "x"
        assert row["created_at"] == "2026-06-04T00:00:00Z"
        assert row["last_used_at"] == "2026-06-04T00:01:00Z"


# ---------------------------------------------------------------------------
# The best-effort connection-close helper
# ---------------------------------------------------------------------------


class TestCloseActiveConnection:
    def test_silent_when_runner_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If the runner can't be resolved (no Fernet key, no
        config, fresh install), the close helper must not raise
        and must not write to stderr. The store-level revoke is
        the source of truth; the close is a courtesy.
        """
        import hermes_nodes_plugin.lifecycle as lc

        def _explode():
            raise TokenStoreError("no runner")

        monkeypatch.setattr(lc, "get_default_runner", _explode)
        cli_mod._close_active_connection("anything")
        _, err = capsys.readouterr()
        assert err == ""


# ---------------------------------------------------------------------------
# The strict connection-close helper (--strict mode in `hermes node revoke`)
# ---------------------------------------------------------------------------


class TestCloseActiveConnectionStrict:
    """Cover the four result codes the strict helper can return.

    The strict helper must distinguish:

    * ``closed`` — clean ACK within the timeout
    * ``no_connection`` — nothing to wait for (exit 0 in caller)
    * ``timed_out`` — close did not ACK in time (exit 1 in caller)
    * ``error`` — runner/registry/loop is unavailable (exit 1 in caller)

    These are unit-level: we mock the registry's ``get`` and the
    websocket's ``close()`` coroutine so the helper sees a synthetic
    outcome without standing up a real uvicorn.
    """

    def test_returns_closed_on_clean_ack(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_nodes_plugin.lifecycle as lc

        class _FakeConn:
            class websocket:
                @staticmethod
                async def close():
                    return None

        class _FakeRegistry:
            @staticmethod
            async def get(name: str):
                return _FakeConn()

        class _FakeRunner:
            _registry = _FakeRegistry()

        monkeypatch.setattr(lc, "get_default_runner", lambda: _FakeRunner())
        result = cli_mod._close_active_connection_strict("laptop1", timeout=1.0)
        assert result == cli_mod.CLOSE_RESULT_CLOSED

    def test_returns_no_connection_when_registry_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_nodes_plugin.lifecycle as lc

        class _FakeRegistry:
            @staticmethod
            async def get(name: str):
                return None

        class _FakeRunner:
            _registry = _FakeRegistry()

        monkeypatch.setattr(lc, "get_default_runner", lambda: _FakeRunner())
        result = cli_mod._close_active_connection_strict("ghost", timeout=1.0)
        assert result == cli_mod.CLOSE_RESULT_NO_CONNECTION

    def test_returns_timed_out_when_close_hangs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_nodes_plugin.lifecycle as lc

        class _FakeConn:
            class websocket:
                @staticmethod
                async def close():
                    await asyncio.sleep(60)  # never returns

        class _FakeRegistry:
            @staticmethod
            async def get(name: str):
                return _FakeConn()

        class _FakeRunner:
            _registry = _FakeRegistry()

        monkeypatch.setattr(lc, "get_default_runner", lambda: _FakeRunner())
        # Tiny timeout so the test runs fast. 0.05s is below the
        # helper's asyncio.wait_for window but well above the
        # scheduler granularity.
        result = cli_mod._close_active_connection_strict("laptop1", timeout=0.05)
        assert result == cli_mod.CLOSE_RESULT_TIMED_OUT

    def test_returns_error_when_runner_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_nodes_plugin.lifecycle as lc

        def _explode():
            raise TokenStoreError("no runner")

        monkeypatch.setattr(lc, "get_default_runner", _explode)
        result = cli_mod._close_active_connection_strict("laptop1", timeout=1.0)
        assert result == cli_mod.CLOSE_RESULT_ERROR

    def test_returns_error_when_runner_has_no_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hermes_nodes_plugin.lifecycle as lc

        class _FakeRunnerNoRegistry:
            pass  # no _registry attribute

        monkeypatch.setattr(
            lc, "get_default_runner", lambda: _FakeRunnerNoRegistry()
        )
        result = cli_mod._close_active_connection_strict("laptop1", timeout=1.0)
        assert result == cli_mod.CLOSE_RESULT_ERROR


# ---------------------------------------------------------------------------
# status (kept from the 2.6 stub)
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_reports_listening_when_port_bound(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The positive branch of ``hermes node status`` reports
        the bind address when the WSS server is up. We mock
        ``socket.socket.connect`` to succeed without a real server.
        """
        import socket as sock_mod

        class _ListeningSocket:
            def __init__(self, *a, **kw):
                pass
            def settimeout(self, t):
                pass
            def connect(self, addr):
                pass  # Success — server is "listening"
            def close(self):
                pass

        monkeypatch.setattr(sock_mod, "socket", lambda *a, **kw: _ListeningSocket())
        rc = node_command(_parse(["node", "status"]))
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "listening" in out
        assert "127.0.0.1:6969" in out

    def test_status_reports_not_running_when_port_unbound(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        '''Without a bound port (server not started, or running in a
        separate binary) status reports 'not running' and exits 1.
        We mock ``socket.socket.connect`` to raise OSError so the
        test doesn't depend on whether a real server is running.
        '''
        import socket as sock_mod

        class _RefusingSocket:
            def __init__(self, *a, **kw):
                pass
            def settimeout(self, t):
                pass
            def connect(self, addr):
                raise OSError("Connection refused")
            def close(self):
                pass

        monkeypatch.setattr(sock_mod, "socket", lambda *a, **kw: _RefusingSocket())
        rc = node_command(_parse(["node", "status"]))
        assert rc == 1
        out, _ = capsys.readouterr()
        assert "not running" in out
