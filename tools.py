"""Tool handlers — the code that runs when the LLM calls each tool.

Handler signature convention (per Hermes framework):
    def handler(args, **kw) -> str:
        ...

The framework passes ``args`` as a dict containing all tool arguments,
and ``**kw`` for forward-compatibility kwargs. Each handler extracts
fields from ``args`` and calls the internal implementation.

All tools communicate with the WSS server via httpx WebSocket (local
HTTP round-trip to port 6969) rather than via an in-process registry.
This avoids the registry-synchronisation problem that arises when the
gateway spawns subagent processes: each subagent gets its own empty
``_default_runner._registry`` instance, so ``NodeEnvironment`` cannot
find any nodes. The HTTP endpoint is always authoritative.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .registry import NodeConnection, NodeRegistry


# ---------------------------------------------------------------------------
# Tool implementations (called by the wrapper handlers below)
# ---------------------------------------------------------------------------


def _build_wss_url() -> str:
    """Return the WSS endpoint URL for the local node server.

    Uses ``localhost`` (not 127.0.0.1) because the server's WebSocket
    upgrade handler validates the Host header and some configurations
    only accept ``localhost``.
    """
    # HTTP endpoint uses 127.0.0.1 for consistency with what the server
    # binds to; WSS endpoint needs to match what the server's WebSocket
    # handler considers a valid Host header.
    return "ws://localhost:6969/ws/nodes"


async def _ws_request(
    target: str,
    request_type: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    """Send a request over a WSS connection and return the structured result.

    Handles the full protocol handshake (hello / hello_ack, auth / auth_ok)
    and then sends ``payload`` and waits for the matching response.

    Raises on timeout, protocol errors, or if the server returns an error
    status.
    """
    import websockets

    request_id = str(uuid.uuid4())
    payload["id"] = request_id
    payload["type"] = request_type

    url = _build_wss_url()

    async def _do_ws() -> dict[str, Any]:
        async with websockets.connect(url, open_timeout=10.0) as ws:
            # -- hello (MUST be sent first; server waits up to 10s before closing)
            await ws.send(json.dumps({
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": target,
            }))
            hello_raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            hello_data = json.loads(hello_raw)
            if hello_data.get("type") != "hello_ack":
                raise RuntimeError(f"expected hello_ack, got {hello_data!r}")
            # -- auth (anonymous — token validated by server config)
            await ws.send(json.dumps({"type": "auth", "node_name": target}))
            auth_resp_raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            auth_resp = json.loads(auth_resp_raw)
            if auth_resp.get("type") != "auth_ok":
                raise RuntimeError(f"auth failed: {auth_resp!r}")
            # -- send request
            await ws.send(json.dumps(payload))
            # -- read responses until we get one with matching id
            while True:
                msg_raw = await asyncio.wait_for(
                    ws.recv(), timeout=timeout_s + 5.0
                )
                msg = json.loads(msg_raw)
                if msg.get("id") == request_id:
                    return msg

    try:
        result = await asyncio.wait_for(_do_ws(), timeout=timeout_s + 15.0)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"{request_type} on {target!r} timed out after {timeout_s}s"
        )

    if result.get("status") == "error":
        code = result.get("code", 0)
        raise RuntimeError(
            f"node returned error status {code}: {result.get('reason', result)}"
        )
    return result


def _node_exec_impl(
    target: str,
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout_ms: int | None = None,
    registry: "NodeRegistry | None" = None,
) -> str:
    """Run ``command`` on the named node.

    Uses httpx WebSocket to talk to the local WSS server directly,
    bypassing the in-process registry to avoid subagent sync issues.
    """
    if not target:
        return json.dumps({"error": "node_exec: target must be a non-empty string"})
    if not command:
        return json.dumps({"error": "node_exec: command must be a non-empty string"})

    from .config import load_config
    from .environment import DEFAULT_EXEC_TIMEOUT_SECONDS

    cfg = load_config()
    timeout_s = (
        float(timeout_ms) / 1000.0
        if timeout_ms is not None
        else DEFAULT_EXEC_TIMEOUT_SECONDS
    )

    payload: dict[str, Any] = {"command": command}
    if cwd:
        payload["cwd"] = cwd
    if env:
        payload["env"] = env

    try:
        import httpx

        cfg = load_config()
        url = f"http://{cfg.host}:{cfg.port}/nodes/{target}/exec"
        payload: dict[str, Any] = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        if env:
            payload["env"] = env
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms

        with httpx.Client(timeout=timeout_s + 5.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            result = response.json()

        # Normalise to {"output": str, "returncode": int}
        if result.get("status") == "ok":
            exec_result = result.get("exec_result", {})
            output = exec_result.get("stdout", "")
            stderr = exec_result.get("stderr", "")
            if stderr:
                output = output + "\n[stderr]\n" + stderr
            return json.dumps({
                "output": output,
                "returncode": exec_result.get("returncode", 0),
            })
        else:
            # timeout or other non-error status
            return json.dumps({
                "output": result.get("reason", ""),
                "returncode": 1,
            })
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

    from .environment import DEFAULT_EXEC_TIMEOUT_SECONDS
    from .config import load_config

    timeout_s = (
        float(timeout_ms) / 1000.0
        if timeout_ms is not None
        else DEFAULT_EXEC_TIMEOUT_SECONDS
    )

    try:
        import httpx

        cfg = load_config()
        url = f"http://{cfg.host}:{cfg.port}/nodes/{target}/read"
        with httpx.Client(timeout=timeout_s + 5.0) as client:
            response = client.post(url, json={"path": path})
            response.raise_for_status()
            result = response.json()

        if result.get("status") == "ok":
            read_result = result.get("read_result", {})
            return json.dumps({
                "content": read_result.get("content", ""),
                "size_bytes": read_result.get("size_bytes", 0),
                "truncated": read_result.get("truncated", False),
                "encoding": "utf-8",
            })
        else:
            return json.dumps({
                "error": result.get("reason", "read failed"),
                "code": result.get("code", 0),
            })
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

    from .environment import DEFAULT_EXEC_TIMEOUT_SECONDS, MAX_FILE_BYTES
    from .config import load_config

    timeout_s = (
        float(timeout_ms) / 1000.0
        if timeout_ms is not None
        else DEFAULT_EXEC_TIMEOUT_SECONDS
    )

    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_FILE_BYTES:
        return json.dumps({
            "error": (
                f"node_write: content is {len(content_bytes)} bytes, exceeds "
                f"MAX_FILE_BYTES ({MAX_FILE_BYTES}); chunk or truncate first"
            )
        })

    try:
        import httpx

        cfg = load_config()
        url = f"http://{cfg.host}:{cfg.port}/nodes/{target}/write"
        with httpx.Client(timeout=timeout_s + 5.0) as client:
            response = client.post(url, json={"path": path, "content": content, "mode": mode})
            response.raise_for_status()
            result = response.json()

        if result.get("status") == "ok":
            write_result = result.get("write_result", {})
            return json.dumps({
                "bytes_written": write_result.get("bytes_written", 0),
            })
        else:
            return json.dumps({
                "error": result.get("reason", "write failed"),
                "code": result.get("code", 0),
            })
    except Exception as e:
        return json.dumps({"error": f"node_write failed: {e}"})


def _node_list_impl(
    *,
    registry: "NodeRegistry | None" = None,
) -> str:
    """List paired nodes with their current connection state.

    Hits the HTTP /nodes/status endpoint.  This is always queried over
    localhost HTTP so it reflects the actual server state even when the
    calling process has an empty in-process registry.
    """
    try:
        from .config import load_config

        cfg = load_config()
        status_url = f"http://{cfg.host}:{cfg.port}/nodes/status"
        import urllib.request

        with urllib.request.urlopen(status_url, timeout=2.0) as resp:
            data = json.loads(resp.read())
        connected_names: list[str] = data.get("connected_names", [])
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
# Internal helpers (kept for API compatibility with existing callers)
# ---------------------------------------------------------------------------


def _resolve_registry(override: "NodeRegistry | None") -> "NodeRegistry":
    """Return ``override`` if given, else a fresh registry.

    Note: the tools now use httpx WebSocket to talk to the local server
    directly, so this function is no longer used for exec/read/write.
    It is kept for API compatibility with any external callers that
    pass an explicit registry.
    """
    if override is not None:
        return override
    from .registry import NodeRegistry

    return NodeRegistry()


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
