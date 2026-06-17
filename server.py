"""WSS server for paired hermes-nodes connections.

Implements the server half of the auth handshake described in
``hermes-nodes/PROTOCOL.md`` §1 (connection lifecycle) and §3.1-3.5
(message types ``hello`` / ``hello_ack`` / ``auth`` / ``auth_ok`` /
``auth_err``). The server is the dial-ee — nodes are outbound-only
and connect to ``/ws/nodes`` on this FastAPI app.

Scope of this file (Task 2.4)
-----------------------------

What lives here:

  * The FastAPI app factory :func:`create_app` that wires the WebSocket
    route to a single connection handler.
  * The handshake logic: read ``hello``, send ``hello_ack`` (or
    ``hello_err`` + close 4002), read ``auth``, send ``auth_ok`` or
    ``auth_err`` + close 4001, register the connection on success.
  * Token validation via :class:`TokenStore`. The handler refuses to
    start if the Fernet key env var is unset, matching the
    "refuse to start with a clear error" requirement (FR-4.2).

What does NOT live here (later tasks):

  * Task 2.5 — heartbeat bookkeeping, ``is_connected``, ``list_connected``,
    mid-session revoke hooks.
  * Task 2.6 — wiring the server into the Hermes plugin lifecycle
    (``register(ctx)`` starts/stops it on gateway startup/shutdown).
  * Task 2.7+ — ``exec`` / ``read`` / ``write`` dispatch; this server
    currently does nothing after ``auth_ok`` except keep the socket
    open and unregister on close. That's the minimum a Task 2.4
    acceptance test needs to verify ("connect with valid token,
    assert registered").

Security choices (matching REQUIREMENTS NFR-1.x and the Go-side
conventions in PROTOCOL.md §7):

  * Hello version check: the server compares ``MAJOR`` components
    only (per PROTOCOL §5 "a MAJOR bump is the only thing that can
    break compatibility"). A major mismatch → ``hello_err`` + close 4002.
  * Out-of-order messages close with 4003 ("Message out of order")
    per PROTOCOL §1 failure modes. Specifically, anything other than
    ``hello`` arriving first, or anything other than ``auth``
    arriving second, gets a 4003 close.
  * Auth failure closes with 4001 ("Auth failed") per PROTOCOL
    §3.5. The server sends ``auth_err`` *before* closing so the
    client gets a structured reason.
  * Token comparison is delegated to :meth:`TokenStore.validate`,
    which uses ``hmac.compare_digest`` (NFR-1.1, enforced by tests
    in ``test_tokens.py``).

WebSocket close codes are taken from PROTOCOL §4 verbatim.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import NodeServerConfig
from .errors import TokenStoreError
from .ratelimit import _RateLimiter
from .registry import NodeConnection, NodeRegistry
from .tokens import TokenStore, token_store_from_config

logger = logging.getLogger(__name__)

# Close codes from PROTOCOL.md §4. Named for clarity at call sites.
CLOSE_AUTH_FAILED = 4001
CLOSE_PROTOCOL_VERSION = 4002
CLOSE_MESSAGE_OUT_OF_ORDER = 4003
# 4004 is "Rate limit exceeded" in PROTOCOL §4. The handshake-timeout
# close (issue #13) reuses this code on the rationale that a
# parked connection is itself a form of resource exhaustion; a
# FR-2.6 burst that crosses the 100-cps cap is the more direct
# trigger and produces a structured ``rate_limit`` error frame.
CLOSE_RATE_LIMIT_EXCEEDED = 4004
CLOSE_HANDSHAKE_TIMEOUT = 4004

# Field caps for Pydantic models (issue #14, DoS hardening).
# ``node_name`` and ``token`` are the spec-mandated 64-char caps. The
# 64-char bound is well above any legitimate value: tokens are 32
# bytes url-safe-base64 = 43 chars; node names like "work-laptop" are
# <20. The cap stops a 1MB-allocated-at-validation DoS payload.
MAX_NODE_NAME_LEN = 64
MAX_TOKEN_LEN = 64
# ``protocol_version`` is a semver string; 32 chars is generous
# (e.g. "0.1.0-rc.1+build.1234" is 21). ``node_version`` and the
# free-form hint fields share the same cap so the model rejects
# oversize payloads uniformly.
MAX_PROTOCOL_VERSION_LEN = 32
MAX_NODE_VERSION_LEN = 32
MAX_PLATFORM_LEN = 32
MAX_ARCH_LEN = 32
# ``capabilities`` is a list; cap the per-item length and the count
# so a 1MB-string-in-a-list payload still fails validation cheaply.
MAX_CAPABILITIES = 16
MAX_CAPABILITY_LEN = 32
MAX_TS_LEN = 32

# Protocol version. We accept any minor at the same major, per
# PROTOCOL §5. The server's own "max major" is a hard cutoff.
PROTOCOL_MAJOR = 0


# ---------------------------------------------------------------------------
# Message envelope (Pydantic models for validated inbound messages)
# ---------------------------------------------------------------------------


class _HelloMessage(BaseModel):
    """``hello`` from the node (PROTOCOL §3.1).

    Field caps (issue #14, DoS hardening): every string field has a
    ``max_length`` so a 1MB payload fails Pydantic validation
    *before* the server allocates the full string. ``extra="forbid"``
    rejects unknown fields with a 4xx-shaped error instead of a 500
    (a malformed message should not crash the handler).

    Optional fields (``node_version``, ``platform``, ``arch``,
    ``capabilities``) are documented in PROTOCOL §3.1 as hints and
    are modelled explicitly here — that way ``extra="forbid"`` only
    catches *truly* unknown fields, not the documented ones.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=r"^hello$")
    protocol_version: str = Field(max_length=MAX_PROTOCOL_VERSION_LEN)
    ts: str | None = Field(default=None, max_length=MAX_TS_LEN)
    # node_name must be non-empty after strip() per PROTOCOL §3.1
    # (issue #21: the server used to accept hello with empty /
    # whitespace-only node_name, a §3.1 shape violation). The cap
    # is 64 chars (issue #14); min_length=1 + the validator below
    # close the gap. The hello message may use either the bare
    # name or "  work-laptop  " (whitespace is operator-friendly
    # for shell-quoted input) — we strip it here.
    node_name: str = Field(max_length=MAX_NODE_NAME_LEN, min_length=1)
    # Optional hints per PROTOCOL §3.1.
    node_version: str | None = Field(default=None, max_length=MAX_NODE_VERSION_LEN)
    platform: str | None = Field(default=None, max_length=MAX_PLATFORM_LEN)
    arch: str | None = Field(default=None, max_length=MAX_ARCH_LEN)
    capabilities: list[str] | None = None

    @field_validator("node_name", mode="before")
    @classmethod
    def _strip_node_name(cls, value: Any) -> Any:
        """Reject whitespace-only node_name (issue #21).

        ``min_length=1`` catches the empty string but not ``"   "``
        — a node_name of three spaces is not "non-empty" in any
        useful sense, and PROTOCOL §3.1 says the field is required.
        Strip + re-check here so the existing string-type check
        above accepts both ``"work-laptop"`` and ``"  work-laptop  "``.
        """
        if isinstance(value, str) and not value.strip():
            raise ValueError("node_name must not be whitespace-only")
        return value

    @field_validator("capabilities", mode="before")
    @classmethod
    def _cap_capabilities(cls, value: Any) -> Any:
        """Bound the ``capabilities`` list to a sane size.

        Pydantic's per-item ``max_length`` only applies to scalar
        fields; a list of strings with each item < ``MAX_CAPABILITY_LEN``
        could still be a million-item list. We cap the count here
        and the per-item length with a small loop (fast — the list
        is short by design).
        """
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
    chars; ``extra="forbid"`` rejects any other fields. The token
    cap is the headline — a 1MB token would otherwise be allocated
    by Pydantic to validate it, the DoS surface called out in the
    issue body.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=r"^auth$")
    node_name: str = Field(max_length=MAX_NODE_NAME_LEN)
    token: str = Field(max_length=MAX_TOKEN_LEN)
    ts: str | None = Field(default=None, max_length=MAX_TS_LEN)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_hello_ack(protocol_version: str, session_id: str) -> dict[str, Any]:
    """Return the ``hello_ack`` payload the server sends after a successful hello."""
    return {
        "type": "hello_ack",
        "protocol_version": protocol_version,
        "session_id": session_id,
        "server_time": _now_rfc3339_ms(),
    }


