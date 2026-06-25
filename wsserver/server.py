"""WSS server for paired hermes-node connections.

Pair with ``hermes-node`` Go binary (``blaspat/hermes-node``).
``../hermes-node/PROTOCOL.md`` §1 (connection lifecycle) and §3.1-3.5
(message types ``hello`` / ``hello_ack`` / ``auth`` / ``auth_ok`` /
``auth_err``). The server is the dial-ee — nodes are outbound-only
and connect to ``/ws/nodes`` on this FastAPI app.

Subpackage structure
-------------------

* ``wsserver.server``   — FastAPI app factory, handshake, HTTP dispatch.
* ``wsserver.handlers`` — inbound message routing and waiter completion.

Security choices (matching REQUIREMENTS NFR-1.x and the Go-side
conventions in PROTOCOL.md §7):

  * Hello version check: the server compares ``MAJOR`` components
    only (per PROTOCOL §5 "a MAJOR bump is the only thing that can
    break compatibility"). A major mismatch → ``hello_err`` + close 4002.
  * Out-of-order messages close with 4003 ("Message out of order")
    per PROTOCOL §1 failure modes.
  * Auth failure closes with 4001 ("Auth failed") per PROTOCOL
    §3.5. The server sends ``auth_err`` *before* closing so the
    client gets a structured reason.
  * Token comparison is delegated to :meth:`TokenStore.validate`,
    which uses ``hmac.compare_digest`` (NFR-1.1).

WebSocket close codes are taken from PROTOCOL §4 verbatim.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..config import NodeServerConfig
from ..errors import TokenStoreError
from ..ratelimit import _RateLimiter
from ..registry import NodeConnection, NodeRegistry
from ..tokens import TokenStore, token_store_from_config
from .handlers import route_inbound

logger = logging.getLogger(__name__)

# Close codes from PROTOCOL.md §4.
CLOSE_AUTH_FAILED = 4001
CLOSE_PROTOCOL_VERSION = 4002
CLOSE_MESSAGE_OUT_OF_ORDER = 4003
# 4004 is "Rate limit exceeded" in PROTOCOL §4. Reused for
# handshake-timeout on the rationale that a parked connection
# is itself a form of resource exhaustion.
CLOSE_RATE_LIMIT_EXCEEDED = 4004
CLOSE_HANDSHAKE_TIMEOUT = 4004

# Field caps for Pydantic models (issue #14, DoS hardening).
MAX_NODE_NAME_LEN = 64
MAX_TOKEN_LEN = 64
MAX_PROTOCOL_VERSION_LEN = 32
MAX_NODE_VERSION_LEN = 32
MAX_PLATFORM_LEN = 32
MAX_ARCH_LEN = 32
MAX_CAPABILITIES = 16
MAX_CAPABILITY_LEN = 32
MAX_TS_LEN = 32

# Protocol version — accept any minor at the same major (PROTOCOL §5).
PROTOCOL_MAJOR = 0


# ---------------------------------------------------------------------------
# Message envelope (Pydantic models for validated inbound messages)
# ---------------------------------------------------------------------------


class _HelloMessage(BaseModel):
    """``hello`` from the node (PROTOCOL §3.1).

    Field caps (issue #14): every string field has a ``max_length``
    so a 1MB payload fails Pydantic validation before the server
    allocates the full string. ``extra="forbid"`` rejects unknown
    fields with a 4xx-shaped error instead of a 500.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=r"^hello$")
    protocol_version: str = Field(max_length=MAX_PROTOCOL_VERSION_LEN)
    ts: str | None = Field(default=None, max_length=MAX_TS_LEN)
    node_name: str = Field(max_length=MAX_NODE_NAME_LEN, min_length=1)
    node_version: str | None = Field(default=None, max_length=MAX_NODE_VERSION_LEN)
    platform: str | None = Field(default=None, max_length=MAX_PLATFORM_LEN)
    arch: str | None = Field(default=None, max_length=MAX_ARCH_LEN)
    capabilities: list[str] | None = None

    @field_validator("node_name", mode="before")
    @classmethod
    def _strip_node_name(cls, value: Any) -> Any:
        """Reject whitespace-only node_name (issue #21)."""
        if isinstance(value, str) and not value.strip():
            raise ValueError("node_name must not be whitespace-only")
        return value

    @field_validator("capabilities", mode="before")
    @classmethod
    def _cap_capabilities(cls, value: Any) -> Any:
        """Bound the ``capabilities`` list to a sane size."""
        if value is None or not isinstance(value, list):
            return value
        if len(value) > MAX_CAPABILITIES:
            raise ValueError(
                f"capabilities must be a list of at most {MAX_CAPABILITIES} items, "
                f"got {len(value)}"
            )
        for i, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(
                    f"capabilities[{i}] must be a string, got {type(item).__name__}"
                )
            if len(item) > MAX_CAPABILITY_LEN:
                raise ValueError(
                    f"capabilities[{i}] exceeds max length {MAX_CAPABILITY_LEN}"
                )
        return value


