"""Tests for :mod:`hermes_nodes_plugin.environment` (Task 2.7).

The plan calls for "mock WSS connection, assert ``execute()``
roundtrip". We do that in two layers:

  1. **Offline** tests that drive :class:`NodeEnvironment`
     directly against a registry with no real node — these
     cover the no-roundtrip paths (unknown target, empty
     command, etc.) and run inline with :func:`asyncio.run`.

  2. **Integration** tests that run a real :class:`NodeEnvironment`
     against a real uvicorn-bound FastAPI app with a *fake
     node* on the other end of the WebSocket. The fake node
     is a coroutine that speaks the PROTOCOL §3 subset the
     server cares about (``exec`` → ``exec_result``). We use
     a single :func:`asyncio.run` per test — the env, the
     fake node, and the server's handlers all share the
     same event loop, so the registry waiters the env
     registers are immediately visible to the dispatch
     loop on the same loop.

Why uvicorn + the ``websockets`` client, not ``TestClient``?
``TestClient`` is a sync wrapper that runs the ASGI app on
a portal in a background thread, and its WebSocket API is
sync — sharing the socket between an async env (running on
the test client's loop) and a sync test body (running on
the test thread) is racy. A real uvicorn + ``websockets``
client lets us drive everything from one async loop and
keeps the test reasoning simple.

Critical sequencing note
-----------------------

``registry.get`` is ``async with self._lock: ...``. The body
of the with block runs synchronously after the lock is
acquired, so the env's first call to ``get`` does *not* yield
to the event loop. That means we can't just
``asyncio.create_task(_pair_node)`` and then immediately
``await env.execute()`` — the env would see an empty
registry and raise ``NodeNotConnectedError`` before the
fake-node task got a chance to start.

The fix is the ``pair_then_run`` helper: it pairs the node,
sets an ``asyncio.Event`` to signal completion, and then
the test awaits the event before running the env call.
That sequencing is the single most important thing the
integration tests rely on.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
import uvicorn
from cryptography.fernet import Fernet

from hermes_nodes_plugin.config import NodeServerConfig
from hermes_nodes_plugin.environment import (
    DEFAULT_EXEC_TIMEOUT_SECONDS,
    NodeEnvironment,
)
from hermes_nodes_plugin.errors import (
    NodeExecutionError,
    NodeNotConnectedError,
)
from hermes_nodes_plugin.registry import NodeRegistry
from hermes_nodes_plugin.server import create_app
from hermes_nodes_plugin.tokens import TokenStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def store(tmp_path: Path, fernet_key: str) -> TokenStore:
    return TokenStore(path=tmp_path / "tokens.json", key=fernet_key)


@pytest.fixture
def registry() -> NodeRegistry:
    """A registry shared between the FastAPI app, the fake node, and the env.

    The :class:`NodeEnvironment` we test against is constructed
    with the same registry, so when a node completes the auth
    handshake the environment's :meth:`get` returns the live
    connection — the roundtrip can actually happen.
    """
    return NodeRegistry()


@pytest.fixture
def app(store: TokenStore, registry: NodeRegistry):
    return create_app(
        token_store=store,
        registry=registry,
        config=NodeServerConfig(),
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_server(app: Any) -> Iterator[uvicorn.Server]:
    """Bring up uvicorn in a background thread bound to a free port.

    Yields the running :class:`uvicorn.Server` so the test
    can call ``server.should_exit = True`` (or just exit the
    context) to drain it. The thread is a daemon so a
    runaway test doesn't hang the suite on interpreter exit.
    """
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=_free_port(),
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    # Wait for the port to bind. Uvicorn sets ``started`` once
    # the socket is listening; we poll on a short sleep to
    # avoid coupling to internal state.
    for _ in range(200):  # 200 * 0.025s = 5s upper bound
        if server.started:
            break
        time.sleep(0.025)
    else:  # pragma: no cover — should never trip in practice
        server.should_exit = True
        thread.join(timeout=2.0)
        raise RuntimeError("uvicorn test server failed to bind within 5s")
    try:
        yield server
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture
def running_server(app):
    with _running_server(app) as server:
        yield server


# ---------------------------------------------------------------------------
# Offline / no-roundtrip paths
# ---------------------------------------------------------------------------


def test_constructor_rejects_empty_target() -> None:
    """``target`` must be a non-empty string — the server keys on it."""
    with pytest.raises(ValueError, match="non-empty"):
        NodeEnvironment("")


def test_constructor_rejects_zero_or_negative_timeout() -> None:
    """A timeout of zero would short-circuit every call; reject up front."""
    with pytest.raises(ValueError, match="timeout"):
        NodeEnvironment("laptop", timeout=0)
    with pytest.raises(ValueError, match="timeout"):
        NodeEnvironment("laptop", timeout=-1)


def test_constructor_uses_module_default_timeout() -> None:
    """The default timeout matches the protocol's default (60s)."""
    env = NodeEnvironment("laptop")
    assert env.timeout == DEFAULT_EXEC_TIMEOUT_SECONDS
    assert DEFAULT_EXEC_TIMEOUT_SECONDS == 60.0


