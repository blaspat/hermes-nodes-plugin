"""In-memory registry of live node WebSocket connections.

Tracks every paired node's current connection from ``auth_ok`` until
the WebSocket closes. Task 2.4 built the minimum the server needs
(register / unregister / get / names); Task 2.5 extends it with
heartbeat bookkeeping, ``is_connected`` / ``list_connected`` lookups,
and a tested disconnect-cleanup contract.

The registry is the source of truth for "is node X online right now?"
on the server side. ``hermes node list`` (Task 2.10) and the
``node_exec`` tool (Task 2.7) both consult it. PROTOCOL §6 says the
server marks a node offline when its heartbeat times out, but the
actual timeout-driven eviction is an out-of-band sweep — the registry
just stores the timestamps and lets callers decide who's stale.

Concurrency model
-----------------

All mutations and reads run inside the event loop, so a single
:class:`asyncio.Lock` is sufficient. We don't need a thread lock
because FastAPI/Starlette dispatch coroutines on a single loop thread;
``unregister`` from a finally-block on connection close is awaited, so
it can't race with a lookup. The lock is here as a belt-and-braces
guard against future refactors that move things to background tasks.

The :class:`NodeConnection` value is treated as immutable from the
registry's perspective — fields are set at construction time and never
mutated. New state (``last_heartbeat`` updates) is applied by
constructing a fresh dataclass and replacing the entry, so the
in-memory shape stays frozen-equivalent.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import WebSocket

logger = logging.getLogger(__name__)


# Sentinel exception type for waiters that get cancelled because their
# connection went away (node disconnect, replacement, server drain). The
# server's dispatch loop and the NodeEnvironment both treat this as a
# non-error "the call cannot complete" signal — callers (Kate) can decide
# whether to surface a user-visible retry.
class _WaiterCancelled(BaseException):
    """Internal: a pending future was cancelled because its connection died.

    Inherits from ``BaseException`` so it bypasses ``except Exception``
    handlers in user code that might want to swallow general errors.
    Waiters should always re-raise this; the environment layer turns it
    into a clean :class:`NodeNotConnectedError` at the API boundary.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now_utc() -> datetime:
    """Return the current UTC time.

    Centralised so tests can monkeypatch it (Task 2.5 acceptance
    explicitly tests heartbeat age, which depends on a clock).
    """
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class NodeConnection:
    """A single live WebSocket connection from a paired node.

    Attributes:
        name: The node name this connection is registered under. Unique
            within a registry — a second connection from the same name
            would replace the first (the server closes the old one
            before installing the new one).
        websocket: The underlying FastAPI WebSocket. The registry
            hands it out to callers (e.g. ``exec`` dispatch in Task
            2.7) but never closes it directly — that's the connection
            handler's job.
        connected_at: UTC timestamp of when the auth handshake completed.
        session_id: UUIDv4 the server assigned during ``hello_ack``.
            Echoed back in ``auth_ok`` and used as the connection's
            identifier in audit logs.
        remote_addr: Client IP, captured at WebSocket accept time. The
            reverse proxy / TLS terminator must be configured to set
            the X-Forwarded-For header for this to be meaningful; in
            direct mode it's the literal peer address. Stored as a
            string because the WebSocket scope's ``client`` tuple
            nests differently across ASGI servers.
        last_heartbeat: UTC timestamp of the most recent activity on
            this connection (any inbound message, or explicit
            ``pong``). Defaults to ``connected_at`` — the moment we
            finished the handshake counts as the first heartbeat.
    """

    name: str
    websocket: WebSocket = field(compare=False, repr=False)
    connected_at: datetime = field(default_factory=_now_utc)
    session_id: str = ""
    remote_addr: str = ""
    last_heartbeat: datetime = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # ``last_heartbeat`` defaults to ``connected_at`` so a freshly
        # registered connection is never "stale". The dataclass
        # machinery evaluates ``field(default=None)`` before the
        # factory for ``connected_at`` runs, so we set it here.
        if self.last_heartbeat is None:
            object.__setattr__(self, "last_heartbeat", self.connected_at)


