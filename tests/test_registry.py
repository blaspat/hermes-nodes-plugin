"""Tests for :mod:`hermes_nodes_plugin.registry`.

Coverage (matching Task 2.5 acceptance criteria):

  **Core ops**
    * ``register`` then ``get`` round-trips a connection.
    * ``register`` returns the previous connection when a slot is
      replaced (the "second connection takes over" path).
    * ``unregister`` removes a registered name and returns it;
      ``unregister`` of an unknown name is a safe no-op.
    * ``is_connected`` flips between ``False`` and ``True`` around
      ``register``/``unregister``.

  **List ops**
    * ``list_connected`` returns every registered connection in a
      fresh list (caller can mutate without affecting the registry).
    * ``names`` returns just the keys.

  **Heartbeat**
    * A freshly-registered connection has ``last_heartbeat == connected_at``
      (the handshake itself counts as the first heartbeat).
    * ``touch_heartbeat`` bumps ``last_heartbeat`` and is a no-op
      (``False``) for unknown names.
    * ``stale(older_than=...)`` returns connections whose last
      heartbeat is past the threshold and leaves the registry alone.

  **Disconnect cleanup** (the "handles disconnect cleanup" line in the plan)
    * The server's WebSocket handler calls ``registry.unregister``
      in its ``finally`` block. We exercise the same path here by
      simulating a connect → unregister cycle and asserting the
      registry is empty afterward. The end-to-end version (real
      WebSocket going away) is covered by ``test_server.py``.

  **Replace semantics**
    * Registering a second connection under the same name replaces
      the first; the caller (the server) is expected to close the
      old WebSocket — the registry returns it via the return value
      of ``register``.

We use ``asyncio.run`` in a pytest fixture so each test gets a fresh
event loop without pulling in ``pytest-asyncio``. The registry
methods are coroutines, so this is unavoidable; running the test
function itself in an event loop is simpler than declaring every
test as ``@pytest.mark.asyncio`` for a one-off file.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest

from hermes_nodes_plugin.registry import NodeConnection, NodeRegistry, _WaiterCancelled


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal stand-in for a :class:`fastapi.WebSocket`.

    The registry only treats the WebSocket as an opaque object — it
    stashes a reference, hands it back via :meth:`NodeRegistry.get`,
    and otherwise leaves it alone. A real WebSocket is impossible to
    construct in a unit test (it needs a live ASGI scope), so we use
    a trivial sentinel here.
    """

    def __init__(self, name: str = "ws") -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"_FakeWebSocket({self.name!r})"


def _make_conn(name: str, **overrides) -> NodeConnection:
    """Build a :class:`NodeConnection` with sensible defaults.

    ``websocket`` defaults to a per-name fake so tests that look at
    the connection's WebSocket can assert identity, and tests that
    don't care get a unique object automatically.
    """
    defaults: dict = {
        "name": name,
        "websocket": _FakeWebSocket(f"ws-{name}"),
        "session_id": f"sess-{name}",
        "remote_addr": f"10.0.0.{abs(hash(name)) % 256}",
    }
    defaults.update(overrides)
    return NodeConnection(**defaults)


@pytest.fixture
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Per-test event loop so coroutine tests don't leak state."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Core ops: register / get / unregister / is_connected
# ---------------------------------------------------------------------------


def test_register_then_get_roundtrips() -> None:
    """``register`` then ``get`` returns the same connection."""

    async def scenario() -> None:
        registry = NodeRegistry()
        conn = _make_conn("laptop")
        previous = await registry.register(conn)
        assert previous is None
        got = await registry.get("laptop")
        assert got is not None
        assert got is conn
        assert got.name == "laptop"
        assert isinstance(got.websocket, _FakeWebSocket)

    asyncio.run(scenario())


def test_register_replaces_existing_and_returns_previous() -> None:
    """Re-registering a name returns the old connection (server closes it)."""

    async def scenario() -> None:
        registry = NodeRegistry()
        first = _make_conn("laptop", session_id="sess-1")
        second = _make_conn("laptop", session_id="sess-2")
        assert await registry.register(first) is None
        previous = await registry.register(second)
        assert previous is first
        # And the slot now points at the new one.
        current = await registry.get("laptop")
        assert current is not None
        assert current is second
        assert current.session_id == "sess-2"

    asyncio.run(scenario())


