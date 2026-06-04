"""Tests for :mod:`hermes_nodes_plugin.config`.

Coverage:

  * Dataclass defaults (no env, no file).
  * Precedence: env > file > default — exercised for every key.
  * Clear error paths: bad port, partial TLS, malformed YAML, wrong
    YAML shape.
  * Predicates: :meth:`uses_tls`, :meth:`is_loopback`.
  * :meth:`token_encryption_key` resolves the key from the env var
    named by :attr:`token_encryption_key_env`.

The tests never touch the real process environment or ``~/.hermes/`` —
``load_config`` accepts an ``env`` mapping and a ``config_path`` for
exactly this reason.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from hermes_nodes_plugin.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_TOKEN_STORE_PATH,
    NodeServerConfig,
    load_config,
)
from hermes_nodes_plugin.errors import ConfigError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A pristine env mapping with no HERMES_NODES_* vars set.

    The loader reads ``env.get(...)``, so we pass a fresh dict rather
    than ``os.environ`` to keep the tests hermetic.
    """
    return {}


# ---------------------------------------------------------------------------
# Default behaviour
# ---------------------------------------------------------------------------


def test_defaults_when_nothing_set(empty_env: dict[str, str], tmp_path: Path) -> None:
    """No env, no file → dataclass defaults, no error."""
    cfg = load_config(env=empty_env, config_path=tmp_path / "missing.yaml")
    assert cfg == NodeServerConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 6969
    assert cfg.tls_cert_path is None
    assert cfg.tls_key_path is None
    assert cfg.token_store_path == str(DEFAULT_TOKEN_STORE_PATH)
    assert cfg.token_encryption_key_env == "HERMES_NODES_TOKEN_KEY"


