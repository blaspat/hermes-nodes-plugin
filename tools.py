"""Tool handlers — the code that runs when the LLM calls each tool.

Handler signature convention (per Hermes framework):
    def handler(args, **kw) -> str:
        ...

The framework passes ``args`` as a dict containing all tool arguments,
and ``**kw`` for forward-compatibility kwargs. Each handler extracts
fields from ``args`` and calls the internal implementation.

Async environment calls (WS-based I/O with the WSS server) are bridged
via ``asyncio.run()`` inside the impl functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .registry import NodeConnection, NodeRegistry


# ---------------------------------------------------------------------------
# Tool implementations (called by the wrapper handlers below)
# ---------------------------------------------------------------------------


def _node_exec_impl(
    target: str,
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_ms: int | None = None,
    registry: "NodeRegistry | None" = None,
) -> str:
    """Run ``command`` on the named node."""
    if not target:
        return json.dumps({"error": "node_exec: target must be a non-empty string"})
    if not command:
        return json.dumps({"error": "node_exec: command must be a non-empty string"})

    try:
        from .environment import (
            DEFAULT_EXEC_TIMEOUT_SECONDS,
            NodeEnvironment,
        )

        timeout_s = (
            float(timeout_ms) / 1000.0
            if timeout_ms is not None
            else DEFAULT_EXEC_TIMEOUT_SECONDS
        )
        env_obj = NodeEnvironment(
            target, registry=_resolve_registry(registry), timeout=timeout_s
        )
        result = asyncio.run(env_obj.execute(command, cwd=cwd or "", env=env))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"node_exec failed: {e}"})


def _node_read_impl(
    target: str,
    path: str,
    *,
    timeout_ms: int | None = None,
    registry: "NodeRegistry | None" = None,
) -> str:
    """Read a file from the named node."""
    if not target:
        return json.dumps({"error": "node_read: target must be a non-empty string"})
    if not path:
        return json.dumps({"error": "node_read: path must be a non-empty string"})

    try:
        from .environment import (
            DEFAULT_EXEC_TIMEOUT_SECONDS,
            NodeEnvironment,
        )

        timeout_s = (
            float(timeout_ms) / 1000.0
            if timeout_ms is not None
            else DEFAULT_EXEC_TIMEOUT_SECONDS
        )
        env_obj = NodeEnvironment(
            target, registry=_resolve_registry(registry), timeout=timeout_s
        )
        result = asyncio.run(env_obj.read(path, timeout=timeout_s))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"node_read failed: {e}"})


def _node_write_impl(
    target: str,
    path: str,
    content: str,
    *,
    mode: str = "overwrite",
    timeout_ms: int | None = None,
    registry: "NodeRegistry | None" = None,
) -> str:
    """Write text to a file on the named node."""
    if not target:
        return json.dumps({"error": "node_write: target must be a non-empty string"})
    if not path:
        return json.dumps({"error": "node_write: path must be a non-empty string"})

    try:
        from .environment import (
            DEFAULT_EXEC_TIMEOUT_SECONDS,
            MAX_FILE_BYTES,
            NodeEnvironment,
        )

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_FILE_BYTES:
            return json.dumps({
                "error": (
                    f"node_write: content is {len(content_bytes)} bytes, exceeds "
                    f"MAX_FILE_BYTES ({MAX_FILE_BYTES}); chunk or truncate first"
                )
            })

        timeout_s = (
            float(timeout_ms) / 1000.0
            if timeout_ms is not None
            else DEFAULT_EXEC_TIMEOUT_SECONDS
        )
        env_obj = NodeEnvironment(
            target, registry=_resolve_registry(registry), timeout=timeout_s
        )
        result = asyncio.run(
            env_obj.write(path, content, mode=mode, timeout=timeout_s)
        )
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"node_write failed: {e}"})


def _node_list_impl(
    *,
    registry: "NodeRegistry | None" = None,
) -> str:
    """List paired nodes with their current connection state.

    Hits the HTTP /nodes/status endpoint rather than the in-process
    registry so the tool works correctly when invoked from a subagent
    process (whose _default_runner._registry is a fresh empty instance
    separate from the parent gateway's running server registry).
    """
    try:
        from .config import load_config

        cfg = load_config()
        status_url = f"http://{cfg.host}:{cfg.port}/nodes/status"
        import urllib.request

        with urllib.request.urlopen(status_url, timeout=2.0) as resp:
            data = json.loads(resp.read())
        connected_names: set[str] = set(data.get("connected_names", []))
        return json.dumps({
            "nodes": [{"name": n, "state": "connected"} for n in connected_names],
            "count": len(connected_names),
        })
    except Exception as e:
        return json.dumps({"error": f"node_list failed: {e}"})


# ---------------------------------------------------------------------------
# Hermes handler wrappers
# ---------------------------------------------------------------------------
# Framework signature: handler(args: dict, **kw) -> str
# args contains the tool-arg dict from the LLM call.
# ---------------------------------------------------------------------------


def node_exec(args: dict, **kw: Any) -> str:
    """Hermes tool handler — dispatches to _node_exec_impl."""
    return _node_exec_impl(
        target=args.get("target"),
        command=args.get("command"),
        cwd=args.get("cwd"),
        env=args.get("env"),
        timeout_ms=args.get("timeout_ms"),
        registry=args.get("registry"),
    )


def node_read(args: dict, **kw: Any) -> str:
    """Hermes tool handler — dispatches to _node_read_impl."""
    return _node_read_impl(
        target=args.get("target"),
        path=args.get("path"),
        timeout_ms=args.get("timeout_ms"),
        registry=args.get("registry"),
    )


def node_write(args: dict, **kw: Any) -> str:
    """Hermes tool handler — dispatches to _node_write_impl."""
    return _node_write_impl(
        target=args.get("target"),
        path=args.get("path"),
        content=args.get("content"),
        mode=args.get("mode", "overwrite"),
        timeout_ms=args.get("timeout_ms"),
        registry=args.get("registry"),
    )


def node_list(args: dict, **kw: Any) -> str:
    """Hermes tool handler — dispatches to _node_list_impl."""
    return _node_list_impl(registry=args.get("registry"))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_registry(override: "NodeRegistry | None") -> "NodeRegistry":
    """Return ``override`` if given, else the singleton runner's registry."""
    if override is not None:
        return override
    from .lifecycle import get_default_runner

    return get_default_runner()._registry  # type: ignore[attr-defined]


def _connection_summary(conn: "NodeConnection") -> dict[str, Any]:
    """Render a :class:`NodeConnection` as a JSON-serialisable dict."""
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


# Public symbols.
__all__ = [
    "node_exec",
    "node_read",
    "node_write",
    "node_list",
]
