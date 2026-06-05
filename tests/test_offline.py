"""FR-3.4 offline-handling tests (Task 2.11).

Covers the contract on REQUIREMENTS.md §FR-3.4:

    "If Kate calls ``node_exec`` against a disconnected node, the
    call returns a structured error in under 2 seconds with this
    message: 'node \\'X\\' is not connected; check \\'hermes node
    list\\' to see its current state'."

The same path is exercised by ``node_read`` and ``node_write``
(they go through :class:`NodeEnvironment` too) — the spec'd
message is the right thing to surface for all three, and that's
what we assert here.

Two layers are tested:

1. **Environment layer** — :class:`NodeEnvironment` directly. This
   is where the actual message is constructed; if a future
   refactor moves the call elsewhere, this layer is the canary.

2. **Tool layer** — the Kate-facing ``node_exec`` / ``node_read`` /
   ``node_write`` wrappers. These don't catch the exception
   (they re-raise), so the message is the same; the test exists
   to lock the user-visible contract from the call site Kate
   actually hits.

Two "disconnected" shapes are tested:

* **Never-paired** — the registry has no record of the name.
  Treated identically to "dropped" by :meth:`NodeRegistry.get`
  (returns ``None``), so the assertion is the same.

* **Dropped** — the registry had a connection but it was
  removed. We register a fake :class:`NodeConnection` then
  unregister it, so the next ``get`` returns ``None`` exactly
  the way a real dropped WebSocket would.

Why a new file rather than more cases in ``test_environment.py``?
The FR-3.4 contract is small but high-visibility — the message
ends up in the agent's prompt and is part of the user-facing
ergonomics. Keeping these tests isolated makes the FR traceable
in the test report and gives a reviewer one place to read the
contract assertion.

Audit-writer convention
-----------------------

These tests use the default :class:`AuditWriter` (the env
constructor's default behaviour) — same as the pre-existing
``test_execute_raises_node_not_connected_for_unknown_target``
in :mod:`tests.test_environment`. The audit row's content is
noise for this contract; what matters is the exception, and
``AuditWriter.record`` is best-effort + non-blocking.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hermes_nodes_plugin.environment import NodeEnvironment
from hermes_nodes_plugin.errors import NodeNotConnectedError
from hermes_nodes_plugin.registry import (
    NodeConnection,
    NodeRegistry,
)
from hermes_nodes_plugin.tools import (
    node_exec,
    node_list,
    node_read,
    node_write,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# The spec'd message is the same regardless of which tool triggered
# the call. Keeping it as a single source of truth here means a
# reviewer can change the spec string in one place and re-run the
# whole contract test in one click.
EXPECTED_TEMPLATE = (
    "node {target!r} is not connected; "
    "check 'hermes node list' to see its current state"
)

# The target we exercise. We pick a realistic name so the
# rendered string is the same shape Kate would see in production.
TARGET = "work-laptop"
EXPECTED_MESSAGE = EXPECTED_TEMPLATE.format(target=TARGET)

# FR-3.4 budget: 2 seconds. We give ourselves a healthy margin
# (well under one second on a normal machine) but assert the bound
# is still observed. If this fails on a slow runner, *that* is a
# regression, not a flake.
MAX_LATENCY_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry() -> NodeRegistry:
    """A fresh :class:`NodeRegistry` per test.

    A test never depends on (or pollutes) the plugin's singleton
    runner state — same convention as the ``isolated_registry``
    fixture in :mod:`tests.test_tools`.
    """
    return NodeRegistry()


def _make_fake_connection(name: str) -> NodeConnection:
    """Return a :class:`NodeConnection` for ``name`` with a no-op websocket.

    Used by the "dropped" tests to populate the registry with a
    real :class:`NodeConnection`, which we then unregister. The
    websocket itself is never used (we never call ``execute`` —
    the test bails at the pre-flight check), so a bare ``object``
    is enough. The dataclass field is typed as ``WebSocket`` but
    Pyright is happy with ``object`` because the field is
    ``compare=False, repr=False`` and never inspected by the
    registry's lookup methods.
    """
    return NodeConnection(name=name, websocket=object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Environment layer — pre-flight check
# ---------------------------------------------------------------------------


class TestEnvironmentOfflineErrors:
    """:class:`NodeEnvironment` raises :class:`NodeNotConnectedError`
    with the FR-3.4 message whenever the target is not currently
    in the registry, regardless of whether it was ever paired.
    """

    # ---- execute() -------------------------------------------------------

    def test_execute_unknown_target_uses_spec_message(
        self, registry: NodeRegistry
    ) -> None:
        env = NodeEnvironment(TARGET, registry=registry)
        with pytest.raises(NodeNotConnectedError) as exc_info:
            asyncio.run(env.execute("echo hi"))
        assert str(exc_info.value) == EXPECTED_MESSAGE

    def test_execute_unknown_target_returns_under_two_seconds(
        self, registry: NodeRegistry
    ) -> None:
        env = NodeEnvironment(TARGET, registry=registry)

        start = time.monotonic()
        with pytest.raises(NodeNotConnectedError):
            asyncio.run(env.execute("echo hi"))
        elapsed = time.monotonic() - start

        assert elapsed < MAX_LATENCY_SECONDS, (
            f"pre-flight took {elapsed:.3f}s, FR-3.4 budget is "
            f"{MAX_LATENCY_SECONDS}s"
        )

    def test_execute_dropped_node_uses_same_message(
        self, registry: NodeRegistry
    ) -> None:
        """A name the registry *did* have, but whose connection
        was later removed, must surface the same message as a
        never-paired name. The user-facing reason is the same:
        the node isn't reachable right now."""
        # Register, then unregister. After this, ``get(TARGET)``
        # returns ``None`` — the same shape as "never paired".
        asyncio.run(registry.register(_make_fake_connection(TARGET)))
        removed = asyncio.run(registry.unregister(TARGET))
        assert removed is not None, "test setup: unregister should return the connection"

        env = NodeEnvironment(TARGET, registry=registry)
        with pytest.raises(NodeNotConnectedError) as exc_info:
            asyncio.run(env.execute("echo hi"))
        assert str(exc_info.value) == EXPECTED_MESSAGE

    # ---- read() ----------------------------------------------------------

    def test_read_unknown_target_uses_spec_message(
        self, registry: NodeRegistry
    ) -> None:
        env = NodeEnvironment(TARGET, registry=registry)
        with pytest.raises(NodeNotConnectedError) as exc_info:
            asyncio.run(env.read("/etc/hostname"))
        assert str(exc_info.value) == EXPECTED_MESSAGE

    def test_read_unknown_target_returns_under_two_seconds(
        self, registry: NodeRegistry
    ) -> None:
        env = NodeEnvironment(TARGET, registry=registry)

        start = time.monotonic()
        with pytest.raises(NodeNotConnectedError):
            asyncio.run(env.read("/etc/hostname"))
        elapsed = time.monotonic() - start

        assert elapsed < MAX_LATENCY_SECONDS, (
            f"read pre-flight took {elapsed:.3f}s, FR-3.4 budget is "
            f"{MAX_LATENCY_SECONDS}s"
        )

    # ---- write() ---------------------------------------------------------

    def test_write_unknown_target_uses_spec_message(
        self, registry: NodeRegistry
    ) -> None:
        env = NodeEnvironment(TARGET, registry=registry)
        with pytest.raises(NodeNotConnectedError) as exc_info:
            asyncio.run(env.write("/tmp/x", "hi"))
        assert str(exc_info.value) == EXPECTED_MESSAGE

    def test_write_unknown_target_returns_under_two_seconds(
        self, registry: NodeRegistry
    ) -> None:
        env = NodeEnvironment(TARGET, registry=registry)

        start = time.monotonic()
        with pytest.raises(NodeNotConnectedError):
            asyncio.run(env.write("/tmp/x", "hi"))
        elapsed = time.monotonic() - start

        assert elapsed < MAX_LATENCY_SECONDS, (
            f"write pre-flight took {elapsed:.3f}s, FR-3.4 budget is "
            f"{MAX_LATENCY_SECONDS}s"
        )


