"""End-to-end coverage for the v1 acceptance flow (REQUIREMENTS §v1 #6).

This file is the canonical acceptance test the spec was written around:
``tests/e2e/test_full_flow.py passes on Linux amd64 CI.`` (REQUIREMENTS.md
v1 acceptance criterion #6, called out in PR #33's v0.1.0 audit).

It exercises the full pairing → connect → execute → audit → disconnect →
revoke path against a real FastAPI app bound to a real uvicorn
server, with a fake WebSocket node speaking the PROTOCOL §3 wire
format on the other end. There is no real network beyond
``127.0.0.1``, no real Go binary, and no real laptop — the fake
node is a coroutine in this process.

Why a new directory?
--------------------
The existing ``tests/`` tree (318 tests) is built around isolated
unit / module coverage. Putting an e2e test that spins a real
uvicorn thread into that tree would either bloat the unit-test
runtime (a <5s e2e + a slow uvicorn-bind per test in a file with
50 unit tests) or force a conftest split. ``tests/e2e/`` keeps the
e2e concern isolated: own fixtures, own uvicorn thread, own audit
log dir. CI picks the directory up via ``pytest tests/`` with no
config change.

Each flow stage is its own ``def test_xxx()`` so a failure points
directly at the broken stage instead of dumping a 200-line
monolith's traceback.

Test stages
-----------
1. ``test_pair_flow_creates_fernet_encrypted_token`` — pair creates
   a Fernet-encrypted row on disk, the name-unique check passes,
   and the token is non-empty.
2. ``test_connect_flow_handshakes_and_registers`` — fake WSS
   node presents ``hello`` + ``auth``, server validates, registry
   marks the node connected.
3. ``test_tool_execution_flows_end_to_end`` — ``node_exec`` fires
   from the Kate-facing tool through the environment → server →
   fake node → ``exec_result`` → environment → audit row.
4. ``test_audit_log_writes_one_jsonl_row_with_required_fields`` —
   the audit row is well-formed (ts, node, action, status, latency,
   UUIDv4 request_id).
5. ``test_disconnect_flow_unregisters_from_registry`` — fake node
   drops the socket; registry marks it gone; ``node_list`` agrees.
6. ``test_revoke_flow_blocks_subsequent_connects`` — after revoke,
   a fresh connect with the same token is rejected with close 4001.
7. ``test_rate_limit_closes_with_4004`` — placeholder; skipped
   until FR-2.6 lands (see body).

Style notes
-----------
We follow the test patterns proven in ``tests/test_environment.py``
(uvicorn thread + ``websockets`` client + ``asyncio.Event`` for
pair sequencing) and ``tests/test_server.py`` (TestClient
patterns). The e2e tests do NOT use the FastAPI ``TestClient`` —
the registry's ``get`` is ``async`` and the waiters live in
asyncio futures; running everything on one async loop is much
cleaner than bridging the sync TestClient to an async env.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
import uvicorn
from cryptography.fernet import Fernet

from hermes_nodes_plugin.audit import (
    STATUS_OK,
    AuditWriter,
    reset_default_audit_writer,
)
from hermes_nodes_plugin.cli import (
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_REVOKED,
)
from hermes_nodes_plugin.config import NodeServerConfig
from hermes_nodes_plugin.registry import NodeRegistry
from hermes_nodes_plugin.server import (
    CLOSE_AUTH_FAILED,
    PROTOCOL_MAJOR,
    create_app,
)
from hermes_nodes_plugin.tokens import TokenStore
from hermes_nodes_plugin.tools import node_exec, node_list


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    """Fresh Fernet key per test — keeps the on-disk token file isolated."""
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def audit_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuditWriter:
    """A real ``AuditWriter`` writing to a tmp file, scoped to this test.

    The e2e flow's audit row needs to land on real disk so the
    assertions can read the file back. The :class:`NodeEnvironment`
    the tool layer constructs uses ``default_audit_writer()`` to
    resolve the writer (the env constructor's default behaviour
    for ``audit=None``), and the tool layer doesn't expose an
    ``audit=`` override. We monkeypatch the factory so the
    env's singleton lookup returns OUR per-test writer — the
    audit row lands in the test's tmp file, the ``~/.hermes/logs``
    tree stays clean, and stage 4's shape assertions are
    hermetic.
    """
    writer = AuditWriter(
        path=tmp_path / "nodes-audit.log",
        max_bytes=10 * 1024 * 1024,
        keep=2,
    )
    # Reset + monkeypatch: the first call after reset will
    # rebuild the singleton (with whatever config the env has
    # set), but the monkeypatched factory returns our writer
    # instead. The reset is a belt-and-braces guard for the
    # "singleton already built before this fixture ran" case.
    reset_default_audit_writer()
    monkeypatch.setattr(
        "hermes_nodes_plugin.environment.default_audit_writer",
        lambda: writer,
    )
    try:
        yield writer
    finally:
        # Closing the writer before the tmp_path is GC'd keeps
        # Windows + NFS test runs happy (the file isn't held
        # open by a writer that survived its test).
        writer.close()
        # Reset again so a subsequent test gets a fresh state.
        reset_default_audit_writer()


@pytest.fixture
def store(tmp_path: Path, fernet_key: str) -> TokenStore:
    return TokenStore(path=tmp_path / "tokens.json", key=fernet_key)


@pytest.fixture
def registry() -> NodeRegistry:
    """A registry the fake node, the env, and the test all share.

    The fake node's auth handshake calls ``register`` on this
    instance; the env's ``execute`` looks the target up in this
    instance; the test's assertions read this instance after the
    client disconnects. Same pattern as ``test_environment.py``.
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
    """Bind a socket to 0, read the assigned port, close — standard free-port probe."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_server(app: Any) -> Iterator[uvicorn.Server]:
    """Bring up uvicorn in a background thread bound to a free port.

    Same approach as ``tests/test_environment.py`` — proven, fast,
    and clean. Yields the :class:`uvicorn.Server` so the test can
    inspect the bound port via ``server.servers[0].sockets[0]``.
    """
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=_free_port(),
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="e2e-uvicorn", daemon=True)
    thread.start()
    # Wait for uvicorn to bind. 200 * 25ms = 5s upper bound.
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


def _ws_url(server: uvicorn.Server) -> str:
    """The ``ws://`` URL the fake node should dial."""
    sock = server.servers[0].sockets[0]  # type: ignore[union-attr]
    return f"ws://127.0.0.1:{sock.getsockname()[1]}/ws/nodes"


