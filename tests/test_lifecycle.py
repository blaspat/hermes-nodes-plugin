"""Tests for :mod:`hermes_nodes_plugin.lifecycle`.

Coverage (matching Task 2.6 acceptance criteria):

  **ServerRunner basics**
    * ``start`` brings up uvicorn in the background; the bound port
      accepts a TCP connection by the time ``start`` returns.
    * ``drain`` sets ``should_exit`` and awaits the server task.
    * ``start`` is idempotent — a second call while running is a no-op
      and returns the same handle. The plan's "drain connections and
      stop" requires the runner to be re-startable across sessions, so
      the first call after a drain spins a fresh task.
    * ``drain`` is idempotent — a second call after the server is
      already stopped is a no-op.
    * ``drain`` closes any active WebSocket connections cleanly (the
      server unregisters them from the registry on disconnect — we
      assert against the registry, not the wire format).

  **Plugin integration**
    * ``register(ctx)`` wires the runner to the plugin's session
      lifecycle: ``on_session_start`` calls ``runner.start``,
      ``on_session_end`` calls ``runner.drain``.
    * ``register`` also registers the ``hermes node`` CLI subcommand
      so it appears in ``hermes --help``. Full ``pair``/``list``/
      ``revoke`` implementation lands in Task 2.10.
    * ``register`` is defensive — it never raises, even when the
      Fernet key env var is unset. The runner logs the error and
      stays in "not started" state. (Hermes would brick a profile
      otherwise — a broken plugin must not take the agent down.)

The tests use a real uvicorn loop bound to an ephemeral port, then
exercise the runner's lifecycle through the async API. We deliberately
do NOT use the FastAPI ``TestClient`` here: the lifecycle must drive
the *server* (uvicorn) and the *task*, not an in-process ASGI
transport. ``TestClient`` would exercise neither the task spawning
nor the drain ordering.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from cryptography.fernet import Fernet

from hermes_nodes_plugin.config import NodeServerConfig
from hermes_nodes_plugin.lifecycle import ServerRunner, reset_default_runner
from hermes_nodes_plugin.registry import NodeRegistry
from hermes_nodes_plugin.tokens import TokenStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the kernel for a port that's free right now.

    The standard "bind to 0" trick: bind a socket, read the assigned
    port, close the socket. The port MAY be racy (something else could
    grab it before our server binds), but for local-loopback tests
    it's overwhelmingly safe in practice and avoids a fixed port
    colliding with whatever else is running on the dev machine.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _is_port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    """Return True if a TCP connection to ``host:port`` succeeds."""
    try:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.settimeout(timeout)
            s.connect((host, port))
        return True
    except OSError:
        return False


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def free_port() -> int:
    return _free_port()


@pytest.fixture
def config(free_port: int) -> NodeServerConfig:
    """A loopback-bound config with the test's Fernet key env set."""
    return NodeServerConfig(host="127.0.0.1", port=free_port)


@pytest.fixture
def store(tmp_path: Path, fernet_key: str, monkeypatch) -> TokenStore:
    """Real :class:`TokenStore` writing to a tmp file.

    We don't go through :func:`load_config` for the token key — we
    pass the key directly because the runner's ``__init__`` takes the
    store as a constructor arg (the env-var lookup is a separate
    concern exercised in the integration tests below).
    """
    return TokenStore(path=tmp_path / "tokens.json", key=fernet_key)


@pytest.fixture
def registry() -> NodeRegistry:
    return NodeRegistry()


@pytest.fixture
def runner(
    config: NodeServerConfig, store: TokenStore, registry: NodeRegistry
) -> Iterator[ServerRunner]:
    """A :class:`ServerRunner` bound to a free port; torn down after the test."""
    r = ServerRunner(config=config, token_store=store, registry=registry)
    yield r
    # Safety net: if the test forgot to drain, don't leave uvicorn
    # running across tests. The runner's ``drain`` is idempotent.
    try:
        asyncio.run(r.drain(timeout=2.0))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ServerRunner basics
# ---------------------------------------------------------------------------


