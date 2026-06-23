"""Tool handlers — the code that runs when the LLM calls each tool.

Handler signature convention (per Hermes framework):
    def handler(args, **kw) -> str:
        ...

The framework passes ``args`` as a dict containing all tool arguments,
and ``**kw`` for forward-compatibility kwargs. Each handler extracts
fields from ``args`` and calls the internal implementation.

All tools communicate with the WSS server via httpx WebSocket (local
HTTP round-trip to the configured port) rather than via an in-process registry.
This avoids the registry-synchronisation problem that arises when the
gateway spawns subagent processes: each subagent gets its own empty
``_default_runner._registry`` instance, so ``NodeEnvironment`` cannot
find any nodes. The HTTP endpoint is always authoritative.
"""

from __future__ import annotations


import json
import logging
import time

from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .registry import NodeConnection, NodeRegistry


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


def _retry_config() -> tuple[int, float]:
    """Return (max_retries, backoff_seconds) from the current config."""
    from .config import load_config

    cfg = load_config()
    return cfg.max_retries, cfg.retry_backoff_seconds


def _should_retry(status_code: int, reason: str = "") -> bool:
    """Return True if the response suggests a transient failure worth retrying.

    Retries on:
    - Server errors (5xx)
    - Node not connected (any status where reason mentions "not connected")
    """
    if status_code >= 500:
        return True
    if "not connected" in reason.lower():
        return True
    return False


def _request_with_retry(
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Make an HTTP request with exponential backoff retry.

    Retries up to ``max_retries`` times when the server is unreachable
    or returns a transient error. Between retries, sleeps
    ``backoff * 2^attempt`` seconds (capped at 30s).
    """
    import httpx

    max_retries, backoff = _retry_config()
    last_error: Exception | None = None
    last_result: dict[str, Any] | None = None

    for attempt in range(max_retries + 1):
        last_error = None
        last_result = None
        try:
            with httpx.Client(timeout=timeout) as client:
                if method == "POST":
                    resp = client.post(url, json=json_body)
                else:
                    resp = client.get(url)
                result = resp.json()
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = min(backoff * (2 ** attempt), 30.0)
                logger.warning(
                    "Request to %s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                    url, attempt + 1, max_retries + 1, e, delay,
                )
                time.sleep(delay)
                continue
            break

        # Check if the response indicates a transient error.
        reason = ""
        if isinstance(result, dict):
            reason = result.get("reason", "") or result.get("error", "")
        if _should_retry(resp.status_code, reason):
            last_result = result
            if attempt < max_retries:
                delay = min(backoff * (2 ** attempt), 30.0)
                logger.warning(
                    "Request to %s returned %d (attempt %d/%d): %s. Retrying in %.1fs...",
                    url, resp.status_code, attempt + 1, max_retries + 1, reason, delay,
                )
                time.sleep(delay)
                continue
        else:
            return result  # success or non-retryable error

    # All retries exhausted.
    if last_error:
        return {"error": f"Request failed after {max_retries + 1} attempts: {last_error}"}
    if last_result:
        return last_result
    return {"error": "Request failed: unknown error"}


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

    try:
        url = f"http://{cfg.connect_host}:{cfg.port}/nodes/{target}/exec"
        payload: dict[str, Any] = {"command": command}
        if cwd:
            payload["cwd"] = cwd
        if env:
            payload["env"] = env
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms

        result = _request_with_retry(
            "POST", url, json_body=payload, timeout=timeout_s + 5.0,
        )

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
            # timeout, node not connected, or other non-ok status
            error_msg = (
                result.get("reason")
                or result.get("error")
                or "unknown error"
            )
            return json.dumps({
                "output": error_msg,
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
        cfg = load_config()
        url = f"http://{cfg.connect_host}:{cfg.port}/nodes/{target}/read"
        result = _request_with_retry(
            "POST", url, json_body={"path": path}, timeout=timeout_s + 5.0,
        )

        if result.get("status") == "ok":
            read_result = result.get("read_result", {})
            return json.dumps({
                "content": read_result.get("content", ""),
                "size_bytes": read_result.get("size_bytes", 0),
                "truncated": read_result.get("truncated", False),
                "encoding": "utf-8",
            })
        else:
            error_msg = (
                result.get("reason")
                or result.get("error")
                or "read failed"
            )
            return json.dumps({
                "error": error_msg,
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
        cfg = load_config()
        url = f"http://{cfg.connect_host}:{cfg.port}/nodes/{target}/write"
        result = _request_with_retry(
            "POST", url, json_body={"path": path, "content": content, "mode": mode},
            timeout=timeout_s + 5.0,
        )

        if result.get("status") == "ok":
            write_result = result.get("write_result", {})
            return json.dumps({
                "bytes_written": write_result.get("bytes_written", 0),
            })
        else:
            error_msg = (
                result.get("reason")
                or result.get("error")
                or "write failed"
            )
            return json.dumps({
                "error": error_msg,
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

    Response shape per the schema (schemas.py NODE_LIST):
        {
          "nodes": [
            {
              "name": str,
              "connected_at": str (ISO 8601),
              "last_heartbeat": str (ISO 8601) | null,
              "session_id": str,
              "remote_addr": str,
              "state": "connected"
            }
          ],
          "count": int
        }
    """
    try:
        from .config import load_config

        cfg = load_config()
        status_url = f"http://{cfg.connect_host}:{cfg.port}/nodes"
        import urllib.request

        with urllib.request.urlopen(status_url, timeout=2.0) as resp:
            data = json.loads(resp.read())
        connected: list[dict[str, Any]] = data.get("nodes", [])
        return json.dumps({
            "nodes": connected,
            "count": len(connected),
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
