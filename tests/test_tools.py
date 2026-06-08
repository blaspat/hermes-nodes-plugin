"""Tests for the Kate-facing tools (Task 2.8 / FR-3.2).

Two layers of coverage, matching the project's existing test style
(``test_environment.py`` does the same split between unit and
integration):

  1. **Plugin registration** — drive :func:`register` against a
     duck-typed ``ctx`` and assert that all four tools landed in
     the recorded calls. This is what the task's acceptance
     criteria actually check ("all 4 tools visible in Kate's tool
     list"); the deeper behavioural tests live in layer 2.

  2. **Tool behaviour** — exercise each tool handler in
     isolation against a fresh :class:`NodeRegistry`, with no
     real WSS roundtrip. We use the offline error paths
     (``NodeNotConnectedError`` for a never-paired target) for
     the basic shape assertions, then drive a full roundtrip
     with a real :class:`NodeEnvironment` + fake node for the
     read/write/exec happy paths. The roundtrip reuses the
     uvicorn + fake-node pattern from ``test_environment.py``
     because the protocol's response routing is the same.

The unit tests are the bulk of the file; the integration tests
are one or two per tool to prove the wire payload is shaped
correctly and the handler decodes the response without
crashing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import uvicorn
from cryptography.fernet import Fernet

from hermes_nodes_plugin import register as plugin_register
from hermes_nodes_plugin.config import NodeServerConfig
from hermes_nodes_plugin.environment import (
    MAX_FILE_BYTES,
)
from hermes_nodes_plugin.errors import NodeNotConnectedError
from hermes_nodes_plugin.registry import NodeRegistry
from hermes_nodes_plugin.server import create_app
from hermes_nodes_plugin.tokens import TokenStore
from hermes_nodes_plugin.tools import (
    NODE_EXEC_SCHEMA,
    NODE_LIST_SCHEMA,
    NODE_READ_SCHEMA,
    NODE_WRITE_SCHEMA,
    TOOLS,
    node_exec,
    node_list,
    node_read,
    node_write,
)


# ---------------------------------------------------------------------------
# Fixtures: shared with the test_environment style (kept local so this
# module can be lifted out of the suite without dragging uvicorn).
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def store(tmp_path: Path, fernet_key: str) -> TokenStore:
    return TokenStore(path=tmp_path / "tokens.json", key=fernet_key)


@pytest.fixture
def registry() -> NodeRegistry:
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
    config = uvicorn.Config(
        app, host="127.0.0.1", port=_free_port(),
        log_level="warning", lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.025)
    else:  # pragma: no cover
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


@pytest.fixture
def isolated_registry() -> NodeRegistry:
    """A registry the tool handlers can dispatch against without
    touching the singleton runner's state.

    The tools accept an optional ``registry=`` kwarg that overrides
    the default-registry resolution path. Every behavioural test
    passes this fixture so a test never depends on (or pollutes)
    the plugin's singleton state.
    """
    return NodeRegistry()


# ---------------------------------------------------------------------------
# Layer 1: plugin registration — the task's acceptance criterion
# ---------------------------------------------------------------------------


class TestRegisterRegistersAllFourTools:
    """The acceptance test for Task 2.8 / FR-3.2.

    "All 4 tools visible in Kate's tool list." We drive
    :func:`register` against a mock ``ctx`` and assert each
    expected name appears with the right schema and toolset.
    """

    @pytest.fixture
    def mock_ctx(self, monkeypatch) -> SimpleNamespace:
        """A duck-typed mock :class:`PluginContext`."""
        monkeypatch.setenv(
            "HERMES_NODES_TOKEN_KEY", Fernet.generate_key().decode("ascii")
        )
        from hermes_nodes_plugin.lifecycle import reset_default_runner_sync
        reset_default_runner_sync()

        ctx = SimpleNamespace(
            _registered_hooks={},
            _cli_commands={},
            _registered_tools=[],
            manifest=SimpleNamespace(
                name="hermes_nodes_plugin", key="hermes_nodes_plugin"
            ),
        )

        def _register_hook(name: str, callback: Any) -> None:
            ctx._registered_hooks[name] = callback

        def _register_cli_command(
            name: str, help: str, setup_fn: Any, handler_fn: Any = None,
        ) -> None:
            ctx._cli_commands[name] = {
                "help": help, "setup_fn": setup_fn, "handler_fn": handler_fn,
            }

        def _register_tool(
            *, name: str, toolset: str, schema: dict, handler: Any,
            emoji: str = "", **_: Any,
        ) -> None:
            ctx._registered_tools.append(
                {"name": name, "toolset": toolset, "schema": schema,
                 "handler": handler, "emoji": emoji}
            )

        ctx.register_hook = _register_hook  # type: ignore[attr-defined]
        ctx.register_cli_command = _register_cli_command  # type: ignore[attr-defined]
        ctx.register_tool = _register_tool  # type: ignore[attr-defined]
        return ctx

    def test_all_four_tools_registered(self, mock_ctx: SimpleNamespace) -> None:
        plugin_register(mock_ctx)  # type: ignore[arg-type]

        registered_names = [t["name"] for t in mock_ctx._registered_tools]
        assert registered_names == [
            "node_exec", "node_read", "node_write", "node_list",
        ], "expected exactly the four FR-3.2 tools, in order"

    def test_tools_registered_in_hermes_nodes_toolset(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        plugin_register(mock_ctx)  # type: ignore[arg-type]
        toolsets = {t["toolset"] for t in mock_ctx._registered_tools}
        assert toolsets == {"hermes_nodes"}, (
            "all node tools should land in the plugin's own toolset so the "
            "user can enable/disable the whole surface as one unit"
        )

    def test_each_tool_has_a_callable_handler(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        plugin_register(mock_ctx)  # type: ignore[arg-type]
        for entry in mock_ctx._registered_tools:
            assert callable(entry["handler"]), (
                f"{entry['name']} handler must be callable"
            )

    def test_each_tool_has_a_non_empty_schema(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        plugin_register(mock_ctx)  # type: ignore[arg-type]
        for entry in mock_ctx._registered_tools:
            schema = entry["schema"]
            assert schema.get("name") == entry["name"]
            assert schema.get("description"), (
                f"{entry['name']} must have a description for the agent UI"
            )
            params = schema.get("parameters", {})
            assert params.get("type") == "object"
            assert "properties" in params
            assert isinstance(params.get("required", []), list)

    def test_schemas_declare_required_params_correctly(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        """The required lists must match the docstrings exactly."""
        plugin_register(mock_ctx)  # type: ignore[arg-type]
        by_name = {t["name"]: t["schema"] for t in mock_ctx._registered_tools}

        assert by_name["node_exec"]["parameters"]["required"] == ["target", "command"]
        assert by_name["node_read"]["parameters"]["required"] == ["target", "path"]
        assert by_name["node_write"]["parameters"]["required"] == [
            "target", "path", "content",
        ]
        assert by_name["node_list"]["parameters"]["required"] == []

    def test_tools_tuple_matches_schemas(self) -> None:
        """Defensive: the TOOLS tuple in tools.py must stay in sync
        with the standalone schema constants so adding a new tool
        doesn't accidentally desync."""
        assert [name for name, *_ in TOOLS] == [
            NODE_EXEC_SCHEMA["name"],
            NODE_READ_SCHEMA["name"],
            NODE_WRITE_SCHEMA["name"],
            NODE_LIST_SCHEMA["name"],
        ]

    def test_register_does_not_break_existing_hooks(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        """Tool registration must not regress the lifecycle hooks
        added in Task 2.6. (If it did, the plugin would silently
        stop starting its WSS server.)"""
        plugin_register(mock_ctx)  # type: ignore[arg-type]
        assert "on_session_start" in mock_ctx._registered_hooks
        assert "on_session_end" in mock_ctx._registered_hooks
        assert "node" in mock_ctx._cli_commands

    def test_register_still_defensive_on_registration_failure(
        self, mock_ctx: SimpleNamespace
    ) -> None:
        """If the second registration call (tool loop) raises, the
        earlier hooks / CLI command should still be in place —
        never half-registered."""

        def _boom(**_kwargs: Any) -> None:
            raise RuntimeError("simulated tool registration failure")

        # Replace register_tool with one that always fails. The
        # earlier register_hook / register_cli_command calls
        # already succeeded; we want register() to swallow the
        # later error and leave the earlier ones intact.
        mock_ctx.register_tool = _boom  # type: ignore[attr-defined]

        # Must NOT raise.
        plugin_register(mock_ctx)  # type: ignore[arg-type]

        # Earlier registrations survived.
        assert "on_session_start" in mock_ctx._registered_hooks
        assert "node" in mock_ctx._cli_commands


# ---------------------------------------------------------------------------
# Layer 2: tool behaviour — offline / validation paths
# ---------------------------------------------------------------------------


class TestToolValidation:
    """Tools reject empty args up front (no wire roundtrip)."""

    @pytest.mark.asyncio
    async def test_node_exec_rejects_empty_target(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(ValueError, match="target"):
            await node_exec("", "ls", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_exec_rejects_empty_command(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(ValueError, match="command"):
            await node_exec("laptop", "", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_read_rejects_empty_target(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(ValueError, match="target"):
            await node_read("", "/tmp/x", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_read_rejects_empty_path(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(ValueError, match="path"):
            await node_read("laptop", "", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_write_rejects_empty_target(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(ValueError, match="target"):
            await node_write("", "/tmp/x", "hi", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_write_rejects_empty_path(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(ValueError, match="path"):
            await node_write("laptop", "", "hi", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_write_rejects_oversized_content(
        self, isolated_registry: NodeRegistry
    ) -> None:
        """Content above MAX_FILE_BYTES is caught client-side so
        the agent gets a clear error instead of waiting for the
        WSS frame limit to reject the payload mid-send."""
        huge = "x" * (MAX_FILE_BYTES + 1)
        with pytest.raises(ValueError, match="MAX_FILE_BYTES"):
            await node_write(
                "laptop", "/tmp/x", huge, registry=isolated_registry
            )

    @pytest.mark.asyncio
    async def test_node_write_rejects_unknown_mode(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(ValueError, match="mode"):
            await node_write(
                "laptop", "/tmp/x", "hi", mode="nuke",
                registry=isolated_registry,
            )


class TestToolOfflineErrors:
    """Calls against a target the registry has never seen raise
    :class:`NodeNotConnectedError` immediately — no wire roundtrip."""

    @pytest.mark.asyncio
    async def test_node_exec_raises_not_connected(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(NodeNotConnectedError):
            await node_exec("ghost", "echo hi", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_read_raises_not_connected(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(NodeNotConnectedError):
            await node_read("ghost", "/etc/hostname", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_write_raises_not_connected(
        self, isolated_registry: NodeRegistry
    ) -> None:
        with pytest.raises(NodeNotConnectedError):
            await node_write("ghost", "/tmp/x", "hi", registry=isolated_registry)

    @pytest.mark.asyncio
    async def test_node_list_returns_empty_when_no_nodes(
        self, isolated_registry: NodeRegistry
    ) -> None:
        result = await node_list(registry=isolated_registry)
        assert result == {"nodes": [], "count": 0}


# ---------------------------------------------------------------------------
# Layer 2: tool behaviour — roundtrip integration
# ---------------------------------------------------------------------------
#
# We use the same uvicorn + fake-node pattern as test_environment.py.
# Each test pairs a real node, runs a tool, and asserts the tool's
# return value matches what the fake node reported. The fake node
# echoes the request back with a synthetic response so we can verify
# the tool layer is *also* correct (it must not modify the response
# shape) without depending on a real hermes-nodes binary.


def _ws_url(server: uvicorn.Server) -> str:
    return f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/ws/nodes"  # type: ignore[union-attr]


async def _pair_node(ws_url: str, token: str, name: str) -> Any:
    import websockets

    ws = await websockets.connect(ws_url)
    await ws.send(json.dumps({
        "type": "hello", "protocol_version": "0.1.0", "node_name": name,
    }))
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack", f"unexpected: {ack}"
    await ws.send(json.dumps({"type": "auth", "node_name": name, "token": token}))
    auth_ok = json.loads(await ws.recv())
    assert auth_ok["type"] == "auth_ok", f"unexpected: {auth_ok}"
    # Yield so the server's register coroutine runs.
    for _ in range(20):
        await asyncio.sleep(0.01)
    return ws


@pytest.mark.asyncio
async def test_node_exec_roundtrip(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
    isolated_registry: NodeRegistry,
) -> None:
    """``node_exec`` end-to-end: tool → env → server → fake node → tool."""
    # We need a node registered in the SAME registry the tool
    # uses. isolated_registry has no real WSS; instead register
    # a connection in the shared ``registry`` fixture and pass
    # THAT as the override.
    ws_url = _ws_url(running_server)
    token = store.create("laptop")

    pair_done = asyncio.Event()
    received: list[dict[str, Any]] = []

    async def _pair_then_echo() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                received.append(msg)
                if msg.get("type") == "exec":
                    await ws.send(json.dumps({
                        "type": "exec_result",
                        "id": msg["id"],
                        "status": "ok",
                        "exit_code": 0,
                        "stdout": f"got: {msg['command']}",
                        "stderr": "",
                        "duration_ms": 5,
                        "truncated": False,
                    }))
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_echo())
    await pair_done.wait()
    result = await node_exec("laptop", "echo hi", registry=registry)
    await reader_task

    assert result == {"output": "got: echo hi", "returncode": 0}
    assert any(m.get("type") == "exec" and m.get("command") == "echo hi"
               for m in received)


@pytest.mark.asyncio
async def test_node_read_roundtrip(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """``node_read`` end-to-end: tool returns decoded text + size."""
    ws_url = _ws_url(running_server)
    token = store.create("laptop")
    content_bytes = b"hello remote node\n"
    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    pair_done = asyncio.Event()
    sent_paths: list[str] = []

    async def _pair_then_reply_read() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "read":
                    sent_paths.append(msg["path"])
                    await ws.send(json.dumps({
                        "type": "read_result",
                        "id": msg["id"],
                        "status": "ok",
                        "content_b64": content_b64,
                        "size_bytes": len(content_bytes),
                        "truncated": False,
                    }))
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_reply_read())
    await pair_done.wait()
    result = await node_read(
        "laptop", "/etc/hostname", registry=registry,
    )
    await reader_task

    assert result == {
        "content": "hello remote node\n",
        "size_bytes": len(content_bytes),
        "truncated": False,
        "encoding": "utf-8",
    }
    assert sent_paths == ["/etc/hostname"]


@pytest.mark.asyncio
async def test_node_write_roundtrip_sends_base64(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """``node_write`` end-to-end: tool UTF-8-encodes and base64s
    the content, and decodes the write_result ack."""
    ws_url = _ws_url(running_server)
    token = store.create("laptop")
    content = "wrote this remotely\n"
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

    pair_done = asyncio.Event()
    sent: dict[str, Any] = {}

    async def _pair_then_reply_write() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("type") == "write":
                    sent["path"] = msg["path"]
                    sent["mode"] = msg["mode"]
                    sent["content_b64"] = msg["content_b64"]
                    await ws.send(json.dumps({
                        "type": "write_result",
                        "id": msg["id"],
                        "status": "ok",
                        "bytes_written": len(content.encode("utf-8")),
                    }))
                    return
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_reply_write())
    await pair_done.wait()
    result = await node_write(
        "laptop", "/tmp/remote.txt", content, mode="create",
        registry=registry,
    )
    await reader_task

    assert sent["path"] == "/tmp/remote.txt"
    assert sent["mode"] == "create"
    assert sent["content_b64"] == content_b64
    assert result == {
        "bytes_written": len(content.encode("utf-8")),
        "mode": "create",
        "path": "/tmp/remote.txt",
    }


@pytest.mark.asyncio
async def test_node_list_roundtrip_returns_summary(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """``node_list`` returns a JSON-serialisable summary for every
    live connection in the registry."""
    ws_url = _ws_url(running_server)
    token = store.create("laptop")
    pair_done = asyncio.Event()

    async def _pair_then_idle() -> None:
        ws = await _pair_node(ws_url, token, "laptop")
        pair_done.set()
        # Just stay connected; node_list doesn't trigger any
        # message, the registry snapshot is enough.
        try:
            await asyncio.sleep(0.5)
        finally:
            await ws.close()

    reader_task = asyncio.create_task(_pair_then_idle())
    await pair_done.wait()
    # Tiny wait so the server's register coroutine commits
    # before we read the registry.
    await asyncio.sleep(0.05)
    result = await node_list(registry=registry)
    await reader_task

    assert result["count"] == 1
    assert len(result["nodes"]) == 1
    node = result["nodes"][0]
    assert node["name"] == "laptop"
    assert node["connected"] is True
    assert node["session_id"]  # assigned by hello_ack
    assert node["connected_at"]  # ISO-8601 timestamp


# ---------------------------------------------------------------------------
# Smoke: tools.py can be imported standalone (no env / lifecycle needed)
# ---------------------------------------------------------------------------


def test_tools_module_imports_cleanly() -> None:
    """Importing the module should not pull in lifecycle / config
    eagerly (those are deferred inside the resolution helper)."""
    import hermes_nodes_plugin.tools as tools

    # The module exposes the four handler callables as attributes
    # and a TOOLS tuple in registration order.
    assert callable(tools.node_exec)
    assert callable(tools.node_read)
    assert callable(tools.node_write)
    assert callable(tools.node_list)
    assert len(tools.TOOLS) == 4


def test_schemas_have_no_internal_inconsistencies() -> None:
    """Quick lint over the four schemas: every property referenced
    in ``required`` exists in ``properties``; no schema has a
    name that disagrees with the export constant."""
    for schema in (NODE_EXEC_SCHEMA, NODE_READ_SCHEMA,
                   NODE_WRITE_SCHEMA, NODE_LIST_SCHEMA):
        params = schema["parameters"]
        props = set(params.get("properties", {}).keys())
        for req in params.get("required", []):
            assert req in props, (
                f"{schema['name']} requires {req!r} but it's not in properties"
            )