def test_unregister_removes_and_returns() -> None:
    """``unregister`` returns the removed connection and clears the slot."""

    async def scenario() -> None:
        registry = NodeRegistry()
        conn = _make_conn("laptop")
        await registry.register(conn)
        removed = await registry.unregister("laptop")
        assert removed is conn
        assert await registry.get("laptop") is None
        assert "laptop" not in registry

    asyncio.run(scenario())


def test_unregister_unknown_is_noop() -> None:
    """Unregistering a name that was never registered returns ``None``."""

    async def scenario() -> None:
        registry = NodeRegistry()
        result = await registry.unregister("ghost")
        assert result is None
        # And the registry is still empty / well-formed.
        assert len(registry) == 0
        assert await registry.list_connected() == []

    asyncio.run(scenario())


def test_is_connected_tracks_lifecycle() -> None:
    """``is_connected`` is ``False`` → ``True`` → ``False`` across register/unregister."""

    async def scenario() -> None:
        registry = NodeRegistry()
        assert await registry.is_connected("laptop") is False
        await registry.register(_make_conn("laptop"))
        assert await registry.is_connected("laptop") is True
        await registry.unregister("laptop")
        assert await registry.is_connected("laptop") is False

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# List ops
# ---------------------------------------------------------------------------


def test_list_connected_returns_all_connections_in_insertion_order() -> None:
    """``list_connected`` is a snapshot in registration order."""

    async def scenario() -> None:
        registry = NodeRegistry()
        a = _make_conn("alpha")
        b = _make_conn("bravo")
        c = _make_conn("charlie")
        await registry.register(a)
        await registry.register(b)
        await registry.register(c)
        snapshot = await registry.list_connected()
        names = [conn.name for conn in snapshot]
        assert names == ["alpha", "bravo", "charlie"]
        assert snapshot[0] is a
        assert snapshot[1] is b
        assert snapshot[2] is c

    asyncio.run(scenario())


def test_list_connected_empty_when_no_nodes() -> None:
    """An empty registry returns an empty list, not ``None``."""

    async def scenario() -> None:
        registry = NodeRegistry()
        assert await registry.list_connected() == []
        assert await registry.names() == []

    asyncio.run(scenario())


def test_list_connected_is_fresh_snapshot() -> None:
    """The returned list can be mutated without affecting the registry."""

    async def scenario() -> None:
        registry = NodeRegistry()
        await registry.register(_make_conn("laptop"))
        snapshot = await registry.list_connected()
        snapshot.clear()
        # Registry still has the entry — snapshot was a copy.
        assert await registry.is_connected("laptop") is True
        assert len(registry) == 1

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_new_connection_has_heartbeat_equal_to_connected_at() -> None:
    """The auth handshake counts as the first heartbeat."""

    async def scenario() -> None:
        registry = NodeRegistry()
        before = datetime.now(timezone.utc)
        conn = _make_conn("laptop")
        after = datetime.now(timezone.utc)
        await registry.register(conn)
        got = await registry.get("laptop")
        assert got is not None
        # ``last_heartbeat`` is bounded by the time we built ``conn``
        # (inside ``__post_init__``). The window is small but real —
        # we just need it to fall inside [before, after].
        assert before <= got.last_heartbeat <= after
        assert got.last_heartbeat == got.connected_at

    asyncio.run(scenario())


def test_touch_heartbeat_updates_timestamp() -> None:
    """``touch_heartbeat`` advances ``last_heartbeat`` and reports success."""

    async def scenario() -> None:
        registry = NodeRegistry()
        await registry.register(_make_conn("laptop"))
        # Backdate the heartbeat so the bump is observable.
        old = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert await registry.touch_heartbeat("laptop", at=old) is True
        got = await registry.get("laptop")
        assert got is not None
        assert got.last_heartbeat == old
        # And a "now" touch brings it forward.
        newer = datetime(2026, 6, 4, tzinfo=timezone.utc)
        assert await registry.touch_heartbeat("laptop", at=newer) is True
        got2 = await registry.get("laptop")
        assert got2 is not None
        assert got2.last_heartbeat == newer

    asyncio.run(scenario())


