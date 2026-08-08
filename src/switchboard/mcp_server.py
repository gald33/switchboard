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
from datetime import datetime, timezone
from typing import Any, Callable

from . import __version__
from .client import Client, Identity, LeaseHeld, SwitchboardError, detect_identity
from .config import ClientConfig
from .crypto import generate_key
from .timing import EFFORT_LEVELS, MIN_SAMPLES, Forecast, TimingModel

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


def _mark_if_expired(forecast: dict[str, Any]) -> dict[str, Any]:
    """Flag a forecast whose p95 has already passed.

    Comparing two timestamps is exactly the kind of arithmetic this
    feature exists to keep out of model reasoning, and the answer changes
    what the forecast is worth: past p95 the event it predicted has almost
    certainly already happened, so the checkpoints carry no remaining
    information and should not be used to defer a check. Left as a flag
    rather than stripping the fields, since a reader may still want to see
    what was predicted.
    """
    p95 = forecast.get("p95")
    if not isinstance(p95, str):
        return forecast
    try:
        expired = datetime.fromisoformat(p95) < datetime.now(timezone.utc)
    except ValueError:
        return forecast
    return {**forecast, "expired": True} if expired else forecast


def _now_iso() -> str:
    """Anchor for interpreting any absolute timestamp in a tool response —
    a timing_forecast checkpoint, a message's 'at' — without the model
    needing its own notion of wall-clock time.
    """
    return datetime.now(timezone.utc).isoformat()


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


#: ADVANCED and deliberately loud about it. This redirects a single call to a
#: private workspace/key instead of the default one — see the `keygen` tool.
#: It is a field, not a mode: every other call you make is unaffected, and
#: nothing about it is inferred or improvised — you only ever set this to a
#: pair an agreement already exists for.
_CUSTOM_SCOPE = {
    "type": "object",
    "description": (
        "ADVANCED, rarely needed. Redirects this ONE call to a private workspace and "
        "key instead of your default one. Only set this if you and the specific "
        "agent(s) you are coordinating with have already agreed, outside of "
        "Switchboard, on a shared workspace and key for a private conversation — get "
        "one with the 'keygen' tool, then tell your peers the workspace and key "
        "directly (a prompt, a dm, however you already trust them). Never invent one "
        "unilaterally and expect a peer to find their way into it. Omit this for all "
        "normal work; it does not change your default workspace for anything else "
        "you do."
    ),
    "properties": {
        "workspace": {**_STR, "description": "the agreed-upon private workspace name"},
        "key": {**_STR, "description": "the agreed-upon private key (omit for no encryption)"},
    },
    "required": ["workspace"],
    "additionalProperties": False,
}

#: Advice for *reading* a forecast, appended where messages arrive.
#: Worth the tokens because how the signal is used swings its value more
#: than the signal's own accuracy does: in simulation, treating p50/p95 as
#: two individual poll times was worse than a plain fixed interval, while
#: using them to size a checking cadence was substantially better. The
#: protocol does not prescribe either — this is a hint, not a rule.
_FORECAST_ADVICE = (
    " A message may carry 'timing_forecast' — the sender's own estimate of when it will "
    "next check its messages ('p50' ~50% likely by then, 'p95' ~95%), compared against "
    "the 'now' field in this result. Predictions, not promises, and you are free to "
    "ignore them. If you do use one, prefer sizing how often you check to the forecast "
    "rather than checking exactly at p50 and p95; a stale forecast whose p95 has already "
    "passed carries no information."
)

