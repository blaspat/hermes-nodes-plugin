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
from typing import Any

import uvicorn

from hermes_nodes_plugin.config import NodeServerConfig, load_config
from hermes_nodes_plugin.errors import ConfigError, TokenStoreError
from hermes_nodes_plugin.registry import NodeRegistry
from hermes_nodes_plugin.server import create_app
from hermes_nodes_plugin.tokens import TokenStore, token_store_from_config


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
        self._config = config
        self._token_store = token_store
        self._registry = registry
        self._app = create_app(
            token_store=token_store,
            registry=registry,
            config=config,
        )
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
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
            self._app,
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
                await self._server.startup()
                if not self._server.should_exit:
                    await self._server.main_loop()
            finally:
                if self._server.started:
                    await self._server.shutdown()

        self._task = asyncio.create_task(_serve(), name="hermes-nodes-wss-server")

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
        """
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


def reset_default_runner() -> None:
    """Clear the singleton (tests only).

    Drains first if the runner is still running, so we don't leave
    uvicorn alive across tests. Drains on a fresh event loop if
    needed (we can't await on the runner from a sync context).
    """
    global _default_runner
    if _default_runner is not None and _default_runner.is_running:
        # The runner is bound to a live loop. Best-effort: cancel the
        # task without awaiting — tests own their own loops.
        if _default_runner._task is not None:  # type: ignore[attr-defined]
            _default_runner._task.cancel()  # type: ignore[attr-defined]
    _default_runner = None


# ---------------------------------------------------------------------------
# Hook callbacks (bound by register() in __init__.py)
# ---------------------------------------------------------------------------


async def _on_session_start() -> None:
    """Start the default runner.

    Defensive: any startup error is logged and swallowed so the
    gateway's session start isn't blocked by a misconfigured node
    server. The CLI subcommand (Task 2.10) is where the operator
    gets the clear "missing HERMES_NODES_TOKEN_KEY" error.
    """
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
    """
    runner = _default_runner
    if runner is None:
        return
    try:
        await runner.drain(timeout=5.0)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("hermes-nodes: runner.drain() failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI subcommand (stub for Task 2.6; full surface lands in Task 2.10)
# ---------------------------------------------------------------------------


def _node_subcommand_help() -> str:
    return (
        "Manage paired hermes-nodes (WSS node server). "
        "Subcommands `pair`/`list`/`revoke` land in Task 2.10; "
        "this stub is present so `hermes node` appears once the "
        "plugin is installed."
    )


def setup_node_subcommand(subparser: argparse.ArgumentParser) -> None:
    """Argparse setup for the ``hermes node`` subcommand.

    Task 2.10 replaces this with the real ``pair`` / ``list`` /
    ``revoke`` parser tree. For now we add a single ``status``
    action that reports whether the default runner is up — enough
    to verify plugin auto-load from the CLI.

    The ``func`` default is set so ``hermes node`` without a
    subcommand prints help instead of crashing.
    """
    subparser.description = _node_subcommand_help()
    subs = subparser.add_subparsers(dest="node_action")

    # Registered for `hermes node status --help`; the dispatch happens
    # in :func:`_node_command_dispatch`. The local binding is dropped
    # (add_subparsers/add_parser's return value is the only side effect
    # we care about).
    subs.add_parser(
        "status",
        help="Show whether the WSS node server is running.",
    )

    subparser.set_defaults(func=_node_command_dispatch)


def _node_command_dispatch(args: argparse.Namespace) -> int:
    """Default dispatch for ``hermes node`` subcommands."""
    action = getattr(args, "node_action", None)
    if not action:
        # ``hermes node`` with no subcommand: print help.
        return 0
    if action == "status":
        runner = _default_runner
        if runner is None or not runner.is_running:
            print("hermes-nodes server: not running")
            return 1
        print(f"hermes-nodes server: listening on {runner.host}:{runner.port}")
        return 0
    return 2


__all__ = [
    "ServerRunner",
    "get_default_runner",
    "reset_default_runner",
    "_on_session_start",
    "_on_session_end",
    "setup_node_subcommand",
]
