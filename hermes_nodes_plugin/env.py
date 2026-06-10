"""Helpers for reading and writing the operator's ``~/.hermes/.env``.

This module exists to support ``hermes node pair`` running unattended
on a fresh install: if the Fernet key is not in the env var named by
:attr:`hermes_nodes_plugin.config.NodeServerConfig.token_encryption_key_env`,
the pair command should auto-generate one and persist it to
``~/.hermes/.env`` so the operator doesn't have to do the
Fernet.generate_key dance by hand (FR-4.2).

It is intentionally narrow — the pair command is the only caller right
now, and we want a single small helper to test in isolation rather
than a generic ``.env`` parser.

Public surface
--------------

* :data:`HERMES_ENV_PATH` — the canonical path the helper writes to.
  ``~/.hermes/.env`` (the same file Hermes's own dotenv loader uses).
* :func:`ensure_fernet_key_in_env` — generate a Fernet key, write it
  to ``~/.hermes/.env`` (creating the file if missing, replacing the
  existing line if present), update :data:`os.environ` so the
  current process sees it, and return the key string. Idempotent
  for the "key already exists" path: it never regenerates an
  existing key.

Format of the line written
--------------------------

The line uses the same ``KEY=VALUE`` shape as the rest of
``~/.hermes/.env``, for example::

    HERMES_NODES_TOKEN_KEY=abcd1234...

The helper does NOT quote the value. Fernet keys are url-safe-base64
(``[A-Za-z0-9_-]`` only, plus ``=`` padding), so they never need
shell quoting and quoting them would break tools that re-read the
file with strict parsers.

The helper also uses an atomic write (write-temp-then-rename) so a
partial write cannot leave the operator's ``.env`` corrupted. If
the rename fails (permission denied, read-only filesystem), the
helper falls back to printing the key and the manual recovery
instructions, mirroring the existing "can't write, warn" path that
pair already documents.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cryptography.fernet import Fernet


# Canonical location of the operator's dotenv. Matches the location
# Hermes itself reads on startup; a default-mode Hermes install
# creates this file on first launch, so it almost always exists
# by the time someone is running ``hermes node pair``. We still
# handle the missing-file case (fresh box, never-ran-Hermes) by
# creating the parent dir + file ourselves.
HERMES_ENV_PATH = Path("~/.hermes/.env").expanduser()


@dataclass(frozen=True)
class EnvWriteResult:
    """Result of :func:`ensure_fernet_key_in_env`.

    Carries the key string the caller needs to encrypt the token
    store plus a tri-state status describing what happened on disk:

    * ``"present"`` — the key was already in the file; the helper
      did not write. CLI should stay silent about the file.
    * ``"wrote"``   — the helper generated a key and successfully
      persisted it. CLI can print a "saved to ~/.hermes/.env"
      confirmation.
    * ``"failed"``  — the helper generated a key and tried to
      persist it, but the write raised (permission denied, read-
      only filesystem, full disk). CLI must surface the key +
      manual recovery instructions so the operator can save it
      themselves.
    """

    key: str
    status: str
    path: Path
    error: str | None = None


def _read_existing_value(env_path: Path, var_name: str) -> str | None:
    """Return the value of ``var_name`` in ``env_path`` if present.

    Parses the file with a tiny hand-rolled line scanner: we don't
    need a full dotenv implementation because we only ever look for
    one variable, and the file is operator-authored (well-formed
    ``KEY=VALUE`` per line, optional ``#`` comments at line start,
    optional surrounding whitespace). Quoted values aren't supported
    — Hermes's own ``.env`` is unquoted throughout, and the only
    value we ever write is a Fernet key (url-safe-base64, no
    special chars).

    Returns ``None`` if the file is missing, the variable is
    missing, or the line is malformed (malformed → caller treats
    the same as missing rather than crashing on a hand-edited
    file).
    """
    if not env_path.exists():
        return None
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        # Unreadable for any reason → treat as "no value" so the
        # helper can still write a new one. We don't want a broken
        # read to block the operator's pair flow.
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != var_name:
            continue
        return value.strip() or None
    return None


def _atomic_write(env_path: Path, content: str) -> None:
    """Write ``content`` to ``env_path`` via a temp-file + rename.

    ``os.replace`` is atomic on POSIX (Linux) — if the rename
    succeeds, the new content is fully visible; if it fails, the
    old file is untouched. This matters because ``~/.hermes/.env``
    is the operator's source-of-truth for secrets and a partial
    write there is much worse than a no-op.
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    # ``delete=False`` so we can rename across filesystem mounts
    # if /tmp and ~/.hermes end up on different devices. We
    # clean up explicitly in a ``finally`` because ``delete=False``
    # leaves the file behind on exceptions.
    import tempfile

    fd, tmp_name = tempfile.mkstemp(
        prefix=env_path.name + ".",
        suffix=".tmp",
        dir=str(env_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, env_path)
    except Exception:
        # Best-effort cleanup of the temp file. The os.replace
        # above either succeeded (tmp is gone) or never ran (tmp
        # is still here) — ``unlink`` is safe to call only in the
        # latter case, but missing-file is fine to swallow.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _format_env_line(var_name: str, value: str) -> str:
    """Format a single ``KEY=VALUE`` line for the dotenv file.

    No quoting (Fernet keys are url-safe-base64, no shell-special
    chars). Trailing newline is the caller's responsibility — we
    keep this function shape-agnostic so the file-with-existing-
    content code path can decide whether to insert a separator
    newline.
    """
    return f"{var_name}={value}\n"


def ensure_fernet_key_in_env(
    var_name: str = "HERMES_NODES_TOKEN_KEY",
    env_path: Path | None = None,
    *,
    generate: Callable[[], str] = lambda: Fernet.generate_key().decode("ascii"),
) -> EnvWriteResult:
    """Ensure ``var_name`` is set in ``env_path``; create if missing.

    Behaviour, in evaluation order:

    1. **Already in env_path** → return the existing value with
       :attr:`EnvWriteResult.status` = ``"present"``. Never
       regenerates an existing key.
    2. **File doesn't exist or var is missing** → generate a new
       Fernet key, append a well-formed ``KEY=VALUE`` line to the
       file (creating the file + parent dir if needed), update
       :data:`os.environ` so the caller's :func:`load_config` call
       sees the new value in-process, and return
       :class:`EnvWriteResult` with :attr:`status` = ``"wrote"``.
    3. **Write fails (permission denied, read-only filesystem)** →
       still generate a key, still update :data:`os.environ` (so
       the current process can complete the pair), still return
       the key with :attr:`status` = ``"failed"`` and the OS
       error string in :attr:`error`. The caller (CLI) decides
       whether to surface a manual-recovery hint based on the
       status — we never raise from a write failure.

    Why we also update :data:`os.environ`: ``load_config`` reads
    from the process env, not the dotenv file. If we wrote the
    key to the file but didn't update the process env, the
    current ``hermes node pair`` invocation would still see "no
    key" and fail. Mirroring the value into ``os.environ`` is the
    only way a single invocation can both persist the key and
    use it without a process restart.

    Args:
        var_name: Name of the env var to ensure (defaults to
            ``HERMES_NODES_TOKEN_KEY``; the plugin config may
            override the literal name).
        env_path: Path to the dotenv file. Defaults to
            :data:`HERMES_ENV_PATH`. Tests pass an explicit
            ``tmp_path`` to keep the operator's real file
            untouched.
        generate: Callable that returns a new Fernet key string.
            Defaults to :func:`Fernet.generate_key`. Injected
            for tests that need a deterministic key.

    Returns:
        :class:`EnvWriteResult` with the resolved key, the
        :attr:`EnvWriteResult.status` describing what happened
        (``"present"`` / ``"wrote"`` / ``"failed"``), the
        target path, and (on failure) the OS error string in
        :attr:`EnvWriteResult.error`.
    """
    target = (Path(env_path) if env_path is not None else HERMES_ENV_PATH).expanduser()
    existing = _read_existing_value(target, var_name)
    if existing:
        # Idempotent path: key already on disk, no regeneration.
        # We still mirror it into os.environ so the current
        # process sees it (the .env file is only loaded by the
        # host's dotenv loader, not by this module).
        os.environ[var_name] = existing
        return EnvWriteResult(key=existing, status="present", path=target)

    new_key = generate()
    # We append, not overwrite, to preserve any other vars the
    # operator has in their .env (API keys, channel tokens, etc).
    # Atomic write: build the new content in memory, then rename.
    # Three cases for the existing content:
    #   * file missing   → new content is just the new line
    #   * file empty     → new content is just the new line
    #   * file has lines → preserve them, then append a separator
    #     newline if the file didn't end in one
    if target.exists():
        try:
            existing_text = target.read_text(encoding="utf-8")
        except OSError:
            existing_text = ""
    else:
        existing_text = ""

    if existing_text and not existing_text.endswith("\n"):
        # No trailing newline → append a separator so the new
        # line doesn't get glued to whatever the last line is.
        new_content = existing_text + "\n" + _format_env_line(var_name, new_key)
    else:
        # Either empty file (we just created it) or file ends in
        # a newline — either way the next line can sit directly
        # against the existing content.
        new_content = existing_text + _format_env_line(var_name, new_key)

    try:
        _atomic_write(target, new_content)
    except OSError as exc:
        # The CLI's caller (the one that knows the operator is
        # watching) will still print the key + the manual recovery
        # instructions, so the operator can recover. We mirror
        # the key into os.environ regardless so the in-process
        # token store still works this invocation.
        os.environ[var_name] = new_key
        return EnvWriteResult(
            key=new_key,
            status="failed",
            path=target,
            error=str(exc) or exc.__class__.__name__,
        )

    # Mirror into process env so load_config() sees the new value
    # in the same invocation. Even when the on-disk write failed
    # above, this is what makes the in-process token store work.
    os.environ[var_name] = new_key

    return EnvWriteResult(key=new_key, status="wrote", path=target)


__all__ = [
    "HERMES_ENV_PATH",
    "EnvWriteResult",
    "ensure_fernet_key_in_env",
]
