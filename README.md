# hermes-nodes-plugin
>A Hermes Agent plugin that turns any Hermes profile into a "brain" that can command remote nodes — laptops, NAS, headless boxes — over an authenticated WebSocket. Pairs with the [`hermes-nodes`](https://github.com/blaspat/hermes-nodes) Go binary.

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

## Core Features
- `node_exec(target, command)` — run shell commands on a paired node
- `node_read(target, path)` — read a file on a paired node
- `node_write(target, path, content, mode="overwrite")` — write a file on a paired node
- `node_list()` — list paired nodes and their connection state
- CLI subcommands: `hermes node pair`, `hermes node list`, `hermes node revoke`, `hermes node status`

## Usage
1. **Installation** – Done in Quick Start.
2. **TLS Configuration** – The plugin listens on `127.0.0.1:6969` by default. For secure production deployments, proxy the WebSocket through nginx or stunnel:
   ```nginx
   upstream hermes_nodes {
     server 127.0.0.1:6969;
   }
   server {
     listen 443 ssl;
     server_name hermes.example.com;
     ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
     ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
     location /ws/nodes {
       proxy_pass http://hermes_nodes;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
     }
   }
   ```
   Skip `tls_cert_path`/`tls_key_path` in the plugin’s config.
   <br>“Option B” – if you prefer the plugin to terminate TLS directly, set `tls_cert_path` and `tls_key_path` in `~/.hermes/hermes-nodes.yaml`.
3. **Node Pairing** – Run the pair command on the server, then on the node. The node binary connects over the WebSocket and stores its token in the encrypted store.
4. **Command Execution** – Within an agent session, use the `node_exec` tool:
   ```python
   node_exec("work-laptop", "pwd")
   --> "/home/packer"
   ```
5. **File Operations** – Reading and writing is straightforward. Paths are interpreted relative to the node’s home directory unless prefixed with `/`.
6. **Disconnecting** – To unpair a node, use `hermes node revoke –name work-laptop`. The server will drop any live connection and deny future sessions until a new token is created.

## Contributing
- Code Style: Follow the project's `CONTRIBUTING.md` guidelines.
- Test it: `pytest tests/ -v` for unit tests, `pytest tests/e2e/ -v -m e2e` for end‑to‑end.
- Workflow: Fork → Branch → PR. See [CONTRIBUTING.md](./CONTRIBUTING.md) for detailed contribution workflow.

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
