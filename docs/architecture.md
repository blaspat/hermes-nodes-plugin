# hermes-node Architecture

## Overview

Two independently-versioned repos:

| Repo | Language | Role |
|------|----------|------|
| `hermes-node` | Go | Node client binary (runs on each remote machine) |
| `hermes-node-plugin` | Python | Hermes Agent plugin + WS server (runs on VPS) |

### Two CLIs — two binaries

It is easy to confuse `hermes node` and `hermes-node`. They are completely separate programs:

- **`hermes-node`** — Standalone Go binary installed on each node machine.
  - `hermes-node node start --server wss://<host>:<port>` — connect to WS server
  - `hermes-node pair --server wss://<host>:<port> --token <token> --name <name>` — pair with WS server using a token

- **`hermes node`** — Plugin commands registered with the Hermes Agent CLI (Python). Only available when `hermes-node-plugin` is loaded.
  - `hermes node server start|stop|status` — manage the WS server on the VPS
  - `hermes node list` — show paired and connected nodes
  - `hermes node revoke <name>` — revoke a node's pairing token

---

## System Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  VPS (WS server only — Python plugin code runs in Kate)      │
│                                                             │
│   Port: 7000 (configurable)                                 │
│   Log:  ~/.hermes-node/server.log                          │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐   │
│   │  WS Server                                          │   │
│   │                                                     │   │
│   │  /nodes/{node}/exec   ← HTTP from Hermes tools     │   │
│   │  /nodes/{node}/read   ← HTTP from Hermes tools     │   │
│   │  /nodes/{node}/write  ← HTTP from Hermes tools     │   │
│   │  /nodes               ← HTTP from Hermes tools     │   │
│   │                                                     │   │
│   │  /ws/nodes             ← WS from node clients      │   │
│   └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
           ▲ WS (node clients connect)
           │
    ┌──────┴─────────┐
    │  Node A        │
    │  (hermes-node │
    │   client)      │
    ├────────────────┤
    │  Node B        │
    │  (hermes-node │
    │   client)      │
    └────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Hermes Agent (Kate)                                        │
│                                                             │
│  ~/.hermes/profiles/kate/plugins/hermes-node-plugin/       │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  Tools (node_exec, node_read, node_write, node_list) │   │
│  │                                                      │   │
│  │  HTTP POST/GET  →  http://localhost:7000/nodes/{node}/...  │
│  │  (Authorization: Bearer <token>)                   │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## hermes-node (Go)

