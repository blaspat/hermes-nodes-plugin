"""Tool handlers — the code that runs when the LLM calls each tool."""

from __future__ import annotations

import json
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
    from .registry import NodeConnection, NodeRegistry

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
    registry: "NodeRegistry | None" = None,
    **kwargs: Any,
) -> str:
    """Run ``command`` on the named node.

    Handler rules (per Hermes plugin guide):
    1. Receive args — the parameters the LLM passed (named above)
    2. Do the work
    3. Return a JSON string — ALWAYS, even on error
    4. Accept **kwargs for forward compatibility

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
        **kwargs: Forward compatibility — Hermes may pass additional
            context in the future.

    Returns:
        JSON string: ``{"output": str, "returncode": int}`` on success,
        ``{"error": str}`` on failure.
    """
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
        result = await env_obj.execute(command, cwd=cwd or "", env=env)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"node_exec failed: {e}"})


async def node_read(
    target: str,
    path: str,
    *,
    timeout_ms: int | None = None,
    registry: "NodeRegistry | None" = None,
    **kwargs: Any,
) -> str:
    """Read a file from the named node.

    Handler rules (per Hermes plugin guide):
    1. Receive args — the parameters the LLM passed (named above)
    2. Do the work
    3. Return a JSON string — ALWAYS, even on error
    4. Accept **kwargs for forward compatibility

    Args:
        target: Node name as paired.
        path: Absolute path on the node's filesystem. Gated by
            the node's allowlist (PROTOCOL §3.8).
        timeout_ms: Optional per-call timeout in **milliseconds**.
        registry: Optional registry override (tests only).
        **kwargs: Forward compatibility.

    Returns:
        JSON string: ``{"content": str, "size_bytes": int, "truncated": bool,
        "encoding": "utf-8"}`` on success, ``{"error": str}`` on failure.
    """
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
        result = await env_obj.read(path, timeout=timeout_s)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"node_read failed: {e}"})


async def node_write(
    target: str,
    path: str,
    content: str,
    *,
    mode: str = "overwrite",
    timeout_ms: int | None = None,
    registry: "NodeRegistry | None" = None,
    **kwargs: Any,
) -> str:
    """Write text to a file on the named node.

    Handler rules (per Hermes plugin guide):
    1. Receive args — the parameters the LLM passed (named above)
    2. Do the work
    3. Return a JSON string — ALWAYS, even on error
    4. Accept **kwargs for forward compatibility

    Args:
        target: Node name as paired.
        path: Absolute path on the node's filesystem.
        content: UTF-8 text to write. Binary content needs a
            different surface (out of scope for v1).
        mode: ``"create"`` | ``"overwrite"`` (default) | ``"append"``
            per PROTOCOL §3.10.
        timeout_ms: Optional per-call timeout in **milliseconds**.
        registry: Optional registry override (tests only).
        **kwargs: Forward compatibility.

    Returns:
        JSON string: ``{"bytes_written": int, "mode": str, "path": str}``
        on success, ``{"error": str}`` on failure.
    """
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

        # Match PROTOCOL §3.9's 10 MB cap on the client side so we
        # fail fast with a clear message rather than waiting for the
        # WSS frame limit to reject the payload mid-send.
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
        result = await env_obj.write(path, content, mode=mode, timeout=timeout_s)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": f"node_write failed: {e}"})


async def node_list(
    registry: "NodeRegistry | None" = None,
    **kwargs: Any,
) -> str:
    """List paired nodes with their current connection state.

    Handler rules (per Hermes plugin guide):
    1. Receive args — the parameters the LLM passed (named above)
    2. Do the work
    3. Return a JSON string — ALWAYS, even on error
    4. Accept **kwargs for forward compatibility

    Args:
        registry: Optional registry override (tests only).
        **kwargs: Forward compatibility.

    Returns:
        JSON string: ``{"nodes": [...], "count": int}`` on success,
        ``{"error": str}`` on failure.
    """
    try:
        reg = _resolve_registry(registry)
        conns = await reg.list_connected()
        return json.dumps({
            "nodes": [_connection_summary(c) for c in conns],
            "count": len(conns),
        })
    except Exception as e:
        return json.dumps({"error": f"node_list failed: {e}"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_registry(override: "NodeRegistry | None") -> "NodeRegistry":
    """Return ``override`` if given, else the singleton runner's registry."""
    if override is not None:
        return override
    # Imported here rather than at module top to avoid the
    # ``lifecycle → config → yaml`` chain in tools-only tests.
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
