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
import sys
import time
from typing import Any, Sequence

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
    if not config.token:
        print(
            "warning: no token set — this hub accepts any caller. "
            "Set SWITCHBOARD_TOKEN or pass --token before exposing it.",
            file=sys.stderr,
        )
    print(f"switchboard {__version__} → http://{args.host}:{args.port}  db={config.db_path}")
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level=args.log_level)
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
            entry = hub.board_set(args.key, value, ttl=args.ttl, if_revision=args.if_revision)
            if args.json:
                _print_json(entry)
            elif not args.quiet:
                print(f"{entry['key']} = rev {entry['revision']}")
        elif args.board_action == "get":
            entry = hub.board_entry(args.key)
            if entry is None:
                print(f"no entry at {args.key}", file=sys.stderr)
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
            deleted = hub.board_delete(args.key)
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
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("whoami", help="show this agent's inferred identity")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser(
        "keygen", help="generate a workspace key for end-to-end encryption")
    p.set_defaults(func=cmd_keygen)

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
    b = bsub.add_parser("set")
    b.add_argument("key")
    b.add_argument("value")
    b.add_argument("--ttl", type=float)
    b.add_argument("--if-revision", type=int, help="optimistic concurrency; 0 means 'if absent'")
    b.add_argument("--json-body", action="store_true")
    b = bsub.add_parser("get")
    b.add_argument("key")
    b = bsub.add_parser("list")
    b.add_argument("--prefix")
    b = bsub.add_parser("delete")
    b.add_argument("key")
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