def test_touch_heartbeat_unknown_name_returns_false() -> None:
    """Bumping a name that isn't registered is a benign no-op."""

    async def scenario() -> None:
        registry = NodeRegistry()
        assert await registry.touch_heartbeat("ghost") is False
        # The registry is still empty.
        assert await registry.list_connected() == []

    asyncio.run(scenario())


def test_stale_filters_by_threshold() -> None:
    """``stale`` returns connections whose last_heartbeat is past the cutoff."""

    async def scenario() -> None:
        registry = NodeRegistry()
        # Three connections, all registered "now" with the same
        # connected_at, then we backdate two of them.
        now = datetime.now(timezone.utc)
        fresh = _make_conn("fresh")
        stale_a = _make_conn("stale-a")
        stale_b = _make_conn("stale-b")
        await registry.register(fresh)
        await registry.register(stale_a)
        await registry.register(stale_b)
        # Backdate via touch_heartbeat's ``at=`` test seam.
        long_ago = now - timedelta(minutes=5)
        await registry.touch_heartbeat("stale-a", at=long_ago)
        await registry.touch_heartbeat("stale-b", at=long_ago)

        # Anything older than 60s per PROTOCOL §6 is dead.
        result = await registry.stale(older_than=timedelta(seconds=60))
        names = sorted(c.name for c in result)
        assert names == ["stale-a", "stale-b"]
        # And the registry is unchanged (read-only contract).
        assert len(registry) == 3
        assert await registry.is_connected("fresh") is True

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Disconnect cleanup
# ---------------------------------------------------------------------------


def test_disconnect_cleanup_removes_entry() -> None:
    """Simulate the server's finally-block: WebSocket closes → entry is gone.

    The real end-to-end version (real WebSocket going away) is
    ``test_server.py::test_websocket_disconnect_unregisters_node``.
    Here we exercise the contract the server relies on: any code
    that calls ``unregister`` on a registered name leaves the
    registry clean.
    """

    async def scenario() -> None:
        registry = NodeRegistry()
        conn = _make_conn("laptop")
        await registry.register(conn)
        # Imagine the WebSocket just closed and the server's
        # ``finally`` block ran ``registry.unregister``.
        await registry.unregister(conn.name)
        # The slot is empty, ``is_connected`` agrees, and
        # ``list_connected`` doesn't leak the dead entry.
        assert await registry.get("laptop") is None
        assert await registry.is_connected("laptop") is False
        assert await registry.list_connected() == []
        # And a follow-up ``is_connected`` after re-register works
        # — i.e. we didn't somehow poison the slot.
        replacement = _make_conn("laptop", session_id="sess-2")
        await registry.register(replacement)
        assert await registry.is_connected("laptop") is True
        reconnected = await registry.get("laptop")
        assert reconnected is not None
        assert reconnected.session_id == "sess-2"

    asyncio.run(scenario())


def test_disconnect_cleanup_is_idempotent() -> None:
    """Two ``unregister`` calls for the same name don't raise."""

    async def scenario() -> None:
        registry = NodeRegistry()
        await registry.register(_make_conn("laptop"))
        assert await registry.unregister("laptop") is not None
        # Second call: the server's finally-block runs once per
        # close event, but defensive code might run it twice in
        # edge cases (e.g. an explicit ``revoke`` racing with a
        # close). The contract is "safe no-op".
        assert await registry.unregister("laptop") is None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_registry_supports_multiple_independent_nodes() -> None:
    """A, B, C are tracked independently — unregistering one leaves the others."""

    async def scenario() -> None:
        registry = NodeRegistry()
        a = _make_conn("alpha")
        b = _make_conn("bravo")
        c = _make_conn("charlie")
        for conn in (a, b, c):
            await registry.register(conn)
        assert len(registry) == 3
        await registry.unregister("bravo")
        assert await registry.is_connected("alpha") is True
        assert await registry.is_connected("bravo") is False
        assert await registry.is_connected("charlie") is True
        snapshot = await registry.list_connected()
        assert sorted(c.name for c in snapshot) == ["alpha", "charlie"]

    asyncio.run(scenario())


