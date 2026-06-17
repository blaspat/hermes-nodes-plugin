# hermes-nodes-plugin
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
Install Python 3.11+, uv (optional), and have a Hermes Agent set up.

## Installation
Install the plugin:

 ```bash
 git clone https://github.com/blaspat/hermes-nodes-plugin \
  ~/.hermes/plugins/hermes-nodes-plugin
 ```

## Core Features
- `node_exec(target, command)`: run shell commands on a paired node.
- `node_read(target, path)`: read a file on a paired node.
- `node_write(target, path, content, mode="overwrite")`: write a file on a paired node.
- `node_list()`: list paired nodes and their connection state.

## Usage

### 1. Configure
Edit `~/.hermes/hermes-nodes.yaml` with host/port/TLS settings.

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
hermes-node pair --server ws://<server>:6969 --token <token>
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

### 6. Revoke

```bash
hermes node revoke --name my-devbox
```

## Contributing
- Code Style: Follow `CONTRIBUTING.md`.
- Lint it: `ruff check .`
- Flow: Fork → Branch → PR.

## FAQ
- **Q:** Does it support Windows nodes?  
  A: Not officially; only Linux/macOS (WSL works).
- **Q:** How is audit handled?  
  A: All interactions are logged to `~/.hermes/logs/nodes-audit.log` and retained per `audit_retention_days`.

## Related
- **[hermes‑nodes](`github.com/blaspat/hermes-nodes`):** Remote node binary.
- **[Hermes Agent](`github.com/NousResearch/hermes-agent`):** Core framework.

---
License: [MIT](LICENSE) | Author: © 2026 Blasius User
