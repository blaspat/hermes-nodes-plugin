# hermes-nodes-plugin
> A Hermes Agent plugin that turns any Hermes profile into a "brain" that can command remote nodes — laptops, NAS, headless boxes — over an authenticated WebSocket. Pairs with the [`hermes-nodes`](https://github.com/blaspat/hermes-nodes) Go binary.

## Quick Start
- **Install:** `python -m pip install hermes-nodes-plugin==0.1.0` (or `uv pip install hermes-nodes-plugin==0.1.0`)
- **Init:** Activate the Hermes venv:
  ```bash
  source ~/.hermes/hermes-agent/venv/bin/activate  # default profile
  # or for a named profile:
  # source ~/.hermes/profiles/<name>/venv/bin/activate
  ```
- **Verify installation:** `hermes node --help` should display the node subcommands.
- **Pair a node:**
  ```bash
  hermes node pair --name work-laptop
  # Server prints a one‑time token
  # Run on the laptop:
  hermes-node pair \
    --server wss://yourdomain.com:6969 \
    --token <token>
  ```
- **Check connectivity:** `hermes node list` should list the new node as *connected*.
- **Example command execution:**
  ```bash
  hermes node exec work-laptop "cd ~/code && pytest -q"
  ```

## Installation
### From a released package (recommended)
1. **Activate the Hermes venv** (see Quick Start).
2. Install the pinned version:
   ```bash
   python -m pip install hermes-nodes-plugin==0.1.0
   # or, using uv for speed:
   uv pip install hermes-nodes-plugin==0.1.0
   ```
   This pulls the pre‑built wheel from PyPI, guaranteeing compatible dependencies.

### From source (for development or pre‑release)
1. **Clone the repo** anywhere you like:
   ```bash
   git clone https://github.com/blaspat/hermes-nodes-plugin.git
   cd hermes-nodes-plugin
   ```
2. **Activate the Hermes venv** (default profile or the one you use for Hermes).
3. **Install in editable mode** so changes reflect instantly:
   ```bash
   python -m pip install -e .
   # or, with uv:
   uv pip install -e .
   ```
4. Verify the CLI tools appear:
   ```bash
   hermes node --help
   ```

## Configuration
The plugin reads its settings from `~/.hermes/hermes-nodes.yaml`. Environment variables prefixed with `HERMES_NODES_` override file values (e.g. `HERMES_NODES_PORT`). Minimal example:
```yaml
host: 127.0.0.1          # bind address (use 0.0.0.0 only behind a trusted proxy)
port: 6969               # WebSocket port
tls_cert_path: null      # path to PEM cert – null to rely on external TLS termination
tls_key_path: null       # path to PEM key – null if not terminating TLS here
token_store_path: ~/.hermes/nodes/tokens.json
audit_log_path: ~/.hermes/logs/nodes-audit.log
audit_retention_days: 365
handshake_timeout_seconds: 10
```
Key options:
- **host / port** – where the plugin listens.
- **tls_cert_path & tls_key_path** – enable the plugin to terminate TLS itself; leave null if you terminate TLS in nginx.
- **token_store_path** – encrypted token store (Fernet). The first `hermes node pair` will generate a key and store it here.
- **audit_log_path** – JSONL audit log for every node interaction.
- **handshake_timeout_seconds** – max seconds to await the node’s hello/auth frames.

## Core Features
- `node_exec(target, command)` — run shell commands on a paired node
- `node_read(target, path)` — read a file on a paired node
- `node_write(target, path, content, mode="overwrite")` — write a file on a paired node
- `node_list()` — list paired nodes and their connection state
- CLI subcommands: `hermes node pair`, `hermes node list`, `hermes node revoke`, `hermes node status`

## Usage
The following steps walk through a typical workflow from start to finish.

1. **TLS Configuration** – You can expose the node endpoint behind a reverse proxy. The quick‑start snippet shows an nginx setup. If you prefer the plugin to handle TLS directly, set `tls_cert_path` and `tls_key_path` in `~/.hermes/hermes-nodes.yaml`.

2. **Start the Plugin** – The `hermes` binary loads the plugin on startup. Verify it’s running by checking the dashboard or `hermes node --help`.

3. **Pair a New Node**
   - Run `hermes node pair --name my‑devbox`. The server prints a one‑time token.
   - On the target machine, execute `hermes-node pair --server wss://<server>:6969 --token <token>`.
   - The node connects, stores its token, and the server logs the event.

4. **Verify the Connection** – `hermes node list` now lists `my‑devbox` as *connected* with timestamps.

5. **Run Commands Remotely** – From an agent session or via CLI:
   ```bash
   node_exec("my‑devbox", "echo hello world")
   
   = "hello world"
   ```
   Or using the external subcommand:
   ```bash
   hermes node exec my‑devbox "cd ~/project && make test"
   ```

6. **Transfer Files**
   - Read: `node_read("my‑devbox", "~/project/README.md")` returns the file content.
   - Write: `node_write("my‑devbox", "~/project/new.txt", "sample", mode="create")` writes a new file, returning `bytes_written`.

7. **Disconnect** – If you need to revoke or remove the node, run `hermes node revoke --name my‑devbox`. The server drops the live connection and deletes the token. Future pairing requires a fresh token.

8. **Audit and Retention** – Every interaction is logged in `~/.hermes/logs/nodes-audit.log`. The log rotates daily by default, and old entries are purged after `audit_retention_days`.

9. **Health Check** – `hermes node status` reports whether the WebSocket server is listening and its uptime.

All of the above can be scripted or run from the agent tooling by importing the four core tools. See the `Core Features` section for exact function signatures.

## Contributing
- Code Style: Follow `CONTRIBUTING.md`.
- Test it: `pytest tests/ -v` for unit tests, `pytest tests/e2e/ -v -m e2e` for end‑to‑end.
- Workflow: Fork ➜ Branch ➜ PR. See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Roadmap / FAQ
- [ ] Stabilize TLS handling across environments.
- [ ] Add auto‑revoke stale connections.
- Q: Does it support Windows nodes? A: Not officially; only Linux/macOS via WSL or similar.

## Related
- **hermes‑nodes:** Remote node binary (`github.com/blaspat/hermes-nodes`).
- **Hermes Agent:** Core agent framework (`github.com/NousResearch/hermes-agent`).
- **Documentation:** Full plugin docs (`~/.hermes/hermes-nodes-plugin/README.md`).

---
License: MIT | Author: © 2026 Blasius Patrick