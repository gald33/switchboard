"""Which rooms a repo takes part in, and which of them this machine can join.

The split this file exists to make (see #61):

**The repo declares rooms.** A committed file lists records — a local name, the
id of the key needed, the workspace token, the hub. All of it is
non-secret and travels with the clone, so a teammate who checks out the repo
already knows what there is to join.

**The environment holds keys**, one variable per key, `SWITCHBOARD_KEY_<ID>`.
Secret, never committed, and shared by every repo set up on that machine.

**The agent joins the intersection.** Not a name anyone types, and nothing to
keep in sync: a room you cannot open is one you do not join, and that is
decidable offline before any hub is contacted.

Two consequences worth stating, because they are the point rather than side
effects. A key and a workspace can no longer disagree, since they are chosen
together as one record instead of being two values that must agree. And a
missing key is a *loud local* failure — "this repo lists room `ops`, you hold
no key `ops`" — rather than an empty inbox that looks exactly like a quiet one.

Keys are referenced **by id, never by position**. A positional index into a
list held somewhere else is the original bug in a new place: prepend a key, or
build the list in a different order on another machine, and every repo silently
resolves to the wrong one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Committed. Non-secret by construction: names, key ids, workspace tokens, hubs.
ROOMS_FILE = ".switchboard/rooms.json"

#: Gitignored overlay, same schema. Where a private room lives — an entry here
#: never appears anywhere guessable, which is also what closes the squatting
#: hole for the rooms that need it.
ROOMS_LOCAL_FILE = ".switchboard/rooms.local.json"

#: The unnamed key, so a setup that predates named keys keeps working.
DEFAULT_KEY_ID = "default"


def workspace_for(workspace_token: str) -> str:
    """The wire identifier for a room, derived from its token.

    The hub routes on this and the client filters on it, so both parties must
    know it — which is exactly why it cannot be the secret-and-rotating value,
    and why it is safe for it to be derived rather than assigned.
    """
    digest = hashlib.sha256(workspace_token.encode("utf-8", "replace")).digest()
    return "w_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")[:22]


class RoomsError(Exception):
    """A room configuration that cannot be resolved into one usable room."""


@dataclass(frozen=True)
class Room:
    """One room this repo can take part in.

    `name` is a local label only. It never reaches the hub — the hub sees the
    workspace token and nothing else — so two machines may label the same room
    differently and nothing breaks, exactly like a git remote name.
    """

    name: str
    key_id: str
    workspace_token: str
    hub_url: str | None = None
    #: Where this record came from, so a conflict can be explained rather than
    #: silently resolved.
    private: bool = False

    @property
    def workspace(self) -> str:
        """What goes on the wire — `hash(workspace_token)`.

        Definitional rather than asserted, which is the whole reason nothing
        needs registering: two parties holding the same token compute the same
        id without being told, and no third party can be asked to arbitrate a
        name nobody typed.

        Hashed rather than sent as-is so the wire value is uniform and opaque
        whatever the token looks like — a token chosen by hand does not become
        a readable room name on the hub just because someone wrote a word.
        """
        return workspace_for(self.workspace_token)


#: Domain separator for the lobby derivation. Versioned, because changing it
#: moves every lobby at once and that should be a deliberate, dateable event
#: rather than a silent consequence of an edit.
LOBBY_INFO = b"switchboard-lobby-v1"


def lobby_token(key: str) -> str:
    """The token of the room every holder of `key` already shares.

    A repo gets its own room, which is right for isolation and leaves agents
    in *different* repos with nowhere to meet: today they must agree on a
    workspace name out of band, and getting it wrong is silent — an agent
    alone in a room it chose by typo looks exactly like an agent in a quiet
    one. The key is already the thing that means "these agents are mine", so
    it can name the meeting place and nobody has to agree on anything they
    could mistype.

    Derived from the key rather than being a well-known constant, and that is
    the whole of the design. A constant would hand every switchboard user on
    earth the same room identifier — contents still sealed, but the hub would
    hold one room with everybody's metadata in it, and what protects a room
    here is that its identifier is unguessable (`docs/model.md`). Derived, a
    lobby is exactly as unguessable as any other room and reachable by exactly
    the agents that already share the key.

    HMAC rather than a bare hash so the key is the secret rather than merely
    an input, and with an explicit `info` so this derivation can never collide
    with another use of the same key.
    """
    digest = hmac.new(key.encode("utf-8", "replace"), LOBBY_INFO, hashlib.sha256)
    return "lobby-" + base64.urlsafe_b64encode(digest.digest()).decode().rstrip("=")[:32]


def lobby(key: str) -> Room:
    """The lobby as a room record, so it is handled like any other."""
    return Room(name="lobby", key_id=DEFAULT_KEY_ID, workspace_token=lobby_token(key))


def env_var_for(key_id: str) -> str:
    """`SWITCHBOARD_KEY_<ID>` for a key id.

    Uppercased with anything unusable replaced, because environment variable
    names are not free-form and a key id like `team/ops` has to land somewhere
    predictable rather than somewhere clever.
    """
    if key_id == DEFAULT_KEY_ID:
        return "SWITCHBOARD_KEY"
    return "SWITCHBOARD_KEY_" + re.sub(r"[^A-Z0-9]+", "_", key_id.upper()).strip("_")


def key_for(key_id: str, env: dict[str, str] | None = None) -> str | None:
    """The key this environment holds for `key_id`, if any."""
    source = os.environ if env is None else env
    return source.get(env_var_for(key_id)) or None


def _parse(path: Path, private: bool) -> list[Room]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise RoomsError(f"{path} is not valid JSON ({exc})") from exc
    entries = data.get("rooms") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise RoomsError(f"{path} should hold a list of rooms under a 'rooms' key")

    rooms = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RoomsError(f"{path}: every room must be an object, got {entry!r}")
        token = entry.get("workspace_token")
        if not isinstance(token, str) or not token:
            raise RoomsError(f"{path}: a room needs a workspace_token")
        rooms.append(Room(
            name=str(entry.get("name") or token[:12]),
            key_id=str(entry.get("key_id") or DEFAULT_KEY_ID),
            workspace_token=token,
            hub_url=entry.get("hub_url") or None,
            private=private,
        ))
    return rooms


def load(directory: Path) -> list[Room]:
    """Every room this repo declares, private overlay first.

    The overlay wins on name collision, and is listed first so that "the first
    match" and "the one that wins" are the same thing rather than two rules.
    """
    private = _parse(directory / ROOMS_LOCAL_FILE, private=True)
    public = _parse(directory / ROOMS_FILE, private=False)
    taken = {room.name for room in private}
    return private + [room for room in public if room.name not in taken]


def joinable(rooms: list[Room], env: dict[str, str] | None = None) -> list[Room]:
    """The rooms this environment holds a key for."""
    return [room for room in rooms if key_for(room.key_id, env)]


def select(
    rooms: list[Room], env: dict[str, str] | None = None, chosen: str | None = None,
) -> Room:
    """The one room to join, or an error explaining precisely why not.

    Ambiguity is refused rather than resolved. Being in two rooms by accident
    is worse than being told to pick one, and picking "the first" would make
    the answer depend on file order — the positional fragility this design
    exists to remove.
    """
    if not rooms:
        raise RoomsError("no rooms declared")

    source = os.environ if env is None else env
    chosen = chosen or source.get("SWITCHBOARD_ROOM") or None
    if chosen:
        for room in rooms:
            if room.name == chosen:
                if not key_for(room.key_id, env):
                    raise RoomsError(
                        f"room {chosen!r} needs key {room.key_id!r}, which this "
                        f"environment does not hold — set {env_var_for(room.key_id)}"
                    )
                return room
        known = ", ".join(sorted(r.name for r in rooms)) or "none"
        raise RoomsError(f"no room named {chosen!r}; this repo declares: {known}")

    usable = joinable(rooms, env)
    if len(usable) == 1:
        return usable[0]
    if not usable:
        missing = sorted({r.key_id for r in rooms})
        wanted = ", ".join(f"{k} ({env_var_for(k)})" for k in missing)
        raise RoomsError(
            "this repo declares "
            + ", ".join(sorted(r.name for r in rooms))
            + f", and this environment holds none of the keys they need: {wanted}"
        )
    names = ", ".join(sorted(r.name for r in usable))
    raise RoomsError(
        f"more than one room is joinable here ({names}) — set SWITCHBOARD_ROOM to "
        "pick one, rather than being in both by accident"
    )
