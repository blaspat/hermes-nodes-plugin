# hermes-nodes-plugin: Requirements

This document is the source of truth for what the `hermes-nodes-plugin` package must do. The implementation plan in `/home/patrick/.hermes/plans/2026-06-04_001727-hermes-nodes.md` derives from this.

> **Audience:** Kate (implements), Quinn (validates against), Patrick (approves).

## 1. Functional requirements

### FR-1: Pairing

**FR-1.1** `hermes node pair --name <name>` generates a cryptographically random token (32 bytes, base64url), binds it to a node name, persists it encrypted at rest, and prints the token to the operator.

**FR-1.2** The token can be used exactly once for pairing (but is not invalidated by use — the binding lasts until revoked).

**FR-1.3** `hermes node list` shows all paired nodes and their current connection state (`connected`, `disconnected`, `never_seen`).

**FR-1.4** `hermes node revoke --name <name>` deletes the token, drops any active connection, and the node cannot reconnect.

**FR-1.5** Node names must be unique. Attempting to pair with an existing name without `--force` is an error.

**Acceptance:** Patrick can run `hermes node pair --name work-laptop`, copy the printed token, run `hermes-node pair --server ... --token ...` on the laptop, and see `work-laptop` appear in `hermes node list` as `connected`.

### FR-2: Connection lifecycle

**FR-2.1** The server accepts inbound WSS connections on the configured port (default 6969), performs TLS, and waits for a `hello` message.

**FR-2.2** The server validates the node's `hello.protocol_version` against its own; rejects with code `4002` if incompatible.

**FR-2.3** The server validates the `auth` message: token matches the bound node name, token not revoked, node name on the `hello` matches the name on the `auth`. Reject with code `4001` otherwise.

**FR-2.4** On successful auth, the server registers the connection in the in-memory node registry and starts sending/receiving application messages.

**FR-2.5** If the connection drops (TCP reset, missed heartbeats, explicit close), the server removes the node from the registry and marks it `disconnected` in `hermes node list`.

**FR-2.6** The server enforces rate limits: max 100 calls/second per node, sliding window. Excess → close with `4004`.

**Acceptance:** End-to-end test in `tests/e2e/test_full_flow.py` exercises: pair, connect, auth, exec, disconnect, reconnect, revoke-during-connection.

### FR-3: Kate (Hermes agent) integration

**FR-3.1** The plugin registers a `NodeEnvironment` class that implements Hermes's `BaseEnvironment` interface, taking a `target` (node name) as constructor arg.

**FR-3.2** The plugin registers four Kate-facing tools:
- `node_exec(target, command, cwd=None, env=None, timeout_ms=60000)` — runs a shell command on the named node
- `node_read(target, path)` — reads a file from the named node
- `node_write(target, path, content, mode="overwrite")` — writes a file on the named node
- `node_list()` — returns all known nodes with their connection state

**FR-3.3** The `node_*` tools are registered in the `terminal` and `file` toolsets (or whatever Hermes uses for environment-style tools — verify against `tools/registry.py` during implementation).

**FR-3.4** If Kate calls `node_exec` against a disconnected node, the call returns a structured error in under 2 seconds with this message: `"node 'X' is not connected; check 'hermes node list' to see its current state"`.

**FR-3.5** Persistent cwd + env are maintained on the node side; the Kate side does not need to track them.

**FR-3.6** Output is bounded to 10 MB per stream; larger output is truncated with a warning surfaced to Kate.

**Acceptance:** In a Kate session, `node_exec("work-laptop", "pytest tests/ -q")` returns real test output from the laptop. Running the same call twice in a row with `cd /tmp` in between proves cwd persistence.

### FR-4: Configuration

**FR-4.1** Plugin reads configuration from `~/.hermes/hermes-nodes.yaml` and env vars. Env vars override file values. Defaults if neither set:
- `host`: `127.0.0.1` (the safe default — assumes nginx is fronting TLS)
- `port`: `6969`
- `tls_cert_path`: unset (TLS termination is expected at the reverse proxy)
- `tls_key_path`: unset
- `token_store_path`: `~/.hermes/nodes/tokens.json`
- `token_encryption_key_env`: `HERMES_NODES_TOKEN_KEY` (the env var name, not the value)

**FR-4.1a** The plugin MUST support two TLS modes:
- **Reverse-proxied (default):** listens on `127.0.0.1:6969` plain HTTP. TLS is terminated by nginx/Caddy/etc in front.
- **Direct TLS:** listens on `0.0.0.0:6969` (or any host) with `tls_cert_path` + `tls_key_path` configured. Used when no reverse proxy is in front.

**FR-4.1b** Selection is automatic based on config: if both `tls_cert_path` and `tls_key_path` are set, use direct TLS; otherwise listen on plain HTTP (assume reverse proxy).

**FR-4.2** The token encryption key is loaded from the env var named in `token_encryption_key_env`. If absent, the plugin refuses to start with a clear error message.

**FR-4.3** First-run UX: if `tokens.json` doesn't exist, `hermes node pair` creates it (with a generated encryption key if `HERMES_NODES_TOKEN_KEY` is unset, warning the operator to persist it).

**Acceptance:** Plugin starts with config from file; env var override beats file; missing token key produces a clear error. Both TLS modes (reverse-proxied and direct) work end-to-end.

### FR-5: Audit logging

**FR-5.1** Every successful and failed call is logged to `~/.hermes/logs/nodes-audit.log` (one JSON object per line) with fields: `ts`, `node`, `action`, `request_id`, `duration_ms`, `exit_code` (for exec), `status` (`ok`/`error`/`timeout`), `error` (if status=error).

