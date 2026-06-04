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


__all__ = [
    "PluginError",
    "ConfigError",
    "TokenStoreError",
    "AuthError",
]
