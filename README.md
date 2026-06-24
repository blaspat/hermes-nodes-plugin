# Hermes Node Plugin
A Hermes Agent plugin that turns any Hermes profile into a “brain” to command remote nodes over an authenticated WebSocket.

## Table of Contents
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Core Features](#core-features)
- [Usage](#usage)
- [Contributing](#contributing)
- [FAQ](#faq)
- [Related](#related)

## Prerequisites
Install Python 3.11+ and have a Hermes Agent set up.

## Installation
Install the plugin:

 ```bash
 git clone https://github.com/blaspat/hermes-node-plugin \
  ~/.hermes/plugins/hermes-node-plugin
 ```
Then, update your `config.yaml`. Add `hermes-node-plugin`
```yaml
plugins:
  enabled:
  - hermes-node-plugin
```

## Core Features
- `node_exec(target, command)`: run shell commands on a paired node (auto-retries on disconnect).
- `node_read(target, path)`: read a file on a paired node (auto-retries on disconnect).
- `node_write(target, path, content, mode="overwrite")`: write a file on a paired node (auto-retries on disconnect).
- `node_list()`: list paired nodes and their connection state.

## Usage

### 1. Configure
Edit `~/.hermes/hermes-node.yaml` with host/port/TLS settings.

Optional retry settings (defaults shown):

```yaml
max_retries: 3
retry_backoff_seconds: 2.0
```

Or via environment variables:
- `HERMES_NODES_MAX_RETRIES` — number of retries on transient failures (default: `3`, set `0` to disable)
- `HERMES_NODES_RETRY_BACKOFF_SECONDS` — initial backoff in seconds, doubles each attempt (default: `2.0`)

The tools automatically retry on connection errors, server errors (5xx), and node-disconnected responses with exponential backoff (`backoff × 2^attempt`, capped at 30s).

### 2. Start the server

The WSS server starts automatically when Hermes starts a session, via the
plugin's `on_session_start` hook. No manual steps required — Hermes wires it
up when the plugin is loaded.

For manual / dev mode (server only, no LLM):

```bash
python scripts/run_server.py
```

> **Note:** The gateway plugin hook (`on_session_start`) does not fire reliably
> when the plugin is enabled while the gateway is already running. For a
> persistent server that survives gateway restarts, run `scripts/run_server.py`
> in a terminal or supervisor.

### 3. Pair a node

```bash
hermes node pair --name my-devbox
# prints a one-time token

# On the remote node, run:
hermes-node pair --server ws://<server>:7000 --token <token>
```

### 4. Run a command

Once a node is paired, instruct the LLM to use the `node_exec` tool:

```
node_exec(target="my-devbox", command="echo hello")
```

The LLM will route the command over WSS to the node and return the output.

### 5. Transfer files

The `node_read` and `node_write` tools let the LLM read and write files on a paired node:

```
node_read(target="my-devbox", path="~/project/README.md")
node_write(target="my-devbox", path="~/project/new.txt", content="sample", mode="create")
```

These are LLM tools — not CLI commands. The LLM handles the routing over WSS automatically.

### 6. List nodes

Check which nodes are connected and their current state:

```
node_list()
```

Returns a JSON object with `nodes` (array of name, connected_at, last_heartbeat, session_id, remote_addr, state) and `count`. The LLM uses this to discover available targets.

### 7. Revoke

```bash
hermes node revoke --name my-devbox
```

## Contributing
- Code Style: Follow [CONTRIBUTING](CONTRIBUTING.md).
- Lint it: `ruff check .`
- Flow: Fork → Branch → PR.

## FAQ
- **Q:** Does it support Windows nodes?  
  A: Not officially; only Linux/macOS (WSL works).
- **Q:** How is audit handled?  
  A: All interactions are logged to `~/.hermes/logs/nodes-audit.log` and retained per `audit_retention_days`.

## Related
- **[Hermes Node (client)](https://github.com/blaspat/hermes-node):** Remote node binary.
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent):** Core framework.

---
License: [MIT](LICENSE) | Author: © 2026 Blasius Patrick
