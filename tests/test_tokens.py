"""Tests for :mod:`hermes_nodes_plugin.tokens`.

Coverage areas (matching REQUIREMENTS FR-1 + NFR-1.1/NFR-1.2):

  * Round-trip: ``create`` returns a token, ``validate`` accepts it.
  * Wrong token → ``validate`` returns False.
  * ``revoke`` invalidates a token; subsequent ``validate`` returns False.
  * Revoking a name twice is a safe no-op.
  * Creating a duplicate name on a live record raises.
  * Creating a duplicate name on a *revoked* record is allowed (re-pair).
  * Encryption at rest: the raw file on disk is Fernet ciphertext, the
    plaintext token does not appear in it.
  * Constant-time compare: the path used by ``validate`` goes through
    :func:`hmac.compare_digest` (NFR-1.1). We assert by structural
    inspection of the source — a future refactor that swaps to ``==``
    would break the test.
  * Wrong key: decrypting with a different Fernet key raises a clear
    ``TokenStoreError`` so the operator knows to recover by re-pairing.
  * Corrupt ciphertext: the file is bytes that aren't valid Fernet
    output → clear error.
  * Decryptable but bad JSON: clear error.
  * ``token_store_from_config`` rejects an unset key env var with the
    "regenerate + export" hint from the config (FR-4.2).
  * The store uses ``hmac.compare_digest`` (NFR-1.1) — checked by
    reading the source to catch a regression that swaps to ``==``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from hermes_nodes_plugin import tokens as tokens_mod
from hermes_nodes_plugin.config import NodeServerConfig
from hermes_nodes_plugin.errors import TokenStoreError
from hermes_nodes_plugin.tokens import TokenStore, token_store_from_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def key() -> str:
    """A fresh Fernet key per test, so tests don't accidentally share state."""
    return Fernet.generate_key().decode()


@pytest.fixture
def store(tmp_path: Path, key: str) -> TokenStore:
    return TokenStore(path=tmp_path / "tokens.json", key=key)


# ---------------------------------------------------------------------------
# create / validate round-trip
# ---------------------------------------------------------------------------


def test_create_returns_string_token(store: TokenStore) -> None:
    token = store.create("laptop1")
    assert isinstance(token, str)
    # 32 random bytes urlsafe-base64 encoded → 43 chars (no padding).
    assert len(token) >= 43


def test_two_creates_produce_different_tokens(store: TokenStore) -> None:
    """REQUIREMENTS FR-1.1: cryptographically random per call."""
    t1 = store.create("laptop1")
    t2 = store.create("laptop2")
    assert t1 != t2


def test_validate_round_trip(store: TokenStore) -> None:
    token = store.create("laptop1")
    assert store.validate("laptop1", token) is True


def test_validate_wrong_token_returns_false(store: TokenStore) -> None:
    token = store.create("laptop1")
    # Mutate the last char so length and prefix are still realistic.
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert bad != token
    assert store.validate("laptop1", bad) is False


def test_validate_wrong_name_returns_false(store: TokenStore) -> None:
    token = store.create("laptop1")
    assert store.validate("laptop2", token) is False


def test_validate_unknown_name_returns_false(store: TokenStore) -> None:
    assert store.validate("never-paired", "any-token-here") is False


def test_validate_empty_inputs_return_false(store: TokenStore) -> None:
    """Defence in depth: empty inputs short-circuit to False (no exception)."""
    assert store.validate("", "x") is False
    assert store.validate("x", "") is False
    assert store.validate("", "") is False


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_revoke_invalidates_token(store: TokenStore) -> None:
    token = store.create("laptop1")
    assert store.validate("laptop1", token) is True
    store.revoke("laptop1")
    assert store.validate("laptop1", token) is False


def test_revoke_unknown_name_is_noop(store: TokenStore) -> None:
    """REQUIREMENTS implicitly: re-running a revoke script shouldn't blow up."""
    store.revoke("never-paired")
    assert store.list() == []


def test_revoke_already_revoked_is_noop(store: TokenStore) -> None:
    token = store.create("laptop1")
    store.revoke("laptop1")
    store.revoke("laptop1")  # second call must not raise
    assert store.validate("laptop1", token) is False
    # And the record is still in list (revoked, not deleted).
    assert [r.name for r in store.list()] == ["laptop1"]


