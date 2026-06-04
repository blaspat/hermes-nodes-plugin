"""Configuration loader for the hermes-nodes plugin.

Precedence (highest to lowest):

  1. Environment variables (``HERMES_NODES_*``).
  2. YAML config file at ``~/.hermes/hermes-nodes.yaml`` (path overridable
     via ``load_config(config_path=...)``).
  3. Built-in defaults baked into :class:`NodeServerConfig`.

Keys (file format matches env-var names, minus the ``HERMES_NODES_``
prefix and lowercased):

  * ``host``                      (str)   default ``"127.0.0.1"``
  * ``port``                      (int)   default ``6969``
  * ``tls_cert_path``             (str|None) default ``None``
  * ``tls_key_path``              (str|None) default ``None``
  * ``token_store_path``          (str)   default ``~/.hermes/nodes/tokens.json``
  * ``token_encryption_key_env``  (str)   default ``"HERMES_NODES_TOKEN_KEY"``
    — the *name* of the env var that holds the Fernet key, not the key itself.

Type coercion rules (applied uniformly to env and file values):

  * ``port`` is parsed via :func:`int`. A non-integer value raises
    :class:`ConfigError` with the offending key + value.
  * All other keys are strings (paths/hosts) or ``None``.

The loader never writes to disk and never imports optional deps beyond
``pyyaml`` (which is declared as a runtime dep). It is safe to call at
import time of the plugin.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_nodes_plugin.errors import ConfigError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("~/.hermes/hermes-nodes.yaml").expanduser()
DEFAULT_TOKEN_STORE_PATH = Path("~/.hermes/nodes/tokens.json").expanduser()

# Env-var prefix. Task spec only named a handful of vars; we map all six
# config keys uniformly to keep precedence rules easy to explain.
_ENV_PREFIX = "HERMES_NODES_"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeServerConfig:
    """Resolved configuration for the node WSS server.

    Construct via :func:`load_config` rather than directly; the loader is
    the only piece that knows about env-var / file precedence. Direct
    construction is supported for tests and for the rare caller that
    already has a fully-resolved mapping.
    """

    host: str = "127.0.0.1"
    port: int = 6969
    tls_cert_path: str | None = None
    tls_key_path: str | None = None
    token_store_path: str = str(DEFAULT_TOKEN_STORE_PATH)
    # Name of the env var that holds the Fernet key for the token store.
    # Not the key itself — see REQUIREMENTS.md FR-4.1/FR-4.2.
    token_encryption_key_env: str = "HERMES_NODES_TOKEN_KEY"

    def __post_init__(self) -> None:
        # TLS partial-config is the most common deployment footgun: an
        # operator sets tls_cert_path but forgets tls_key_path (or vice
        # versa), the server then fails to bind with a confusing ssl
        # error, and nobody knows why. Catch it at load time instead.
        if (self.tls_cert_path is None) != (self.tls_key_path is None):
            raise ConfigError(
                "TLS is partially configured: tls_cert_path and tls_key_path "
                "must both be set or both be unset. "
                f"Got tls_cert_path={self.tls_cert_path!r}, "
                f"tls_key_path={self.tls_key_path!r}."
            )

    # -- predicates ---------------------------------------------------------

    def uses_tls(self) -> bool:
        """True when the server should listen with TLS (direct mode).

        Reverse-proxied deployments (the default) leave both TLS paths
        unset and the plugin listens on plain HTTP behind nginx/Caddy.
        """
        return self.tls_cert_path is not None and self.tls_key_path is not None

    def is_loopback(self) -> bool:
        """True when the bind address is loopback-only (safe without TLS)."""
        return self.host in {"127.0.0.1", "::1", "localhost"}

    # -- env helpers --------------------------------------------------------

    def token_encryption_key(self, env: Mapping[str, str] | None = None) -> str | None:
        """Read the Fernet key from the env var named by ``token_encryption_key_env``.

        Returns ``None`` when the var is unset/empty. The caller (token
        store, server startup) is responsible for turning that ``None``
        into the user-facing "refuse to start" error per FR-4.2 — this
        method is intentionally permissive so tests can construct configs
        without a real key.
        """
        src = env if env is not None else os.environ
        return src.get(self.token_encryption_key_env) or None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _env_name(key: str) -> str:
    return f"{_ENV_PREFIX}{key.upper()}"


def _coerce_port(value: Any, *, source: str, key: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{source}: {key} must be an integer, got {value!r}") from exc
    if not (1 <= port <= 65535):
        raise ConfigError(f"{source}: {key} must be in 1..65535, got {port}")
    return port


def _coerce_optional_str(value: Any) -> str | None:
    """Treat ``None``, empty string, and YAML null as "unset"."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return str(value)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML config file. Missing file → empty dict (caller falls back to defaults)."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML config at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"YAML config at {path} must be a mapping at the top level, "
            f"got {type(data).__name__}"
        )
    return data


