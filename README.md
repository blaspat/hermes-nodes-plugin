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
- **Run:** See usage section for pairing and CLI commands.
- **Example:**
  ```bash
  hermes node pair --name work-laptop
  hermes node list
  ```

## Core Features
- `node_exec(target, command)` — run shell commands on a paired node
- `node_read(target, path)` — read a file on a paired node
- `node_write(target, path, content, mode="overwrite")` — write a file on a paired node
- `node_list()` — list paired nodes and their connection state
- CLI subcommands: `hermes node pair`, `hermes node list`, `hermes node revoke`, `hermes node status`

## Usage
Detailed steps for pairing nodes, listing, executing, reading, and writing. Includes TLS configuration options (terminate TLS in nginx, direct plugin TLS, self-signed for dev).

## Contributing
- Code Style: Follow the project's `CONTRIBUTING.md` guidelines.
- Test it: `pytest tests/ -v` for unit tests, `pytest tests/e2e/ -v -m e2e` for end‑to‑end.
- Workflow: Fork → Branch → PR. See [CONTRIBUTING.md](./CONTRIBUTING.md) for detailed contribution workflow.

## Roadmap / FAQ
- [ ] Stabilize TLS handling across environments.
- [ ] Add auto‑revoke stale connections.
- Q: Does it support Windows nodes?
  A: Not officially; only Linux/macOS via WSL or similar.

## Related
- **[hermes-nodes](`github.com/blaspat/hermes-nodes`):** Remote node binary.
- **[Hermes Agent](`github.com/NousResearch/hermes-agent`):** Core agent framework.
- **[Documentation](README.md):** Full plugin docs.

---
License: [MIT](LICENSE.md) | Author: © 2026 Blasius Patrick