**FR-5.2** Audit log is append-only. There is no CLI command to delete entries.

**FR-5.3** The format matches the node-side audit log exactly so logs from both sides can be correlated by `request_id`.

**FR-5.4** Default retention: 1 year. Configurable via env var `HERMES_NODES_AUDIT_RETENTION_DAYS`.

**Acceptance:** After 5 mixed calls (3 ok, 1 timeout, 1 path-denied), `tail -f` of the audit log shows 5 entries with correct fields and request IDs that match the node's audit log.

### FR-6: Error handling

**FR-6.1** The plugin never panics. All exceptions are caught, logged, and surfaced as structured errors to Kate.

**FR-6.2** If the WSS server fails to bind (port in use, missing cert), the plugin logs the error and does not block Hermes startup.

**FR-6.3** Connection errors are logged at WARN, not ERROR, since they're routine (laptops go offline).

**Acceptance:** Stopping the node mid-call causes Kate to receive a clear "node disconnected" error within 2 seconds, not a hang.

---

## 2. Non-functional requirements

### NFR-1: Security

**NFR-1.1** Tokens are compared in constant time (use `hmac.compare_digest`).

**NFR-1.2** Tokens are stored encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256). The encryption key is never logged or printed.

**NFR-1.3** The plugin does not write to disk anywhere outside `~/.hermes/` and the configured log paths.

**NFR-1.4** All incoming WSS connections go through TLS. The server refuses to start without a valid cert.

**NFR-1.5** No dependency on `eval`, `exec`, `os.system`, or any dynamic code execution from external input.

### NFR-2: Performance

**NFR-2.1** The plugin must not add more than 50ms latency to Kate's tool calls (excluding the actual round-trip time to the laptop).

**NFR-2.2** The WSS server handles at least 50 concurrent node connections on a 1-CPU VPS without dropping messages.

**NFR-2.3** Memory footprint under 100 MB at idle, under 500 MB at 50 concurrent nodes.

### NFR-3: Observability

**NFR-3.1** All log lines go through the Hermes logger (so they show up in `hermes logs`).

**NFR-3.2** The plugin registers a `hermes nodes` subcommand group in the Hermes CLI (verifiable by `hermes --help`).

### NFR-4: Compatibility

**NFR-4.1** The plugin works with any Hermes version that supports the entry-point plugin system (v0.13+). The protocol version of nodes this accepts is `0.1.x`.

**NFR-4.2** Python 3.11+ (matches Hermes's runtime).

**NFR-4.3** The plugin can be installed into any Hermes profile (claire, luna, kate, custom) without per-profile code changes.

### NFR-5: Testability

**NFR-5.1** Unit test coverage >= 80% (enforced by `pytest-cov` in CI).

**NFR-5.2** Every public function in `hermes_nodes_plugin` has at least one test.

**NFR-5.3** Integration tests can run without a real laptop (use a mock WSS node in-process).

---

## 3. Explicitly out of scope (v1)

- Camera, screen, browser, mic, push notifications, location
- Live file watcher / auto-sync
- Multi-server federation
- Automatic token rotation
- GUI pairing flow (QR codes, etc.)
- OS keychain integration for token storage on the node side
- Per-call RBAC (a token grants all capabilities to its bound node name)
- Hot-reload of config (restart required)
- IPv6-only deployments (works, but not tested in CI)

---

## 4. Acceptance criteria for "v1 done"

All of the following must be true:

1. ✅ Kate (running in the `kate` profile on a VPS) can run `node_exec("work-laptop", "echo hello")` and receive `hello\n` as the result.
2. ✅ `hermes node pair --name work-laptop` generates a token. `hermes node revoke --name work-laptop` invalidates it. The laptop can no longer reconnect.
3. ✅ When the laptop is offline, Kate's `node_exec` call returns within 2 seconds with a clear error message.
4. ✅ Audit logs on both the laptop and the VPS show every call with matching `request_id`.
5. ✅ The plugin's unit test suite passes in CI with >= 80% coverage.
6. ✅ The e2e test in `tests/e2e/test_full_flow.py` passes on Linux amd64 (CI), and the install scripts work on a clean Mac and a clean Windows machine (manual verification by Patrick).
7. ✅ `SECURITY-REVIEW.md` exists and is suitable for showing to a corporate security team.
8. ✅ A `pip install git+https://github.com/blaspat/hermes-nodes-plugin.git` in any Hermes profile's venv results in the plugin auto-loading and the `hermes node ...` commands appearing in the CLI.

---

## 5. Resolved decisions

All open questions from earlier drafts are decided. Recording them here so future contributors don't re-litigate.

| # | Question | Decision | Date |
|---|---|---|---|
| 1 | Default WSS port | **6969** | 2026-06-04 |
| 2 | Token rotation cadence | **Manual only.** `hermes node revoke` + `hermes node pair` is the rotation path. v1 ships with no auto-rotation. | 2026-06-04 |
| 3 | Audit log retention | **90 days laptop-side, 1 year server-side.** Both configurable via `audit_retention_days` (server) and `audit_retention_days` in node config (laptop). | 2026-06-04 |
| 4 | TLS cert source | **Configurable.** Default mode is reverse-proxied (nginx fronts TLS, plugin binds to `127.0.0.1:6969` plain HTTP). Direct TLS mode available when no reverse proxy is in front. See `README.md` "TLS configuration" for the nginx snippet. | 2026-06-04 |
| 5 | QR code in `hermes node pair` | **Text token only for v1.** QR code is a v2 nice-to-have. | 2026-06-04 |