def _read_file_value(file_data: dict[str, Any], key: str) -> Any:
    """Look up a key in the YAML file, accepting both lower and upper case.

    File authors are split — some write ``host: 127.0.0.1`` (mirroring
    dataclass field names), others write ``HOST: 127.0.0.1`` (mirroring
    env-var names). We accept both so the operator doesn't have to think
    about it.
    """
    if key in file_data:
        return file_data[key]
    upper = key.upper()
    if upper in file_data:
        return file_data[upper]
    return None


def _resolve_str(
    *,
    key: str,
    env: Mapping[str, str],
    file_data: dict[str, Any],
) -> str | None:
    """Apply env > file precedence for a string field. Returns ``None`` if neither sets it."""
    env_name = _env_name(key)
    if env_name in env:
        return env[env_name]
    return _read_file_value(file_data, key)


def _build(
    *,
    env: Mapping[str, str],
    file_data: dict[str, Any],
) -> NodeServerConfig:
    """Apply precedence (env > file > dataclass default) per key.

    Strategy: for every key, compute ``raw`` (env beats file, else file,
    else dataclass default), then coerce through type-specific helpers.
    Doing it as one pass per key keeps precedence uniform and avoids
    the "env set tls_cert but file set tls_key" asymmetry surprises.
    """
    # -- host (str) ---------------------------------------------------------
    host = _resolve_str(key="host", env=env, file_data=file_data)
    if host is not None:
        host = str(host)

    # -- port (int) ---------------------------------------------------------
    port_raw: Any = None
    port_source = "default"
    if env.get(_env_name("port")) is not None:
        port_raw = env[_env_name("port")]
        port_source = "env"
    elif _read_file_value(file_data, "port") is not None:
        port_raw = _read_file_value(file_data, "port")
        port_source = "file"
    port: int | None = None
    if port_raw is not None:
        port = _coerce_port(port_raw, source=port_source, key="port")

    # -- TLS paths (optional str) ------------------------------------------
    tls_cert = _coerce_optional_str(
        _resolve_str(key="tls_cert_path", env=env, file_data=file_data)
    )
    tls_key = _coerce_optional_str(
        _resolve_str(key="tls_key_path", env=env, file_data=file_data)
    )

    # -- token store path (str) --------------------------------------------
    token_store_raw = _resolve_str(key="token_store_path", env=env, file_data=file_data)

    # -- token encryption key env var name (str) ---------------------------
    key_env_raw = _resolve_str(
        key="token_encryption_key_env", env=env, file_data=file_data
    )

    # Now assemble. We use a partial dict + NodeServerConfig defaults for
    # any key we didn't resolve — dataclass handles the "default" leg of
    # the precedence chain.
    resolved: dict[str, Any] = {}
    if host is not None:
        resolved["host"] = host
    if port is not None:
        resolved["port"] = port
    if tls_cert is not None:
        resolved["tls_cert_path"] = tls_cert
    if tls_key is not None:
        resolved["tls_key_path"] = tls_key
    if token_store_raw is not None:
        resolved["token_store_path"] = str(token_store_raw)
    if key_env_raw is not None:
        resolved["token_encryption_key_env"] = str(key_env_raw)

    return NodeServerConfig(**resolved)


def load_config(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
) -> NodeServerConfig:
    """Resolve a :class:`NodeServerConfig` from env, file, and defaults.

    Args:
        env: Override the env mapping (defaults to :data:`os.environ`).
            Tests use this to inject fixtures without touching the real
            process environment.
        config_path: Override the YAML file location (defaults to
            ``~/.hermes/hermes-nodes.yaml``). A missing file is *not* an
            error — the loader falls through to dataclass defaults.

    Returns:
        A fully-resolved, frozen :class:`NodeServerConfig`.

    Raises:
        ConfigError: YAML parse failure, bad port value, partial TLS
            config, or non-mapping top-level YAML.
    """
    if env is None:
        env = os.environ
    if config_path is None:
        path = DEFAULT_CONFIG_PATH
    else:
        path = Path(config_path).expanduser()

    file_data = _load_yaml(path)
    return _build(env=env, file_data=file_data)


# ---------------------------------------------------------------------------
# Convenience re-exports
# ---------------------------------------------------------------------------


__all__ = [
    "NodeServerConfig",
    "load_config",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_TOKEN_STORE_PATH",
    "ConfigError",
]
