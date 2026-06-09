"""Tests for :mod:`hermes_nodes_plugin.ratelimit` and the FR-2.6 dispatch hook.

Coverage:

  **Unit tests** (no network, no FastAPI):

    * Window rollover — a call older than ``window_seconds`` ages out
      and the next call is allowed even if the cap was hit.
    * Per-node isolation — exhausting the limit on one node does not
      affect another.
    * Exact 100/101 boundary — calls 1..100 succeed, call 101 fails.
    * Fail-open behaviour — ``max_calls <= 0`` allows every call
      and logs a warning.
    * Rejected calls are not recorded — a chatty node that hits the
      cap keeps its window at the cap, not growing past it.

  **Config-layer tests**:

    * Env override (``HERMES_NODES_RATE_LIMIT=200``).
    * ``=0`` and ``=-1`` disable the limit (no config error).
    * Non-integer values raise :class:`ConfigError`.
    * Empty string from the env falls back to the default.

  **Integration tests** (one WSS round-trip, low cap):

    * 4 calls in <1s with a 3-cps cap → calls 1..3 succeed, call 4
      triggers a ``rate_limit`` error frame + close 4004.
    * ``HERMES_NODES_RATE_LIMIT=0`` config: server starts, accepts
      every call (the limit is disabled).
    * Per-node isolation across two connections: a noisy node A
      hitting its cap does not block a quiet node B.

The unit tests use a fake clock (a small list iterated over by
``next()``) so the boundary conditions are deterministic and the
test does not depend on wall-clock time.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from hermes_nodes_plugin.config import NodeServerConfig, load_config
from hermes_nodes_plugin.ratelimit import (
    DEFAULT_RATE_LIMIT_PER_NODE,
    DEFAULT_WINDOW_SECONDS,
    _RateLimiter,
)
from hermes_nodes_plugin.registry import NodeRegistry
from hermes_nodes_plugin.server import (
    CLOSE_RATE_LIMIT_EXCEEDED,
    create_app,
)
from hermes_nodes_plugin.tokens import TokenStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic clock stand-in driven by a list of timestamps.

    Each call to ``__call__()`` returns the next value from the
    ``ticks`` sequence. When the sequence is exhausted, the clock
    raises :class:`IndexError` — this is the signal that the test
    asked for more ticks than it provided, and is loud on purpose
    (silent wraparound would mask clock-related bugs).
    """

    def __init__(self, *ticks: float) -> None:
        self._ticks = list(ticks)
        self._idx = 0

    def __call__(self) -> float:
        if self._idx >= len(self._ticks):
            raise IndexError(
                f"FakeClock exhausted after {self._idx} ticks; "
                "test should provide enough timestamps for all check() calls"
            )
        value = self._ticks[self._idx]
        self._idx += 1
        return value

    def set(self, *ticks: float) -> None:
        """Replace the remaining ticks (used in the tracked_nodes test)."""
        self._ticks = list(ticks)
        self._idx = 0


# ---------------------------------------------------------------------------
# Unit tests — sliding-window semantics
# ---------------------------------------------------------------------------


def test_check_allows_up_to_max_calls_in_window() -> None:
    """Calls 1..N all succeed when spaced inside the window."""
    clock = _FakeClock(0.0, 0.1, 0.2, 0.3, 0.4)
    limiter = _RateLimiter(max_calls=5, window_seconds=1.0, clock=clock)
    for i in range(5):
        assert limiter.check("node-a") is True, f"call {i + 1} should be allowed"


def test_check_rejects_call_101_at_100_cap() -> None:
    """The exact 100/101 boundary from the spec.

    With ``max_calls=100`` and a 1s window, 100 calls at t=0 all
    succeed; the 101st call at t=0.1 fails.
    """
    # 100 successes + 1 rejection = 101 ticks.
    clock = _FakeClock(*([0.0] * 101))
    limiter = _RateLimiter(
        max_calls=DEFAULT_RATE_LIMIT_PER_NODE,
        window_seconds=DEFAULT_WINDOW_SECONDS,
        clock=clock,
    )
    for i in range(DEFAULT_RATE_LIMIT_PER_NODE):
        assert limiter.check("node-a") is True, f"call {i + 1} should be allowed"
    assert limiter.check("node-a") is False, "call 101 should be rejected"