def test_execute_returns_empty_for_empty_command() -> None:
    """An empty command is a no-op (matches the local backend's stance)."""
    env = NodeEnvironment("never-paired")
    result = asyncio.run(env.execute(""))
    assert result == {"output": "", "returncode": 0}


def test_execute_raises_node_not_connected_for_unknown_target() -> None:
    """A name the registry has never seen → ``NodeNotConnectedError``, no roundtrip."""
    env = NodeEnvironment("ghost")
    with pytest.raises(NodeNotConnectedError, match="ghost"):
        asyncio.run(env.execute("echo hi"))


# ---------------------------------------------------------------------------
# Helpers for the integration tests
# ---------------------------------------------------------------------------


def _ws_url(server: uvicorn.Server) -> str:
    """The ``ws://`` URL the fake node should connect to."""
    return f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/ws/nodes"  # type: ignore[union-attr]


async def _pair_node(ws_url: str, token: str, name: str) -> Any:
    """Connect, handshake (hello → auth), and return the WebSocket.

    Yields a few times after the handshake so the server's
    ``register`` coroutine (scheduled on its event loop) has
    a chance to run. The env's ``get`` is the consumer of
    the registration; if the env runs *before* the server
    loop processes the register call, the roundtrip can't
    start (the env raises ``NodeNotConnectedError``).
    """
    import websockets

    ws = await websockets.connect(ws_url)
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": name,
            }
        )
    )
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack", f"unexpected: {ack}"
    await ws.send(json.dumps({"type": "auth", "node_name": name, "token": token}))
    auth_ok = json.loads(await ws.recv())
    assert auth_ok["type"] == "auth_ok", f"unexpected: {auth_ok}"
    # Yield several times so the server's ``register`` runs.
    for _ in range(20):
        await asyncio.sleep(0.01)
    return ws


def _command_of(received: list[dict[str, Any]]) -> str:
    """Extract the single ``exec`` message the server sent (asserts exactly one)."""
    exec_msgs = [m for m in received if m.get("type") == "exec"]
    assert len(exec_msgs) == 1, f"expected one exec, got {len(exec_msgs)}"
    return exec_msgs[0]["command"]


# ---------------------------------------------------------------------------
# Roundtrip: real WebSocket, real server, fake node, all in one loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_routes_exec_and_returns_stdout(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """The full path: env -> exec -> server -> fake node -> exec_result -> env."""
    env = NodeEnvironment("laptop", registry=registry)
    ws_url = _ws_url(running_server)
    token = store.create("laptop")

    def _echo_exec(msg: dict[str, Any]) -> dict[str, Any] | None:
        if msg.get("type") != "exec":
            return None
        return {
            "type": "exec_result",
            "id": msg["id"],
            "status": "ok",
            "exit_code": 0,
            "stdout": f"got: {msg['command']}",
            "stderr": "",
            "duration_ms": 12,
            "truncated": False,
        }

    pair_done = asyncio.Event()
    received: list[dict[str, Any]] = []

    async def _pair_then_read() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                received.append(msg)
                reply = _echo_exec(msg)
                if reply is not None:
                    await ws.send(json.dumps(reply))
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_read())
    await pair_done.wait()
    result = await env.execute("echo hi")
    await reader_task

    assert result == {"output": "got: echo hi", "returncode": 0}
    assert _command_of(received) == "echo hi"
    # No cwd/env overrides on this call → not on the wire.
    exec_msg = next(m for m in received if m.get("type") == "exec")
    assert exec_msg.get("cwd", "") == "", "no cwd override → not on wire"
    assert exec_msg.get("env", {}) == {}, "no env override → not on wire"


@pytest.mark.asyncio
async def test_execute_sends_cwd_and_env_overrides_when_supplied(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """``cwd`` and ``env`` go on the wire when the caller passes them.

    Per the plan, persistent state lives on the node; the wire
    only carries *overrides*. If the environment were sending
    cwd/env on every call regardless, that would defeat the
    persistence contract.
    """
    env = NodeEnvironment("laptop", registry=registry)
    ws_url = _ws_url(running_server)
    token = store.create("laptop")

    received: list[dict[str, Any]] = []
    pair_done = asyncio.Event()

    async def _pair_then_read() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                received.append(msg)
                if msg.get("type") == "exec":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "exec_result",
                                "id": msg["id"],
                                "status": "ok",
                                "exit_code": 0,
                                "stdout": "",
                                "stderr": "",
                                "duration_ms": 1,
                                "truncated": False,
                            }
                        )
                    )
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_read())
    await pair_done.wait()
    await env.execute("ls", cwd="/tmp", env={"FOO": "bar", "BAZ": "qux"})
    await reader_task

    exec_msg = next(m for m in received if m.get("type") == "exec")
    assert exec_msg["cwd"] == "/tmp"
    assert exec_msg["env"] == {"FOO": "bar", "BAZ": "qux"}