class _AuthMessage(BaseModel):
    """``auth`` from the node (PROTOCOL §3.4).

    Field caps (issue #14): ``node_name`` and ``token`` capped at 64
    chars; ``extra="forbid"`` rejects any other fields.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=r"^auth$")
    node_name: str = Field(max_length=MAX_NODE_NAME_LEN)
    token: str = Field(max_length=MAX_TOKEN_LEN)
    ts: str | None = Field(default=None, max_length=MAX_TS_LEN)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_hello_ack(
    protocol_version: str, session_id: str
) -> dict[str, Any]:
    return {
        "type": "hello_ack",
        "protocol_version": protocol_version,
        "session_id": session_id,
        "server_time": _now_rfc3339_ms(),
    }


def _build_hello_err(
    reason: str, code: int, server_max_version: str
) -> dict[str, Any]:
    return {
        "type": "hello_err",
        "reason": reason,
        "code": code,
        "server_max_version": server_max_version,
    }


def _build_auth_ok(session_id: str) -> dict[str, Any]:
    return {"type": "auth_ok", "session_id": session_id}


def _build_auth_err(reason: str, code: int) -> dict[str, Any]:
    return {"type": "auth_err", "reason": reason, "code": code}


def _build_rate_limit_err(
    *, node_name: str, limit_per_second: int
) -> dict[str, Any]:
    """Structured ``rate_limit`` error frame (FR-2.6)."""
    return {
        "type": "rate_limit",
        "reason": "rate_limit_exceeded",
        "code": CLOSE_RATE_LIMIT_EXCEEDED,
        "node_name": node_name,
        "limit_per_second": limit_per_second,
    }


def _now_rfc3339_ms() -> str:
    """UTC RFC 3339 timestamp with millisecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _server_max_version() -> str:
    """The protocol version string the server advertises in ``hello_ack``/``hello_err``."""
    return f"{PROTOCOL_MAJOR}.1.0"


def _major_compatible(declared: str) -> bool:
    """True iff ``declared`` shares our major version (PROTOCOL §5)."""
    try:
        major_str = declared.split(".", 1)[0]
        return int(major_str) == PROTOCOL_MAJOR
    except (ValueError, IndexError):
        return False


