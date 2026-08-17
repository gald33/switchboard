#!/usr/bin/env python3
"""A read-only window on a room, for the human the agents are working for.

    cd your-repo          # one `switchboard init` has been run in
    switchboard-viewer    # → http://127.0.0.1:8799

Configuration comes from the checkout you are standing in, the way the CLI
resolves it, with the environment winning over it — see `_provenance` and
`ClientConfig.from_repo`. Anywhere without a checkout, export
SWITCHBOARD_URL / _WORKSPACE / _TOKEN / _KEY instead.

Who is awake and on what, what is claimed and for how long, what is on the
blackboard, and the conversation as it happens.

**This is an application built on the SDK, not part of it**, and it ships as
its own distribution to keep that true. `examples/coordinated_worker.py` shows
an agent *taking part* in a room; this shows a program *reading* one — the
other half of the surface, and the half nothing else exercised. It imports
only what `switchboard` exports, and because CI installs it against the
`agent-switchboard` on PyPI rather than the one in this checkout, a private
name — or one that exists on `main` but in no release — is a broken install
rather than a code-review question. See the `viewer-addon` job in
`.github/workflows/ci.yml`. Four such holes turned up
while writing it and were closed: `read_channels()`,
`Client.encrypted`, messages marked `unreadable` rather than arriving as raw
envelopes, and `ClientConfig.from_repo` — the resolution that made "stand in
the repo and run it" the whole setup. See `docs/viewer.md`.

Three things about the shape of it, because they are not arbitrary.

**It runs beside the human, not on the hub.** A hub cannot serve this page.
Message bodies, agent names, lease notes and board values are sealed with a
key the hub never receives, so a page it rendered would be a wall of
ciphertext. The viewer is an ordinary client, configured exactly like the
agents it is watching, doing the decryption in the one place it can happen.
That is also why the page is bound to loopback by default: it shows
plaintext, and the hub's own perimeter protects nothing here.

**It reads without disturbing.** Every call it makes is a read that leaves no
trace an agent could trip over: `agents()`, `leases()`, `board_list()`,
`channels()`, and `read_channels()` — which reads from the beginning with
every cursor left where it was. The viewer never registers, never heartbeats, and never
posts, so it does not appear in the roster it is displaying and cannot make an
agent's next `inbox` come back empty.

**It shows what the key it holds can open, and says so when it can't.** An
encrypted room hides channel names, lease resources and board keys from the
hub by blinding them, and blinding is one-way — nobody can reverse it, this
viewer included. Channel names and board keys come back anyway, because a
sealed body and a sealed board value each carry their own label. Lease
resources have no such carrier, so they are shown as the tokens they are,
marked sealed rather than dressed up as names. A room using a different key
than this viewer holds is
reported as unreadable, in place, instead of failing the whole page.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlsplit

import httpx

from switchboard import (
    Client,
    ClientConfig,
    CryptoError,
    SwitchboardError,
    __version__,
    rooms_in,
    unwrap_forecast,
)
from switchboard.invite import PROBE_SENTINEL, Invite, InviteError

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "snapshot", "page", "make_server", "serve"]

#: The page, and the modules it loads. Shared with the static build in
#: `switchboard_viewer/web/`: one renderer painting one state shape, whether the reading
#: was done here in Python or in the browser. Two pages would drift, and the
#: one that drifts is always the one nobody is looking at.
_WEB = Path(__file__).with_name("web")
_PAGE_FILE = _WEB / "index.html"
_MODULES = {"/render.js", "/switchboard-room.js", "/switchboard-open.js"}

DEFAULT_HOST = "127.0.0.1"

#: Deliberately not 8787 (the hub's default). Both run on a laptop at once and
#: the viewer is the one that should move.
DEFAULT_PORT = 8799

#: Messages fetched per channel. The hub caps `limit` at 500.
DEFAULT_LIMIT = 50

#: How often the page asks for a fresh snapshot.
DEFAULT_REFRESH = 3.0

#: Channels read per snapshot. They come back in one request, but `limit`
#: applies per channel, so an unbounded room would still pull an unbounded
#: number of messages. Truncation is reported in the payload rather than
#: passed off as the whole room.
MAX_CHANNELS = 60


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _channel_label(token: str, opened: list[dict[str, Any]]) -> str:
    """The readable name for a channel, or the token if nothing recovered one.

    A sealed message body carries `ch`, the plaintext channel name, which the
    client restores while opening it. So a channel with any readable message
    in it names itself. An empty-but-listed channel, or one sealed under
    another key, has nothing to name it with.
    """
    for m in opened:
        name = m.get("channel")
        if isinstance(name, str) and name and name != token:
            return name
    return token


def _who(agent_id: str | None, names: dict[str, str]) -> dict[str, Any]:
    """A sender or holder as the page wants it: id, plus a name if we know one.

    Under encryption an agent id is blinded and unreadable, while the name it
    registered is sealed and therefore *is* readable — so the roster is the
    only thing that turns `Yk3n…` back into `my-repo:feat/x`.
    """
    return {"id": agent_id or "", "name": names.get(agent_id or "")}


_WRONG_ROOM = (
    "WRONG ROOM: reached this hub and workspace, but could not read the proof "
    "the inviter left. Your key does not match theirs — you would appear on "
    "each other's roster and be unable to exchange anything. Ask for a fresh "
    "invite rather than editing settings"
)


def _probe_verdict(probe: str, board: list[dict[str, Any]]) -> str | None:
    """What an invite's proof-of-room says, or None when there is nothing to
    say.

    Only failures. Opening a value the inviter sealed proves the hub, the
    workspace *and* the key all match — which a roster listing you both does
    not — but announcing every success would be shouting the normal case, and
    these notes are drawn as warnings. Silence is the good outcome.

    Free: the probe is an ordinary board entry and the board has already been
    read, so checking it costs no request. The browser reader does the same
    thing in the same place.
    """
    entry = next((e for e in board if e.get("key") == probe), None)
    if entry is not None:
        return None if entry.get("value") == PROBE_SENTINEL else _WRONG_ROOM
    # Not found is not the same as absent. A board key travels sealed beside
    # its value, so a key this room cannot open never comes back as a name —
    # the inviter's probe is sitting right there under a token neither side
    # can match. An unopened entry is the answer, not a missing one.
    if any(e.get("sealed") for e in board):
        return _WRONG_ROOM
    return ("this invite carries a proof-of-room and it is not on the "
            "blackboard — it may have expired, or this may not be the room "
            "the invite described")


def snapshot(hub: Client, *, limit: int = DEFAULT_LIMIT,
             refresh: float = DEFAULT_REFRESH, probe: str = "") -> dict[str, Any]:
    """One complete view of the room, as JSON the page can render.

    Section by section, so a hub that has gone away mid-poll — or a room whose
    channels this key cannot open — degrades to a page with a note on it
    rather than a stack trace and a blank screen.
    """
    sealed_ids = hub.encrypted
    view: dict[str, Any] = {
        "generated_at": _iso(datetime.now(tz=timezone.utc).timestamp()),
        "version": __version__,
        "refresh_ms": int(max(0.5, refresh) * 1000),
        "hub": {
            "url": hub.config.url,
            "workspace": hub.workspace,
            "encrypted": sealed_ids,
            "reachable": True,
        },
        "agents": [],
        "leases": [],
        "board": [],
        "channels": [],
        "messages": [],
        "notes": [],
    }
    notes: list[str] = view["notes"]

    def section(name: str, read: Callable[[], Any]) -> Any:
        """Run one read, or turn its failure into something the page can say.

        The three failures are genuinely different and a human needs to be
        told which one happened: the hub answered "no" (a token problem), the
        hub did not answer at all (a network problem), or it answered fine and
        this key could not open the reply (a key problem). Only the middle one
        means the room is dark.
        """
        try:
            return read()
        except CryptoError as exc:
            notes.append(f"cannot open {name} with this key: {exc}")
        except SwitchboardError as exc:
            notes.append(f"the hub refused {name}: {exc}")
            if exc.status is None:
                view["hub"]["reachable"] = False
        except (OSError, httpx.HTTPError) as exc:
            notes.append(f"cannot reach the hub: {exc}")
            view["hub"]["reachable"] = False
        return None

    agents = section("the roster", hub.agents) or []
    names = {a.get("agent_id", ""): a.get("name") or "" for a in agents if a.get("name")}
    view["agents"] = [
        {
            "id": a.get("agent_id", ""),
            "name": a.get("name"),
            "kind": a.get("kind"),
            "branch": a.get("branch"),
            "task": a.get("task"),
            "channels": a.get("channels") or [],
            "last_seen_at": a.get("last_seen_at"),
            "expires_in": a.get("expires_in"),
            "stale": bool(a.get("stale")),
            "unreadable": bool(a.get("unreadable")),
        }
        for a in agents
    ]
    mismatched = [a.get("agent_id", "") for a in agents if a.get("unreadable")]
    if mismatched:
        notes.append(
            f"{len(mismatched)} agent(s) here hold a different key — "
            "you cannot read their messages and they cannot read yours"
        )

    leases = section("leases", hub.leases) or []
    view["leases"] = [
        {
            "resource": le.get("resource", ""),
            "sealed": sealed_ids,
            "holder": _who(le.get("holder"), names),
            "note": le.get("note"),
            "expires_in": le.get("expires_in"),
            "acquired_at": le.get("acquired_at"),
        }
        for le in leases
    ]

    board = section("the blackboard", hub.board_list) or []
    view["board"] = [
        {
            "key": e.get("key", ""),
            # A board key comes back readable now — it travels sealed beside
            # the value — so the lock belongs only on the ones that did not:
            # an entry written on another key, whose token is all we have.
            "sealed": sealed_ids and e.get("key") == e.get("hub_key"),
            "value": e.get("value"),
            "revision": e.get("revision"),
            "updated_by": _who(e.get("updated_by"), names),
            "updated_at": e.get("updated_at"),
            "expires_in": e.get("expires_in"),
        }
        for e in board
    ]

    if probe:
        verdict = _probe_verdict(probe, view["board"])
        if verdict:
            notes.append(verdict)

    channels = section("the channel list", hub.channels) or []
    if len(channels) > MAX_CHANNELS:
        notes.append(
            f"showing {MAX_CHANNELS} of {len(channels)} channels — "
            "the busiest by message count"
        )
        channels = sorted(channels, key=lambda c: c.get("messages", 0),
                          reverse=True)[:MAX_CHANNELS]

    # The whole conversation in one request, whatever it is spread across.
    # `channels()` names these the way the hub does — blinded tokens in an
    # encrypted room — and `read_channels` is the reader that takes them in
    # that form, leaves every cursor where it found it, and marks what this
    # key cannot open rather than failing the lot.
    tokens = [entry.get("channel", "") for entry in channels]
    opened = section("the conversation", lambda: hub.read_channels(tokens, limit=limit)) or []

    #: Set when this viewer holds no key but the room is plainly encrypted —
    #: the tier gap `whoami` warns about, met from the reading side.
    keyless = False
    by_token: dict[str, list[dict[str, Any]]] = {token: [] for token in tokens}
    for m in opened:
        by_token.setdefault(m.get("hub_channel", ""), []).append(m)

    messages: list[dict[str, Any]] = []
    for entry in channels:
        token = entry.get("channel", "")
        here = by_token.get(token, [])
        label = _channel_label(token, here)
        unreadable = bool(here) and all(m.get("unreadable") for m in here)
        view["channels"].append({
            "token": token,
            "name": label,
            "named": label != token,
            "dm": label.startswith("@"),
            "count": entry.get("messages", 0),
            "latest_at": _iso(entry.get("latest_at")),
            "unreadable": unreadable,
        })
        for m in here:
            body, forecast = unwrap_forecast(m.get("body"))
            if m.get("unreadable"):
                # Sealed under a key this viewer does not hold — either a peer
                # who disagrees with us, or us holding no key at all. Rendering
                # it as an empty message is the one dishonest thing this page
                # could do, so it does not.
                keyless = keyless or not sealed_ids
                body, forecast = None, None
            messages.append({
                "seq": m.get("seq", 0),
                "channel": label,
                "token": token,
                "dm": label.startswith("@"),
                "from": _who(m.get("from"), names),
                "type": m.get("type") or "note",
                "body": body,
                "sealed_body": bool(m.get("unreadable")),
                "forecast": forecast,
                "thread": m.get("thread"),
                "created_at": m.get("created_at"),
            })

    # An agent registers its subscriptions, and the roster hands them back
    # blinded — the one place a channel name is *not* carried in the clear
    # inside a body. The channels above recovered those names, so a token seen
    # there can be swapped back in here; one nobody has spoken on yet cannot,
    # and stays a token rather than being dropped.
    named = {c["token"]: c["name"] for c in view["channels"] if c["named"]}
    for agent in view["agents"]:
        agent["channels"] = [named.get(c, c) for c in agent["channels"]]

    # Both halves of "no key here": messages that came back sealed, and a
    # roster the client could not open either. The client marks both.
    if keyless or (not sealed_ids and any(a.get("unreadable") for a in agents)):
        notes.append(
            "this room is encrypted and this viewer holds no key: export "
            "SWITCHBOARD_KEY (the same one your agents use) and restart it"
        )

    if any(c["unreadable"] for c in view["channels"]):
        notes.append(
            "some channels are sealed under a different key and are listed "
            "without their messages"
        )

    # Sequence, not timestamp: seq is the hub's own total order over the
    # workspace, so a message from an agent whose clock is off still lands
    # where the hub put it.
    messages.sort(key=lambda m: m["seq"])
    view["messages"] = messages
    # A hub that has gone away fails every section with the same sentence.
    # Saying it four times reads like four problems.
    view["notes"] = list(dict.fromkeys(notes))
    return view


# --- rooms ------------------------------------------------------------------


@dataclass
class Room:
    """One room the viewer can show, and the client that reads it.

    A room per checkout, or several per checkout when a repo declares them —
    `ClientConfig.rooms_in` answers that, including the local label, which is
    the only name a human recognises. Each carries its own client because each
    may be on a different hub under a different key: that is the normal case
    once you have more than one repo, not an exotic one.
    """

    label: str
    client: Client
    source: str = ""
    #: Board key of a value an inviter sealed, when this room came from an
    #: invite. Checking it is what turns "these settings" into "the same
    #: room" — see `_probe_verdict`.
    probe: str = ""

    @property
    def id(self) -> str:
        """Stable across refreshes and unique per room, so the page can keep
        its selection when the list is rebuilt."""
        return f"{self.client.config.url}/{self.client.workspace}"


def summarise(room: Room) -> dict[str, Any]:
    """The cheap per-room line: enough to see where something is happening.

    One request, against the roster, for every room the viewer is *not*
    currently showing. The alternative — a full snapshot each — multiplies the
    cost of watching by the number of rooms, and the question a room switcher
    has to answer is only "is anyone in there".
    """
    out = {
        "id": room.id, "label": room.label, "source": room.source,
        "hub": room.client.config.url, "workspace": room.client.workspace,
        "encrypted": room.client.encrypted, "awake": None, "error": None,
    }
    try:
        agents = room.client.agents()
    except (SwitchboardError, CryptoError, OSError, httpx.HTTPError) as exc:
        out["error"] = str(exc)
        return out
    out["awake"] = sum(1 for a in agents if not a.get("stale"))
    return out


# --- the server -------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = f"switchboard-viewer/{__version__}"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._send(200, "text/html; charset=utf-8", page().encode())
        elif path in _MODULES:
            # Served rather than inlined so the local page and the static one
            # are the same bytes. `_MODULES` is a fixed set, not a directory
            # walk: this process can read the user's whole home directory and
            # has no business turning that into an HTTP surface.
            body = (_WEB / path.lstrip("/")).read_bytes()
            self._send(200, "text/javascript; charset=utf-8", body)
        elif path == "/api/state":
            wanted = parse_qs(urlsplit(self.path).query).get("room", [None])[0]
            server = self.server  # type: ignore[attr-defined]
            rooms = server.rooms
            selected = next((r for r in rooms if r.id == wanted), rooms[0])
            with server.lock:
                payload = snapshot(selected.client, limit=server.limit,
                                   refresh=server.refresh, probe=selected.probe)
                payload["rooms"] = [
                    # The selected room is already fully read; asking it for a
                    # roster a second time would be a request per refresh spent
                    # on an answer we are holding.
                    {**summarise(room), "selected": False} if room.id != selected.id
                    else {"id": room.id, "label": room.label, "source": room.source,
                          "hub": room.client.config.url, "workspace": room.client.workspace,
                          "encrypted": room.client.encrypted, "selected": True,
                          "awake": sum(1 for a in payload["agents"] if not a["stale"]),
                          "error": None}
                    for room in rooms
                ]
            body = json.dumps(payload, default=str).encode()
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # This page is a live view of a room; a cached one is a lie with a
        # timestamp on it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        """Quiet by default: one line per poll, three polls a second, is noise
        that would bury the one message worth printing — the URL to open."""
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)


class _Viewer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], rooms: list[Room], *,
                 limit: int, refresh: float, verbose: bool) -> None:
        super().__init__(address, _Handler)
        self.rooms = rooms
        self.limit = limit
        self.refresh = refresh
        self.verbose = verbose
        #: One snapshot at a time. The handler is threaded so a slow hub does
        #: not wedge the page load, but a `Client` is one connection pool and
        #: one cipher — serialising the reads is cheaper than a second client.
        self.lock = threading.Lock()


def as_rooms(hub: Client | Room | Sequence[Room]) -> list[Room]:
    """One client, one room, or several — all the same to everything below."""
    if isinstance(hub, Client):
        return [Room(label=hub.workspace, client=hub)]
    if isinstance(hub, Room):
        return [hub]
    return list(hub)


def make_server(hub: Client | Room | Sequence[Room], *, host: str = DEFAULT_HOST,
                port: int = DEFAULT_PORT, limit: int = DEFAULT_LIMIT,
                refresh: float = DEFAULT_REFRESH,
                verbose: bool = False) -> ThreadingHTTPServer:
    """A viewer bound and ready, not yet serving.

    Separate from `serve` so a caller — a test, or anything embedding this —
    can read back the port it actually got when it asked for 0.
    """
    return _Viewer((host, port), as_rooms(hub), limit=limit, refresh=refresh,
                   verbose=verbose)


def serve(hub: Client | Room | Sequence[Room], *, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT,
          limit: int = DEFAULT_LIMIT, refresh: float = DEFAULT_REFRESH,
          verbose: bool = False, open_browser: bool = False,
          announce: Callable[[str], None] | None = None) -> None:
    """Serve until interrupted."""
    server = make_server(hub, host=host, port=port, limit=limit,
                         refresh=refresh, verbose=verbose)
    shown = host if ":" not in host else f"[{host}]"
    if shown in ("0.0.0.0", "[::]", ""):  # noqa: S104 - matching, not binding
        shown = "127.0.0.1"
    url = f"http://{shown}:{server.server_address[1]}"
    if announce:
        announce(url)
    if open_browser:
        # Never fatal: a headless box has no browser, and the URL was printed.
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - platform dependent
            pass
    try:
        server.serve_forever()
    finally:
        server.server_close()


# --- the page ---------------------------------------------------------------


def page() -> str:
    """The viewer page: one packaged file, no build step, no CDN.

    Everything it needs is inline, because the machine running it may have no
    network beyond the hub — and because a coordination tool that phones a
    third party to render a page it claims cannot be read is not one.

    Read per request rather than cached at import, which costs one small file
    read on page load and makes editing it a browser refresh instead of a
    restart.

    `data-source="local"` is how the page knows the reading is already done —
    that it should ask this process for state rather than fetch and decrypt a
    hub itself. The same file with the attribute absent is the static build.
    """
    return _PAGE_FILE.read_text(encoding="utf-8").replace(
        "<html lang=\"en\">", "<html lang=\"en\" data-source=\"local\">", 1)



# --- running it -------------------------------------------------------------


def discover(paths: Sequence[str], scan: Sequence[str]) -> list[Room]:
    """Every room the checkouts you named can take part in, deduplicated.

    A room is identified by hub and workspace, so the same room reached from
    two clones appears once — under the first label that named it, which is
    the one you typed first.
    """
    found: dict[tuple[str, str], Room] = {}
    for directory in [*paths, *(d for root in scan for d in _checkouts(root))]:
        for repo_room in rooms_in(directory, include_secrets=True):
            client = Client(repo_room.config)
            room = Room(label=repo_room.label, client=client, source=repo_room.source)
            key = (client.config.url, client.workspace)
            if key in found:
                client.close()
                continue
            found[key] = room
    return list(found.values())


def _checkouts(root: str, depth: int = 3) -> list[str]:
    """Directories under `root` that have been set up to coordinate.

    Bounded rather than exhaustive: a home directory is not a search space,
    and a viewer that walks one is a viewer nobody starts twice. What it skips
    it skips for a reason that holds everywhere — dot directories and package
    trees are where checkouts are not.
    """
    skip = {"node_modules", "venv", ".venv", "__pycache__", "target", "dist", "build"}
    out, root_path = [], Path(root).expanduser()
    for candidate in [root_path, *(p for p in root_path.rglob("*") if p.is_dir())]:
        rel = candidate.relative_to(root_path).parts
        if len(rel) > depth or any(p in skip or p.startswith(".") for p in rel):
            continue
        # "Set up" means the same thing here as everywhere else: the
        # directory declares a room. No second definition to drift.
        if rooms_in(candidate):
            out.append(str(candidate))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve a read-only page showing your Switchboard rooms. "
                    "With no arguments: the room this checkout coordinates in, "
                    "resolved the way the CLI resolves it. Point it elsewhere "
                    "with --url, or at several checkouts at once with --repo.",
    )
    parser.add_argument("--url", help="hub to read, overriding whatever the "
                                      "checkout says (env: SWITCHBOARD_URL)")
    parser.add_argument("--workspace", "-w", help="room to read, overriding the checkout")
    parser.add_argument("--invite", metavar="swb1_…",
                        help="read a room somebody sent you, from the string "
                             "`switchboard invite` produced — hub, workspace, "
                             "token and key in one paste, with the proof-of-room "
                             "checked on every refresh")
    parser.add_argument("--repo", action="append", default=[], metavar="PATH",
                        help="also show the rooms this checkout takes part in; "
                             "repeatable. Defaults to the current directory")
    parser.add_argument("--scan", action="append", default=[], metavar="DIR",
                        help="also show every set-up checkout under DIR (3 levels deep)")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"interface to bind (default {DEFAULT_HOST}; the page "
                             "has no authentication, so anything else is a decision)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"default {DEFAULT_PORT}; 0 picks a free one")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"messages read per channel (default {DEFAULT_LIMIT})")
    parser.add_argument("--refresh", type=float, default=DEFAULT_REFRESH,
                        help=f"seconds between refreshes (default {DEFAULT_REFRESH:.0f})")
    parser.add_argument("--open", action="store_true", help="open a browser at it")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)

    if not _is_loopback(args.host):
        # The page is the plaintext of a room whose whole security model is
        # that the plaintext never leaves the machines holding the key. There
        # is no login on it, and there is not going to be one.
        print(
            f"warning: binding to {args.host} publishes these rooms' decrypted "
            "contents to anyone who can reach that address. This page has no "
            "authentication — use an SSH tunnel instead.",
            file=sys.stderr,
        )

    # Resolved from the directories you name, exactly as the CLI resolves the
    # one it is standing in, plus the two gitignored files `init` writes on
    # this machine — so in repos that have been set up, this is the whole
    # configuration step. `include_secrets` is the viewer saying what it is: a
    # reader, on the machine the key is already on, that never sends anything.
    if args.invite:
        # An invite is the whole configuration and outranks the checkout it is
        # pasted in, because it names a room somebody else is already in — the
        # case where reading the local repo would silently show the wrong one.
        try:
            blob = Invite.decode(args.invite)
        except InviteError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        config = ClientConfig(url=blob.url, url_source="flag",
                              workspace=blob.workspace, token=blob.token,
                              key=blob.key)
        rooms = [Room(label=blob.note or blob.workspace, client=Client(config),
                      source="invite", probe=blob.probe)]
        if not blob.probe:
            print("note: this invite carries no proof-of-room, so nothing can "
                  "check that these settings reach the room it describes",
                  file=sys.stderr)
        return _run(args, rooms)

    rooms = discover(args.repo or (["."] if not args.scan else []), args.scan)
    if not rooms and not args.repo and not args.scan:
        # Nothing declared here. Fall back to wherever an unconfigured client
        # would go — which is usually the managed hub, and which the greeting
        # says plainly rather than letting an empty page imply a quiet room.
        config = ClientConfig.from_repo(include_secrets=True)
        rooms = [Room(label=config.workspace, client=Client(config), source="default")]
    for room in rooms:
        # Flags last, as everywhere else in this project: the person typing
        # now outranks anything a file said.
        if args.url:
            room.client.config.url = args.url.rstrip("/")
            room.client.config.url_source = "flag"
        if args.workspace:
            room.client.config.workspace = args.workspace
            room.client.workspace = args.workspace
    if args.url or args.workspace:
        # Retargeting collapses distinct rooms onto one, and showing the same
        # room three times under three labels would be a worse lie than
        # showing it once under a made-up one.
        rooms = _dedupe(rooms)
    if not rooms:
        print("no rooms found: name a checkout with --repo, or set SWITCHBOARD_URL "
              "and SWITCHBOARD_WORKSPACE", file=sys.stderr)
        return 1

    return _run(args, rooms)


def _run(args: argparse.Namespace, rooms: list[Room]) -> int:
    """Serve whatever set of rooms was resolved, and close them on the way out.

    Split from `main` because an invite resolves rooms a completely different
    way — one string instead of a directory walk — and the serving half is
    identical either way.
    """
    try:
        serve(
            rooms, host=args.host, port=args.port, limit=args.limit,
            refresh=args.refresh, verbose=args.verbose, open_browser=args.open,
            announce=lambda url: print(_greeting(url, rooms), flush=True),
        )
    except OSError as exc:
        print(f"cannot serve on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        for room in rooms:
            room.client.close()
    return 0


def _dedupe(rooms: list[Room]) -> list[Room]:
    seen: dict[str, Room] = {}
    for room in rooms:
        if room.id in seen:
            room.client.close()
            continue
        seen[room.id] = room
    return list(seen.values())


def _greeting(url: str, rooms: list[Room]) -> str:
    lines = [f"switchboard viewer → {url}"]
    for room in rooms:
        lines.append(f"  {room.label}: room {room.client.workspace} "
                     f"on {room.client.config.url}")
        lines.append(f"    {_provenance(room.client.config)}")
    return "\n".join(lines)


def _provenance(config: ClientConfig) -> str:
    """One line on where this configuration came from.

    Worth printing because the failure it heads off is silent: a viewer that
    quietly fell back to the managed hub shows an empty room, which looks
    exactly like a quiet one. Naming the hub is not enough — `url_source`
    is what says whether anybody chose it.
    """
    origin = {
        "flag": "chosen here", "env": "from the environment",
        "mcp.json": "from this repo's .mcp.json", "rooms": "from this repo's rooms file",
    }.get(config.url_source, "the built-in default — nothing here named a hub")
    sealed = "with this repo's key" if config.key else "no key: sealed rooms will read as sealed"
    return f"{origin}, {sealed}"


def _is_loopback(host: str) -> bool:
    """`0.0.0.0` is deliberately not local: it is every interface, which is
    the case the warning above exists for."""
    return host.lower() in ("127.0.0.1", "localhost", "::1", "[::1]") or host.startswith("127.")


if __name__ == "__main__":
    raise SystemExit(main())