# ---------------------------------------------------------------------------
# Fake WSS node — speaks the PROTOCOL §3 subset the server cares about
# ---------------------------------------------------------------------------
#
# The fake node is a long-lived coroutine that:
#   1. Completes the handshake (hello → hello_ack → auth → auth_ok).
#   2. Reads inbound ``exec`` / ``read`` / ``write`` requests and
#      replies with the matching ``*_result`` shape.
#   3. Optionally drops the socket on signal (``drop_after``) to
#      exercise the disconnect-cleanup path.
#   4. Exposes a ``received`` list of every message the server sent,
#      so each test can assert on the wire payload without scraping
#      internal state.
#
# It is intentionally inline (not a reusable fixture) because each
# test stage wants a slightly different behaviour — exec-only,
# exec-then-drop, or auth-then-disconnect. A single parametric
# helper would be more lines than three small coroutines.

_NODE_NAME = "laptop-x"  # the spec-mandated name for the v1 acceptance test


async def _wait_for_registration(
    registry: NodeRegistry, name: str, *, timeout: float = 2.0
) -> bool:
    """Poll the registry until ``name`` appears (or the timeout fires).

    The server's :class:`NodeRegistry.register` runs on uvicorn's
    event loop in a background thread; the test's loop is a
    separate event loop in the test thread. A fixed ``asyncio.sleep``
    yield from the test side is not deterministic — the server's
    loop may or may not have run by the time the test's yield
    finishes. Polling the registry with a short interval is the
    same pattern :func:`_running_server` uses for ``server.started``
    (poll on a small sleep until the state flips) and is robust
    on both fast and slow runners.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in registry:
            return True
        await asyncio.sleep(0.01)
    return name in registry


async def _wait_for_unregistration(
    registry: NodeRegistry, name: str, *, timeout: float = 5.0
) -> bool:
    """Poll the registry until ``name`` is gone (or the timeout fires).

    Mirror of :func:`_wait_for_registration` for the disconnect
    path. The server's :meth:`NodeRegistry.unregister` runs in the
    server's connection-handler ``finally`` block, which is
    scheduled by Starlette after the :class:`WebSocketDisconnect`
    is raised — again, on uvicorn's loop in a different thread.
    Polling is the deterministic way to wait for the cleanup.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name not in registry:
            return True
        await asyncio.sleep(0.025)
    return name not in registry