async def _send_json_safe(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send JSON, swallowing client-gone errors."""
    try:
        await websocket.send_json(payload)
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.debug(
            "send_json after client disconnect (%s); payload dropped", exc
        )


async def _safe_close(websocket: WebSocket, code: int) -> None:
    """Close the WebSocket with ``code``, ignoring "already closed" errors."""
    try:
        await websocket.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


# ---------------------------------------------------------------------------
# Request body models for internal HTTP endpoints.
# Defined at module level to avoid the `from __future__ import annotations`
# + inline-BaseModel interaction that causes FastAPI/Pydantic to treat body
# params as query params → silent 422 (see skill §2.7.0).
# ---------------------------------------------------------------------------


class _ExecRequest(BaseModel):
    command: str
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout_ms: int | None = None


class _ReadRequest(BaseModel):
    path: str


class _WriteRequest(BaseModel):
    path: str
    content: str
    mode: str = "overwrite"


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    token_store: TokenStore | None = None,
    registry: NodeRegistry | None = None,
    config: NodeServerConfig | None = None,
    rate_limiter: _RateLimiter | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build a FastAPI app for the WSS node server.

    Args:
        token_store: Token store used to validate the ``auth`` message.
            If ``None``, the app factory calls
            :func:`token_store_from_config` against ``config`` (or a
            default :class:`NodeServerConfig`). Tests inject a stub.
        registry: Connection registry. Defaults to a fresh
            :class:`NodeRegistry` per app.
        config: Server config. Consulted when ``token_store`` is
            not provided; seeds the rate limiter cap.
        rate_limiter: Per-node sliding-window rate limiter (FR-2.6).
            Defaults to a fresh :class:`_RateLimiter` constructed
            with ``config.rate_limit_per_node``.
        clock: Monotonic-clock callable threaded into the default
            :class:`_RateLimiter` so integration tests can drive
            the rate-limit window from a fake clock.

    Returns:
        A :class:`fastapi.FastAPI` with the ``/ws/nodes`` WebSocket
        route registered. The app is not started; the caller (CLI,
        plugin lifecycle, or test) runs it with uvicorn or
        ``httpx.ASGITransport``.
    """
    if config is None:
        config = NodeServerConfig()
    if registry is None:
        registry = NodeRegistry()
    if token_store is None:
        token_store = token_store_from_config(config)
    if rate_limiter is None:
        rate_limiter = _RateLimiter(
            max_calls=config.rate_limit_per_node,
            clock=clock,
        )

    app = FastAPI(title="hermes-node WSS server")
    app.state.token_store = token_store
    app.state.registry = registry
    app.state.config = config
    app.state.rate_limiter = rate_limiter

    @app.websocket("/ws/nodes")
    async def ws_nodes(websocket: WebSocket) -> None:
        await websocket.accept()
        session_id = str(uuid.uuid4())
        client = websocket.scope.get("client")
        remote_addr = client[0] if client else ""

        # -- 1. hello --------------------------------------------------------
        try:
            raw = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=config.handshake_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "WSS hello timeout (%.1fs) from %r; closing 4004",
                config.handshake_timeout_seconds,
                remote_addr,
            )
            await _send_json_safe(
                websocket,
                _build_hello_err(
                    reason="handshake_timeout",
                    code=CLOSE_HANDSHAKE_TIMEOUT,
                    server_max_version=_server_max_version(),
                ),
            )
            await _safe_close(websocket, CLOSE_HANDSHAKE_TIMEOUT)
            return
        except Exception:
            await _safe_close(websocket, CLOSE_MESSAGE_OUT_OF_ORDER)
            return

        try:
            hello = _HelloMessage.model_validate(raw)
        except ValidationError:
            msg_type = raw.get("type") if isinstance(raw, dict) else None
            if msg_type != "hello":
                await _send_json_safe(
                    websocket,
                    _build_hello_err(
                        reason="expected_hello",
                        code=CLOSE_MESSAGE_OUT_OF_ORDER,
                        server_max_version=_server_max_version(),
                    ),
                )
            await _safe_close(websocket, CLOSE_MESSAGE_OUT_OF_ORDER)
            return

        if not _major_compatible(hello.protocol_version):
            await _send_json_safe(
                websocket,
                _build_hello_err(
                    reason="unsupported_protocol_version",
                    code=CLOSE_PROTOCOL_VERSION,
                    server_max_version=_server_max_version(),
                ),
            )
            await _safe_close(websocket, CLOSE_PROTOCOL_VERSION)
            return

        await _send_json_safe(
            websocket,
            _build_hello_ack(hello.protocol_version, session_id),
        )

        # -- 2. auth ---------------------------------------------------------
        try:
            raw = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=config.handshake_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "WSS auth timeout (%.1fs) from %r (node_name=%r); closing 4004",
                config.handshake_timeout_seconds,
                remote_addr,
                hello.node_name,
            )
            await _send_json_safe(
                websocket,
                _build_auth_err(
                    reason="handshake_timeout",
                    code=CLOSE_HANDSHAKE_TIMEOUT,
                ),
            )
            await _safe_close(websocket, CLOSE_HANDSHAKE_TIMEOUT)
            return
        except Exception:
            await _safe_close(websocket, CLOSE_MESSAGE_OUT_OF_ORDER)
            return

        try:
            auth = _AuthMessage.model_validate(raw)
        except ValidationError:
            logger.warning(
                "auth message validation failed from %r (node_name=%r)",
                remote_addr,
                raw.get("node_name") if isinstance(raw, dict) else "?",
            )
            await _safe_close(websocket, CLOSE_MESSAGE_OUT_OF_ORDER)
            return

        # Validate: presented node_name must match hello.
        if auth.node_name != hello.node_name:
            await _send_json_safe(
                websocket,
                _build_auth_err(reason="unknown_node", code=CLOSE_AUTH_FAILED),
            )
            await _safe_close(websocket, CLOSE_AUTH_FAILED)
            return

        is_valid = False
        try:
            is_valid = token_store.validate(auth.node_name, auth.token)
        except TokenStoreError as exc:
            logging.getLogger(__name__).error(
                "token store error during auth: %s", exc
            )
            await _send_json_safe(
                websocket,
                _build_auth_err(reason="invalid_token", code=CLOSE_AUTH_FAILED),
            )
            await _safe_close(websocket, CLOSE_AUTH_FAILED)
            return

        if not is_valid:
            await _send_json_safe(
                websocket,
                _build_auth_err(reason="invalid_token", code=CLOSE_AUTH_FAILED),
            )
            await _safe_close(websocket, CLOSE_AUTH_FAILED)
            return

        # -- 3. register and hold the connection ----------------------------
        conn = NodeConnection(
            name=auth.node_name,
            websocket=websocket,
            session_id=session_id,
            remote_addr=remote_addr,
        )
        previous = await registry.register(conn)
        if previous is not None and previous.websocket is not websocket:
            await _safe_close(previous.websocket, 1000)  # WS_1000_NORMAL_CLOSURE

        await _send_json_safe(websocket, _build_auth_ok(session_id))

        # -- 4. hold open; route inbound messages -------------------------
        try:
            while True:
                raw = await websocket.receive_json()
                # Heartbeat: any inbound message counts.
                await registry.touch_heartbeat(auth.node_name)
                # Respond to ping.
                if raw.get("type") == "ping":
                    await _send_json_safe(
                        websocket,
                        {
                            "type": "pong",
                            "ts": _now_rfc3339_ms(),
                            "echo_ts": raw.get("ts", ""),
                        },
                    )
                # FR-2.6 rate-limit check.
                if not rate_limiter.check(auth.node_name):
                    logger.warning(
                        "rate limit exceeded for node %r; closing 4004",
                        auth.node_name,
                    )
                    await _send_json_safe(
                        websocket,
                        _build_rate_limit_err(
                            node_name=auth.node_name,
                            limit_per_second=rate_limiter.max_calls,
                        ),
                    )
                    await _safe_close(websocket, CLOSE_RATE_LIMIT_EXCEEDED)
                    return
                # Route to the waiter (if any).
                await route_inbound(registry, auth.node_name, raw)
        except WebSocketDisconnect:
            pass
        finally:
            await registry.unregister(
                auth.node_name, expected_session_id=session_id
            )

    # -- Status endpoint ---------------------------------------------------
    @app.get("/nodes")
    async def nodes_status() -> dict[str, Any]:
        """List all currently-connected nodes with their metadata.

        GET /nodes  →  list of connected node info
        """
        connected = await registry.list_connected()
        return {
            "nodes": [
                {
                    "name": c.name,
                    "connected_at": c.connected_at.isoformat(),
                    "last_heartbeat": (
                        c.last_heartbeat.isoformat() if c.last_heartbeat else None
                    ),
                    "session_id": c.session_id,
                    "remote_addr": c.remote_addr,
                    "state": "connected",
                }
                for c in connected
            ]
        }

    @app.get("/nodes/status")
    async def server_status() -> dict[str, Any]:
        """Simple server liveness + count check (used by hermes node status CLI)."""
        connected = await registry.list_connected()
        return {
            "server": "ok",
            "port": config.port,
            "connected_count": len(connected),
        }

    # -- Internal exec endpoint --------------------------------------------
    @app.post("/nodes/{node_name}/exec")
    async def nodes_exec(node_name: str, body: _ExecRequest) -> dict[str, Any]:
        conn = await registry.get(node_name)
        if conn is None:
            return {
                "status": "error",
                "code": 404,
                "reason": f"node {node_name!r} is not connected",
            }

        request_id = str(uuid.uuid4())
        timeout_ms = body.timeout_ms or 30_000
        try:
            future = await registry.register_waiter(node_name, request_id)
        except Exception as e:
            return {
                "status": "error",
                "code": 500,
                "reason": f"failed to register waiter: {e}",
            }

        exec_payload: dict[str, Any] = {
            "type": "exec",
            "id": request_id,
            "command": body.command,
        }
        if body.cwd:
            exec_payload["cwd"] = body.cwd
        if body.env:
            exec_payload["env"] = body.env

        try:
            await conn.websocket.send_json(exec_payload)
        except Exception as e:
            await registry.unregister_waiter(node_name, request_id)
            return {
                "status": "error",
                "code": 500,
                "reason": f"failed to send exec: {e}",
            }

        try:
            result = await asyncio.wait_for(future, timeout=timeout_ms / 1000.0)
            return {"status": "ok", "exec_result": result}
        except asyncio.TimeoutError:
            await registry.unregister_waiter(node_name, request_id)
            return {
                "status": "error",
                "code": 408,
                "reason": f"exec timed out after {timeout_ms}ms",
            }
        except Exception as e:
            return {"status": "error", "code": 500, "reason": str(e)}

    # -- Internal read endpoint -------------------------------------------
    @app.post("/nodes/{node_name}/read")
    async def nodes_read(node_name: str, body: _ReadRequest) -> dict[str, Any]:
        conn = await registry.get(node_name)
        if conn is None:
            return {
                "status": "error",
                "code": 404,
                "reason": f"node {node_name!r} is not connected",
            }

        request_id = str(uuid.uuid4())
        timeout_ms = 30_000
        try:
            future = await registry.register_waiter(node_name, request_id)
        except Exception as e:
            return {
                "status": "error",
                "code": 500,
                "reason": f"failed to register waiter: {e}",
            }

        try:
            await conn.websocket.send_json(
                {"type": "read", "id": request_id, "path": body.path}
            )
        except Exception as e:
            await registry.unregister_waiter(node_name, request_id)
            return {
                "status": "error",
                "code": 500,
                "reason": f"failed to send read: {e}",
            }

        try:
            result = await asyncio.wait_for(future, timeout=timeout_ms / 1000.0)
            return {"status": "ok", "read_result": result}
        except asyncio.TimeoutError:
            await registry.unregister_waiter(node_name, request_id)
            return {
                "status": "error",
                "code": 408,
                "reason": f"read timed out after {timeout_ms}ms",
            }
        except Exception as e:
            return {"status": "error", "code": 500, "reason": str(e)}

    # -- Internal write endpoint -------------------------------------------
    @app.post("/nodes/{node_name}/write")
    async def nodes_write(node_name: str, body: _WriteRequest) -> dict[str, Any]:
        conn = await registry.get(node_name)
        if conn is None:
            return {
                "status": "error",
                "code": 404,
                "reason": f"node {node_name!r} is not connected",
            }

        request_id = str(uuid.uuid4())
        timeout_ms = 30_000
        try:
            future = await registry.register_waiter(node_name, request_id)
        except Exception as e:
            return {
                "status": "error",
                "code": 500,
                "reason": f"failed to register waiter: {e}",
            }

        try:
            await conn.websocket.send_json(
                {
                    "type": "write",
                    "id": request_id,
                    "path": body.path,
                    "content": body.content,
                    "mode": body.mode,
                }
            )
        except Exception as e:
            await registry.unregister_waiter(node_name, request_id)
            return {
                "status": "error",
                "code": 500,
                "reason": f"failed to send write: {e}",
            }

        try:
            result = await asyncio.wait_for(future, timeout=timeout_ms / 1000.0)
            return {"status": "ok", "write_result": result}
        except asyncio.TimeoutError:
            await registry.unregister_waiter(node_name, request_id)
            return {
                "status": "error",
                "code": 408,
                "reason": f"write timed out after {timeout_ms}ms",
            }
        except Exception as e:
            return {"status": "error", "code": 500, "reason": str(e)}

    return app


# ---------------------------------------------------------------------------
# Convenience: run the server with uvicorn
# ---------------------------------------------------------------------------


def run_server(
    config: NodeServerConfig,
    *,
    token_store: TokenStore | None = None,
    registry: NodeRegistry | None = None,
) -> None:
    """Run the WSS server on ``config.host:config.port`` until interrupted.

    Production entry point. Test code should call :func:`create_app`
    and drive the ASGI app through ``httpx`` / ``websockets`` instead
    of binding a real socket.
    """
    import uvicorn

    app = create_app(
        token_store=token_store, registry=registry, config=config
    )
    ssl_kwargs: dict[str, Any] = {}
    if config.uses_tls():
        ssl_kwargs = {
            "ssl_certfile": config.tls_cert_path,
            "ssl_keyfile": config.tls_key_path,
        }
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level="info",
        **ssl_kwargs,
    )


__all__ = [
    "create_app",
    "run_server",
    "CLOSE_AUTH_FAILED",
    "CLOSE_PROTOCOL_VERSION",
    "CLOSE_MESSAGE_OUT_OF_ORDER",
    "CLOSE_RATE_LIMIT_EXCEEDED",
    "PROTOCOL_MAJOR",
]
