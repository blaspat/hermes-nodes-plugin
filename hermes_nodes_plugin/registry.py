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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable

from fastapi import WebSocket


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

    A fresh registry is created per server process. There is no
    persistence — losing the process loses the live-connection table,
    which is the correct behaviour (clients must reconnect).
    """

    def __init__(self) -> None:
        self._connections: dict[str, NodeConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, conn: NodeConnection) -> NodeConnection | None:
        """Install ``conn`` under ``conn.name``.

        Returns the previous connection at that name, or ``None`` if
        the slot was empty. The caller is responsible for closing the
        previous WebSocket — the registry only returns the object.

        Idempotency: registering the same :class:`NodeConnection` twice
        (e.g. by mistake in a test) is treated as a normal replace.
        """
        async with self._lock:
            previous = self._connections.get(conn.name)
            self._connections[conn.name] = conn
            return previous

    async def unregister(self, name: str) -> NodeConnection | None:
        """Remove the entry for ``name``. Returns what was removed, or ``None``.

        Safe to call from a connection's ``finally`` block — calling
        with a name that was never registered is a no-op.
        """
        async with self._lock:
            return self._connections.pop(name, None)

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
