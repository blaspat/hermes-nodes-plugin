"""Kate-facing tools for paired remote nodes (Task 2.8).

The plugin exposes four tools to the agent:

* :func:`node_exec` — run a shell command on a paired node
* :func:`node_read` — read a file from a paired node
* :func:`node_write` — write a file on a paired node
* :func:`node_list` — list paired nodes with their connection state

Each is a thin async wrapper around :class:`NodeEnvironment` (or
:class:`NodeRegistry` for ``node_list``). The environment does the
real work — registering waiters, sending the wire payload,
awaiting the response. The tool layer's job is just to:

1. Accept a plain dict of arguments from the agent runtime (the
   schema is enforced by the registered tool definition).
2. Resolve the :class:`NodeRegistry` the tool should dispatch
   against (the singleton runner's registry, or a test override).
3. Translate :class:`NodeNotConnectedError` / :class:`NodeExecutionError`
   / :class:`NodeReadError` into structured return values the
   agent can render without crashing.

Tool registration lives in :mod:`hermes_nodes_plugin.__init__`.
This module is the implementations + schemas; the ``register``
function ties the two together.

Why per-call env construction (instead of caching)?
--------------------------------------------------

A :class:`NodeEnvironment` is cheap to build (just a target name +
a registry reference), and the per-call construction pattern keeps
the connection state inside the registry — the env is stateless
from one call to the next, so a long-lived cache would just add
GC pinning without saving real work. If a future call site needs
stateful per-target envs (e.g. cwd snapshots), this is the place
to add that cache.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

# ``environment`` and ``registry`` both import ``fastapi`` at module
# top (the registry needs ``WebSocket``; the environment needs the
# app + connection types). Importing them here would block module
# load inside the hermes runtime when the pydantic_core native
# extension can't be loaded — the very chain this plugin's
# register() refactor is breaking.

# Type-checker-only: the names below are referenced in the tool
# signatures (e.g. ``registry: NodeRegistry | None = None``). With
# ``from __future__ import annotations`` they're already strings at
# runtime, but the type-checker still wants a real definition.
# TYPE_CHECKING is False at runtime, so this import never executes.
if TYPE_CHECKING:
    from hermes_nodes_plugin.registry import NodeConnection, NodeRegistry

# The tool handlers below are async functions that the agent
# runtime invokes at *call* time, so we resolve the heavy imports
# at the top of each handler body rather than at module import.
# ``from __future__ import annotations`` keeps the type hints as
# strings, so annotations don't trigger a real import either.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def node_exec(
    target: str,
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_ms: int | None = None,
    registry: NodeRegistry | None = None,
) -> dict[str, Any]:
    """Run ``command`` on the named node.

    Args:
        target: Node name as paired (e.g. ``"work-laptop"``).
        command: Shell command. The node runs it through its
            persistent bash session.
        cwd: Optional working-directory override. Empty / ``None``
            means "use the node's persistent cwd" (PROTOCOL §3.6).
        env: Optional env-var overrides. Empty / ``None`` means
            "use the node's persistent env".
        timeout_ms: Optional per-call timeout in **milliseconds**.
            ``None`` means "use the environment default"
            (``DEFAULT_EXEC_TIMEOUT_SECONDS``).
        registry: Optional registry override. Tests pass an
            isolated registry; production callers leave it
            ``None`` and we fall back to the singleton runner's.

    Returns:
        The :meth:`NodeEnvironment.execute` return value —
        ``{"output": str, "returncode": int}``.

    Raises:
        ValueError: ``target`` or ``command`` is empty.
    """
    # Lazy imports — see module docstring. The handlers run on the
    # agent's tool-invocation thread, well after plugin load, so
    # paying the import cost here doesn't block register().
    from hermes_nodes_plugin.environment import (
        DEFAULT_EXEC_TIMEOUT_SECONDS,
        NodeEnvironment,
    )

    if not target:
        raise ValueError("node_exec: target must be a non-empty string")
    if not command:
        raise ValueError("node_exec: command must be a non-empty string")

    timeout_s = (
        float(timeout_ms) / 1000.0
        if timeout_ms is not None
        else DEFAULT_EXEC_TIMEOUT_SECONDS
    )
    env_obj = NodeEnvironment(
        target, registry=_resolve_registry(registry), timeout=timeout_s
    )
    return await env_obj.execute(command, cwd=cwd or "", env=env)


async def node_read(
    target: str,
    path: str,
    *,
    timeout_ms: int | None = None,
    registry: NodeRegistry | None = None,
) -> dict[str, Any]:
    """Read a file from the named node.

    Args:
        target: Node name as paired.
        path: Absolute path on the node's filesystem. Gated by
            the node's allowlist (PROTOCOL §3.8).
        timeout_ms: Optional per-call timeout in **milliseconds**.
        registry: Optional registry override (tests only).

    Returns:
        ``{"content": str, "size_bytes": int, "truncated": bool,
        "encoding": "utf-8"}``.

    Raises:
        ValueError: ``target`` or ``path`` is empty.
    """
    # Lazy imports — see module docstring.
    from hermes_nodes_plugin.environment import (
        DEFAULT_EXEC_TIMEOUT_SECONDS,
        NodeEnvironment,
    )

    if not target:
        raise ValueError("node_read: target must be a non-empty string")
    if not path:
        raise ValueError("node_read: path must be a non-empty string")

    timeout_s = (
        float(timeout_ms) / 1000.0
        if timeout_ms is not None
        else DEFAULT_EXEC_TIMEOUT_SECONDS
    )
    env_obj = NodeEnvironment(
        target, registry=_resolve_registry(registry), timeout=timeout_s
    )
    return await env_obj.read(path, timeout=timeout_s)


async def node_write(
    target: str,
    path: str,
    content: str,
    *,
    mode: str = "overwrite",
    timeout_ms: int | None = None,
    registry: NodeRegistry | None = None,
) -> dict[str, Any]:
    """Write text to a file on the named node.

    Args:
        target: Node name as paired.
        path: Absolute path on the node's filesystem.
        content: UTF-8 text to write. Binary content needs a
            different surface (out of scope for v1).
        mode: ``"create"`` | ``"overwrite"`` (default) | ``"append"``
            per PROTOCOL §3.10.
        timeout_ms: Optional per-call timeout in **milliseconds**.
        registry: Optional registry override (tests only).

    Returns:
        ``{"bytes_written": int, "mode": str, "path": str}``.

    Raises:
        ValueError: ``target`` or ``path`` is empty, ``mode`` is
            not one of the three allowed values, or ``content`` is
            too large (exceeds :data:`MAX_FILE_BYTES`).
    """
    # Lazy imports — see module docstring.
    from hermes_nodes_plugin.environment import (
        DEFAULT_EXEC_TIMEOUT_SECONDS,
        MAX_FILE_BYTES,
        NodeEnvironment,
    )

    if not target:
        raise ValueError("node_write: target must be a non-empty string")
    if not path:
        raise ValueError("node_write: path must be a non-empty string")
    # Match PROTOCOL §3.9's 10 MB cap on the client side so we
    # fail fast with a clear message rather than waiting for the
    # WSS frame limit to reject the payload mid-send.
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_FILE_BYTES:
        raise ValueError(
            f"node_write: content is {len(content_bytes)} bytes, exceeds "
            f"MAX_FILE_BYTES ({MAX_FILE_BYTES}); chunk or truncate first"
        )

    timeout_s = (
        float(timeout_ms) / 1000.0
        if timeout_ms is not None
        else DEFAULT_EXEC_TIMEOUT_SECONDS
    )
    env_obj = NodeEnvironment(
        target, registry=_resolve_registry(registry), timeout=timeout_s
    )
    return await env_obj.write(path, content, mode=mode, timeout=timeout_s)


async def node_list(
    registry: NodeRegistry | None = None,
) -> dict[str, Any]:
    """List paired nodes with their current connection state.

    The output is a small JSON-serialisable dict the agent can
    drop into a table or bullet list. We intentionally don't
    return full :class:`NodeConnection` objects — those carry a
    live WebSocket reference (not serialisable) and aren't what
    the agent's UI needs.

    Args:
        registry: Optional registry override (tests only).

    Returns:
        ``{"nodes": [...], "count": int}`` where each entry is
        ``{"name": str, "connected": bool, "connected_at": str,
        "last_heartbeat": str, "session_id": str, "remote_addr":
        str}``. Timestamps are ISO-8601 UTC strings.
    """
    reg = _resolve_registry(registry)
    conns = await reg.list_connected()
    return {
        "nodes": [_connection_summary(c) for c in conns],
        "count": len(conns),
    }


# ---------------------------------------------------------------------------
# Tool schemas (the agent runtime parses these into its own format)
# ---------------------------------------------------------------------------


# Common string param shape — keeps the schemas uniform.
_STRING_PARAM: dict[str, Any] = {"type": "string"}


NODE_EXEC_SCHEMA: dict[str, Any] = {
    "name": "node_exec",
    "description": (
        "Run a shell command on a paired remote node (e.g. a laptop with "
        "the hermes-nodes Go binary installed) and return its stdout/stderr "
        "and exit code. The command runs in the node's persistent shell, "
        "so `cd` and `export` between calls persist. Use `hermes node list` "
        "(or `node_list()`) to see which nodes are currently connected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                **_STRING_PARAM,
                "description": "Node name (as paired), e.g. 'work-laptop'",
            },
            "command": {
                **_STRING_PARAM,
                "description": "Shell command to run on the node",
            },
            "cwd": {
                **_STRING_PARAM,
                "description": (
                    "Optional working-directory override. Omit to use the "
                    "node's persistent cwd."
                ),
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Optional env-var overrides merged into the node's "
                    "persistent env. Omit to use the node's existing env."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Per-call timeout in milliseconds. Defaults to 60000 "
                    "(60s, matches the protocol default)."
                ),
            },
        },
        "required": ["target", "command"],
    },
}


NODE_READ_SCHEMA: dict[str, Any] = {
    "name": "node_read",
    "description": (
        "Read a UTF-8 text file from a paired remote node. Returns the "
        "file content as a string, plus `size_bytes` and a `truncated` "
        "flag (true if the node hit the 10 MB cap). The node enforces its "
        "own path allowlist; out-of-allowlist paths return "
        "`path_not_allowed`. For binary files, the bytes are decoded with "
        "errors='replace' — invalid sequences become U+FFFD."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                **_STRING_PARAM,
                "description": "Node name (as paired), e.g. 'work-laptop'",
            },
            "path": {
                **_STRING_PARAM,
                "description": "Absolute path on the node's filesystem",
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "description": "Per-call timeout in milliseconds",
            },
        },
        "required": ["target", "path"],
    },
}


NODE_WRITE_SCHEMA: dict[str, Any] = {
    "name": "node_write",
    "description": (
        "Write UTF-8 text to a file on a paired remote node. By default "
        "overwrites the file; pass `mode='append'` to add to an existing "
        "file or `mode='create'` to refuse overwriting. Content is "
        "capped at 10 MB per call (matches the protocol's file cap). The "
        "node enforces its own path allowlist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                **_STRING_PARAM,
                "description": "Node name (as paired), e.g. 'work-laptop'",
            },
            "path": {
                **_STRING_PARAM,
                "description": "Absolute path on the node's filesystem",
            },
            "content": {
                **_STRING_PARAM,
                "description": "UTF-8 text to write to the file",
            },
            "mode": {
                "type": "string",
                "enum": ["create", "overwrite", "append"],
                "description": (
                    "Write mode. 'overwrite' (default) replaces any "
                    "existing file; 'append' adds to it; 'create' refuses "
                    "to overwrite."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "description": "Per-call timeout in milliseconds",
            },
        },
        "required": ["target", "path", "content"],
    },
}


NODE_LIST_SCHEMA: dict[str, Any] = {
    "name": "node_list",
    "description": (
        "List all paired remote nodes that are currently connected to the "
        "WSS server. Each entry includes the node name, the time it "
        "connected, the last heartbeat timestamp, the session id, and the "
        "remote address. Disconnected nodes (dropped TCP, missed "
        "heartbeats, revoked tokens) are not returned by this call — "
        "they're only visible in the CLI's `hermes node list`."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# Tuple of (name, schema, handler, emoji) for the register() loop
# in __init__.py. Centralised here so adding a new tool is a
# one-line change in this file plus one import in __init__.py.
TOOLS: tuple[tuple[str, dict[str, Any], Any, str], ...] = (
    ("node_exec", NODE_EXEC_SCHEMA, node_exec, "🖥️"),
    ("node_read", NODE_READ_SCHEMA, node_read, "📄"),
    ("node_write", NODE_WRITE_SCHEMA, node_write, "✍️"),
    ("node_list", NODE_LIST_SCHEMA, node_list, "📋"),
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_registry(override: NodeRegistry | None) -> NodeRegistry:
    """Return ``override`` if given, else the singleton runner's registry.

    The default runner is the same one :func:`register` wires
    into the plugin's session lifecycle, so a tool call from
    inside a Kate session sees the connections the WSS server
    has registered. Tests pass ``override`` to use a clean
    :class:`NodeRegistry` that has no live connections, which
    keeps them hermetic.

    We resolve lazily to keep import-time side effects out of
    the module (e.g. a config-load error in
    :func:`get_default_runner` should not block ``import
    hermes_nodes_plugin.tools``).
    """
    if override is not None:
        return override
    # Imported here rather than at module top to avoid the
    # ``lifecycle → config → yaml`` chain in tools-only tests.
    from hermes_nodes_plugin.lifecycle import get_default_runner

    return get_default_runner()._registry  # type: ignore[attr-defined]


def _connection_summary(conn: NodeConnection) -> dict[str, Any]:
    """Render a :class:`NodeConnection` as a JSON-serialisable dict.

    Drops the live WebSocket (not serialisable, not useful to the
    agent's UI) and normalises the timestamps to ISO-8601 UTC.
    """
    return {
        "name": conn.name,
        "connected": True,
        "connected_at": conn.connected_at.isoformat(),
        "last_heartbeat": conn.last_heartbeat.isoformat()
        if conn.last_heartbeat is not None
        else None,
        "session_id": conn.session_id,
        "remote_addr": conn.remote_addr,
    }


# Public symbols the test module needs to reach.
__all__ = [
    "node_exec",
    "node_read",
    "node_write",
    "node_list",
    "NODE_EXEC_SCHEMA",
    "NODE_READ_SCHEMA",
    "NODE_WRITE_SCHEMA",
    "NODE_LIST_SCHEMA",
    "TOOLS",
]
