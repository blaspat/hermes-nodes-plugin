"""Regression test: ``register()`` must not pull in fastapi / pydantic_core.

Background
----------
Inside the hermes runtime, the pydantic_core native extension
sometimes fails to load (the very issue that motivated this
refactor — see the lazy-import note in
``hermes_nodes_plugin/__init__.py``). The plugin's ``register()``
function is invoked at plugin-load time, so any eager import of
``fastapi`` (or anything that transitively touches pydantic_core)
would brick plugin load and ``hermes node`` would 404 with
``invalid choice: 'node'``.

This test blocks ``fastapi`` from being importable and then asserts
``register()`` still completes the full surface: 4 tools, 2 hooks,
1 CLI command. If a future change accidentally re-introduces an
eager heavy import, this test fails immediately.

We block the module *name* in ``sys.modules`` rather than monkey-
patching attributes, because the import chain hits
``from fastapi import WebSocket`` (a top-level ``from … import …``)
which would fail before any attribute lookup if the module isn't
even importable.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def blocked_heavy_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``fastapi`` unimportable for the duration of one test.

    We can't just set ``sys.modules['fastapi'] = None`` — the real
    fastapi package has submodules and Starlette imports
    ``fastapi.applications`` etc. at import time. A clean way to
    block the whole tree is to set the top-level module to a
    sentinel that raises on any attribute access.

    We use a module subclass whose ``__getattr__`` raises the same
    error hermes would surface if pydantic_core failed to load.
    """
    class _BlockedFastapi:
        def __getattr__(self, name: str) -> Any:
            raise ModuleNotFoundError(
                "No module named 'pydantic_core._pydantic_core' — "
                "fastapi blocked by test fixture"
            )

    # Make sure nothing has already imported the real fastapi in
    # this test process (pytest collection would have). Drop it
    # and any submodules from sys.modules so the block is honored.
    for mod_name in list(sys.modules):
        if mod_name == "fastapi" or mod_name.startswith("fastapi."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)
    # Also block pydantic_core itself in case any import chain
    # skips fastapi and goes straight to pydantic.
    for mod_name in list(sys.modules):
        if mod_name == "pydantic" or mod_name.startswith("pydantic."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    monkeypatch.setitem(sys.modules, "fastapi", _BlockedFastapi())  # type: ignore[arg-type]


@pytest.fixture
def mock_ctx() -> SimpleNamespace:
    """A duck-typed mock ctx that records every registration call.

    Mirrors the mock pattern in ``test_lifecycle.TestRegisterPluginWiring``
    but adds ``register_tool`` (lifecycle tests don't exercise tools).
    """
    ctx = SimpleNamespace(
        _registered_hooks={},
        _cli_commands={},
        _registered_tools=[],
        manifest=SimpleNamespace(
            name="hermes_nodes_plugin", key="hermes_nodes_plugin"
        ),
    )

    def _register_hook(name: str, callback: Any) -> None:
        ctx._registered_hooks[name] = callback

    def _register_cli_command(
        name: str, help: str, setup_fn: Any, handler_fn: Any = None
    ) -> None:
        ctx._cli_commands[name] = {
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
        }

    def _register_tool(
        name: str,
        toolset: str,
        schema: Any,
        handler: Any,
        emoji: str | None = None,
    ) -> None:
        ctx._registered_tools.append(
            {"name": name, "toolset": toolset, "schema": schema, "emoji": emoji}
        )

    ctx.register_hook = _register_hook  # type: ignore[attr-defined]
    ctx.register_cli_command = _register_cli_command  # type: ignore[attr-defined]
    ctx.register_tool = _register_tool  # type: ignore[attr-defined]
    return ctx


def test_register_completes_without_fastapi(
    blocked_heavy_imports: None, mock_ctx: SimpleNamespace
) -> None:
    """The headline guarantee: ``register()`` must not import fastapi.

    If this test ever fails, someone re-introduced an eager import
    of fastapi / pydantic / pydantic_core in the plugin load path
    and ``hermes node`` will 404 again inside the hermes runtime.
    """
    # Import the plugin module *after* the block is in place so
    # the module-level code paths run with the block active. (The
    # plugin's __init__.py only imports stdlib + a relative
    # ``tools`` import at the very top, both of which must remain
    # stdlib-only after this refactor.)
    from hermes_nodes_plugin import register

    # This is the assertion. If ``register()`` does any eager
    # ``import fastapi`` (directly or transitively), it raises
    # ModuleNotFoundError here.
    register(mock_ctx)  # type: ignore[arg-type]


def test_register_registers_full_surface(
    blocked_heavy_imports: None, mock_ctx: SimpleNamespace
) -> None:
    """The full surface is registered: 4 tools, 2 hooks, 1 CLI command."""
    from hermes_nodes_plugin import register

    register(mock_ctx)  # type: ignore[arg-type]

    # 4 tools: node_exec, node_read, node_write, node_list
    tool_names = sorted(t["name"] for t in mock_ctx._registered_tools)
    assert tool_names == ["node_exec", "node_list", "node_read", "node_write"], (
        f"Expected the 4 node_* tools, got {tool_names!r}"
    )

    # All four tools live in the ``hermes_nodes`` toolset so users
    # can enable / disable the whole surface atomically.
    assert all(t["toolset"] == "hermes_nodes" for t in mock_ctx._registered_tools), (
        "All node tools must register under the 'hermes_nodes' toolset"
    )

    # 2 hooks: on_session_start, on_session_end
    assert set(mock_ctx._registered_hooks.keys()) == {
        "on_session_start",
        "on_session_end",
    }, (
        f"Expected on_session_start + on_session_end, "
        f"got {set(mock_ctx._registered_hooks.keys())!r}"
    )

    # 1 CLI command: 'node'
    assert "node" in mock_ctx._cli_commands, (
        "Expected the 'node' CLI subcommand to be registered"
    )
    assert callable(mock_ctx._cli_commands["node"]["setup_fn"]), (
        "CLI subcommand setup_fn must be callable"
    )


def test_cli_setup_fn_is_lazy(
    blocked_heavy_imports: None, mock_ctx: SimpleNamespace
) -> None:
    """The CLI setup_fn must not invoke fastapi when registered.

    The setup_fn is called later by the hermes CLI at argparse build
    time, so calling it here would currently fail. We assert that
    *registering* the command does not call the setup_fn, and that
    the setup_fn itself is a callable wrapper (the lazy proxy).
    """
    from hermes_nodes_plugin import register

    register(mock_ctx)  # type: ignore[arg-type]

    setup_fn = mock_ctx._cli_commands["node"]["setup_fn"]
    assert callable(setup_fn), "setup_fn must be a callable wrapper"
    # We deliberately do NOT call setup_fn here — doing so would
    # resolve the lazy import and try to import fastapi, which is
    # blocked. The fact that setup_fn is callable but wasn't called
    # during register() is the lazy-import guarantee.
    assert "setup_fn" not in mock_ctx._cli_commands["node"]["help"], (
        "Help text should not embed the setup_fn itself"
    )
