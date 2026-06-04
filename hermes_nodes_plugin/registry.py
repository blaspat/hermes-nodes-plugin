"""In-memory registry of live node WebSocket connections.

This is the minimum the WSS server (Task 2.4) needs: a way to register a
connection after a successful auth handshake, look it up by node name,
and clean up on disconnect. Task 2.5 will extend it (heartbeat
tracking, IP tracking, capability cache, multi-server federation hooks)
without changing the existing surface.

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
mutated. New state (last_heartbeat, etc.) goes into a new object.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import WebSocket


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
    """

    name: str
    websocket: WebSocket = field(compare=False, repr=False)
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str = ""
    remote_addr: str = ""


class NodeRegistry:
    """Thread-safe (single-loop) map of node name → :class:`NodeConnection`.

    The registry is intentionally small in Task 2.4. Task 2.5 will add
    ``is_connected``, ``list_connected``, heartbeat bookkeeping, and a
    "drop on token revoke" hook. The public methods we need now are:

    * :meth:`register` — install a connection under a node name.
      Replacing an existing entry closes the old WebSocket so the
      previous holder can't keep a phantom session alive.
    * :meth:`unregister` — remove by name; safe no-op if absent.
    * :meth:`get` — return the connection for a name, or ``None``.
    * :meth:`names` — return a snapshot of currently-registered names.

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

    async def names(self) -> list[str]:
        """Snapshot of currently-registered node names.

        Order is not guaranteed. The snapshot is a fresh list, so the
        caller can iterate without worrying about concurrent mutation.
        """
        async with self._lock:
            return list(self._connections.keys())

    def __len__(self) -> int:
        # Synchronous convenience for tests / debug printing. Not part
        # of the post-2.5 public API; using the unsynchronised
        # ``len`` of the dict is fine because CPython dict reads are
        # atomic and the only mutation paths are coroutines running
        # on the same loop.
        return len(self._connections)

    def __contains__(self, name: object) -> bool:
        return name in self._connections


__all__ = ["NodeConnection", "NodeRegistry"]
