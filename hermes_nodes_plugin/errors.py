"""Plugin-internal exception types.

Centralised so callers (server, token store, CLI) can catch a single
hierarchy without importing deep modules. Public API: :class:`PluginError`
and its concrete subclasses.
"""

from __future__ import annotations


class PluginError(Exception):
    """Base class for all hermes-nodes-plugin errors."""


class ConfigError(PluginError):
    """Configuration is missing, malformed, or inconsistent.

    Examples: unparseable YAML, port out of range, partial TLS config
    (cert set but key missing), non-mapping top-level YAML.
    """


class TokenStoreError(PluginError):
    """Token store read/write/decrypt failure."""


class AuthError(PluginError):
    """Authentication failure on the WSS server (bad token, wrong node name)."""


class NodeNotConnectedError(PluginError):
    """A call was made to a node that's not currently in the registry.

    Surfaces to the caller of :meth:`NodeEnvironment.execute` (and
    its sibling ``read``/``write`` methods) when the target node
    name is unknown or its WebSocket has dropped. Distinct from
    :class:`NodeExecutionError` (the node is fine, but the *call*
    failed) and from :class:`asyncio.TimeoutError` (the node is
    fine, but the call didn't return in time).
    """


class NodeExecutionError(PluginError):
    """A node returned a structured ``exec_result`` with status=error.

    Carries the protocol-level reason and code so callers (Kate)
    can decide whether to retry, surface a user-visible message, or
    fall back to a different node. The node connection itself
    stays open; the failure is per-call.

    Attributes:
        code: The protocol error code (e.g. 3001 for
            ``exec_timeout``). May be 0 for shape violations the
            server couldn't categorise — the ``str(exc)`` message
            is the authoritative description in that case.
    """

    def __init__(self, message: str, *, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


__all__ = [
    "PluginError",
    "ConfigError",
    "TokenStoreError",
    "AuthError",
    "NodeNotConnectedError",
    "NodeExecutionError",
]