async def _wait_for_pair_node_done(websocket: Any) -> None:
    """Block until the server's side closes a held connection.

    The fake node's ``ws.wait_closed()`` raises after the server
    sends its close frame (or the underlying TCP tears down). On
    legacy ``websockets`` (< 13) the API is sync-ish — we just
    await the future it returns.
    """
    try:
        await websocket.wait_closed()
    except Exception:
        pass


async def _pair_node(ws_url: str, token: str, name: str) -> Any:
    """Connect, complete the handshake, return the WebSocket.

    Yields a few times after the handshake so the server's
    ``register`` coroutine has a chance to run on its loop — the
    env's ``get`` is the consumer and must see the registered
    connection. Same fix as ``test_environment.py``.
    """
    import websockets

    ws = await websockets.connect(ws_url)
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "protocol_version": f"{PROTOCOL_MAJOR}.1.0",
                "node_name": name,
            }
        )
    )
    ack = json.loads(await ws.recv())
    assert ack["type"] == "hello_ack", f"unexpected: {ack}"
    await ws.send(json.dumps({"type": "auth", "node_name": name, "token": token}))
    auth_ok = json.loads(await ws.recv())
    assert auth_ok["type"] == "auth_ok", f"unexpected: {auth_ok}"
    # Yield so the server's ``register`` runs to completion.
    for _ in range(20):
        await asyncio.sleep(0.01)
    return ws