# ---------------------------------------------------------------------------
# Tool layer — Kate-facing wrappers
# ---------------------------------------------------------------------------


class TestToolOfflineErrors:
    """The Kate-facing ``node_*`` wrappers must propagate the
    env-layer error unchanged (they don't catch it; the contract
    is that the env's message is what reaches the agent).
    """

    @pytest.mark.asyncio
    async def test_node_exec_uses_spec_message(self, registry: NodeRegistry) -> None:
        with pytest.raises(NodeNotConnectedError) as exc_info:
            await node_exec(TARGET, "echo hi", registry=registry)
        assert str(exc_info.value) == EXPECTED_MESSAGE

    @pytest.mark.asyncio
    async def test_node_read_uses_spec_message(self, registry: NodeRegistry) -> None:
        with pytest.raises(NodeNotConnectedError) as exc_info:
            await node_read(TARGET, "/etc/hostname", registry=registry)
        assert str(exc_info.value) == EXPECTED_MESSAGE

    @pytest.mark.asyncio
    async def test_node_write_uses_spec_message(self, registry: NodeRegistry) -> None:
        with pytest.raises(NodeNotConnectedError) as exc_info:
            await node_write(TARGET, "/tmp/x", "hi", registry=registry)
        assert str(exc_info.value) == EXPECTED_MESSAGE

    @pytest.mark.asyncio
    async def test_node_exec_latency_under_two_seconds(
        self, registry: NodeRegistry
    ) -> None:
        start = time.monotonic()
        with pytest.raises(NodeNotConnectedError):
            await node_exec(TARGET, "echo hi", registry=registry)
        elapsed = time.monotonic() - start

        assert elapsed < MAX_LATENCY_SECONDS, (
            f"node_exec pre-flight took {elapsed:.3f}s, "
            f"FR-3.4 budget is {MAX_LATENCY_SECONDS}s"
        )

    @pytest.mark.asyncio
    async def test_node_exec_dropped_node_uses_spec_message(
        self, registry: NodeRegistry
    ) -> None:
        """Same shape as the env-layer dropped-node test, but
        going through the Kate-facing wrapper — proves the
        message reaches the agent layer for both 'never paired'
        and 'was paired, now dropped' cases."""
        await registry.register(_make_fake_connection(TARGET))
        removed = await registry.unregister(TARGET)
        assert removed is not None

        with pytest.raises(NodeNotConnectedError) as exc_info:
            await node_exec(TARGET, "echo hi", registry=registry)
        assert str(exc_info.value) == EXPECTED_MESSAGE

    @pytest.mark.asyncio
    async def test_node_list_excludes_dropped_node_but_includes_live(
        self, registry: NodeRegistry
    ) -> None:
        """``node_list`` is a read-only inventory call and must
        only report *live* connections. The FR-3.4 contract is
        about call-sites that *target* a node
        (``exec``/``read``/``write``); ``node_list`` is the
        diagnostic the spec message points the agent at, so it
        has to stay callable regardless of state, and its
        contents have to reflect current connectivity — not
        the registry's history of names it has ever seen.

        We register two fake nodes, then unregister one. The
        call must return exactly the *live* one. If the tool
        ever conflated "known name" with "connected", this
        test would catch it."""
        live_name = "live-laptop"
        dropped_name = TARGET  # "work-laptop" — same as the rest
        await registry.register(_make_fake_connection(live_name))
        await registry.register(_make_fake_connection(dropped_name))
        await registry.unregister(dropped_name)

        result = await node_list(registry=registry)
        assert result["count"] == 1
        assert [n["name"] for n in result["nodes"]] == [live_name]
