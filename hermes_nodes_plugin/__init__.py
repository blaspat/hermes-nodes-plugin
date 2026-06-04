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

    Called once by ``hermes_cli.plugins.PluginManager._load_plugin`` after
    the package is loaded via the ``hermes_agent.plugins`` entry-point
    group.

    Wires the plugin's surface to the host:

    * **Lifecycle hooks** — ``on_session_start`` brings the WSS server
      up in the background; ``on_session_end`` drains it. Both are
      defensive: a misconfigured plugin (missing Fernet key, port
      collision) logs and skips rather than raising, so a broken
      plugin cannot take down the host. See
      :mod:`hermes_nodes_plugin.lifecycle` for the runner details.
    * **CLI subcommand** — registers ``hermes node`` so the surface
      appears once the plugin auto-loads. The full
      ``pair``/``list``/``revoke`` argparse tree lands in Task 2.10;
      a ``status`` stub is present now.

    Args:
        ctx: A :class:`hermes_cli.plugins.PluginContext` facade exposing
            ``register_tool``, ``register_hook``, ``register_cli_command``,
            ``register_slash_command``, and ``inject_message`` helpers.

    Notes:
        A failing ``register`` would brick plugin load, so the body is
        wrapped in a broad ``try/except``. We swallow-and-log (never
        raise) — the lifecycle callbacks themselves are also defensive.
    """
    from hermes_nodes_plugin.lifecycle import (
        _on_session_end,
        _on_session_start,
        setup_node_subcommand,
    )

    try:
        ctx.register_hook("on_session_start", _on_session_start)
        ctx.register_hook("on_session_end", _on_session_end)
        ctx.register_cli_command(
            "node",
            help=(
                "Manage paired hermes-nodes (WSS node server). "
                "Subcommands land in Task 2.10; `status` is available now."
            ),
            setup_fn=setup_node_subcommand,
            handler_fn=None,
        )
    except Exception as exc:  # pragma: no cover — defensive
        # Never let a wiring bug take down the host. The Hermes loader
        # does catch ``register`` exceptions and logs them, but a
        # partial registration (hooks done, CLI command raises) would
        # leave the plugin in a half-registered state that's harder
        # to diagnose. Swallow here, surface via the logger the
        # loader already configures.
        import logging

        logging.getLogger(__name__).warning(
            "hermes-nodes-plugin: register() encountered an error: %s", exc
        )