def test_node_connection_is_immutable() -> None:
    """``NodeConnection`` is frozen — fields can't be reassigned.

    This is the property the registry relies on: when we need to
    bump ``last_heartbeat``, we build a new instance and replace
    the slot, never mutate in place.
    """
    conn = _make_conn("laptop")
    with pytest.raises(
        (AttributeError, Exception)
    ):  # FrozenInstanceError is a subclass
        conn.name = "other"  # type: ignore[misc]


def test_node_connection_last_heartbeat_default_is_connected_at() -> None:
    """``last_heartbeat`` is auto-set to ``connected_at`` if omitted."""
    conn = _make_conn("laptop")
    assert conn.last_heartbeat == conn.connected_at


# ---------------------------------------------------------------------------
# Waiter surface (issue #17 — TestNodeRegistryWaiters)
# ---------------------------------------------------------------------------
#
# The waiter registry is the bridge between the server's inbound dispatch
# loop and ``NodeEnvironment.execute``. Five behaviours must hold for the
# server to be safe under reconnect storms and for the environment to
# surface clean errors on the caller side:
#
#   1. ``register_waiter`` returns a pending ``asyncio.Future``;
#      ``complete_waiter`` resolves it with the inbound payload;
#      ``unregister_waiter`` is a no-op-safe cleanup.
#   2. ``fail_node_waiters`` resolves every waiter for a node with
#      ``_WaiterCancelled`` carrying the right ``reason``.
#   3. The lock-ordering invariant: a callback that re-enters the
#      registry (e.g. an exception handler that re-registers) does not
#      deadlock. This is the property the two-phase
#      snapshot-outside-the-lock design exists to guarantee.
#   4. The replace path (issue #10 fix-lock): when ``register`` is
#      called with a new connection, the OLD connection's waiters are
#      failed with ``reason="connection_replaced"`` and the NEW
#      connection's waiters are *not* affected.
#   5. ``register_waiter`` for a name with no current connection still
#      returns a future (the future is pending until the caller
#      observes it or ``fail_node_waiters`` resolves it).

# 100ms is short enough to fail loud on a deadlock, long enough to
# tolerate event-loop scheduling jitter on a busy CI box.
_WAIT_TIMEOUT_S = 0.1


async def _drain(future: asyncio.Future, timeout: float = _WAIT_TIMEOUT_S) -> None:
    """Await ``future`` (consuming its exception if any) with a timeout.

    A short timeout makes a deadlock in the registry surface as a
    clear test failure rather than hanging the suite. Callers
    inspect ``future.exception()`` and ``future.result()``
    separately so this helper can swallow the exception.
    """
    try:
        await asyncio.wait_for(_wait(future), timeout=timeout)
    except asyncio.TimeoutError:
        pytest.fail(
            f"future {future!r} did not resolve within {timeout}s — "
            "likely a deadlock or missed resolution"
        )


async def _wait(future: asyncio.Future) -> None:
    """Await ``future`` (consumes its exception if any)."""
    try:
        await future
    except BaseException:
        # Swallow — caller inspects ``future.exception()`` separately
        # so the test can assert on ``reason`` etc.
        pass


