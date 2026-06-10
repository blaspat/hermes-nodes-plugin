"""Tests for :mod:`hermes_nodes_plugin.env` (the auto-token helper).

Five edge cases pinned by the task body, plus a few defensive
coverage areas:

1. **Key already in .env** — return it, do not regenerate, do not
   write. Mirrors the production behaviour: regenerating an
   existing key would invalidate every previously-paired node.
2. **.env doesn't exist** — create the file + parent dir, append
   the new key, return status ``"wrote"``.
3. **Key line exists in .env, file is otherwise empty** — write a
   single line, no double-key in the file. (Production case: the
   operator ran a partial config earlier.)
4. **.env has no trailing newline** — append a separator newline
   before the new line so the new key isn't glued to whatever the
   last existing line was.
5. **Can't write to .env** — return status ``"failed"`` with the
   OS error string, the in-process env still gets the key, the
   caller (CLI) decides how to surface it.

Defensive coverage:

* ``.env`` with comments before the key — comments are skipped
* ``.env`` with whitespace around ``KEY=VALUE`` — stripped
* ``.env`` with multiple other vars and the key we care about
  missing — append at the end, do not overwrite anything
* ``.env`` with a prefix-matched var name (``TOKEN_KEY_BACKUP``)
  — the reader must match the exact var, not a prefix
* ``os.environ`` is updated in all three success statuses
* atomic write failure: a partial write cannot leave a corrupt
  file (we mock the atomic write to raise and verify the
  in-memory state matches the on-disk state)

The tests pin a deterministic ``generate`` callable so the
assertions don't depend on Fernet's randomness. A real Fernet
key is 44 chars of url-safe-base64; we use 44 ``x``s plus the
trailing ``=`` padding that ``Fernet.generate_key().decode()``
returns.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from hermes_nodes_plugin import env as env_mod


# A Fernet-shaped key: 32 random bytes b64url-encoded (44 chars
# including the trailing ``=``). Using a fixed string lets us
# assert exact equality without coupling to Fernet's randomness.
FAKE_KEY = "x" * 43 + "="


def _make_generate(key: str = FAKE_KEY) -> Callable[[], str]:
    """Return a no-argument callable that always returns ``key``."""
    return lambda: key


# ---------------------------------------------------------------------------
# 1. Key already in .env → use it silently, don't regenerate
# ---------------------------------------------------------------------------


class TestKeyAlreadyPresent:
    def test_existing_key_returned_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The pre-existing key round-trips; no regeneration."""
        env_path = tmp_path / "hermes.env"
        env_path.write_text(f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\nOTHER=x\n")
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate("DIFFERENT_KEY_SHOULD_NOT_BE_USED"),
        )

        assert result.status == "present"
        assert result.key == FAKE_KEY
        # File is byte-for-byte unchanged — the helper did not
        # rewrite the existing content. We verify by reading
        # back rather than stat'ing mtime to keep the test
        # hermetic.
        assert env_path.read_text() == (
            f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\nOTHER=x\n"
        )

    def test_existing_key_mirrors_into_process_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The present path still sets ``os.environ`` so the
        current process can encrypt the token store.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        env_path.write_text(f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\n")

        env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )

        assert os.environ["HERMES_NODES_TOKEN_KEY"] == FAKE_KEY


# ---------------------------------------------------------------------------
# 2. .env doesn't exist → create it
# ---------------------------------------------------------------------------