Installed on each node machine. Download from the [hermes-node releases page](https://github.com/blaspat/hermes-node/releases) or build from source with `go install`.

### Commands

```bash
hermes-node node start --server wss://<server>:<port>  # Start node client (connects to WS server, default port 7000)
hermes-node pair --server wss://<server>:<port> --token <token> --name <name>  # Pair node client to WS server
```

**Node → WS Server protocol:**

Each node client maintains a persistent WS connection. When Hermes Agent sends an exec/read/write HTTP request, the WS server relays it over the node's live WS connection and waits for the response via a waiter/future pattern (request ID matching).

**Log file:** The Python WS server on the VPS writes to `~/.hermes-node/server.log`. Each node client (Go binary) also writes to `~/.hermes-node/server.log` on its own machine. Both rotate daily, 10MB max.

### Pairing

Before a node can connect, it must be paired with the WS server:

**On the VPS (server side):**
```bash
hermes node pair --name <node-name>
# → generates a random token, prints it, stores it server-side
```

**On the node machine (client side):**
```bash
hermes-node pair --server wss://<host>:<port> --token <token> --name <node-name>
# → presents token to server, token is bound to this node name
```

After pairing, the node is configured with the token in `~/.hermes-node/config.yaml`. Subsequent connections use `node start` instead of `pair`:

```bash
hermes-node node start --server wss://<host>:<port>
# → reads token from ~/.hermes-node/config.yaml, connects
```

**Revocation:** `hermes node revoke <name>` on the VPS deletes the token. The node cannot reconnect — it must `pair` again with a new token.

**Re-pairing:** A node whose token was revoked can run `pair` again with the new token. A node that loses its token (e.g. fresh machine) must pair again.

**Token storage server-side:** Token bindings are stored in memory by the WS server (not persisted to disk).

### Node Client

Runs on each remote machine. Connects to the WS server and registers itself by name.

**Auth:** Uses the same pre-shared token as the WS server (configured in `~/.hermes-node/config.yaml` on the node machine).

**Configuration:** All configuration (Auth, Name, WS Server URL, etc) configured in `~/.hermes-node/config.yaml` on the node machine

**Startup:**
```bash
hermes-node node start --server wss://<server>:<port>  # default port 7000
```

---

## hermes-node-plugin (Python)

Located at: `~/.hermes/profiles/kate/plugins/hermes-node-plugin/`

Installed by copying the plugin directory to the plugins folder (no pip install).

### Installation

1. Copy `hermes-node-plugin/` to `~/.hermes/profiles/kate/plugins/`
2. Ensure `hermes_nodes` config block is present in `~/.hermes/profiles/kate/config.yaml`
3. (Re)start Hermes Agent — the plugin is auto-loaded at startup, and `hermes node ...` commands become available

The `hermes node` commands only appear when the plugin is loaded.

### Tools

| Tool | Description |
|------|-------------|
| `node_exec` | Execute command on a remote node via WS server relay |
| `node_read` | Read file from a remote node |
| `node_write` | Write file to a remote node |
| `node_list` | List all connected nodes |


### WS Server (VPS side)

Runs on the VPS alongside hermes-node-plugin. Started automatically via `on_session_start` plugin hook (auto-start).

**TCP port:** 7000 (default, configurable via `HERMES_NODES_PORT`)

**Token auth:** Nodes authenticate using pairing tokens generated by `hermes node pair --name <name>`. Tokens are stored encrypted in `~/.hermes/nodes/tokens.json`. The WS server validates tokens via the `TokenStore`. The Fernet encryption key is read from the env var named by `token_encryption_key_env` (default `HERMES_NODES_TOKEN_KEY`).

**WS Server config:** Reads token store path and Fernet key env var name from `~/.hermes/hermes-node.yaml` (or env vars). The WS server itself has no separate auth token — tool calls from the Hermes Agent to the WS server use plain HTTP on localhost with no additional auth (the server is not exposed externally).

**HTTP Endpoints** (internal, same host, no additional auth):

```
POST /nodes/{node}/exec
Body:   { "command": "ls -la", "cwd": "/home/patrick", "env": {}, "timeout_ms": 30000 }
Resp:   { "status": "ok"|"error", "output": "...", "exit_code": 0, "duration_ms": 120 }

POST /nodes/{node}/read
Body:   { "path": "/etc/hosts" }
Resp:   { "status": "ok"|"error", "content": "...", "size_bytes": 0, "truncated": false }

POST /nodes/{node}/write
Body:   { "path": "/tmp/test.txt", "content": "hello world", "mode": "overwrite" }
Resp:   { "status": "ok"|"error", "bytes_written": 11 }

GET  /nodes
Resp:   { "nodes": [{ "name": "laptop", "connected_at": "2026-06-20T00:00:00Z",
                       "last_heartbeat": "2026-06-20T00:05:00Z",
                       "session_id": "uuid", "remote_addr": "203.0.113.10",
                       "state": "connected" }], "count": 1 }

GET  /nodes/status
Resp:   { "server": "ok", "port": 7000, "connected_count": 1 }
        (Note: not currently called by any in-repo consumer.
         ``hermes node status`` CLI uses a TCP socket probe instead.
         This endpoint is intended for external monitoring tools.)
```

**WebSocket Endpoint** (for node client connections):

```
WS  /ws/nodes
Header: Authorization: Bearer <token>  (token presented on WS connect)
```


### Hermes Agent Config

The plugin reads from `~/.hermes/hermes-node.yaml` (and env vars, which override file values):

```yaml
# ~/.hermes/hermes-node.yaml
host: "127.0.0.1"          # WS server bind address (default)
# connect_host is resolved automatically at load time: the loader probes
# ``host`` first, then ``localhost``, and stores the reachable address here.
# HTTP clients (tools, CLI) always use ``connect_host`` to avoid connecting
# to an unreachable loopback address. Override via HERMES_NODES_CONNECT_HOST.
port: 7000                 # WS server port (default)
token_encryption_key_env: "HERMES_NODES_TOKEN_KEY"  # env var holding Fernet key
# Both TLS paths must be set together to enable direct TLS mode:
# tls_cert_path: "/path/to/cert.pem"
# tls_key_path: "/path/to/key.pem"
```

The token encryption key is auto-generated by `hermes node pair --name <name>` if not yet set. Tools (node_exec, node_read, node_write, node_list) authenticate to the WS server over plain HTTP on localhost — no separate token needed at the tool layer.

### CLI Commands

```
hermes node pair --name <node-name>   # Generate a pairing token (run on VPS first)
hermes node list                      # List paired + connected nodes
hermes node revoke --name <name>      # Revoke a node's pairing
hermes node status                    # Show whether WS server is listening
```

> **Note:** The WS server is auto-started when the Hermes Agent gateway starts
> (via `on_session_start` hook). Use `hermes node status` to check if it's
> running. `hermes node server start/stop` commands are not yet implemented.

---

## Request/Response Specs

All requests travel: Hermes tool → HTTP POST/GET → WS server (VPS) → relayed over the node's live WS connection → node response returned via the same path.

### node_exec

**Request body (from Hermes tool to WS server):**
```json
{
  "command": "ls -la /home/patrick",
  "cwd": "/home/patrick",
  "env": { "DEBUG": "1" },
  "timeout_ms": 30000
}
```

**Response:**
```json
{
  "status": "ok",
  "output": "total 128\ndrwxrwxr-xr-x  5 patrick patrick  4096 Jun 20 00:00 .\ndwxrwxr-xr-x  9 patrick patrick  4096 Jun 20 00:00 ..\n-rw-rw-r--  1 patrick patrick  4096 Jun 19 23:57 .bashrc\n",
  "exit_code": 0,
  "duration_ms": 45
}
```

Error:
```json
{
  "status": "error",
  "code": 404,
  "reason": "node 'winpc' is not connected"
}
```

### node_read

**Request:**
```json
{
  "path": "/var/log/syslog",
  "offset": 0,
  "limit": 1000
}
```

**Response:**
```json
{
  "status": "ok",
  "content": "...(first 1000 lines)...\n",
  "total_lines": 5420
}
```

### node_write

**Request:**
```json
{
  "path": "/tmp/test.txt",
  "content": "hello world\n",
  "append": false
}
```

**Response:**
```json
{
  "status": "ok",
  "bytes_written": 12
}
```

### node_list

**Response:**
```json
{
  "nodes": [
    {
      "name": "laptop",
      "connected_at": "2026-06-20T00:00:00Z",
      "last_heartbeat": "2026-06-20T00:05:00Z",
      "session_id": "a1b2c3d4-...",
      "remote_addr": "203.0.113.10",
      "state": "connected"
    }
  ],
  "count": 1
}
```

---

## Security

- **Node → WS Server:** Token via WS `Authorization` header on connect.
- **Hermes Agent → WS Server:** Plain HTTP on localhost; no additional auth layer needed.
- **Token storage:** Fernet-encrypted tokens in `~/.hermes/nodes/tokens.json`, generated by `hermes node pair`.
- **Network:** Tools connect to the WS server over the internet (HTTPS recommended for production, WSS for WebSocket transport).

---

## File Structure

### hermes-node (Go) — Client side only

```
hermes-node/
├── cmd/
│   └── node/
│       └── main.go          # hermes-node node start
├── internal/
│   ├── wsclient/            # Node WS client implementation
│   └── protocol/            # Node ↔ WS server protocol
├── config/
│   └── config.go
├── go.mod
└── README.md
```

### hermes-node-plugin (Python) — Server side (VPS)

```
hermes-node-plugin/
├── __init__.py              # register(ctx) — exposes tools
├── tools.py                 # node_exec, node_read, node_write, node_list
├── schemas.py               # Tool schemas
├── cli.py                   # hermes-node server|node CLI commands
├── config.py                # WS server URL / token config
├── wsserver/                # WS server implementation
│   ├── __init__.py
│   ├── server.py            # WS server runner
│   └── handlers.py          # HTTP API + WS relay handlers
├── plugin.yaml
└── README.md
```

---

## Launch Sequence

1. **Hermes Agent startup:** `hermes-node-plugin` is loaded via `register(ctx)` → `on_session_start` hook auto-starts the WS server on port 7000
2. **VPS:** WS server listens on `127.0.0.1:7000`; logs to `~/.hermes-node/server.log`
3. **Node machines:** `hermes-node node start --server wss://<server>:<port>` → each node connects to WS server via WSS
4. **Hermes Agent:** Already has `hermes-node-plugin` in its plugins folder → tools `node_exec`, `node_read`, `node_write`, `node_list` are registered at startup
5. **Usage:** Hermes Agent calls tools → HTTP to WS server → relayed over WS to node → response back