class TestNodeRegistryWaiters:
    """Locks in the waiter surface contract (issue #17)."""

    # ----- 1. register / complete / unregister round-trip ----------------

    def test_register_then_complete_resolves_with_payload(self) -> None:
        """The happy path: register a waiter, complete it with the payload."""

        async def scenario() -> None:
            registry = NodeRegistry()
            await registry.register(_make_conn("laptop"))
            future = await registry.register_waiter("laptop", "r-1")
            # Future is pending until complete_waiter fires.
            assert not future.done()

            assert (
                await registry.complete_waiter("laptop", "r-1", {"out": "ok"}) is True
            )
            # The future is resolved with the exact payload we passed in.
            await _drain(future)
            assert future.exception() is None
            assert future.result() == {"out": "ok"}
            # And the waiter is gone from the map (no leak).
            assert await registry.complete_waiter("laptop", "r-1", {}) is False

        asyncio.run(scenario())

    def test_unregister_waiter_is_idempotent_and_no_resolve(self) -> None:
        """``unregister_waiter`` removes the future without resolving it."""

        async def scenario() -> None:
            registry = NodeRegistry()
            await registry.register(_make_conn("laptop"))
            future = await registry.register_waiter("laptop", "r-1")
            await registry.unregister_waiter("laptop", "r-1")
            # Future never resolved: still pending, no result, no
            # exception. Callers can decide whether to keep waiting
            # or treat the future as orphaned.
            assert not future.done()
            # And a second unregister is a safe no-op.
            await registry.unregister_waiter("laptop", "r-1")
            # The same is true for a never-registered request_id.
            await registry.unregister_waiter("laptop", "r-ghost")

        asyncio.run(scenario())

    # ----- 2. fail_node_waiters -------------------------------------------

    def test_fail_node_waiters_resolves_every_waiter_with_reason(self) -> None:
        """All waiters for a node are failed with ``_WaiterCancelled(reason)``."""

        async def scenario() -> None:
            registry = NodeRegistry()
            await registry.register(_make_conn("laptop"))
            f1 = await registry.register_waiter("laptop", "r-1")
            f2 = await registry.register_waiter("laptop", "r-2")
            f_other = await registry.register_waiter("desktop", "r-1")

            woken = await registry.fail_node_waiters("laptop", "node_disconnected")
            assert woken == 2

            for fut in (f1, f2):
                await _drain(fut)
                exc = fut.exception()
                assert isinstance(exc, _WaiterCancelled)
                assert exc.reason == "node_disconnected"

            # The other node's waiter was not touched.
            assert not f_other.done()

        asyncio.run(scenario())

    def test_fail_node_waiters_with_no_waiters_returns_zero(self) -> None:
        """Failing an idle node is a no-op (return value 0)."""

        async def scenario() -> None:
            registry = NodeRegistry()
            await registry.register(_make_conn("laptop"))
            assert await registry.fail_node_waiters("laptop", "anything") == 0

        asyncio.run(scenario())

    # ----- 3. lock ordering (the snapshot/resolve split) -----------------

    def test_exception_callback_can_re_enter_registry(self) -> None:
        """A callback fired during ``set_exception`` can re-enter the registry.

        Regression lock for the two-phase ``fail_node_waiters`` design:
        if the implementation ever re-acquires the lock before calling
        ``set_exception``, a callback that awaits on the registry will
        deadlock. The test's short ``_WAIT_TIMEOUT_S`` makes the
        deadlock surface as a clear failure.
        """

        async def scenario() -> None:
            registry = NodeRegistry()
            await registry.register(_make_conn("laptop"))
            await registry.register(_make_conn("desktop"))
            reentered: list[str] = []
            reentry_done = asyncio.Event()

            future = await registry.register_waiter("laptop", "r-1")
            # The callback awaits on the registry — a lock-then-resolve
            # implementation would deadlock here. We schedule it via
            # ``add_done_callback`` so it fires inside
            # ``fail_node_waiters``'s second phase, then ``await`` an
            # ``asyncio.Event`` for a deterministic sync point (avoids
            # racing on ``asyncio.sleep(0)``).
            future.add_done_callback(
                lambda fut: asyncio.ensure_future(
                    _reenter(registry, fut, reentered, reentry_done)
                )
            )

            await registry.fail_node_waiters("laptop", "node_disconnected")

            # Wait for the re-entry path to finish (or timeout via
            # the asyncio.Event mechanism — but pytest's
            # ``_drain`` below also bounds the wait, so a deadlock
            # here surfaces as a clear test failure rather than a
            # hang).
            await asyncio.wait_for(reentry_done.wait(), timeout=_WAIT_TIMEOUT_S)
            await _drain(future)
            assert isinstance(future.exception(), _WaiterCancelled)
            # The callback ran to completion and re-entered the
            # registry twice (a register + an unregister on desktop).
            assert reentered == ["registered", "unregistered"]

        async def _reenter(
            registry: NodeRegistry,
            fut: asyncio.Future,
            log: list[str],
            done: asyncio.Event,
        ) -> None:
            try:
                # Only act if the failure was the one we set up.
                exc = fut.exception()
                if isinstance(exc, _WaiterCancelled):
                    # First re-entry: register a fresh waiter on
                    # another node.
                    f2 = await registry.register_waiter("desktop", "r-1")
                    log.append("registered")
                    # Second re-entry: clean it up. The first one is
                    # the load-bearing call (proves we could
                    # re-acquire the lock under the exception
                    # handler); this one proves the lock is released
                    # cleanly afterwards.
                    await registry.unregister_waiter("desktop", "r-1")
                    log.append("unregistered")
                    assert not f2.done()
            finally:
                # Signal the test that the re-entry path completed
                # — even on failure — so a deadlock surfaces as a
                # clear test failure rather than a hang.
                done.set()

        asyncio.run(scenario())

    # ----- 4. replace path (issue #10 fix-lock) ---------------------------

    def test_replace_path_fails_old_connection_waiters_only(self) -> None:
        """When ``register`` replaces, only the OLD connection's waiters fail.

        This is the regression lock for issue #10. The old code keyed
        the connection table by name and used ``(name, request_id)``
        for waiters — so when a new connection took over the slot,
        ``fail_node_waiters`` for the old connection couldn't tell
        the waiters apart. The new contract: registering a new
        connection under an existing name fails the old connection's
        in-flight waiters with ``reason="connection_replaced"`` and
        leaves the new connection's waiters alone.
        """

        async def scenario() -> None:
            registry = NodeRegistry()
            old = _make_conn("laptop", session_id="sess-old")
            new = _make_conn("laptop", session_id="sess-new")
            await registry.register(old)

            # Two in-flight calls on the old connection. These are
            # what the old bug would have wrongly fired when the new
            # connection registered.
            old_f1 = await registry.register_waiter("laptop", "r-1")
            old_f2 = await registry.register_waiter("laptop", "r-2")

            # The NEW connection registers and replaces the old one.
            await registry.register(new)

            # A new in-flight call against the new connection. This
            # MUST survive the replace — issue #10.
            new_f1 = await registry.register_waiter("laptop", "r-3")

            for fut in (old_f1, old_f2):
                await _drain(fut)
                exc = fut.exception()
                assert isinstance(exc, _WaiterCancelled)
                assert exc.reason == "connection_replaced"

            # The new connection's waiter is still pending.
            assert not new_f1.done()
            # And it's reachable via the normal completion path.
            assert await registry.complete_waiter("laptop", "r-3", {"ok": True}) is True
            await _drain(new_f1)
            assert new_f1.exception() is None
            assert new_f1.result() == {"ok": True}

        asyncio.run(scenario())

    # ----- 5. register_waiter for an unknown name -------------------------

    def test_register_waiter_for_unknown_name_returns_pending_future(self) -> None:
        """No-connection register: future is created and pending, not blocked.

        The docstring contract: callers should ``is_connected`` first,
        or accept that the future will resolve when the registry
        eventually fails it. The future is *created* regardless — the
        caller is not blocked at registration time — and a follow-up
        ``fail_node_waiters`` for the same name resolves it with
        ``_WaiterCancelled`` through the normal failure path.
        """

        async def scenario() -> None:
            registry = NodeRegistry()
            # No connection is registered for "ghost".
            assert await registry.is_connected("ghost") is False

            future = await registry.register_waiter("ghost", "r-1")
            # Created and pending — the call did not block on
            # "is there a connection?" and did not pre-resolve.
            assert isinstance(future, asyncio.Future)
            assert not future.done()

            # The waiter's entry exists in the map (we can find it
            # by failing waiters for the same name).
            woken = await registry.fail_node_waiters("ghost", "no_connection")
            assert woken == 1
            await _drain(future)
            exc = future.exception()
            assert isinstance(exc, _WaiterCancelled)
            assert exc.reason == "no_connection"

        asyncio.run(scenario())