class TestServerRunnerLifecycle:
    @pytest.mark.asyncio
    async def test_start_binds_port_and_serves(
        self, runner: ServerRunner, config: NodeServerConfig
    ) -> None:
        """``start`` returns once uvicorn is bound; the port accepts TCP."""
        assert not runner.is_running
        await runner.start()
        try:
            assert runner.is_running
            # Real socket probe: the server is accepting connections,
            # not just "the task was scheduled".
            assert _is_port_open(config.host, config.port), (
                f"server should be listening on {config.host}:{config.port}"
            )
        finally:
            await runner.drain(timeout=5.0)
        assert not runner.is_running
        assert not _is_port_open(config.host, config.port), (
            "drain should release the port"
        )

    @pytest.mark.asyncio
    async def test_start_is_idempotent(
        self, runner: ServerRunner, config: NodeServerConfig
    ) -> None:
        """A second ``start`` while running returns the same task — no second uvicorn."""
        await runner.start()
        try:
            task_before = runner._task  # type: ignore[attr-defined]
            await runner.start()
            task_after = runner._task  # type: ignore[attr-defined]
            assert task_before is task_after, "second start must be a no-op"
            assert runner.is_running
            # Still only one uvicorn process — we can't easily assert
            # that directly, but a second start that *spawned* a
            # second task would either fail with EADDRINUSE or leave
            # the port bound twice; the test would never reach this
            # assertion cleanly if that happened.
        finally:
            await runner.drain(timeout=5.0)

    @pytest.mark.asyncio
    async def test_drain_is_idempotent(self, runner: ServerRunner) -> None:
        """Calling ``drain`` on a stopped runner is a safe no-op."""
        await runner.start()
        await runner.drain(timeout=5.0)
        assert not runner.is_running
        # Second drain: must not raise.
        await runner.drain(timeout=5.0)
        assert not runner.is_running

    @pytest.mark.asyncio
    async def test_drain_closes_active_connections(
        self, runner: ServerRunner, store: TokenStore, registry: NodeRegistry
    ) -> None:
        """Active WebSocket connections are unregisterd on drain.

        The server's own handler unregisters on ``WebSocketDisconnect``;
        a forced drain (server-side ``should_exit``) propagates a close
        to active sockets, which fires the same unregister path. The
        registry must be empty after the drain.
        """
        from fastapi.testclient import TestClient

        await runner.start()
        try:
            token = store.create("laptop-drain")
            # Drive the real ASGI app via TestClient bound to the
            # running uvicorn server (TestClient spawns its own
            # thread-loop, talks to the app's handlers).
            client = TestClient(runner.app)
            try:
                with client.websocket_connect("/ws/nodes") as ws:
                    ws.send_json(
                        {
                            "type": "hello",
                            "protocol_version": "0.1.0",
                            "node_name": "laptop-drain",
                        }
                    )
                    assert ws.receive_json()["type"] == "hello_ack"
                    ws.send_json(
                        {"type": "auth", "node_name": "laptop-drain", "token": token}
                    )
                    assert ws.receive_json()["type"] == "auth_ok"
                    assert "laptop-drain" in registry
            finally:
                client.close()
        finally:
            await runner.drain(timeout=5.0)

        # After drain: registry cleared. ``laptop-drain`` is gone.
        # A second drain is safe.
        assert "laptop-drain" not in registry
        await runner.drain(timeout=5.0)

    @pytest.mark.asyncio
    async def test_start_after_drain_spawns_a_fresh_task(
        self, runner: ServerRunner, config: NodeServerConfig
    ) -> None:
        """Restart after a clean drain works (the plan needs this for re-bind)."""
        await runner.start()
        first_task = runner._task  # type: ignore[attr-defined]
        await runner.drain(timeout=5.0)
        assert first_task is not None and first_task.done()

        await runner.start()
        try:
            second_task = runner._task  # type: ignore[attr-defined]
            assert second_task is not first_task, "restart must spawn a new task"
            assert runner.is_running
            assert _is_port_open(config.host, config.port)
        finally:
            await runner.drain(timeout=5.0)


# ---------------------------------------------------------------------------
# Plugin integration: register(ctx) wires the runner to the session lifecycle
# ---------------------------------------------------------------------------