def test_revoke_only_affects_targeted_name(store: TokenStore) -> None:
    t1 = store.create("laptop1")
    t2 = store.create("laptop2")
    store.revoke("laptop1")
    assert store.validate("laptop1", t1) is False
    assert store.validate("laptop2", t2) is True


def test_revoke_empty_name_raises(store: TokenStore) -> None:
    with pytest.raises(TokenStoreError, match="non-empty"):
        store.revoke("")
    with pytest.raises(TokenStoreError, match="non-empty"):
        store.revoke("   ")


# ---------------------------------------------------------------------------
# create idempotency / uniqueness (REQUIREMENTS FR-1.5)
# ---------------------------------------------------------------------------


def test_create_duplicate_active_name_raises(store: TokenStore) -> None:
    store.create("laptop1")
    with pytest.raises(TokenStoreError, match="already paired"):
        store.create("laptop1")


def test_create_on_revoked_name_is_allowed(store: TokenStore) -> None:
    """Re-pairing: revoke + create with the same name should work and
    produce a new, different token."""
    old_token = store.create("laptop1")
    store.revoke("laptop1")
    new_token = store.create("laptop1")
    assert new_token != old_token
    # Old token still fails (it was revoked).
    assert store.validate("laptop1", old_token) is False
    # New token succeeds.
    assert store.validate("laptop1", new_token) is True


def test_create_empty_name_raises(store: TokenStore) -> None:
    with pytest.raises(TokenStoreError, match="non-empty"):
        store.create("")
    with pytest.raises(TokenStoreError, match="non-empty"):
        store.create("   ")


def test_create_strips_whitespace(store: TokenStore) -> None:
    """Trailing whitespace in CLI args is a common typo. Normalise it
    so ``"laptop1 "`` and ``"laptop1"`` don't create two records."""
    store.create("  laptop1  ")
    with pytest.raises(TokenStoreError, match="already paired"):
        store.create("laptop1")


# ---------------------------------------------------------------------------
# list()
# ---------------------------------------------------------------------------


def test_list_empty_when_no_file(store: TokenStore) -> None:
    """First-run UX: no file, list returns empty, no error."""
    assert store.list() == []


def test_list_includes_revoked(store: TokenStore) -> None:
    """Revoked entries are kept on disk and visible in list, so the
    operator can see what used to exist (audit)."""
    store.create("laptop1")
    store.create("laptop2")
    store.revoke("laptop1")
    rows = store.list()
    assert {r.name for r in rows} == {"laptop1", "laptop2"}
    by_name = {r.name: r for r in rows}
    assert by_name["laptop1"].revoked is True
    assert by_name["laptop2"].revoked is False


def test_list_is_sorted_by_creation(store: TokenStore) -> None:
    store.create("a")
    store.create("b")
    store.create("c")
    assert [r.name for r in store.list()] == ["a", "b", "c"]


def test_list_public_dict_excludes_token_hash() -> None:
    """`to_public_dict` must not leak the token hash — that's the
    secret's last line of defence if the file leaks."""
    rec = tokens_mod.TokenRecord(
        name="x", created_at="2026-01-01T00:00:00Z", revoked=False
    )
    d = rec.to_public_dict()
    assert "token" not in d
    assert "token_hash" not in d
    assert "hash" not in d
    assert d == {
        "name": "x",
        "created_at": "2026-01-01T00:00:00Z",
        "revoked": False,
        "last_used_at": None,
    }


# ---------------------------------------------------------------------------
# Encryption at rest (REQUIREMENTS NFR-1.2)
# ---------------------------------------------------------------------------


def test_raw_file_is_fernet_ciphertext(tmp_path: Path, key: str) -> None:
    """The file on disk must start with the Fernet version byte and
    never contain the plaintext token."""
    p = tmp_path / "tokens.json"
    store = TokenStore(path=p, key=key)
    token = store.create("laptop1")
    raw = p.read_bytes()
    # Fernet ciphertext starts with the version byte 0x80 → urlsafe-
    # base64 encoded as "gAAAAA".
    assert raw.startswith(b"gAAAAA"), (
        f"token store is not Fernet-encrypted; first bytes: {raw[:10]!r}"
    )
    # Plaintext token must not appear in the ciphertext.
    assert token.encode("ascii") not in raw
    # The JSON literal "laptop1" *does* appear (the name is not a
    # secret) but the Fernet envelope wraps it, so we check the
    # ciphertext doesn't contain the unencrypted JSON structure.
    assert b'"records"' not in raw
    assert b'"token_hash"' not in raw


