# Hermes Nodes Protocol — Python (plugin) side

The wire protocol spoken between a Hermes Agent brain (Python, on the
server/VPS) and a Hermes Node (Go, on the laptop/remote machine) is
**defined canonically in
[`hermes-nodes/PROTOCOL.md`](https://github.com/blaspat/hermes-nodes/blob/main/PROTOCOL.md)**.
Both implementations must conform to that contract.

This document exists for two reasons:

1. **Point of entry** — anyone working on the Python plugin needs a quick
   pointer to the protocol spec without grepping the cross-repo README.
2. **Python-specific extensions** — anything the Python server sends
   *beyond* the canonical spec (server-originated envelopes, side
   channels, or fields the Go side does not need to understand) is
   documented here, so the Go client maintainer has a single page to
   watch when bumping their frame-type parser.

If this document and the Go-side `PROTOCOL.md` ever disagree, the Go-side
doc wins. File a PR against this file to bring it back in sync.

---

## 1. Canonical protocol

See
[`hermes-nodes/PROTOCOL.md`](https://github.com/blaspat/hermes-nodes/blob/main/PROTOCOL.md).
That doc covers:

- Connection lifecycle (hello / hello_ack / auth / exec / bye)
- Message envelope (`type` / `id` / `ts`)
- All message types (`hello`, `auth`, `exec`, `read`, `write`, `ping`,
  `pong`, `error`, `bye`, and the `*_result` echoes)
- WebSocket close codes (`1000`, `4001`–`4006`)
- Heartbeat cadence and reconnect policy
- Security and size limits

The Python server in `hermes_nodes_plugin/server.py` implements that
contract. The parser at `server.py:664-666` deliberately ignores
unknown message types (it's symmetric with how the Go client should
treat Python-originated frames it does not yet know about).

---

## 2. Python-specific extensions

This section lists server-originated envelopes the Python plugin sends
that are **not** part of the canonical Go-side spec, plus any
Python-side fields added to envelopes that the canonical spec does not
require. The Go client should treat these as informational; it must not
panic on receipt.

### 2.1 `rate_limit` (server → node)

**Added in:** PR #37 (FR-2.6 — per-node sliding-window rate limit).
**Status:** server-originated, advisory. Go client must accept and may
ignore.

**Purpose:** sent immediately before the server closes the WSS with
close code `4004` (rate limit exceeded), so the node operator sees a
structured reason for the drop instead of an opaque socket close.

**Envelope shape:**

```json
{
  "type": "rate_limit",
  "reason": "rate_limit_exceeded",
  "code": 4004,
  "node_name": "<auth.node_name>",
  "limit_per_second": 100
}
```

| Field             | Type        | Required | Meaning |
|-------------------|-------------|----------|---------|
| `type`            | string      | yes      | Discriminator. Always the literal `"rate_limit"`. |
| `reason`          | string      | yes      | Machine-readable reason. Currently always `"rate_limit_exceeded"`. |
| `code`            | integer     | yes      | The WebSocket close code that will follow. Currently always `4004`. |
| `node_name`       | string      | yes      | The authenticated `node_name` whose calls tripped the limiter. Mirrors the value from the node's `auth` envelope. |
| `limit_per_second`| integer     | yes      | The effective cap applied to this node (`config.rate_limit_per_node`, default 100). Lets the node log "we hit the 100 rps cap" without round-tripping. |

**Timing:** the server sends this envelope, then closes the WSS with
code `4004` in the same task. There is no application-level
acknowledgement — the connection is gone.

**Go-side handling:** the frame-type parser should accept `type:
"rate_limit"` as a known envelope (or, at minimum, not reject unknown
types outright — see the symmetric pattern at `server.py:664-666`).
The Go client may log `node_name` + `limit_per_second` for operator
debugging and should not attempt to reply.

**Construction:** `hermes_nodes_plugin/server.py:255-258` (function
`_build_rate_limit_err`). The `CLOSE_RATE_LIMIT_EXCEEDED` constant
(`server.py:83`) is the single source of truth for the `4004` value;
if the close code ever changes, update the constant and this table
together.

---

## 3. Versioning and bump policy

- The canonical spec is the source of truth. This document follows it
  with a lag — when the Go-side `PROTOCOL.md` is updated, this file
  should be reviewed in the same release cycle.
- A new "Python-specific extension" section in §2 must include:
  - PR / issue that introduced the envelope
  - Envelope shape (JSON example + field table)
  - When the server sends it
  - What the Go client is expected to do (accept, ignore, ack, etc.)
- Removing an extension requires a coordinated deprecation cycle in
  the Go-side spec first; do not delete from here in isolation.
