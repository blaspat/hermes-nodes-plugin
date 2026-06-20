"""hermes_nodes_plugin.wsserver: WS node server.

Splits the server into two concerns:

* ``wsserver.server``  — FastAPI app factory, handshake logic, HTTP
  dispatch endpoints, and helpers.
* ``wsserver.handlers`` — inbound message routing (exec / read / write
  result handling and the waiter/future completion logic).
"""

from .server import create_app, run_server

__all__ = ["create_app", "run_server"]