def test_decrypt_with_different_key_fails(tmp_path: Path) -> None:
    """Rotating the Fernet key without re-pairing should fail loudly,
    not silently accept (which would let the new key's owner in)."""
    p = tmp_path / "tokens.json"
    s1 = TokenStore(path=p, key=Fernet.generate_key().decode())
    s1.create("laptop1")

    s2 = TokenStore(path=p, key=Fernet.generate_key().decode())
    with pytest.raises(TokenStoreError, match="does not match"):
        s2.list()


def test_corrupt_ciphertext_raises(tmp_path: Path, key: str) -> None:
    """If an operator accidentally ``echo 'x' > tokens.json``, we
    should fail with a clear error, not crash with a stack trace."""
    p = tmp_path / "tokens.json"
    p.write_bytes(b"this is not fernet ciphertext at all")
    store = TokenStore(path=p, key=key)
    with pytest.raises(TokenStoreError, match="decrypt"):
        store.list()


def test_decryptable_but_bad_json_raises(tmp_path: Path, key: str) -> None:
    """Edge case: the file is a valid Fernet envelope but the
    plaintext is not JSON (e.g. partial-write that left a valid
    envelope around junk). Should still error cleanly."""
    fernet = Fernet(key.encode("ascii"))
    p = tmp_path / "tokens.json"
    p.write_bytes(fernet.encrypt(b"not json {"))
    store = TokenStore(path=p, key=key)
    with pytest.raises(TokenStoreError, match="not valid JSON"):
        store.list()


def test_decryptable_but_wrong_shape_raises(tmp_path: Path, key: str) -> None:
    """Valid JSON, wrong top-level shape (e.g. an array, or missing
    'records' key) → clear error."""
    fernet = Fernet(key.encode("ascii"))
    p = tmp_path / "tokens.json"
    p.write_bytes(
        fernet.encrypt(json.dumps(["nope", "should", "be", "object"]).encode())
    )
    store = TokenStore(path=p, key=key)
    with pytest.raises(TokenStoreError, match="unexpected shape"):
        store.list()


def test_empty_file_treated_as_empty_store(tmp_path: Path, key: str) -> None:
    """A zero-byte file (e.g. write was interrupted before any bytes
    landed) shouldn't brick the store — treat it as 'no tokens'."""
    p = tmp_path / "tokens.json"
    p.write_bytes(b"")
    store = TokenStore(path=p, key=key)
    assert store.list() == []
    # And subsequent writes should still work.
    store.create("laptop1")
    assert len(store.list()) == 1


# ---------------------------------------------------------------------------
# last_used_at updates on successful validate
# ---------------------------------------------------------------------------


def test_validate_updates_last_used_at(store: TokenStore) -> None:
    token = store.create("laptop1")
    assert store.list()[0].last_used_at is None
    store.validate("laptop1", token)
    assert store.list()[0].last_used_at is not None
    # Second call updates it to a (probably) later timestamp.
    first = store.list()[0].last_used_at
    store.validate("laptop1", token)
    second = store.list()[0].last_used_at
    # In rare cases the two timestamps are equal (second-resolution
    # clock + fast test). We just require both to be set.
    assert first is not None
    assert second is not None


def test_failed_validate_does_not_update_last_used_at(store: TokenStore) -> None:
    store.create("laptop1")
    store.validate("laptop1", "wrong-token")
    assert store.list()[0].last_used_at is None


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------


def test_state_persists_across_instances(tmp_path: Path, key: str) -> None:
    """A second TokenStore pointed at the same file should see the
    first instance's writes (this is how the CLI and the long-running
    server share state)."""
    p = tmp_path / "tokens.json"
    s1 = TokenStore(path=p, key=key)
    token = s1.create("laptop1")

    s2 = TokenStore(path=p, key=key)
    assert [r.name for r in s2.list()] == ["laptop1"]
    assert s2.validate("laptop1", token) is True

    s2.revoke("laptop1")
    # s1 should see the revocation after re-reading.
    assert s1.validate("laptop1", token) is False


# ---------------------------------------------------------------------------
# Construction: bad keys
# ---------------------------------------------------------------------------


