"""The rooms this machine knows, and what an agent does with the list.

A session accumulates rooms: the repo's, a room per invite it was handed, a
side room per `keygen`, another repo's. On 2026-09-03 a maintainer session
sat parked in two of them for an hour while the peer it was helping waited in
a third — a room the session had already been in and had simply stopped
watching, because nothing remembered it existed. The wrong fix is a listener
in every room. The right one is to *know* the rooms and do two different
things with the list: sweep all of them when looking for someone (a few GETs
per room, cheap), and park only where a peer is expected (the default pair
plus whatever this session used lately).

Two rules shape everything here, both decided with the owner:

**A key is stored as the way it was acquired, never as its value.** An entry
holds a *reference* the tool resolves the same way it did the first time —
the name of the environment variable that holds the key, the checkout whose
gitignored file `init` wrote it to, or the invite the key arrived as. An
invite is already in the agent's context and terminal, so keeping it exposes
nothing new; a key that never appeared in the conversation is never copied
into this file, printed, or retyped. There is no reference kind for a bare
value, on purpose.

**The mechanism is the tool's, and costs the model nothing.** Entries are
written by the commands that already hold the coordinates (`init`, `join`,
`keygen --as-invite`, any `--invite <command>`). `rendezvous` sweeps the book
by default; `listen` parks in rooms used lately without being told. The
agent learns no new habit.

Per machine, like `peers.py`: never per repo, never committed, and every
method fails soft — a read-only home directory or a corrupt file must not
take a command down with it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import rendezvous
from .config import ClientConfig, local_setting, mcp_env
from .invite import Invite, InviteError

DEFAULT_PATH = "~/.switchboard/known-rooms.json"
#: Overrides the path; set to the empty string to disable the book entirely.
ENV = "SWITCHBOARD_KNOWN_ROOMS"
#: How recently a room must have been used for `listen` to park in it
#: unasked. An hour: long enough to cover a session that was handed an
#: invite, verified it, and went quiet; short enough that last week's side
#: room is not a standing long-poll.
RECENT_SECONDS = 3600.0
#: How a room was learned. Only rooms the agent was *put in* — by an invite
#: or by minting one — count as recent for parking; a repo `init` set up is
#: that repo's business, and the lobby is derived rather than stored.
LEARNED_FOR_PARKING = ("join", "invite", "keygen")


def env_reference(var: str) -> dict[str, str]:
    return {"from": "env", "var": var}


def repo_reference(directory: Path | str) -> dict[str, str]:
    return {"from": "repo", "path": str(Path(directory).resolve())}


def invite_reference(blob: str) -> dict[str, str]:
    return {"from": "invite", "invite": blob}


NONE: dict[str, str] = {"from": "none"}


def resolve_secret(ref: dict[str, Any] | None, name: str,
                   env: dict[str, str] | None = None) -> str | None:
    """A secret from its reference — the way it was acquired, again.

    `name` is which one: `SWITCHBOARD_KEY`, `SWITCHBOARD_WRITE_KEY` or
    `SWITCHBOARD_TOKEN`. For an `env` reference the variable named is read
    (for the key; the token and write key are read from their own variables,
    since a reference records one name); a `repo` reference reads the files
    `init` writes in that checkout; an `invite` reference decodes the string.
    """
    source = os.environ if env is None else env
    if not ref:
        return None
    kind = ref.get("from")
    if kind == "env":
        var = ref.get("var") if name == "SWITCHBOARD_KEY" else name
        return source.get(str(var or name)) or None
    if kind == "repo":
        where = Path(str(ref.get("path") or ""))
        if not where.is_dir():
            return None
        if name == "SWITCHBOARD_TOKEN":
            return (local_setting(where, name) or mcp_env(where, name)
                    or _dotenv(where, name))
        return local_setting(where, name)
    if kind == "invite":
        try:
            room = Invite.decode(str(ref.get("invite") or ""))
        except InviteError:
            return None
        return {"SWITCHBOARD_KEY": room.key, "SWITCHBOARD_WRITE_KEY": room.write_key,
                "SWITCHBOARD_TOKEN": room.token}.get(name)
    return None


def _dotenv(where: Path, name: str) -> str | None:
    from .config import dotenv_setting

    return dotenv_setting(where, name)


@dataclass
class KnownRoom:
    """One room this machine can reach, as references."""

    label: str
    url: str
    workspace: str
    key: dict[str, Any] = field(default_factory=lambda: dict(NONE))
    token: dict[str, Any] = field(default_factory=lambda: dict(NONE))
    learned: str = "invite"
    first_used: float = 0.0
    last_used: float = 0.0
    note: str = ""
    #: Peers seen here, by the sealed name the roster showed — never by id,
    #: which is blinded per room and would match nothing anywhere else.
    peers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label, "url": self.url, "workspace": self.workspace,
            "key": self.key, "token": self.token, "learned": self.learned,
            "first_used": self.first_used, "last_used": self.last_used,
            "note": self.note, "peers": self.peers,
        }

    @classmethod
    def from_json(cls, raw: Any) -> KnownRoom | None:
        if not isinstance(raw, dict) or not raw.get("url") or not raw.get("workspace"):
            return None
        try:
            return cls(
                label=str(raw.get("label") or raw["workspace"]),
                url=str(raw["url"]).rstrip("/"), workspace=str(raw["workspace"]),
                key=dict(raw.get("key") or NONE), token=dict(raw.get("token") or NONE),
                learned=str(raw.get("learned") or "invite"),
                first_used=float(raw.get("first_used") or 0.0),
                last_used=float(raw.get("last_used") or 0.0),
                note=str(raw.get("note") or ""),
                peers=dict(raw.get("peers") or {}),
            )
        except (TypeError, ValueError):
            return None

    @property
    def encrypted(self) -> bool:
        return self.key.get("from") not in (None, "none")

    def config(self, env: dict[str, str] | None = None) -> ClientConfig | None:
        """A client config for this room, or None when its key cannot be found.

        None rather than a plaintext client: a room recorded as encrypted and
        opened without its key is the quiet failure the whole book exists to
        avoid — present, on a roster, reading nothing.
        """
        key = resolve_secret(self.key, "SWITCHBOARD_KEY", env)
        if self.encrypted and not key:
            return None
        return ClientConfig(
            url=self.url, url_source="known-room", workspace=self.workspace,
            token=resolve_secret(self.token, "SWITCHBOARD_TOKEN", env),
            key=key,
            write_key=resolve_secret(self.key, "SWITCHBOARD_WRITE_KEY", env),
            peer_log="",
        )


class Book:
    """The file, with every method failing soft."""

    def __init__(self, path: str | None = None) -> None:
        raw = os.environ.get(ENV) if path is None else path
        if raw is None:
            raw = DEFAULT_PATH
        self.path = Path(os.path.expanduser(raw)) if raw else None
        self._rooms: list[KnownRoom] | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def rooms(self) -> list[KnownRoom]:
        if self._rooms is None:
            self._rooms = self._load()
        return list(self._rooms)

    def _load(self) -> list[KnownRoom]:
        if self.path is None or not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        entries = data.get("rooms") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            return []
        out = []
        for raw in entries:
            room = KnownRoom.from_json(raw)
            if room is not None:
                out.append(room)
        return out

    def _save(self) -> None:
        if self.path is None or self._rooms is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"rooms": [r.as_json() for r in self._rooms]}, indent=2) + "\n")
        except OSError:
            pass

    def find(self, url: str, workspace: str) -> KnownRoom | None:
        url = url.rstrip("/")
        for room in self.rooms():
            if room.url == url and room.workspace == workspace:
                return room
        return None

    def by_label(self, label: str) -> KnownRoom | None:
        for room in self.rooms():
            if room.label == label or room.workspace == label:
                return room
        return None

    def remember(self, room: KnownRoom, *, now: float | None = None) -> KnownRoom:
        """Record a room, or refresh what is already known about it.

        Keyed by hub and workspace. The first label and `learned` stick — the
        room does not become "an invite" because it was later reached through
        one — but a reference that resolves replaces one that does not, so a
        room first seen without its key is upgraded the moment the key is
        found somewhere the tool can name.
        """
        if not self.enabled:
            return room
        now = time.time() if now is None else now
        existing = self.find(room.url, room.workspace)
        if existing is None:
            room.first_used = room.first_used or now
            room.last_used = now
            self.rooms()
            assert self._rooms is not None
            self._rooms.append(room)
            self._save()
            return room
        existing.last_used = now
        if existing.key.get("from") in (None, "none") and room.key.get("from") != "none":
            existing.key = dict(room.key)
        if existing.token.get("from") in (None, "none") and room.token.get("from") != "none":
            existing.token = dict(room.token)
        if not existing.note and room.note:
            existing.note = room.note
        self._save()
        return existing

    def note_peers(self, url: str, workspace: str, agents: list[dict[str, Any]],
                   *, now: float | None = None) -> None:
        """Remember who was on a room's roster, by name and branch."""
        if not self.enabled:
            return
        room = self.find(url, workspace)
        if room is None:
            return
        now = time.time() if now is None else now
        for agent in agents:
            name = agent.get("name")
            if not isinstance(name, str) or not name or agent.get("unreadable"):
                continue
            room.peers[name] = {"branch": agent.get("branch"), "last": now}
        self._save()

    def forget(self, label: str) -> bool:
        room = self.by_label(label)
        if room is None:
            return False
        assert self._rooms is not None
        self._rooms.remove(room)
        self._save()
        return True

    def relabel(self, workspace: str, label: str) -> bool:
        room = self.by_label(workspace)
        if room is None:
            return False
        room.label = label
        self._save()
        return True

    def recent(self, *, within: float = RECENT_SECONDS,
               now: float | None = None) -> list[KnownRoom]:
        """Rooms this machine was put in lately — what `listen` parks in unasked."""
        now = time.time() if now is None else now
        return [
            r for r in self.rooms()
            if r.learned in LEARNED_FOR_PARKING and now - r.last_used <= within
        ]