def _build_hello_err(reason: str, code: int, server_max_version: str) -> dict[str, Any]:
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
    """Build the structured ``rate_limit`` error frame (FR-2.6).

    Sent before the close-4004 so the client surfaces a useful
    reason rather than just seeing the socket drop. The frame
    shape mirrors the other error envelopes (``type``/``reason``/
    ``code``) and adds ``node_name`` + ``limit_per_second`` so
    the node operator can correlate the drop with their config.
    """
    # See PROTOCOL.md "Server-originated frames: rate_limit" — keep the Go client in sync.
    return {
        "type": "rate_limit",
        "reason": "rate_limit_exceeded",
        "code": CLOSE_RATE_LIMIT_EXCEEDED,
        "node_name": node_name,
        "limit_per_second": limit_per_second,
    }


def _now_rfc3339_ms() -> str:
    """UTC RFC 3339 timestamp with millisecond precision.

    Matches the ``ts`` field format described in PROTOCOL §2
    ("RFC3339 timestamp, milliseconds, UTC"). The ``Z`` suffix is
    equivalent to ``+00:00`` and is what stdlib parsers prefer.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


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
            default :class:`NodeServerConfig`). Tests inject a stub
            store with a fixed mapping.
        registry: Connection registry. Defaults to a fresh
            :class:`NodeRegistry` per app. Tests inject a shared
            instance so they can assert on the registry after the
            client disconnects.
        config: Server config. Only consulted when ``token_store`` is
            not provided — used to read the Fernet key env var name
            and the token store path. Also seeds the rate limiter
            cap (``config.rate_limit_per_node``) when
            ``rate_limiter`` is not injected.
        rate_limiter: Per-node sliding-window rate limiter (FR-2.6).
            Defaults to a fresh :class:`_RateLimiter` constructed
            with ``config.rate_limit_per_node``. Tests inject a
            low-cap instance to exercise the close-on-excess path
            with only a few round-trips.
        clock: Monotonic-clock callable threaded into the default
            :class:`_RateLimiter` so the integration tests can drive
            the rate-limit window from a fake clock and avoid
            real-time flakiness on slow CI runners. Ignored when
            ``rate_limiter`` is injected.

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
        # Production path: read the Fernet key from the env var named
        # in config. Raises TokenStoreError with the
        # "regenerate + export" hint if the var is unset, which
        # matches the "refuse to start with a clear error" rule
        # (REQUIREMENTS FR-4.2). Tests always pass ``token_store``
        # explicitly so they don't need a real key.
        token_store = token_store_from_config(config)
    if rate_limiter is None:
        # FR-2.6: cap defaults to config.rate_limit_per_node (100).
        # A misconfigured ``<= 0`` value makes the limiter fail-open
        # and log a warning at construction (see ``ratelimit.py``).
        # The ``clock`` seam is plumbed from the factory parameter
        # so integration tests can drive the window with a fake
        # clock and stay deterministic on slow CI runners.
        rate_limiter = _RateLimiter(
            max_calls=config.rate_limit_per_node,
            clock=clock,
        )

    app = FastAPI(title="hermes-nodes WSS server")
    app.state.token_store = token_store
    app.state.registry = registry
    app.state.config = config
    app.state.rate_limiter = rate_limiter

    @app.websocket("/ws/nodes")
    async def ws_nodes(websocket: WebSocket) -> None:
        # We accept the WebSocket *before* reading the hello, because
        # we need the connection in scope to send hello_ack / hello_err
        # / auth_err back. PROTOCOL §1 failure modes still apply: any
        # protocol violation is followed by a close with the
        # appropriate code.
        await websocket.accept()
        session_id = str(uuid.uuid4())
        client = websocket.scope.get("client")  # (host, port) or None
        remote_addr = client[0] if client else ""

        # -- 1. hello ----------------------------------------------------
        # Issue #13: bound the wait for ``hello`` so a misbehaving
        # client cannot park the connection forever. A parked
        # connection is a trivial DoS — every open WSS holds a
        # coroutine + FD. 10s is generous for a small envelope on
        # any plausible network. On timeout we send a structured
        # ``hello_err`` (reason ``handshake_timeout``) and close
        # with 4004, the PROTOCOL §4 code reserved for resource-
        # exhaustion-style rejections ("Rate limit exceeded").
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
            # Non-JSON or no message → protocol violation. Close with
            # 4003 ("Message out of order") — the node sent nothing
            # parseable.
            await _safe_close(websocket, CLOSE_MESSAGE_OUT_OF_ORDER)
            return

        try:
            hello = _HelloMessage.model_validate(raw)
        except ValidationError:
            # Wrong type / missing required fields / wrong shape.
            # If the type was wrong, send a structured error and close.
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

        # Major-version negotiation per PROTOCOL §5.
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

        # hello_ack. We send the same protocol version the node
        # declared (matching the negotiation rule: "the lower of the
        # two" — we're at the same major, and we don't bump minor
        # from the wire; the node's value is what we both agree to).
        await _send_json_safe(
            websocket,
            _build_hello_ack(hello.protocol_version, session_id),
        )

        # -- 2. auth -----------------------------------------------------
        # Same bounded-wait pattern for the auth recv (issue #13).
        # The hello succeeded, so we know the node's name — useful
        # for the timeout log line.
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
                "auth message validation failed from %r (node_name=%r): %s",
                remote_addr, raw.get("node_name") if isinstance(raw, dict) else "?",
                ValidationError,
            )
            # Either the message wasn't type=auth, or it was missing
            # node_name / token. Per PROTOCOL §1: anything other than
            # auth after hello_ack → close 4003.
            await _safe_close(websocket, CLOSE_MESSAGE_OUT_OF_ORDER)
            return

        # Validate the token. We check three things, in order of
        # cheapness: (1) presented node_name matches the claimed
        # name from hello (avoids leaking token validity to a wrong
        # name), (2) the token is well-formed enough to look up, (3)
        # the store says the token is live for that name.
        if auth.node_name != hello.node_name:
            # The node changed its claimed name between hello and
            # auth. This is always wrong — a token is bound to one
            # name. We use the "unknown_node" reason because we have
            # no token to validate against the (different) name.
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
            # Store-level failure (key wrong, file corrupt) — the
            # auth can't be processed. Log and close with auth
            # failure so the node retries (and we don't lie about
            # internal errors on the wire).
            logging.getLogger(__name__).error("token store error during auth: %s", exc)
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

        # -- 3. register and hold the connection -------------------------
        conn = NodeConnection(
            name=auth.node_name,
            websocket=websocket,
            session_id=session_id,
            remote_addr=remote_addr,
        )
        previous = await registry.register(conn)
        # If a previous connection held this name, close it cleanly.
        # The old WebSocket's own handler will unregister itself when
        # the close propagates, which is a safe no-op (we already
        # removed the old entry above by overwriting it).
        if previous is not None and previous.websocket is not websocket:
            await _safe_close(previous.websocket, status.WS_1000_NORMAL_CLOSURE)

        await _send_json_safe(websocket, _build_auth_ok(session_id))

        # -- 4. hold the connection open until the client disconnects ----
        # Task 2.4 didn't dispatch any messages yet — the registry
        # was the artefact. Task 2.7 adds the request/response
        # dispatch: we read every inbound JSON message, touch the
        # heartbeat (PROTOCOL §6), and route response messages
        # (``exec_result`` / ``read_result`` / ``write_result`` /
        # ``error``) back to the in-flight :class:`asyncio.Future`
        # that :class:`NodeEnvironment` registered in
        # :meth:`NodeRegistry.register_waiter`. Unknown message types
        # are ignored (forward-compat: future PROTOCOL versions can
        # add types without breaking the server).
        try:
            while True:
                raw = await websocket.receive_json()
                # Heartbeat bookkeeping: any inbound message counts.
                # We await this so the registry's lock isn't held
                # longer than necessary, but it's cheap.
                await registry.touch_heartbeat(auth.node_name)
                # Respond to ping with pong echoing the ts so the
                # client's heartbeat watchdog stays quiet (PROTOCOL §6).
                if raw.get("type") == "ping":
                    await _send_json_safe(websocket, {
                        "type": "pong",
                        "ts": _now_rfc3339_ms(),
                        "echo_ts": raw.get("ts", ""),
                    })
                # FR-2.6 sliding-window rate limit, applied *after*
                # the handshake (we know ``auth.node_name``) and
                # *before* the action handler dispatches the
                # message. A burst that crosses the cap is dropped
                # with a structured ``rate_limit`` error frame and
                # close 4004. We check the limiter AFTER touching
                # the heartbeat so a healthy node's "I'm alive"
                # traffic is still counted even when a misbehaving
                # sibling is being throttled — but the rejected
                # call itself is NOT routed to the action handler.
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
                await _route_inbound(registry, auth.node_name, raw)
        except WebSocketDisconnect:
            pass
        finally:
            # Pass ``session_id`` so the old connection's finally-block
            # cannot pop the new connection's entry on the reconnect
            # path. See hermes-nodes-plugin issue #10.
            await registry.unregister(auth.node_name, expected_session_id=session_id)

    # --------------------------------------------------------------------- #
    # Status endpoint — provides ``hermes node list`` with the live set of  #
    # connected node names without the CLI needing its own registry.        #
    # Registered unconditionally so the CLI can always reach it.             #
    # --------------------------------------------------------------------- #

    @app.get("/nodes/status")
    async def nodes_status() -> dict[str, Any]:
        connected = await registry.list_connected()
        return {"connected_names": [c.name for c in connected]}

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _send_json_safe(websocket: WebSocket, payload: dict[str, Any]) -> None:
    """Send a JSON payload, swallowing client-gone errors.

    After a protocol violation the client may have already initiated
    a close of its own; trying to send the structured error would
    raise. We log and continue so the close-with-code path still runs.
    """
    try:
        await websocket.send_json(payload)
    except (WebSocketDisconnect, RuntimeError) as exc:
        logging.getLogger(__name__).debug(
            "send_json after client disconnect (%s); payload dropped", exc
        )


async def _safe_close(websocket: WebSocket, code: int) -> None:
    """Close the WebSocket with ``code``, ignoring "already closed" errors.

    Per PROTOCOL §1, the server must close with the specific code on
    each failure path. Starlette's :meth:`WebSocket.close` accepts a
    ``code`` parameter; we pass it through. A second close raises —
    catch and move on.
    """
    try:
        await websocket.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        # Already closed by either side. Nothing to do.
        pass


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


# ---------------------------------------------------------------------------
# Inbound dispatch (Task 2.7)
# ---------------------------------------------------------------------------


# Response-shaped message types that the server routes back to a
# in-flight :class:`NodeEnvironment` call. We don't validate the
# payload here — the environment layer does that, since each
# request type has its own result shape. Anything outside this set
# is ignored (PROTOCOL §3 lists ``ping``/``pong``/``event`` as
# node-originated only; an unknown type is treated as a no-op so
# future PROTOCOL versions can add messages without breaking us).
_ROUTABLE_RESULT_TYPES = frozenset(
    {"exec_result", "read_result", "write_result", "error"}
)


async def _route_inbound(registry: NodeRegistry, node_name: str, raw: Any) -> None:
    """Dispatch one inbound JSON message to a registered waiter.

    Args:
        registry: The :class:`NodeRegistry` holding both the
            connection table and the pending-waiter map.
        node_name: The authenticated node name the message came
            from. The server's connection handler passes the
            ``auth``-time name so we never trust a client-supplied
            ``node_name`` here.
        raw: The decoded JSON payload from
            :meth:`WebSocket.receive_json`. ``None`` or non-dict
            values are logged and dropped — they indicate a
            protocol violation or transport corruption, and the
            keep-alive loop will hit the next ``receive_json`` to
            surface a structured error (PROTOCOL §1).

    Behaviour:
        * ``exec_result`` / ``read_result`` / ``write_result`` /
          ``error`` with a string ``id`` field → the matching waiter
          is resolved with the message body. A ``False`` return
          from :meth:`NodeRegistry.complete_waiter` (no such
          waiter) is logged at DEBUG — the call has either timed
          out or its connection was replaced, and the result is
          discarded.
        * Unknown message types (e.g. ``pong``, future PROTOCOL
          additions) are ignored. They're still counted as heartbeat
          activity by the caller.
        * Malformed payloads (``type`` not a string, ``id``
          missing) are logged at WARNING. The connection stays
          open; one bad message shouldn't kill the session.
    """
    if not isinstance(raw, dict):
        logger.warning("dropping non-dict inbound message from %r: %r", node_name, raw)
        return

    msg_type = raw.get("type")
    if not isinstance(msg_type, str):
        logger.warning(
            "dropping inbound message with non-string type from %r: %r",
            node_name,
            raw,
        )
        return

    if msg_type not in _ROUTABLE_RESULT_TYPES:
        # Not a result we route. Could be ``pong``, an ``event``
        # notification (forward-compat), or a future PROTOCOL
        # addition. The connection loop already counted it as
        # heartbeat activity; nothing else to do.
        return

    request_id = raw.get("id")
    if not isinstance(request_id, str) or not request_id:
        logger.warning(
            "dropping %s message without string id from %r: %r",
            msg_type,
            node_name,
            raw,
        )
        return

    resolved = await registry.complete_waiter(node_name, request_id, raw)
    if not resolved:
        logger.debug(
            "no waiter for %s on %r (id=%s); call may have timed out",
            msg_type,
            node_name,
            request_id,
        )


# ---------------------------------------------------------------------------
# Convenience: start the server with uvicorn
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
    of binding a real socket. The TLS paths on ``config`` are
    honoured — when both cert and key are set, uvicorn serves
    ``wss://`` directly; otherwise the server listens on plain ``ws://``
    and is expected to sit behind a reverse proxy (per the resolved
    TLS decision in the plan).
    """
    import uvicorn  # Imported lazily so tests don't pay the cost.

    app = create_app(token_store=token_store, registry=registry, config=config)
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