def test_empty_key_raises(tmp_path: Path) -> None:
    with pytest.raises(TokenStoreError, match="empty"):
        TokenStore(path=tmp_path / "x", key="")


def test_wrong_length_key_raises(tmp_path: Path) -> None:
    """The most common misconfiguration: paste of a key with
    truncation, whitespace, or the wrong env var's value."""
    with pytest.raises(TokenStoreError, match="wrong length"):
        TokenStore(path=tmp_path / "x", key="too-short")


def test_malformed_but_right_length_key_raises(tmp_path: Path) -> None:
    """44 chars but not valid base64 → Fernet raises → we wrap it."""
    bad = "x" * 44  # wrong chars but right length
    with pytest.raises(TokenStoreError, match="malformed"):
        TokenStore(path=tmp_path / "x", key=bad)


# ---------------------------------------------------------------------------
# token_store_from_config (FR-4.2: clear error when key env unset)
# ---------------------------------------------------------------------------


def test_token_store_from_config_reads_key_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory reads the Fernet key from the env var named in
    the config and constructs a working store."""
    monkeypatch.setenv("MY_PLUGIN_KEY", Fernet.generate_key().decode())
    cfg = NodeServerConfig(
        token_store_path=str(tmp_path / "tokens.json"),
        token_encryption_key_env="MY_PLUGIN_KEY",
    )
    s = token_store_from_config(cfg)
    assert s.path == tmp_path / "tokens.json"
    s.create("laptop1")


def test_token_store_from_config_missing_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset key env var (FR-4.2): clear error naming the var."""
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cfg = NodeServerConfig(
        token_store_path=str(tmp_path / "tokens.json"),
        token_encryption_key_env="MISSING_KEY",
    )
    with pytest.raises(TokenStoreError, match="MISSING_KEY"):
        token_store_from_config(cfg)


def test_token_store_from_config_clear_error_includes_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unset-key error must tell the operator how to fix it
    (REQUIREMENTS FR-4.2: "refuses to start with a clear error message")."""
    monkeypatch.delenv("NOPE", raising=False)
    cfg = NodeServerConfig(
        token_store_path=str(tmp_path / "tokens.json"),
        token_encryption_key_env="NOPE",
    )
    with pytest.raises(TokenStoreError) as exc_info:
        token_store_from_config(cfg)
    msg = str(exc_info.value)
    assert "Fernet.generate_key" in msg
    assert "NOPE" in msg


# ---------------------------------------------------------------------------
# Best-effort last_used_at write (issue #12)
# ---------------------------------------------------------------------------


def test_validate_does_not_leak_oserror_on_write_failure(
    tmp_path: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #12: ``validate()`` must not let an ``OSError`` from the
    best-effort ``last_used_at`` write escape.

    Before the fix, the ``try/except TokenStoreError`` was dead code:
    ``_write_all`` raises ``OSError`` (from ``os.replace`` /
    ``os.fdopen`` / ``tempfile.mkstemp``), not ``TokenStoreError``. A
    successful auth followed by a write failure (disk full, read-only
    directory, permissions) propagated the ``OSError`` out of
    ``validate()`` to the WebSocket handler in ``server.py``, which
    could close the socket with 1011. The docstring promised the
    opposite: "if the write fails (disk full, permissions), the
    validation result is still True."
    """
    p = tmp_path / "tokens.json"
    store = TokenStore(path=p, key=key)
    token = store.create("laptop1")
    # Sanity: round-trip works on a writable path.
    assert store.validate("laptop1", token) is True

    # Simulate a write failure on the post-auth audit update. We patch
    # ``os.replace`` (the only function inside ``_write_all`` whose
    # failure would surface on a normal path) to raise OSError, which
    # is what the underlying filesystem would do on ENOSPC / EROFS /
    # EACCES.
    def boom_replace(src: str, dst: str, *a: object, **kw: object) -> None:
        raise OSError(28, "No space left on device (simulated)")

    monkeypatch.setattr("os.replace", boom_replace)
    # Must still return True — the auth succeeded; the audit update
    # is best-effort per the docstring contract.
    assert store.validate("laptop1", token) is True


