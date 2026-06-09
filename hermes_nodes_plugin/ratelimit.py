"""Per-node sliding-window rate limiter (FR-2.6).

Implements the spec-mandated ``100 calls/second per node`` cap on
inbound WebSocket messages, applied after the handshake completes
and before the action handler dispatches the message. The 101st
call within any rolling 1-second window triggers a close-4004 with
a structured ``rate_limit`` error frame (PROTOCOL §4).

Algorithm
---------

Each node name gets a ``collections.deque`` of monotonic-clock
timestamps. On every call we:

  1. Evict timestamps older than ``window_seconds`` (default 1.0).
  2. If ``len(deque) >= max_calls``, return ``False`` (limit hit).
  3. Otherwise append ``now()`` and return ``True``.

Per-node isolation is achieved by keying on ``node_name`` in the
top-level dict; one noisy node cannot exhaust another node's budget.

Async safety
------------

``check()`` is intentionally synchronous and lock-free. The WSS
dispatch loop is single-coroutine per connection, and CPython's GIL
makes the deque ``append``/``popleft`` pair atomic. The module is
imported by the WSS handler but is otherwise independent of
FastAPI / Starlette.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Deque

logger = logging.getLogger(__name__)

#: Default cap per node (FR-2.6 spec).
DEFAULT_RATE_LIMIT_PER_NODE: int = 100

#: Window length the cap applies over. Spec says "100 calls/second"
#: — that's a 1-second sliding window. Constant, not a config key,
#: because the spec phrases the limit in terms of "per second" and
#: a different window would change its meaning.
DEFAULT_WINDOW_SECONDS: float = 1.0

#: A callable returning a monotonic-clock float. Pluggable for tests
#: so boundary conditions (t=0.0, t=1.001) can be driven
#: deterministically. Production code uses :func:`time.monotonic`.
ClockFn = Callable[[], float]


class _RateLimiter:
    """Per-node sliding-window rate limiter.

    Keyed on ``node_name``; each node gets its own ``deque`` of
    timestamps. ``check(node_name)`` evicts expired timestamps,
    rejects when the cap is hit, and otherwise records the call.

    Args:
        max_calls: Maximum calls allowed in any rolling
            ``window_seconds`` window per node. A value ``<= 0``
            makes the limiter fail-open (always allow); the
            constructor logs a warning so an operator
            misconfiguration is visible.
        window_seconds: Window length in seconds. Default 1.0
            (the spec's "per second").
        clock: Callable returning a monotonic-clock float.
            Defaults to :func:`time.monotonic`. Tests inject a
            stub clock to make boundary conditions deterministic.

    The class name is underscored because it is an internal
    implementation detail of the server's dispatch loop. Tests
    import it directly; production callers go through
    :func:`hermes_nodes_plugin.server.create_app`.
    """

    __slots__ = ("_max_calls", "_window_seconds", "_clock", "_windows")

    def __init__(
        self,
        *,
        max_calls: int = DEFAULT_RATE_LIMIT_PER_NODE,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: ClockFn | None = None,
    ) -> None:
        self._max_calls = int(max_calls)
        self._window_seconds = float(window_seconds)
        self._clock: ClockFn = clock if clock is not None else time.monotonic
        # node_name -> deque[float] of recent call timestamps.
        # Bounded by ``max_calls`` per node (we never append past
        # the cap), so memory is O(nodes * max_calls) at worst.
        self._windows: dict[str, Deque[float]] = {}

        if self._max_calls <= 0:
            # Fail-open. A warning is logged so a typo'd
            # ``HERMES_NODES_RATE_LIMIT=0`` doesn't silently
            # disable the limit without trace.
            logger.warning(
                "rate limit max_calls=%r (<= 0): failing open (all calls allowed)",
                self._max_calls,
            )
        if self._window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be > 0, got {self._window_seconds!r}"
            )

    # -- introspection --------------------------------------------------

    @property
    def max_calls(self) -> int:
        """Configured cap per node. ``<= 0`` means fail-open."""
        return self._max_calls

    @property
    def window_seconds(self) -> float:
        """Window length in seconds."""
        return self._window_seconds

    def is_fail_open(self) -> bool:
        """True if this limiter accepts every call (max_calls <= 0)."""
        return self._max_calls <= 0

    def tracked_nodes(self) -> set[str]:
        """Snapshot of the node names currently being tracked.

        A node whose deque is empty is *not* in the snapshot —
        entries are pruned when their last timestamp ages out.
        """
        # Prune empty deques so a fresh ``tracked_nodes()`` after a
        # quiet period reflects current state, not history.
        empty = [name for name, dq in self._windows.items() if not dq]
        for name in empty:
            del self._windows[name]
        return set(self._windows.keys())

    # -- the hot path ---------------------------------------------------

    def check(self, node_name: str) -> bool:
        """Record a call from ``node_name`` and report whether it is allowed.

        Returns ``True`` if the call is within the limit, ``False``
        if the cap is hit. ``False`` is the *signal* to the server
        to send the ``rate_limit`` error frame and close with 4004.
        """
        if self._max_calls <= 0:
            # Fail-open. Short-circuit before touching the dict so
            # a misconfigured limit doesn't grow ``_windows`` with
            # unbounded keys.
            return True

        now = self._clock()
        cutoff = now - self._window_seconds
        window = self._windows.get(node_name)
        if window is None:
            window = deque()
            self._windows[node_name] = window

        # Evict expired timestamps. ``popleft`` is O(1) on a deque
        # and stops as soon as we see a timestamp in-window.
        while window and window[0] <= cutoff:
            window.popleft()

        if len(window) >= self._max_calls:
            # Cap hit. Do not record the rejected call — counting
            # rejected calls would let a chatty node keep its
            # window full forever, and "calls in the last second"
            # is what the spec measures.
            return False

        window.append(now)
        return True

    # -- testing hooks --------------------------------------------------

    def reset(self) -> None:
        """Clear all tracked windows. Tests call this between cases."""
        self._windows.clear()

    def snapshot(self, node_name: str) -> tuple[float, ...]:
        """Read-only view of the timestamps currently held for ``node_name``."""
        dq = self._windows.get(node_name)
        if dq is None:
            return ()
        return tuple(dq)


__all__ = [
    "DEFAULT_RATE_LIMIT_PER_NODE",
    "DEFAULT_WINDOW_SECONDS",
    "_RateLimiter",
]