def sweep(book: Book, *, topic: str, agent_id: str, client_factory: Callable[..., Any],
          exclude: tuple[str, str] | None = None, query: str | None = None,
          now: float | None = None) -> list[dict[str, Any]]:
    """Look in every known room at once, without announcing anywhere.

    Read-only by design: the note an agent leaves belongs in the room it is
    actually in; a note in every room it has ever visited is litter. Each room
    is reported on its own — roster, live notes on `topic`, who has a listener
    parked — so the agent can DM into the right one. `query` narrows the
    roster and notes to entries mentioning it, which is `find`. A room that
    cannot be reached, or whose key this environment no longer holds, is
    reported as such rather than skipped silently.
    """
    now = time.time() if now is None else now
    needle = (query or "").lower()

    def mentions(*parts: Any) -> bool:
        if not needle:
            return True
        return any(needle in str(p or "").lower() for p in parts)

    out: list[dict[str, Any]] = []
    for room in book.rooms():
        if exclude and (room.url, room.workspace) == (exclude[0].rstrip("/"), exclude[1]):
            continue
        report: dict[str, Any] = {
            "label": room.label, "workspace": room.workspace, "url": room.url,
            "roster": [], "notes": [], "reachable": [], "problem": None,
        }
        config = room.config()
        if config is None:
            report["problem"] = "this environment no longer holds its key"
            out.append(report)
            continue
        try:
            with client_factory(config, agent_id=agent_id) as hub:
                everyone = hub.agents()
                strangers = {a.get("agent_id") for a in hub.key_mismatches(everyone)}
                roster = [
                    a for a in everyone
                    if a.get("agent_id") != hub.agent_id
                    and a.get("agent_id") not in strangers
                    and mentions(a.get("name"), a.get("branch"), a.get("task"))
                ]
                notes = []
                for entry in hub.board_list(prefix=rendezvous.key_for(topic)):
                    if entry.get("unreadable"):
                        continue
                    note = rendezvous.Intent.from_json(entry.get("value"))
                    if (note and note.agent_id != hub.agent_id
                            and note.still_looking(now) and mentions(note.want)):
                        notes.append(note)
                parked = rendezvous.reachable_now(
                    hub.board_list(prefix=rendezvous.LISTENER_PREFIX))
                book.note_peers(room.url, room.workspace, everyone, now=now)
        except Exception as exc:  # noqa: BLE001 - one room must not end the sweep
            report["problem"] = f"unreachable ({exc.__class__.__name__}: {exc})"
            out.append(report)
            continue
        report["roster"] = roster
        report["notes"] = [n.as_json() for n in notes]
        report["reachable"] = sorted(
            a["agent_id"] for a in roster if a.get("agent_id") in parked
        ) + sorted(n.agent_id for n in notes if n.agent_id in parked
                   and n.agent_id not in {a.get("agent_id") for a in roster})
        out.append(report)
    return out