def test_validate_does_not_leak_typeerror_on_write_failure(
    tmp_path: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke test for the broader ``except Exception`` boundary: even a
    non-OSError exception (here ``TypeError``) from the write path
    must not leak. This is the "we catch ALL write-time errors, not
    just the ones we know about" promise of the fix.
    """
    p = tmp_path / "tokens.json"
    store = TokenStore(path=p, key=key)
    token = store.create("laptop1")

    def boom_replace(src: str, dst: str, *a: object, **kw: object) -> None:
        raise TypeError("simulated type error in write path")

    monkeypatch.setattr("os.replace", boom_replace)
    assert store.validate("laptop1", token) is True


def test_validate_write_failure_does_not_corrupt_existing_store(
    tmp_path: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure during a best-effort ``last_used_at`` update
    must not corrupt the on-disk store. The original valid record
    must still validate on the next call (after the patched-out
    failure is removed) and must still round-trip across instances.
    """
    p = tmp_path / "tokens.json"
    store = TokenStore(path=p, key=key)
    token = store.create("laptop1")

    def boom_replace(src: str, dst: str, *a: object, **kw: object) -> None:
        raise OSError(28, "No space left on device (simulated)")

    monkeypatch.setattr("os.replace", boom_replace)
    # Auth still succeeds under failure.
    assert store.validate("laptop1", token) is True
    # Restore the real os.replace — subsequent writes work again.
    monkeypatch.undo()

    # A second instance can read the file (i.e. the original record is
    # intact; the failed write left the previous good file in place
    # because atomic rename only happens at the end of _write_all).
    s2 = TokenStore(path=p, key=key)
    assert [r.name for r in s2.list()] == ["laptop1"]
    assert s2.validate("laptop1", token) is True


# ---------------------------------------------------------------------------
# Power-loss durability (issue #16)
# ---------------------------------------------------------------------------


def test_write_all_fsyncs_temp_file_then_parent_dir(
    tmp_path: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #16: ``_write_all`` must ``fsync`` the temp file *before*
    the rename and the parent directory *after* the rename. Without
    these, a power loss between the rename and the next page-cache
    flush can leave the new file empty/stale or the directory still
    pointing at the old inode.

    We record every ``os.fsync`` call (the actual fd is opaque) plus
    the ``os.replace`` call, then assert the ordering:

        fsync(temp_fd)  →  os.replace(tmp, target)  →  fsync(parent_dir_fd)

    The parent-dir fsync is the one issue #16 says is missing
    entirely — a regression that drops it would fail here.
    """
    p = tmp_path / "tokens.json"
    store = TokenStore(path=p, key=key)

    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd: int) -> None:
        events.append(f"fsync({fd})")
        real_fsync(fd)

    def recording_replace(src: str, dst: str, *a: object, **kw: object) -> None:
        events.append(f"replace({Path(src).name}->{Path(dst).name})")
        real_replace(src, dst, *a, **kw)

    monkeypatch.setattr("os.fsync", recording_fsync)
    monkeypatch.setattr("os.replace", recording_replace)

    store.create("laptop1")

    # We expect at least two fsyncs per write: one for the temp file,
    # one for the parent directory. We can't peek at the fds directly,
    # but we can assert the order of events and that the replace sits
    # between two fsyncs.
    fsync_indices = [i for i, e in enumerate(events) if e.startswith("fsync(")]
    replace_indices = [
        i for i, e in enumerate(events) if e.startswith("replace(")
    ]
    assert replace_indices, "os.replace was never called"
    assert len(fsync_indices) >= 2, (
        f"expected at least 2 fsync calls (temp file + parent dir), "
        f"got {len(fsync_indices)}: {events!r}"
    )
    # The replace must happen *after* the first fsync (the temp file's)
    # and *before* the last fsync (the parent dir's).
    first_fsync = fsync_indices[0]
    last_fsync = fsync_indices[-1]
    replace_at = replace_indices[0]
    assert first_fsync < replace_at < last_fsync, (
        f"expected fsync(temp) -> replace -> fsync(parent) ordering, "
        f"got events: {events!r}"
    )


def test_write_all_rename_failure_leaves_no_half_written_state(
    tmp_path: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #16: if ``os.replace`` raises mid-write, the on-disk
    store must be unchanged (atomic rename semantics) and the
    temp file must be cleaned up so the directory doesn't accumulate
    ``.tokens.*.tmp`` debris.

    This restates the "no half-written state" invariant from
    issue #12 against the fsync-aware code path. It exercises the
    ``except Exception`` branch that wraps both the file write *and*
    the parent-dir fsync.
    """
    p = tmp_path / "tokens.json"
    store = TokenStore(path=p, key=key)
    store.create("laptop1")

    def boom_replace(src: str, dst: str, *a: object, **kw: object) -> None:
        raise OSError(28, "No space left on device (simulated)")

    monkeypatch.setattr("os.replace", boom_replace)

    # A write that fails mid-way must propagate (validate's
    # best-effort path will catch it; create/revoke must NOT).
    with pytest.raises(OSError):
        store.create("laptop2")

    # No .tmp debris in the directory — the except branch cleaned up.
    debris = list(tmp_path.glob(".tokens.*.tmp"))
    assert debris == [], f"temp file cleanup failed: {debris!r}"

    # The original record is still intact.
    s2 = TokenStore(path=p, key=key)
    assert [r.name for r in s2.list()] == ["laptop1"]


def test_write_all_swallows_fsync_oserror(
    tmp_path: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #16: ``os.fsync`` raising ``OSError`` (e.g. on FUSE or
    ``/dev/null``) must not crash the write path. The write either
    succeeded (in which case we accept the durability loss) or it
    raised for some other reason, and a transient fsync failure
    is not a reason to convert a successful write into a crash.
    """
    p = tmp_path / "tokens.json"
    store = TokenStore(path=p, key=key)

    def boom_fsync(fd: int) -> None:
        raise OSError(22, "Invalid argument (simulated FUSE)")

    monkeypatch.setattr("os.fsync", boom_fsync)

    # The create must still succeed — fsync failure is best-effort.
    token = store.create("laptop1")
    # And a second store can read the new record.
    s2 = TokenStore(path=p, key=key)
    assert [r.name for r in s2.list()] == ["laptop1"]
    assert s2.validate("laptop1", token) is True


def test_write_all_round_trip_after_fsync_changes(
    tmp_path: Path, key: str
) -> None:
    """End-to-end smoke for issue #16: with the fsync recipe in
    place, a normal create → revoke → create-new flow on a real
    on-disk store still round-trips. This is the "we didn't break
    the happy path while adding durability" guard.

    ``list()`` returns every record (revoked and active) in creation
    order, so after the revoke-then-recreate sequence the store
    holds two records with the same name. We assert:
      * the second store can read both records (no corruption),
      * the new token validates,
      * the old (revoked) token does not.
    """
    p = tmp_path / "tokens.json"
    store = TokenStore(path=p, key=key)

    token_a = store.create("laptop1")
    store.revoke("laptop1")
    assert store.validate("laptop1", token_a) is False

    token_b = store.create("laptop1")
    assert store.validate("laptop1", token_b) is True

    # Across instances: a fresh TokenStore reads the latest on-disk
    # state, including the post-fsync durability.
    s2 = TokenStore(path=p, key=key)
    assert [r.name for r in s2.list()] == ["laptop1", "laptop1"]
    assert s2.validate("laptop1", token_b) is True
    assert s2.validate("laptop1", token_a) is False


# ---------------------------------------------------------------------------
# Constant-time compare (REQUIREMENTS NFR-1.1)
# ---------------------------------------------------------------------------


def test_validate_uses_hmac_compare_digest() -> None:
    """Regression guard: NFR-1.1 mandates constant-time token compare.

    We don't have a reliable cross-platform way to assert timing, so
    we check the source directly. A future PR that swaps ``==`` for
    the comparison would fail this test and force the change through
    a SECURITY-REVIEW update.
    """
    src = Path(tokens_mod.__file__).read_text(encoding="utf-8")
    assert "hmac.compare_digest" in src, (
        "tokens.py must use hmac.compare_digest for token comparison "
        "(REQUIREMENTS NFR-1.1)."
    )
    # The string equality operator '==' should not appear in the
    # validate() body. We use a regex limited to a window around the
    # 'def validate' line to avoid false positives elsewhere in the
    # file.
    match = re.search(
        r"def validate\([^)]*\).*?(?=\n    def |\nclass |\Z)", src, re.DOTALL
    )
    assert match, "could not locate validate() body"
    validate_body = match.group(0)
    # No equality check on candidate/match (the string hash compare
    # must go through hmac.compare_digest, not == / !=).
    assert "candidate ==" not in validate_body
    assert "match.token_hash ==" not in validate_body
