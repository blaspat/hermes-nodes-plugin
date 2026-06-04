"""Hermes ``BaseEnvironment`` implementation for paired remote nodes.

This is the interface Kate uses to run shell commands on a paired
``hermes-nodes`` Go binary over the WSS connection. It satisfies the
same contract as ``hermes_agent.tools.environments.base.BaseEnvironment``
— ``execute()`` returns ``{"output": str, "returncode": int}`` — so
the agent's tool layer can swap a local shell for a node shell without
any change in how the tool result is rendered.

Why an environment (not just a tool)?
-------------------------------------

Hermes environments are responsible for *how* a command is run, not
*which* command. The ``node_exec`` tool (Task 2.8) is a thin wrapper
around :class:`NodeEnvironment`; the environment does the actual
dispatch, the tool just exposes it to the agent. Having the dispatch
logic in an environment also means the same code path can be reused
by any other Hermes feature that already speaks the environment
contract (e.g. ``hermes``'s built-in terminal tool, if Patrick ever
wires it up).

Persistent shell semantics
--------------------------

Hermes expects ``cwd`` and ``env`` to persist across ``execute()``
calls within one environment instance (see
``tools/environments/local.py`` for the local equivalent, which
sources a session snapshot on every command). The remote node owns
that persistence: it runs a long-lived ``bash`` process and applies
``cwd`` / ``env`` updates to it. We do NOT snapshot env on the
client side — sending ``cwd`` and ``env`` overrides on every call
would defeat the persistence guarantee and double-bookkeep.

The protocol's ``exec`` message (PROTOCOL §3.6) makes both fields
optional. If the caller of :meth:`execute` doesn't pass a ``cwd``
override, we omit ``cwd`` from the wire payload and the node
continues in its current shell. Same for ``env`` (an empty dict
is treated as "no overrides"). This matches the principle: client
state is *minimal*, node state is *authoritative*.

Public surface (Task 2.7)
-------------------------

* :class:`NodeEnvironment` — the environment class. Construct one
  per target node; reuse it across calls.
* :class:`NodeNotConnectedError` / :class:`NodeExecutionError` —
  the two call-site exceptions; both come from
  :mod:`hermes_nodes_plugin.errors`.

The ``read`` and ``write`` methods on a node land in Task 2.8
(``node_read`` / ``node_write`` tools). They're one-liners over
the same dispatcher and were deliberately left out of this
commit to keep the scope tight.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Mapping

from fastapi import WebSocket

from hermes_nodes_plugin.errors import (
    NodeExecutionError,
    NodeNotConnectedError,
)
from hermes_nodes_plugin.registry import (
    NodeRegistry,
    _WaiterCancelled,
)

logger = logging.getLogger(__name__)


# Default timeout for a single ``exec`` call. Matches the protocol's
# default (``timeout_ms=60000`` per PROTOCOL §3.6). The brain can
# override per-call via ``execute(..., timeout=...)``.
DEFAULT_EXEC_TIMEOUT_SECONDS = 60.0


class NodeEnvironment:
    """Hermes environment that runs commands on a paired remote node.

    Args:
        target: The node name to dispatch commands to. Must match
            the name a node presented during its ``auth`` handshake
            (PROTOCOL §3.4). The constructor does NOT verify the
            node is currently connected — :meth:`execute` does
            that check on every call, so a long-lived
            :class:`NodeEnvironment` for a frequently-offline node
            doesn't blow up at construction time.
        registry: The :class:`NodeRegistry` to look the target up
            in. Defaults to a fresh registry per environment — that
            matches what the plugin's singleton lifecycle does
            (each ``get_default_runner`` gets its own registry) and
            keeps tests isolated. Production callers in Task 2.8
            will pass the shared registry explicitly.
        timeout: Default per-call timeout in seconds. Used when
            :meth:`execute`'s own ``timeout`` is ``None``. Defaults
            to 60s, matching the protocol's default ``timeout_ms``.

    Threading / concurrency:
        The environment is safe to share across coroutines in the
        same event loop. Each call to :meth:`execute` registers a
        fresh future in the registry, so concurrent calls to the
        same node fan out independently and the per-call
        :class:`asyncio.TimeoutError` from ``wait_for`` only
        cancels that one call (the future is removed from the
        registry in the cancel branch).

    Lifecycle:
        The base ``BaseEnvironment`` defines a ``cleanup()`` method.
        We don't hold any per-environment resources (the registry
        and WebSocket live in the server / registry), so
        :meth:`cleanup` is a no-op. ``BaseEnvironment`` also calls
        ``cleanup`` from ``__del__``; the no-op implementation is
        GC-safe.
    """

    def __init__(
        self,
        target: str,
        *,
        registry: NodeRegistry | None = None,
        timeout: float = DEFAULT_EXEC_TIMEOUT_SECONDS,
    ) -> None:
        if not target:
            raise ValueError("NodeEnvironment target must be a non-empty string")
        if timeout <= 0:
            raise ValueError(f"NodeEnvironment timeout must be > 0, got {timeout!r}")
        self._target = target
        self._registry = registry if registry is not None else NodeRegistry()
        self._timeout = float(timeout)

    # -- introspection (for tests + the eventual tool wrappers) -----------

    @property
    def target(self) -> str:
        """The node name this environment dispatches to."""
        return self._target

    @property
    def timeout(self) -> float:
        """The default per-call timeout in seconds."""
        return self._timeout

    # -- the actual interface ---------------------------------------------

    async def execute(
        self,
        command: str,
        cwd: str = "",
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run ``command`` on the target node and await the result.

        Matches the :class:`BaseEnvironment.execute` contract:
        returns ``{"output": str, "returncode": int}``. ``output``
        is the node's ``exec_result.stdout`` (with ``stderr``
        merged in on a non-zero exit, prefixed with a tag line —
        same convention the local backend uses so downstream
        rendering is uniform). ``returncode`` is the process
        exit code, or ``1`` if the node returned a protocol-level
        error (e.g. ``exec_failed`` / ``exec_timeout``).

        Args:
            command: The shell command to run. The node receives
                it as a single string and runs it through its
                persistent bash session (PROTOCOL §3.6). We do
                NOT split / re-quote on this side — the node is
                responsible for safe parsing.
            cwd: Optional directory to run in. If empty, the
                node's persistent shell keeps its current cwd.
                Per the plan: "persistent cwd + env maintained
                on the node (no client-side snapshot)".
            env: Optional mapping of env-var overrides merged
                into the node's persistent shell environment.
                Empty / ``None`` means "no overrides" — the
                node keeps its existing env. Per-call env
                overrides do NOT replace the persistent env;
                they are merged on top.
            timeout: Per-call timeout in seconds. Overrides the
                constructor's default. ``None`` (the default
                for the kwarg) means "use the constructor
                default".

        Raises:
            NodeNotConnectedError: The target node is not in the
                registry (never paired, dropped, or replaced).
                Raised immediately; no wire roundtrip.
            NodeExecutionError: The node returned a structured
                ``exec_result`` with ``status="error"`` or
                ``status="timeout"``. The exception's ``.code``
                attribute carries the protocol error code (e.g.
                3001 for ``exec_timeout``).
            asyncio.TimeoutError: The call did not complete
                within ``timeout`` seconds. The pending future
                is cancelled and the registry entry is cleaned
                up; the node connection is left alone (the
                call may still be running on the other side
                and will deliver its result to a now-orphan
                future that the dispatch loop will discard).
            RuntimeError: The WebSocket refused the outbound
                frame. The connection is most likely dead; the
                registry will catch up on the next inbound
                ``WebSocketDisconnect`` and unregister the
                node.
        """
        if not command:
            # Match local ``BaseEnvironment.execute``'s stance: an
            # empty command is a no-op that exits 0. Cheaper than
            # wasting a wire roundtrip.
            return {"output": "", "returncode": 0}

        effective_timeout = float(timeout) if timeout is not None else self._timeout

        # Step 1: confirm the node is connected. We do this *after*
        # generating the request_id and *before* registering the
        # waiter so a not-connected call doesn't leak a dict entry.
        conn = await self._registry.get(self._target)
        if conn is None:
            raise NodeNotConnectedError(
                f"node {self._target!r} is not connected; "
                "pair it (Task 2.10) or wait for the next heartbeat"
            )

        # Step 2: build the request envelope. PROTOCOL §3.6 says
        # ``id`` is server-generated (UUIDv4) and ``command`` is
        # required. ``cwd``, ``env``, ``timeout_ms`` are optional;
        # we omit them when the caller didn't supply values so
        # the node keeps its persistent state.
        request_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "type": "exec",
            "id": request_id,
            "command": command,
        }
        if cwd:
            payload["cwd"] = cwd
        if env:
            # The protocol expects a flat object (PROTOCOL §3.6).
            # We materialise the mapping into a dict so JSON
            # encoding is unambiguous.
            payload["env"] = dict(env)
        if effective_timeout:
            # Convert seconds → ms, matching the protocol's unit.
            # Round up so a 0.5s timeout becomes at least 1ms
            # rather than truncating to 0 and being ignored by
            # the node.
            payload["timeout_ms"] = max(1, int(effective_timeout * 1000))

        # Step 3: register the waiter, then send. The order
        # matters: we want the future in place *before* the
        # inbound dispatch loop can possibly see the response,
        # otherwise a fast node could race us. A single
        # ``await`` between ``register_waiter`` and the send is
        # enough — no other task can complete a future the
        # loop hasn't seen registered yet.
        future = await self._registry.register_waiter(self._target, request_id)
        try:
            await self._send(conn.websocket, payload)
        except Exception:
            # The send failed before the node ever saw the
            # request. Clean up the waiter so we don't leak a
            # dict entry that will never resolve.
            await self._registry.unregister_waiter(self._target, request_id)
            raise

        # Step 4: await the result, with timeout. The
        # ``wait_for`` cancel branch fires when ``effective_timeout``
        # elapses; we clean up the waiter so a late response
        # doesn't try to complete a future that the environment
        # has already given up on.
        try:
            start = time.monotonic()
            result = await asyncio.wait_for(future, timeout=effective_timeout)
            elapsed = time.monotonic() - start
        except asyncio.TimeoutError:
            await self._registry.unregister_waiter(self._target, request_id)
            logger.warning(
                "exec on %r (id=%s) timed out after %.1fs",
                self._target,
                request_id,
                effective_timeout,
            )
            raise
        except _WaiterCancelled as exc:
            # The node disconnected (or was replaced) mid-call.
            # The registry's ``unregister`` / ``register`` path
            # already removed the future, so we don't need to
            # unregister it again — but we do want to translate
            # the internal sentinel into the public exception.
            raise NodeNotConnectedError(
                f"node {self._target!r} disconnected mid-call ({exc.reason})"
            ) from exc

        # Step 5: decode the ``exec_result`` payload. The
        # dispatch loop in :mod:`server` has already validated
        # ``type`` and ``id``; we still need to enforce the
        # shape we expect (``exec_result``, status, exit_code,
        # stdout, stderr).
        return self._decode_exec_result(result, elapsed=elapsed)

    async def cleanup(self) -> None:
        """Release any per-environment resources. No-op for nodes.

        :class:`BaseEnvironment` requires this; we override only
        to make the intent explicit. The WebSocket lives in the
        server's connection handler; the registry is shared
        with the runner. Nothing here to close.
        """
        return None

    # Alias for older callers (and a friendlier name in stack
    # traces). Matches ``tools/environments/base.py:BaseEnvironment.stop``.
    async def stop(self) -> None:
        await self.cleanup()

    # -- helpers (test seams + protocol encoding) -------------------------

    async def _send(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        """Send ``payload`` as one JSON frame.

        Wrapped so tests can monkeypatch one method instead of
        stubbing ``WebSocket.send_json``. Also centralises the
        ``WebSocketDisconnect`` / ``RuntimeError`` contract —
        both indicate "the socket is gone" and we let them
        propagate; the caller's ``except Exception`` on the
        register-then-send path handles them.
        """
        await websocket.send_json(payload)

    @staticmethod
    def _decode_exec_result(result: Any, *, elapsed: float) -> dict[str, Any]:
        """Turn a raw ``exec_result`` payload into the BaseEnvironment shape.

        Contract (PROTOCOL §3.7):

        * ``status="ok"`` → ``returncode`` is the process exit
          code, ``output`` is ``stdout`` (with ``stderr``
          appended on a non-zero exit so the agent sees
          failures).
        * ``status="error"`` → the node hit a protocol-level
          error (e.g. malformed request). We raise
          :class:`NodeExecutionError` with the protocol's
          ``code`` and ``reason`` so callers can branch.
        * ``status="timeout"`` → the node killed the process
          because its own timer fired. We raise
          :class:`NodeExecutionError` with the protocol's
          ``code`` (3001) — the agent should treat this the
          same as a local timeout.

        A payload that doesn't match the expected shape
        (missing fields, wrong types) is treated as a
        :class:`NodeExecutionError` with a synthetic code of
        ``0`` and a reason explaining what was wrong. We
        prefer a structured error over a raw ``KeyError`` /
        ``TypeError`` because Kate's tool layer will display
        the message verbatim.
        """
        if not isinstance(result, dict):
            raise NodeExecutionError(
                f"node returned non-dict exec_result: {result!r}",
                code=0,
            )
        msg_type = result.get("type")
        if msg_type != "exec_result":
            # A different result type landed in our slot (read /
            # write / error). That's a server bug — the dispatch
            # loop routes by ``type``, so this shouldn't happen.
            # Treat as a structured error rather than crashing.
            raise NodeExecutionError(
                f"node returned {msg_type!r} where exec_result was expected",
                code=0,
            )

        status_value = result.get("status")
        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        exit_code = result.get("exit_code")
        truncated = bool(result.get("truncated", False))

        if status_value == "error":
            raise NodeExecutionError(
                f"node exec failed (code={result.get('code')!r}, "
                f"reason={result.get('reason')!r})",
                code=int(result.get("code") or 0),
            )
        if status_value == "timeout":
            # PROTOCOL §4 puts exec_timeout at 3001. The node may
            # or may not include ``code`` in the payload; we
            # default to 3001 when missing.
            raise NodeExecutionError(
                "node exec timed out before completing",
                code=int(result.get("code") or 3001),
            )

        # ``status == "ok"`` (and any unknown status we treat
        # defensively as ok, since the exit_code tells the real
        # story). ``exit_code`` is required for the local base
        # environment's contract; the protocol says it's 0 on
        # success / non-zero on error, so we default to ``0``
        # for a missing value rather than raising.
        rc = int(exit_code) if isinstance(exit_code, int) else 0

        # Mirror the local backend: merge stderr into output on
        # non-zero exit so the agent can see what went wrong
        # without rendering a second stream. Stdout-first keeps
        # the common case (success) clean.
        if rc != 0 and stderr:
            output = f"{stdout}\n[stderr]\n{stderr}"
            if not output.endswith("\n"):
                output += "\n"
        else:
            output = stdout

        if truncated:
            # Surface a hint line so the agent doesn't silently
            # act on partial output. Appended, not prepended,
            # so the bulk of the output keeps its original
            # shape (important for things like ``pytest`` that
            # parse their own tail).
            output += "\n[output truncated at 10MB]\n"

        # ``elapsed`` is the round-trip latency the environment
        # saw, *not* the node's ``duration_ms`` (which measures
        # only the bash subprocess). Useful for the agent's
        # per-call accounting and for tests.
        _ = elapsed  # currently only logged; kept on the signature
        # for forward-compat (a future revision may surface it
        # in the result dict).

        return {"output": output, "returncode": rc}


__all__ = ["NodeEnvironment", "DEFAULT_EXEC_TIMEOUT_SECONDS"]
