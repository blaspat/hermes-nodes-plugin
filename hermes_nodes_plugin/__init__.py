"""hermes-nodes-plugin: Hermes Agent plugin for remote node control.

Pairs with the ``hermes-nodes`` Go binary. The plugin turns Kate (or any
Hermes agent) into a "brain" that can exec / read / write on paired
remote nodes over an authenticated WSS connection.

See ``REQUIREMENTS.md`` for the full spec and ``../README.md`` for usage.
"""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:
    """Hermes plugin entry point.

    Called once by ``hermes_cli.plugins.PluginManager._load_plugin`` after the
    package is loaded via the ``hermes_agent.plugins`` entry-point group.

    Args:
        ctx: A :class:`hermes_cli.plugins.PluginContext` facade exposing
            ``register_tool``, ``register_hook``, ``register_cli_command``,
            ``register_slash_command``, and ``inject_message`` helpers. Real
            tool/hook wiring lands in later tasks (Task 2.2+).

    Notes:
        This stub intentionally does nothing. A failing ``register`` would
        brick plugin load, so we keep it defensive: any unexpected exception
        here is swallowed by the loader (see ``_load_plugin``), but we still
        keep this body empty until we have something real to register.
    """
    return None