class NodeRegistry:
    """Thread-safe (single-loop) map of node name → :class:`NodeConnection`.

    Public surface (Task 2.5):

    * :meth:`register` — install a connection under a node name.
      Replacing an existing entry closes the old WebSocket so the
      previous holder can't keep a phantom session alive.
    * :meth:`unregister` — remove by name; safe no-op if absent.
    * :meth:`get` — return the connection for a name, or ``None``.
    * :meth:`is_connected` — boolean variant of :meth:`get` for the
      common "is this node online?" check. Cheap; same lock as ``get``.
    * :meth:`list_connected` — snapshot of *all* live connections, as
      a list of :class:`NodeConnection`. Used by ``hermes node list``.
    * :meth:`names` — snapshot of just the names (kept from Task 2.4
      for code that only needs the keys).
    * :meth:`touch_heartbeat` — bump ``last_heartbeat`` for a name.
      Called from the connection handler whenever any inbound message
      arrives (PROTOCOL §6 — the heartbeat is "any message", not
      just ``ping``/``pong``).
    * :meth:`stale` — connections whose ``last_heartbeat`` is older
      than the supplied threshold. Used by the eventual sweep job
      (PROTOCOL §6 server side) — not part of Task 2.5 acceptance
      but it's the natural sibling of ``touch_heartbeat`` and lives
      here so the time math is in one place.

    Waiter registry (Task 2.7)
    --------------------------

    ``NodeEnvironment`` is a request/response interface: the brain sends
    ``exec`` and awaits ``exec_result``. The future that represents the
    pending call must be registered somewhere keyed by ``(node_name,
    request_id)`` so the server's inbound dispatch loop can complete it
    when the matching response arrives. We hold that map here (rather
    than on :class:`NodeConnection`) because:

      * The map is server-wide state — multiple in-flight calls to the
        same node share one WebSocket and one waiter namespace.
      * When a connection is replaced or lost, we need to fail *all*
        of that node's pending waiters with a single call. Keeping the
        map on the registry makes that a one-liner
        (:meth:`fail_node_waiters`) instead of fishing waiters out of
        a frozen dataclass.

    See :meth:`register_waiter`, :meth:`complete_waiter`, and
    :meth:`fail_node_waiters` for the public surface.

    A fresh registry is created per server process. There is no
    persistence — losing the process loses the live-connection table,
    which is the correct behaviour (clients must reconnect).
    """

    def __init__(self) -> None:
        self._connections: dict[str, NodeConnection] = {}
        self._lock = asyncio.Lock()
        # Pending request/response futures, keyed by (node_name, request_id).
        # The future's value is the decoded inbound message body (typically
        # the ``exec_result`` payload). Lives in the registry (not the
        # dataclass) because the dataclass is frozen and waiter count
        # changes constantly.
        self._waiters: dict[tuple[str, str], asyncio.Future[Any]] = {}

    async def register(self, conn: NodeConnection) -> NodeConnection | None:
        """Install ``conn`` under ``conn.name``.

        Returns the previous connection at that name, or ``None`` if
        the slot was empty. The caller is responsible for closing the
        previous WebSocket — the registry only returns the object.

        Idempotency: registering the same :class:`NodeConnection` twice
        (e.g. by mistake in a test) is treated as a normal replace.

        Replacing an existing connection also fails every pending
        waiter for that node name (Task 2.7). The previous session's
        in-flight calls can never resolve through the new socket, so
        we wake them with :class:`_WaiterCancelled` rather than letting
        them hang until the per-call timeout fires.
        """
        async with self._lock:
            previous = self._connections.get(conn.name)
            self._connections[conn.name] = conn
        if previous is not None and previous is not conn:
            # Fail the old session's waiters outside the lock so the
            # cancellation callbacks (which may themselves await on
            # the registry) don't deadlock.
            await self.fail_node_waiters(conn.name, "connection_replaced")
        return previous

    async def unregister(
        self, name: str, *, expected_session_id: str | None = None
    ) -> NodeConnection | None:
        """Remove the entry for ``name``. Returns what was removed, or ``None``.

        Safe to call from a connection's ``finally`` block — calling
        with a name that was never registered is a no-op.

        Args:
            name: The node name to unregister.
            expected_session_id: When provided, the pop is a no-op
                unless the entry currently under ``name`` has a
                matching :attr:`NodeConnection.session_id`. This is
                the reconnect-safety guard: a connection's
                ``finally`` block can fire after a newer connection
                has already replaced it on the slot (PROTOCOL §1
                reconnect), and the old ``finally`` must not pop the
                new entry. The server passes the auth-time
                ``session_id`` here; a bare caller (e.g. a test, or a
                forced ``revoke`` that doesn't care which session
                it's removing) can pass ``None`` to disable the
                guard and behave like the legacy unconditional
                ``unregister``.

        Also fails every pending waiter for ``name`` with
        :class:`_WaiterCancelled` (``reason="node_disconnected"``).
        A closed WebSocket can never answer its in-flight calls, so we
        wake them rather than letting them time out. Waiters are only
        failed when an entry is actually popped — a guarded no-op
        leaves the new connection's in-flight calls untouched.
        """
        async with self._lock:
            current = self._connections.get(name)
            if current is None:
                return None
            if (
                expected_session_id is not None
                and current.session_id != expected_session_id
            ):
                # The slot has been replaced by a newer connection.
                # The old session's finally-block must not pop the
                # new entry, and must not fail the new connection's
                # in-flight waiters with a bogus "node_disconnected"
                # reason. See issue #10.
                logger.debug(
                    "unregister(%r) skipped — current session_id=%r, expected=%r",
                    name,
                    current.session_id,
                    expected_session_id,
                )
                return None
            # ``current`` is non-None and matches the expected
            # session_id (or no guard was requested), so the
            # remove is guaranteed to succeed — no default needed.
            del self._connections[name]
        await self.fail_node_waiters(name, "node_disconnected")
        return current

    async def get(self, name: str) -> NodeConnection | None:
        """Return the live connection for ``name``, or ``None``."""
        async with self._lock:
            return self._connections.get(name)

    async def is_connected(self, name: str) -> bool:
        """Return ``True`` iff a connection is currently registered for ``name``.

        Equivalent to ``await registry.get(name) is not None`` but
        reads more naturally at call sites like
        ``if not await registry.is_connected("laptop"): ...``.
        """
        async with self._lock:
            return name in self._connections

    async def list_connected(self) -> list[NodeConnection]:
        """Snapshot of every live :class:`NodeConnection`.

        Returned in insertion order (the registry's underlying dict
        preserves insertion order since CPython 3.7). The snapshot is
        a fresh list of *new* dataclass instances — callers can
        mutate the returned list without affecting the registry, and
        the contained ``NodeConnection`` objects are still frozen
        (you can't touch their fields, only the list).
        """
        async with self._lock:
            return list(self._connections.values())

    async def names(self) -> list[str]:
        """Snapshot of currently-registered node names.

        Kept from Task 2.4 for code that only needs the keys.
        Prefer :meth:`list_connected` if you need any connection
        field — fetching a name and then calling :meth:`get` is a
        race window in principle (though the lock makes it safe in
        practice).
        """
        async with self._lock:
            return list(self._connections.keys())

    async def touch_heartbeat(self, name: str, *, at: datetime | None = None) -> bool:
        """Bump ``last_heartbeat`` for ``name``.

        Args:
            name: The node to update.
            at: Override the timestamp (test seam — production code
                should pass nothing and let ``_now_utc`` decide). When
                ``None``, the current UTC time is used.

        Returns:
            ``True`` if a connection was found and updated, ``False``
            otherwise. The connection handler in :mod:`server` logs
            (but does not crash) on a ``False`` return — that means
            the node disconnected between the inbound message being
            read and the registry being updated, which is benign.
        """
        ts = at if at is not None else _now_utc()
        async with self._lock:
            current = self._connections.get(name)
            if current is None:
                return False
            self._connections[name] = replace(current, last_heartbeat=ts)
            return True

    async def stale(self, *, older_than: timedelta) -> list[NodeConnection]:
        """Connections whose ``last_heartbeat`` is older than ``older_than``.

        Per PROTOCOL §6, a server considers a node dead after 60s
        without any message. The actual eviction is a separate sweep
        (Task 2.5 keeps it in the registry's sibling code), but
        ``stale`` returns the candidates so the sweep can close them.

        Returned connections are NOT removed from the registry —
        callers must call :meth:`unregister` after closing the
        WebSocket. This keeps ``stale`` read-only and testable.
        """
        cutoff = _now_utc() - older_than
        async with self._lock:
            return [c for c in self._connections.values() if c.last_heartbeat < cutoff]

    # -- waiter registry (Task 2.7) ---------------------------------------

    async def register_waiter(self, name: str, request_id: str) -> asyncio.Future[Any]:
        """Register a future for an in-flight request to ``name``.

        The future is set (with a result payload) by
        :meth:`complete_waiter` when the matching ``exec_result`` /
        ``read_result`` / etc. arrives, or by :meth:`fail_node_waiters`
        when the connection dies. :class:`NodeEnvironment` awaits the
        future with its own timeout.

        Args:
            name: The node the request was sent to. Must currently
                have a registered connection — callers should
                :meth:`is_connected` first, or accept that the future
                will resolve quickly with :class:`_WaiterCancelled`.
            request_id: The protocol-level correlation id (UUIDv4 per
                PROTOCOL §2). Unique within ``name`` for the duration
                of the call; the server enforces this by keying on
                ``(name, request_id)``.

        Returns:
            A pending :class:`asyncio.Future`. The caller is
            responsible for removing the waiter on timeout/cancel via
            :meth:`unregister_waiter` to avoid leaking the dict entry
            on the failure path.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        async with self._lock:
            self._waiters[(name, request_id)] = future
        return future

    async def complete_waiter(self, name: str, request_id: str, payload: Any) -> bool:
        """Resolve the waiter for ``(name, request_id)`` with ``payload``.

        Called from the server's inbound dispatch loop when an
        ``exec_result`` (or equivalent) arrives. Returns ``True`` if a
        waiter was found and resolved, ``False`` otherwise. A
        ``False`` return is benign — it means the call already
        timed out (or its connection died) and the waiter was
        removed; the inbound response is discarded.

        This method is *synchronous-ish* (no ``await`` on the body
        beyond the lock acquisition) so the dispatch loop can keep
        reading the next message without yielding to other tasks.
        """
        async with self._lock:
            future = self._waiters.pop((name, request_id), None)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True

    async def unregister_waiter(self, name: str, request_id: str) -> None:
        """Remove a waiter without resolving it.

        Used on the caller's timeout/cancel path so the dict entry
        doesn't outlive the future. Safe to call when no waiter is
        registered (idempotent).
        """
        async with self._lock:
            self._waiters.pop((name, request_id), None)

    async def fail_node_waiters(self, name: str, reason: str) -> int:
        """Wake every pending waiter for ``name`` with :class:`_WaiterCancelled`.

        Called from :meth:`unregister` (node disconnect) and from
        :meth:`register` (connection replaced). The dispatch is
        asynchronous and runs the snapshot/resolve split so we never
        hold the lock while invoking ``future.set_exception`` — a
        future's exception callback might itself try to take the
        lock, and re-entry would deadlock.

        Returns:
            The number of waiters that were woken. Useful for tests
            and for a log line on the rare "many waiters at once"
            event.
        """
        # Phase 1: under the lock, snapshot and pop every matching waiter.
        async with self._lock:
            keys = [k for k in self._waiters if k[0] == name]
            futures: list[asyncio.Future[Any]] = []
            for k in keys:
                f = self._waiters.pop(k, None)
                if f is not None:
                    futures.append(f)
        # Phase 2: outside the lock, resolve the futures. We do this in
        # a second pass so a waiter callback that touches the registry
        # (e.g. an environment's cancel handler) doesn't deadlock.
        woken = 0
        for future in futures:
            if future.done():
                continue
            future.set_exception(_WaiterCancelled(reason))
            woken += 1
        if woken:
            logger.debug(
                "failed %d pending waiter(s) for node %r (reason=%s)",
                woken,
                name,
                reason,
            )
        return woken

    def __len__(self) -> int:
        # Synchronous convenience for tests / debug printing. Not part
        # of the public API; using the unsynchronised ``len`` of the
        # dict is fine because CPython dict reads are atomic and the
        # only mutation paths are coroutines running on the same loop.
        return len(self._connections)

    def __contains__(self, name: object) -> bool:
        return name in self._connections

    def __iter__(self) -> Iterable[str]:
        return iter(tuple(self._connections))


__all__ = ["NodeConnection", "NodeRegistry"]
