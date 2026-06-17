#!/usr/bin/env python3
"""
Hermes Nodes WSS server runner.

Starts the ServerRunner (hermes_nodes_plugin.lifecycle) in a daemon thread
with its own asyncio event loop.  The main thread just keeps the process
alive so systemd sees an active service.

Why a daemon thread instead of forking / subprocess?
  - In-process: shares the gateway's venv / pydantic-core bindings with
    no extra packaging.
  - No double-fork: systemd service type=simple works cleanly.
  - No separate virtualenv to maintain.

Usage:
    hermes-nodes-server.service runs this directly via ExecStart.
    Or for dev:  python scripts/run_server.py
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────

HERMES_HOME = Path.home() / ".hermes"
PLUGIN_DIR = HERMES_HOME / "plugins" / "hermes-nodes-plugin"
VENV_PYTHON = HERMES_HOME / "hermes-agent" / "venv" / "bin" / "python"

# Make sure the plugin is importable (it lives in PLUGIN_DIR)
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# ── logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("hermes-nodes-server")


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
            "Starting hermes-nodes server on %s:%s …",
            runner.host,
            runner.port,
        )

        # runner.start() is idempotent — safe to call on an already-running runner
        loop.run_until_complete(runner.start())
        logger.info(
            "hermes-nodes server is running on %s:%s [pid=%d]",
            runner.host,
            runner.port,
            _get_pid(),
        )

        # Block the thread — loop.run_forever() keeps the server alive
        loop.run_forever()

    except Exception as exc:
        logger.exception("hermes-nodes server failed to start: %s", exc)

    finally:
        # Give loop a chance to finish pending tasks, then close
        loop.close()
        logger.info("hermes-nodes server event loop closed")


def _get_pid() -> int:
    try:
        return __import__("os").getpid()
    except Exception:
        return 0


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    logger.info("hermes-nodes-server starting …")

    server_thread = threading.Thread(
        target=_run_server,
        name="hermes-nodes-server",
        daemon=True,  # systemd gets the main process exit; daemon thread dies with it
    )
    server_thread.start()

    # Main thread blocks — process stays alive as long as the thread is alive.
    # SIGTERM / SIGINT from systemd triggers _handle_signal → _shutdown.set()
    # and the process exits cleanly.
    while not _shutdown.is_set():
        time.sleep(1)

    logger.info("hermes-nodes-server stopped")


if __name__ == "__main__":
    # asyncio is only imported when needed (deferred from top-level import)
    import asyncio

    main()
