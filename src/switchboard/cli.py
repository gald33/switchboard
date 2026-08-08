"""Command-line interface for Switchboard.

Two audiences: humans looking at what their agents are doing, and shell hooks
wiring agents into a hub. Commands that agents call in hooks all accept
``--quiet`` and use exit codes, so they compose in scripts:

    switchboard claim db/migrations || exit 0   # someone else has it
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__
from .client import Client, LeaseHeld, SwitchboardError, detect_identity
from .config import ClientConfig
from .crypto import CryptoError, generate_key

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 2


# --- output helpers ---------------------------------------------------------


def _use_color(stream: Any) -> bool:
    return stream.isatty() and not os.environ.get("NO_COLOR")


class Fmt:
    def __init__(self, color: bool) -> None:
        self.color = color

    def _wrap(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def dim(self, t: str) -> str:
        return self._wrap(t, "2")

    def bold(self, t: str) -> str:
        return self._wrap(t, "1")

    def green(self, t: str) -> str:
        return self._wrap(t, "32")

    def yellow(self, t: str) -> str:
        return self._wrap(t, "33")

    def red(self, t: str) -> str:
        return self._wrap(t, "31")

    def cyan(self, t: str) -> str:
        return self._wrap(t, "36")


def _ago(iso_ts: str | None) -> str:
    """Render an ISO timestamp as a compact relative age."""
    if not iso_ts:
        return "-"
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return iso_ts
    delta = (datetime.now(timezone.utc) - then).total_seconds()
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _body_text(body: Any) -> str:
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# --- command implementations ------------------------------------------------


def _make_client(args: argparse.Namespace) -> Client:
    config = ClientConfig.from_env()
    if args.url:
        config.url = args.url.rstrip("/")
    if args.token:
        config.token = args.token
    if args.workspace:
        config.workspace = args.workspace
    if getattr(args, "key", None):
        config.key = args.key
    identity = detect_identity(agent_id=args.agent_id)
    return Client(config, agent_id=identity.agent_id)


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "the server needs extra dependencies: pip install 'agent-switchboard[server]'",
            file=sys.stderr,
        )
        return EXIT_ERROR
    from .config import ServerConfig
    from .server import create_app

    config = ServerConfig.from_env()
    if args.db:
        config.db_path = args.db
    if args.token:
        config.token = args.token
    if args.keys_file:
        config.keys_file = args.keys_file
    if args.self_issued_keys:
        config.self_issued_keys = True
    modes_set = sum([bool(config.token), bool(config.keys_file), config.self_issued_keys])
    if modes_set > 1:
        print(
            "error: --token/SWITCHBOARD_TOKEN, --keys-file/SWITCHBOARD_KEYS_FILE and "
            "--self-issued-keys/SWITCHBOARD_SELF_ISSUED_KEYS are mutually exclusive — "
            "a hub runs in exactly one auth mode.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    resolver = None
    store = None
    if config.keys_file:
        from .auth import load_static_keys

        try:
            resolver = load_static_keys(config.keys_file)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"loaded {len(resolver)} scoped key(s) from {config.keys_file}", file=sys.stderr)
    elif config.self_issued_keys:
        from .auth import SelfIssuedKeyResolver
        from .store import Store

        # Built here rather than left to create_app's own default so the same
        # instance backs both the resolver's lookups and the app's routes —
        # two Store objects would still agree (same file, WAL-mode SQLite),
        # but there is no reason to open the database twice.
        store = Store(config.db_path)
        resolver = SelfIssuedKeyResolver(store)
        print(
            "self-issued keys enabled — clients register via "
            "`switchboard register-key --workspace <name>`",
            file=sys.stderr,
        )
    elif not config.token:
        print(
            "warning: no token set — this hub accepts any caller. "
            "Set SWITCHBOARD_TOKEN or pass --token before exposing it.",
            file=sys.stderr,
        )
    print(f"switchboard {__version__} → http://{args.host}:{args.port}  db={config.db_path}")
    uvicorn.run(
        create_app(config, store=store, resolver=resolver), host=args.host, port=args.port,
        log_level=args.log_level,
    )
    return EXIT_OK


def cmd_keygen(args: argparse.Namespace) -> int:
    """Print a fresh workspace key, and an opaque workspace name to go with it.

    The workspace name is the one thing a hub sees in the clear — it is the
    shard and routing key, so it cannot be encrypted. But nothing requires it
    to be *meaningful*, and "acme/billing" tells an operator more than any
    other single string they hold. Emitting an opaque one here is the cheapest
    privacy win available, and it costs nothing to take.
    """
    key = generate_key()
    workspace = "w_" + generate_key()[:16]
    if args.json:
        _print_json({"key": key, "workspace": workspace})
        return EXIT_OK
    print(key)
    if sys.stdout.isatty():
        print(
            "\nShare this with the agents in the workspace and nobody else:\n"
            f"  export SWITCHBOARD_KEY={key}\n"
            "\nThe hub never receives it, so it cannot read this workspace — and\n"
            "cannot help you recover it either. Everything expires within a day,\n"
            "so losing it costs less here than almost anywhere else.\n"
            "\nThe workspace name is NOT encrypted — it is how the hub routes.\n"
            "Use an opaque one and it stops being the most descriptive thing\n"
            "the hub holds. Keep the readable name in your own notes:\n"
            f"  export SWITCHBOARD_WORKSPACE={workspace}",
            file=sys.stderr,
        )
    return EXIT_OK


def cmd_register_key(args: argparse.Namespace) -> int:
    """Generate an auth key locally and bind it to a workspace on a hub.

    Only meaningful against a hub running ``--self-issued-keys``; every other
    hub already knows what its tokens are and has nothing to register. First
    key to claim a workspace name owns it — a teammate re-running this with
    the *same* key against the *same* workspace is a harmless no-op, but a
    different key claiming a name already taken is rejected, the same way a
    squatted username would be.
    """
    import httpx

    url = (args.url or os.environ.get("SWITCHBOARD_URL") or MANAGED_HUB_URL).rstrip("/")
    workspace = args.workspace or os.environ.get("SWITCHBOARD_WORKSPACE") or _default_workspace(
        Path(".").resolve()
    )
    token = secrets.token_urlsafe(32)
    try:
        response = httpx.post(
            f"{url}/keys/register",
            json={"workspace": workspace},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        print(f"error: could not reach {url}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        print(f"error: {detail}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        _print_json({"token": token, "workspace": workspace, "url": url})
        return EXIT_OK
    print(token)
    if sys.stdout.isatty():
        print(
            f"\nBound to workspace {workspace!r} on {url}.\n"
            "Share this with the agents in the workspace and nobody else:\n"
            f"  export SWITCHBOARD_TOKEN={token}\n"
            f"  export SWITCHBOARD_URL={url}\n"
            f"  export SWITCHBOARD_WORKSPACE={workspace}\n"
            "\nThe hub stores only a hash of this, never the key itself. If it's\n"
            "lost, there is no recovery — generate a new one against a new\n"
            "workspace name, the same as `switchboard keygen` for encryption.",
            file=sys.stderr,
        )
    return EXIT_OK


def cmd_whoami(args: argparse.Namespace) -> int:
    identity = detect_identity(agent_id=args.agent_id)
    config = ClientConfig.from_env()
    encrypted = bool(args.key or config.key)
    payload = {
        "agent_id": identity.agent_id,
        "name": identity.name,
        "kind": identity.kind,
        "branch": identity.branch,
        "workspace": args.workspace or config.workspace,
        "hub": args.url or config.url,
        "encrypted": encrypted,
        "meta": identity.meta,
    }
    if args.json:
        _print_json(payload)
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    for key in ("agent_id", "name", "kind", "branch", "workspace", "hub"):
        print(f"{fmt.dim(key.rjust(10))}  {payload[key]}")
    print(f"{fmt.dim('encrypted'.rjust(10))}  "
          + (fmt.green("yes — the hub cannot read this workspace")
             if encrypted else "no"))
    return EXIT_OK


def cmd_register(args: argparse.Namespace) -> int:
    identity = detect_identity(agent_id=args.agent_id)
    with _make_client(args) as hub:
        agent = hub.register(
            name=args.name or identity.name,
            kind=args.kind or identity.kind,
            branch=identity.branch,
            task=args.task,
            channels=args.channel or [],
            meta=identity.meta,
            ttl=args.ttl,
        )
    if args.json:
        _print_json(agent)
    elif not args.quiet:
        print(f"registered {agent['agent_id']} ({agent['kind']}) in {agent['workspace']}")
    return EXIT_OK


def cmd_agents(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        agents = hub.agents()
        mismatched = hub.key_mismatches(agents)
    if args.json:
        _print_json(agents)
        return EXIT_OK
    if not agents:
        print("no agents registered")
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    print(fmt.bold(f"{'AGENT':<34} {'KIND':<7} {'BRANCH':<24} {'SEEN':<10} TASK"))
    for a in agents:
        seen = _ago(a["last_seen_at"])
        seen_txt = fmt.yellow(seen) if a.get("stale") else seen
        print(
            f"{a['agent_id'][:33]:<34} {a['kind'][:6]:<7} "
            f"{(a.get('branch') or '-')[:23]:<24} {seen_txt:<10} {a.get('task') or ''}"
        )
    if mismatched:
        # A key mismatch is otherwise completely silent: their messages never
        # reach this inbox, this agent's never reach theirs, and neither
        # side's leases exclude the other. Nothing raises, so say it here.
        print(
            "\n" + fmt.red(
                f"warning: {len(mismatched)} agent(s) here hold a DIFFERENT "
                f"workspace key."
            )
            + "\nYou cannot see their messages and they cannot see yours, and "
              "your leases\ndo not exclude each other. Check SWITCHBOARD_KEY "
              "matches on every agent.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    return EXIT_OK


def cmd_claim(args: argparse.Namespace) -> int:
    fmt = Fmt(_use_color(sys.stdout))
    with _make_client(args) as hub:
        try:
            lease = hub.acquire(args.resource, note=args.note, ttl=args.ttl)
        except LeaseHeld as exc:
            if args.json:
                _print_json({"acquired": False, **exc.payload})
            elif not args.quiet:
                print(
                    fmt.yellow(f"held by {exc.holder} for another {_dur(exc.expires_in)}"),
                    file=sys.stderr,
                )
            return EXIT_CONFLICT
    if args.json:
        _print_json({"acquired": True, **lease})
    elif not args.quiet:
        print(fmt.green(f"claimed {lease['resource']} for {_dur(lease['expires_in'])}"))
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        released = hub.release(args.resource, force=args.force)
    if args.json:
        _print_json({"released": released})
    elif not args.quiet:
        print(f"released {args.resource}" if released else f"no lease on {args.resource}")
    return EXIT_OK


def cmd_claims(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        leases = hub.leases(holder=args.holder)
    if args.json:
        _print_json(leases)
        return EXIT_OK
    if not leases:
        print("nothing claimed")
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    print(fmt.bold(f"{'RESOURCE':<38} {'HOLDER':<30} {'EXPIRES':<8} NOTE"))
    for le in leases:
        print(
            f"{le['resource'][:37]:<38} {le['holder'][:29]:<30} "
            f"{_dur(le['expires_in']):<8} {le.get('note') or ''}"
        )
    return EXIT_OK


def cmd_say(args: argparse.Namespace) -> int:
    body = _read_body(args)
    with _make_client(args) as hub:
        msg = hub.post(args.channel, body, type=args.type, thread=args.thread, ttl=args.ttl)
    if args.json:
        _print_json(msg)
    elif not args.quiet:
        print(f"posted #{msg['seq']} to {msg['channel']}")
    return EXIT_OK


def cmd_dm(args: argparse.Namespace) -> int:
    body = _read_body(args)
    with _make_client(args) as hub:
        msg = hub.send(args.to, body, type=args.type, thread=args.thread, ttl=args.ttl)
    if args.json:
        _print_json(msg)
    elif not args.quiet:
        print(f"sent #{msg['seq']} to {args.to}")
    return EXIT_OK


def _read_body(args: argparse.Namespace) -> Any:
    parts: Sequence[str] = args.message or []
    text = " ".join(parts)
    if text == "-" or (not text and not sys.stdin.isatty()):
        text = sys.stdin.read().strip()
    if args.json_body:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise SystemExit(f"--json-body given but message is not valid JSON: {exc}") from exc
    return text


def cmd_inbox(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        messages = hub.inbox(
            channels=args.channel,
            wait=args.wait,
            limit=args.limit,
            peek=args.peek,
            include_own=args.include_own,
        )
    if args.json:
        _print_json(messages)
        return EXIT_OK
    if not messages:
        if not args.quiet:
            print("(nothing new)")
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    for m in messages:
        head = f"{fmt.cyan(m['channel'])} {fmt.bold(m['from'])} {fmt.dim(_ago(m['created_at']))}"
        if m.get("type") and m["type"] != "note":
            head += f" {fmt.dim('[' + m['type'] + ']')}"
        print(head)
        print(f"  {_body_text(m['body'])}")
    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        messages = hub.history(args.channel, limit=args.limit)
    if args.json:
        _print_json(messages)
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    for m in messages:
        print(f"{fmt.dim(_ago(m['created_at']))} {fmt.bold(m['from'])}: {_body_text(m['body'])}")
    return EXIT_OK


def cmd_channels(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        channels = hub.channels()
    if args.json:
        _print_json(channels)
        return EXIT_OK
    for c in channels:
        print(f"{c['channel']:<30} {c['messages']:>4} msg  {_ago_epoch(c.get('latest_at'))}")
    return EXIT_OK


def _ago_epoch(ts: float | None) -> str:
    if not ts:
        return "-"
    delta = max(0, time.time() - ts)
    return _dur(delta) + " ago"


def cmd_board(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        if args.board_action == "set":
            value: Any = args.value
            if args.json_body:
                value = json.loads(args.value)
            entry = hub.board_set(
                args.board_key, value, ttl=args.ttl, if_revision=args.if_revision
            )
            if args.json:
                _print_json(entry)
            elif not args.quiet:
                print(f"{entry['key']} = rev {entry['revision']}")
        elif args.board_action == "get":
            entry = hub.board_entry(args.board_key)
            if entry is None:
                print(f"no entry at {args.board_key}", file=sys.stderr)
                return EXIT_ERROR
            if args.json:
                _print_json(entry)
            else:
                print(_body_text(entry["value"]))
        elif args.board_action == "list":
            entries = hub.board_list(prefix=args.prefix)
            if args.json:
                _print_json(entries)
            else:
                for e in entries:
                    print(
                        f"{e['key']:<34} rev {e['revision']:<4} "
                        f"{e['updated_by'][:24]:<26} {_dur(e['expires_in'])}"
                    )
        elif args.board_action == "delete":
            deleted = hub.board_delete(args.board_key)
            if not args.quiet:
                print("deleted" if deleted else "not found")
    return EXIT_OK


def cmd_checkin(args: argparse.Namespace) -> int:
    """Heartbeat, renew leases, and drain the inbox in one round-trip."""
    with _make_client(args) as hub:
        result = hub.heartbeat(task=args.task, ttl=args.ttl)
        messages = hub.inbox(wait=args.wait, limit=args.limit)
    payload = {
        "agent": result["agent"],
        "leases": result["leases"],
        "messages": messages,
    }
    if args.json:
        _print_json(payload)
        return EXIT_OK
    fmt = Fmt(_use_color(sys.stdout))
    held = ", ".join(le["resource"] for le in result["leases"]) or "nothing"
    print(f"{fmt.dim('holding')}  {held}")
    if messages:
        print(f"{fmt.dim('new')}      {len(messages)} message(s)")
        for m in messages:
            print(f"  {fmt.cyan(m['channel'])} {fmt.bold(m['from'])}: {_body_text(m['body'])}")
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    """Follow messages continuously — a tail -f for the hub."""
    fmt = Fmt(_use_color(sys.stdout))
    with _make_client(args) as hub:
        try:
            while True:
                messages = hub.inbox(channels=args.channel, wait=25.0, limit=args.limit)
                for m in messages:
                    print(
                        f"{fmt.dim(_ago(m['created_at']))} {fmt.cyan(m['channel'])} "
                        f"{fmt.bold(m['from'])}: {_body_text(m['body'])}",
                        flush=True,
                    )
        except KeyboardInterrupt:
            return EXIT_OK


def cmd_stats(args: argparse.Namespace) -> int:
    with _make_client(args) as hub:
        _print_json(hub.stats())
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    try:
        with _make_client(args) as hub:
            payload = hub.health()
    except Exception as exc:  # noqa: BLE001 - health check reports any failure
        print(f"unreachable: {exc}", file=sys.stderr)
        return EXIT_ERROR
    _print_json(payload)
    return EXIT_OK


# --- init ---------------------------------------------------------------
#
# Everything below wires up a repo so agents pick up a hub with zero manual
# copy-pasting from docs: an .mcp.json entry, lifecycle hooks, and the
# CLAUDE.md section that tells an agent when to use them. Every writer here
# merges into whatever is already on disk and is safe to run more than once.

_LOCAL_HUB_URLS = {"http://127.0.0.1:8787", "http://localhost:8787"}

#: The managed hub `switchboard init` points at by default. Self-host instead
#: with `switchboard init --local` (or any other `--url`).
MANAGED_HUB_URL = "https://switchboard.lucille-ai.com"

def _hook_env_prefix(url: str, workspace: str) -> str:
    """`SessionStart`/`Stop` hooks run as plain shell commands, not inside the
    `switchboard-mcp` subprocess — `.mcp.json`'s `env` block never reaches
    them, so `SWITCHBOARD_URL`/`SWITCHBOARD_WORKSPACE` may not be ambient
    there even when a token is (see #32). Neither is secret, and both are
    already known at `init` time, so export them explicitly rather than
    relying on an environment the hook doesn't actually share. `export`
    rather than a simple-command prefix so it also reaches the commands on
    the far side of `|` and the `subprocess.run` calls made by the nested
    `python -c` below — a plain `VAR=val cmd` prefix would not."""
    return (
        f"export SWITCHBOARD_URL={shlex.quote(url)}; "
        f"export SWITCHBOARD_WORKSPACE={shlex.quote(workspace)}; "
    )


def _session_start_cmd(url: str, workspace: str) -> str:
    return f"{_hook_env_prefix(url, workspace)}switchboard -q register -c build || true"


def _stop_cmd(url: str, workspace: str) -> str:
    return (
        f"{_hook_env_prefix(url, workspace)}"
        'switchboard --json claims --holder "$(switchboard --json whoami | '
        "python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" | "
        "python -c 'import sys,json,subprocess;"
        "[subprocess.run([\"switchboard\",\"-q\",\"release\",l[\"resource\"]]) "
        "for l in json.load(sys.stdin)]' "
        "|| true"
    )


#: Every hook command `init` has ever generated, oldest first, excluding the
#: current one — extracted verbatim from git history, not retyped, so a repo
#: whose hooks are untouched output from a past `init` run (rather than
#: hand-edited) gets recognized and safely upgraded on the next run instead
#: of being left on a stale — possibly buggy, see #32 — revision forever.
_SESSION_START_CMD_HISTORY: list[Callable[[str, str], str]] = [
    lambda url, workspace: "switchboard register --quiet -c build || true",
    lambda url, workspace: "switchboard -q register -c build || true",
]
_STOP_CMD_HISTORY: list[Callable[[str, str], str]] = [
    lambda url, workspace: (
        'switchboard claims --holder "$(switchboard whoami --json | '
        "python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" --json | "
        "python -c 'import sys,json,subprocess;"
        "[subprocess.run([\"switchboard\",\"release\",l[\"resource\"],\"--quiet\"]) "
        "for l in json.load(sys.stdin)]' "
        "|| true"
    ),
    lambda url, workspace: (
        'switchboard --json claims --holder "$(switchboard --json whoami | '
        "python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" | "
        "python -c 'import sys,json,subprocess;"
        "[subprocess.run([\"switchboard\",\"-q\",\"release\",l[\"resource\"]]) "
        "for l in json.load(sys.stdin)]' "
        "|| true"
    ),
]


def _revision_status(existing: str, current: str, history: Sequence[str]) -> str:
    """Classify existing generated content against the current canonical
    version and every past revision it might be untouched output from.

    "current" — nothing to do. "stale" — matches a past revision exactly, so
    it's machine output we can safely replace. "unrecognized" — matches
    nothing we ever generated, so it's probably hand-edited; leave it alone
    unless told to --force."""
    if existing == current:
        return "current"
    if existing in history:
        return "stale"
    return "unrecognized"


_CLAUDE_MD_MARKER = "## Coordinating with other agents"
_CLAUDE_MD_SECTION = f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
"""


#: Every _CLAUDE_MD_SECTION `init` has ever generated, oldest first,
#: excluding the current one — extracted verbatim from git history, not
#: retyped, for the same reason as the hook-command history above: a repo
#: whose CLAUDE.md section is untouched output from a past `init` run
#: gets recognized and upgraded automatically instead of stuck forever.
_CLAUDE_MD_SECTION_HISTORY: list[str] = [
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive and hands you anything other agents have said. If you stop calling it,
  your claims expire and free themselves — which is correct if you have
  crashed and wrong if you are still working.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message. Messages are for signals; the blackboard is
  for payloads.

Switchboard is ephemeral by design. Anything that should outlive the work
still belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message. Messages are for signals; the blackboard is
  for payloads.

Switchboard is ephemeral by design. Anything that should outlive the work
still belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
- **If you are ending a turn while still waiting on another agent**, and your
  environment can schedule a future wake-up, use it to check back rather than
  letting the wait go unbounded — a short interval if you are waiting on one
  specific reply, longer for a general "check in later." `unread_dms` only
  helps while you are still making tool calls; it does nothing once you have
  gone idle, and nothing else will interrupt you. When the wake-up fires,
  `checkin` tells you whether anything changed.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message. Messages are for signals; the blackboard is for
  payloads.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
""",
    f"""{_CLAUDE_MD_MARKER}

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
""",
]


def _git_remote_workspace(directory: Path) -> str | None:
    """Guess `org/repo` from the git remote, or None if there isn't one."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    match = re.search(r"[:/]([^/:]+/[^/:]+?)(\.git)?$", url)
    return match.group(1) if match else None


def _default_workspace(directory: Path) -> str:
    return _git_remote_workspace(directory) or directory.resolve().name


def _init_token(directory: Path, token: str | None) -> tuple[str, str]:
    """Resolve a dev token for a local hub, generating and persisting one if needed."""
    if token:
        return token, "using the provided token"
    env_token = os.environ.get("SWITCHBOARD_TOKEN")
    if env_token:
        return env_token, "using SWITCHBOARD_TOKEN from your environment"
    env_path = directory / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("SWITCHBOARD_TOKEN="):
                return line.split("=", 1)[1].strip(), "reused the token already in .env"
    generated = secrets.token_urlsafe(32)
    with env_path.open("a") as f:
        f.write(f"SWITCHBOARD_TOKEN={generated}\n")
    return generated, "generated a dev token, saved to .env (already gitignored)"


def _init_mcp_json(directory: Path, url: str, workspace: str) -> str:
    path = directory / ".mcp.json"
    entry = {
        "command": "switchboard-mcp",
        "env": {"SWITCHBOARD_URL": url, "SWITCHBOARD_WORKSPACE": workspace},
    }
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            return f"left .mcp.json alone: existing file is not valid JSON ({exc})"
        servers = data.setdefault("mcpServers", {})
        if "switchboard" in servers:
            return 'left .mcp.json alone: a "switchboard" server is already registered'
        servers["switchboard"] = entry
    else:
        data = {"mcpServers": {"switchboard": entry}}
    path.write_text(json.dumps(data, indent=2) + "\n")
    return "wrote .mcp.json"


def _sync_hook(
    data: dict[str, Any],
    event: str,
    current_cmd: str,
    history: Sequence[Callable[[str, str], str]],
    url: str,
    workspace: str,
    force: bool,
) -> str:
    """Add, update, or leave alone the switchboard command for one hook
    event. An existing command gets replaced only if it's untouched output
    from a past `init` run (matches `current_cmd` or a rendering of a past
    revision from `history`) or `--force` was passed — anything else is
    presumed hand-edited and left alone."""
    entries = data.setdefault("hooks", {}).setdefault(event, [])
    for group in entries:
        for h in group.get("hooks", []):
            cmd = h.get("command", "")
            if "switchboard" not in cmd:
                continue
            if cmd == current_cmd:
                return f"left the {event} hook alone: already up to date"
            historical = {template(url, workspace) for template in history}
            if force or cmd in historical:
                h["command"] = current_cmd
                return f"updated the {event} hook to the latest revision"
            return (
                f"left the {event} hook alone: it doesn't match a known "
                "switchboard revision (looks hand-edited) — pass --force to overwrite anyway"
            )
    entries.append({"hooks": [{"type": "command", "command": current_cmd}]})
    return f"added the {event} hook"


def _init_claude_settings(
    directory: Path, url: str, workspace: str, *, force: bool = False
) -> list[str]:
    path = directory / ".claude" / "settings.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            return [f"left .claude/settings.json alone: existing file is not valid JSON ({exc})"]
    else:
        data = {}
    steps = [
        _sync_hook(
            data, "SessionStart", _session_start_cmd(url, workspace),
            _SESSION_START_CMD_HISTORY, url, workspace, force,
        ),
        _sync_hook(
            data, "Stop", _stop_cmd(url, workspace),
            _STOP_CMD_HISTORY, url, workspace, force,
        ),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return steps


#: Where `init` puts the workspace key. Deliberately not `.mcp.json`: that
#: file has to be committed for a cloud session or CI runner to find the MCP
#: server at all, and a key committed to git is not a key. Claude Code applies
#: this file's `env` block to the session *and* to the subprocesses it spawns,
#: which is what gets `SWITCHBOARD_KEY` to `switchboard-mcp` without anyone
#: having to `export` it by hand. `.env` would stay out of git just as well
#: but nothing loads it into that subprocess — it works for the local dev
#: token only because `docker compose` reads `.env` itself.
_LOCAL_SETTINGS_REL = ".claude/settings.local.json"
_GITIGNORE_PATTERN = "**/.claude/settings.local.json"


def _ensure_gitignored(directory: Path) -> str | None:
    """Make sure the local settings file is ignored, returning a step to
    report if we had to change something.

    Claude Code adds this pattern to your *global* git excludes, but only for
    a file it wrote itself. `init` writes this one by hand, so that safety net
    does not apply and we have to place the entry ourselves. Getting this
    wrong means writing a secret into a file the user then commits, so it is
    checked on every run rather than only at creation.
    """
    path = directory / ".gitignore"
    existing = path.read_text() if path.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    if lines & {_GITIGNORE_PATTERN, ".claude/settings.local.json", ".claude/"}:
        return None
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with path.open("a") as f:
        f.write(f"{prefix}{_GITIGNORE_PATTERN}\n")
    return f"added {_GITIGNORE_PATTERN} to .gitignore"


def _init_key(directory: Path, key: str, *, force: bool) -> tuple[list[str], bool]:
    """Record the workspace key so agents in this repo pick it up.

    Refuses to replace a *different* key already on disk without --force.
    Silently swapping one is the worst available failure: nothing errors, but
    this agent and the ones still holding the old key seal to different keys
    and simply never see each other's messages again.
    """
    steps: list[str] = []
    ignored = _ensure_gitignored(directory)
    if ignored:
        steps.append(ignored)

    path = directory / _LOCAL_SETTINGS_REL
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            steps.append(f"left {_LOCAL_SETTINGS_REL} alone: not valid JSON ({exc})")
            return steps, False
    else:
        data = {}
    env = data.setdefault("env", {})
    existing = env.get("SWITCHBOARD_KEY")
    if existing == key:
        steps.append(f"left {_LOCAL_SETTINGS_REL} alone: this key is already set")
        return steps, True
    if existing and not force:
        steps.append(
            f"left {_LOCAL_SETTINGS_REL} alone: a different SWITCHBOARD_KEY is "
            "already set. Replacing it silently would cut this agent off from "
            "everyone still using the old one — pass --force if that is what you want"
        )
        return steps, False
    env["SWITCHBOARD_KEY"] = key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    steps.append(
        f"{'replaced the key in' if existing else 'wrote the workspace key to'} "
        f"{_LOCAL_SETTINGS_REL}"
    )
    return steps, True


_SKILL_NAME = "switchboard-coordinate"

#: Every SKILL.md `init` has ever installed, oldest first, excluding the
#: current one. Empty today — the skill was only just introduced — but this
#: is where a future content revision gets recorded, the same as the two
#: history lists above.
_SKILL_HISTORY: list[str] = []


def _skill_source() -> str:
    """The packaged skill content — the single source `init` installs from
    and `docs/claude-code.md`/`docs/codex-cli.md` link to, so there is only
    ever one copy of this protocol to drift out of sync."""
    return (
        resources.files("switchboard")
        .joinpath("skill", _SKILL_NAME, "SKILL.md")
        .read_text(encoding="utf-8")
    )


def _init_skill(directory: Path, *, force: bool = False) -> str:
    path = directory / ".claude" / "skills" / _SKILL_NAME / "SKILL.md"
    current = _skill_source()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(current)
        return f"installed the {_SKILL_NAME} skill"
    label = path.relative_to(directory)
    status = _revision_status(path.read_text(), current, _SKILL_HISTORY)
    if status == "current":
        return f"left {label} alone: already up to date"
    if status == "stale" or force:
        path.write_text(current)
        return f"updated the {_SKILL_NAME} skill to the latest revision"
    return (
        f"left {label} alone: it doesn't match a known switchboard revision "
        "(looks hand-edited) — pass --force to overwrite anyway"
    )


def _init_claude_md(directory: Path, *, force: bool = False) -> str:
    path = directory / "CLAUDE.md"
    if not path.exists():
        path.write_text("# CLAUDE.md\n\n" + _CLAUDE_MD_SECTION)
        return "wrote CLAUDE.md"
    text = path.read_text()
    marker_at = text.find(_CLAUDE_MD_MARKER)
    if marker_at == -1:
        sep = "\n" if text.endswith("\n\n") else ("\n\n" if text.endswith("\n") else "\n\n\n")
        path.write_text(text + sep + _CLAUDE_MD_SECTION)
        return "appended a coordination section to CLAUDE.md"
    existing = text[marker_at:]
    status = _revision_status(existing, _CLAUDE_MD_SECTION, _CLAUDE_MD_SECTION_HISTORY)
    if status == "current":
        return "left CLAUDE.md alone: coordination section is already up to date"
    if status == "stale" or force:
        path.write_text(text[:marker_at] + _CLAUDE_MD_SECTION)
        return "updated CLAUDE.md's coordination section to the latest revision"
    return (
        "left CLAUDE.md alone: its coordination section doesn't match a known "
        "switchboard revision (looks hand-edited) — pass --force to overwrite anyway"
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Wire up a repo end to end: .mcp.json, lifecycle hooks, CLAUDE.md, the
    coordination skill, a dev token."""
    arg_url = getattr(args, "init_url", None) or args.url
    quiet = getattr(args, "init_quiet", False) or args.quiet
    as_json = getattr(args, "init_json", False) or args.json
    arg_token = getattr(args, "init_token", None) or args.token
    if args.local and arg_url:
        print("error: --local and --url are mutually exclusive — pick one.", file=sys.stderr)
        return EXIT_ERROR

    key = getattr(args, "init_key", None) or args.key or os.environ.get("SWITCHBOARD_KEY")
    if args.new_key and key:
        print(
            "error: --new-key and --key are mutually exclusive — mint a key or adopt "
            "one, not both.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    directory = Path(args.dir or ".").resolve()
    arg_workspace = getattr(args, "init_workspace", None) or args.workspace
    explicit_workspace = bool(arg_workspace or os.environ.get("SWITCHBOARD_WORKSPACE"))
    workspace = arg_workspace or os.environ.get("SWITCHBOARD_WORKSPACE") or _default_workspace(
        directory
    )
    minted: str | None = None
    if args.new_key:
        minted = key = generate_key()
        # Pair it with an opaque name, for the reason `keygen` explains: the
        # workspace is the one thing the hub always sees in the clear, and a
        # repo slug tells an operator more than anything else we hand over.
        if not explicit_workspace:
            workspace = "w_" + generate_key()[:16]
    if args.local:
        url = "http://127.0.0.1:8787"
    else:
        url = (arg_url or os.environ.get("SWITCHBOARD_URL") or MANAGED_HUB_URL).rstrip("/")
    local_hub = url in _LOCAL_HUB_URLS
    managed_hub = url == MANAGED_HUB_URL and not local_hub

    steps: list[str] = []
    token: str | None = None
    if local_hub:
        token, msg = _init_token(directory, arg_token)
        steps.append(msg)
    if not args.skip_mcp:
        steps.append(_init_mcp_json(directory, url, workspace))
    if not args.skip_hooks:
        steps.extend(_init_claude_settings(directory, url, workspace, force=args.force))
    if not args.skip_claude_md:
        steps.append(_init_claude_md(directory, force=args.force))
    if not args.skip_skill:
        steps.append(_init_skill(directory, force=args.force))
    key_ok = True
    if key:
        key_steps, key_ok = _init_key(directory, key, force=args.force)
        steps.extend(key_steps)

    local_hub_note = (
        "this hub is only reachable from this machine — a cloud session or CI runner "
        "pointed at it would start its own separate, empty hub and never see agents "
        "here. To coordinate across machines, deploy one shared hub (see "
        "docs/deployment.md) and re-run `switchboard init --url https://your-hub` so "
        "that URL is what gets committed, or self-host locally with `--local`."
    )
    managed_hub_note = (
        f"{MANAGED_HUB_URL} is a shared public hub with one token everyone uses — "
        "that's what makes it zero-setup, and as set up here it means every other "
        "user of the default hub can read and post to your workspace. Fine for "
        "trying things out; not yet a private space.\n"
        "  To make it private without leaving the hub, give your team a key: "
        "re-run `switchboard init --new-key`. It mints one, pairs it with an "
        "opaque workspace name, and prints the command your teammates run to "
        "adopt it. Bodies, board values, lease notes and branch names are then sealed before "
        "they leave your machine and the key never reaches the hub, so nobody else "
        "on it can read your workspace or guess its name.\n"
        "  What a key does not hide is metadata — the hub still sees message "
        "timing, volume, and how many agents you run. If that matters, self-host: "
        "`switchboard init --local` (or `--url` to point at a hub you already "
        "deployed)."
    )
    sealed_note = (
        f"this workspace is sealed with a key held only on this machine, so "
        f"other users of {MANAGED_HUB_URL} cannot read it. The hub still sees "
        "message timing, volume, and how many agents you run — a key hides "
        "content, not metadata. Self-host if that matters.\n"
        f"  Every agent that should see this workspace needs the same key and "
        f"the same workspace name ({workspace}). They do not have to be on this "
        "machine — run `switchboard init --key <key> -w " + workspace + "` in "
        "each place, or set SWITCHBOARD_KEY and SWITCHBOARD_WORKSPACE in a "
        "cloud environment's config."
    )
    if key and key_ok and managed_hub:
        managed_hub_note = sealed_note
    # A key without a matching workspace name is the quiet failure: sealing
    # works, routing does not, and the two agents just never meet.
    if key and not minted and not explicit_workspace:
        steps.append(
            f"note: workspace defaulted to {workspace!r}. A key only puts you in "
            "the same room as whoever gave it to you if the workspace name "
            "matches theirs too — pass -w if it does not"
        )

    if as_json:
        payload: dict[str, Any] = {"workspace": workspace, "url": url, "steps": steps}
        if local_hub:
            payload["note"] = local_hub_note
        elif managed_hub:
            payload["note"] = managed_hub_note
        if minted and key_ok:
            payload["key"] = minted
        _print_json(payload)
        return EXIT_OK if key_ok else EXIT_ERROR

    fmt = Fmt(_use_color(sys.stdout))
    if not quiet:
        print(fmt.bold(f"switchboard init — workspace {fmt.cyan(workspace)}, hub {url}"))
        for step in steps:
            if step.startswith("left "):
                marker = fmt.dim("·")
            elif step.startswith("note:"):
                marker = fmt.yellow("!")
            else:
                marker = fmt.green("+")
            print(f"  {marker} {step}")
        if local_hub:
            print()
            print(fmt.yellow("Note: ") + local_hub_note)
        elif managed_hub:
            print()
            print(fmt.yellow("Note: ") + managed_hub_note)
        print()
        print(fmt.bold("Next"))
        n = 1
        if local_hub:
            print(f"  {n}. start the hub — either:")
            print("       docker compose up -d          # reads the token from .env")
            print("     or:")
            print(f"       export SWITCHBOARD_TOKEN={token}")
            print("       switchboard serve")
            n += 1
        print(f"  {n}. export SWITCHBOARD_TOKEN={token or '<token>'} in every agent's shell")
        n += 1
        print(f"  {n}. restart Claude Code and check `/mcp` — switchboard should show connected")
        if minted and key_ok:
            # Printed once, and never again: it is on disk but `init` will not
            # read it back out to show you, and the hub cannot recover it.
            print()
            print(fmt.bold("Your new workspace key — copy it now, it is not shown again:"))
            print(f"  {minted}")
            print(
                f"  Give teammates: switchboard init --key {minted} -w {workspace}"
            )
    return EXIT_OK if key_ok else EXIT_ERROR


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchboard",
        description="Ephemeral orchestration hub for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"switchboard {__version__}")
    parser.add_argument("--url", help="hub base URL (env: SWITCHBOARD_URL)")
    parser.add_argument("--token", help="bearer token (env: SWITCHBOARD_TOKEN)")
    parser.add_argument("-w", "--workspace", help="workspace (env: SWITCHBOARD_WORKSPACE)")
    parser.add_argument("--agent-id", help="override this agent's id")
    parser.add_argument(
        "--key",
        help="workspace key for end-to-end encryption (env: SWITCHBOARD_KEY). "
             "Never sent to the hub; generate one with `switchboard keygen`.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress success chatter")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run a hub")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--db", help="SQLite path (env: SWITCHBOARD_DB)")
    p.add_argument("--log-level", default="info")
    p.add_argument(
        "--keys-file",
        help="JSON file of scoped keys for a multi-tenant hub (env: SWITCHBOARD_KEYS_FILE). "
             "Mutually exclusive with --token/SWITCHBOARD_TOKEN — see config.py's module "
             "docstring for the file format.",
    )
    p.add_argument(
        "--self-issued-keys",
        action="store_true",
        help="multi-tenant without a curated file (env: SWITCHBOARD_SELF_ISSUED_KEYS=1) — "
             "clients register their own key with `switchboard register-key`, scoped to "
             "one workspace on a first-claim-wins basis. Mutually exclusive with --token "
             "and --keys-file.",
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "init",
        help="wire up this repo: .mcp.json, lifecycle hooks, CLAUDE.md, "
             "the coordination skill, a dev token",
    )
    p.add_argument("--dir", help="target repo directory (default: current directory)")
    p.add_argument(
        "--local",
        action="store_true",
        help="self-host instead of using the managed hub: point .mcp.json at "
             "http://127.0.0.1:8787 and generate a dev token. Shorthand for "
             "--url http://127.0.0.1:8787. Mutually exclusive with --url.",
    )
    # `init` accepts the connection options after the subcommand as well as
    # before it. They are global options, so `switchboard init -w foo` was an
    # "unrecognized arguments" error — including in the two places init's own
    # output tells you to type it. Separate dests so that the parent form
    # (`switchboard -w foo init`) keeps working: a subparser option sharing a
    # dest overwrites the parent's value with its own default whenever the
    # flag is not repeated after the subcommand. cmd_init reads both.
    p.add_argument("-w", "--workspace", dest="init_workspace", help=argparse.SUPPRESS)
    p.add_argument("--url", dest="init_url", help=argparse.SUPPRESS)
    p.add_argument("--token", dest="init_token", help=argparse.SUPPRESS)
    p.add_argument("-q", "--quiet", dest="init_quiet", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--json", dest="init_json", action="store_true", help=argparse.SUPPRESS)
    # A separate dest from the global --key so that `switchboard --key K init`
    # keeps working: a subparser option sharing a dest overwrites the parent's
    # value with its own default when the flag is not repeated after the
    # subcommand. cmd_init reads both.
    p.add_argument(
        "--key", dest="init_key",
        help="adopt an existing workspace key (env: SWITCHBOARD_KEY) — the one a "
             "teammate gave you. Written to .claude/settings.local.json, which is "
             "kept out of git. Pair it with the same -w workspace they use, or you "
             "will be sealed correctly and routed somewhere else.",
    )
    p.add_argument(
        "--new-key", action="store_true",
        help="mint a fresh workspace key and an opaque workspace name to go with "
             "it, then print the key once so you can share it. Use this on the "
             "first machine only; every other machine adopts it with --key. "
             "Mutually exclusive with --key.",
    )
    p.add_argument("--skip-mcp", action="store_true", help="do not write .mcp.json")
    p.add_argument(
        "--skip-hooks", action="store_true", help="do not write .claude/settings.json hooks"
    )
    p.add_argument("--skip-claude-md", action="store_true", help="do not touch CLAUDE.md")
    p.add_argument(
        "--skip-skill", action="store_true",
        help="do not install the switchboard-coordinate skill",
    )
    p.add_argument(
        "--force", action="store_true",
        help="overwrite CLAUDE.md's coordination section, the skill file, and the "
             "SessionStart/Stop hooks even if they don't match a known switchboard "
             "revision. Without this, anything that looks hand-edited — not just "
             "untouched output from a past `init` run — is left alone.",
    )
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("whoami", help="show this agent's inferred identity")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser(
        "keygen", help="generate a workspace key for end-to-end encryption")
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser(
        "register-key",
        help="generate an auth key and bind it to a workspace (hubs running "
             "--self-issued-keys only)",
    )
    p.set_defaults(func=cmd_register_key)

    p = sub.add_parser("health", help="check the hub is reachable")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("stats", help="hub-wide counts")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("register", help="announce this agent to the hub")
    p.add_argument("--name")
    p.add_argument("--kind", choices=["local", "cloud", "ci", "unknown"])
    p.add_argument("--task", help="what this agent is working on")
    p.add_argument("-c", "--channel", action="append", help="subscribe (repeatable)")
    p.add_argument("--ttl", type=float)
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("agents", help="who else is awake")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("claim", help="take an exclusive lease on a resource")
    p.add_argument("resource")
    p.add_argument("-m", "--note", help="why you want it")
    p.add_argument("--ttl", type=float, help="seconds (default 900)")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="drop a lease")
    p.add_argument("resource")
    p.add_argument("--force", action="store_true", help="release even if held by another agent")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("claims", help="list live leases")
    p.add_argument("--holder", help="filter by holding agent")
    p.set_defaults(func=cmd_claims)

    p = sub.add_parser("say", help="post to a channel")
    p.add_argument("channel")
    p.add_argument("message", nargs="*", help="text, or - to read stdin")
    p.add_argument("--type", default="note")
    p.add_argument("--thread")
    p.add_argument("--ttl", type=float)
    p.add_argument("--json-body", action="store_true", help="parse the message as JSON")
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("dm", help="message one agent directly")
    p.add_argument("to")
    p.add_argument("message", nargs="*", help="text, or - to read stdin")
    p.add_argument("--type", default="note")
    p.add_argument("--thread")
    p.add_argument("--ttl", type=float)
    p.add_argument("--json-body", action="store_true")
    p.set_defaults(func=cmd_dm)

    p = sub.add_parser("inbox", help="drain new messages for this agent")
    p.add_argument("-c", "--channel", action="append", help="override subscriptions")
    p.add_argument("--wait", type=float, default=0.0, help="long-poll up to N seconds")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--peek", action="store_true", help="do not advance the read cursor")
    p.add_argument("--include-own", action="store_true")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("watch", help="follow messages until interrupted")
    p.add_argument("-c", "--channel", action="append")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("history", help="recent messages on a channel (cursor untouched)")
    p.add_argument("channel")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("channels", help="list active channels")
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("checkin", help="heartbeat + renew leases + drain inbox")
    p.add_argument("--task")
    p.add_argument("--ttl", type=float)
    p.add_argument("--wait", type=float, default=0.0)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_checkin)

    p = sub.add_parser("board", help="shared key/value scratch space")
    bsub = p.add_subparsers(dest="board_action", required=True)
    # Named "board_key" (displayed as "key" via metavar): the implicit dest
    # from a positional literally named "key" would collide with the global
    # --key (workspace encryption key) on the same Namespace, letting a board
    # entry name silently overwrite/be mistaken for the encryption key — see
    # issue #16. argparse has no dest= override for positionals, so the
    # argument itself has to be renamed.
    b = bsub.add_parser("set")
    b.add_argument("board_key", metavar="key")
    b.add_argument("value")
    b.add_argument("--ttl", type=float)
    b.add_argument("--if-revision", type=int, help="optimistic concurrency; 0 means 'if absent'")
    b.add_argument("--json-body", action="store_true")
    b = bsub.add_parser("get")
    b.add_argument("board_key", metavar="key")
    b = bsub.add_parser("list")
    b.add_argument("--prefix")
    b = bsub.add_parser("delete")
    b.add_argument("board_key", metavar="key")
    p.set_defaults(func=cmd_board)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LeaseHeld as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return EXIT_CONFLICT
    except CryptoError as exc:
        print(f"encryption error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SwitchboardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_OK
    except OSError as exc:
        print(f"cannot reach hub: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