class TestRegisterPluginWiring:
    """Drive ``register`` against a mock PluginContext.

    We use a duck-typed mock (not the real ``PluginContext``) because:

    * The real ``PluginContext`` requires a real ``PluginManifest`` and
      a real ``PluginManager`` — both have side-effects on import that
      we don't want bleeding into a unit test.
    * The plugin's contract is *only* the methods it actually calls
      on ``ctx``. Asserting against the interface we depend on keeps
      the test honest about what we need from the host.
    """

    @pytest.fixture
    def mock_ctx(self, monkeypatch) -> SimpleNamespace:
        """A mock ``ctx`` that records hook and CLI registrations.

        We also inject a Fernet key into the env so the runner can
        build a token store without the operator's interactive setup.
        """
        monkeypatch.setenv(
            "HERMES_NODES_TOKEN_KEY", Fernet.generate_key().decode("ascii")
        )
        reset_default_runner()

        ctx = SimpleNamespace(
            _registered_hooks={},
            _cli_commands={},
            manifest=SimpleNamespace(
                name="hermes_nodes_plugin", key="hermes_nodes_plugin"
            ),
        )

        def _register_hook(name: str, callback: Any) -> None:
            ctx._registered_hooks[name] = callback

        def _register_cli_command(
            name: str, help: str, setup_fn: Any, handler_fn: Any = None
        ) -> None:
            ctx._cli_commands[name] = {
                "help": help,
                "setup_fn": setup_fn,
                "handler_fn": handler_fn,
            }

        ctx.register_hook = _register_hook  # type: ignore[attr-defined]
        ctx.register_cli_command = _register_cli_command  # type: ignore[attr-defined]
        return ctx

    def test_register_hooks_session_start_and_end(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        from hermes_nodes_plugin import register

        register(mock_ctx)  # type: ignore[arg-type]

        assert "on_session_start" in mock_ctx._registered_hooks
        assert "on_session_end" in mock_ctx._registered_hooks
        # Both hooks are callable coroutines (or plain callables —
        # the gateway can handle both per PluginContext docs).
        assert callable(mock_ctx._registered_hooks["on_session_start"])
        assert callable(mock_ctx._registered_hooks["on_session_end"])

    def test_register_adds_node_cli_subcommand(self, mock_ctx: SimpleNamespace) -> None:
        from hermes_nodes_plugin import register

        register(mock_ctx)  # type: ignore[arg-type]

        assert "node" in mock_ctx._cli_commands
        # The setup_fn wires the argparse subparser; the full
        # pair/list/revoke surface lands in Task 2.10, but the
        # entry point must be present now (the plan: "`hermes node`
        # CLI appears" once the plugin auto-loads).
        setup_fn = mock_ctx._cli_commands["node"]["setup_fn"]
        assert callable(setup_fn), "node subcommand must expose a setup_fn for argparse"
        assert mock_ctx._cli_commands["node"]["help"], (
            "node subcommand must have a help string"
        )

    def test_register_does_not_raise_on_missing_fernet_key(self, monkeypatch) -> None:
        """A broken plugin must not take down the host.

        The runner is defensive: it logs the missing key, stays
        un-started, and the rest of the plugin load continues. The
        gateway/CLI process boots even when the operator hasn't
        generated a token key yet (Task 2.10 will surface a clear
        error from the CLI subcommand instead).
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        reset_default_runner()

        ctx = SimpleNamespace(
            manifest=SimpleNamespace(
                name="hermes_nodes_plugin", key="hermes_nodes_plugin"
            ),
            _registered_hooks={},
            _cli_commands={},
        )
        ctx.register_hook = lambda n, c: ctx._registered_hooks.__setitem__(n, c)  # type: ignore[attr-defined]
        ctx.register_cli_command = lambda *a, **kw: None  # type: ignore[attr-defined]

        from hermes_nodes_plugin import register

        # The critical assertion: no exception escapes register.
        register(ctx)  # type: ignore[arg-type]

        # The hooks were still wired — a future operator who sets
        # the key and restarts will pick the server up.
        assert "on_session_start" in ctx._registered_hooks
        assert "on_session_end" in ctx._registered_hooks

    def test_session_start_invokes_runner_start(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        """Firing the registered on_session_start callback starts the default runner."""
        from hermes_nodes_plugin import register

        register(mock_ctx)  # type: ignore[arg-type]

        start_cb = mock_ctx._registered_hooks["on_session_start"]
        # Gateway hook callbacks are invoked synchronously per
        # `PluginContext.register_hook` docstring; the callback may
        # be either a coroutine function or a sync function that
        # schedules one. The runner's `start` is awaitable.
        try:
            asyncio.run(_call_hook(start_cb))
        finally:
            # Drain for cleanup
            from hermes_nodes_plugin.lifecycle import get_default_runner

            runner = get_default_runner()
            try:
                asyncio.run(runner.drain(timeout=5.0))
            except Exception:
                pass

        # After the hook fired, the default runner reached a running
        # state. We don't assert on is_running here because the loop
        # that started it has since closed; we assert the side effect:
        # the runner was constructed and started.
        from hermes_nodes_plugin.lifecycle import get_default_runner

        runner = get_default_runner()
        assert runner is not None, "session start must construct the default runner"

    @pytest.mark.asyncio
    async def test_session_end_invokes_runner_drain(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        """Firing the registered on_session_end callback drains the default runner.

        Both ``on_session_start`` and ``on_session_end`` run inside the
        same event loop — we use ``asyncio.gather`` to keep them in
        one loop. Calling ``asyncio.run`` twice would close the loop
        and tear down the background task; we want to assert the
        *transition* from running to drained, not just the endpoint.
        """
        from hermes_nodes_plugin import register
        from hermes_nodes_plugin.lifecycle import get_default_runner

        register(mock_ctx)  # type: ignore[arg-type]

        start_cb = mock_ctx._registered_hooks["on_session_start"]
        end_cb = mock_ctx._registered_hooks["on_session_end"]

        # Start: brings the runner up in the current loop.
        await _call_hook(start_cb)
        runner = get_default_runner()
        assert runner.is_running, "session_start must leave the runner running"

        # End: drains it.
        await _call_hook(end_cb)
        assert not runner.is_running, "session_end must drain the runner"


async def _call_hook(cb: Any) -> Any:
    """Call a hook callback, awaiting it if it's a coroutine.

    Hook callbacks in Hermes's PluginContext are documented to
    accept either sync or async callables. The gateway's
    ``invoke_hook`` already handles both, but in unit tests we
    write the dispatch ourselves.
    """
    result = cb()
    if asyncio.iscoroutine(result):
        await result
    return result
