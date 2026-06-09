# Security Review — hermes-nodes-plugin

**Date:** 2026-06-09
**Reviewer:** Kate (code-agent)
**Source revision:** `961df1a` (main, post-`#33`)
**Verdict:** No critical findings. One spec gap (FR-2.6 rate limit) is acknowledged and tracked. Three intentional spec deviations are stricter or equally safe. Suitable for a corporate security team's initial posture review.

## Scope

**In scope.** The Python plugin at `github.com/blaspat/hermes-nodes-plugin` — code under `hermes_nodes_plugin/`, tests under `tests/`, and configuration in `pyproject.toml` and `REQUIREMENTS.md`. The review builds on the v0.1.0 requirements audit at `REQUIREMENTS-AUDIT.md` (PR #33), which already enumerates per-row evidence for every FR and NFR; this document summarises its security-relevant findings.

**Out of scope.** The Go client at `github.com/blaspat/hermes-nodes` (separate repo, separate review); the Hermes Agent core (treated as a trusted consumer of the plugin's `BaseEnvironment` and CLI registration); the operator's host (filesystem permissions, secrets manager, and reverse-proxy config are documented in `README.md`).

## What was checked

**Requirements coverage.** Every FR (1.1–6.3) and NFR (1.1–5.3) in `REQUIREMENTS.md` was reviewed against the source. Per-row evidence lives in `REQUIREMENTS-AUDIT.md`; the pass/fail count is 27 of 31 implemented, with 3 intentional deviations and 1 missing feature (FR-2.6). The NFR-1 (security) sub-suite is the focus here.

**NFR-1.1 — constant-time token comparison.** `TokenStore.validate()` at `hermes_nodes_plugin/tokens.py:474` calls `hmac.compare_digest(candidate, match.token_hash)`. Hashes are equal-length hex digests, so the constant-time contract holds.

**NFR-1.2 — Fernet at rest, key not logged.** `TokenStore` uses `cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256). The key is loaded from `HERMES_NODES_TOKEN_KEY` and passed only to the Fernet constructor. A grep of `hermes_nodes_plugin/` for `print(key)`, `logger.*key`, and `repr(key)` returns no matches in operational code paths. The store file is opened with mode `0o600` (`tokens.py:690`).

**NFR-1.3 — write paths.** All writes are confined to: `token_store_path` (`~/.hermes/nodes/tokens.json` by default), `audit_log_path` (`~/.hermes/logs/nodes-audit.log`), and `/tmp/` for atomic-write temps. `audit.py:520` sets parent directory mode `0o700`; `audit.py:528` opens the file with mode `0o600`. No writes outside the configured locations.

**NFR-1.4 — TLS at the WSS layer.** Two modes via `config.uses_tls()` (`config.py:165`): direct-TLS (uvicorn binds with `ssl_context`) and reverse-proxied (plain HTTP, terminate TLS upstream). Default is reverse-proxied (Resolved Decision #4). Direct-TLS mode requires valid cert and key paths; uvicorn fails to start with a clear error otherwise. The spec's "refuse to start without a valid cert" guarantee holds in direct mode, not in default — a deviation in text but not a weakening of posture, since in default mode TLS is terminated at the documented reverse proxy.

**NFR-1.5 — no dynamic exec.** `grep -rE '\beval\(|\bos\.system\(|\bexec\(' hermes_nodes_plugin/` returns zero matches in operational code. Dependencies are not in scope for this grep; supply-chain review is in `Findings → Out-of-scope risks`.

**Auth path.** `server.py:_ws_handler` reads `hello` with a bounded `asyncio.wait_for` (`server.py:319-322`); the version check closes with `4002` on major mismatch; the auth frame is validated via `TokenStore.validate()` and closes with `4001` on failure. The token never appears in a log line; only the node name does.

**Storage and audit.** `TokenStore` encrypts with Fernet, never logs the key, opens its file `0o600`, and uses `fcntl.flock` for cross-process safety. `AuditWriter.record()` is contractually never-raises (PR #28, issue #11) — any write failure is swallowed with `logger.warning`, so an audit disk failure cannot crash the server or block an authenticated call. Append-only; no CLI command deletes entries.

**WSS lifecycle close codes (PROTOCOL §4).** `4001` auth failed, `4002` protocol version mismatch, `4003` message out of order, `4004` reserved for resource-exhaustion-style rejections (currently the handshake-timeout close per issue #13; FR-2.6 will reuse it for the rate-limit case once landed). All four are named constants in `server.py:74-81` and used consistently.

**Plugin lifecycle.** `__init__.py:58-82` wraps `register()` in try/except. `lifecycle.py:240-256` detects a WSS bind failure, cancels the server task, logs the error, and returns rather than re-raising — so a port conflict does not block Hermes startup. The other lifecycle hooks get the same defensive try/except.

## Findings

**None critical.** No issues that would block a v1 release. The most material item is the missing FR-2.6 rate limit, a DoS protection gap rather than a confidentiality or integrity issue.

**FR-2.6 — rate limiting (100 calls/sec/node, sliding window, close 4004 on excess) is not implemented.** The audit at `REQUIREMENTS-AUDIT.md` §FR-2.6 confirms no rate-limit class exists; `4004` is currently used for handshake timeout only. The attack surface is the operator's paired nodes (not the public internet, since the server is intended to run on a private network behind a reverse proxy), so practical exposure is bounded. Mitigation: pair only trusted devices (`SECURITY.md`). Remediation: tracked as a follow-up card; ~80 LOC + 5–6 tests.

**Intentional deviations from the spec, recommended for spec amendment rather than code change:**

- **FR-3.3 — toolset name.** Tools register in `hermes_nodes` (`__init__.py:77`), not the spec's `terminal`/`file` toolsets. Stricter (toggleable as a unit); locked in by `tests/test_tools.py:210-215`.
- **FR-4.3 — first-run key generation.** Refuses to start with a clear error if `HERMES_NODES_TOKEN_KEY` is unset (`tokens.py:310`), rather than auto-generating and warning. Stricter (no surprise persistence requirement).
- **NFR-1.4 — TLS not required by default.** Already approved as Resolved Decision #4.

**Out-of-scope risks (mentioned, not fixed here):** the Go client has not been independently reviewed (it is the implicit trust root for path-allowlist enforcement, the project's load-bearing security claim per `SECURITY.md`); the threat model in `SECURITY.md` has not been updated since v0.1.0; no third-party audit has been performed (recommended before enterprise adoption).

## Residual risks

**Filesystem trust.** The WSS server trusts the operator's `tokens.json` filesystem. If the host is compromised, an attacker with read access can exfiltrate ciphertext (useless without the Fernet key) and with write access can replace tokens. Mitigation: file mode `0600` is set by `tokens.py:690`; the directory mode `0700` is the operator's responsibility and is documented in `README.md`.

**No per-call rate limit until FR-2.6 lands.** A misbehaving or compromised paired node can drive the server into resource exhaustion. Workaround for early adopters: run the plugin behind a reverse proxy that enforces its own per-IP rate limits (nginx `limit_req`, Caddy `ratelimit`, etc.). The reverse proxy is already the operator's TLS termination point in default mode.

**Fernet key handling.** The key lives in `HERMES_NODES_TOKEN_KEY`. If the env var leaks (process listing, `/proc/<pid>/environ`, a careless log line), all stored tokens can be decrypted. Mitigation: load the key from a secrets manager (AWS Secrets Manager, HashiCorp Vault) or use systemd `LoadCredential=` on systemd hosts. The `config.py` interface is env-var based, so no code change is needed to switch.

## Verification commands

```bash
# 1. File modes on the token store and audit log
stat -c '%a %n' ~/.hermes/nodes/tokens.json ~/.hermes/logs/nodes-audit.log
# Expect: 600 on each file; parent dirs at 700.

# 2. No dynamic exec in the plugin source
grep -rE '\beval\(|\bos\.system\(|\bexec\(' hermes_nodes_plugin/
# Expect: no matches.

# 3. The Fernet key is not echoed anywhere in the local log dir
grep -rE 'HERMES_NODES_TOKEN_KEY\s*=|token_encryption_key' ~/.hermes/logs/ ~/.hermes/nodes/
# Expect: no matches. (The key itself must never appear in a log line.)

# 4. Protocol handshake version sanity check
python -c "from hermes_nodes_plugin.server import PROTOCOL_MAJOR; print(PROTOCOL_MAJOR)"
# Expect: 0

# 5. Local test suite is green
python -m pytest -q
# Current snapshot: ~325 collected, a few skipped pending FR-2.6.
```

## Cross-references

- `REQUIREMENTS.md` v1 acceptance criterion #7 — the requirement this document satisfies.
- `REQUIREMENTS-AUDIT.md` (PR #33) — per-row evidence for every FR and NFR.
- `SECURITY.md` — the threat model and disclosure process.
- `hermes_nodes_plugin/tokens.py`, `audit.py`, `server.py`, `lifecycle.py` — source files referenced throughout.
