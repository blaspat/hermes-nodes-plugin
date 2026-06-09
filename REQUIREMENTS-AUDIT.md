# Requirements Audit — v0.1.0

**Date:** 2026-06-09
**Auditor:** Claire
**Scope:** `hermes-nodes-plugin` source at `0df4806` (main, after v0.2.1 hardening)
**Method:** Read each FR + NFR in `REQUIREMENTS.md`, grep the plugin source + tests for the corresponding code/assertion, classify as ✅ / ⚠️ / ❌.

---

## FR-1 Pairing

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-1.1 | `hermes node pair` generates 32-byte base64url token, Fernet at rest, prints to operator | ✅ | `tokens.py:62` (32 bytes / base64url), `tokens.py:280` (Fernet store), `cli.py` prints to stdout |
| FR-1.2 | Token usable, lasts until revoked (not invalidated by use) | ✅ | `tokens.py:464` matches by `(name, token_hash)`, no `use_count` field; binding persists until `revoke()` |
| FR-1.3 | `hermes node list` shows `connected` / `disconnected` / `never_seen` | ✅ | `cli.py:82` `STATE_NEVER_SEEN`, `cli.py:340-367` rendering |
| FR-1.4 | Revoke deletes token, drops connection, prevents reconnect | ✅ | `tokens.py:399-420` `revoke()`, `registry.py:207-244` `unregister(expected_session_id=...)`; auth rejects revoked tokens (`server.py:404`) |
| FR-1.5 | Unique names, `--force` to override | ✅ | `cli.py:137` `--force` flag, `cli.py:302-313` enforcement, `tokens.py:370-372` uniqueness check |

**FR-1: 5/5 implemented.**