@pytest.mark.asyncio
async def test_execute_converts_timeout_to_milliseconds(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """The protocol uses ``timeout_ms``; the env accepts seconds and converts."""
    env = NodeEnvironment("laptop", registry=registry, timeout=2.5)
    ws_url = _ws_url(running_server)
    token = store.create("laptop")

    received: list[dict[str, Any]] = []
    pair_done = asyncio.Event()

    async def _pair_then_read() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                received.append(msg)
                if msg.get("type") == "exec":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "exec_result",
                                "id": msg["id"],
                                "status": "ok",
                                "exit_code": 0,
                                "stdout": "",
                                "stderr": "",
                                "duration_ms": 1,
                                "truncated": False,
                            }
                        )
                    )
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_read())
    await pair_done.wait()
    await env.execute("ls", timeout=3.0)
    await reader_task

    exec_msg = next(m for m in received if m.get("type") == "exec")
    assert exec_msg["timeout_ms"] == 3000


# ---------------------------------------------------------------------------
# Non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_merges_stderr_on_non_zero_exit(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """Non-zero exit: stderr is appended to output with a [stderr] tag.

    Matches the local backend's behaviour so the agent's
    renderer doesn't need a node-specific code path.
    """
    env = NodeEnvironment("laptop", registry=registry)
    ws_url = _ws_url(running_server)
    token = store.create("laptop")

    received: list[dict[str, Any]] = []
    pair_done = asyncio.Event()

    async def _pair_then_read() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                received.append(msg)
                if msg.get("type") == "exec":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "exec_result",
                                "id": msg["id"],
                                "status": "ok",
                                "exit_code": 2,
                                "stdout": "out-line\n",
                                "stderr": "err-line\n",
                                "duration_ms": 5,
                                "truncated": False,
                            }
                        )
                    )
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_read())
    await pair_done.wait()
    result = await env.execute("false")
    await reader_task

    assert result["returncode"] == 2
    assert "out-line" in result["output"]
    assert "[stderr]" in result["output"]
    assert "err-line" in result["output"]


@pytest.mark.asyncio
async def test_execute_appends_truncation_hint(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """``truncated: true`` becomes a hint line in output."""
    env = NodeEnvironment("laptop", registry=registry)
    ws_url = _ws_url(running_server)
    token = store.create("laptop")

    received: list[dict[str, Any]] = []
    pair_done = asyncio.Event()

    async def _pair_then_read() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                received.append(msg)
                if msg.get("type") == "exec":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "exec_result",
                                "id": msg["id"],
                                "status": "ok",
                                "exit_code": 0,
                                "stdout": "head",
                                "stderr": "",
                                "duration_ms": 1,
                                "truncated": True,
                            }
                        )
                    )
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_read())
    await pair_done.wait()
    result = await env.execute("huge")
    await reader_task

    assert "[output truncated at 10MB]" in result["output"]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_raises_node_execution_error_on_protocol_error(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """``status="error"`` from the node → ``NodeExecutionError`` carrying the code."""
    env = NodeEnvironment("laptop", registry=registry)
    ws_url = _ws_url(running_server)
    token = store.create("laptop")

    pair_done = asyncio.Event()

    async def _pair_then_read() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "exec":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "exec_result",
                                "id": msg["id"],
                                "status": "error",
                                "code": 3002,  # exec_failed (PROTOCOL §4)
                                "reason": "exec_failed",
                            }
                        )
                    )
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_read())
    await pair_done.wait()
    with pytest.raises(NodeExecutionError) as excinfo:
        await env.execute("false")
    await reader_task

    assert excinfo.value.code == 3002
    assert "exec_failed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_execute_raises_node_execution_error_on_timeout_status(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """``status="timeout"`` from the node → ``NodeExecutionError`` (code 3001)."""
    env = NodeEnvironment("laptop", registry=registry)
    ws_url = _ws_url(running_server)
    token = store.create("laptop")

    pair_done = asyncio.Event()

    async def _pair_then_read() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "exec":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "exec_result",
                                "id": msg["id"],
                                "status": "timeout",
                                # No code on the wire — the env should
                                # default to 3001 (PROTOCOL §4
                                # ``exec_timeout``).
                            }
                        )
                    )
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_read())
    await pair_done.wait()
    with pytest.raises(NodeExecutionError) as excinfo:
        await env.execute("sleep 999")
    await reader_task

    assert excinfo.value.code == 3001
    assert "timed out" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Lifecycle / cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_is_a_noop() -> None:
    """The environment holds no per-instance resources to release."""
    env = NodeEnvironment("laptop")
    assert await env.cleanup() is None
    assert await env.stop() is None