#: Optional semantic timing hints. This is the entire burden a model takes
#: on for adaptive timing forecasts — everything past this (consulting
#: local history, estimating percentiles, attaching a forecast) happens
#: automatically. Neither field is required; omit both for no forecast.
#: Both fields describe the stretch of work you are about to disappear
#: into, because that is what determines how long until you next look up.
_TIMING_CLASS = {
    **_STR,
    "description": (
        "OPTIONAL. A short free-form label for the work you are about to do before you "
        "next check your messages, e.g. 'coding' or 'research'. Used only to look up "
        "your own local timing history and estimate when you will next come looking — "
        "it is never sent as-is. Any label is accepted; the ones listed below are just "
        "the ones you have been using lately."
    ),
}
_TIMING_EFFORT = {
    **_STR,
    "enum": list(EFFORT_LEVELS),
    "description": (
        "OPTIONAL. Your rough relative size estimate for that stretch of work — 'low', "
        "'medium', or 'high'. Not a time estimate; your local timing history converts "
        "it into one. Omit if you don't want a forecast."
    ),
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
            "'wait' to block for up to 25s waiting for a message." + _FORECAST_ADVICE
        ),
        "inputSchema": _schema({
            "task": {**_STR, "description": "what you are working on right now"},
            "wait": {**_NUM, "description": "seconds to long-poll for messages (0-25)"},
            "execution_class": _TIMING_CLASS,
            "effort": _TIMING_EFFORT,
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
            "custom_scope": _CUSTOM_SCOPE,
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
            "custom_scope": _CUSTOM_SCOPE,
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
            "permanent belongs in a commit message or a PR instead. Optionally pass "
            "execution_class/effort to attach a check-in forecast for collaborators — "
            "advisory only, based on your own local history."
        ),
        "inputSchema": _schema({
            "channel": {**_STR, "description": "channel name, e.g. 'build' or 'backend'"},
            "message": {**_STR, "description": "what to say"},
            "type": {**_STR, "description": "optional tag, e.g. 'warning', 'handoff'"},
            "ttl": {**_NUM, "description": "seconds to keep it (default 3600)"},
            "custom_scope": _CUSTOM_SCOPE,
            "execution_class": _TIMING_CLASS,
            "effort": _TIMING_EFFORT,
        }, ["channel", "message"]),
    },
    {
        "name": "dm",
        "description": (
            "Send a message to one specific agent, by the agent id shown in roster. Use "
            "this to hand off context, answer another agent's question, or warn one agent "
            "specifically that you are about to change something it depends on. Optionally "
            "pass execution_class/effort to attach a check-in forecast — advisory only, "
            "based on your own local history."
        ),
        "inputSchema": _schema({
            "to": {**_STR, "description": "recipient agent id (see roster)"},
            "message": {**_STR, "description": "what to say"},
            "type": _STR,
            "ttl": _NUM,
            "custom_scope": _CUSTOM_SCOPE,
            "execution_class": _TIMING_CLASS,
            "effort": _TIMING_EFFORT,
        }, ["to", "message"]),
    },
    {
        "name": "inbox",
        "description": (
            "Read messages addressed to you or posted to channels you subscribe to. Each "
            "message is returned once — the read position advances automatically. Set "
            "'wait' to block until something arrives." + _FORECAST_ADVICE
        ),
        "inputSchema": _schema({
            "channels": {
                "type": "array", "items": _STR,
                "description": "override your subscriptions for this read",
            },
            "wait": {**_NUM, "description": "seconds to long-poll (0-25)"},
            "peek": {**_BOOL, "description": "read without advancing your position"},
            "custom_scope": _CUSTOM_SCOPE,
            "execution_class": _TIMING_CLASS,
            "effort": _TIMING_EFFORT,
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
    {
        "name": "keygen",
        "description": (
            "Mint a fresh (key, workspace) pair for a private side-conversation with "
            "specific other agents — a confidentiality boundary you set up yourself, not "
            "a hub permission. Purely local: nothing is sent to the hub. Tell the pair "
            "directly to exactly the peers you want included (a prompt, a dm, however you "
            "already trust them), then have each of you pass it as 'custom_scope' on say / "
            "dm / inbox / claim / release. Always mint a fresh workspace here rather than "
            "reusing your default one — reusing it makes 'roster' wrongly warn everyone "
            "else in that workspace about a key mismatch."
        ),
        "inputSchema": _schema({}),
    },
]


# --- the bridge -------------------------------------------------------------


class Bridge:
    """Holds the hub client and turns tool calls into hub calls."""

    def __init__(self) -> None:
        self.config = ClientConfig.from_env()
        self.identity: Identity = detect_identity()
        self.client = Client(self.config, agent_id=self.identity.agent_id)
        self.timing = TimingModel(self.config.timing_db)
        self._registered = False

    def close(self) -> None:
        self.client.close()
        self.timing.close()

    def tools(self) -> list[dict[str, Any]]:
        """The tool list, with the execution-class shortlist filled in from
        this agent's own recent usage.

        The offer is advisory: `execution_class` stays an open string, so a
        model can always coin a new label, and a new label that catches on
        rises into the shortlist on its own. Purely local — no hub call —
        and it falls back to the static list if the timing store is
        unreadable, since tools/list must never fail over a nicety.
        """
        try:
            classes = self.timing.top_classes(self.identity.agent_id, self.config.workspace)
        except Exception:
            return TOOLS
        if not classes:
            return TOOLS
        hint = f" Ones you use most: {', '.join(classes)} — or any other label that fits."

        patched = []
        for tool in TOOLS:
            properties = tool["inputSchema"]["properties"]
            if "execution_class" not in properties:
                patched.append(tool)
                continue
            prop = {**properties["execution_class"]}
            prop["description"] = prop["description"] + hint
            patched.append({
                **tool,
                "inputSchema": {
                    **tool["inputSchema"],
                    "properties": {**properties, "execution_class": prop},
                },
            })
        return patched

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

    def _touch(self) -> int:
        """Bump presence and report unread DMs — called on every tool, not
        just checkin, so a ping gets noticed as soon as the agent does
        anything at all rather than only when it remembers to check in.

        Deliberately narrow: one presence update, one indexed count. It does
        NOT renew leases (an unrelated op renewing every held lease as a side
        effect would be a real behavior change — an agent could no longer let
        one claim lapse while staying active elsewhere) and does NOT drain
        the inbox (that risks marking a message read before the agent ever
        saw it). Both stay behind the explicit `checkin`/`inbox` calls.
        """
        self._ensure_registered()
        try:
            result = self.client.heartbeat(renew_leases=False)
        except SwitchboardError as exc:
            if exc.status == 404:
                # Presence expired between calls; re-register and retry once,
                # same recovery checkin already relies on.
                self._registered = False
                self._ensure_registered()
                result = self.client.heartbeat(renew_leases=False)
            else:
                raise
        return result.get("unread_dms", 0)

    # --- individual tools ---

    def whoami(self) -> dict[str, Any]:
        unread_dms = self._touch()
        out = {
            "agent_id": self.identity.agent_id,
            "name": self.identity.name,
            "kind": self.identity.kind,
            "branch": self.identity.branch,
            "unread_dms": unread_dms,
            "workspace": self.config.workspace,
            "hub": self.config.url,
        }
        calibration = self._calibration()
        if calibration:
            out["forecast_calibration"] = calibration
        return out

    def _calibration(self) -> dict[str, Any] | None:
        """How well this agent's own past forecasts have held up.

        Surfaced because the data was otherwise dark: an agent could
        publish badly calibrated forecasts indefinitely with no way to
        discover it, and collaborators have no channel to tell it. Local
        only — nothing here is shared, and it stays out of the response
        entirely until there is enough history to mean anything.
        """
        try:
            report = self.timing.calibration(
                self.identity.agent_id, self.config.workspace)
        except Exception:
            return None
        if report["samples"] < MIN_SAMPLES:
            return None
        summary = {
            "samples": report["samples"],
            "p50_hit_rate": round(report["p50_hit_rate"], 2),
            "p95_hit_rate": round(report["p95_hit_rate"], 2),
        }
        if report["dropped_as_outliers"]:
            # Every dropped observation was longer than anything the
            # estimator could see, so p95 is optimistic by an unknown
            # margin and the hit rate above flatters itself.
            summary["ignored_as_too_long"] = report["dropped_as_outliers"]
        if not 0.3 <= report["p50_hit_rate"] <= 0.7 or report["p95_hit_rate"] < 0.8:
            summary["note"] = (
                "Your check-in forecasts are not matching reality "
                "(p50 should land near 0.5, p95 near 0.95). Collaborators "
                "reading them will be misled; weigh your own timing "
                "classifications accordingly."
            )
        return summary

    def roster(self) -> dict[str, Any]:
        unread_dms = self._touch()
        agents = self.client.agents()
        leases = self.client.leases()
        mismatched = self.client.key_mismatches(agents)
        by_holder: dict[str, list[str]] = {}
        for lease in leases:
            by_holder.setdefault(lease["holder"], []).append(lease["resource"])
        return {
            "you": self.identity.agent_id,
            "unread_dms": unread_dms,
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
            **({
                "WARNING": (
                    f"{len(mismatched)} agent(s) in this workspace hold a different "
                    "encryption key. You cannot see their messages, they cannot see "
                    "yours, and your leases do not exclude each other. Tell the user "
                    "that SWITCHBOARD_KEY does not match across agents — coordination "
                    "here is silently not working."
                ),
                "mismatched_agents": [a["agent_id"] for a in mismatched],
            } if mismatched else {}),
        }

    def checkin(self, task: str | None = None, wait: float = 0.0,
                execution_class: str | None = None,
                effort: str | None = None) -> dict[str, Any]:
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
        # This call read the inbox — that is the event every forecast
        # predicts, so it closes any window this agent had open. Only then
        # does a fresh declaration open the next one.
        self._note_look()
        forecast = self._declare(execution_class, effort)
        return {
            "holding": [
                {"resource": le["resource"], "expires_in": le["expires_in"]}
                for le in result["leases"]
            ],
            "messages": [self._msg(m) for m in messages],
            "new_messages": len(messages),
            "now": _now_iso(),
            **({"timing_forecast": self._sender_forecast(forecast)} if forecast else {}),
        }

    def claim(self, resource: str, note: str | None = None,
              ttl: float | None = None,
              custom_scope: dict[str, str] | None = None) -> dict[str, Any]:
        unread_dms = self._touch()
        try:
            lease = self.client.acquire(resource, note=note, ttl=ttl, custom_scope=custom_scope)
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
                "unread_dms": unread_dms,
            }
        return {
            "acquired": True,
            "resource": lease["resource"],
            "expires_in": lease["expires_in"],
            "note": "Renewed automatically by checkin. Call release when you finish.",
            "unread_dms": unread_dms,
        }

    def release(self, resource: str,
                custom_scope: dict[str, str] | None = None) -> dict[str, Any]:
        unread_dms = self._touch()
        released = self.client.release(resource, custom_scope=custom_scope)
        return {"released": released, "resource": resource, "unread_dms": unread_dms}

    def claims(self, mine: bool = False) -> dict[str, Any]:
        unread_dms = self._touch()
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
            "unread_dms": unread_dms,
        }

    def _body_with_forecast(self, message: str, execution_class: str | None,
                             effort: str | None) -> tuple[Any, Forecast | None]:
        """Classify this send locally and fold any resulting forecast into
        the outgoing body. Purely advisory — see timing.py; never raises on
        a bad/missing local timing store, since a forecast is a nice-to-have,
        not something correctness can depend on.
        """
        forecast = self._declare(execution_class, effort)
        if forecast is None:
            return message, None
        return {"text": message, "timing_forecast": forecast.as_message_meta()}, forecast

    def _declare(self, execution_class: str | None,
                 effort: str | None) -> Forecast | None:
        """Open a forecast window. Never raises: the whole feature is
        advisory, so a broken local timing store costs a hint, not a call.
        """
        try:
            return self.timing.declare(
                self.identity.agent_id, self.config.workspace, execution_class, effort,
            )
        except Exception:
            return None

    def _note_look(self) -> None:
        """Tell the timing model the agent just read its inbox — the event
        every forecast is a prediction of. Never raises, same reasoning.
        """
        try:
            self.timing.note_look(self.identity.agent_id, self.config.workspace)
        except Exception:
            pass

    @staticmethod
    def _sender_forecast(forecast: Forecast) -> dict[str, Any]:
        """The richer view the *sender* gets back on its own tool call.

        Includes everything shared over the wire (absolute p50/p95, so it
        matches what a receiver sees) plus relative seconds, since the
        sender already knows "now" is this exact moment and a countdown is
        more directly useful than re-deriving one from two timestamps.
        """
        return {
            **forecast.as_message_meta(),
            "p50_in_seconds": round(forecast.p50_seconds),
            "p95_in_seconds": round(forecast.p95_seconds),
        }

    def say(self, channel: str, message: str, type: str = "note",
            ttl: float | None = None,
            custom_scope: dict[str, str] | None = None,
            execution_class: str | None = None,
            effort: str | None = None) -> dict[str, Any]:
        unread_dms = self._touch()
        body, forecast = self._body_with_forecast(message, execution_class, effort)
        msg = self.client.post(channel, body, type=type, ttl=ttl, custom_scope=custom_scope)
        out = {
            "posted": True, "channel": msg["channel"], "seq": msg["seq"],
            "unread_dms": unread_dms, "now": _now_iso(),
        }
        if forecast:
            out["timing_forecast"] = self._sender_forecast(forecast)
        return out

    def dm(self, to: str, message: str, type: str = "note",
           ttl: float | None = None,
           custom_scope: dict[str, str] | None = None,
           execution_class: str | None = None,
           effort: str | None = None) -> dict[str, Any]:
        unread_dms = self._touch()
        body, forecast = self._body_with_forecast(message, execution_class, effort)
        msg = self.client.send(to, body, type=type, ttl=ttl, custom_scope=custom_scope)
        out = {
            "sent": True, "to": to, "seq": msg["seq"],
            "unread_dms": unread_dms, "now": _now_iso(),
        }
        if forecast:
            out["timing_forecast"] = self._sender_forecast(forecast)
        return out

    def inbox(self, channels: list[str] | None = None, wait: float = 0.0,
              peek: bool = False,
              custom_scope: dict[str, str] | None = None,
              execution_class: str | None = None,
              effort: str | None = None) -> dict[str, Any]:
        unread_dms = self._touch()
        messages = self.client.inbox(
            channels=channels, wait=min(max(wait, 0.0), 25.0), peek=peek,
            custom_scope=custom_scope,
        )
        # Reading is the predicted event, whether or not the cursor moved —
        # a peek still means the agent looked.
        self._note_look()
        forecast = self._declare(execution_class, effort)
        out = {
            "messages": [self._msg(m) for m in messages], "count": len(messages),
            "unread_dms": unread_dms, "now": _now_iso(),
        }
        if forecast:
            out["timing_forecast"] = self._sender_forecast(forecast)
        return out

    def keygen(self) -> dict[str, Any]:
        """Mint a fresh (key, workspace) pair for a private side-conversation.

        Purely local — no hub call, no registration needed first.
        """
        key = generate_key()
        workspace = "w_" + generate_key()[:16]
        return {"key": key, "workspace": workspace}

    def history(self, channel: str, limit: float = 30) -> dict[str, Any]:
        unread_dms = self._touch()
        messages = self.client.history(channel, limit=int(limit))
        return {
            "channel": channel, "messages": [self._msg(m) for m in messages],
            "unread_dms": unread_dms, "now": _now_iso(),
        }

    def board_set(self, key: str, value: Any, ttl: float | None = None) -> dict[str, Any]:
        unread_dms = self._touch()
        entry = self.client.board_set(key, value, ttl=ttl)
        return {"key": entry["key"], "revision": entry["revision"],
                "expires_in": entry["expires_in"], "unread_dms": unread_dms}

    def board_get(self, key: str) -> dict[str, Any]:
        unread_dms = self._touch()
        entry = self.client.board_entry(key)
        if entry is None:
            return {"key": key, "found": False, "value": None, "unread_dms": unread_dms}
        return {
            "key": key, "found": True, "value": entry["value"],
            "revision": entry["revision"], "updated_by": entry["updated_by"],
            "updated_at": entry["updated_at"], "unread_dms": unread_dms,
        }

    def board_list(self, prefix: str | None = None) -> dict[str, Any]:
        unread_dms = self._touch()
        entries = self.client.board_list(prefix=prefix)
        return {
            "entries": [
                {"key": e["key"], "revision": e["revision"], "updated_by": e["updated_by"],
                 "updated_at": e["updated_at"], "expires_in": e["expires_in"]}
                for e in entries
            ],
            "count": len(entries),
            "unread_dms": unread_dms,
        }

    @staticmethod
    def _msg(m: dict[str, Any]) -> dict[str, Any]:
        body = m["body"]
        timing_forecast = None
        if (isinstance(body, dict) and set(body.keys()) <= {"text", "timing_forecast"}
                and "timing_forecast" in body):
            timing_forecast = body.get("timing_forecast")
            body = body.get("text")
        out = {
            "seq": m["seq"],
            "from": m["from"],
            "channel": m["channel"],
            "body": body,
            "at": m["created_at"],
        }
        if timing_forecast:
            out["timing_forecast"] = _mark_if_expired(timing_forecast)
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
                "so your claims stay alive, and release when you are done. Every tool "
                "result also carries 'unread_dms' — how many direct messages are waiting, "
                "kept current on every call so a ping is noticed as soon as you do "
                "anything, not just when you next checkin. Treat a nonzero value as a cue "
                "to call inbox or checkin soon rather than waiting."
            ),
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _response(request_id, {})

    if method == "tools/list":
        return _response(request_id, {"tools": bridge.tools()})

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
