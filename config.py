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
  * ``audit_log_path``            (str)   default ``~/.hermes/logs/nodes-audit.log``
    — path to the append-only JSONL audit log (FR-5.1). The audit
    module also accepts ``HERMES_NODES_AUDIT_LOG_PATH`` as a
    direct override; see :mod:`hermes_nodes_plugin.audit`.
  * ``audit_retention_days``      (int)   default ``365``
    — how long rotated audit files are kept before being purged
    (FR-5.4). The audit module also accepts
    ``HERMES_NODES_AUDIT_RETENTION_DAYS`` as a direct override.
  * ``handshake_timeout_seconds`` (float) default ``10.0``
    — max time the server waits for the ``hello`` and ``auth``
    messages on the inbound handshake. A parked WSS is a trivial
    DoS (one coroutine + FD per open socket); bounding the wait
    caps the resource cost. On timeout the server sends a
    structured ``hello_err`` / ``auth_err`` (reason
    ``handshake_timeout``) and closes with 4004. Issue #13.

  * ``heartbeat_stale_seconds``   (int)   default ``60``
    — a node whose last inbound message is older than this is
    considered dead (PROTOCOL §6) and the runner's background
    sweep will close its WebSocket. Override with
    ``HERMES_NODES_HEARTBEAT_STALE_SECONDS``.
  * ``heartbeat_sweep_interval_seconds`` (int)   default ``30``
    — how often the runner's background sweep checks the
    registry for stale connections. Must be ``> 0``. Override
    with ``HERMES_NODES_HEARTBEAT_SWEEP_INTERVAL_SECONDS``.
  * ``rate_limit_per_node`` (int)   default ``100``
    — FR-2.6: max calls/second per node the WSS server will
    accept from a single connected node before closing with
    code 4004 ("Rate limit exceeded" in PROTOCOL §4). The
    limiter is a sliding 1-second window keyed on
    ``node_name``; a node that bursts past the cap is dropped
    with a structured ``rate_limit`` error frame. ``<= 0``
    disables the limiter (fail-open; the limiter logs a warning
    at construction so a typo'd ``=0`` is visible). A
    non-integer value is a config error. Override with
    ``HERMES_NODES_RATE_LIMIT`` (the env-var name is the spec
    literal; the dataclass field uses the longer name for
    clarity at call sites).

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

from .errors import ConfigError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path("~/.hermes/hermes-nodes.yaml").expanduser()
DEFAULT_TOKEN_STORE_PATH = Path("~/.hermes/nodes/tokens.json")
DEFAULT_TOKEN_STORE_STR = "~/.hermes/nodes/tokens.json"

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
    token_store_path: str = DEFAULT_TOKEN_STORE_STR
    # Name of the env var that holds the Fernet key for the token store.
    # Not the key itself — see REQUIREMENTS.md FR-4.1/FR-4.2.
    token_encryption_key_env: str = "HERMES_NODES_TOKEN_KEY"
    # Path to the append-only JSONL audit log (FR-5.1). The audit
    # module also accepts ``HERMES_NODES_AUDIT_LOG_PATH`` as a
    # direct override, which beats this value. The default is
    # duplicated as a literal here to avoid a circular import
    # (``audit`` imports this dataclass); keep in sync with
    # ``hermes_nodes_plugin.audit.DEFAULT_AUDIT_LOG_PATH``.
    audit_log_path: str = "~/.hermes/logs/nodes-audit.log"
    # Retention window for rotated audit files (FR-5.4). The audit
    # module also accepts ``HERMES_NODES_AUDIT_RETENTION_DAYS``.
    # See ``hermes_nodes_plugin.audit.DEFAULT_RETENTION_DAYS`` for
    # the source of truth (kept as a literal here to break the
    # ``audit`` ↔ ``config`` import cycle).
    audit_retention_days: int = 365
    # Handshake read timeout (issue #13). A single bound for both
    # the hello and the auth recv — they are two phases of the same
    # handshake, and splitting them invites an operator to set one
    # tight and the other lax. 10s is generous for a small envelope
    # on any plausible network. ``__post_init__`` enforces > 0.
    handshake_timeout_seconds: float = 10.0

    # PROTOCOL §6: a node is considered dead after this many seconds
    # without any inbound message. The runner's background sweep
    # closes the WebSocket of any node whose ``last_heartbeat`` is
    # older than this threshold. See issue #19.
    heartbeat_stale_seconds: int = 60
    # How often the runner's background sweep runs. Lower means
    # faster cleanup of dead nodes; higher means less registry
    # churn. Must be ``> 0``. See issue #19.
    heartbeat_sweep_interval_seconds: int = 30

    # FR-2.6 per-node sliding-window rate limit. A node whose
    # inbound call count within a 1-second window exceeds this
    # cap gets a ``rate_limit`` error frame and the WSS is closed
    # with 4004. The dispatcher reads this once at app
    # construction; the cap is in-process and resets on server
    # boot. ``<= 0`` disables the limiter (fail-open; the
    # limiter itself logs the disable). The dataclass does NOT
    # raise on ``<= 0`` because ``rate_limit_per_node=0`` is a
    # legitimate "unlimited" operator choice. Env var:
    # ``HERMES_NODES_RATE_LIMIT`` (spec-literal, NOT derived
    # from the field name).
    rate_limit_per_node: int = 100

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
        if self.audit_retention_days <= 0:
            raise ConfigError(
                f"audit_retention_days must be > 0, got {self.audit_retention_days!r}"
            )
        if self.handshake_timeout_seconds <= 0:
            raise ConfigError(
                f"handshake_timeout_seconds must be > 0, got "
                f"{self.handshake_timeout_seconds!r}"
            )

        if self.heartbeat_stale_seconds <= 0:
            raise ConfigError(
                f"heartbeat_stale_seconds must be > 0, got {self.heartbeat_stale_seconds!r}"
            )
        if self.heartbeat_sweep_interval_seconds <= 0:
            raise ConfigError(
                "heartbeat_sweep_interval_seconds must be > 0, "
                f"got {self.heartbeat_sweep_interval_seconds!r}"
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

    # -- audit log path (str) ----------------------------------------------
    audit_log_raw = _resolve_str(key="audit_log_path", env=env, file_data=file_data)

    # -- audit retention days (int) ----------------------------------------
    audit_retention_raw: Any = None
    audit_retention_source = "default"
    if env.get("HERMES_NODES_AUDIT_RETENTION_DAYS") is not None:
        audit_retention_raw = env["HERMES_NODES_AUDIT_RETENTION_DAYS"]
        audit_retention_source = "env"
    elif _read_file_value(file_data, "audit_retention_days") is not None:
        audit_retention_raw = _read_file_value(file_data, "audit_retention_days")
        audit_retention_source = "file"
    audit_retention: int | None = None
    if audit_retention_raw is not None:
        try:
            audit_retention = int(audit_retention_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{audit_retention_source}: audit_retention_days must be an integer, "
                f"got {audit_retention_raw!r}"
            ) from exc
        if audit_retention <= 0:
            raise ConfigError(
                f"{audit_retention_source}: audit_retention_days must be > 0, "
                f"got {audit_retention}"
            )

    # -- handshake timeout (float) -----------------------------------------
    # Issue #13. Bounded waits on the hello + auth recv, applied
    # uniformly through the standard env > file > default chain.
    handshake_timeout_raw: Any = None
    handshake_timeout_source = "default"
    if env.get(_env_name("handshake_timeout_seconds")) is not None:
        handshake_timeout_raw = env[_env_name("handshake_timeout_seconds")]
        handshake_timeout_source = "env"
    elif _read_file_value(file_data, "handshake_timeout_seconds") is not None:
        handshake_timeout_raw = _read_file_value(file_data, "handshake_timeout_seconds")
        handshake_timeout_source = "file"
    handshake_timeout: float | None = None
    if handshake_timeout_raw is not None:
        try:
            handshake_timeout = float(handshake_timeout_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{handshake_timeout_source}: handshake_timeout_seconds must be a number, "
                f"got {handshake_timeout_raw!r}"
            ) from exc
        if handshake_timeout <= 0:
            raise ConfigError(
                f"{handshake_timeout_source}: handshake_timeout_seconds must be > 0, "
                f"got {handshake_timeout}"
            )

    # -- heartbeat stale seconds (int) -------------------------------------
    # PROTOCOL §6: a node is dead after this many seconds without any
    # inbound message. The runner's background sweep (issue #19) uses
    # this to decide who's stale.
    heartbeat_stale_raw: Any = None
    heartbeat_stale_source = "default"
    if env.get("HERMES_NODES_HEARTBEAT_STALE_SECONDS") is not None:
        heartbeat_stale_raw = env["HERMES_NODES_HEARTBEAT_STALE_SECONDS"]
        heartbeat_stale_source = "env"
    elif _read_file_value(file_data, "heartbeat_stale_seconds") is not None:
        heartbeat_stale_raw = _read_file_value(file_data, "heartbeat_stale_seconds")
        heartbeat_stale_source = "file"
    heartbeat_stale: int | None = None
    if heartbeat_stale_raw is not None:
        try:
            heartbeat_stale = int(heartbeat_stale_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{heartbeat_stale_source}: heartbeat_stale_seconds must be an integer, "
                f"got {heartbeat_stale_raw!r}"
            ) from exc
        if heartbeat_stale <= 0:
            raise ConfigError(
                f"{heartbeat_stale_source}: heartbeat_stale_seconds must be > 0, "
                f"got {heartbeat_stale}"
            )

    # -- heartbeat sweep interval seconds (int) ----------------------------
    # How often the background sweep runs. Decoupled from the stale
    # threshold so operators can tune cleanup latency independently
    # of the dead-node definition.
    sweep_interval_raw: Any = None
    sweep_interval_source = "default"
    if env.get("HERMES_NODES_HEARTBEAT_SWEEP_INTERVAL_SECONDS") is not None:
        sweep_interval_raw = env["HERMES_NODES_HEARTBEAT_SWEEP_INTERVAL_SECONDS"]
        sweep_interval_source = "env"
    elif _read_file_value(file_data, "heartbeat_sweep_interval_seconds") is not None:
        sweep_interval_raw = _read_file_value(file_data, "heartbeat_sweep_interval_seconds")
        sweep_interval_source = "file"
    sweep_interval: int | None = None
    if sweep_interval_raw is not None:
        try:
            sweep_interval = int(sweep_interval_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{sweep_interval_source}: heartbeat_sweep_interval_seconds must be an integer, "
                f"got {sweep_interval_raw!r}"
            ) from exc
        if sweep_interval <= 0:
            raise ConfigError(
                f"{sweep_interval_source}: heartbeat_sweep_interval_seconds must be > 0, "
                f"got {sweep_interval}"
            )

    # -- rate limit per node (int, FR-2.6) --------------------------------
    # Spec literal env-var name ``HERMES_NODES_RATE_LIMIT`` (NOT
    # derived from the dataclass field name ``rate_limit_per_node``).
    # ``<= 0`` is a legitimate "unlimited" operator choice and is
    # not a config error — the limiter logs a warning at construction.
    rate_limit_raw: Any = None
    rate_limit_source = "default"
    if env.get("HERMES_NODES_RATE_LIMIT") is not None:
        rate_limit_raw = env["HERMES_NODES_RATE_LIMIT"]
        rate_limit_source = "env"
    elif _read_file_value(file_data, "rate_limit_per_node") is not None:
        rate_limit_raw = _read_file_value(file_data, "rate_limit_per_node")
        rate_limit_source = "file"
    rate_limit: int | None = None
    if rate_limit_raw is not None:
        # Empty string from the env (e.g. ``export HERMES_NODES_RATE_LIMIT=``)
        # falls through to the default. Some shells normalise unset vars
        # this way and the operator clearly didn't intend to set a value.
        if isinstance(rate_limit_raw, str) and rate_limit_raw.strip() == "":
            rate_limit = None
        else:
            try:
                rate_limit = int(rate_limit_raw)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"{rate_limit_source}: rate_limit_per_node must be an integer, "
                    f"got {rate_limit_raw!r}"
                ) from exc

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
    if audit_log_raw is not None:
        resolved["audit_log_path"] = str(audit_log_raw)
    if audit_retention is not None:
        resolved["audit_retention_days"] = audit_retention
    if handshake_timeout is not None:
        resolved["handshake_timeout_seconds"] = handshake_timeout

    if heartbeat_stale is not None:
        resolved["heartbeat_stale_seconds"] = heartbeat_stale
    if sweep_interval is not None:
        resolved["heartbeat_sweep_interval_seconds"] = sweep_interval
    if rate_limit is not None:
        resolved["rate_limit_per_node"] = rate_limit

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
