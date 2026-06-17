# Security

## Threat model

**In scope:**
- A malicious or compromised Hermes brain (VPS) attempting to do things outside what the node's allowlist permits
- A passive network observer attempting to read commands or file contents
- An attacker who has obtained a node's config file (e.g. laptop theft) attempting to use the token
- A compromised node attempting to access files outside the allowlist (defense in depth — both sides enforce)

**Out of scope (v1):**
- A compromised Hermes brain tricking the node into running commands the user would object to *within* the allowlist. (Mitigation: the path allowlist is the trust boundary; if the user trusts Agent to operate inside `/Users/User`, they trust Agent inside `/Users/User`. The plugin does not solve "user trusts Agent" — that's a separate problem.)
- Supply-chain attacks on dependencies. (Mitigation: use pinned dependency versions, audit them, but v1 doesn't ship a full SBOM or signed builds.)
- Physical access to the laptop while the node is running. (Mitigation: full-disk encryption, screen lock — the standard laptop-security story, not this project's job.)
- DoS against the VPS WSS endpoint. (Mitigation: rate limits at 100 calls/sec/node, but a determined attacker can exhaust VPS resources. Out of scope.)

## Authentication

- **Token format:** 32 random bytes, base64url-encoded, generated with `secrets.token_urlsafe(32)`. ~256 bits of entropy.
- **Token comparison:** constant-time on the server (`hmac.compare_digest`).
- **Token storage (server):** Fernet-encrypted (AES-128-CBC + HMAC-SHA256) at `~/.hermes/nodes/tokens.json`. The Fernet key is loaded from `HERMES_NODES_TOKEN_KEY`.
- **Token storage (node):** plaintext in `~/.hermes-nodes/config.toml`, file mode `0600`. **v1 limitation** — see "Future hardening" below.
- **Token lifetime:** unlimited. Revoked only by `hermes node revoke`. v1 has no auto-rotation.

## Transport

- **Required:** TLS 1.3 (or 1.2 minimum). The server refuses to start without a valid cert + key pair.
- **Default port:** 6969. Operator's choice — change it via `HERMES_NODES_PORT` if the corporate firewall blocks this port. 443 is the most universally-allowed alternative.
- **Direction:** node → server only. The node never accepts inbound connections. The laptop needs no firewall changes.
- **MITM protection:** standard CA validation. Self-signed certs require explicit `ca_cert` config on the node (not in default install path).

## Authorization (path allowlist)

**Enforced on the node, not the server.** This is the load-bearing security claim of the whole project: even a fully compromised Hermes brain cannot read or write files outside the laptop's configured `allowed_paths`. The server sends a `read` or `write` request; the node checks the path; the node denies.

**Implementation:**
- `allowed_paths` is a list of root directories. Default: empty list (deny all filesystem ops).
- The check is done on the **canonicalized** path (after `os.path.realpath`), defeating symlink escapes.
- `exec` is unaffected by the allowlist — see the threat model caveat above.

**Default recommendation:** laptop operators should configure `allowed_paths` to be the smallest set of directories Agent needs. `/Users/<you>/code` is typical.

## Audit log

**Both sides log every call.** Format is identical: one JSON object per line.

**Node-side log:** `~/.hermes-nodes/audit.log` (configurable). Default retention: 90 days (configurable).
**Server-side log:** `~/.hermes/logs/nodes-audit.log`. Default retention: 1 year (configurable).

**Fields:**
```json
{
  "ts": "2026-06-04T10:00:00.000Z",
  "node": "work-laptop",
  "action": "exec" | "read" | "write" | "auth" | "revoke",
  "request_id": "uuid-v4",
  "duration_ms": 1234,
  "exit_code": 0,
  "status": "ok" | "error" | "timeout" | "denied",
  "error": "path_not_allowed",
  "command_summary": "pytest tests/ -q"   // exec only, first 200 chars
}
```

**`request_id` is the join key.** Given a `request_id` from either side's log, the other side's log has the matching entry. Useful for forensics.

Audit logs are append-only. There is no CLI command to delete entries. (Operators can of course `rm` the file, but that breaks the append-only invariant visibly.)

## Rate limits

- **Per-node:** 100 calls/sec sliding window. Excess → server closes WSS with code `4004`. The node auto-reconnects with backoff.
- **Per-server:** unlimited (v1). If this becomes an issue, add a global rate limit in v2.

## Resource limits

- **WSS frame size:** 16 MB max.
- **Output cap per `exec`:** 10 MB per stream. Truncated with a marker if exceeded.
- **File size cap per `read`/`write`:** 10 MB. Error `file_too_large` if exceeded.
- **Command timeout:** 60s default, 600s max.

These limits exist to prevent a single misbehaving call from exhausting laptop or VPS resources.

## What the binary does NOT do

This list is important for a security review. The node binary:

- ❌ Does not access the camera, microphone, screen, or location.
- ❌ Does not capture keystrokes or clipboard.
- ❌ Does not modify system settings, install software, or change firewall rules.
- ❌ Does not open any inbound network ports.
- ❌ Does not write to disk outside `~/.hermes-nodes/` (the config dir).
- ❌ Does not read files outside the configured `allowed_paths`.
- ❌ Does not load dynamic code or evaluate external input.
- ❌ Does not phone home (no telemetry, no update checks, no analytics).

The binary's full source is ~1000 lines of Go. A reviewer can read it end-to-end in an afternoon.

## Future hardening (v2 candidates)

- **OS keychain integration for the token** on the node side (macOS Keychain, Windows Credential Manager, Linux Secret Service). Removes the plaintext-on-disk concern.
- **Automatic token rotation** with a grace period.
- **Per-call capability grants** (e.g. a token that can only `exec`, not `read`/`write`).
- **Signed releases** with cosign/Sigstore, so a node can verify it's running the binary the maintainer built.
- **Audit log streaming to a remote SIEM** (currently local-only).
- **Time-bounded tokens** (auto-revoke after a configurable duration).
- **Hardware-bound tokens** (TPM-backed, like FIDO2).

## Reporting a vulnerability

File an issue on the relevant GitHub repo, or contact User directly. Please don't disclose publicly until we've had a chance to fix.
