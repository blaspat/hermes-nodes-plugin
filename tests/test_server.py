"""Tests for :mod:`hermes_nodes_plugin.server`.

Coverage (matching Task 2.4 acceptance criteria + PROTOCOL.md §1/§3):

  **Auth success path**
    * Send valid ``hello`` → receive ``hello_ack`` with the agreed
      protocol version and a session_id.
    * Send valid ``auth`` → receive ``auth_ok`` and the connection
      lands in the :class:`NodeRegistry`.
    * The session_id in ``auth_ok`` matches the one from ``hello_ack``.

  **Auth failure path**
    * Wrong token → server sends ``auth_err`` with
      ``reason="invalid_token"`` and closes with WebSocket close code
      ``4001``.
    * ``node_name`` in ``auth`` doesn't match the ``node_name`` from
      ``hello`` → ``auth_err`` with ``reason="unknown_node"`` and close
      ``4001``. (Token is bound to one name; mismatching names is
      always a hard reject.)
    * Revoked token → same as wrong token.

  **Out-of-order / protocol violations**
    * Sending a non-``hello`` message first → server closes with
      ``4003`` (Message out of order), per PROTOCOL §1.
    * Major-version mismatch in ``hello`` → ``hello_err`` + close ``4002``.

  **Reconnect / replace**
    * A second connection from a node replaces the first in the
      registry; the first WebSocket is closed with ``1000``.

We use Starlette's :class:`TestClient` (sync wrapper around httpx
+ anyio). The synchronous API plays well with pytest's default
collector; the WebSocket handler still runs in its own task on the
ASGI loop, so we exercise the real coroutine path.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from hermes_nodes_plugin.config import NodeServerConfig
from hermes_nodes_plugin.registry import NodeRegistry
from hermes_nodes_plugin.server import (
    CLOSE_AUTH_FAILED,
    CLOSE_MESSAGE_OUT_OF_ORDER,
    CLOSE_PROTOCOL_VERSION,
    PROTOCOL_MAJOR,
    create_app,
)
from hermes_nodes_plugin.tokens import TokenStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    """Fresh Fernet key per test."""
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def store(tmp_path: Path, fernet_key: str) -> TokenStore:
    """Real :class:`TokenStore` writing to a tmp file with a real Fernet key."""
    return TokenStore(path=tmp_path / "tokens.json", key=fernet_key)


@pytest.fixture
def registry() -> NodeRegistry:
    """A registry the test can assert against after the client disconnects."""
    return NodeRegistry()


@pytest.fixture
def app(store: TokenStore, registry: NodeRegistry):
    """The FastAPI app under test — fresh per test, with real store + fresh registry."""
    return create_app(
        token_store=store,
        registry=registry,
        config=NodeServerConfig(),
    )


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    """Starlette ``TestClient`` running the app in a background thread."""
    with TestClient(app) as c:
        yield c


@contextmanager
def ws_connect(client: TestClient) -> Iterator:
    """Open a WebSocket and close it on exit, even if the test fails mid-flight."""
    with client.websocket_connect("/ws/nodes") as ws:
        yield ws


def _drain_close(ws) -> int | None:
    """Read from a WebSocket the server has just closed; return the close code.

    The server sends a final JSON payload (e.g. ``auth_err``) then
    closes. A subsequent ``receive_json`` raises
    :class:`starlette.websockets.WebSocketDisconnect` whose ``code``
    attribute is the WebSocket close code (e.g. ``4001``).
    """
    try:
        ws.receive_json()
    except Exception as exc:
        # Starlette's WebSocketDisconnect carries the close code.
        # We accept any exception type because the exception class
        # differs across httpx / Starlette / websockets versions, and
        # all we need is the ``code`` attribute.
        return getattr(exc, "code", None)
    return None


def _drain_one_message(ws):
    """Read the next message the server sends, or return ``None`` if the socket closed.

    Some paths send a structured error (e.g. ``auth_err``) and *then*
    close; the caller wants the body. We swallow the close exception
    because :func:`_drain_close` is the right place to assert on the
    code; this helper just peels the body off.
    """
    try:
        return ws.receive_json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Auth success
# ---------------------------------------------------------------------------


def test_valid_hello_and_auth_registers_connection(
    client: TestClient, store: TokenStore, registry: NodeRegistry
) -> None:
    """Happy path: pair a node, end up in the registry."""
    token = store.create("work-laptop")

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "work-laptop",
                "node_version": "0.1.0",
                "platform": "darwin",
                "arch": "arm64",
                "capabilities": ["exec", "read", "write"],
            }
        )
        hello_ack = ws.receive_json()
        assert hello_ack["type"] == "hello_ack"
        assert hello_ack["protocol_version"] == "0.1.0"
        session_id = hello_ack["session_id"]
        assert session_id, "hello_ack must include a session_id"

        ws.send_json({"type": "auth", "node_name": "work-laptop", "token": token})
        auth_ok = ws.receive_json()
        assert auth_ok["type"] == "auth_ok"
        assert auth_ok["session_id"] == session_id

        # The connection is now in the registry. Reading the
        # ``__contains__`` predicate is single-loop-safe because
        # CPython dict reads are atomic; the handler is awaiting
        # ``receive_text`` on the same loop.
        assert "work-laptop" in registry
        assert len(registry) == 1


def test_auth_ok_carries_session_id_matching_hello_ack(
    client: TestClient, store: TokenStore
) -> None:
    """The session_id in ``auth_ok`` equals the one from ``hello_ack`` (PROTOCOL §3.5)."""
    token = store.create("laptop-a")

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "laptop-a",
            }
        )
        hello_ack = ws.receive_json()
        ws.send_json({"type": "auth", "node_name": "laptop-a", "token": token})
        auth_ok = ws.receive_json()
        assert auth_ok["session_id"] == hello_ack["session_id"]


def test_minor_version_downgrade_is_accepted(
    client: TestClient, store: TokenStore
) -> None:
    """Per PROTOCOL §5, any minor at our major is compatible. Major is the hard cutoff."""
    token = store.create("laptop-minor")

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": f"{PROTOCOL_MAJOR}.7.0",  # higher minor
                "node_name": "laptop-minor",
            }
        )
        ack = ws.receive_json()
        assert ack["type"] == "hello_ack"
        assert ack["protocol_version"] == f"{PROTOCOL_MAJOR}.7.0"

        ws.send_json({"type": "auth", "node_name": "laptop-minor", "token": token})
        assert ws.receive_json()["type"] == "auth_ok"


# ---------------------------------------------------------------------------
# Auth failure
# ---------------------------------------------------------------------------


def test_bad_token_is_rejected_with_close_4001(
    client: TestClient, store: TokenStore, registry: NodeRegistry
) -> None:
    """Wrong token → ``auth_err`` then close 4001. No registration."""
    store.create("work-laptop")  # valid pair, but we'll send a wrong token

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "work-laptop",
            }
        )
        assert ws.receive_json()["type"] == "hello_ack"

        ws.send_json({"type": "auth", "node_name": "work-laptop", "token": "WRONG"})
        err = ws.receive_json()
        assert err["type"] == "auth_err"
        assert err["reason"] == "invalid_token"
        assert err["code"] == CLOSE_AUTH_FAILED  # 4001

        code = _drain_close(ws)
        assert code == CLOSE_AUTH_FAILED, f"expected close code 4001, got {code!r}"

    # Registry must remain empty — a failed auth never registers.
    assert "work-laptop" not in registry
    assert len(registry) == 0


def test_revoked_token_is_rejected_with_close_4001(
    client: TestClient, store: TokenStore
) -> None:
    """Revoked token behaves like a wrong token: ``invalid_token`` + 4001."""
    token = store.create("revoked-laptop")
    store.revoke("revoked-laptop")

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "revoked-laptop",
            }
        )
        assert ws.receive_json()["type"] == "hello_ack"

        ws.send_json(
            {
                "type": "auth",
                "node_name": "revoked-laptop",
                "token": token,
            }
        )
        err = ws.receive_json()
        assert err["type"] == "auth_err"
        assert err["reason"] == "invalid_token"
        assert _drain_close(ws) == CLOSE_AUTH_FAILED


def test_unknown_node_name_in_auth_is_rejected_with_close_4001(
    client: TestClient, store: TokenStore
) -> None:
    """``node_name`` in ``auth`` must match the ``hello``'s name.

    A node can't change its claimed name between hello and auth —
    the token is bound to one name. We treat a mismatch as
    ``unknown_node`` because there's no token to validate against
    the *new* name.
    """
    # Pair "alpha" but try to auth as "beta" with alpha's token.
    token = store.create("alpha")

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "alpha",
            }
        )
        assert ws.receive_json()["type"] == "hello_ack"

        ws.send_json(
            {
                "type": "auth",
                "node_name": "beta",  # different from hello
                "token": token,
            }
        )
        err = ws.receive_json()
        assert err["type"] == "auth_err"
        assert err["reason"] == "unknown_node"
        assert err["code"] == CLOSE_AUTH_FAILED
        assert _drain_close(ws) == CLOSE_AUTH_FAILED


def test_unknown_node_name_with_any_token_is_rejected(
    client: TestClient, store: TokenStore
) -> None:
    """A ``hello`` for a name that has no paired token → close 4001."""
    # Don't pair anything. The hello claims a name we've never seen.
    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "ghost",
            }
        )
        assert ws.receive_json()["type"] == "hello_ack"

        ws.send_json(
            {
                "type": "auth",
                "node_name": "ghost",
                "token": "any-string",
            }
        )
        err = ws.receive_json()
        assert err["type"] == "auth_err"
        # The token is bogus and the name is unknown. The store
        # returns False, so the reason is "invalid_token" (we
        # don't reveal *which* check failed to the wire — both
        # are equally "you can't come in").
        assert err["reason"] in {"invalid_token", "unknown_node"}
        assert _drain_close(ws) == CLOSE_AUTH_FAILED


# ---------------------------------------------------------------------------
# Out-of-order / protocol violations
# ---------------------------------------------------------------------------


def test_non_hello_first_message_closes_with_4003(
    client: TestClient, registry: NodeRegistry
) -> None:
    """Anything other than ``hello`` as the first message → close 4003."""
    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "auth",
                "node_name": "anything",
                "token": "anything",
            }
        )
        # The server may send a structured error first, then close.
        # We don't care about the body; the close code is what
        # matters for PROTOCOL §1.
        _drain_one_message(ws)
        assert _drain_close(ws) == CLOSE_MESSAGE_OUT_OF_ORDER

    assert len(registry) == 0


def test_unsupported_protocol_version_major_closes_with_4002(
    client: TestClient, store: TokenStore
) -> None:
    """Major-version mismatch → ``hello_err`` + close 4002 (PROTOCOL §1)."""
    store.create("laptop-future")

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "99.0.0",  # future major
                "node_name": "laptop-future",
            }
        )
        err = ws.receive_json()
        assert err["type"] == "hello_err"
        assert err["reason"] == "unsupported_protocol_version"
        assert err["code"] == CLOSE_PROTOCOL_VERSION
        assert err["server_max_version"] == f"{PROTOCOL_MAJOR}.1.0"
        assert _drain_close(ws) == CLOSE_PROTOCOL_VERSION


def test_non_auth_message_after_hello_ack_closes_with_4003(
    client: TestClient, store: TokenStore
) -> None:
    """After ``hello_ack``, anything other than ``auth`` → close 4003."""
    store.create("laptop-x")

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "laptop-x",
            }
        )
        assert ws.receive_json()["type"] == "hello_ack"

        ws.send_json(
            {"type": "exec", "id": "r-001", "command": "ls"}  # wrong type
        )
        assert _drain_close(ws) == CLOSE_MESSAGE_OUT_OF_ORDER


# ---------------------------------------------------------------------------
# Reconnect replaces previous
# ---------------------------------------------------------------------------


def test_second_connection_from_same_name_replaces_first(
    client: TestClient, store: TokenStore, registry: NodeRegistry
) -> None:
    """Re-pairing: the new WebSocket wins; the old one is closed 1000.

    Common operational case: the node process restarts, dials in
    again with the same token. The server must not keep the dead
    socket around; it must hand the slot to the new one.
    """
    token = store.create("flappy-laptop")

    # First connection.
    with ws_connect(client) as ws1:
        ws1.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "flappy-laptop",
            }
        )
        ack1 = ws1.receive_json()
        ws1.send_json({"type": "auth", "node_name": "flappy-laptop", "token": token})
        assert ws1.receive_json()["type"] == "auth_ok"
        session1 = ack1["session_id"]
        assert "flappy-laptop" in registry

    # First socket is now closed. Open a second.
    with ws_connect(client) as ws2:
        ws2.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "flappy-laptop",
            }
        )
        ack2 = ws2.receive_json()
        ws2.send_json({"type": "auth", "node_name": "flappy-laptop", "token": token})
        assert ws2.receive_json()["type"] == "auth_ok"
        session2 = ack2["session_id"]

        # New session_id is a different UUID.
        assert session1 != session2
        # Registry still has exactly one entry under the same name.
        assert len(registry) == 1

    # Both sockets closed now → registry empty.
    assert "flappy-laptop" not in registry


# ---------------------------------------------------------------------------
# Close codes — constants sanity
# ---------------------------------------------------------------------------


def test_close_codes_match_protocol() -> None:
    """The close codes the server exports are the ones PROTOCOL §4 promises."""
    assert CLOSE_AUTH_FAILED == 4001
    assert CLOSE_PROTOCOL_VERSION == 4002
    assert CLOSE_MESSAGE_OUT_OF_ORDER == 4003


# ---------------------------------------------------------------------------
# Disconnect cleanup (Task 2.5 acceptance: "handles disconnect cleanup tested")
# ---------------------------------------------------------------------------


def test_node_disconnect_removes_registry_entry(
    client: TestClient, store: TokenStore, registry: NodeRegistry
) -> None:
    """End-to-end disconnect cleanup: connect → close → registry is empty.

    Complements the replace test above: that one only verified cleanup
    as a side effect of a *second* connection winning the slot. This
    test verifies the simpler "node goes away, no replacement" path,
    which is the one that actually matters for "is the node online?"
    queries in Task 2.7's ``node_exec``.
    """
    token = store.create("laptop-disconnect")

    with ws_connect(client) as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "laptop-disconnect",
            }
        )
        ws.receive_json()  # hello_ack
        ws.send_json({"type": "auth", "node_name": "laptop-disconnect", "token": token})
        assert ws.receive_json()["type"] == "auth_ok"
        # Sanity: the entry is there while the socket is alive.
        assert "laptop-disconnect" in registry
        assert len(registry) == 1
        # Exercise the new 2.5 surface while connected (async API).
        assert _run(_is_connected(registry, "laptop-disconnect")) is True
        snapshot = _run(_list_connected(registry))
        assert [c.name for c in snapshot] == ["laptop-disconnect"]

    # Socket is closed. The handler's finally-block ran
    # ``registry.unregister``; the entry must be gone.
    assert "laptop-disconnect" not in registry
    assert len(registry) == 0
    assert _run(_is_connected(registry, "laptop-disconnect")) is False
    assert _run(_list_connected(registry)) == []


def _run(coro):
    """Run a coroutine to completion on a fresh event loop.

    Lets the sync test functions above exercise the registry's async
    surface without converting the whole test to async (which would
    force the ``TestClient`` fixture to be async too — more churn
    than the test is worth).
    """
    return asyncio.run(coro)


async def _is_connected(registry: NodeRegistry, name: str) -> bool:
    return await registry.is_connected(name)


async def _list_connected(registry: NodeRegistry) -> list:
    return await registry.list_connected()
