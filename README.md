# hermes-nodes-plugin

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that turns any Hermes profile into a "brain" that can command remote nodes (laptops, NAS, headless boxes) over an authenticated WebSocket. Pairs with the [`hermes-nodes`](https://github.com/blaspat/hermes-nodes) Go binary.

> **Status:** pre-v0.1.0. Implementation in progress; see [`REQUIREMENTS.md`](./REQUIREMENTS.md) for the spec.

## What it does

Once installed, Kate (or any Hermes agent in any profile) gains four new tools:

- `node_exec(target, command)` — run a shell command on a paired node
- `node_read(target, path)` — read a file on a paired node
- `node_write(target, path, content)` — write a file on a paired node
- `node_list()` — show all known nodes and their connection state

This lets Kate do things like:
- Run tests on a laptop that don't fit in a container on the VPS
- Edit a project that lives on the user's machine
- Read a file from a NAS without going through SSH
- Pair as a brain-and-arm with machines that have no inbound network access

## Install

From GitHub (recommended during v0.x):

```bash
# Activate the Hermes profile's venv first
source ~/.hermes/profiles/kate/venv/bin/activate

# Install the plugin (editable mode for development)
pip install -e git+https://github.com/blaspat/hermes-nodes-plugin.git#egg=hermes-nodes-plugin

# OR from a local clone
git clone https://github.com/blaspat/hermes-nodes-plugin.git
cd hermes-nodes-plugin
pip install -e .
```

Once installed, the plugin auto-loads via Hermes's `hermes_agent.plugins` entry-point group — **no config file changes needed**. Verify with:

```bash
hermes --help | grep "node "
# should show: node        Pair, list, and revoke remote node connections.
```

## Configuration

The plugin reads from `~/.hermes/hermes-nodes.yaml` (created automatically on first run) and env vars. Env vars override file values.

```yaml
# ~/.hermes/hermes-nodes.yaml
host: 0.0.0.0
port: 6969
tls_cert_path: ~/.hermes/nodes/server.crt
tls_key_path: ~/.hermes/nodes/server.key
token_store_path: ~/.hermes/nodes/tokens.json
audit_log_path: ~/.hermes/logs/nodes-audit.log
audit_retention_days: 365
```

| Env var | Default | Purpose |
|---|---|---|
| `HERMES_NODES_HOST` | `0.0.0.0` | WSS bind address |
| `HERMES_NODES_PORT` | `6969` | WSS bind port |
| `HERMES_NODES_TLS_CERT` | `~/.hermes/nodes/server.crt` | TLS cert path |
| `HERMES_NODES_TLS_KEY` | `~/.hermes/nodes/server.key` | TLS key path |
| `HERMES_NODES_TOKEN_KEY` | *(required)* | Fernet key for encrypting tokens at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `HERMES_NODES_AUDIT_RETENTION_DAYS` | `365` | How long to keep audit log entries |

**`HERMES_NODES_TOKEN_KEY` is required.** If unset, `hermes node pair` will guide you through generating one.

## Usage

```bash
# Pair a new node (laptop, NAS, etc.)
hermes node pair --name work-laptop
# Output:
#   Pairing token for "work-laptop":
#   aBcD1234eFgH5678...
#
#   Run on the laptop:
#     hermes-node pair \
#       --server wss://vps.yourdomain.com:6969 \
#       --token aBcD1234eFgH5678...

# List paired nodes + connection state
hermes node list
# Output:
#   work-laptop    connected    2026-06-04 10:00:00
#   home-nas       disconnected never seen

# Revoke a node
hermes node revoke --name work-laptop
# Output:
#   Revoked node "work-laptop". Active connection (if any) has been dropped.
```

From inside a Kate (or any Hermes) session:

```
> node_list()
["work-laptop (connected)", "home-nas (disconnected)"]

> node_exec("work-laptop", "cd ~/code/myapp && pytest -q")
"====== test session starts ======
 ...
 5 passed in 0.42s"

> node_read("work-laptop", "~/code/myapp/src/x.py")
"...file contents..."

> node_write("work-laptop", "~/code/myapp/src/x.py", new_content, mode="overwrite")
{"bytes_written": 1234, "status": "ok"}
```

## Architecture

```
┌──────────────────────────────────────┐         ┌──────────────────────────────────────┐
│ VPS (any Hermes profile)             │         │ Laptop (or any node device)          │
│                                      │         │                                      │
│  hermes-agent (Python)               │         │  hermes-node (Go binary)             │
│  ┌─────────────────────────────┐     │         │  ┌─────────────────────────────┐     │
│  │ hermes-nodes-plugin         │     │  WSS    │  │  - shell executor           │     │
│  │  - NodeServer (FastAPI)     │◄────┼────┐    ├────┤  - filesystem ops           │     │
│  │  - NodeRegistry             │     │    │    │    │  - audit log                │     │
│  │  - NodeEnvironment (Kate)   │     │    │    │    │  - path allowlist           │     │
│  │  - tokens (Fernet)          │     │    │    │    │                             │     │
│  └─────────────────────────────┘     │    │    │    └─────────────────────────────┘     │
│                                      │    │    │                                      │
└──────────────────────────────────────┘    │    └──────────────────────────────────────┘
                                            │
                              WSS over TLS 6969
```

**The protocol contract between them lives in [`hermes-nodes/PROTOCOL.md`](https://github.com/blaspat/hermes-nodes/blob/main/PROTOCOL.md).** Both sides pin to it for tests.

## Development

```bash
git clone https://github.com/blaspat/hermes-nodes-plugin.git
cd hermes-nodes-plugin
python3.11 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Unit tests
pytest tests/ -v

# E2E (requires the Go binary from hermes-nodes built and on PATH)
pytest tests/e2e/ -v -m e2e
```

## Related

- **[hermes-nodes](https://github.com/blaspat/hermes-nodes)** — the Go node binary (the "arm")
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — the agent framework this plugs into
- **[OpenClaw](https://docs.openclaw.ai/nodes)** — the design pattern this is inspired by (different protocol, different ecosystem)

## License

MIT
