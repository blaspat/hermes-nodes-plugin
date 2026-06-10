"""hermes-nodes-plugin: Hermes Agent plugin for remote node control.

Pairs with the ``hermes-nodes`` Go binary. The plugin turns Kate (or any
Hermes agent) into a "brain" that can exec / read / write on paired
remote nodes over an authenticated WSS connection.

See ``REQUIREMENTS.md`` for the full spec and ``../README.md`` for usage.
"""

from __future__ import annotations

__version__ = "0.1.0"

def _node_handler(args) -> None:
    """Dispatch parsed CLI args to the correct node subcommand handler.
    
    setup_node_subcommand calls subparser.set_defaults(func=<handler>)
    for each subcommand, so argparse wires the right function onto
    args.func. We just invoke it here.
    """
    func = getattr(args, "func", None)
    if func:
        func(args)
    else:
        print("Usage: hermes node <pair|list|revoke|status>")
        print("Run `hermes node --help` for details.")


def register(ctx) -> None:
    
    # Hooks — always register these first, they're safe
    ctx.register_hook("on_session_start", _on_session_start_lazy)
    ctx.register_hook("on_session_end", _on_session_end_lazy)

    # CLI — guard separately, failure here must not block tools
    try:
        register_fn = getattr(ctx, "register_cli_command", None)
        if register_fn is not None:
            register_fn(
                "node",
                help="Manage paired hermes-nodes (WSS node server).",
                setup_fn=_setup_node_subcommand_lazy,
                handler_fn=_node_handler,
            )
        else:
            import logging
            logging.getLogger(__name__).info(
                "hermes-nodes-plugin: ctx.register_cli_command not available "
                "in this Hermes version — 'hermes node' subcommand skipped."
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "hermes-nodes-plugin: CLI registration failed: %s", exc
        )

    # Tools — register unconditionally
    from hermes_nodes_plugin.tools import TOOLS
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="hermes_nodes",
            schema=schema,
            handler=handler,
            emoji=emoji,
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