class TestEnvFileMissing:
    def test_missing_file_is_created(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Helper creates the file (and any missing parent dirs)
        and writes a single KEY=VALUE line.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "nested" / "deeper" / "hermes.env"
        assert not env_path.exists()

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )

        assert result.status == "wrote"
        assert result.key == FAKE_KEY
        assert env_path.exists()
        # File is exactly the one line we expect (no BOM, no
        # leading/trailing junk, trailing newline).
        assert env_path.read_text() == f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\n"

    def test_missing_file_creates_parent_dirs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Parent dir is created on the fly — operator doesn't
        have to ``mkdir -p ~/.hermes`` first.
        """
        env_path = tmp_path / "fresh" / ".env"
        assert not env_path.parent.exists()

        env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )
        assert env_path.parent.is_dir()


# ---------------------------------------------------------------------------
# 3. Key line exists in .env → replace, don't duplicate
# ---------------------------------------------------------------------------


class TestKeyLineExistsInEnv:
    def test_existing_key_line_treated_as_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If the key line is already there (e.g. operator added
        it manually with the wrong value), the helper treats it
        as the "present" path. We never silently rewrite an
        existing value — that would invalidate every paired
        node.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        env_path.write_text(
            f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\nFOO=bar\n"
        )

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate("ANOTHER_KEY"),
        )

        # Present → existing value returned, file unchanged.
        assert result.status == "present"
        assert result.key == FAKE_KEY
        # Crucially: the var appears exactly ONCE, not twice.
        assert (
            env_path.read_text().count("HERMES_NODES_TOKEN_KEY=")
            == 1
        )
        # FOO=bar still there.
        assert "FOO=bar" in env_path.read_text()

    def test_var_missing_from_existing_file_appends(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """File exists with other vars, our var is missing —
        append our var without disturbing the others.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        original = "FOO=bar\nBAZ=qux\n"
        env_path.write_text(original)

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )

        assert result.status == "wrote"
        text = env_path.read_text()
        # Pre-existing lines preserved verbatim.
        assert "FOO=bar\n" in text
        assert "BAZ=qux\n" in text
        # New line appended at the end, well-formed.
        assert text.endswith(f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\n")


# ---------------------------------------------------------------------------
# 4. .env has no trailing newline → append cleanly
# ---------------------------------------------------------------------------


class TestNoTrailingNewline:
    def test_no_trailing_newline_separator_inserted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """File ends mid-line (operator typed ``export ... >> .env``
        or pasted without a newline) — the new key must not get
        glued to the last existing line. We insert a separator
        newline.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        # Note: no trailing newline on the last line.
        env_path.write_text("FOO=bar")

        env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )

        text = env_path.read_text()
        # FOO=bar still ends in the same last char (no extra
        # whitespace bolted on). Our key is on its own line, with
        # a separator newline before it.
        assert "FOO=bar\n" in text
        assert text.endswith(f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\n")
        # The new key line is parseable: a second pass through
        # the reader should find it.
        from hermes_nodes_plugin.env import _read_existing_value

        assert (
            _read_existing_value(env_path, "HERMES_NODES_TOKEN_KEY")
            == FAKE_KEY
        )

    def test_trailing_newline_kept_clean(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """File ends in a newline — the new line sits directly
        against the existing content, no extra blank line.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        env_path.write_text("FOO=bar\n")

        env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )
        text = env_path.read_text()
        # No double newline between FOO and our key.
        assert "FOO=bar\n" + f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\n" in text


# ---------------------------------------------------------------------------
# 5. Can't write to .env → warn, expose key + manual instructions
# ---------------------------------------------------------------------------


class TestWriteFailure:
    def test_atomic_write_failure_marks_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When the atomic-write step raises (permission denied,
        read-only filesystem, full disk), the helper reports a
        write failure and surfaces the key in the result so the
        CLI can print manual recovery instructions.

        We mock the ``_atomic_write`` helper to raise — that
        covers the same production code path as a real fs
        failure (the ``except OSError`` branch) without depending
        on chmod / uid behaviour in the test sandbox.
        """
        env_path = tmp_path / "hermes.env"
        env_path.write_text("FOO=bar\n")
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)

        def _raise_oserror(*_args, **_kwargs):
            raise OSError("simulated read-only filesystem")

        monkeypatch.setattr(env_mod, "_atomic_write", _raise_oserror)

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )

        assert result.status == "failed"
        assert result.key == FAKE_KEY
        assert "read-only" in (result.error or "")
        # The pre-existing FOO=bar line is preserved — the
        # atomic write either succeeds or never ran.
        assert env_path.read_text() == "FOO=bar\n"
        # The key is mirrored into os.environ regardless so
        # the in-process pair can complete.
        assert os.environ["HERMES_NODES_TOKEN_KEY"] == FAKE_KEY

    def test_failed_status_captures_oserror_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The ``error`` field carries the OS error message
        verbatim so the CLI can include it in the manual-
        recovery hint.
        """
        env_path = tmp_path / "hermes.env"
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)

        def _raise_oserror(*_args, **_kwargs):
            raise PermissionError(13, "Permission denied", str(env_path))

        monkeypatch.setattr(env_mod, "_atomic_write", _raise_oserror)

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )

        assert result.status == "failed"
        # OS error string is captured (the message + the path).
        assert "Permission denied" in (result.error or "")