def test_missing_file_is_not_an_error(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    """A nonexistent config file should fall through to defaults silently.

    The plugin is shipped in a fresh checkout with no config file —
    refusing to start in that state would block every install.
    """
    missing = tmp_path / "definitely-not-here.yaml"
    assert not missing.exists()
    cfg = load_config(env=empty_env, config_path=missing)
    assert cfg == NodeServerConfig()


def test_empty_yaml_file_is_not_an_error(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    """A file with no top-level keys is the same as a missing file."""
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    cfg = load_config(env=empty_env, config_path=p)
    assert cfg == NodeServerConfig()


# ---------------------------------------------------------------------------
# Precedence: env > file > default
# ---------------------------------------------------------------------------


def test_file_values_used_when_no_env(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    p = tmp_path / "hermes-nodes.yaml"
    p.write_text(
        dedent(
            """
            host: 10.0.0.5
            port: 8080
            tls_cert_path: /etc/ssl/cert.pem
            tls_key_path: /etc/ssl/key.pem
            token_store_path: /var/lib/hermes/tokens.json
            token_encryption_key_env: MY_CUSTOM_KEY_VAR
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(env=empty_env, config_path=p)
    assert cfg.host == "10.0.0.5"
    assert cfg.port == 8080
    assert cfg.tls_cert_path == "/etc/ssl/cert.pem"
    assert cfg.tls_key_path == "/etc/ssl/key.pem"
    assert cfg.token_store_path == "/var/lib/hermes/tokens.json"
    assert cfg.token_encryption_key_env == "MY_CUSTOM_KEY_VAR"


def test_env_overrides_file(empty_env: dict[str, str], tmp_path: Path) -> None:
    """When both env and file set the same key, env wins (every key)."""
    p = tmp_path / "hermes-nodes.yaml"
    p.write_text(
        dedent(
            """
            host: 10.0.0.5
            port: 8080
            tls_cert_path: /etc/ssl/cert.pem
            tls_key_path: /etc/ssl/key.pem
            token_store_path: /var/lib/hermes/tokens.json
            token_encryption_key_env: MY_CUSTOM_KEY_VAR
            """
        ),
        encoding="utf-8",
    )
    env = {
        "HERMES_NODES_HOST": "192.168.1.1",
        "HERMES_NODES_PORT": "9000",
        "HERMES_NODES_TLS_CERT_PATH": "/run/secrets/cert.pem",
        "HERMES_NODES_TLS_KEY_PATH": "/run/secrets/key.pem",
        "HERMES_NODES_TOKEN_STORE_PATH": "/run/secrets/tokens.json",
        "HERMES_NODES_TOKEN_ENCRYPTION_KEY_ENV": "OVERRIDE_VAR",
    }
    cfg = load_config(env=env, config_path=p)
    assert cfg.host == "192.168.1.1"
    assert cfg.port == 9000
    assert cfg.tls_cert_path == "/run/secrets/cert.pem"
    assert cfg.tls_key_path == "/run/secrets/key.pem"
    assert cfg.token_store_path == "/run/secrets/tokens.json"
    assert cfg.token_encryption_key_env == "OVERRIDE_VAR"


def test_env_overrides_default(empty_env: dict[str, str], tmp_path: Path) -> None:
    """Env alone (no file) should still apply — env beats default."""
    env = {"HERMES_NODES_HOST": "0.0.0.0", "HERMES_NODES_PORT": "9999"}
    cfg = load_config(env=env, config_path=tmp_path / "nope.yaml")
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9999
    # Unset keys still fall through to defaults.
    assert cfg.tls_cert_path is None
    assert cfg.token_encryption_key_env == "HERMES_NODES_TOKEN_KEY"


def test_partial_env_only_overrides_listed_keys(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    """Setting one env var shouldn't disturb the rest of the resolution."""
    p = tmp_path / "hermes-nodes.yaml"
    p.write_text(
        dedent(
            """
            host: 10.0.0.5
            port: 8080
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(env={"HERMES_NODES_PORT": "1234"}, config_path=p)
    assert cfg.host == "10.0.0.5"  # from file
    assert cfg.port == 1234  # from env, beats file's 8080


# ---------------------------------------------------------------------------
# File parsing edge cases
# ---------------------------------------------------------------------------


def test_yaml_keys_are_case_insensitive(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    """YAML authors may write HOST or host — both should work.

    Env-var names are uppercase by convention; dataclass field names
    are lowercase. We accept either so operators don't have to
    remember which file format uses which case.
    """
    p = tmp_path / "hermes-nodes.yaml"
    p.write_text(
        dedent(
            """
            HOST: 172.16.0.1
            PORT: 4242
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(env=empty_env, config_path=p)
    assert cfg.host == "172.16.0.1"
    assert cfg.port == 4242


def test_malformed_yaml_raises(empty_env: dict[str, str], tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("host: : :\n  - [unbalanced", encoding="utf-8")
    with pytest.raises(ConfigError, match="failed to parse YAML"):
        load_config(env=empty_env, config_path=p)


def test_non_mapping_yaml_raises(empty_env: dict[str, str], tmp_path: Path) -> None:
    """A YAML list at the top level is a config author bug — refuse clearly."""
    p = tmp_path / "list.yaml"
    p.write_text("- host: 1.2.3.4\n- port: 8080\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(env=empty_env, config_path=p)


def test_scalar_yaml_raises(empty_env: dict[str, str], tmp_path: Path) -> None:
    p = tmp_path / "scalar.yaml"
    p.write_text("just-a-string\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(env=empty_env, config_path=p)


# ---------------------------------------------------------------------------
# Port coercion
# ---------------------------------------------------------------------------


def test_port_from_env_must_be_integer(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match=r"port must be an integer"):
        load_config(
            env={"HERMES_NODES_PORT": "not-a-number"},
            config_path=tmp_path / "nope.yaml",
        )


def test_port_from_file_must_be_integer(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    p = tmp_path / "bad-port.yaml"
    p.write_text("port: not-a-number\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"port must be an integer"):
        load_config(env=empty_env, config_path=p)


def test_port_out_of_range(empty_env: dict[str, str], tmp_path: Path) -> None:
    for bad in ("0", "65536", "100000", "-1"):
        with pytest.raises(ConfigError, match=r"1\.\.65535"):
            load_config(
                env={"HERMES_NODES_PORT": bad},
                config_path=tmp_path / "nope.yaml",
            )


def test_port_boundary_values_are_accepted(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    for good in ("1", "65535", "6969"):
        cfg = load_config(
            env={"HERMES_NODES_PORT": good},
            config_path=tmp_path / "nope.yaml",
        )
        assert cfg.port == int(good)


# ---------------------------------------------------------------------------
# TLS partial-config: the footgun catcher
# ---------------------------------------------------------------------------


def test_tls_cert_without_key_raises() -> None:
    """Common deployment mistake: cert set, key forgotten.

    We catch this at config-load time rather than letting the server
    fail to bind with a confusing ssl error at startup.
    """
    with pytest.raises(ConfigError, match="partially configured"):
        NodeServerConfig(tls_cert_path="/etc/ssl/cert.pem")


def test_tls_key_without_cert_raises() -> None:
    with pytest.raises(ConfigError, match="partially configured"):
        NodeServerConfig(tls_key_path="/etc/ssl/key.pem")


def test_both_tls_paths_accepted() -> None:
    """Setting both is fine — that's the direct-TLS deployment."""
    cfg = NodeServerConfig(
        tls_cert_path="/etc/ssl/cert.pem",
        tls_key_path="/etc/ssl/key.pem",
    )
    assert cfg.uses_tls() is True


def test_neither_tls_path_accepted() -> None:
    """Reverse-proxied (default) — plain HTTP behind nginx."""
    cfg = NodeServerConfig()
    assert cfg.uses_tls() is False


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def test_uses_tls_predicate() -> None:
    assert NodeServerConfig().uses_tls() is False
    assert NodeServerConfig(tls_cert_path="/c", tls_key_path="/k").uses_tls() is True


def test_is_loopback_predicate() -> None:
    assert NodeServerConfig(host="127.0.0.1").is_loopback() is True
    assert NodeServerConfig(host="::1").is_loopback() is True
    assert NodeServerConfig(host="localhost").is_loopback() is True
    assert NodeServerConfig(host="0.0.0.0").is_loopback() is False
    assert NodeServerConfig(host="10.0.0.5").is_loopback() is False


def test_tls_with_non_loopback_warns_via_predicate() -> None:
    """Document the safety property: loopback + no TLS is fine.

    Non-loopback + no TLS would be a deployment bug, but the config
    loader doesn't refuse it — that's a runtime check, not a config
    check (some operators may intentionally start the plugin on a
    private VLAN without TLS).
    """
    cfg = NodeServerConfig(host="0.0.0.0")
    assert cfg.uses_tls() is False
    assert cfg.is_loopback() is False


# ---------------------------------------------------------------------------
# token_encryption_key lookup
# ---------------------------------------------------------------------------


def test_token_encryption_key_returns_value_when_set() -> None:
    cfg = NodeServerConfig(token_encryption_key_env="MY_KEY")
    assert cfg.token_encryption_key(env={"MY_KEY": "abc123"}) == "abc123"


def test_token_encryption_key_returns_none_when_missing() -> None:
    cfg = NodeServerConfig()
    assert cfg.token_encryption_key(env={}) is None
    # Empty string is treated as unset (so an accidentally exported
    # empty var doesn't crash startup with a confusing error).
    assert cfg.token_encryption_key(env={"HERMES_NODES_TOKEN_KEY": ""}) is None


def test_token_encryption_key_default_env_var_name() -> None:
    """The default env var name should match REQUIREMENTS FR-4.1."""
    cfg = NodeServerConfig()
    assert cfg.token_encryption_key_env == "HERMES_NODES_TOKEN_KEY"


def test_token_encryption_key_reads_real_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_NODES_TOKEN_KEY", "from-real-env")
    cfg = NodeServerConfig()
    assert cfg.token_encryption_key() == "from-real-env"


# ---------------------------------------------------------------------------
# Dataclass properties
# ---------------------------------------------------------------------------


def test_config_is_frozen() -> None:
    cfg = NodeServerConfig()
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError subclass
        cfg.host = "evil"  # type: ignore[misc]


def test_direct_construction_works() -> None:
    """Skip the loader; build directly with explicit kwargs (used in tests)."""
    cfg = NodeServerConfig(host="0.0.0.0", port=443)
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 443


def test_config_equality_is_value_based() -> None:
    """frozen dataclass → __eq__ compares all fields by default.

    This makes assertions like ``cfg == NodeServerConfig()`` robust
    against irrelevant field reorderings.
    """
    a = NodeServerConfig(host="1.2.3.4", port=8080)
    b = NodeServerConfig(host="1.2.3.4", port=8080)
    c = NodeServerConfig(host="1.2.3.4", port=8081)
    assert a == b
    assert a != c
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------


def test_default_config_path_is_under_hermes_home() -> None:
    assert str(DEFAULT_CONFIG_PATH).endswith(".hermes/hermes-nodes.yaml")


def test_default_token_store_path_is_under_hermes_home() -> None:
    assert str(DEFAULT_TOKEN_STORE_PATH).endswith("nodes/tokens.json")


def test_default_paths_collapse_tilde() -> None:
    """Constants should be absolute paths (Path("~").expanduser() is eager)."""
    assert "~" not in str(DEFAULT_CONFIG_PATH)
    assert "~" not in str(DEFAULT_TOKEN_STORE_PATH)
    assert str(DEFAULT_CONFIG_PATH).startswith(str(Path.home()))


# ---------------------------------------------------------------------------
# Optional str coercion
# ---------------------------------------------------------------------------


def test_tls_path_empty_string_means_unset(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    """Setting HERMES_NODES_TLS_CERT_PATH="" should *unset* TLS, not set it to ''.

    Useful for "override the file" cases without leaving the cert set
    to a literal empty path that would fail at TLS handshake time.
    """
    p = tmp_path / "hermes-nodes.yaml"
    p.write_text(
        dedent(
            """
            tls_cert_path: /etc/ssl/cert.pem
            tls_key_path: /etc/ssl/key.pem
            """
        ),
        encoding="utf-8",
    )
    env = {
        "HERMES_NODES_TLS_CERT_PATH": "",
        "HERMES_NODES_TLS_KEY_PATH": "",
    }
    cfg = load_config(env=env, config_path=p)
    assert cfg.tls_cert_path is None
    assert cfg.tls_key_path is None


def test_yaml_null_for_tls_path_means_unset(
    empty_env: dict[str, str], tmp_path: Path
) -> None:
    p = tmp_path / "hermes-nodes.yaml"
    p.write_text(
        dedent(
            """
            tls_cert_path: null
            tls_key_path: ~
            """
        ),
        encoding="utf-8",
    )
    cfg = load_config(env=empty_env, config_path=p)
    assert cfg.tls_cert_path is None
    assert cfg.tls_key_path is None


# ---------------------------------------------------------------------------
# Sanity: hermetic test environment
# ---------------------------------------------------------------------------


def test_test_does_not_touch_real_environ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Even if HERMES_NODES_* is set in the developer's shell, tests
    using the ``empty_env`` fixture should be unaffected.

    Guards against the easy-to-make mistake of accidentally reading
    ``os.environ`` inside the loader.
    """
    monkeypatch.setenv("HERMES_NODES_HOST", "from-real-shell")
    monkeypatch.setenv("HERMES_NODES_PORT", "12345")
    cfg = load_config(env={}, config_path=tmp_path / "nope.yaml")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 6969


def test_real_load_config_uses_environ_when_no_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Document the default: load_config() without env= falls back to os.environ.

    This is the behaviour the operator gets in production; the env=
    override exists only for tests.
    """
    # Use monkeypatch instead of bare os.environ mutation: the previous
    # version snapshotted HERMES_NODES_* keys and restored them in a
    # ``finally``, but if the test *added* a key not present in the
    # snapshot (HERMES_NODES_HOST here) the restore loop would never
    # see it, and the env var leaked into later tests — including
    # the lifecycle integration tests, which tried to bind the
    # host and failed DNS resolution. monkeypatch.setenv auto-undo
    # fixes the leak.
    monkeypatch.delenv("HERMES_NODES_PORT", raising=False)
    monkeypatch.setenv("HERMES_NODES_HOST", "from-prod-call")
    cfg = load_config(config_path=tmp_path / "nope.yaml")
    assert cfg.host == "from-prod-call"
    assert cfg.port == 6969  # default
