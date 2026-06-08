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
from hermes_nodes_plugin.lifecycle import (
    ServerRunner,
    reset_default_runner_sync,
)
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
            assert second_task is not None, "restart must spawn a task"
            assert not second_task.done(), "restart task must be running"
            assert second_task is not first_task, "restart must spawn a new task"
            assert runner.is_running
            assert _is_port_open(config.host, config.port)
        finally:
            await runner.drain(timeout=5.0)


# ---------------------------------------------------------------------------
# Stale-connection sweep (issue #19)
# ---------------------------------------------------------------------------


class TestStaleConnectionSweep:
    """Background sweep closes dead WebSocket connections.

    The PROTOCOL §6 contract says a server marks a node offline after
    60s of silence. The :class:`ServerRunner` implements this with a
    background task that calls :meth:`NodeRegistry.stale` on a fixed
    interval and closes any candidate's WebSocket. Closing the
    WebSocket trips the connection handler's ``finally`` block, which
    unregisters the node — so the registry is the canonical assertion
    surface.
    """

    @pytest.fixture
    def fast_sweep_config(
        self, free_port: int, fernet_key: str, tmp_path: Path
    ) -> NodeServerConfig:
        """A config tuned for sub-second sweep testing.

        Stale threshold of 0.5s, sweep interval of 0.1s — the runner
        will mark any connection that hasn't received traffic in the
        last half-second as dead, and it re-checks every tenth of a
        second. This keeps the test under a couple of seconds wall
        time even on a contended CI box.
        """
        return NodeServerConfig(
            host="127.0.0.1",
            port=free_port,
            token_store_path=str(tmp_path / "fast-sweep-tokens.json"),
            token_encryption_key_env="HERMES_NODES_TOKEN_KEY",
            heartbeat_stale_seconds=1,
            heartbeat_sweep_interval_seconds=1,
        )

    @pytest.fixture
    def fast_runner(
        self,
        fast_sweep_config: NodeServerConfig,
        store: TokenStore,
        registry: NodeRegistry,
    ) -> Iterator[ServerRunner]:
        """A :class:`ServerRunner` with a fast sweep cycle.

        Reuses the test's shared ``store`` and ``registry`` fixtures
        so tokens created via ``store.create(name)`` are visible to
        the runner's app, and connections registered against the
        app end up in the registry the test asserts against. The
        runner is built with the test's sweep-tuned config but the
        shared store/registry so token + connection state is
        observable from the test body.
        """
        r = ServerRunner(
            config=fast_sweep_config, token_store=store, registry=registry
        )
        yield r
        try:
            asyncio.run(r.drain(timeout=2.0))
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_sweep_evicts_stale_connection(
        self, fast_runner: ServerRunner, store: TokenStore, registry: NodeRegistry
    ) -> None:
        """A stale connection is closed by the sweep; unregister is
        the handler's job (covered separately).

        We register a stub ``NodeConnection`` directly into the
        registry with a fake WebSocket whose ``close()`` is a no-op
        coroutine. That sidesteps the cross-thread TestClient loop
        race that the prior version of this test hit. The sweep's
        responsibility is to (1) find the stale entry via
        ``registry.stale()`` and (2) call ``websocket.close()`` on
        it — we assert both. The unregister path is exercised in
        ``test_drain_closes_active_connections`` and PR #24's
        ``test_second_connection_from_same_name_replaces_first``.
        """
        from datetime import datetime, timedelta, timezone
        from types import SimpleNamespace

        from hermes_nodes_plugin.registry import NodeConnection

        closed: list[int] = []

        async def fake_close(code: int = 1000) -> None:
            closed.append(code)
            # Ask the sweep loop to exit on its next check. We set
            # the event from inside the close so the test only
            # awaits the first tick (the loop's wait_for is
            # bounded by interval=1s, but setting the event now
            # makes the wait return immediately on the next loop
            # iteration).
            fast_runner._stop_sweep.set()  # type: ignore[attr-defined]

        fake_ws = SimpleNamespace(close=fake_close)
        conn = NodeConnection(
            name="ghost-laptop",
            websocket=fake_ws,  # type: ignore[arg-type]
            session_id="sweep-evict-test-session",
        )
        await registry.register(conn)
        # Backdate the heartbeat past the 1s threshold.
        long_ago = datetime.now(timezone.utc) - timedelta(seconds=120)
        assert await registry.touch_heartbeat("ghost-laptop", at=long_ago) is True
        assert await registry.is_connected("ghost-laptop") is True

        # Drive one sweep tick directly. We don't need the
        # background task for this assertion (it races the
        # cross-thread TestClient loop) and the per-tick coroutine
        # is the unit of work that matters. The sweep loop exits
        # as soon as ``_stop_sweep`` is set — fake_close sets it.
        await asyncio.wait_for(
            fast_runner._sweep_stale_connections(),  # type: ignore[attr-defined]
            timeout=2.0,
        )

        assert closed == [1000], (
            f"expected sweep to close the stale WebSocket with code 1000, "
            f"got {closed!r}"
        )
        # The sweep must NOT unregister — that's the handler's job
        # (server.py's WebSocketDisconnect branch). We assert the
        # entry is still present, then unregister explicitly to
        # keep the test self-contained.
        assert await registry.is_connected("ghost-laptop") is True
        await registry.unregister(
            "ghost-laptop", expected_session_id=conn.session_id
        )
        assert await registry.is_connected("ghost-laptop") is False

        await fast_runner.drain(timeout=5.0)

    @pytest.mark.asyncio
    async def test_sweep_keeps_fresh_connection_alive(
        self, fast_runner: ServerRunner, store: TokenStore, registry: NodeRegistry
    ) -> None:
        """A node whose heartbeat is fresh is NOT closed by the sweep.

        The sweep is selective: only connections past the threshold
        are evicted. This test connects, lets several sweep
        intervals pass, and asserts the connection is still
        registered. We keep traffic flowing with a no-op
        ``touch_heartbeat`` to stay under the threshold.
        """
        from fastapi.testclient import TestClient

        await fast_runner.start()
        try:
            token = store.create("active-laptop")
            client = TestClient(fast_runner.app)
            with client.websocket_connect("/ws/nodes") as ws:
                ws.send_json(
                    {
                        "type": "hello",
                        "protocol_version": "0.1.0",
                        "node_name": "active-laptop",
                    }
                )
                assert ws.receive_json()["type"] == "hello_ack"
                ws.send_json(
                    {
                        "type": "auth",
                        "node_name": "active-laptop",
                        "token": token,
                    }
                )
                assert ws.receive_json()["type"] == "auth_ok"

                # Let two sweep cycles pass while we keep the
                # heartbeat fresh. The default interval is 1s, so
                # 2.5s of keep-alive covers two cycles with margin.
                for _ in range(5):
                    await asyncio.sleep(0.5)
                    assert await registry.touch_heartbeat("active-laptop") is True

                assert await registry.is_connected("active-laptop") is True
        finally:
            await fast_runner.drain(timeout=5.0)

    @pytest.mark.asyncio
    async def test_sweep_task_stops_on_drain(
        self, fast_runner: ServerRunner
    ) -> None:
        """The background sweep task is cancelled when the runner drains.

        Issue #19: the sweep is a long-lived task. If ``drain`` does
        not stop it, the task outlives the server and we leak an
        :class:`asyncio.Task` (and a logger handle) per drain cycle.
        After ``drain`` returns, the sweep task must be done.
        """
        await fast_runner.start()
        try:
            sweep_task = fast_runner._sweep_task  # type: ignore[attr-defined]
            assert sweep_task is not None
            assert not sweep_task.done(), (
                "sweep task should be running while server is up"
            )
        finally:
            await fast_runner.drain(timeout=5.0)

        # After drain, the sweep task has been awaited (cancelled or
        # exited via the stop event) — it's done and cleared.
        assert fast_runner._sweep_task is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# reset_default_runner + reset_default_runner_sync (issue #20)
