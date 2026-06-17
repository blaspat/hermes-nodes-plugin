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

### 2. Start the server (recommended: systemd service)

The WSS server runs best as a standalone systemd service — independent of the
Hermes gateway process. This ensures it survives gateway restarts and boots
automatically.

```bash
# Copy the service unit
cp ~/.hermes/plugins/hermes-nodes-plugin/systemd/hermes-nodes-server.service \
   ~/.config/systemd/user/

# Set your Fernet key — find it with:
#   grep HERMES_NODES_TOKEN_KEY ~/.bashrc ~/.profile 2>/dev/null
# Edit the service file and replace <your-fernet-key-here> with the actual value

# Reload systemd and enable the service
systemctl --user daemon-reload
systemctl --user enable --now hermes-nodes-server

# Verify
ss -tlnp | grep 6969
curl http://127.0.0.1:6969/nodes/status
```

> **Note:** The gateway plugin hook (`on_session_start`) does not fire reliably
> when the plugin is enabled while the gateway is already running. The systemd
> service avoids this issue entirely.

### 3. Pair a node

```bash
hermes node pair --name my-devbox
# prints a one-time token

# On the remote node, run:
hermes-node pair --server ws://<server>:6969 --token <token>
```

### 4. Run a command

```bash
hermes node exec my-devbox "echo hello"
```

### 5. Transfer files

```bash
node_read("my-devbox", "~/project/README.md")
node_write("my-devbox", "~/project/new.txt", "sample", mode="create")
```

### 6. Revoke

```bash
hermes node revoke --name my-devbox
```

## Contributing
- Code Style: Follow `CONTRIBUTING.md`.
- Test it: `pytest -v` for unit tests, `pytest -v -m e2e` for end‑to‑end.
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
