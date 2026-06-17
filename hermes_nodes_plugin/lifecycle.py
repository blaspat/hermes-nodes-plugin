"""Background asyncio lifecycle for the WSS node server.

Wires :class:`hermes_nodes_plugin.server.create_app` (Task 2.4) and
:class:`hermes_nodes_plugin.registry.NodeRegistry` (Task 2.5) into
a long-lived :class:`ServerRunner` that can be started, drained, and
re-started across plugin sessions.

Scope of this module (Task 2.6)
--------------------------------

What lives here:

  * :class:`ServerRunner` — owns the uvicorn.Server instance, the
    background asyncio task, and the FastAPI app.
  * :func:`get_default_runner` / :func:`reset_default_runner` — module
    singletons used by :func:`hermes_nodes_plugin.register` to attach
    a single runner to the gateway's session lifecycle.
  * The two thin hook callbacks the plugin's ``register(ctx)`` binds:
    they translate Hermes session events into ``runner.start`` /
    ``runner.drain`` calls.

What does NOT live here (later tasks):

  * Task 2.7+ — message dispatch (``exec`` / ``read`` / ``write``)
    handlers in :mod:`hermes_nodes_plugin.server`. This module
    only owns the *transport* lifecycle, not the request/response
    semantics.
  * Task 2.10 — the ``hermes node pair`` / ``list`` / ``revoke`` CLI
    subcommand surface. We register a ``node`` subcommand stub here
    so the CLI appears (``hermes --help`` shows it) but the full
    argparse tree lands in 2.10.
  * Task 2.11 — offline error handling (``node_exec`` against a
    disconnected node returns in <2s with a clear message). The
    runner's drain-timeout defaults to 5s because draining in-flight
    WebSocket close negotiations can take longer than the 2s
    request budget.

Design notes
------------

**Why a custom asyncio wrapper around uvicorn.Server?**  The
``uvicorn.run`` entry point calls ``asyncio.run`` internally, which
*creates* a new event loop. Inside the gateway the loop is already
running, so we drive ``Server.startup()`` / ``Server.main_loop()`` /
``Server.shutdown()`` directly. We also skip the signal handler
capture (the gateway owns signals) by not calling
``Server.serve()`` — we re-implement the three coroutines it would
have called, in order.

**Idempotency.**  ``start`` is a no-op when the runner is already
running; ``drain`` is a no-op when the runner is already stopped.
Hermes may invoke ``on_session_start`` repeatedly across a long-lived
gateway (one per conversation); we must not spawn a second uvicorn
on the same port.

**Defensive startup.**  The runner's :meth:`__init__` does NOT
immediately build a token store from the env — that defers the
"Fernet key missing" error to :meth:`start`, where it can be logged
and skipped without raising. A broken plugin must not take down the
gateway. The CLI subcommand (Task 2.10) is where the operator
gets a clear "missing HERMES_NODES_TOKEN_KEY" error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Imported only for type checkers / annotations (under
    # ``from __future__ import annotations`` these are strings, so
    # even at runtime nothing here triggers a real import). We
    # avoid real imports at module top so this module — which is
    # wired into the plugin's hot register() path — can be
    # imported without pulling in fastapi / pydantic_core, whose
    # native extension sometimes fails to load inside the hermes
    # runtime's plugin loader. The runtime imports live next to
    # the functions that need them.
    import uvicorn  # noqa: F401
    from hermes_nodes_plugin.config import NodeServerConfig
    from hermes_nodes_plugin.registry import NodeRegistry
    from hermes_nodes_plugin.tokens import TokenStore

from hermes_nodes_plugin.audit import default_audit_writer
from hermes_nodes_plugin.config import load_config
from hermes_nodes_plugin.errors import ConfigError, TokenStoreError
from hermes_nodes_plugin.tokens import token_store_from_config


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ServerRunner
# ---------------------------------------------------------------------------


class ServerRunner:
    """Owns the uvicorn.Server task and the FastAPI app.

    Construction is cheap — the FastAPI app is built immediately (so
    the registry and store are wired up and the ``/ws/nodes`` route is
    registered), but uvicorn is *not* started. :meth:`start` brings
    it up; :meth:`drain` takes it down. Both are idempotent.

    The runner is single-shot for any given (config, store, registry)
    triple. Re-binding to a different port requires a fresh instance.
    """

    def __init__(
        self,
        *,
        config: NodeServerConfig,
        token_store: TokenStore,
        registry: NodeRegistry,
    ) -> None:
        # The FastAPI app / pydantic / uvicorn stack is built lazily
        # in :meth:`start` so constructing a ServerRunner is cheap.
        # This keeps CLI subcommands (which only check ``_registry``)
        # from triggering the fastapi import chain.
        self._config = config
        self._token_store = token_store
        self._registry = registry
        self._app: object | None = None  # Built on first start()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        # Background sweep task (issue #19) — calls
        # ``registry.stale()`` every ``heartbeat_sweep_interval_seconds``
        # and closes any node whose ``last_heartbeat`` is older than
        # ``heartbeat_stale_seconds``. Created in :meth:`start`,
        # cancelled in :meth:`drain`.
        self._sweep_task: asyncio.Task[None] | None = None
        # ``_stop_sweep`` is set in :meth:`drain` to ask the sweep
        # loop to exit at the top of its next iteration. We use an
        # :class:`asyncio.Event` rather than a bare ``bool`` so the
        # awaitable ``wait`` can be cancelled cleanly (the event's
        # ``wait`` is the only thing we await, so cancelling the
        # sweep task while it's sleeping unblocks immediately).
        self._stop_sweep: asyncio.Event = asyncio.Event()
        # Expose the port for tests + the CLI; the config is the
        # source of truth, but having a flat attribute saves the
        # boilerplate in callers that just want ``runner.port``.
        self.port: int = config.port
        self.host: str = config.host

    # -- introspection ------------------------------------------------------

    @property
    def app(self):
        """The FastAPI app (used by tests via ``TestClient(runner.app)``)."""
        return self._app

    @property
    def is_running(self) -> bool:
        """True when the uvicorn task is alive (started, not done)."""
        return self._task is not None and not self._task.done()

    # -- control -----------------------------------------------------------

    async def start(self) -> None:
        """Start uvicorn in the background; return once the port is bound.

        Idempotent: a second call while running is a no-op and returns
        immediately. The actual bind happens inside
        :meth:`uvicorn.Server.startup`; we await the task until
        ``started`` flips, then return control to the caller. That
        ordering matches uvicorn's contract — the port is open before
        ``startup`` returns.
        """
        if self.is_running:
            logger.debug("hermes-nodes server already running; start() is a no-op")
            return

        # Build the FastAPI app lazily on first start. ``create_app``
        # imports fastapi/pydantic/uvicorn, so we defer it to the
        # point where we actually need the ASGI stack.
        if self._app is None:
            from hermes_nodes_plugin.server import create_app

            self._app = create_app(
                token_store=self._token_store,
                registry=self._registry,
                config=self._config,
            )

        # uvicorn is only needed when we actually start the server;
        # import it lazily so module import stays free of the heavy
        # ASGI / native-extension stack.
        import uvicorn

        ssl_kwargs: dict[str, Any] = {}
        if self._config.uses_tls():
            ssl_kwargs = {
                "ssl_certfile": self._config.tls_cert_path,
                "ssl_keyfile": self._config.tls_key_path,
            }

        # ``lifespan="off"`` is correct here: our FastAPI app has no
        # lifespan events. We drive uvicorn's three lifecycle
        # coroutines (``startup`` / ``main_loop`` / ``shutdown``)
        # directly instead of calling :meth:`Server.serve` because
        # ``serve`` wraps them in :meth:`capture_signals` — the
        # gateway already owns SIGINT/SIGTERM, and a second handler
        # layer would race with the gateway's own drain logic.
        # ``Server._serve`` (uvicorn's internal helper) installs the
        # lifespan object *before* calling ``startup``; the property
        # is only available after ``Config.load()`` returns.
        uvicorn_config = uvicorn.Config(
            self._app,  # type: ignore[arg-type]
            host=self._config.host,
            port=self._config.port,
            log_level="warning",
            lifespan="off",
            **ssl_kwargs,
        )
        if not uvicorn_config.loaded:
            uvicorn_config.load()
        self._server = uvicorn.Server(uvicorn_config)
        self._server.lifespan = uvicorn_config.lifespan_class(uvicorn_config)

        async def _serve() -> None:
            try:
                try:
                    await self._server.startup()
                except (OSError, SystemExit) as exc:
                    logger.warning(
                        "hermes-nodes: server startup failed "
                        "(port %d): %s",
                        self._config.port,
                        exc,
                    )
                    return
                if not self._server.should_exit:
                    await self._server.main_loop()
            finally:
                if self._server.started:
                    await self._server.shutdown()

        self._task = asyncio.create_task(_serve(), name="hermes-nodes-wss-server")

        # Background sweep (issue #19). Created after the uvicorn
        # task so the sweep is never running while the server isn't.
        # We re-arm ``_stop_sweep`` in case the runner was
        # start→drain→start-restart (drain clears it on the way down).
        self._stop_sweep = asyncio.Event()
        self._sweep_task = asyncio.create_task(
            self._sweep_stale_connections(),
            name="hermes-nodes-stale-sweep",
        )

        # Wait for the server to actually be listening before
        # returning. ``Server.started`` is set inside ``startup``
        # after the socket binds; we poll on a short sleep to avoid
        # coupling to uvicorn's internal state-machine ordering.
        for _ in range(200):  # 200 * 0.025s = 5s upper bound
            if self._server.started:
                break
            if self._server.should_exit:
                # startup() exited early (port in use, etc.).
                break
            await asyncio.sleep(0.025)
        else:
            # Timed out waiting for the bind. Drain so the task
            # doesn't leak.
            await self.drain(timeout=1.0)
            raise RuntimeError(
                f"hermes-nodes server failed to bind {self._config.host}:"
                f"{self._config.port} within 5s"
            )

        if not self._server.started and self._server.should_exit:
            # Server couldn't start (EADDRINUSE, etc.). Surface the
            # error so the caller can log it; do not raise past
            # here in the hook path (callers handle errors).
            logger.error(
                "hermes-nodes server failed to start on %s:%d",
                self._config.host,
                self._config.port,
            )
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
            self._server = None
            return

        logger.info(
            "hermes-nodes WSS server listening on %s:%d",
            self._config.host,
            self._config.port,
        )

    async def drain(self, *, timeout: float = 5.0) -> None:
        """Stop the server gracefully.

        Idempotent: a no-op when the server is already stopped. Sets
        uvicorn's ``should_exit`` flag (the same flag a SIGINT would
        set), then awaits the background task. Active WebSocket
        connections receive a close frame; their own handlers
        unregister from the registry on disconnect.

        Also stops the background stale-connection sweep (issue #19)
        before awaiting the uvicorn task. The sweep is cheap, so the
        drain timeout is unchanged.
        """
        # Stop the sweep first (issue #19) so the loop exits at the
        # top of its next iteration. ``_stop_sweep.set()`` is
        # synchronous and safe to call here; awaiting the task itself
        # is folded into the uvicorn-task wait below.
        self._stop_sweep.set()
        sweep_task = self._sweep_task
        if sweep_task is not None and not sweep_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(sweep_task), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "hermes-nodes stale sweep did not exit within %.1fs; cancelling",
                    timeout,
                )
                sweep_task.cancel()
                try:
                    await sweep_task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("hermes-nodes stale sweep raised on drain: %s", exc)
        self._sweep_task = None

        server = self._server
        task = self._task
        if server is None or task is None:
            return
        if task.done():
            self._server = None
            self._task = None
            return

        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "hermes-nodes server did not drain within %.1fs; cancelling",
                timeout,
            )
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("hermes-nodes server drain raised: %s", exc)
        finally:
            self._server = None
            self._task = None

    async def _sweep_stale_connections(self) -> None:
        """Background sweep: close dead nodes (PROTOCOL §6, issue #19).

        Loops until :attr:`_stop_sweep` is set. On each tick:

        1. ``await registry.stale(older_than=...)`` returns a snapshot
           of nodes whose last_heartbeat is older than the configured
           threshold. ``stale`` is read-only — it does NOT unregister.
        2. For each candidate, call ``websocket.close()`` (via the
           server module's ``_safe_close`` helper) with a normal
           close code. The connection handler's ``finally`` block
           then unregisters the node and fails any pending waiters.
        3. Sleep for ``heartbeat_sweep_interval_seconds``, but bail
           out immediately if the stop event fires.

        Failures closing an individual WebSocket are logged and
        swallowed — one dead socket must not poison the sweep loop
        or take the server down.
        """
        from hermes_nodes_plugin.server import _safe_close

        stale_after = timedelta(seconds=self._config.heartbeat_stale_seconds)
        interval = self._config.heartbeat_sweep_interval_seconds

        try:
            while not self._stop_sweep.is_set():
                try:
                    candidates = await self._registry.stale(older_than=stale_after)
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning("hermes-nodes stale sweep query failed: %s", exc)
                    candidates = []

                for conn in candidates:
                    if self._stop_sweep.is_set():
                        break
                    try:
                        # ``_safe_close`` swallows "already closed"
                        # errors so we don't double-log on a socket
                        # that closed between the stale query and us
                        # reaching this line.
                        await _safe_close(conn.websocket, code=1000)
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning(
                            "hermes-nodes stale sweep: failed to close %r: %s",
                            conn.name,
                            exc,
                        )
                    else:
                        logger.info(
                            "hermes-nodes stale sweep: closed stale connection %r "
                            "(idle > %ds)",
                            conn.name,
                            self._config.heartbeat_stale_seconds,
                        )

                # Sleep in 1s slices so the drain path can wake us
                # within ~1s even on the longest interval. The
                # ``wait_for`` is bounded by the remainder of the
                # interval (or 0 if it's already past); the stop
                # event is the *only* thing we wait on, so cancelling
                # the sweep task unblocks it immediately.
                try:
                    await asyncio.wait_for(
                        self._stop_sweep.wait(), timeout=interval
                    )
                except asyncio.TimeoutError:
                    # Expected: interval elapsed, run another sweep.
                    pass
        except asyncio.CancelledError:
            # Drain cancelled the sweep before the stop event fired.
            # Re-raise so the cancelling task sees the cancellation.
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.error("hermes-nodes stale sweep crashed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level default runner (used by the plugin's register())
# ---------------------------------------------------------------------------


# The plugin's ``register(ctx)`` binds a *singleton* runner: there's
# one per Hermes process. Tests call :func:`reset_default_runner` to
# start each test from a clean slate. The runner is constructed
# lazily on the first ``start()`` so the "Fernet key missing" error
# is deferred to startup, not to plugin load.
_default_runner: ServerRunner | None = None


def _build_default_runner() -> ServerRunner:
    """Construct the singleton :class:`ServerRunner`.

    Resolves config via :func:`load_config` and the token store via
    :func:`token_store_from_config`. Errors are raised to the caller
    (we don't swallow them in the build path; that's the whole point
    of the singleton — the caller explicitly asked for the runner).
    """
    # ``NodeRegistry`` (and via it, ``fastapi.WebSocket`` and the
    # pydantic_core native extension) is imported lazily. If a host
    # pulls in this module before the gateway has finished wiring
    # its native-extension paths, eager import would brick the
    # process. We pay the import cost here, only when a runner is
    # actually being built (typically on first session start).
    from hermes_nodes_plugin.registry import NodeRegistry

    config = load_config()
    store = token_store_from_config(config)
    registry = NodeRegistry()
    return ServerRunner(config=config, token_store=store, registry=registry)


def get_default_runner() -> ServerRunner:
    """Return the singleton runner, constructing it on first call.

    Raises:
        ConfigError: YAML parse failure / bad port / partial TLS.
        TokenStoreError: Fernet key env var unset.
    """
    global _default_runner
    if _default_runner is None:
        _default_runner = _build_default_runner()
    return _default_runner


async def reset_default_runner() -> None:
    """Clear the singleton (tests only), awaiting its drain.

    Issue #20: the previous implementation synchronously cancelled
    the runner's task without awaiting it. The cancel is a *signal*
    — the actual shutdown is async, so a test that called
    ``reset_default_runner`` and then immediately re-built a runner
    could race the previous uvicorn's socket release and hit
    ``EADDRINUSE``.

    This coroutine drains first (so the port is actually free) and
    then clears the singleton. Async tests should ``await`` it.

    If there is no running runner, this is a cheap no-op.
    """
    global _default_runner
    if _default_runner is None:
        return
    runner = _default_runner
    _default_runner = None
    if not runner.is_running:
        return
    # 2s is enough for a clean drain (uvicorn propagates a close
    # frame to active WebSockets, which usually completes in <100ms
    # on loopback). If the drain doesn't finish in time, the runner
    # is forcibly cancelled — see ServerRunner.drain.
    await runner.drain(timeout=2.0)


def reset_default_runner_sync(timeout: float = 5.0) -> None:
    """Sync shim for :func:`reset_default_runner` (issue #20).

    Some test paths (fixture teardown, session cleanup) call reset
    from synchronous code that is *not* itself running inside a
    coroutine. For that common case, the shim builds a fresh event
    loop via :func:`asyncio.run` and drives the runner's drain on
    it. By the time the shim returns, uvicorn is fully unwound
    and the port is free.

    This shim **must not be called from inside a running event
    loop** (e.g. from within an async test that has its own
    ``asyncio.run`` already in flight). The proper API for that
    context is :func:`reset_default_runner` — `await` it. If we
    detect this misuse we raise :class:`RuntimeError` with a
    clear message rather than deadlocking the loop.

    The shim is safe to call when there is no running runner
    (returns immediately).

    The ``timeout`` is independent of the runner's own drain
    timeout: if the drain doesn't complete in ``timeout`` seconds
    we surface a :class:`RuntimeError` rather than silently
    leaking uvicorn into the next test.
    """
    global _default_runner
    if _default_runner is None or not _default_runner.is_running:
        # Either no runner yet, or the async test path already
        # cleared it. No work to do.
        return
    runner = _default_runner
    _default_runner = None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — drive the drain on a fresh loop. This
        # is the common sync-test path. ``asyncio.run`` builds a
        # loop, runs the coroutine, and tears the loop down
        # cleanly. By the time we return, the uvicorn task is
        # fully unwound and the port is released.
        try:
            asyncio.run(runner.drain(timeout=2.0))
        except Exception as exc:
            # Drain failed — the runner is still alive and
            # registered. Restore the global so a retry or
            # a diagnostic call can find it.
            _default_runner = runner
            raise RuntimeError(
                f"reset_default_runner_sync: drain failed: {exc!r}"
            ) from exc
        return

    # We're inside a running loop. The sync shim cannot block the
    # loop's thread waiting for a coroutine on the same loop — the
    # coroutine would never get scheduled. Tell the caller to use
    # the async path.
    raise RuntimeError(
        "reset_default_runner_sync called from inside a running "
        "event loop. Use `await reset_default_runner()` instead — "
        "the sync shim cannot wait for a coroutine on the same loop "
        "without deadlocking."
    )


# ---------------------------------------------------------------------------
# Hook callbacks (bound by register() in __init__.py)
# ---------------------------------------------------------------------------


async def _on_session_start() -> None:
    """Start the default runner.

    Defensive: any startup error is logged and swallowed so the
    gateway's session start isn't blocked by a misconfigured node
    server. The CLI subcommand (Task 2.10) is where the operator
    gets the clear "missing HERMES_NODES_TOKEN_KEY" error.

    Side effect: purges audit-log rotations older than
    ``audit_retention_days`` (FR-5.4). The purge runs *before*
    the runner starts so a multi-GB leaked log from a previous
    install does not slow down the first call of the new
    session. Failures here are logged and swallowed — they
    must not block the gateway.
    """
    try:
        # Resolve the audit writer eagerly so the purge happens
        # at session start (when operators are paying attention
        # to log warnings) rather than mid-call. ``close`` on
        # the writer is a no-op until the first ``record`` opens
        # the file, so the cost is purely config resolution.
        audit = default_audit_writer()
        try:
            purged = audit.purge_expired_rotations()
            if purged:
                logger.info(
                    "hermes-nodes: purged %d expired audit-log rotation(s)", purged
                )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("hermes-nodes: audit purge failed: %s", exc)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-nodes: audit writer init failed: %s", exc)

    try:
        runner = get_default_runner()
    except (ConfigError, TokenStoreError) as exc:
        logger.warning(
            "hermes-nodes: cannot start server (%s) — set "
            "HERMES_NODES_TOKEN_KEY and check ~/.hermes/hermes-nodes.yaml",
            exc,
        )
        return
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-nodes: unexpected error building runner: %s", exc)
        return

    try:
        await runner.start()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-nodes: runner.start() failed: %s", exc)


async def _on_session_end() -> None:
    """Drain the default runner.

    Defensive: a drain failure is logged but never raised — the
    gateway is shutting down regardless. A stale runner leaks
    uvicorn at worst; we'd rather that than a noisy traceback on
    every session boundary.

    Also flushes the audit writer so the last few rows of the
    session land on disk before the process exits. A graceful
    drain that leaves the audit log unflushed would lose the
    most recent call entries on the next cold start.
    """
    runner = _default_runner
    if runner is not None:
        try:
            await runner.drain(timeout=5.0)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("hermes-nodes: runner.drain() failed: %s", exc)
    try:
        from hermes_nodes_plugin.audit import reset_default_audit_writer

        reset_default_audit_writer()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-nodes: audit close failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI subcommand (Task 2.10 — real surface lives in hermes_nodes_plugin.cli)
# ---------------------------------------------------------------------------


def setup_node_subcommand(subparser: argparse.ArgumentParser) -> None:
    """Argparse setup for ``hermes node`` — delegates to :mod:`cli`.

    The real ``pair`` / ``list`` / ``revoke`` / ``status`` argparse
    tree lives in :func:`hermes_nodes_plugin.cli.setup_node_cli`
    (Task 2.10). This shim remains so the import in
    :mod:`hermes_nodes_plugin.__init__` and the existing test
    (``test_register_adds_node_cli_subcommand``) keep working
    without modification.
    """
    from hermes_nodes_plugin.cli import setup_node_cli

    setup_node_cli(subparser)


def _node_command_dispatch(args: argparse.Namespace) -> int:
    """Default dispatch for ``hermes node`` subcommands.

    Kept as a thin wrapper for backward compat with any code that
    imports it directly. The real dispatch lives in
    :func:`hermes_nodes_plugin.cli.node_command`.
    """
    from hermes_nodes_plugin.cli import node_command

    return node_command(args)


__all__ = [
    "ServerRunner",
    "get_default_runner",
    "reset_default_runner",
    "reset_default_runner_sync",
    "_on_session_start",
    "_on_session_end",
    "setup_node_subcommand",
]
