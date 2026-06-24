#!/usr/bin/env python3
"""
Hermes Nodes WSS server runner.

Starts the ServerRunner (hermes_nodes_plugin.lifecycle) in a daemon thread
with its own asyncio event loop.  The main thread just keeps the process
alive.

Usage:
    python scripts/run_server.py
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time
import types
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────

HERMES_HOME = Path.home() / ".hermes"
PLUGIN_DIR = HERMES_HOME / "plugins" / "hermes-node-plugin"

# Register the hermes_nodes_plugin namespace in sys.modules so that
# "from hermes_nodes_plugin.lifecycle import ..." works with flat layout.
# This replicates what Hermes's _load_directory_module does when it
# loads the plugin as hermes_plugins.hermes_nodes_plugin.
_HERMES_PLUGINS_PARENT = "hermes_nodes_plugin"
_plugin_ns = types.ModuleType(_HERMES_PLUGINS_PARENT)
_plugin_ns.__path__ = [str(PLUGIN_DIR)]          # enables "from .X" relative imports
_plugin_ns.__package__ = _HERMES_PLUGINS_PARENT   # __package__ = "hermes_nodes_plugin"
_plugin_ns.__name__ = _HERMES_PLUGINS_PARENT
sys.modules[_HERMES_PLUGINS_PARENT] = _plugin_ns

# ── logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("hermes-node-server")


# ── signal handling ───────────────────────────────────────────────────────────

_shutdown = threading.Event()


def _handle_signal(signum, _frame):
    logger.info("Received signal %d — initiating shutdown", signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── server thread ─────────────────────────────────────────────────────────────


def _run_server() -> None:
    """
    Create a fresh asyncio event loop in a daemon thread and run the
    ServerRunner inside it.  This is the same pattern uvicorn uses
    internally — a dedicated loop for the ASGI server, run with
    loop.run_forever() so it never returns.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        from hermes_nodes_plugin.lifecycle import get_default_runner

        runner = get_default_runner()
        logger.info(
            "Starting hermes-node server on %s:%s …",
            runner.host,
            runner.port,
        )

        # runner.start() is idempotent — safe to call on an already-running runner
        loop.run_until_complete(runner.start())
        logger.info(
            "hermes-node server is running on %s:%s [pid=%d]",
            runner.host,
            runner.port,
            _get_pid(),
        )

        # Block the thread — loop.run_forever() keeps the server alive
        loop.run_forever()

    except Exception as exc:
        logger.exception("hermes-node server failed to start: %s", exc)

    finally:
        # Give loop a chance to finish pending tasks, then close
        loop.close()
        logger.info("hermes-node server event loop closed")


def _get_pid() -> int:
    try:
        return __import__("os").getpid()
    except Exception:
        return 0


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    logger.info("hermes-node-server starting …")

    server_thread = threading.Thread(
        target=_run_server,
        name="hermes-node-server",
        daemon=True,  # systemd gets the main process exit; daemon thread dies with it
    )
    server_thread.start()

    # Main thread blocks — process stays alive as long as the thread is alive.
    # SIGTERM / SIGINT from systemd triggers _handle_signal → _shutdown.set()
    # and the process exits cleanly.
    while not _shutdown.is_set():
        time.sleep(1)

    logger.info("hermes-node-server stopped")


if __name__ == "__main__":
    # asyncio is only imported when needed (deferred from top-level import)
    import asyncio

    main()