def test_check_rejected_call_is_not_recorded() -> None:
    """A rejected call must not extend the window.

    If we counted rejected calls, a chatty node could keep its
    window perpetually full and never recover. The spec measures
    "calls in the last second" — only successful attempts count.
    """
    clock = _FakeClock(0.0, 0.0, 0.0)  # 3 ticks: 2 ok, 1 rejected
    limiter = _RateLimiter(max_calls=2, window_seconds=1.0, clock=clock)
    assert limiter.check("node-a") is True
    assert limiter.check("node-a") is True
    assert limiter.check("node-a") is False  # 3rd call rejected
    # Window should still hold exactly 2 timestamps.
    assert len(limiter.snapshot("node-a")) == 2


def test_check_window_rollover_allows_call_after_age_out() -> None:
    """A call at t=0 aged out at t=1.001 lets the next call in.

    Acceptance criterion #4: "a call at t=0.0 + a call at t=0.5 +
    a call at t=1.001 should NOT all fail — the first one has
    aged out." We exercise that path explicitly: with max_calls=2,
    the 3rd call at t=0.5 would normally fail; but if we fast-
    forward the clock past the window, the 1st call ages out and
    the new call is allowed.
    """
    clock = _FakeClock(0.0, 0.5, 1.001)
    limiter = _RateLimiter(max_calls=2, window_seconds=1.0, clock=clock)
    assert limiter.check("node-a") is True  # t=0.0
    assert limiter.check("node-a") is True  # t=0.5
    # Window is full: 2 calls in the last 1.0s.
    # Without advancing the clock, the next call would fail. But
    # we advance the clock to t=1.001 so the t=0.0 call is now
    # older than the window.
    assert limiter.check("node-a") is True  # t=1.001, t=0.0 aged out


def test_check_window_boundary_exact_age_out() -> None:
    """The cut-off is strict: a call at exactly window_seconds ages out.

    The condition is ``window[0] <= cutoff``, where ``cutoff =
    now - window_seconds``. So a call exactly window_seconds old
    IS considered expired. Documented behaviour, verified here so
    a future "off-by-one" tweak gets caught.
    """
    clock = _FakeClock(0.0, 1.0)  # 2 ticks
    limiter = _RateLimiter(max_calls=1, window_seconds=1.0, clock=clock)
    assert limiter.check("node-a") is True  # t=0.0
    # At t=1.0, cutoff = 0.0, the stored t=0.0 is exactly at the
    # boundary and ages out, so the new call is allowed.
    assert limiter.check("node-a") is True


