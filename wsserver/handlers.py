"""Inbound message routing for the WSS node server.

Exports :func:`route_inbound`, which is called from the WebSocket
connection handler in :mod:`wsserver.server` every time a JSON message
arrives from a paired node.

Also exports the close codes (shared with the handshake module) so
handlers can close the WebSocket on rate-limit violations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..registry import NodeRegistry

logger = logging.getLogger(__name__)

# Close code reused for rate-limit violations (PROTOCOL §4 + issue #13).
CLOSE_RATE_LIMIT_EXCEEDED = 4004

# Result-shaped message types that get routed to a registered waiter.
# Anything outside this set is silently dropped (forward-compat).
_ROUTABLE_RESULT_TYPES = frozenset(
    {"exec_result", "read_result", "write_result", "error"}
)


async def route_inbound(
    registry: "NodeRegistry",
    node_name: str,
    raw: Any,
) -> None:
    """Dispatch one inbound JSON message to a registered waiter.

    Called from the server's connection handler for each message
    received over the authenticated WebSocket.

    Behaviour:
        * ``exec_result`` / ``read_result`` / ``write_result`` /
          ``error`` with a string ``id`` field → the matching waiter
          is resolved with the message body.
        * Unknown types (``pong``, future PROTOCOL additions) are
          ignored silently — the caller already counted them as
          heartbeat activity.
        * Malformed payloads (non-dict, missing ``type``, missing
          ``id``) are logged at WARNING; the connection stays open.

    Args:
        registry: The :class:`NodeRegistry` holding both the
            connection table and the pending-waiter map.
        node_name: The authenticated node name the message came from
            (the server vouches for it from the ``auth`` phase; we
            never trust a client-supplied ``node_name`` field here).
        raw: The decoded JSON payload from
            :meth:`WebSocket.receive_json`.
    """
    if not isinstance(raw, dict):
        logger.warning(
            "dropping non-dict inbound message from %r: %r", node_name, raw
        )
        return

    msg_type = raw.get("type")
    if not isinstance(msg_type, str):
        logger.warning(
            "dropping inbound message with non-string type from %r: %r",
            node_name,
            raw,
        )
        return

    if msg_type not in _ROUTABLE_RESULT_TYPES:
        # Not a result we route — could be ``pong``, an ``event``
        # notification, or a future PROTOCOL addition. Silently
        # ignore; the connection loop already counted it as heartbeat.
        return

    request_id = raw.get("id")
    if not isinstance(request_id, str) or not request_id:
        logger.warning(
            "dropping %s message without string id from %r: %r",
            msg_type,
            node_name,
            raw,
        )
        return

    resolved = await registry.complete_waiter(node_name, request_id, raw)
    if not resolved:
        logger.debug(
            "no waiter for %s on %r (id=%s); call may have timed out",
            msg_type,
            node_name,
            request_id,
        )


__all__ = [
    "route_inbound",
    "CLOSE_RATE_LIMIT_EXCEEDED",
]
