"""Smoke tests for the hermes-nodes-plugin package skeleton.

Confirms:
  * The package imports cleanly and exposes ``register``.
  * ``register(ctx)`` accepts a mock context (anything with attributes) and
    returns ``None`` (the loader treats non-``None`` returns as a load error
    in some code paths, so the contract is "return None").
  * The ``hermes_agent.plugins`` entry point resolves to the expected
    ``module:function`` target after ``pip install -e .``.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import types

import pytest


def test_package_imports_and_exposes_register():
    pkg = importlib.import_module("hermes_nodes_plugin")
    assert hasattr(pkg, "register"), "package must expose a register() function"
    assert callable(pkg.register), "register must be callable"
    assert pkg.__version__ == "0.1.0"


def test_register_accepts_a_mock_context_and_returns_none():
    pkg = importlib.import_module("hermes_nodes_plugin")

    class MockCtx:
        # The stub doesn't touch the context, but verify duck-typed attr
        # access doesn't blow up — the real PluginContext has many more.
        manifest = None

        def register_tool(self, *a, **kw):
            return None

    result = pkg.register(MockCtx())
    assert result is None, "register() must return None (loader contract)"


def test_register_is_idempotent():
    """Calling register() multiple times should be safe — the stub no-ops."""
    pkg = importlib.import_module("hermes_nodes_plugin")

    class MockCtx:
        pass

    for _ in range(3):
        assert pkg.register(MockCtx()) is None


def test_hermes_plugins_entry_point_is_registered():
    """Verify the entry-point is actually discoverable via importlib.metadata.

    This only passes after ``pip install -e .`` has been run in the active
    environment. In a fresh checkout without an editable install, pytest
    will report this as a skip rather than a hard failure (the test runner
    can't always tell editable installs apart from sdist installs).
    """
    try:
        eps = importlib.metadata.entry_points()
        # Python 3.10+ SelectableGroups; older returns dict
        if hasattr(eps, "select"):
            group_eps = eps.select(group="hermes_agent.plugins")
        else:
            group_eps = eps.get("hermes_agent.plugins", [])  # type: ignore[union-attr]

        target = None
        for ep in group_eps:
            if ep.name == "hermes_nodes_plugin":
                target = ep
                break

        if target is None:
            pytest.skip(
                "hermes_nodes_plugin entry point not found in this environment — "
                "run `pip install -e .` first"
            )

        # Target the module, not `module:register` — Hermes's plugin loader
        # does `module = ep.load(); getattr(module, "register", None)` and
        # `ep.load()` on a `module:function` target returns the function, not
        # the module, which would break the loader silently.
        assert target.value == "hermes_nodes_plugin", (
            f"entry point target should be 'hermes_nodes_plugin' (the module), got {target.value!r}"
        )

        # Lock-in: the loaded object must be the module itself, not the
        # register function. Regression guard against accidentally switching
        # to the `module:register` form, which would silently break Hermes's
        # plugin loader (it expects a module, not a function).
        loaded = target.load()
        assert isinstance(loaded, types.ModuleType), (
            f"entry point must load a module, got {type(loaded).__name__!r} — "
            f"this means the entry-point target is `module:function` form, "
            f"which Hermes's loader can't handle"
        )
        assert hasattr(loaded, "register"), (
            "loaded module is missing a register() function"
        )
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("hermes-nodes-plugin not installed in this environment")