def test_check_per_node_isolation() -> None:
    """Exhausting node A's cap does NOT affect node B.

    Acceptance criterion #5: per-node isolation. We exhaust node A
    with a 2-cps cap, then verify node B can still make 2 calls
    in the same window.
    """
    clock = _FakeClock(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    limiter = _RateLimiter(max_calls=2, window_seconds=1.0, clock=clock)
    # Exhaust A.
    assert limiter.check("node-a") is True
    assert limiter.check("node-a") is True
    assert limiter.check("node-a") is False
    # B is untouched — fresh budget.
    assert limiter.check("node-b") is True
    assert limiter.check("node-b") is True
    assert limiter.check("node-b") is False


def test_check_per_node_independent_windows() -> None:
    """A's window can roll over without affecting B.

    Older A calls age out independently of B. After time passes,
    A can make calls again even if B's window is still saturated.
    """
    # 4 check() calls: A@t=0, B@t=0, A-rejected@t=0, A@t=2.
    # The rejected call still consumes a clock tick (the check
    # happened; we just didn't record the timestamp).
    clock = _FakeClock(0.0, 0.0, 0.0, 2.0)
    limiter = _RateLimiter(max_calls=1, window_seconds=1.0, clock=clock)
    # Each at t=0.0: A allowed, B allowed.
    assert limiter.check("node-a") is True
    assert limiter.check("node-b") is True
    # A's 2nd attempt at t=0.0: rejected.
    assert limiter.check("node-a") is False
    # Time passes; A's old call ages out. B's also ages out, but
    # A's check here is independent of B's state.
    assert limiter.check("node-a") is True


def test_check_prunes_empty_deque_after_window_ages_out() -> None:
    """A node whose deque empties must not leave a lingering empty entry.

    Regression test for the W2 finding from Quinn's review of
    t_c9cf21f0: ``_windows`` previously held empty deques for
    nodes whose last timestamp had aged out, until either the
    same node called again or ``reset()`` ran. With the prune
    step in ``check()``, the dict only contains nodes that have
    at least one timestamp inside the current window.

    The pre-fix code would still hold a ``deque()`` for
    ``node_a`` after the quiet period; the post-fix code
    deletes the key entirely on the first call after the window
    empties, then re-inserts a fresh deque. We assert the
    internal dict state directly so the test would fail loudly
    on the pre-fix code (where the deque, though length 1, is
    a different object than a freshly-allocated one).
    """
    # 5 calls at t=0.0..0.4 fill node_a's window, then the
    # clock jumps to t=2.0 (well past the 1.0s window) for the
    # next check on node_a. We hold on the reference to the
    # pre-quiet deque so we can prove the post-quiet deque is
    # a fresh one.
    clock = _FakeClock(0.0, 0.1, 0.2, 0.3, 0.4, 2.0, 0.0)
    limiter = _RateLimiter(max_calls=5, window_seconds=1.0, clock=clock)

    for _ in range(5):
        assert limiter.check("node_a") is True
    pre_quiet_deque = limiter._windows["node_a"]
    assert len(pre_quiet_deque) == 5

    # Quiet period: clock advances past the window. The next
    # check on node_a evicts all 5 timestamps; the prune step
    # drops the key entirely, then inserts a fresh deque.
    assert limiter.check("node_a") is True
    post_quiet_deque = limiter._windows["node_a"]

    # Fresh deque object — the prune step in check() removes
    # the key before re-inserting, so the post-quiet deque is
    # NOT the same object as the pre-quiet one. (The pre-fix
    # code mutates the existing deque in place, so this
    # identity check fails loudly.)
    assert post_quiet_deque is not pre_quiet_deque
    assert len(post_quiet_deque) == 1
    assert limiter.snapshot("node_a") == (2.0,)

    # And the public read-back: the node is still tracked,
    # with a single entry.
    assert limiter.tracked_nodes() == {"node_a"}


def test_check_prune_is_keyed_on_caller_node() -> None:
    """The prune in ``check()`` is keyed on the caller, not a global sweep.

    Important behavioural contract: a ``check("node_b")`` call
    does NOT prune an empty deque left behind by a different
    node ``node_a``. The prune fires only when the matching
    node's deque goes empty on its own check. This is what
    keeps ``check()`` O(1)-ish (a single dict lookup, single
    deque eviction) — a global sweep would make the hot path
    linear in the number of tracked nodes.

    Drives node_a into a quiet state, then verifies that
    subsequent ``check("node_b")`` calls leave ``node_a``'s
    deque in place (3 timestamps, no eviction). A later
    ``check("node_a")`` call evicts those timestamps, triggers
    the prune, and inserts a fresh deque — and the deque
    object is a *different* one from the pre-prune deque,
    proving the prune ran.
    """
    clock = _FakeClock(0.0, 0.0, 0.0, 0.0, 0.0)
    limiter = _RateLimiter(max_calls=3, window_seconds=1.0, clock=clock)
    # node_a: 3 calls at t=0.0
    for _ in range(3):
        assert limiter.check("node_a") is True
    # node_b: 1 call at t=0.0
    assert limiter.check("node_b") is True
    assert set(limiter._windows) == {"node_a", "node_b"}
    pre_a = limiter._windows["node_a"]
    assert len(pre_a) == 3

    # Fast-forward past the 1.0s window. check("node_b") at
    # t=2.0 ages out node_b's old call and reaps it. node_a's
    # deque is NOT touched (the prune is keyed on the caller),
    # so its 3 timestamps from t=0.0 are still in place.
    clock.set(2.0, 2.0)
    assert limiter.check("node_b") is True
    assert "node_b" in limiter._windows
    assert len(limiter._windows["node_b"]) == 1
    # node_a's deque is exactly the same object it was before
    # — the prune is caller-keyed, not a global sweep.
    assert limiter._windows["node_a"] is pre_a
    assert len(limiter._windows["node_a"]) == 3

    # NOW drive the prune for node_a. A check("node_a") at
    # t=3.0 evicts all 3 timestamps, triggers the prune
    # (deletes the key), and inserts a fresh deque of
    # length 1. The new deque is a *different* object from
    # the one that just got pruned.
    clock.set(3.0)
    assert limiter.check("node_a") is True
    assert limiter._windows["node_a"] is not pre_a
    assert len(limiter._windows["node_a"]) == 1


def test_check_recreate_fresh_deque_after_full_age_out() -> None:
    """After every entry ages out, the deque is a fresh one, not a carry-over.

    Stronger companion to the test above. Drives two nodes
    through a quiet period, then asserts each ``_windows``
    entry is a *different object* from the pre-quiet deque —
    not the in-place mutation that the pre-fix code produced.
    The deque is length 1 (the just-appended timestamp) and
    not the same object as the pre-quiet deque. This locks in
    the "O(active_nodes * max_calls) at worst" claim in the
    module's docstring.
    """
    clock = _FakeClock(0.0, 0.0, 2.0, 2.0)
    limiter = _RateLimiter(max_calls=1, window_seconds=1.0, clock=clock)
    assert limiter.check("node_a") is True
    assert limiter.check("node_b") is True
    pre_a = limiter._windows["node_a"]
    pre_b = limiter._windows["node_b"]
    assert set(limiter._windows) == {"node_a", "node_b"}

    # Both windows age out at t=2.0. The prune step drops the
    # key entirely before re-inserting, so the post-quiet
    # deques are different objects — not the in-place mutated
    # deques the pre-fix code would have produced.
    assert limiter.check("node_a") is True
    assert limiter.check("node_b") is True

    assert limiter._windows["node_a"] is not pre_a
    assert limiter._windows["node_b"] is not pre_b
    assert all(len(dq) == 1 for dq in limiter._windows.values())


def test_check_unrelated_node_does_not_register() -> None:
    """Fail-open path must not grow the internal dict.

    When max_calls <= 0, every check() returns True without
    touching ``_windows`` — a misconfigured server doesn't leak
    memory for every new node name.
    """
    limiter = _RateLimiter(max_calls=0)
    for _ in range(100):
        assert limiter.check("node-a") is True
    # Internal state should be untouched.
    assert limiter.tracked_nodes() == set()


def test_check_max_calls_zero_warns_at_construction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The fail-open warning is logged once at construction time.

    A silent fail-open would defeat the limit for an operator
    who typo'd ``HERMES_NODES_RATE_LIMIT=oops`` (which raises
    at config load, but a ``=0`` typo would silently disable).
    """
    with caplog.at_level(logging.WARNING, logger="hermes_nodes_plugin.ratelimit"):
        _RateLimiter(max_calls=0)
    assert any(
        "failing open" in record.message for record in caplog.records
    ), f"expected fail-open warning, got: {[r.message for r in caplog.records]}"


def test_check_window_seconds_must_be_positive() -> None:
    """A non-positive window is a programming error, not a config knob.

    The window length is the spec's "per second"; a 0 or negative
    window would make the limiter reject every call (or accept
    every call, depending on the comparator). Reject at construction.
    """
    with pytest.raises(ValueError, match="window_seconds must be > 0"):
        _RateLimiter(max_calls=10, window_seconds=0)
    with pytest.raises(ValueError, match="window_seconds must be > 0"):
        _RateLimiter(max_calls=10, window_seconds=-1.0)


def test_reset_clears_all_windows() -> None:
    """``reset()`` is the test-only escape hatch between cases."""
    clock = _FakeClock(0.0, 0.0)
    limiter = _RateLimiter(max_calls=1, window_seconds=1.0, clock=clock)
    limiter.check("node-a")
    assert limiter.tracked_nodes() == {"node-a"}
    limiter.reset()
    assert limiter.tracked_nodes() == set()


# ---------------------------------------------------------------------------
# Config-layer tests — env precedence and validation
# ---------------------------------------------------------------------------


def test_node_server_config_default_rate_limit() -> None:
    """The default 100-cps cap is the spec value, baked in."""
    assert NodeServerConfig().rate_limit_per_node == DEFAULT_RATE_LIMIT_PER_NODE


def test_load_config_env_overrides_default() -> None:
    """``HERMES_NODES_RATE_LIMIT=200`` beats the dataclass default."""
    cfg = load_config(env={"HERMES_NODES_RATE_LIMIT": "200"})
    assert cfg.rate_limit_per_node == 200


def test_load_config_zero_is_allowed() -> None:
    """``=0`` is the operator's "disable the limit" escape hatch.

    It is *not* a config error. The limiter itself logs the
    fail-open warning at construction time.
    """
    cfg = load_config(env={"HERMES_NODES_RATE_LIMIT": "0"})
    assert cfg.rate_limit_per_node == 0


def test_load_config_non_integer_raises_config_error() -> None:
    """A typo at config-load time raises, never silently disables."""
    from hermes_nodes_plugin.errors import ConfigError

    with pytest.raises(ConfigError, match="rate_limit_per_node must be an integer"):
        load_config(env={"HERMES_NODES_RATE_LIMIT": "fast"})


def test_load_config_negative_is_allowed() -> None:
    """Negative values mean "disabled" (same as 0)."""
    cfg = load_config(env={"HERMES_NODES_RATE_LIMIT": "-1"})
    assert cfg.rate_limit_per_node == -1


def test_load_config_empty_string_falls_back_to_default() -> None:
    """``export HERMES_NODES_RATE_LIMIT=`` is the same as unset."""
    cfg = load_config(env={"HERMES_NODES_RATE_LIMIT": ""})
    assert cfg.rate_limit_per_node == DEFAULT_RATE_LIMIT_PER_NODE


# ---------------------------------------------------------------------------
# Integration tests — through the WSS dispatch loop
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def store(tmp_path, fernet_key: str) -> TokenStore:
    return TokenStore(path=tmp_path / "tokens.json", key=fernet_key)


@pytest.fixture
def registry() -> NodeRegistry:
    return NodeRegistry()


@pytest.fixture
def low_cap_limiter() -> _RateLimiter:
    """A 3-cps cap so the boundary test sends 4 messages, not 101.

    The clock is a :class:`_FakeClock` with timestamps all inside
    the same 1s window. Without a fake clock, a stalled CI runner
    could age out the window between the 3rd and 4th send and the
    boundary test would fail spuriously. The fixture is shared by
    tests that make different numbers of calls (the close-path
    test sends 4 pongs; the per-node test sends 8 across two
    nodes), so the clock is provisioned with enough ticks to
    cover the busiest case (16 — well over what the 8-call test
    needs, with all timestamps inside the 1s window so the rate
    limit still fires on the 4th call).
    """
    return _RateLimiter(
        max_calls=3,
        window_seconds=1.0,
        clock=_FakeClock(*[round(0.01 * i, 3) for i in range(16)]),
    )


@pytest.fixture
def low_cap_client(
    store: TokenStore,
    registry: NodeRegistry,
    low_cap_limiter: _RateLimiter,
) -> Iterator[TestClient]:
    """TestClient wired to a 3-cps cap.

    The default 100-cps cap would force the integration test to
    send 100+ messages to exercise the rejection path. With a
    3-cps cap we can do it in 4 round-trips and keep the test
    under a second. The unit tests above exercise the exact
    100/101 boundary directly on the limiter; this fixture only
    verifies the *wiring* (limiter consulted, error frame sent,
    close code right).
    """
    app = create_app(
        token_store=store,
        registry=registry,
        config=NodeServerConfig(),
        rate_limiter=low_cap_limiter,
    )
    with TestClient(app) as c:
        yield c


def test_dispatch_loop_sends_rate_limit_frame_and_closes_4004(
    low_cap_client: TestClient,
    store: TokenStore,
) -> None:
    """4 calls in <1s with a 3-cps cap → call 4 is rejected.

    Drives a full handshake, then sends 4 application messages.
    The first 3 pass through ``_route_inbound``; the 4th trips
    the rate limiter and the server sends a ``rate_limit`` error
    frame and closes with 4004.
    """
    token = store.create("work-laptop")
    with low_cap_client.websocket_connect("/ws/nodes") as ws:
        ws.send_json(
            {
                "type": "hello",
                "protocol_version": "0.1.0",
                "node_name": "work-laptop",
            }
        )
        assert ws.receive_json()["type"] == "hello_ack"
        ws.send_json(
            {"type": "auth", "node_name": "work-laptop", "token": token}
        )
        # Drain auth_ok before sending application messages.
        assert ws.receive_json()["type"] == "auth_ok"

        # 4 application messages. The first 3 pass through
        # ``_route_inbound``; the 4th trips the rate limiter and
        # the server sends a ``rate_limit`` error frame and
        # closes with 4004. We use ``pong`` (an unknown/ignored
        # result type) so the dispatch loop counts them as
        # accepted inbound messages without needing a real
        # NodeEnvironment to consume them. The server doesn't
        # push anything for accepted pongs, so we only call
        # ``receive_json()`` once, after the 4th send, to pick
        # up the structured rate_limit frame.
        for i in range(4):
            ws.send_json({"type": "pong", "i": i})
        err = ws.receive_json()
        assert err["type"] == "rate_limit"
        assert err["reason"] == "rate_limit_exceeded"
        assert err["code"] == CLOSE_RATE_LIMIT_EXCEEDED
        assert err["node_name"] == "work-laptop"
        assert err["limit_per_second"] == 3

        # Subsequent receive surfaces the disconnect with 4004.
        with pytest.raises(Exception) as excinfo:
            ws.receive_json()
        assert getattr(excinfo.value, "code", None) == 4004


def test_dispatch_loop_rate_limit_is_per_node(
    store: TokenStore,
    low_cap_limiter: _RateLimiter,
) -> None:
    """A noisy node A hitting its cap must not block a quiet node B.

    This is the integration-test counterpart to the unit-level
    per-node isolation test. We open two WebSocket connections
    (one per node), exhaust node A's budget, then verify node B
    can still send messages.
    """
    app = create_app(
        token_store=store,
        registry=NodeRegistry(),
        config=NodeServerConfig(),
        rate_limiter=low_cap_limiter,
    )
    token_a = store.create("noisy-node")
    token_b = store.create("quiet-node")

    with TestClient(app) as client:
        # Exhaust node A's budget. Send 4 pongs; the 4th trips the
        # limiter and the server sends ``rate_limit`` + close 4004.
        with client.websocket_connect("/ws/nodes") as ws_a:
            ws_a.send_json(
                {
                    "type": "hello",
                    "protocol_version": "0.1.0",
                    "node_name": "noisy-node",
                }
            )
            ws_a.receive_json()
            ws_a.send_json(
                {"type": "auth", "node_name": "noisy-node", "token": token_a}
            )
            assert ws_a.receive_json()["type"] == "auth_ok"
            for i in range(4):
                ws_a.send_json({"type": "pong", "i": i})
            err = ws_a.receive_json()
            assert err["type"] == "rate_limit"
            with pytest.raises(Exception) as excinfo:
                ws_a.receive_json()
            assert getattr(excinfo.value, "code", None) == 4004

        # Node B opens a fresh connection with a fresh budget.
        # The two nodes share the limiter instance but the
        # limiter keys on node_name, so A's budget exhaustion
        # does not bleed into B. B can also send 4 pongs; the
        # 4th is rejected with B's name in the error frame.
        with client.websocket_connect("/ws/nodes") as ws_b:
            ws_b.send_json(
                {
                    "type": "hello",
                    "protocol_version": "0.1.0",
                    "node_name": "quiet-node",
                }
            )
            ws_b.receive_json()
            ws_b.send_json(
                {"type": "auth", "node_name": "quiet-node", "token": token_b}
            )
            assert ws_b.receive_json()["type"] == "auth_ok"
            for i in range(4):
                ws_b.send_json({"type": "pong", "i": i})
            err = ws_b.receive_json()
            assert err["type"] == "rate_limit"
            assert err["node_name"] == "quiet-node"


def test_dispatch_loop_zero_cap_disables_limit(
    store: TokenStore,
    registry: NodeRegistry,
) -> None:
    """``HERMES_NODES_RATE_LIMIT=0`` disables the limit entirely.

    The limiter fails open and logs a warning at construction
    time. The server accepts every call; the connection stays
    open past the configured cap.
    """
    cfg = load_config(env={"HERMES_NODES_RATE_LIMIT": "0"})
    assert cfg.rate_limit_per_node == 0

    no_limiter = _RateLimiter(max_calls=cfg.rate_limit_per_node)
    assert no_limiter.is_fail_open()

    app = create_app(
        token_store=store,
        registry=registry,
        config=cfg,
        rate_limiter=no_limiter,
    )
    token = store.create("chatty-node")
    with TestClient(app) as client:
        with client.websocket_connect("/ws/nodes") as ws:
            ws.send_json(
                {
                    "type": "hello",
                    "protocol_version": "0.1.0",
                    "node_name": "chatty-node",
                }
            )
            ws.receive_json()
            ws.send_json(
                {"type": "auth", "node_name": "chatty-node", "token": token}
            )
            # Send 10 messages well past any cap; all should be
            # accepted (no rate_limit frame, no close). The server
            # doesn't push anything unsolicited, so the absence
            # of an exception during the 10 sends is the
            # assertion: if the limiter had tripped, one of these
            # sends would have closed the socket.
            for i in range(10):
                ws.send_json({"type": "pong", "i": i})


def test_create_app_uses_config_rate_limit_when_no_limiter_given(
    tmp_path,
    fernet_key: str,
) -> None:
    """The app factory seeds the limiter cap from config.

    When the caller does not inject a ``rate_limiter``, the
    factory builds a default from ``config.rate_limit_per_node``.
    This is the production path; we verify the wiring here so
    a refactor that drops the cap loses its 100-cps default
    loudly.
    """
    cfg = NodeServerConfig(rate_limit_per_node=50)
    app = create_app(
        token_store=TokenStore(path=tmp_path / "tokens.json", key=fernet_key),
        registry=NodeRegistry(),
        config=cfg,
    )
    assert app.state.rate_limiter.max_calls == 50


def test_create_app_injects_clock_into_default_limiter(
    tmp_path,
    fernet_key: str,
) -> None:
    """``create_app(clock=...)`` threads the clock into the default limiter.

    When no ``rate_limiter`` is injected, the factory builds one
    from ``config.rate_limit_per_node`` and the ``clock`` parameter.
    A custom clock should be observable on the resulting limiter's
    recorded timestamps, so the integration tests can drive the
    sliding window deterministically. Default behaviour (no clock
    injected) keeps the real ``time.monotonic``.
    """
    cfg = NodeServerConfig(rate_limit_per_node=3)
    store = TokenStore(path=tmp_path / "tokens.json", key=fernet_key)

    # 1. Custom clock is observable: the limiter's stored timestamps
    #    come from the fake clock, not from time.monotonic().
    fake = _FakeClock(0.0, 0.1, 0.2, 0.3)
    app = create_app(
        token_store=store,
        registry=NodeRegistry(),
        config=cfg,
        clock=fake,
    )
    limiter = app.state.rate_limiter
    assert isinstance(limiter, _RateLimiter)
    # 3 allowed calls consume 3 ticks from the fake clock. The
    # 4th tick stays in reserve. The recorded window contains
    # exactly those 3 fake-clock timestamps — proof that the seam
    # is plumbed all the way from ``create_app(clock=...)`` through
    # to ``_RateLimiter.check``.
    assert limiter.check("node-a") is True
    assert limiter.check("node-a") is True
    assert limiter.check("node-a") is True
    assert limiter.snapshot("node-a") == (0.0, 0.1, 0.2)

    # 2. Default clock is real time.monotonic. We don't measure it
    #    (that would be flaky); we just assert the parameter is
    #    optional and the construction path doesn't blow up.
    app2 = create_app(
        token_store=store,
        registry=NodeRegistry(),
        config=cfg,
    )
    limiter2 = app2.state.rate_limiter
    assert isinstance(limiter2, _RateLimiter)
    assert limiter2.max_calls == 3