# ---------------------------------------------------------------------------
# Defensive coverage
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_comments_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ``#``-prefixed line in the .env is a comment, not
        a malformed key. The reader ignores it.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        env_path.write_text(
            "# this is a comment\n"
            f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\n"
        )

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate("NEW"),
        )
        assert result.status == "present"
        assert result.key == FAKE_KEY

    def test_blank_lines_and_whitespace_tolerated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Blank lines and spaces around ``=`` are stripped.

        The reader is intentionally lenient: hand-edited .env
        files accumulate noise, and the helper should not
        crash on a stray blank line.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        env_path.write_text(
            "\n"
            "\n"
            f"   HERMES_NODES_TOKEN_KEY   =   {FAKE_KEY}   \n"
        )

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate("NEW"),
        )
        assert result.status == "present"
        assert result.key == FAKE_KEY

    def test_var_name_does_not_match_prefix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``HERMES_NODES_TOKEN_KEY`` must not match a
        hypothetical ``HERMES_NODES_TOKEN_KEY_BACKUP`` line.
        A naive ``startswith`` would yield a false positive.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        env_path.write_text(
            f"HERMES_NODES_TOKEN_KEY_BACKUP={FAKE_KEY}\n"
        )

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )
        # The exact var is missing → helper writes a new line,
        # does not return the _BACKUP value.
        assert result.status == "wrote"
        assert result.key == FAKE_KEY
        text = env_path.read_text()
        assert "HERMES_NODES_TOKEN_KEY_BACKUP=" in text
        assert f"HERMES_NODES_TOKEN_KEY={FAKE_KEY}\n" in text

    def test_malformed_line_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A line without ``=`` is malformed. The reader
        skips it (rather than crashing) so a hand-edited
        file with an accidental line doesn't break pair.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"
        env_path.write_text("this is not a valid env line\n")

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )
        # The var is missing → helper writes one.
        assert result.status == "wrote"
        assert result.key == FAKE_KEY

    def test_wrote_status_mirrors_into_process_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The fresh-write path also sets os.environ so the
        current process can encrypt the token store in the
        same invocation. This is the central "auto-token"
        promise.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )
        assert result.status == "wrote"
        assert os.environ["HERMES_NODES_TOKEN_KEY"] == FAKE_KEY

    def test_failed_status_mirrors_into_process_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The failed-write path also sets os.environ so the
        in-process pair can complete even though the on-disk
        persistence failed. The CLI's manual-recovery hint
        rides on top of this.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = tmp_path / "hermes.env"

        def _raise_oserror(*_args, **_kwargs):
            raise OSError("simulated")

        monkeypatch.setattr(env_mod, "_atomic_write", _raise_oserror)

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )
        assert result.status == "failed"
        assert os.environ["HERMES_NODES_TOKEN_KEY"] == FAKE_KEY

    def test_result_path_is_expanded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A path with a literal ``~`` is expanded before
        returning, so the CLI can show the operator an
        absolute path.
        """
        monkeypatch.delenv("HERMES_NODES_TOKEN_KEY", raising=False)
        env_path = Path("~/should_be_expanded.env")

        result = env_mod.ensure_fernet_key_in_env(
            var_name="HERMES_NODES_TOKEN_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )
        assert str(result.path).startswith(str(Path.home()))
        # Clean up the file we just created in HOME — we
        # don't want to pollute the operator's real env.
        result.path.unlink(missing_ok=True)

    def test_default_path_is_hermes_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``HERMES_ENV_PATH`` is the canonical location. The
        helper's default behaviour writes there when no
        ``env_path`` is passed.
        """
        # We don't actually want to touch the real HOME here.
        # The constant's value is what we're pinning: it must
        # end in ``.hermes/.env`` so it lines up with Hermes's
        # own dotenv loader.
        assert env_mod.HERMES_ENV_PATH.name == ".env"
        assert env_mod.HERMES_ENV_PATH.parent.name == ".hermes"

    def test_custom_var_name_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The plugin config can override the env var name
        (e.g. ``HERMES_NODES_TOKEN_KEY`` is the default but
        operators can rename it). The helper writes the
        operator-configured name, not a hard-coded literal.
        """
        monkeypatch.delenv("MY_PLUGIN_FERNET_KEY", raising=False)
        env_path = tmp_path / "hermes.env"

        result = env_mod.ensure_fernet_key_in_env(
            var_name="MY_PLUGIN_FERNET_KEY",
            env_path=env_path,
            generate=_make_generate(),
        )
        assert result.status == "wrote"
        assert os.environ["MY_PLUGIN_FERNET_KEY"] == FAKE_KEY
        # The file uses the operator-configured name, not the
        # default.
        text = env_path.read_text()
        assert "MY_PLUGIN_FERNET_KEY=" in text
        assert "HERMES_NODES_TOKEN_KEY" not in text
