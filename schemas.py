"""Tool schemas — what the LLM reads to decide when to call each tool."""

from __future__ import annotations

# Common string param shape — keeps the schemas uniform.
_STRING_PARAM: dict[str, str] = {"type": "string"}


NODE_EXEC: dict[str, str | dict] = {
    "name": "node_exec",
    "description": (
        "Run a shell command on a paired remote node (e.g. a laptop with "
        "the hermes-nodes Go binary installed) and return its stdout/stderr "
        "and exit code. The command runs in the node's persistent shell, "
        "so `cd` and `export` between calls persist. Use `hermes node list` "
        "(or `node_list()`) to see which nodes are currently connected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                **_STRING_PARAM,
                "description": "Node name (as paired), e.g. 'work-laptop'",
            },
            "command": {
                **_STRING_PARAM,
                "description": "Shell command to run on the node",
            },
            "cwd": {
                **_STRING_PARAM,
                "description": (
                    "Optional working-directory override. Omit to use the "
                    "node's persistent cwd."
                ),
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Optional env-var overrides merged into the node's "
                    "persistent env. Omit to use the node's existing env."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Per-call timeout in milliseconds. Defaults to 60000 "
                    "(60s, matches the protocol default)."
                ),
            },
        },
        "required": ["target", "command"],
    },
}


NODE_READ: dict[str, str | dict] = {
    "name": "node_read",
    "description": (
        "Read a UTF-8 text file from a paired remote node. Returns the "
        "file content as a string, plus `size_bytes` and a `truncated` "
        "flag (true if the node hit the 10 MB cap). The node enforces its "
        "own path allowlist; out-of-allowlist paths return "
        "`path_not_allowed`. For binary files, the bytes are decoded with "
        "errors='replace' — invalid sequences become U+FFFD."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                **_STRING_PARAM,
                "description": "Node name (as paired), e.g. 'work-laptop'",
            },
            "path": {
                **_STRING_PARAM,
                "description": "Absolute path on the node's filesystem",
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "description": "Per-call timeout in milliseconds",
            },
        },
        "required": ["target", "path"],
    },
}


NODE_WRITE: dict[str, str | dict] = {
    "name": "node_write",
    "description": (
        "Write UTF-8 text to a file on a paired remote node. By default "
        "overwrites the file; pass `mode='append'` to add to an existing "
        "file or `mode='create'` to refuse overwriting. Content is "
        "capped at 10 MB per call (matches the protocol's file cap). The "
        "node enforces its own path allowlist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                **_STRING_PARAM,
                "description": "Node name (as paired), e.g. 'work-laptop'",
            },
            "path": {
                **_STRING_PARAM,
                "description": "Absolute path on the node's filesystem",
            },
            "content": {
                **_STRING_PARAM,
                "description": "UTF-8 text to write to the file",
            },
            "mode": {
                "type": "string",
                "enum": ["create", "overwrite", "append"],
                "description": (
                    "Write mode. 'overwrite' (default) replaces any "
                    "existing file; 'append' adds to it; 'create' refuses "
                    "to overwrite."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "description": "Per-call timeout in milliseconds",
            },
        },
        "required": ["target", "path", "content"],
    },
}


NODE_LIST: dict[str, str | dict] = {
    "name": "node_list",
    "description": (
        "List all paired remote nodes that are currently connected to the "
        "WSS server. Each entry includes the node name, the time it "
        "connected, the last heartbeat timestamp, the session id, and the "
        "remote address. Disconnected nodes (dropped TCP, missed "
        "heartbeats, revoked tokens) are not returned by this call — "
        "they're only visible in the CLI's `hermes node list`."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# Tool implementations live in tools.py. Schemas and handlers are wired
# together in __init__.py via register_tool().
SCHEMAS: dict[str, dict[str, str | dict]] = {
    "node_exec": NODE_EXEC,
    "node_read": NODE_READ,
    "node_write": NODE_WRITE,
    "node_list": NODE_LIST,
}