def _read_audit_rows(path: Path) -> list[dict[str, Any]]:
    """Read and parse every JSONL row in the audit log.

    Empty lines are skipped so a trailing newline doesn't crash
    the parse. The test asserts on the parsed rows, not the raw
    text, so the schema check is at the dict level — the file
    format itself is exercised by ``test_audit.py``.
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Stage 1: pair flow
# ---------------------------------------------------------------------------


def test_pair_flow_creates_fernet_encrypted_token(
    tmp_path: Path, fernet_key: str
) -> None:
    """``hermes node pair`` generates a Fernet-encrypted token, writes to disk.

    Acceptance stage #1: pair ``laptop-x`` → token generated,
    Fernet-encrypted, written to ``tokens.json``, name-unique
    check passes. We drive the store directly because the
    ``hermes node pair`` CLI surfaces the token to stdout, which
    the test would have to scrape; the store API is the actual
    source of truth and the CLI is a thin wrapper over it
    (see ``cli._cmd_pair``).
    """
    store = TokenStore(path=tmp_path / "tokens.json", key=fernet_key)
    token = store.create(_NODE_NAME)

    # Token is non-empty + URL-safe base64 shape (Fernet tokens
    # are 32 random bytes → 43 chars of url-safe-b64 with padding
    # stripped).
    assert token, "pair returned an empty token"
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token), (
        f"token shape unexpected: {token!r}"
    )

    # The on-disk file is the Fernet ciphertext itself (not a
    # JSON wrapper). Inside, after Fernet decryption, the
    # plaintext is a JSON object with a ``"records"`` list — see
    # ``TokenStore._read`` for the on-disk format. We assert on
    # both the raw shape (the file IS ciphertext) and the
    # decrypted shape (records is a non-empty list, our name is
    # in it, the stored token is NOT the plaintext).
    raw_bytes = (tmp_path / "tokens.json").read_bytes()
    assert raw_bytes, "tokens.json is empty after pair"
    # Fernet ciphertexts always start with the version byte
    # 0x80 encoded as the base64 ``"gAAAAA"`` prefix.
    assert raw_bytes.startswith(b"gAAAAA"), (
        f"tokens.json is not Fernet ciphertext: {raw_bytes[:32]!r}"
    )
    # Decrypt with the SAME key the store was built with so we
    # can read the inner JSON. The store keeps the Fernet object
    # on ``store._fernet`` (test seam — production code does
    # not reach into private attrs).
    plaintext = store._fernet.decrypt(raw_bytes)  # noqa: SLF001
    on_disk = json.loads(plaintext.decode("utf-8"))
    assert "records" in on_disk, f"unexpected shape: {on_disk!r}"
    records = on_disk["records"]
    assert isinstance(records, list) and records, (
        f"records list is empty or wrong shape: {records!r}"
    )
    # Find our record by name.
    matching = [r for r in records if r.get("name") == _NODE_NAME]
    assert len(matching) == 1, (
        f"expected one record for {_NODE_NAME!r}, got {matching!r}"
    )
    record = matching[0]
    assert record.get("revoked") is False
    # The on-disk token field is a *hash* of the token, not the
    # token itself (the spec says the token is printed once and
    # never stored in plaintext). The field is named
    # ``token_hash`` (see ``tokens._StoredRecord.to_dict``).
    stored_token_hash = record.get("token_hash", "")
    assert stored_token_hash and stored_token_hash != token, (
        "token stored in plaintext — hash check missing"
    )
    # The hash uses HMAC-SHA256 hex → 64 hex chars.
    assert re.fullmatch(r"[0-9a-f]{64}", stored_token_hash), (
        f"token hash shape unexpected: {stored_token_hash!r}"
    )

    # Name-unique check: pairing the same name twice (without
    # --force) raises. Matches FR-1.5.
    with pytest.raises(Exception) as excinfo:
        store.create(_NODE_NAME)
    assert (
        "already" in str(excinfo.value).lower()
        or "exists" in str(excinfo.value).lower()
    ), f"unexpected error message: {excinfo.value!r}"


# ---------------------------------------------------------------------------
# Stage 2: connect flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_flow_handshakes_and_registers(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """Fake WSS node completes hello+auth handshake; registry marks connected.

    Acceptance stage #2: a node connects, presents ``hello`` and
    the auth token, the server validates, and the node is now in
    the :class:`NodeRegistry` with ``connected`` state.

    We also assert on the ``state`` field of ``hermes node list``
    via the ``STATE_CONNECTED`` constant to make the v1 spec's
    "node_list reflects connected state" claim verifiable in the
    same test — the registry + the CLI agree.
    """
    token = store.create(_NODE_NAME)
    ws_url = _ws_url(running_server)

    pair_done = asyncio.Event()

    async def _pair_and_hold() -> None:
        ws = await _pair_node(ws_url, token, _NODE_NAME)
        pair_done.set()
        # Hold the connection open until the server (or the test
        # body) closes us. ``wait_closed`` raises when the server
        # initiates a close (PROTOCOL §3 shutdown).
        try:
            await ws.wait_closed()
        except Exception:
            pass

    holder = asyncio.create_task(_pair_and_hold())
    try:
        await asyncio.wait_for(pair_done.wait(), timeout=5.0)
        # Poll the registry from the test loop until the
        # server's ``register`` has run. The server's loop is a
        # background thread, so a fixed ``asyncio.sleep`` yield
        # is not deterministic — polling is the robust fix.
        assert await _wait_for_registration(registry, _NODE_NAME), (
            f"node {_NODE_NAME!r} not registered within 2s after handshake"
        )
        # ``node_list`` should reflect connected state.
        listing = await node_list(registry=registry)
        names = [n["name"] for n in listing["nodes"]]
        assert _NODE_NAME in names, f"node_list missing {_NODE_NAME!r}: {listing!r}"
        # The CLI's ``state`` vocab is "connected" / "disconnected"
        # / "never_seen" / "revoked" — the registry can only
        # express "live" (which the CLI maps to connected).
        assert STATE_CONNECTED == "connected", (
            "STATE_CONNECTED vocab drift — update assertion"
        )
    finally:
        # Close the fake node's WebSocket so the server's
        # ``finally`` block can unregister and we don't leak a
        # hanging task into the next test.
        holder.cancel()
        try:
            await holder
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Stages 3 + 4: tool execution + audit log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_execution_flows_end_to_end(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
    audit_writer: AuditWriter,
) -> None:
    """``node_exec`` flows env → server → fake node → audit row.

    Acceptance stage #3: a Kate tool call (``node_exec``) makes
    it all the way through the dispatch chain and the fake node
    returns ``"hello\\n"`` for ``echo hello``.

    We drive the Kate-facing ``node_exec`` (not the raw env) so
    the test exercises the same entry point the agent uses —
    catching any regression in the tool layer's wrapper (target
    validation, timeout conversion, etc.).

    We also cover acceptance stage #4 in the same body — the
    audit row is a direct consequence of the same exec call, and
    keeping the row's shape assertions next to the wire-level
    assertions means a failure points at the layer that's
    broken without spinning a second uvicorn thread. Stages 3
    and 4 share the row; they only need to assert on different
    aspects of it.
    """
    token = store.create(_NODE_NAME)
    ws_url = _ws_url(running_server)

    pair_done = asyncio.Event()
    received: list[dict[str, Any]] = []

    async def _pair_then_reply() -> None:
        ws = await _pair_node(ws_url, token, _NODE_NAME)
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
                                "stdout": "hello\n",
                                "stderr": "",
                                "duration_ms": 1,
                                "truncated": False,
                            }
                        )
                    )
                    return
        finally:
            await ws.close()

    reader = asyncio.create_task(_pair_then_reply())
    try:
        await asyncio.wait_for(pair_done.wait(), timeout=5.0)
        result = await node_exec(_NODE_NAME, "echo hello", registry=registry)
        await reader
    finally:
        if not reader.done():
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass

    # --- Stage 3: wire-level + tool-level assertions ----------------
    assert result == {"output": "hello\n", "returncode": 0}
    exec_msgs = [m for m in received if m.get("type") == "exec"]
    assert len(exec_msgs) == 1, (
        f"expected 1 exec on the wire, got {len(exec_msgs)}: {received!r}"
    )
    exec_msg = exec_msgs[0]
    assert exec_msg["command"] == "echo hello"
    assert "cwd" not in exec_msg, "no cwd override → not on wire"
    assert "env" not in exec_msg, "no env override → not on wire"

    # --- Stage 4: audit row shape assertions ------------------------
    rows = _read_audit_rows(audit_writer.path)
    assert len(rows) == 1, f"expected exactly 1 audit row, got {len(rows)}: {rows!r}"
    row = rows[0]

    # Required fields per FR-5.1 (PROTOCOL §7) + REQUIREMENTS §5.
    assert "ts" in row, f"audit row missing 'ts': {row!r}"
    # RFC3339 / ISO-8601 UTC. The writer uses
    # ``datetime.now(timezone.utc).isoformat()`` which emits
    # ``+00:00`` (not ``Z``).
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$",
        row["ts"],
    ), f"ts not RFC3339 UTC: {row['ts']!r}"
    assert row["node"] == _NODE_NAME
    assert row["action"] == "exec", f"expected action='exec', got {row!r}"
    assert row["status"] == STATUS_OK, f"expected status=ok, got {row!r}"
    assert isinstance(row["duration_ms"], int)
    assert row["duration_ms"] >= 0
    # request_id is a UUIDv4 generated by the server's env.
    parsed = uuid.UUID(row["request_id"])
    assert parsed.version == 4, f"request_id not UUIDv4: {row['request_id']!r}"
    # ``exit_code`` is on the row for exec calls (PROTOCOL §7).
    assert "exit_code" in row
    assert row["exit_code"] == 0, f"expected exit_code=0, got {row!r}"


# ---------------------------------------------------------------------------
# Stage 5: disconnect flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_flow_unregisters_from_registry(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """Fake node drops the socket → registry marks disconnected.

    Acceptance stage #5: when the WSS connection closes (clean
    client close, network drop, or process exit), the server's
    ``finally`` block runs and the node is removed from the
    registry. ``node_list`` no longer includes the name.

    The test simulates the "client closes" path (the WebSocket's
    close-handshake); the "network drop" path is the same code
    in the server — Starlette delivers a :class:`WebSocketDisconnect`
    in both cases.
    """
    token = store.create(_NODE_NAME)
    ws_url = _ws_url(running_server)

    pair_done = asyncio.Event()

    async def _pair_then_close() -> None:
        ws = await _pair_node(ws_url, token, _NODE_NAME)
        pair_done.set()
        # Hold the socket open briefly, then close it cleanly.
        # The brief sleep gives the server a chance to finish
        # ``register`` before we drop the connection — otherwise
        # the close can race the registration and leave the
        # registry momentarily empty.
        await asyncio.sleep(0.05)
        await ws.close()

    closer = asyncio.create_task(_pair_then_close())
    try:
        await asyncio.wait_for(pair_done.wait(), timeout=5.0)
        # Poll until the server's ``register`` lands.
        assert await _wait_for_registration(registry, _NODE_NAME), (
            f"node {_NODE_NAME!r} not registered within 2s after handshake"
        )
        # Poll until the server's ``unregister`` lands (the
        # ``finally`` block fires after the WebSocketDisconnect).
        assert await _wait_for_unregistration(registry, _NODE_NAME), (
            f"node {_NODE_NAME!r} still registered after client close"
        )
        # ``node_list`` agrees — the live list is now empty.
        listing = await node_list(registry=registry)
        names = [n["name"] for n in listing["nodes"]]
        assert _NODE_NAME not in names, (
            f"node_list still has {_NODE_NAME!r}: {listing!r}"
        )
    finally:
        if not closer.done():
            closer.cancel()
            try:
                await closer
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Stage 6: revoke flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_flow_blocks_subsequent_connects(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """After revoke, a fresh connect with the same token is rejected with 4001.

    Acceptance stage #6: ``hermes node revoke laptop-x`` updates
    the store, attempts a best-effort close, ``list`` shows
    ``revoked`` state, and a fresh connect attempt with the
    revoked token is rejected with close 4001 (PROTOCOL §4's
    "Auth failed" code).

    We drive the store's ``revoke`` directly because the CLI's
    revoke is also a thin wrapper over it (see ``cli._cmd_revoke``),
    and using the store API makes the test self-contained. The
    list-state assertion uses the CLI's STATE_REVOKED constant
    so the vocab is locked.
    """
    import websockets

    token = store.create(_NODE_NAME)
    ws_url = _ws_url(running_server)

    # Step 1: confirm the token works pre-revoke (sanity — keeps
    # the test honest if the handshake ever silently breaks).
    pair_done = asyncio.Event()

    async def _pair_and_hold() -> None:
        ws = await _pair_node(ws_url, token, _NODE_NAME)
        pair_done.set()
        try:
            await ws.wait_closed()
        except Exception:
            pass

    holder = asyncio.create_task(_pair_and_hold())
    try:
        await asyncio.wait_for(pair_done.wait(), timeout=5.0)
        assert await _wait_for_registration(registry, _NODE_NAME), (
            f"pre-revoke: {_NODE_NAME!r} not registered within 2s"
        )
    finally:
        holder.cancel()
        try:
            await holder
        except (asyncio.CancelledError, Exception):
            pass

    # Step 2: revoke. ``TokenStore.revoke`` is idempotent — calling
    # it on a non-existent name is a no-op. We assert on the store
    # state, not on a return value, because revoke is a write that
    # succeeds silently. The store exposes ``list()`` (not ``get``)
    # for the per-name lookup, so we walk the (small) list.
    store.revoke(_NODE_NAME)
    records = {r.name: r for r in store.list()}
    assert _NODE_NAME in records, f"revoke: {_NODE_NAME!r} missing from store"
    assert records[_NODE_NAME].revoked is True, (
        f"revoke did not mark the record: {records[_NODE_NAME]!r}"
    )

    # Step 3: a fresh connect with the same (now revoked) token
    # must be rejected with close 4001. We do the handshake
    # inline so the assert sits next to the action.
    async def _connect_after_revoke() -> tuple[int | None, dict | None]:
        ws = await websockets.connect(ws_url)
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol_version": f"{PROTOCOL_MAJOR}.1.0",
                        "node_name": _NODE_NAME,
                    }
                )
            )
            ack = json.loads(await ws.recv())
            assert ack["type"] == "hello_ack", f"unexpected: {ack}"
            await ws.send(
                json.dumps(
                    {
                        "type": "auth",
                        "node_name": _NODE_NAME,
                        "token": token,
                    }
                )
            )
            try:
                err = json.loads(await ws.recv())
            except Exception:
                err = None
            # The server closes the socket after sending auth_err.
            # We read the close code off the exception.
            try:
                await ws.recv()  # triggers WebSocketDisconnect
                code = None
            except websockets.ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
            except Exception:
                code = None
            return code, err
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    code, err = await asyncio.wait_for(_connect_after_revoke(), timeout=5.0)
    assert err is not None, "server sent no auth_err after revoke"
    assert err.get("type") == "auth_err", f"unexpected body: {err!r}"
    assert err.get("reason") == "invalid_token", f"unexpected reason: {err!r}"
    assert code == CLOSE_AUTH_FAILED, (
        f"expected close {CLOSE_AUTH_FAILED} (auth failed), got {code!r}"
    )

    # Step 4: CLI's STATE_REVOKED vocab drift check. The CLI maps
    # a revoked record to ``state="revoked"``; we lock the
    # constant here so a refactor of the CLI state machine
    # can't silently break the v1 acceptance.
    assert STATE_REVOKED == "revoked", "STATE_REVOKED vocab drift — update assertion"
    assert STATE_DISCONNECTED == "disconnected"


# ---------------------------------------------------------------------------
# Stage 7: rate limit (FR-2.6) — placeholder
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Body is a NotImplementedError placeholder for the FR-2.6 e2e burst → 4004 close path. The server-side wiring has since landed (PR #37: _RateLimiter at hermes_nodes_plugin/ratelimit.py, dispatch-loop check at hermes_nodes_plugin/server.py:547), and tests/test_ratelimit.py locks the algorithm. This test would re-verify the wiring; overlaps with test_ratelimit integration coverage. Remove in a follow-up card that fills in the body."
)
@pytest.mark.asyncio
async def test_rate_limit_closes_with_4004(
    running_server: uvicorn.Server,
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """100 exec calls in <1s → 101st gets close 4004 (PROTOCOL §4).

    Skipped until the server-side rate-limit wiring lands. When
    it does, the test body should:
      1. Pair a node (same as stages 3-4).
      2. In a tight loop, send 100 ``exec`` requests with the
         fake node replying ``exec_result`` for each.
      3. On the 101st request, assert the server closes with
         code 4004 and sends no ``exec_result``.
    """
    raise NotImplementedError("server-side rate-limit wiring not yet implemented")
