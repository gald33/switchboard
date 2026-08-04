"""MCP server exposing a Switchboard hub as tools.

This speaks the MCP stdio protocol (JSON-RPC 2.0 over stdin/stdout) directly
rather than through an SDK. The tools-only subset is small and stable, and
implementing it here means the bridge has no dependency beyond ``httpx`` and
cannot break when an SDK renames its API between majors.

Run it as ``switchboard-mcp``. Everything is configured by environment:

    SWITCHBOARD_URL        hub base URL
    SWITCHBOARD_TOKEN      bearer token
    SWITCHBOARD_WORKSPACE  workspace to join
    SWITCHBOARD_AGENT_ID   override the inferred agent id

stdout is the protocol channel — every diagnostic goes to stderr.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

from . import __version__
from .client import Client, Identity, LeaseHeld, SwitchboardError, detect_identity
from .config import ClientConfig

# Versions of the MCP spec this server knows how to speak. If a client asks
# for something else we answer with our newest and let it decide.
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INTERNAL_ERROR = -32603


def log(message: str) -> None:
    print(f"[switchboard-mcp] {message}", file=sys.stderr, flush=True)


# --- tool schemas -----------------------------------------------------------

_STR = {"type": "string"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "whoami",
        "description": (
            "Your identity on the Switchboard hub: agent id, workspace, branch, and which "
            "hub you are connected to. Call this once at the start of a session so you know "
            "how other agents will refer to you."
        ),
        "inputSchema": _schema({}),
    },
    {
        "name": "roster",
        "description": (
            "Who else is working right now: every live agent in the workspace with its "
            "branch, current task, how long since it was last seen, and what it currently "
            "holds. Call this BEFORE starting work to avoid duplicating what another agent "
            "is already doing."
        ),
        "inputSchema": _schema({}),
    },
    {
        "name": "checkin",
        "description": (
            "The main loop tool. Sends a heartbeat (keeping you listed as alive), renews "
            "every lease you hold, and returns any messages other agents sent you since "
            "your last check-in. Call this periodically during long tasks — if you stop "
            "calling it, your claims expire and free themselves for other agents. Set "
            "'wait' to block for up to 25s waiting for a message."
        ),
        "inputSchema": _schema({
            "task": {**_STR, "description": "what you are working on right now"},
            "wait": {**_NUM, "description": "seconds to long-poll for messages (0-25)"},
        }),
    },
    {
        "name": "claim",
        "description": (
            "Take an exclusive, self-expiring lease on a resource so no other agent works "
            "it at the same time. The resource is any string you and the other agents "
            "agree on — a path ('backend/migrations'), a subsystem ('auth'), a ticket id. "
            "Returns an error naming the current holder if someone else has it; pick "
            "different work rather than waiting. The lease expires on its own, so a "
            "crashed agent never blocks anyone permanently."
        ),
        "inputSchema": _schema({
            "resource": {**_STR, "description": "resource key to claim"},
            "note": {**_STR, "description": "short reason, shown to other agents"},
            "ttl": {**_NUM, "description": "seconds to hold it (default 900)"},
        }, ["resource"]),
    },
    {
        "name": "release",
        "description": (
            "Give up a lease as soon as you are done with it, rather than letting it time "
            "out. Always release when you finish or abandon a piece of work."
        ),
        "inputSchema": _schema({
            "resource": {**_STR, "description": "resource key to release"},
        }, ["resource"]),
    },
    {
        "name": "claims",
        "description": (
            "List live leases in the workspace — what is taken, by whom, and for how much "
            "longer. Pass mine=true for only your own."
        ),
        "inputSchema": _schema({"mine": _BOOL}),
    },
    {
        "name": "say",
        "description": (
            "Post a message to a channel every subscribed agent will see. Use this for "
            "things that are true for a while but should not outlive the work: what you "
            "are about to change, an interface you just altered, a warning that a test is "
            "flaky. Messages expire after an hour by default — anything that should be "
            "permanent belongs in a commit message or a PR instead."
        ),
        "inputSchema": _schema({
            "channel": {**_STR, "description": "channel name, e.g. 'build' or 'backend'"},
            "message": {**_STR, "description": "what to say"},
            "type": {**_STR, "description": "optional tag, e.g. 'warning', 'handoff'"},
            "ttl": {**_NUM, "description": "seconds to keep it (default 3600)"},
        }, ["channel", "message"]),
    },
    {
        "name": "dm",
        "description": (
            "Send a message to one specific agent, by the agent id shown in roster. Use "
            "this to hand off context, answer another agent's question, or warn one agent "
            "specifically that you are about to change something it depends on."
        ),
        "inputSchema": _schema({
            "to": {**_STR, "description": "recipient agent id (see roster)"},
            "message": {**_STR, "description": "what to say"},
            "type": _STR,
            "ttl": _NUM,
        }, ["to", "message"]),
    },
    {
        "name": "inbox",
        "description": (
            "Read messages addressed to you or posted to channels you subscribe to. Each "
            "message is returned once — the read position advances automatically. Set "
            "'wait' to block until something arrives."
        ),
        "inputSchema": _schema({
            "channels": {
                "type": "array", "items": _STR,
                "description": "override your subscriptions for this read",
            },
            "wait": {**_NUM, "description": "seconds to long-poll (0-25)"},
            "peek": {**_BOOL, "description": "read without advancing your position"},
        }),
    },
    {
        "name": "history",
        "description": (
            "Recent messages on a channel regardless of whether you have already read "
            "them. Use this to catch up on context when you join a channel mid-flight."
        ),
        "inputSchema": _schema({
            "channel": _STR,
            "limit": {**_NUM, "description": "how many messages (default 30)"},
        }, ["channel"]),
    },
    {
        "name": "board_set",
        "description": (
            "Write a value to the shared blackboard — a key/value scratch space for "
            "handoffs too big for a message: a plan another agent should continue, a list "
            "of files already migrated, a decision with its reasoning. Values expire after "
            "24h by default. Overwrites the key."
        ),
        "inputSchema": _schema({
            "key": {**_STR, "description": "key, e.g. 'migration/plan'"},
            "value": {"description": "any JSON value, or a string"},
            "ttl": {**_NUM, "description": "seconds to keep it (default 86400)"},
        }, ["key", "value"]),
    },
    {
        "name": "board_get",
        "description": "Read one value from the shared blackboard. Returns null if absent.",
        "inputSchema": _schema({"key": _STR}, ["key"]),
    },
    {
        "name": "board_list",
        "description": (
            "List blackboard keys with who wrote them and when, optionally filtered by "
            "prefix. Use this to discover what context other agents have left behind."
        ),
        "inputSchema": _schema({"prefix": _STR}),
    },
]


# --- the bridge -------------------------------------------------------------


class Bridge:
    """Holds the hub client and turns tool calls into hub calls."""

    def __init__(self) -> None:
        self.config = ClientConfig.from_env()
        self.identity: Identity = detect_identity()
        self.client = Client(self.config, agent_id=self.identity.agent_id)
        self._registered = False

    def close(self) -> None:
        self.client.close()

    def _ensure_registered(self) -> None:
        """Register lazily and idempotently.

        Registering on first use rather than at startup means a hub that is
        briefly down does not prevent the MCP server from starting; the agent
        just gets an error on the first tool call and can retry.
        """
        if self._registered:
            return
        self.client.register(
            name=self.identity.name,
            kind=self.identity.kind,
            branch=self.identity.branch,
            meta=self.identity.meta,
        )
        self._registered = True

    # --- individual tools ---

    def whoami(self) -> dict[str, Any]:
        self._ensure_registered()
        return {
            "agent_id": self.identity.agent_id,
            "name": self.identity.name,
            "kind": self.identity.kind,
            "branch": self.identity.branch,
            "workspace": self.config.workspace,
            "hub": self.config.url,
        }

    def roster(self) -> dict[str, Any]:
        self._ensure_registered()
        agents = self.client.agents()
        leases = self.client.leases()
        by_holder: dict[str, list[str]] = {}
        for lease in leases:
            by_holder.setdefault(lease["holder"], []).append(lease["resource"])
        return {
            "you": self.identity.agent_id,
            "agents": [
                {
                    "agent_id": a["agent_id"],
                    "name": a["name"],
                    "kind": a["kind"],
                    "branch": a.get("branch"),
                    "task": a.get("task"),
                    "last_seen": a["last_seen_at"],
                    "stale": a.get("stale", False),
                    "holding": by_holder.get(a["agent_id"], []),
                    "is_you": a["agent_id"] == self.identity.agent_id,
                }
                for a in agents
            ],
            "count": len(agents),
        }

    def checkin(self, task: str | None = None, wait: float = 0.0) -> dict[str, Any]:
        self._ensure_registered()
        try:
            result = self.client.heartbeat(task=task)
        except SwitchboardError as exc:
            if exc.status == 404:
                # Presence expired while we were busy; re-register and retry.
                self._registered = False
                self._ensure_registered()
                result = self.client.heartbeat(task=task)
            else:
                raise
        messages = self.client.inbox(wait=min(max(wait, 0.0), 25.0))
        return {
            "holding": [
                {"resource": le["resource"], "expires_in": le["expires_in"]}
                for le in result["leases"]
            ],
            "messages": [self._msg(m) for m in messages],
            "new_messages": len(messages),
        }

    def claim(self, resource: str, note: str | None = None,
              ttl: float | None = None) -> dict[str, Any]:
        self._ensure_registered()
        try:
            lease = self.client.acquire(resource, note=note, ttl=ttl)
        except LeaseHeld as exc:
            return {
                "acquired": False,
                "resource": resource,
                "held_by": exc.holder,
                "free_in": exc.expires_in,
                "advice": (
                    f"{exc.holder} is working on this. Pick different work rather than "
                    f"waiting; use dm to coordinate if you must have it."
                ),
            }
        return {
            "acquired": True,
            "resource": lease["resource"],
            "expires_in": lease["expires_in"],
            "note": "Renewed automatically by checkin. Call release when you finish.",
        }

    def release(self, resource: str) -> dict[str, Any]:
        self._ensure_registered()
        return {"released": self.client.release(resource), "resource": resource}

    def claims(self, mine: bool = False) -> dict[str, Any]:
        self._ensure_registered()
        holder = self.identity.agent_id if mine else None
        leases = self.client.leases(holder=holder)
        return {
            "leases": [
                {
                    "resource": le["resource"],
                    "holder": le["holder"],
                    "is_you": le["holder"] == self.identity.agent_id,
                    "note": le.get("note"),
                    "expires_in": le["expires_in"],
                }
                for le in leases
            ],
            "count": len(leases),
        }

    def say(self, channel: str, message: str, type: str = "note",
            ttl: float | None = None) -> dict[str, Any]:
        self._ensure_registered()
        msg = self.client.post(channel, message, type=type, ttl=ttl)
        return {"posted": True, "channel": msg["channel"], "seq": msg["seq"]}

    def dm(self, to: str, message: str, type: str = "note",
           ttl: float | None = None) -> dict[str, Any]:
        self._ensure_registered()
        msg = self.client.send(to, message, type=type, ttl=ttl)
        return {"sent": True, "to": to, "seq": msg["seq"]}

    def inbox(self, channels: list[str] | None = None, wait: float = 0.0,
              peek: bool = False) -> dict[str, Any]:
        self._ensure_registered()
        messages = self.client.inbox(
            channels=channels, wait=min(max(wait, 0.0), 25.0), peek=peek
        )
        return {"messages": [self._msg(m) for m in messages], "count": len(messages)}

    def history(self, channel: str, limit: float = 30) -> dict[str, Any]:
        self._ensure_registered()
        messages = self.client.history(channel, limit=int(limit))
        return {"channel": channel, "messages": [self._msg(m) for m in messages]}

    def board_set(self, key: str, value: Any, ttl: float | None = None) -> dict[str, Any]:
        self._ensure_registered()
        entry = self.client.board_set(key, value, ttl=ttl)
        return {"key": entry["key"], "revision": entry["revision"],
                "expires_in": entry["expires_in"]}

    def board_get(self, key: str) -> dict[str, Any]:
        self._ensure_registered()
        entry = self.client.board_entry(key)
        if entry is None:
            return {"key": key, "found": False, "value": None}
        return {
            "key": key, "found": True, "value": entry["value"],
            "revision": entry["revision"], "updated_by": entry["updated_by"],
            "updated_at": entry["updated_at"],
        }

    def board_list(self, prefix: str | None = None) -> dict[str, Any]:
        self._ensure_registered()
        entries = self.client.board_list(prefix=prefix)
        return {
            "entries": [
                {"key": e["key"], "revision": e["revision"], "updated_by": e["updated_by"],
                 "updated_at": e["updated_at"], "expires_in": e["expires_in"]}
                for e in entries
            ],
            "count": len(entries),
        }

    @staticmethod
    def _msg(m: dict[str, Any]) -> dict[str, Any]:
        out = {
            "from": m["from"],
            "channel": m["channel"],
            "body": m["body"],
            "at": m["created_at"],
        }
        if m.get("type") and m["type"] != "note":
            out["type"] = m["type"]
        return out

    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        handler: Callable[..., Any] | None = getattr(self, name, None)
        if handler is None or name.startswith("_") or name not in {t["name"] for t in TOOLS}:
            raise ValueError(f"unknown tool: {name}")
        return handler(**arguments)


# --- JSON-RPC plumbing ------------------------------------------------------


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def handle_request(bridge: Bridge, request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns None for notifications."""
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    is_notification = "id" not in request

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
        return _response(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "switchboard", "version": __version__},
            "instructions": (
                "Switchboard coordinates you with other AI agents working this same "
                "codebase. Call roster before starting work to see who else is active, "
                "claim before editing a resource others might touch, checkin periodically "
                "so your claims stay alive, and release when you are done."
            ),
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _response(request_id, {})

    if method == "tools/list":
        return _response(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            result = bridge.dispatch(name, arguments)
        except LeaseHeld as exc:
            return _response(request_id, _tool_result(
                {"error": "lease_held", "detail": str(exc), **exc.payload}, is_error=True
            ))
        except SwitchboardError as exc:
            return _response(request_id, _tool_result(
                {"error": "hub_error", "detail": str(exc), "status": exc.status}, is_error=True
            ))
        except TypeError as exc:
            return _response(request_id, _tool_result(
                {"error": "bad_arguments", "detail": str(exc)}, is_error=True
            ))
        except ValueError as exc:
            return _response(request_id, _tool_result(
                {"error": "unknown_tool", "detail": str(exc)}, is_error=True
            ))
        except OSError as exc:
            return _response(request_id, _tool_result(
                {"error": "hub_unreachable", "detail": str(exc),
                 "hub": bridge.config.url}, is_error=True
            ))
        return _response(request_id, _tool_result(result))

    if is_notification:
        return None
    return _error(request_id, JSONRPC_METHOD_NOT_FOUND, f"unknown method: {method}")


def serve_stdio(bridge: Bridge, stdin: Any = None, stdout: Any = None) -> None:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            payload = _error(None, JSONRPC_PARSE_ERROR, "invalid JSON")
            stdout.write(json.dumps(payload) + "\n")
            stdout.flush()
            continue
        if not isinstance(request, dict):
            payload = _error(None, JSONRPC_INVALID_REQUEST, "expected a JSON object")
            stdout.write(json.dumps(payload) + "\n")
            stdout.flush()
            continue
        try:
            response = handle_request(bridge, request)
        except Exception:  # noqa: BLE001 - a tool bug must not kill the server
            log("unhandled error:\n" + traceback.format_exc())
            response = _error(
                request.get("id"), JSONRPC_INTERNAL_ERROR, "internal error (see stderr)"
            )
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    bridge = Bridge()
    log(
        f"agent={bridge.identity.agent_id} workspace={bridge.config.workspace} "
        f"hub={bridge.config.url}"
    )
    try:
        serve_stdio(bridge)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
