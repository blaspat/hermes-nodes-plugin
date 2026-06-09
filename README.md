# hermes-nodes-plugin

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that turns any Hermes profile into a "brain" that can command remote nodes — laptops, NAS, headless boxes — over an authenticated WebSocket. Pairs with the [`hermes-nodes`](https://github.com/blaspat/hermes-nodes) Go binary.

> **Status:** v0.1.0-alpha. The WSS server, token store, audit log, and CLI surface are in place and tested; the protocol contract is stable but the on-disk format may shift before v0.2.

## What it does

Once installed, the plugin gives any Hermes agent four new tools and a CLI subcommand:

**Tools (used from inside an agent session):**

- `node_exec(target, command)` — run a shell command on a paired node
- `node_read(target, path)` — read a file on a paired node
- `node_write(target, path, content)` — write a file on a paired node
- `node_list()` — list paired nodes and their connection state

**CLI subcommands (`hermes node …`):**

- `hermes node pair --name <name>` — generate a one-time pairing token
- `hermes node list` — show every paired node and whether it's connected
- `hermes node revoke --name <name>` — invalidate a token and drop the live connection
- `hermes node status` — show whether the WSS server is listening

The agent then becomes a single point of control over a fleet of headless machines: running tests on a laptop that doesn't fit in a container, editing a project on a workstation over the WAN, reading a config off a NAS, or pairing with machines that have no inbound network access.

## Install

The plugin auto-loads via Hermes's `hermes_agent.plugins` entry-point group — no config file changes needed once the package is installed.

From GitHub (recommended during v0.x):

```bash
# Activate the Hermes profile's venv first
source ~/.hermes/profiles/<name>/venv/bin/activate

# Install the plugin (editable mode for development)
pip install -e git+https://github.com/blaspat/hermes-nodes-plugin.git#egg=hermes-nodes-plugin
```

Or from a local clone:

```bash
git clone https://github.com/blaspat/hermes-nodes-plugin.git
cd hermes-nodes-plugin
pip install -e .
```

Verify the install landed:

```bash
hermes --help | grep "node "
# should show: node        Pair, list, and revoke remote node connections.
```

## Configuration

The plugin reads its config from a YAML file at `~/.hermes/hermes-nodes.yaml`, with environment variables (prefixed `HERMES_NODES_`) overriding file values. Built-in defaults apply when neither is set.

Minimal `~/.hermes/hermes-nodes.yaml`:

```yaml
host: 127.0.0.1
port: 6969
token_store_path: ~/.hermes/nodes/tokens.json
audit_log_path: ~/.hermes/logs/nodes-audit.log
audit_retention_days: 365
handshake_timeout_seconds: 10
```

**All keys:**

- `host` — WSS bind address. Default `127.0.0.1` (use `0.0.0.0` only if you're not putting a reverse proxy in front).
- `port` — WSS bind port. Default `6969`.
- `tls_cert_path` / `tls_key_path` — PEM paths. Both must be set together, or both `null`.
- `token_store_path` — encrypted token store. Default `~/.hermes/nodes/tokens.json`.
- `token_encryption_key_env` — name of the env var holding the Fernet key (not the key itself). Default `HERMES_NODES_TOKEN_KEY`.
- `audit_log_path` — append-only JSONL audit log. Default `~/.hermes/logs/nodes-audit.log`.
- `audit_retention_days` — how long rotated audit files are kept. Default `365`.
- `handshake_timeout_seconds` — max time the server waits for the `hello` and `auth` frames during handshake. Default `10`.

**Environment variable override** (all of the above are also accepted as `HERMES_NODES_<KEY>` in upper-snake form, e.g. `HERMES_NODES_PORT`, `HERMES_NODES_HANDSHAKE_TIMEOUT_SECONDS`).

**`HERMES_NODES_TOKEN_KEY` is required.** Generate one with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add it to `~/.hermes/profiles/<name>/.env` (or wherever the profile loads its env) so it's set on every session start. If it's missing at first pair, `hermes node pair` will refuse to run with a clear error.

## TLS configuration

The plugin does **not** need its own TLS cert in the common case. Three options:

**Option A (recommended) — terminate TLS in nginx, run plugin on localhost.** Nginx fronts the public WSS endpoint and proxies to the plugin on `127.0.0.1:6969` over plain HTTP. This is how almost every production deployment looks.

```nginx
# /etc/nginx/sites-enabled/hermes.yourdomain.com
upstream hermes_nodes {
    server 127.0.0.1:6969;
}

server {
    listen 443 ssl;
    server_name hermes.yourdomain.com;
    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    location /ws/nodes {
        proxy_pass http://hermes_nodes;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 3600s;  # WSS connections are long-lived
    }
}
```

In this setup, the plugin binds to `127.0.0.1:6969` and `tls_cert_path` / `tls_key_path` are not used.

**Option B — plugin terminates TLS directly.** If you're not running a reverse proxy, point the plugin at your cert and key:

```yaml
host: 0.0.0.0
port: 6969
tls_cert_path: /etc/letsencrypt/live/yourdomain.com/fullchain.pem
tls_key_path: /etc/letsencrypt/live/yourdomain.com/privkey.pem
```

You'll need to open port 6969 in your VPS firewall. Certbot with `--nginx` or `--standalone` both work; restart the plugin after each renewal (no hot-reload in v0.1).

**Option C — development / self-signed.** For local testing without a real domain. Generate a cert, point the plugin at it, pin the CA on the node side. **Not recommended for production.**

## Usage

Pair a new node and run the install on the device:

```bash
# On the server (the Hermes profile's machine):
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
#   work-laptop    connected    2026-06-08 10:00:00
#   home-nas       disconnected never seen

# Revoke a node (drops the live connection if any)
hermes node revoke --name work-laptop
# Output:
#   Revoked node "work-laptop". Active connection (if any) has been dropped.

# Is the WSS server running?
hermes node status
# Output:
#   WSS server: listening on 127.0.0.1:6969
#   TLS: terminated upstream (nginx)
#   Paired nodes: 2 (1 connected, 1 disconnected)
```

From inside an agent session, the four tools work the same way:

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
│  │  - NodeEnvironment          │     │    │    │    │  - path allowlist           │     │
│  │  - tokens (Fernet)          │     │    │    │    │                             │     │
│  └─────────────────────────────┘     │    │    │    └─────────────────────────────┘     │
│                                      │    │    │                                      │
└──────────────────────────────────────┘    │    └──────────────────────────────────────┘
                                            │
                              WSS over TLS 6969
```

The plugin side is a FastAPI app exposing a WSS endpoint plus four agent tools. The Go binary on the node side speaks the protocol in [`hermes-nodes/PROTOCOL.md`](https://github.com/blaspat/hermes-nodes/blob/main/PROTOCOL.md); both sides pin to that contract for tests.

**Plugin modules:**

- `server.py` — FastAPI + uvicorn WSS server, handshake validation, hello/auth/error frame handling
- `registry.py` — in-memory registry of live `NodeConnection`s with heartbeat bookkeeping and a stale-connection sweep
- `lifecycle.py` — session-start/session-end hooks, runner state, `reset()` for clean shutdown
- `environment.py` — `NodeEnvironment`, the per-target async wrapper the tools call into
- `tools.py` — the four agent tools (`node_exec`, `node_read`, `node_write`, `node_list`)
- `tokens.py` — Fernet-encrypted token store with atomic writes (fsync temp + parent dir)
- `audit.py` — append-only JSONL audit log with rotation and retention sweep; never-raises on write failure
- `cli.py` — argparse for the `hermes node …` subcommands
- `config.py` — YAML + env-var loader with precedence rules

## Development

```bash
git clone https://github.com/blaspat/hermes-nodes-plugin.git
cd hermes-nodes-plugin
python3.11 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Unit tests
pytest tests/ -v

# Lint (CI runs the same)
ruff check src/ tests/

# E2E (requires the Go binary from hermes-nodes built and on PATH)
pytest tests/e2e/ -v -m e2e
```

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the PR workflow, branch naming, and commit conventions.

## Security

The pairing flow generates a one-time token that is hashed (Fernet) at rest. See [`SECURITY.md`](./SECURITY.md) for the threat model and disclosure policy.

## Related

- **[hermes-nodes](https://github.com/blaspat/hermes-nodes)** — the Go node binary (the "arm")
- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** — the agent framework this plugs into
- **[OpenClaw](https://docs.openclaw.ai/nodes)** — the design pattern this is inspired by (different protocol, different ecosystem)

## License

MIT — see [`LICENSE`](./LICENSE). © 2026 Blasius Patrick.
