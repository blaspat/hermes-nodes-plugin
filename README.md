# hermes-nodes-plugin

A Hermes Agent plugin that turns any Hermes profile into a "brain" to command remote nodes over an authenticated WebSocket.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Start the server](#start-the-server)
- [Pair a node](#pair-a-node)
- [Tools](#tools)
- [Revoke a node](#revoke-a-node)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

## Prerequisites

- Python 3.11+
- Hermes Agent installed
- A Fernet key for token encryption (generated automatically on first run, or provide your own)

## Installation

### 1. Clone and install

```bash
git clone https://github.com/blaspat/hermes-nodes-plugin.git
cd hermes-nodes-plugin
pip install -e .
# or: uv pip install -e .
```

This installs the `hermes-node` CLI and registers the plugin with Hermes.

### 2. Enable the plugin

```bash
hermes plugins enable hermes-nodes-plugin
```

Restart the gateway for the plugin to load its tools:

```bash
systemctl --user restart hermes-gateway
```

Verify the toolset is available:

```bash
hermes tools list | grep hermes_nodes
# ✓ enabled  hermes_nodes  🔌 Hermes Nodes
```

## Start the server

The WSS server can run as a standalone systemd service — independent of the Hermes gateway. This is the recommended approach.

### Option A: systemd service (recommended)

```bash
# Copy the service unit
cp systemd/hermes-nodes-server.service ~/.config/systemd/user/

# Set your Fernet key in the service
# Find your existing key:
grep HERMES_NODES_TOKEN_KEY ~/.hermes/.env

# Edit the service file and set the actual key value:
# Environment="HERMES_NODES_TOKEN_KEY=<your-key-here>"

# Fix the path (the repo ships with /home/User as a placeholder)
sed -i 's|/home/User|/home/patrick|g' ~/.config/systemd/user/hermes-nodes-server.service

# Reload and start
systemctl --user daemon-reload
systemctl --user enable --now hermes-nodes-server

# Verify
ss -tlnp | grep 6969
curl http://127.0.0.1:6969/nodes/status
```

### Option B: manual

```bash
# Set the Fernet key from ~/.hermes/.env
export HERMES_NODES_TOKEN_KEY=$(grep HERMES_NODES_TOKEN_KEY ~/.hermes/.env | cut -d= -f2)
python scripts/run_server.py
```

The server listens on `127.0.0.1:6969`. External access requires a reverse proxy (e.g. nginx, Cloudflare Tunnel) forwarding `/:6969/ws/nodes` to it.

## Pair a node

### 1. Generate a pairing token

```bash
hermes node pair --name my-devbox
```

This prints a command to run on the remote node:

```
name:  my-devbox

Run this on the laptop:
  hermes-node pair --server <host:port>/ws/nodes --token <token> --name my-devbox
token: <one-time token — copy it now>
```

### 2. Re-pairing

If a node's token was revoked or expired, use `--force` to revoke the old record and issue a fresh one:

```bash
hermes node pair --name my-devbox --force
```

> **Warning:** `--force` immediately revokes the existing token. The old node will be disconnected and unable to reconnect.

### 3. Verify connection

```bash
curl http://127.0.0.1:6969/nodes/status
# {"connected_names":["my-devbox"]}
```

## Tools

Once the plugin is enabled and the server is running, four tools are available to the agent:

| Tool | Description |
|------|-------------|
| `node_exec(target, command)` | Run a shell command on a paired node |
| `node_read(target, path)` | Read a file from a paired node |
| `node_write(target, path, content)` | Write content to a file on a paired node |
| `node_list()` | List all paired nodes and their connection state |

`node_list()` works even when no nodes are connected — it returns an empty list. The other tools return a helpful error if the target node is not connected.

## Revoke a node

```bash
hermes node revoke --name my-devbox
```

The node is immediately disconnected and its token is invalidated.

## Troubleshooting

### `hermes plugins enable` says "not installed"

The plugin was installed in the default profile's plugins directory (`~/.hermes/plugins/`) but the current Hermes profile uses a different directory. Symlink it:

```bash
ln -s ~/.hermes/plugins/hermes-nodes-plugin \
       ~/.hermes/profiles/<current-profile>/plugins/
```

### Port 6969 not listening after server restart

The server process may have been killed. Restart it:

```bash
systemctl --user restart hermes-nodes-server
# or, if not using systemd:
export HERMES_NODES_TOKEN_KEY=$(grep HERMES_NODES_TOKEN_KEY ~/.hermes/.env | cut -d= -f2)
python scripts/run_server.py &
```

### Node shows as disconnected in `hermes node list`

- The token may have been revoked — re-pair with `hermes node pair --name <name> --force`
- The node's `hermes-node` binary may be offline — start it on the node machine
- Check `~/.hermes/logs/nodes-audit.log` for connection errors

### Tools return "node is not connected" even though it appears in `node_list`

The node is paired but the server cannot reach it. The Go binary on the node may have lost its connection — restart `hermes-node run` on that machine.

## Architecture

```
┌─────────────────────────────────────────┐
│   Hermes Agent (Kate / CLI)             │
│   hermes-nodes-plugin tool layer        │
│   node_exec / node_read / node_write    │
└──────────────┬──────────────────────────┘
               │ WSS /ws/nodes (auth token)
               ▼
┌─────────────────────────────────────────┐
│   hermes-nodes WSS Server (port 6969)   │
│   hermes_nodes_plugin.lifecycle          │
└──────────────┬──────────────────────────┘
               │ internal NodeRegistry
               ▼
┌─────────────────────────────────────────┐
│   Paired nodes (hermes-node binary)     │
│   work-mac, devbox, ...                 │
└─────────────────────────────────────────┘
```

The server is stateless per connection — each node holds its own pairing token. Tokens are one-way hashed server-side (Fernet encryption, PBKDF2-HMAC-SHA256).

## Contributing

- Code Style: Follow `CONTRIBUTING.md`.
- Test it: `pytest -v` for unit tests.
- Flow: Fork → Branch → PR.

## Related

- **[hermes-nodes](https://github.com/blaspat/hermes-nodes):** Remote node binary (run on each node machine).
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent):** Core framework.

---

License: MIT