## FR-2 Connection lifecycle

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-2.1 | Server accepts inbound WSS, performs TLS, waits for hello | ✅ | `server.py:create_app` exposes `/ws/nodes`; `lifecycle.py:165-170` binds uvicorn with ssl when configured |
| FR-2.2 | Validates `hello.protocol_version`, closes 4002 on mismatch | ✅ | `server.py:75` `CLOSE_PROTOCOL_VERSION = 4002`, `server.py:365-369` enforcement, `_major_compatible()` at line 552 |
| FR-2.3 | Validates auth (token match, not revoked, name match), closes 4001 | ✅ | `server.py:74` `CLOSE_AUTH_FAILED = 4001`, `tokens.py:434-474` `verify_presented` with `hmac.compare_digest`, `server.py:404/436/452/460` enforcement |
| FR-2.4 | On auth OK, registers connection in registry, starts app messages | ✅ | `registry.py:register()` after `auth_ok`, `server.py` continues to message loop |
| FR-2.5 | Drops removed from registry, marked `disconnected` | ✅ | `registry.py:unregister()` + `lifecycle.py:_sweep_stale_connections()` (issue #19); `cli.py` reflects state in `list` |
| FR-2.6 | **Rate limit 100 calls/sec per node, sliding window, close 4004 on excess** | ❌ | **NOT IMPLEMENTED.** No rate-limit class anywhere in `server.py`; no calls/second tracking; no `sliding_window` references. 4004 is used for handshake timeout (a different code path) but never for rate limit. This is a real spec gap. |

**FR-2: 5/6 implemented; FR-2.6 missing.**

### FR-2.6 — what to do

Add a `_RateLimiter` class (e.g. `server.py` or new `ratelimit.py`) keyed on `node_name`, sliding 1-second window, evict on the 101st call in any window, send a `rate_limit` error frame and close 4004. Need:

- Class + tests (window-rollover edge cases, per-node isolation, close-vs-deny choice)
- Plumb into the message dispatch loop in `server.py` (after the `auth_ok` branch, before the action handler)
- Add to the e2e test (which doesn't exist yet — see v1 criterion #6)

Effort estimate: small. ~80 LOC + 5-6 tests. Genuine missing requirement, not a polish item.

## FR-3 Agent integration

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-3.1 | Plugin registers `NodeEnvironment` implementing `BaseEnvironment` | ✅ | `environment.py:1` "implements `BaseEnvironment`"; `tools.py` constructs `NodeEnvironment` per call |
| FR-3.2 | Four tools: `node_exec` / `node_read` / `node_write` / `node_list` | ✅ | `tools.py:59/108/147/201`; `TOOLS` tuple at `tools.py:384` |
| FR-3.3 | **Tools registered in `terminal` and `file` toolsets** | ⚠️ | **Deviation.** Registered in the plugin's own `hermes_nodes` toolset instead (`__init__.py:77`). `tests/test_tools.py:210-215` locks this in. **Decision needed:** is this an intentional departure from the spec, or a bug to fix? My read: intentional — the plugin wants to be toggleable as a unit — but it diverges from the requirement. |
| FR-3.4 | Disconnected `node_exec` returns structured error in < 2s with exact message | ✅ | `environment.py:340-342` raises `NodeNotConnectedError` with the literal spec message; check is a single `await registry.get()` (no network await) → well under 2s |
| FR-3.5 | cwd/env persisted on node side, Kate side doesn't track | ✅ | `environment.py:execute()` passes `cwd`/`env` to node per-call; `tools.py:node_exec` does the same — no client-side state |
| FR-3.6 | Output bounded to 10 MB per stream, truncation surfaced | ✅ | Truncation happens on the Go node side; `environment.py:937` appends `[output truncated at 10MB]` hint; `MAX_FILE_BYTES = 10 * 1024 * 1024` at `environment.py:100` for `node_read` |

**FR-3: 5/6 implemented; FR-3.3 deviates from spec (decision needed).**

## FR-4 Configuration

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-4.1 | YAML + env vars, env > file, all defaults documented | ✅ | `config.py:load_config` with `env > file_data` precedence; all defaults match spec |
| FR-4.1a | Two TLS modes (reverse-proxied + direct) | ✅ | `config.py:uses_tls()` at line 171; `server.py:run_server` at line 670-677 conditionally passes ssl kwargs |
| FR-4.1b | Auto-select mode based on config | ✅ | Same — `config.uses_tls()` is the single decision point |
| FR-4.2 | Token key from env var, refuse to start with clear error if missing | ✅ | `config.py:token_encryption_key()` at line ~190 returns None when unset; `tokens.py:313/325/335/719` surface clear errors with the exact `python -c` recipe |
| FR-4.3 | First-run: `tokens.json` missing → create it; if `HERMES_NODES_TOKEN_KEY` unset, generate + warn | ⚠️ | **Partial.** `tokens.py:316` raises "missing key" with a clear `python -c '... Fernet.generate_key() ...'` error. But it does NOT auto-generate a key + warn the operator to persist it — it just refuses. Spec says "creates it (with a generated encryption key if `HERMES_NODES_TOKEN_KEY` is unset, warning the operator to persist it)." Current behavior is stricter (refuse), which is arguably safer (no surprise key in a file the operator doesn't know to back up), but it's a deviation. |

**FR-4: 4/5 implemented; FR-4.3 deviates from spec (decision needed).**

## FR-5 Audit logging

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-5.1 | All calls logged with `ts`/`node`/`action`/`request_id`/`duration_ms`/`exit_code`/`status`/`error` | ✅ | `audit.py:record()` at line 272; `environment.py:_record_audit()` writes the canonical row |
| FR-5.2 | Append-only, no delete command | ✅ | No `audit` subcommand in `cli.py`; only the `node` subcommand exists |
| FR-5.3 | Format matches node-side exactly (correlate by `request_id`) | ✅ | `audit.py:7` docstring explicitly says "joined on `request_id` for an end-to-end trail"; `request_id` is server-generated UUIDv4 |
| FR-5.4 | Default retention 1 year, configurable via env var | ✅ | `audit.py:138` `DEFAULT_RETENTION_DAYS = 365`; `audit.py:140` env var name; `audit.py:670-675` resolver |

**FR-5: 4/4 implemented.**

## FR-6 Error handling

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-6.1 | Plugin never panics, all exceptions caught/logged/structured | ✅ | `__init__.py:58-82` wraps `register()` in try/except; `lifecycle.py:197/247/281` defensive try/except in hooks |
| FR-6.2 | WSS bind failure logs and does not block Hermes startup | ✅ | `lifecycle.py:240-256` — bind failure detected, server task cancelled, error logged, **return** (no raise) |
| FR-6.3 | Connection errors logged at WARN, not ERROR | ✅ | Verified in `server.py` and `lifecycle.py` — disconnect paths use `logger.warning` |

**FR-6: 3/3 implemented.**

---

## NFR-1 Security

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| NFR-1.1 | Constant-time token comparison | ✅ | `tokens.py:474` `hmac.compare_digest(candidate, match.token_hash)` |
| NFR-1.2 | Fernet at rest, key never logged/printed | ✅ | `tokens.py:280` Fernet store; key is read from env var and only used as the Fernet constructor arg. No `print(key)` or `logger.info(key)` in the codebase |
| NFR-1.3 | No writes outside `~/.hermes/` and configured log paths | ✅ | All writes go to `token_store_path`, `audit_log_path`, or `/tmp/` for atomic-write temps. No `Path("/")` or similar |
| NFR-1.4 | All WSS through TLS, server refuses to start without valid cert | ⚠️ | **Deviation from spec, intentional per Resolved Decision #4.** Default mode is plain HTTP behind nginx (relying on the reverse proxy for TLS). Direct-TLS mode (`uses_tls()=True`) does require valid cert+key paths — uvicorn will fail to start with a clear error if the files are missing or unreadable. The spec's "refuses to start without a valid cert" is honored in direct mode, not in default mode. |
| NFR-1.5 | No `eval` / `exec` / `os.system` from external input | ✅ | Plugin code: zero matches for `\beval(`, `\bos.system(`, or bare `\bexec(` (the `node_exec` tool name is a false-positive-safe identifier, not a call). Verified against `hermes_nodes_plugin/` only, not deps |

**NFR-1: 4/5 implemented; NFR-1.4 deviates by resolved decision (already approved).**

## NFR-2 Performance

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| NFR-2.1 | < 50ms overhead per tool call (excl. round-trip) | ✅ (inferred) | `environment.py:execute()` does one `registry.get()`, builds envelope, sends. No blocking I/O on the plugin side. No benchmark in tests, but the code path is short — 50ms budget is easily met |
| NFR-2.2 | 50 concurrent node connections on 1-CPU VPS | ✅ (inferred) | `registry.py` uses a single `asyncio.Lock`; FastAPI/uvicorn handle concurrency. No load test in the suite, but no architectural blockers |
| NFR-2.3 | < 100 MB idle, < 500 MB at 50 nodes | ✅ (inferred) | No large in-process caches; FastAPI app is small; registry holds one dataclass per connection. No measurement in tests |

**NFR-2: 3/3 — implementation likely meets these, but no benchmarks exist to prove it. Spec asks for behavior, not measurement. Recommendation: add a small load test in the e2e suite as a "v0.2 nicety," not a v1 blocker.**

## NFR-3 Observability

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| NFR-3.1 | All log lines through Hermes logger | ✅ | `tools.py:51`, `registry.py:43`, `lifecycle.py:82` all use `logging.getLogger(__name__)` — Hermes's logger config picks these up |
| NFR-3.2 | Plugin registers `hermes node` subcommand group | ✅ | `__init__.py:55-67` `ctx.register_cli_command("node", ...)` |

**NFR-3: 2/2 implemented.**

## NFR-4 Compatibility

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| NFR-4.1 | Works with any Hermes v0.13+; accepts protocol 0.1.x | ✅ | `server.py:105` `PROTOCOL_MAJOR = 0`, `server.py:549` `f"{PROTOCOL_MAJOR}.1.0"` = `0.1.0` |
| NFR-4.2 | **Python 3.11+** | ⚠️ | `pyproject.toml:11` says `requires-python = ">=3.10"`. Off by one minor. The plugin code uses `X \| None` syntax (PEP 604) which is 3.10+, so the code matches the pyproject, but the pyproject is one version behind the spec. Trivial fix. |
| NFR-4.3 | Installable into any Hermes profile (claire, luna, kate, custom) | ✅ | `pyproject.toml` uses generic entry-point, no per-profile code |

**NFR-4: 2/3 implemented; NFR-4.2 pyproject is `>=3.10`, spec asks `3.11+`. Trivial bump.**

## NFR-5 Testability

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| NFR-5.1 | **>= 80% coverage, enforced by pytest-cov in CI** | ❌ | **NOT IMPLEMENTED.** `pyproject.toml` has no `pytest-cov` dep; no `.github/workflows/` exists; no CI config at all. Test suite runs locally and passes 318/318 (with 1 pre-existing flake), but coverage is not measured. |
| NFR-5.2 | Every public function in `hermes_nodes_plugin` has ≥ 1 test | ✅ (approx.) | 318 tests collected; public surface (`register`, `setup_node_cli`, `create_app`, `run_server`, `NodeRegistry`, `TokenStore`, `NodeEnvironment`, `TOOLS`, `load_config`, `NodeServerConfig`, `AuditWriter`) all have tests. No exhaustive audit done — spot checks pass. |
| NFR-5.3 | Integration tests run without a real laptop (mock WSS node) | ✅ | `test_offline.py`, `test_server.py`, `test_lifecycle.py` all use `TestClient` / in-process FastAPI / fake `NodeConnection` — no real network |

**NFR-5: 2/3 implemented; NFR-5.1 missing entirely (no CI, no coverage gate).**

---

## v1 Done — Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | `node_exec("work-laptop", "echo hello")` returns `hello\n` from a Kate profile on a VPS | ✅ (logic in place; real-world demo gated on having a real node + Go binary, which is outside the plugin) |
| 2 | Pair / revoke / can't-reconnect-after-revoke flow | ✅ (all paths tested) |
| 3 | Disconnected → clear error in < 2s | ✅ |
| 4 | Audit logs on both sides correlate by `request_id` | ✅ (server generates UUIDv4; Go side embeds it; format matches) |
| 5 | Test suite passes in CI with ≥ 80% coverage | ❌ (no CI; coverage not measured) |
| 6 | **`tests/e2e/test_full_flow.py` passes on Linux amd64 CI** | ❌ (file doesn't exist; `tests/e2e/` directory doesn't exist) |
| 7 | **`SECURITY-REVIEW.md` exists, suitable for corporate security team** | ⚠️ (have `SECURITY.md` — 110-line threat model + disclosure. Adjacent but not the same. The name and framing are different: a "security review" is a posture assessment with findings + remediation status, while `SECURITY.md` is policy + threat model) |
| 8 | `pip install git+...hermes-nodes-plugin` in any profile → auto-load + CLI subcommand appears | ✅ (entry-point configured correctly per `__init__.py:55-67` and the `pyproject.toml` `[project.entry-points]`) |

**Acceptance: 5/8 pass, 3/8 have gaps (5, 6, 7).**

---

## Summary

**Genuinely missing requirements (need work):**

1. **FR-2.6 — Rate limiting (100 calls/sec, 4004 on excess).** No code, no test. Most concrete spec gap.
2. **NFR-5.1 — CI + coverage gate.** No `.github/workflows/`, no `pytest-cov` in deps. Tests exist and pass locally, but there's no machine-checked 80% threshold.
3. **v1 #6 — `tests/e2e/test_full_flow.py`.** File doesn't exist. The e2e directory doesn't exist. This is the canonical acceptance test the spec was written around.
4. **v1 #7 — `SECURITY-REVIEW.md` (posture assessment).** Have `SECURITY.md` (threat model + disclosure), which is adjacent but not the same artifact. A 1-2 page "what we checked, what we found, residual risks" document on top of the existing `SECURITY.md` is what's missing.

**Spec deviations (intentional, need to update the spec or the code):**

5. **FR-3.3 — Toolset name.** Code uses `hermes_nodes`, spec says `terminal` + `file`. Decision: keep current behavior (test locks it in) and amend the spec, OR change `__init__.py:77` to register in both. **Recommend amend spec** — the plugin-owned toolset is cleaner.
6. **FR-4.3 — First-run key generation.** Code refuses to start with a clear error; spec says generate + warn. Decision: keep the stricter refusal (safer — no surprise persistence requirement) and amend the spec, OR add the generate-and-warn path. **Recommend amend spec.**
7. **NFR-4.2 — Python version.** pyproject says `>=3.10`, spec says `3.11+`. One-line fix: bump `pyproject.toml`.

**Already-approved deviations:**

- **NFR-1.4 — TLS not required by default** (Resolved Decision #4). No action.
- **NFR-2 — Performance NFRs unmeasured.** Not blocking; no spec says "prove it with a benchmark." Add a load test as a v0.2 nicety.

**Pattern:** most of the functional surface is in place. The remaining work is:
- One missing feature (rate limit) — small
- The testing/CI harness (e2e test, coverage gate, CI workflow) — medium
- A posture document (`SECURITY-REVIEW.md`) — small
- Three spec/code reconciliation decisions — small

Roughly 1-2 days of focused work to close the v1 gaps. The code itself is in good shape; the spec contract just isn't fully wired into the CI/deliverable story yet.

---

## Recommendations, in priority order

1. **Build FR-2.6 rate limit.** Real missing requirement, small, gets a checkbox + a test.
2. **Add `tests/e2e/test_full_flow.py`.** This is the canonical acceptance test. Spec literally names it. Even a mocked-node version is enough to satisfy the criterion.
3. **Set up CI.** `.github/workflows/test.yml` with `pytest --cov=hermes_nodes_plugin --cov-fail-under=80`. Closes NFR-5.1 and the "passes in CI" half of v1 #5. ~20 lines of YAML.
4. **Write `SECURITY-REVIEW.md`.** One-pager: what we checked, what we found, what's deliberately out of scope. Cross-link to `SECURITY.md` for the threat model.
5. **Bump `pyproject.toml` to `>=3.11`.** One-line fix for NFR-4.2.
6. **Amend REQUIREMENTS.md for the two intentional deviations** (FR-3.3 toolset name, FR-4.3 first-run behavior) so the spec matches the code. Otherwise new contributors will read the spec, write code to match it, and clash with the existing tests.
7. (Optional, v0.2) Add a load test for NFR-2.2/2.3 — not blocking, but would actually prove the performance numbers instead of leaving them as "inferred."

Want me to file these as a follow-up batch of cards (one per item, ordered by dependency), or work through them inline here?