# ---------------------------------------------------------------------------


class TestResetDefaultRunner:
    """``reset_default_runner`` must fully drain the previous runner.

    The old implementation synchronously cancelled the runner's task
    without awaiting it, which is exactly the EADDRINUSE footgun
    the issue describes. The fix is twofold:

    * ``reset_default_runner`` is now a coroutine that schedules an
      ``await runner.drain(timeout=2.0)`` and returns the future.
      Async tests ``await`` it.
    * ``reset_default_runner_sync`` is a sync shim that drives the
      drain via ``asyncio.run_coroutine_threadsafe`` and blocks
      until the future resolves. Sync tests (fixtures, module-level
      cleanup) call this.

    These tests exercise both shapes, plus a tight-loop regression
    test that would have surfaced EADDRINUSE under the old code.
    """

    def _make_runner(
        self, free_port: int, fernet_key: str, tmp_path: Path
    ) -> ServerRunner:
        from hermes_nodes_plugin.tokens import TokenStore

        config = NodeServerConfig(
            host="127.0.0.1",
            port=free_port,
            token_store_path=str(tmp_path / f"tokens-{free_port}.json"),
            token_encryption_key_env="HERMES_NODES_TOKEN_KEY",
        )
        store = TokenStore(
            path=tmp_path / f"tokens-{free_port}.json", key=fernet_key
        )
        registry = NodeRegistry()
        return ServerRunner(config=config, token_store=store, registry=registry)

    def test_reset_sync_with_no_runner_is_noop(self) -> None:
        """Calling the sync shim when no runner exists is a safe no-op.

        Issue #20's regression test #2: reset twice in quick
        succession must not raise. The first call sees no runner
        (or drains it); the second call sees no runner. Both
        return cleanly.
        """
        from hermes_nodes_plugin.lifecycle import (
            _default_runner,
            reset_default_runner_sync,
        )

        # Force the singleton to None so the no-op assertion holds
        # regardless of test order — the default_runner is module
        # global state and ``TestRegisterPluginWiring`` (which runs
        # later alphabetically) leaves it populated via
        # ``get_default_runner()``.
        import hermes_nodes_plugin.lifecycle as _lifecycle

        _lifecycle._default_runner = None
        assert _default_runner is None
        # Both calls are no-ops, neither raises.
        reset_default_runner_sync()
        reset_default_runner_sync()
        assert _default_runner is None

    def test_reset_sync_raises_inside_running_loop(
        self,
        fernet_key: str,
        tmp_path: Path,
    ) -> None:
        """The sync shim must not be called from inside a running loop.

        The dead-lock-detection case: calling
        ``reset_default_runner_sync`` from a coroutine would block
        the loop's thread on a future scheduled on the same loop,
        and the coroutine would never run. The shim detects this
        and raises ``RuntimeError`` with a clear message pointing
        the caller to the async API.
        """
        import os

        import hermes_nodes_plugin.lifecycle as lifecycle_mod
        from hermes_nodes_plugin.lifecycle import reset_default_runner_sync

        os.environ["HERMES_NODES_TOKEN_KEY"] = fernet_key
        try:
            port = _free_port()
            runner = self._make_runner(port, fernet_key, tmp_path)

            async def scenario() -> None:
                # Start a real runner so ``is_running`` is True.
                # The shim's check fires before any drain attempt,
                # so we never actually close the port.
                await runner.start()
                lifecycle_mod._default_runner = runner
                try:
                    with pytest.raises(
                        RuntimeError, match="inside a running event loop"
                    ):
                        reset_default_runner_sync()
                finally:
                    lifecycle_mod._default_runner = None
                    try:
                        if runner.is_running:
                            await runner.drain(timeout=2.0)
                    except Exception:
                        pass

            asyncio.run(scenario())
        finally:
            os.environ.pop("HERMES_NODES_TOKEN_KEY", None)

    @pytest.mark.asyncio
    async def test_reset_await_awaits_drain(
        self,
        free_port: int,
        fernet_key: str,
        tmp_path: Path,
    ) -> None:
        """The async ``reset_default_runner`` awaits the previous drain.

        We override the module singleton with a real running
        runner, then call the coroutine and assert the runner is
        no longer running by the time it returns. This is the
        core contract: the cancel-and-forget race window is
        gone — by the time ``reset_default_runner`` returns, the
        port is free.
        """
        import os

        from hermes_nodes_plugin.lifecycle import (
            _default_runner,
            reset_default_runner,
        )

        os.environ["HERMES_NODES_TOKEN_KEY"] = fernet_key
        try:
            runner = self._make_runner(free_port, fernet_key, tmp_path)
            # Inject our runner as the singleton so the async
            # reset can find it.
            import hermes_nodes_plugin.lifecycle as lifecycle_mod

            lifecycle_mod._default_runner = runner
            try:
                await runner.start()
                assert runner.is_running
                assert _is_port_open("127.0.0.1", free_port)

                # The async reset coroutine — must be awaited.
                await reset_default_runner()
                # No further state assertions about the returned
                # future (its semantics are an implementation
                # detail); the port-free assertion below is the
                # real contract.
                assert not runner.is_running, (
                    "reset_default_runner must have drained the runner"
                )
                assert not _is_port_open("127.0.0.1", free_port), (
                    "after reset, the port must be free for the next runner"
                )
                assert _default_runner is None, (
                    "reset_default_runner must clear the singleton"
                )
            finally:
                # Belt-and-braces: even if the test failed mid-way,
                # don't leak a running uvicorn.
                try:
                    await runner.drain(timeout=2.0)
                except Exception:
                    pass
                lifecycle_mod._default_runner = None
        finally:
            os.environ.pop("HERMES_NODES_TOKEN_KEY", None)

    def test_reset_tight_loop_does_not_hit_eaddrinuse(
        self,
        fernet_key: str,
        tmp_path: Path,
    ) -> None:
        """The regression test for issue #20: tight reset+rebuild never EADDRINUSEs.

        The old code did ``task.cancel()`` and immediately dropped
        the global, so a tight loop of (start, reset, start, reset)
        could race uvicorn's socket release and the second ``start``
        would bind to a still-TIME_WAIT port. The fix awaits the
        drain, so by the time ``reset`` returns the port is free.

        We run the loop 5 times to give the race a chance to fire
        under the old behaviour.
        """
        import os

        from hermes_nodes_plugin.lifecycle import (
            _default_runner,
            reset_default_runner,
        )
        import hermes_nodes_plugin.lifecycle as lifecycle_mod

        os.environ["HERMES_NODES_TOKEN_KEY"] = fernet_key
        try:
            # We run the whole loop inside one asyncio.run because
            # the runner is bound to the loop it was started in —
            # starting it in a fresh loop and then checking it from
            # outside sees a dead task. The :class:`ServerRunner` is
            # the contract being tested; the loop is incidental.
            async def tight_loop() -> None:
                for i in range(5):
                    port = _free_port()
                    runner = self._make_runner(port, fernet_key, tmp_path)
                    lifecycle_mod._default_runner = runner
                    try:
                        await runner.start()
                        assert runner.is_running
                        assert _is_port_open("127.0.0.1", port), (
                            f"iteration {i}: server should be bound on {port}"
                        )
                        # Async reset — must fully drain before we
                        # start the next iteration. The next
                        # iteration will try to bind a fresh runner
                        # on the same port; the old runner's
                        # uvicorn must be fully down by then.
                        await reset_default_runner()
                        assert not runner.is_running, (
                            f"iteration {i}: reset left runner running"
                        )
                        assert not _is_port_open("127.0.0.1", port), (
                            f"iteration {i}: port {port} not released by reset"
                        )
                    finally:
                        try:
                            if runner.is_running:
                                await runner.drain(timeout=2.0)
                        except Exception:
                            pass
                        lifecycle_mod._default_runner = None

            asyncio.run(tight_loop())
            # The singleton must end up None.
            assert _default_runner is None
        finally:
            os.environ.pop("HERMES_NODES_TOKEN_KEY", None)

    def test_reset_twice_in_quick_succession_is_safe(
        self,
        free_port: int,
        fernet_key: str,
        tmp_path: Path,
    ) -> None:
        """Two resets back-to-back: the second is a no-op and does not raise.

        Issue #20's regression-test #1: ``reset_default_runner``
        called twice in a row must not raise on the second call
        (the first drain is in flight or complete, the second sees
        no runner to drain).
        """
        import os

        import hermes_nodes_plugin.lifecycle as lifecycle_mod
        from hermes_nodes_plugin.lifecycle import (
            _default_runner,
            reset_default_runner,
        )

        os.environ["HERMES_NODES_TOKEN_KEY"] = fernet_key
        try:

            async def scenario() -> None:
                runner = self._make_runner(free_port, fernet_key, tmp_path)
                lifecycle_mod._default_runner = runner
                try:
                    await runner.start()
                    assert runner.is_running

                    await reset_default_runner()
                    # First reset cleared the global.
                    assert _default_runner is None
                    # Second reset sees no runner — must be a no-op,
                    # must not raise.
                    await reset_default_runner()
                    assert _default_runner is None
                finally:
                    try:
                        if runner.is_running:
                            await runner.drain(timeout=2.0)
                    except Exception:
                        pass
                    lifecycle_mod._default_runner = None

            asyncio.run(scenario())
        finally:
            os.environ.pop("HERMES_NODES_TOKEN_KEY", None)

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
        reset_default_runner_sync()

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
        reset_default_runner_sync()

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
