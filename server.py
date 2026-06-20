"""hermes_nodes_plugin.server: re-exported from hermes_nodes_plugin.wsserver.

This module exists as a backwards-compatible shim. All functionality
has moved to the ``wsserver`` subpackage:

* ``wsserver.server``   — FastAPI app factory, handshake, HTTP dispatch.
* ``wsserver.handlers`` — inbound message routing and waiter completion.

Existing imports (e.g. ``from .server import create_app``) are still
supported and resolve to the new subpackage.
"""

from __future__ import annotations

# Re-export everything from the new subpackage so existing importers
# (lifecycle.py, __init__.py, tests) don't need to change.
from .wsserver.server import (
    CLOSE_AUTH_FAILED,
    CLOSE_MESSAGE_OUT_OF_ORDER,
    CLOSE_PROTOCOL_VERSION,
    CLOSE_RATE_LIMIT_EXCEEDED,
    PROTOCOL_MAJOR,
    create_app,
    run_server,
)
from .wsserver.server import _safe_close

__all__ = [
    "create_app",
    "run_server",
    "CLOSE_AUTH_FAILED",
    "CLOSE_PROTOCOL_VERSION",
    "CLOSE_MESSAGE_OUT_OF_ORDER",
    "CLOSE_RATE_LIMIT_EXCEEDED",
    "PROTOCOL_MAJOR",
    "_safe_close",
]
