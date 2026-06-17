"""hermes-nodes-plugin: Hermes Agent plugin for remote node control.

Pairs with the ``hermes-nodes`` Go binary. The plugin turns Agent (or any
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
      (Task 2.10). Guarded separately so a missing or broken CLI
      registration cannot block tool registration.
    * **Agent tools** (Task 2.8) — registers ``node_exec``,
      ``node_read``, ``node_write``, ``node_list`` via
      ``ctx.register_tool``. The tool bodies live in
      :mod:`hermes_nodes_plugin.tools`; this file just hands them
      to the host registry.

    Args:
        ctx: A :class:`hermes_cli.plugins.PluginContext` facade exposing
            ``register_tool``, ``register_hook``, and optionally
            ``register_cli_command`` helpers.

    Notes:
        **Isolation.** Hooks, CLI, and tools are each registered in
        their own ``try/except`` so a failure in one surface cannot
        prevent the others from loading.

        **Lazy imports.** The lifecycle module pulls in ``fastapi`` /
        ``uvicorn`` (and via :mod:`registry`, the ``pydantic_core``
        native extension) at import time. Inside the hermes runtime
        that native extension sometimes fails to load, which would
        brick plugin registration. To avoid that we resolve the
        lifecycle handlers lazily — the wrappers below do their real
        import at *call* time, so importing this module (and calling
        ``register``) stays on the stdlib-only path.
    """
    import logging

    log = logging.getLogger(__name__)

    # ------------------------------------------------------------------ #
    # 1. Lifecycle hooks                                                   #
    # ------------------------------------------------------------------ #
    # Thin wrappers that defer the lifecycle import until the gateway
    # actually fires the event. Keeps register() free of fastapi/pydantic.

    async def _on_session_start_lazy(session_id: str = "") -> None:
        from .lifecycle import _on_session_start
        await _on_session_start()

    async def _on_session_end_lazy(session_id: str = "") -> None:
        from .lifecycle import _on_session_end
        await _on_session_end()

    try:
        ctx.register_hook("on_session_start", _on_session_start_lazy)
        ctx.register_hook("on_session_end", _on_session_end_lazy)
    except Exception as exc:
        log.warning("hermes-nodes-plugin: hook registration failed: %s", exc)

    # ------------------------------------------------------------------ #
    # 2. CLI subcommand — guarded separately                               #
    # ------------------------------------------------------------------ #
    # register_cli_command does not exist on PluginContext in the current
    # Hermes version. Isolating it here means a missing or broken CLI
    # registration cannot prevent tools from loading (Bug fix: previously
    # the whole try/except shared one block so an AttributeError here
    # silently aborted tool registration too).

    def _setup_node_subcommand_lazy(subparser) -> None:
        from .cli import setup_node_cli
        setup_node_cli(subparser)
    
    def _node_handler_lazy(args) -> None:
        from .cli import node_command
        node_command(args)

    try:
        register_cli = getattr(ctx, "register_cli_command", None)
        if register_cli is not None:
            register_cli(
                "node",
                help=(
                    "Manage paired hermes-nodes (WSS node server). "
                    "Subcommands: pair, list, revoke, status."
                ),
                setup_fn=_setup_node_subcommand_lazy,
                handler_fn=_node_handler_lazy,
            )
        else:
            log.debug(
                "hermes-nodes-plugin: ctx.register_cli_command not available "
                "in this Hermes version — 'hermes node' subcommand skipped."
            )
    except Exception as exc:
        log.warning("hermes-nodes-plugin: CLI registration failed: %s", exc)

    # ------------------------------------------------------------------ #
    # 3. Agent tools                                                        #
    # ------------------------------------------------------------------ #
    # schemas.py — tool schemas (what the LLM reads)
    # tools.py  — tool handlers (what runs when called)
    # Separated per the Hermes plugin guide structure.

    try:
        from . import schemas, tools

        for name, schema in schemas.SCHEMAS.items():
            handler = getattr(tools, name)
            emoji = {
                "node_exec": "🖥️",
                "node_read": "📄",
                "node_write": "✍️",
                "node_list": "📋",
            }.get(name, "🔧")
            ctx.register_tool(
                name=name,
                toolset="hermes_nodes",
                schema=schema,
                handler=handler,
                emoji=emoji,
            )
    except Exception as exc:
        log.warning("hermes-nodes-plugin: tool registration failed: %s", exc)

    # ------------------------------------------------------------------ #
    # 4. Auto-start the WSS server                                         #
    # ------------------------------------------------------------------ #
    # Bring up the node server in a background daemon thread so it's
    # ready before the first user message. The thread runs its own event
    # loop so uvicorn can bind the port and stay alive. Any startup
    # error is logged but does not block plugin registration.
    #
    # Disabled in test/CI environments by setting the env var to ``0``.

    import os

    if os.environ.get("HERMES_NODES_AUTO_START", "1") == "1":
        # Quick socket check: if port 6969 is already bound, the server
        # is already running (e.g. from a previous gateway instance or
        # a test server). Skip auto-start to avoid "address in use" spam.
        import socket

        _port_free = True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 6969))
            s.close()
        except OSError:
            _port_free = False

        if _port_free:

            def _start_server() -> None:
                import asyncio
                import contextlib

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    with contextlib.suppress(Exception):
                        from .lifecycle import _on_session_start

                        loop.run_until_complete(_on_session_start())
                        log.info(
                            "hermes-nodes-plugin: WSS server started on port 6969"
                            " (background thread)",
                        )
                    # Keep the loop alive so uvicorn tasks continue running.
                    loop.run_forever()
                except Exception as exc:
                    log.warning(
                        "hermes-nodes-plugin: server background thread failed: %s",
                        exc,
                    )
                finally:
                    loop.close()

            try:
                import threading

                t = threading.Thread(
                    target=_start_server, daemon=True, name="hermes-nodes-wss"
                )
                t.start()
            except Exception as exc:
                log.warning(
                    "hermes-nodes-plugin: could not start server thread: %s", exc
                )
        else:
            log.debug(
                "hermes-nodes-plugin: port 6969 already bound — "
                "server likely already running. Skipping auto-start."
            )
    else:
        log.debug(
            "hermes-nodes-plugin: auto-start disabled (HERMES_NODES_AUTO_START=0)"
        )