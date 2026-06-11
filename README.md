# hermes-nodes-plugin
> One‑paragraph hook: A Hermes Agent plugin that turns any Hermes profile into a “brain” to command remote nodes over an authenticated WebSocket.

## Table of Contents
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Core Features](#core-features)
- [Usage](#usage)
- [Contributing](#contributing)
- [FAQ](#faq)
- [Related](#related)

## Prerequisites
> Install Python 3.11+, uv (optional), and have a Hermes Agent profile set up.

## Installation
> Install the plugin via pip:
>
> ```bash
> python -m pip install hermes-nodes-plugin==0.1.0
> # or with uv for speed
> uv pip install hermes-nodes-plugin==0.1.0
> ```
>
> For development, clone the repo and install in editable mode.
>
> ```bash
> git clone https://github.com/blaspat/hermes-nodes-plugin.git
> cd hermes-nodes-plugin
> python -m pip install -e .
> # or uv pip install -e .
> ```
>
> Verify the CLI appears:
>
> ```bash
> hermes node --help
> ```

## Core Features
- `node_exec(target, command)`: run shell commands on a paired node.
- `node_read(target, path)`: read a file on a paired node.
- `node_write(target, path, content, mode="overwrite")`: write a file on a paired node.
- `node_list()`: list paired nodes and their connection state.

## Usage
1. **Configure** – edit `~/.hermes/hermes-nodes.yaml` with host/port/TLS settings.
2. **Start the plugin** – the Hermes binary loads the plugin on startup.
3. **Pair a node** – `hermes node pair --name my‑devbox` prints a one‑time token; run `hermes-node pair --server wss://<server>:6969 --token <token>` on the remote.
4. **Run a command** – `hermes node exec my‑devbox "echo hello"` returns the output.
5. **Transfer files** – `node_read("my‑devbox", "~/project/README.md")` or `node_write("my‑devbox", "~/project/new.txt", "sample", mode="create")`.
6. **Revoke** – `hermes node revoke --name my‑devbox` removes the node and its token.

## Contributing
- Code Style: Follow `CONTRIBUTING.md`.
- Test it: `pytest -v` for unit tests, `pytest -v -m e2e` for end‑to‑end.
- Flow: Fork → Branch → PR.

## FAQ
- **Q:** Does it support Windows nodes?
> A: Not officially; only Linux/macOS (WSL works).
- **Q:** How is audit handled?
> A: All interactions are logged to `~/.hermes/logs/nodes-audit.log` and retained per `audit_retention_days`.

## Related
- **hermes‑nodes:** Remote node binary (`github.com/blaspat/hermes-nodes`).
- **Hermes Agent:** Core framework (`github.com/NousResearch/hermes-agent`).
- **Documentation:** Full plugin docs (`~/.hermes/hermes-nodes-plugin/README.md`).

---
License: MIT | Author: © 2026 Blasius Patrick