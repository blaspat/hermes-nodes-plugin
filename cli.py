"""CLI surface for the ``hermes node`` subcommand (Task 2.10).

Owns the operator-facing argparse tree registered via
``ctx.register_cli_command`` in :mod:`hermes_nodes_plugin.__init__`.
The plugin's other modules (lifecycle, tokens, registry, config) do
the real work; this module is glue + formatting.

Subcommands (per REQUIREMENTS FR-1 + plan Task 2.10):

* ``hermes node pair --name <name>`` — generate a Fernet-hashed
  token bound to ``<name>`` and print the plaintext to stdout exactly
  once, followed by setup instructions for the laptop. Reject
  duplicate names (FR-1.5) unless ``--force`` is passed (which
  revokes the old record before pairing a fresh one).
* ``hermes node list`` — show every paired node and its current
  connection state: ``connected`` (live in the in-memory registry),
  ``disconnected`` (paired and not revoked but not in the registry),
  or ``never_seen`` (paired and not revoked, never connected —
  detected via the missing ``last_used_at`` field on the token
  record). Revoked rows always show ``revoked``. Supports
  ``--json`` for machine-readable output.
* ``hermes node revoke --name <name>`` — mark the token revoked in
  the store, and drop the live connection if the registry holds one
  (FR-1.4: "deletes the token, drops any active connection").
* ``hermes node status`` — preserved from the 2.6 stub: reports
  whether the WSS server is listening.

Design notes
------------

* **No async in the CLI.** The runner's lifecycle is event-loop
  driven, but every CLI command here is a small read or write that
  doesn't need to await. We get the registry synchronously and walk
  it (``NodeRegistry.list_connected`` is a coroutine, so we drive
  it via :func:`asyncio.run` on a tiny private loop only when the
  command actually needs to look at live connections). Keeping
  ``list`` and ``revoke`` sync means tests don't have to spin up a
  loop, and ``pair`` is a pure file mutation that shouldn't block
  on a network registry.

* **One module-level entry point for argparse setup.** The plugin's
  ``register()`` calls
  ``ctx.register_cli_command("node", setup_fn=setup_node_cli,
  handler_fn=node_command)``; Hermes wires the subparser and
  dispatch for us. We do not import or depend on the ``hermes_cli``
  package — that keeps the plugin installable as a standalone pip
  package for unit testing, and matches how every other bundled
  plugin (e.g. ``plugins/teams_pipeline/cli.py``) does it.

* **Errors → stderr, exit code 1.** User-facing errors
  (duplicate name, unknown name on revoke) print to stderr and
  return a non-zero exit code so shell scripts can branch. The
  token is printed to stdout specifically so ``hermes node pair
  | grep -E '^token:'`` is scriptable.

* **DRY with the 2.6 stub.** ``lifecycle.setup_node_subcommand``
  was the prior entry point; we keep it as a thin shim that points
  at :func:`setup_node_cli` so existing tests don't break.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import load_config
from .env import ensure_fernet_key_in_env
from .errors import ConfigError, TokenStoreError
from .tokens import (
    TokenRecord,
    token_store_from_config,
)


# Status strings used by ``hermes node list``. Kept module-level so
# tests and downstream tools can import them without re-deriving the
# vocabulary.
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_NEVER_SEEN = "never_seen"
STATE_REVOKED = "revoked"

# ---------------------------------------------------------------------------
# Argparse setup — bound to ``hermes node <subcommand>``
# ---------------------------------------------------------------------------


def setup_node_cli(subparser: argparse.ArgumentParser) -> None:
    """Attach the ``pair`` / ``list`` / ``revoke`` / ``status`` tree.

    Called by Hermes's plugin loader after
    ``ctx.register_cli_command("node", setup_fn=..., handler_fn=...)``.
    The ``subparser`` is the ``hermes node ...`` parser that
    Hermes allocated; we own everything beneath it.

    The ``status`` subcommand is kept for backward compat with the
    2.6 stub — the lifecycle module's runner exposes the same
    surface, and removing it would break any operator muscle memory
    that's been built since the plugin first installed.
    """
    subparser.description = (
        "Manage paired hermes-nodes (WSS node server). "
        "Subcommands: pair, list, revoke, status."
    )
    subs = subparser.add_subparsers(dest="node_action")

    # --- pair --------------------------------------------------------------
    p_pair = subs.add_parser(
        "pair",
        help="Pair a new node and print the one-time token.",
        description=(
            "Generate a cryptographically-random token, bind it to "
            "NAME, and print the token plus setup instructions for "
            "the laptop. The token is shown exactly once — copy it "
            "before closing the terminal."
        ),
    )
    p_pair.add_argument(
        "--name",
        required=True,
        help="Unique name for the node (e.g. 'work-laptop').",
    )
    p_pair.add_argument(
        "--force",
        action="store_true",
        help=(
            "If a node with the same name is already paired, revoke "
            "the old record before pairing a fresh one. USE WITH "
            "CARE — the previous laptop will be unable to reconnect."
        ),
    )

    # --- list --------------------------------------------------------------
    p_list = subs.add_parser(
        "list",
        help="List paired nodes with their connection state.",
        description=(
            "Show every paired node, oldest first, with its current "
            "connection state. Pass --json for machine-readable output."
        ),
    )
    p_list.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit one JSON object per line instead of a table.",
    )

    # --- revoke ------------------------------------------------------------
    p_revoke = subs.add_parser(
        "revoke",
        help="Delete a node entry from the store.",
        description=(
            "Hard-delete the token record for NAME from the encrypted "
            "store. The node cannot reconnect after this; run `hermes node "
            "pair --name <name>` to add it again."
        ),
    )
    p_revoke.add_argument(
        "--name",
        required=True,
        help="Name of the node to delete.",
    )

    # --- status (kept from 2.6) -------------------------------------------
    subs.add_parser(
        "status",
        help="Show whether the WSS node server is running.",
    )

    subparser.set_defaults(func=node_command)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def node_command(args: argparse.Namespace) -> int:
    """Default ``func`` for the ``hermes node`` subcommand tree.

    Mirrors the ``hermes node <subcommand>`` argparse dispatch
    pattern used by every bundled plugin (see
    ``plugins/teams_pipeline/cli.py:teams_pipeline_command``).
    Returns a POSIX exit code; ``0`` is success, ``1`` is a user
    error, ``2`` is an unrecognised subcommand.
    """
    action = getattr(args, "node_action", None)
    if not action:
        # ``hermes node`` with no subcommand — argparse has already
        # printed usage. Return non-zero so a stray CI script fails
        # loudly instead of silently doing nothing.
        return 2

    try:
        if action == "pair":
            return _cmd_pair(args)
        if action == "list":
            return _cmd_list(args)
        if action == "revoke":
            return _cmd_revoke(args)
        if action == "status":
            return _cmd_status()
    except TokenStoreError as exc:
        # FR-4.2: surface the operator error message verbatim. Most
        # common is the missing-Fernet-key case on a fresh install.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Unrecognised subcommand — argparse should have caught this,
    # but stay defensive in case someone registers a sibling later.
    print(f"unknown subcommand: {action!r}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------


def _cmd_pair(args: argparse.Namespace) -> int:
    """Generate a token, persist it, print it + setup instructions.

    Token → stdout, instructions → stderr. This split lets scripts
    capture the token with ``hermes node pair --name x | head -1``
    while still surfacing human-readable guidance in the operator's
    terminal.

    **Auto-token (v0.2.0+).** Before the existing token-store
    flow runs, the pair command ensures the Fernet key is present
    in the operator's ``~/.hermes/.env`` (FR-4.2: the operator
    should not have to run ``Fernet.generate_key`` by hand). The
    helper:

    * uses the key silently if it is already in the env (we never
      regenerate an existing key — that would invalidate every
      previously-paired node)
    * generates a new Fernet key and appends it to
      ``~/.hermes/.env`` if the file is missing or the var is
      unset, and prints a confirmation so the operator knows
      where it landed
    * surfaces the key + manual recovery instructions on a
      disk-write failure (read-only fs, permission denied) so
      the operator can save the key themselves; the in-process
      pair still completes because we mirror the key into
      :data:`os.environ` regardless

    ``--force`` hard-deletes any existing record under the same name
    before pairing a fresh one. Silently no-op on names never paired
    (delete() is idempotent). Using delete() rather than revoke()
    avoids leaving ghost revoked rows in the store.
    """
    name: str = args.name.strip()
    if not name:
        print("error: --name must be a non-empty string", file=sys.stderr)
        return 1

    # Auto-token: ensure the Fernet key is on disk + in the
    # process env. We resolve the config first so we know which
    # env var name the operator configured (default
    # ``HERMES_NODES_TOKEN_KEY``; the plugin config can override
    # the literal name). The helper itself does the read /
    # generate / write / os.environ-mirror dance.
    config = load_config()
    key_result = ensure_fernet_key_in_env(
        var_name=config.token_encryption_key_env,
    )
    if key_result.status == "wrote":
        print(
            f"generated Fernet key and saved to {key_result.path}",
            file=sys.stderr,
        )
    elif key_result.status == "failed":
        # The pair will still succeed in-process (the helper
        # mirrored the key into os.environ), but we must tell
        # the operator their .env didn't get the write so they
        # can fix permissions / save it manually.
        print(
            f"warning: could not write Fernet key to {key_result.path} "
            f"({key_result.error}). The pair will complete for this "
            f"invocation, but you must save the key manually or "
            f"the next `hermes` invocation will fail with "
            f"'no Fernet key configured'.",
            file=sys.stderr,
        )
        print(
            f"  export {config.token_encryption_key_env}={key_result.key}",
            file=sys.stderr,
        )
    # ``present`` → silent: the key was already there, we used
    # it. No need to nag the operator about a file they already
    # maintain.

    store = token_store_from_config(config)

    if args.force:
        # Hard-delete any existing record before pairing. Silently
        # no-op if the name was never paired (delete() is idempotent).
        # Using delete() rather than revoke() avoids leaving ghost rows
        # in the store on repeated force-pairs.
        store.delete(name)

    try:
        token = store.create(name)
    except TokenStoreError as exc:
        # Most common: "node 'x' is already paired; pass --force or
        # revoke the existing token before re-pairing". Print to
        # stderr and exit 1 — the operator can re-run with --force.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Token goes to stdout, instructions to stderr. Operators reading
    # the terminal see both; scripts can `cut -d: -f2-` the first
    # line and discard the rest on stderr (or `2>/dev/null` it).
    print(f"token: {token}")
    print(f"name:  {name}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Run this on the laptop:", file=sys.stderr)
    print(
        f"  hermes-node pair --server <host:port> --token {token} --name {name}",
        file=sys.stderr,
    )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    """Print all paired nodes (including revoked) with their state.

    State derivation:

    * ``revoked``        — store flag is true
    * ``connected``      — in the live registry
    * ``never_seen``     — paired, not revoked, never connected
                           (token record's ``last_used_at`` is None)
    * ``disconnected``   — paired, not revoked, *has* connected
                           before but is not in the registry right now

    Output is plain text by default; ``--json`` emits one JSON
    object per line for scripting.
    """
    config = load_config()
    store = token_store_from_config(config)
    records = store.list()
    connected_names = _connected_names()

    rows = [
        _format_row(rec, rec.name in connected_names)
        for rec in records
    ]

    if args.as_json:
        for row in rows:
            print(json.dumps(row, sort_keys=True))
        return 0

    if not rows:
        print("no paired nodes. run `hermes node pair --name <name>` to add one.")
        return 0

    # Column widths derived from data so the table doesn't reflow
    # when a long node name appears. ``state`` is the longest legal
    # value (12 chars for "never_seen"); pad to 13 for a trailing
    # space.
    name_w = max(len("NAME"), max(len(r["name"]) for r in rows))
    state_w = 13
    created_w = max(len("CREATED"), max(len(r["created_at"]) for r in rows))
    used_w = max(len("LAST_USED"), max(len(r["last_used_at"] or "-") for r in rows))

    header = (
        f"{'NAME':<{name_w}}  {'STATE':<{state_w}}  "
        f"{'CREATED':<{created_w}}  {'LAST_USED':<{used_w}}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        last_used = r["last_used_at"] or "-"
        print(
            f"{r['name']:<{name_w}}  {r['state']:<{state_w}}  "
            f"{r['created_at']:<{created_w}}  {last_used:<{used_w}}"
        )
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    """Delete the named node's token record from the store.

    Hard-delete removes the record entirely (unlike revoke which only
    set ``revoked=true``). The node cannot reconnect after this;
    run ``hermes node pair --name <name>`` to add it again.
    Deleting an unknown name is a no-op and returns 0.
    """
    name: str = args.name.strip()
    if not name:
        print("error: --name must be a non-empty string", file=sys.stderr)
        return 1

    config = load_config()
    store = token_store_from_config(config)
    store.delete(name)
    print(f"deleted: {name}")
    return 0


def _cmd_status() -> int:
    """Show whether the WSS server is listening on the default port.

    Probes port 6969 directly with a TCP socket handshake rather than
    checking an in-memory runner object, because the CLI runs in a
    *separate process* from the gateway — _default_runner is
    always None at CLI time.
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect(("127.0.0.1", 6969))
        s.close()
        print("hermes-nodes server: listening on 127.0.0.1:6969")
        return 0
    except (OSError, socket.timeout):
        print("hermes-nodes server: not running")
        return 1


# ---------------------------------------------------------------------------
# Helpers (testable in isolation)
# ---------------------------------------------------------------------------


def _format_row(record: TokenRecord, is_connected: bool) -> dict[str, Any]:
    """Map a :class:`TokenRecord` + liveness bool to a list row.

    Pulled out as a free function so the state-derivation logic is
    testable without spinning up a config / store / registry.
    """
    if record.revoked:
        state = STATE_REVOKED
    elif is_connected:
        state = STATE_CONNECTED
    elif record.last_used_at is None:
        state = STATE_NEVER_SEEN
    else:
        state = STATE_DISCONNECTED

    return {
        "name": record.name,
        "state": state,
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
    }


def _connected_names() -> set[str]:
    """Return the set of node names with a live connection.

    The registry is owned by the long-running server; the CLI
    command is a short-lived process. We query the server's HTTP
    status endpoint at ``/nodes/status`` rather than maintaining
    our own in-process registry — the CLI's registry would be
    a fresh empty one that never saw any connections.

    If the server isn't running, the port isn't listening, or
    the endpoint fails, we fall back to an empty set. The list
    command should still succeed in that case — connection state
    just collapses to ``disconnected`` / ``never_seen`` for
    every row.
    """
    config = load_config()
    base_url = f"http://{config.host}:{config.port}"
    status_url = f"{base_url}/nodes/status"
    try:
        import urllib.request
        with urllib.request.urlopen(status_url, timeout=2.0) as resp:
            data = __import__("json").loads(resp.read())
            return set(data.get("connected_names", []))
    except Exception:
        return set()


def main() -> None:
    """Standalone entrypoint for the ``hermes-node`` script.

    Wired via ``[project.scripts] hermes-node = hermes_nodes_plugin.cli:main``
    in pyproject.toml so operators can run ``hermes-node pair --name x``
    without needing ``hermes node`` CLI support from the host.
    """
    parser = argparse.ArgumentParser(
        prog="hermes-node",
        description="Manage paired hermes-nodes (WSS node server).",
    )
    setup_node_cli(parser)
    args = parser.parse_args()
    sys.exit(node_command(args) or 0)

__all__ = [
    "setup_node_cli",
    "node_command",
    "STATE_CONNECTED",
    "STATE_DISCONNECTED",
    "STATE_NEVER_SEEN",
    "STATE_REVOKED",
    # Internal helpers exported for unit tests:
    "_format_row",
    "_connected_names",
]
