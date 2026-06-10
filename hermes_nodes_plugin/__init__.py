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
    * **CLI subcommand** — registers ``hermes node pair`` /
      ``hermes node list`` / ``hermes node revoke`` /
      ``hermes node status`` via ``ctx.register_cli_command``
      (Task 2.10). The argparse tree lives in
      :mod:`hermes_nodes_plugin.cli`.
    * **Kate tools** (Task 2.8) — registers ``node_exec``,
      ``node_read``, ``node_write``, ``node_list`` via
      ``ctx.register_tool``. The tool bodies live in
      :mod:`hermes_nodes_plugin.tools`; this file just hands them
      to the host registry.

    Args:
        ctx: A :class:`hermes_cli.plugins.PluginContext` facade exposing
            ``register_tool``, ``register_hook``, ``register_cli_command``,
            ``register_slash_command``, and ``inject_message`` helpers.

    Notes:
        A failing ``register`` would brick plugin load, so the body is
        wrapped in a broad ``try/except``. We swallow-and-log (never
        raise) — the lifecycle callbacks themselves are also defensive.

    **Lazy imports.** The lifecycle module pulls in ``fastapi`` /
    ``uvicorn`` (and via :mod:`registry`, the ``pydantic_core`` native
    extension) at import time. Inside the hermes runtime that native
    extension sometimes fails to load, which would brick plugin
    registration. To avoid that we resolve the lifecycle handlers
    lazily — the wrappers below do their real import at *call* time,
    so importing this module (and calling ``register``) stays on
    the stdlib-only path.
    """
    # Hook callbacks: thin wrappers that defer the lifecycle import
    # until the gateway actually fires the event. This keeps
    # ``register()`` (and module import) free of fastapi / pydantic
    # so the plugin loads even when those native extensions are
    # unavailable in the host's import context.
    async def _on_session_start_lazy() -> None:
        from hermes_nodes_plugin.lifecycle import _on_session_start

        await _on_session_start()

    async def _on_session_end_lazy() -> None:
        from hermes_nodes_plugin.lifecycle import _on_session_end

        await _on_session_end()

    def _setup_node_subcommand_lazy(subparser) -> None:
        # Imported lazily so the argparse wiring only pulls cli (and
        # its downstream deps) when the operator actually invokes
        # ``hermes node ...``. The hermes CLI calls setup_fn when
        # building the parser tree, not at plugin-load time.
        from hermes_nodes_plugin.lifecycle import setup_node_subcommand

        setup_node_subcommand(subparser)

    try:
        ctx.register_hook("on_session_start", _on_session_start_lazy)
        ctx.register_hook("on_session_end", _on_session_end_lazy)
        ctx.register_cli_command(
            "node",
            help=(
                "Manage paired hermes-nodes (WSS node server). "
                "Subcommands land in Task 2.10; `status` is available now."
            ),
            setup_fn=_setup_node_subcommand_lazy,
            handler_fn=None,
        )
        # Kate tools (Task 2.8 / FR-3.2). Each entry in TOOLS is
        # (name, schema, handler, emoji); the toolset is the
        # plugin's own ("hermes_nodes") so users can enable /
        # disable the whole surface from their Hermes config.
        # tools.py is stdlib-only, so we can import it eagerly
        # without dragging fastapi/pydantic into plugin load.
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
