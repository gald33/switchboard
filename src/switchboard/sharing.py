"""Shared rooms: a room one machine mints and passes to the machines it is paired with.

An invite is a room as a string a human carries. A pairing introduces two
machines once and never sends a room key at all. A shared room is the third
shape: a room several paired machines work in, which travels sealed inside
the pair rooms that already connect them — so no human carries it, and no
conversation, ticket or transcript holds it.

The model is the user's, and deliberately small:

- **The hub knows nothing about it.** A shared room is an ordinary
  write-protected room — hub, workspace, key, write key. It has no members;
  access is knowing it.
- **Each machine keeps, per room, its own list** of the pairs it shares the
  room with. Nothing else decides who is offered it.
- **One action: `share_new`.** It mints a room and offers it to exactly the
  list given. There is no add and no remove: to work without someone, make a
  new room with a new list. `from_room=` starts from an old room's list and
  never touches the old room's list or records. Rooms run in parallel;
  nothing is revoked.
- **`leave`** drops a room here and asks the machines on its list to take
  this one off theirs, so a later `share_new --from` there leaves it out.
- **Nothing moves on its own.** Each side reads its peers' records and is
  told what changed, as notes; keeping, accepting and leaving are each an
  agent's own decision.

Why the pair room carries it. The pair room already reaches exactly the two
machines that hold the pair secret — sealed, write-protected, and kept fresh
by every use of the pair — and a room key sent anywhere else would need a
channel of its own with the same three properties. The cost is said rather
than hidden: whoever holds a pair secret holds every room currently offered
over that pair, and a heal does not change that; a fresh pairing, then a new
room without the old pair, does.

**The records.** Per pair room, per shared room and per writer, one sealed
board entry, `coord/share/<writer's role>/<rid>`, that only that writer's
side writes — pairing's D4, so between the two machines there is nothing to
race. An `offer` in it means "you are on my list for this room" and carries
the room; `left` means "I left it: take me off yours". Per writer and rid a
record only moves forward, none → offer → left, and `left` is final. The rid
is a hash of the room's URL and workspace — a fixed-length key that names
nothing — and the pair room blinds it on the wire like every board key.

**Reading trusts nothing it does not have to.** Every record carries `at`, a
per-writer counter, and is applied only if its `at` is newer than the last
one applied for that pair and room — so neither a hub replaying an older
record nor two processes here applying their reads out of order can put back
a pair that left. Two offers are the same room only if URL, workspace, key
and write key all match, and an offer whose write key does not open its own
workspace is ignored: a peer cannot pass a key of its own off as a room held
here. This side's own slot, which the peer can also write (both hold the pair
room's write key), is read only for its revision and `at`, never acted on as
content — with one forward-only exception: a `left` there means another copy
of this machine left the room, and this copy stops offering it back.

**Writing is a function of local state.** Every touch reads first, then
writes what this machine's store says should be there — the offer of each
held room to each live pair on its list, a `left` for each tombstone not yet
acknowledged — wherever the read-back is missing, different, or a day old.
That one rule is the retry for a failed write, a lost reply, a conflict, and
a `share_new` that reached only some of its pairs. A list entry names a pair
by its name *and* its transcript, so a pair forgotten, ended or paired again
under the same name leaves every list with a note, and is never written to.

**The store** is one owner-only document beside the pair store, with a lock
of its own. Like `pairing.Store`'s, it is never held across a hub call or a
pairing call: a touch reads, then applies under the lock.

See `docs/sharing.md` for the walk-through.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from . import pairing
from .config import ClientConfig
from .crypto import CryptoError
from .pairing import Connect, PairingError, PairRecord

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no advisory locks
    fcntl = None  # type: ignore[assignment]

#: The one share protocol this module speaks. It rides inside every record,
#: so a newer writer's records are skipped here — and said — rather than
#: half-read.
PROTOCOL = 1

#: Room handles: `shared:<name>`, the same string on every surface.
HANDLE_PREFIX = "shared:"

#: Where the records live in a pair room, beside `coord/pair/side/`.
PREFIX = "coord/share/"
#: The sealed tag inside every record. Bound into the value because the
#: board's AEAD context is the same for every key in a room.
TAG_SHARE = "swbp1/share"

#: A record lives as long as the board allows and is rewritten at most once a
#: day while its room is held — the side records' rhythm, so a machine away
#: for less than a week is never mistaken for one that left.
RECORD_TTL = pairing.SIDE_TTL
REFRESH = pairing.SIDE_REFRESH
#: Notes, and tombstones every pair has acknowledged, are kept this long.
NOTE_KEEP = 7 * 86400
#: Peer-supplied text is cut to this, as pairing cuts the answer's note.
NOTE_MAX = 200
#: A room used here this recently is one a listener parks in.
RECENT = 86400.0
#: A room whose last refresh did not reach every pair on its list is tried
#: again after this long, not on every command in it: each try waits out an
#: unreachable pair's timeout, and `--room shared:` has no sweep to pace it.
REFRESH_RETRY = 3600.0

#: The document and its lock, in the pair store's directory. Not
#: `shared.json`: `PairStore` keeps a pair called `shared` in exactly that
#: file, and an underscore is one thing a pair name can never contain.
STORE_FILE = "shared_rooms.json"
LOCK_FILE = "shared.lock"

# Frozen for protocol 1: two machines must compute the same rid for a room.
_L_RID = b"switchboard/share/v1/rid\x00"
_RID = re.compile(r"[a-z2-7]{20}")
# Control characters, C1 included, and lone surrogates: JSON carries a
# surrogate as `\ud800` without complaint, and a strict UTF-8 stdout raises
# on it — in `share status`, or in the listener's wake.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
#: A token as an HTTP header can carry it.
_TOKEN = re.compile(r"[\x20-\x7e]+")

#: What `pairing.resolve` raises when the pair itself is over. Any of these
#: takes the pair off every list here; anything else means only that it
#: could not be read this time.
_PAIR_OVER = frozenset({
    "ended", "ended_by_peer", "unknown", "never_confirmed", "aborted",
    "superseded", "already_redeemed", "unsent", "expired",
})
_OVER_WHY = {
    "ended": "was ended from this side",
    "ended_by_peer": "was ended by the other side",
    "unknown": "is no longer on this machine",
    "never_confirmed": "was never confirmed",
}


class SharingError(PairingError):
    """A share that cannot proceed, said as a sentence with the way out.

    A `PairingError`, so every surface that already turns a pairing refusal
    into its `state` — the CLI's exit codes, the MCP bridge's error rows —
    reports these the same way without learning a second class.
    """


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _default_connect(config: ClientConfig, *, agent_id: str) -> Any:
    from .client import Client

    return Client(config, agent_id=agent_id)


def handle(name: str) -> str:
    """The room handle for a shared room: `shared:<name>`."""
    return HANDLE_PREFIX + name


def parse_handle(text: str | None) -> str | None:
    """The room name in a `shared:<name>` handle, or None for anything else."""
    if isinstance(text, str) and text.startswith(HANDLE_PREFIX):
        return text[len(HANDLE_PREFIX):]
    return None


def validate_name(name: str) -> str:
    """A shared room's local name, or a refusal saying what a name may be.

    The pair-name rule, for the same reasons: it becomes part of a handle and
    a key in the store, and a default one comes from another machine.
    """
    try:
        return pairing.validate_name(name)
    except PairingError:
        raise SharingError(
            f"{name!r} is not a usable shared-room name: use lowercase letters, "
            "digits and dashes, starting with a letter or digit, at most 32 "
            "characters.", state="invalid") from None


def room_id(url: str, workspace: str) -> str:
    """The rid a room's records are filed under, in every pair room."""
    digest = hashlib.sha256(
        _L_RID + url.rstrip("/").encode() + b"\x00" + workspace.encode()).digest()
    return base64.b32encode(digest).decode().lower()[:20]


def _clean(text: Any) -> str:
    """Peer text as something safe to print: one line, at most NOTE_MAX."""
    if not isinstance(text, str):
        return ""
    return _CONTROL.sub(" ", text).strip()[:NOTE_MAX]


def _own_note(note: str | None) -> str:
    note = note or ""
    if len(note) > NOTE_MAX:
        raise SharingError(f"a note is at most {NOTE_MAX} characters (this one is "
                           f"{len(note)}); it travels in every record of the room.",
                           state="invalid")
    return _clean(note)


# --- the store ------------------------------------------------------------------


class Store(Protocol):
    """Where shared rooms are kept: what every sharing call takes as `store=`.

    `ShareStore`, one file beside the pair store, is the one this package
    uses. An embedder that keeps pairs in a table of its own
    (`pairing.Store`) keeps this document beside them, one per owner. What
    the core relies on:

    - **`load()` returns the whole document**, or an empty dict when nothing
      is saved, and **`save(doc)` replaces it atomically and durably** before
      it returns. For a room this machine created it is the only copy of the
      room's keys, so keep it encrypted at rest, as the pair secrets are.
    - **`lock()` is exclusive per owner, and is not the pair store's lock.** A
      touch calls `pairing.resolve`, which takes that one. The core never
      nests this lock, and never makes a hub call or a pairing call while
      holding it — only the load and save of one read-modify-write.
    - **`require()` raises `PairingError(state="disabled")`** when shared
      rooms cannot be kept at all, and `enabled` is False then.
    - **A storage failure raises `OSError` or `PairingError`**; the core turns
      an `OSError` into a refusal with state `store`.
    """

    @property
    def enabled(self) -> bool: ...

    def require(self) -> object: ...

    def lock(self) -> contextlib.AbstractContextManager[None]: ...

    def load(self) -> dict[str, Any]: ...

    def save(self, data: dict[str, Any]) -> None: ...


class ShareStore:
    """This machine's shared rooms: one document in the pair store's directory.

    Under the same `SWITCHBOARD_PAIRS` override and the same empty-string
    switch as the pair store, because the two only work together: every room
    here is reached through pairs there. Written the way the pair store is —
    owner-only, a temporary file renamed over the old one — because for a
    room this machine created it holds the only copy of the room's keys. Its
    lock is its own, `shared.lock`: sharing calls pairing, and pairing takes
    the pair store's.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.env = os.environ if env is None else env
        raw = os.fspath(path) if path is not None else self.env.get(pairing.ENV,
                                                                     pairing.DEFAULT_DIR)
        self.enabled = bool(raw)
        self.path = Path(os.path.expanduser(raw)) if raw else None

    def require(self) -> Path:
        if self.path is None:
            raise SharingError(
                f"shared rooms are disabled on this machine: {pairing.ENV} is set to "
                "the empty string, which turns pairing off too. Unset it, or point "
                "it at a directory, to keep shared rooms here.", state="disabled")
        return self.path

    def _dir(self) -> Path:
        directory = self.require()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        return directory

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Serialise read-modify-write across this machine's processes."""
        if fcntl is None:  # pragma: no cover
            yield
            return
        fd = os.open(self._dir() / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load(self) -> dict[str, Any]:
        path = self.require() / STORE_FILE
        try:
            text = path.read_text()
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SharingError(
                f"{path} is not valid JSON ({exc}). It may hold the only copy of the "
                "keys of rooms this machine created, so it is not overwritten — move "
                "it aside.", state="store") from exc
        if not isinstance(data, dict):
            raise SharingError(f"{path} is not a share store; move it aside.",
                               state="store")
        return data

    def save(self, data: dict[str, Any]) -> None:
        pairing.PairStore._write(self._dir() / STORE_FILE, data)


_TABLES = ("rooms", "offers", "seen", "tombstones", "own", "outage", "skipped")


def _normal(data: dict[str, Any]) -> dict[str, Any]:
    """The document with every table present, whatever an older save left out."""
    version = data.get("v", PROTOCOL)
    if version != PROTOCOL:
        raise SharingError(
            f"the share store is protocol {version!r}; this release speaks "
            f"{PROTOCOL}. Upgrade agent-switchboard rather than editing it.",
            state="upgrade")
    doc: dict[str, Any] = {"v": PROTOCOL, "seq": int(data.get("seq") or 0)}
    for table in _TABLES:
        value = data.get(table)
        doc[table] = value if isinstance(value, dict) else {}
    notes = data.get("notes")
    doc["notes"] = notes if isinstance(notes, list) else []
    return doc


def _store_failure(exc: OSError) -> SharingError:
    return SharingError(
        f"the share store could not be used ({exc.__class__.__name__}: {exc}). It "
        "holds the only copy of the keys of rooms this machine created, so nothing "
        f"was overwritten; check that {pairing.ENV} (or ~/.switchboard/pairs) is a "
        "writable directory with space left.", state="store")


@contextlib.contextmanager
def _editing(store: Store) -> Iterator[dict[str, Any]]:
    """Load the document under the lock, and save it if the block changed it.

    A refusal raised inside saves nothing, so a block that must both record
    something and refuse records it here and raises after.
    """
    try:
        with store.lock():
            doc = _normal(store.load())
            before = json.dumps(doc, sort_keys=True)
            yield doc
            if json.dumps(doc, sort_keys=True) != before:
                store.save(doc)
    except OSError as exc:
        raise _store_failure(exc) from exc


def _snapshot(store: Store) -> dict[str, Any]:
    try:
        with store.lock():
            return _normal(store.load())
    except OSError as exc:
        raise _store_failure(exc) from exc


def _say(doc: dict[str, Any], now: float, kind: str, text: str, *,
         pair: str | None = None, room: str | None = None) -> None:
    doc["seq"] += 1
    doc["notes"].append({"id": doc["seq"], "at": now, "kind": kind, "pair": pair,
                         "room": room, "text": text})


def _prune(doc: dict[str, Any], now: float) -> None:
    doc["notes"] = [n for n in doc["notes"]
                    if isinstance(n, dict) and now - float(n.get("at") or 0) <= NOTE_KEEP]
    for rid in [r for r, t in doc["tombstones"].items()
                if t.get("done_at") is not None and now - t["done_at"] > NOTE_KEEP]:
        del doc["tombstones"][rid]


def _note_view(note: dict[str, Any]) -> dict[str, Any]:
    return {"id": note.get("id"), "at": pairing.iso(note.get("at")),
            "kind": note.get("kind"), "pair": note.get("pair"),
            "room": note.get("room"), "text": note.get("text")}


# --- list entries: a pair by name *and* transcript --------------------------------


def _has(entries: list[dict[str, Any]], pair: str, th: str) -> bool:
    return any(e.get("pair") == pair and e.get("th") == th for e in entries)


def _without(entries: list[dict[str, Any]], pair: str, th: str) -> list[dict[str, Any]]:
    return [e for e in entries if not (e.get("pair") == pair and e.get("th") == th)]


def _union(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for group in groups:
        for e in group:
            seen.setdefault((e["pair"], e["th"]), {"pair": e["pair"], "th": e["th"]})
    return list(seen.values())


def _is_origin(room: dict[str, Any], pair: str, th: str) -> bool:
    origin = room.get("origin")
    return isinstance(origin, dict) and origin.get("pair") == pair and origin.get("th") == th


def _room_by_rid(doc: dict[str, Any], rid: str) -> tuple[str | None, dict[str, Any] | None]:
    for name, room in doc["rooms"].items():
        if room.get("rid") == rid:
            return name, room
    return None, None


def _seen(doc: dict[str, Any], pair: str, rid: str) -> dict[str, Any] | None:
    for side in pairing.ROLES:
        entry = doc["seen"].get(f"{pair}/{rid}/{side}")
        if entry is not None:
            return entry
    return None


def _drop_pair(doc: dict[str, Any], pair: str, why: str, now: float,
               match: Callable[[Any], bool], *, whole: bool = False) -> None:
    """Take a pair that is over off every list here, and say what that cost.

    `match(th)` picks which of the name's entries are that pair: all of them
    when the name is gone, the old transcript's when it now names another
    pair. Nothing is ever written to it again — including, for a room this
    machine left, the `left` it was still owed, which the note says.
    """
    rooms: list[str] = []
    for name, room in sorted(doc["rooms"].items()):
        kept = [e for e in room.get("list", []) if not (e.get("pair") == pair
                                                        and match(e.get("th")))]
        if len(kept) != len(room.get("list", [])):
            room["list"] = kept
            rooms.append(name)
    offers = sorted({doc["offers"].pop(key)["name"] for key in [
        k for k, o in doc["offers"].items()
        if o.get("pair") == pair and match((o.get("from") or {}).get("th"))]})
    untold: set[str] = set()
    for tomb in doc["tombstones"].values():
        for column in ("pairs", "told"):
            before = tomb.get(column, [])
            tomb[column] = [e for e in before if not (e.get("pair") == pair
                                                      and match(e.get("th")))]
            if column == "pairs" and len(tomb[column]) != len(before):
                untold.add(tomb["name"])
                if not tomb[column]:
                    tomb.setdefault("done_at", now)
    for table in ("seen", "own"):
        for key in [k for k, v in doc[table].items()
                    if k.split("/", 1)[0] == pair and match(v.get("th"))]:
            del doc[table][key]
    if whole:
        # The name itself is gone, and so is anything said about reaching it.
        doc["outage"].pop(pair, None)
        doc["skipped"].pop(pair, None)
    parts = []
    if rooms:
        parts.append(f"it is off the list of {', '.join(rooms)}")
    if offers:
        parts.append(f"its offer of {', '.join(offers)} was dropped")
    if untold:
        parts.append(f"it will not be told that you left {', '.join(sorted(untold))}")
    if parts:
        _say(doc, now, "pair_gone",
             f"pair {pair} {why}: {'; '.join(parts)}. Nothing more is written to it.",
             pair=pair)


def _identity(doc: dict[str, Any], pair: str, record: PairRecord | None,
              now: float) -> None:
    if record is None:
        _drop_pair(doc, pair, "is no longer on this machine (forgotten, or a container "
                   "that did not keep it)", now, lambda th: True, whole=True)
    else:
        current = record.transcript
        _drop_pair(doc, pair, "now names a different pairing (paired again under the "
                   "same name)", now, lambda th: th != current)


# --- records -----------------------------------------------------------------------


@dataclass
class _Rec:
    """One record as read from a pair room."""

    rid: str
    revision: int | None
    expires_in: float | None
    at: float | None = None
    name: str | None = None
    note: str = ""
    left: bool = False
    offer: dict[str, Any] | None = field(default=None, repr=False)
    raw: dict[str, Any] | None = field(default=None, repr=False)


@dataclass
class _Write:
    rid: str
    value: dict[str, Any] = field(repr=False)
    revision: int | None


def _at(value: Any) -> float | None:
    """A record's `at`, or None when it is not a time this machine can use.

    pairing's rule for a time the other side wrote: an integer too large for
    a float, NaN, or a year `iso` cannot show is no time at all — skipped as
    the record's own fault, never raised out of the touch that read it.
    """
    return pairing.record_time(value) or None


def _number(value: Any) -> float | None:
    """A number the hub reported (`expires_in`), or None. Unsealed, so the hub
    chooses it, and a hostile one must cost the value, not the touch."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _record_key(role: str, rid: str) -> str:
    return f"{PREFIX}{role}/{rid}"


def _parse(entries: list[dict[str, Any]], record: PairRecord
           ) -> tuple[dict[str, _Rec], dict[str, _Rec], list[tuple[str | None, str]]]:
    """The peer's records, this side's own, and what had to be skipped.

    Only the peer's slot is read as content. This side's slot is kept for
    its revision, its `at`, and whether it says `left` — the one thing about
    it that is acted on — and is otherwise only compared with what local
    state says it should hold.
    """
    peer: dict[str, _Rec] = {}
    own: dict[str, _Rec] = {}
    skipped: list[tuple[str | None, str]] = []
    for entry in entries:
        key = entry.get("key")
        if not isinstance(key, str) or not key.startswith(PREFIX):
            # Kept by the client's prefix filter only because it could not be
            # opened — so it cannot be classified, and is not anybody's record.
            skipped.append((None, "a record this pair's key cannot open"))
            continue
        slot, _, rid = key[len(PREFIX):].partition("/")
        if slot not in pairing.ROLES or not _RID.fullmatch(rid):
            skipped.append((None, "a key that is not a share record's"))
            continue
        revision = entry.get("revision")
        expires = entry.get("expires_in")
        rec = _Rec(rid, revision if isinstance(revision, int)
                   and not isinstance(revision, bool) else None, _number(expires))
        value = None if entry.get("unreadable") else entry.get("value")
        if slot == record.role:
            if isinstance(value, dict) and value.get("t") == TAG_SHARE:
                rec.at, rec.left, rec.raw = _at(value.get("at")), value.get("left") is True, value
            own[rid] = rec
            continue
        why = _peer_value(rec, value)
        if why:
            skipped.append((rid, why))
        else:
            peer[rid] = rec
    return peer, own, skipped


def _peer_value(rec: _Rec, value: Any) -> str | None:
    """Fill `rec` from the peer's value, or say why it cannot be used at all."""
    if not isinstance(value, dict):
        return "it cannot be opened"
    if value.get("t") != TAG_SHARE:
        return "it is not a share record"
    version = value.get("v")
    if version != PROTOCOL:
        # Never the value itself: it is the peer's, of any length, and this
        # reason becomes a note every agent on the machine reads.
        if isinstance(version, int) and not isinstance(version, bool) \
                and PROTOCOL < version < 1000:
            return f"it is share protocol {version} (upgrade to read it)"
        return "it names no share protocol this release knows"
    at = _at(value.get("at"))
    if at is None:
        return "it carries no usable time"
    name = value.get("name")
    if not isinstance(name, str) or not pairing._NAME.fullmatch(name):
        # Never used, as a path or anything else: shown by rid only.
        return "its room name is not a usable name"
    offer = value.get("offer")
    left = value.get("left") is True
    if offer is not None and not left:
        if not isinstance(offer, dict):
            return "its offer is malformed"
        url, workspace = offer.get("url"), offer.get("workspace")
        try:
            names = isinstance(url, str) and isinstance(workspace, str) \
                and room_id(url, workspace) == rec.rid
        except UnicodeError:
            names = False  # a lone surrogate: no room has that URL
        if not names:
            return "the room it offers is not the one its key names"
    rec.at, rec.name, rec.note, rec.left, rec.raw = at, name, _clean(value.get("note")), left, value
    rec.offer = None if left else offer
    return None


def _unusable(offer: dict[str, Any]) -> str | None:
    """Why an offer is not a room this machine could join, or None.

    Nothing missing is ever filled in from this environment — an offer
    without a key is not "the room, with your key", it is not a room. And
    each part is checked the way opening the room will use it, here rather
    than there: a room accepted with a key no cipher takes, or a URL no
    client takes, fails every time it is opened — the listener included,
    which opens every room used lately and would stop before parking at all.
    """
    import httpx

    from .crypto import WorkspaceCipher
    from .writekey import RoomWriteKey, WriteKeyError

    for column in ("key", "write_key"):
        if not isinstance(offer.get(column), str) or not offer[column]:
            return f"it carries no {column.replace('_', ' ')}"
    token = offer.get("token")
    if token is not None and not (isinstance(token, str) and _TOKEN.fullmatch(token)):
        return "its token is malformed"
    try:
        derived = RoomWriteKey.from_seed(offer["write_key"]).workspace
    except (WriteKeyError, ValueError, TypeError):
        return "its write key is malformed"
    if derived != offer.get("workspace"):
        return "its write key does not open its workspace"
    try:
        WorkspaceCipher.from_key(offer["key"], derived)
    except (CryptoError, ValueError, TypeError):
        return "its key is malformed"
    url = offer.get("url")
    try:
        parsed = httpx.URL(url) if isinstance(url, str) and not _CONTROL.search(url) else None
    except (httpx.InvalidURL, ValueError, TypeError):
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
        return "its hub URL is not an http(s) URL"
    return None


def _fp(offer: dict[str, Any]) -> str:
    """Which room an offer is, exactly — kept for a pending offer, whose keys
    are not kept, so a later offer can be compared with it."""
    blob = json.dumps([str(offer.get("url", "")).rstrip("/"), offer.get("workspace"),
                       offer.get("key"), offer.get("write_key")])
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _offer_text(pair: str, name: str, note: str) -> str:
    return (f"{pair} offers room {name}{': ' + note if note else ''} "
            f"(`share accept {pair}/{name}` keeps it)")


def _known_rids(doc: dict[str, Any], pair: str) -> set[str]:
    rids = {k.split("/")[1] for k in doc["seen"] if k.split("/", 1)[0] == pair}
    rids |= {o["rid"] for o in doc["offers"].values() if o.get("pair") == pair}
    rids |= {r["rid"] for r in doc["rooms"].values()
             if any(e.get("pair") == pair for e in r.get("list", []))}
    rids |= {rid for rid, t in doc["tombstones"].items()
             if any(e.get("pair") == pair for e in t.get("pairs", []))}
    return rids


def _apply(doc: dict[str, Any], pair: str, record: PairRecord, peer: dict[str, _Rec],
           own: dict[str, _Rec], skipped: list[tuple[str | None, str]], *,
           read_at: float, now: float, peer_active_at: float | None = None
           ) -> list[_Write]:
    """Apply one read of a pair room to the document; return what to write.

    Runs under the share lock, after the hub read: whatever another process
    applied meanwhile is in `doc`, and only records newer than it count.
    `peer_active_at` is when the other side last wrote its side record, as
    the resolution before this read saw it (`_acknowledge`).
    """
    th = record.transcript
    listed = sorted(f"{rid or '?'}: {why}" for rid, why in skipped)
    signature = "; ".join(listed)
    if signature and doc["skipped"].get(pair) != signature:
        # Five at most: the rids are the peer's to choose, and so is how many.
        shown = "; ".join(listed[:5]) + (f"; and {len(listed) - 5} more"
                                         if len(listed) > 5 else "")
        _say(doc, now, "unreadable",
             f"{len(skipped)} record(s) in pair {pair}'s room could not be read and "
             f"were ignored ({shown})", pair=pair)
    if signature:
        doc["skipped"][pair] = signature
    else:
        doc["skipped"].pop(pair, None)
    unread = {rid for rid, _ in skipped if rid}
    # First, so an offer of a room another copy of this machine left is
    # already known as one when it is read below.
    _own_left(doc, pair, th, own, now)
    for rid in sorted((set(peer) | _known_rids(doc, pair)) - unread):
        _apply_one(doc, pair, th, record.peer_role, rid, peer.get(rid),
                   read_at=read_at, now=now)
    _acknowledge(doc, pair, th, peer, own, unread, now, peer_active_at)
    return _desired(doc, pair, th, own, now)


def _apply_one(doc: dict[str, Any], pair: str, th: str, side: str, rid: str,
               rec: _Rec | None, *, read_at: float, now: float) -> None:
    key = f"{pair}/{rid}"
    seen_key = f"{key}/{side}"
    prev = doc["seen"].get(seen_key)
    if prev is not None and prev.get("th") != th:
        prev = None
    was = prev.get("kind") if prev else None
    room_name, room = _room_by_rid(doc, rid)
    on_list = room is not None and _has(room.get("list", []), pair, th)
    pending = doc["offers"].get(key)

    if rec is None:
        # Gone from the room. Only a read at least as recent as the last one
        # applied may say so, or a slow reader would undo a newer offer.
        if prev is None or was in ("gone", "left") or read_at < float(prev.get("read") or 0):
            return
        doc["seen"][seen_key] = {**prev, "kind": "gone", "read": read_at}
        if pending is not None:
            del doc["offers"][key]
            _say(doc, now, "withdrawn",
                 f"{pair}'s offer of {pending['name']} lapsed or was withdrawn",
                 pair=pair, room=pending["name"])
        elif was == "offer" and on_list:
            _say(doc, now, "record_gone",
                 f"{pair}'s record for {room_name} is gone — not refreshed for 7 days, "
                 "or it lost its copy; still on your list", pair=pair, room=room_name)
        return

    assert rec.at is not None and rec.name is not None
    if prev is not None and (was == "left" or rec.at <= float(prev.get("at") or 0)):
        return  # not newer: a replay, or a reader that lost the race
    entry = {"th": th, "at": rec.at, "read": read_at, "kind": "none"}
    told = any(_has(t.get("told", []), pair, th) for r, t in doc["tombstones"].items()
               if r == rid)
    # This side's own slot said `left` for the rid (`_own_left`): another copy
    # of this machine left the room, or refused it, over this pair.
    left_here = doc["own"].get(key, {}).get("left") is True
    if rec.left:
        entry["kind"] = "left"
        name = room_name if on_list else (pending or {}).get("name")
        if on_list:
            room["list"] = _without(room["list"], pair, th)
        if pending is not None:
            del doc["offers"][key]
        if name:
            _say(doc, now, "left", f"{pair} left {name}{': ' + rec.note if rec.note else ''}",
                 pair=pair, room=name)
    elif rec.offer is None:
        if pending is not None:
            del doc["offers"][key]
            _say(doc, now, "withdrawn",
                 f"{pair}'s offer of {pending['name']} lapsed or was withdrawn",
                 pair=pair, room=pending["name"])
        elif on_list and was == "offer":
            _say(doc, now, "record_gone",
                 f"{pair}'s record for {room_name} no longer carries it; still on your "
                 "list", pair=pair, room=room_name)
    else:
        why = _unusable(rec.offer)
        fp = _fp(rec.offer)
        entry["kind"] = "offer"
        if why:
            entry["kind"] = "unusable"
            if was != "unusable":
                _say(doc, now, "unusable", f"{pair} offers {rec.name}, but {why}; ignored",
                     pair=pair, room=rec.name)
        elif told:
            pass  # a room this machine left, offered by a pair it told: not new
        elif room is not None:
            if _fp(room) != fp:
                entry["kind"] = "mismatch"
            elif on_list:
                if was != "offer" and not _is_origin(room, pair, th):
                    _say(doc, now, "accepted", f"{pair} accepted {room_name}",
                         pair=pair, room=room_name)
            else:
                if pending is None:
                    _say(doc, now, "offered_held",
                         f"{pair} offers {rec.name}, which you already hold as "
                         f"{room_name} (`share accept {pair}/{rec.name}` puts {pair} "
                         "on its list)", pair=pair, room=room_name)
                doc["offers"][key] = _pending(rec, pair, th, fp)
        elif left_here:
            # Not new: this machine already said no to it over this pair. A
            # fresh container has no tombstone to say so, and its slot does —
            # read forward-only, so a peer planting it there only withholds
            # its own offer.
            if pending is not None:
                del doc["offers"][key]
            if was != "offer" or pending is not None:
                _say(doc, now, "left_elsewhere",
                     f"another copy of this machine left {rec.name} (its record to {pair} "
                     f"says so): {pair}'s offer of it is not taken as new here",
                     pair=pair, room=rec.name)
        else:
            rival = any(o.get("rid") == rid and k != key and o.get("fp") != fp
                        for k, o in doc["offers"].items())
            if rival or (pending is not None and pending.get("fp") != fp):
                entry["kind"] = "mismatch"
            else:
                if pending is None:
                    _say(doc, now, "offered", _offer_text(pair, rec.name, rec.note),
                         pair=pair, room=rec.name)
                doc["offers"][key] = _pending(rec, pair, th, fp)
        if entry["kind"] == "mismatch" and was != "mismatch":
            _say(doc, now, "different_keys",
                 f"{pair} offers {rec.name} with different keys; ignored",
                 pair=pair, room=rec.name)
    doc["seen"][seen_key] = entry


def _pending(rec: _Rec, pair: str, th: str, fp: str) -> dict[str, Any]:
    return {"pair": pair, "rid": rec.rid, "name": rec.name, "note": rec.note,
            "at": rec.at, "from": {"pair": pair, "th": th}, "fp": fp}


def _own_left(doc: dict[str, Any], pair: str, th: str, own: dict[str, _Rec],
              now: float) -> None:
    """Another copy of this machine left a room over this pair: never offer it back.

    Forward-only and final, like every `left`: kept locally, so the record
    expiring from the hub changes nothing. A room this copy holds it keeps,
    but stops offering to that pair; a room it does not hold — a fresh
    container's case — is not taken as a new offer from that pair either
    (`_apply_one`). Recorded for every rid, held or not, since the offer may
    be read before the room is ever held here.
    """
    for rid, rec in own.items():
        if not rec.left:
            continue
        mine = doc["own"].setdefault(f"{pair}/{rid}", {"th": th})
        if mine.get("left"):
            continue
        mine["left"] = True
        name, room = _room_by_rid(doc, rid)
        if room is not None and _has(room.get("list", []), pair, th):
            _say(doc, now, "left_elsewhere",
                 f"another copy of this machine left {name} (its record to {pair} says "
                 f"so): this copy keeps the room, but no longer offers it to {pair}",
                 pair=pair, room=name)


def _acknowledge(doc: dict[str, Any], pair: str, th: str, peer: dict[str, _Rec],
                 own: dict[str, _Rec], unread: set[str], now: float,
                 peer_active_at: float | None) -> None:
    """A tombstone is done with a pair once this side's `left` is on the hub,
    the pair no longer offers the room back, and the pair has been active
    since that `left` landed.

    The last is what tells "it read the leave" from "it has been away": an
    offer that lapsed because its machine was gone for over a week looks
    exactly like one withdrawn, and stopping the `left` then lets it lapse
    too, unread — the machine comes back still listing this one, and its
    next `share new --from` offers it the room it left. Active is its side
    record, which every share touch there rewrites within a day (it resolves
    the pair first), so a pair that comes back is acknowledged within a day
    of reading the leave, and one that stays away keeps being told.
    """
    for rid, tomb in doc["tombstones"].items():
        if rid in unread or not _has(tomb.get("pairs", []), pair, th):
            continue
        mine, theirs = own.get(rid), peer.get(rid)
        if mine is None or not mine.left or (theirs is not None and theirs.offer is not None):
            continue
        landed = doc["own"].get(f"{pair}/{rid}", {}).get("left_landed")
        if landed is None or peer_active_at is None or peer_active_at <= float(landed):
            continue
        tomb["pairs"] = _without(tomb["pairs"], pair, th)
        if not tomb["pairs"]:
            tomb.setdefault("done_at", now)


def _desired(doc: dict[str, Any], pair: str, th: str, own: dict[str, _Rec],
             now: float) -> list[_Write]:
    """What this side's slot should hold, wherever the read-back says otherwise."""
    wanted: dict[str, dict[str, Any]] = {}
    for name, room in doc["rooms"].items():
        rid = room["rid"]
        if not _has(room.get("list", []), pair, th) or \
                doc["own"].get(f"{pair}/{rid}", {}).get("left"):
            continue
        wanted[rid] = {"t": TAG_SHARE, "v": PROTOCOL, "name": name,
                       "offer": {"url": room["url"], "workspace": room["workspace"],
                                 "key": room["key"], "write_key": room["write_key"],
                                 "token": room.get("token")},
                       "left": False, "note": room.get("note", "")}
    for rid, tomb in doc["tombstones"].items():
        if _has(tomb.get("pairs", []), pair, th):
            # Final for the rid: wins over an offer, should the room be held again.
            wanted[rid] = {"t": TAG_SHARE, "v": PROTOCOL, "name": tomb["name"],
                           "left": True, "note": tomb.get("note", "")}
    writes = []
    for rid, value in sorted(wanted.items()):
        mine = own.get(rid)
        if mine is not None and mine.raw is not None and _same(mine.raw, value) \
                and not _stale(mine, now):
            continue
        before = max((mine.at or 0.0) if mine is not None else 0.0,
                     float(doc["own"].get(f"{pair}/{rid}", {}).get("at") or 0.0))
        # A counter, not a clock: a clock stepped back must not make this
        # side's newer record look older than the one it replaces.
        value["at"] = max(now, before + 0.001)
        writes.append(_Write(rid, value, 0 if mine is None else mine.revision))
    return writes


def _same(raw: dict[str, Any], value: dict[str, Any]) -> bool:
    columns = ("t", "v", "name", "offer", "left", "note")
    return all(raw.get(c) == value.get(c) for c in columns)


def _stale(rec: _Rec, now: float) -> bool:
    if rec.expires_in is not None and rec.expires_in < RECORD_TTL - REFRESH:
        return True
    return rec.at is None or now - rec.at > REFRESH


# --- touches ---------------------------------------------------------------------


@dataclass
class _Outcome:
    pair: str
    state: str = "ok"  # ok | unreachable | gone | unconfirmed
    reason: str = ""
    error_state: str | None = None
    wrote: bool = False
    live: dict[str, _Rec] = field(default_factory=dict, repr=False)


@dataclass
class Touched:
    """What one touch reached, and what it found."""

    reached: list[str] = field(default_factory=list)
    #: Pairs not read, or not fully written, with why — each retried by the
    #: next touch. `states` says which kind: unreachable, gone, unconfirmed.
    unreached: dict[str, str] = field(default_factory=dict)
    states: dict[str, str] = field(default_factory=dict)
    written: list[str] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"reached": list(self.reached), "written": list(self.written),
                "unreached": [{"pair": p, "state": self.states.get(p), "reason": r}
                              for p, r in sorted(self.unreached.items())],
                "notes": [_note_view(n) for n in self.notes]}


def _cannot(store: Store, out: _Outcome, reason: str, now: float,
            state: str | None = None) -> _Outcome:
    """A pair whose room could not be used this time: one note per outage."""
    with _editing(store) as doc:
        if out.pair not in doc["outage"]:
            doc["outage"][out.pair] = {"since": now, "reason": reason[:300]}
            _say(doc, now, "cannot_read",
                 f"cannot reach pair {out.pair}'s room ({reason}); its share records are "
                 "read and written again on the next touch", pair=out.pair)
    out.state, out.reason, out.error_state = "unreachable", reason, state or "unreachable"
    return out


def _is_conflict(exc: BaseException) -> bool:
    return getattr(exc, "status", None) == 409


def _hub_errors() -> tuple[type[BaseException], ...]:
    """The hub's errors, and a value the client could not seal or open — each
    costs one pair this time, never the whole touch."""
    return pairing.hub_errors() + (CryptoError,)


class _Unappliable(Exception):
    """A peer's records broke the reader in a way no check above foresaw."""


def _moved(doc: dict[str, Any], pair: str, lines: list[str], now: float) -> None:
    """Say a move a share touch's resolution made — a heal adopted, a lapse.

    Said here because nothing else will: the resolution that makes a move is
    the only one that sees it, and the next `--room pair:` finds the pair
    already moved. Every pair is resolved by every full touch, the bridge's
    sweep each minute among them, so without this a heal could be followed
    in silence — claims and unread messages left behind in the old work room
    with nobody told.
    """
    for line in lines:
        _say(doc, now, "pair_moved", f"pair {pair}: {line}", pair=pair)


def _read_clock(now: float | None) -> Callable[[], float]:
    """When each read of one touch was made.

    A caller that passes `now` fixes it at the start of a touch that may read
    a dozen pair rooms after it. Every read stamped with that one time would
    order wrongly against another process's reads in between, and S4's rule
    — a "gone" counts only from a read at least as recent as the last one
    applied — would let an earlier read withdraw a later one's offer. So the
    time that passes since is added, on the monotonic clock.
    """
    if now is None:
        return time.time
    started, base = time.monotonic(), float(now)
    return lambda: base + (time.monotonic() - started)


def _touch_pair(name: str, base: ClientConfig | None, *, store: Store,
                pair_store: pairing.Store, connect: Connect,
                now: float | None, clock: Callable[[], float] | None = None) -> _Outcome:
    """Read one pair's share records, apply them, write what is owed, wake.

    `pairing.resolve` first: it reads the pair room's side records, and
    raises when the pair is over — which here takes it off every list. It
    does not mark the pair used: a touch reads every pair on a schedule of
    its own, and a listener parks in a pair that carries shared rooms because
    it carries them (`active_pairs`), not because a sweep looked at it. A move
    that resolution makes is said as a note (`_moved`).

    Whatever the peer's records hold costs this pair alone. The parsers are
    total for every value JSON can carry; anything they still meet becomes
    this pair's `cannot_read`, named by its class only, since an exception's
    text can quote the record that caused it.
    """
    out = _Outcome(name)
    local = _now(now)
    clock = clock or _read_clock(now)
    try:
        record = pair_store.get(name)
    except (PairingError, OSError) as exc:
        return _cannot(store, out, f"the pair store could not read it "
                       f"({exc.__class__.__name__}: {exc})", local,
                       getattr(exc, "state", None) or "store")
    with _editing(store) as doc:
        _identity(doc, name, record, local)
    if record is None:
        out.state, out.error_state = "gone", "unknown"
        out.reason = f"no pair called {name!r} on this machine"
        return out
    out_th = record.transcript
    try:
        resolution = pairing.resolve(name, base, store=pair_store, connect=connect,
                                     now=now, mark_used=False)
    except PairingError as exc:
        if exc.state in _PAIR_OVER:
            with _editing(store) as doc:
                _drop_pair(doc, name, _OVER_WHY.get(exc.state or "",
                                                    f"is over ({exc.state})"),
                           local, lambda t: t == out_th, whole=True)
            out.state, out.error_state, out.reason = "gone", exc.state, str(exc)
            return out
        return _cannot(store, out, str(exc), local, exc.state)
    except pairing.hub_errors() as exc:
        return _cannot(store, out, f"{exc.__class__.__name__}: {exc}", local)
    record = resolution.record
    if record.transcript != out_th or resolution.move_warnings:
        with _editing(store) as doc:
            if record.transcript != out_th:
                # Paired again under this name between the first look and the
                # resolution: the old pair's entries go, and the new pair
                # starts with nothing here.
                _identity(doc, name, record, local)
            _moved(doc, name, resolution.move_warnings, local)
    if not resolution.checked:
        return _cannot(store, out, resolution.warnings[0] if resolution.warnings
                       else "the pair room could not be read", local)
    if resolution.state != "paired":
        # Checked after the resolution, which is what confirms a responder:
        # its record says unconfirmed until it has read the initiator's proof.
        out.state, out.error_state = "unconfirmed", "unconfirmed"
        out.reason = (f"pair {name!r} is not confirmed yet, so nothing is shared over it "
                      f"(`switchboard pair wait {name}`)")
        return out
    try:
        with connect(pairing.pair_room_config(record, base),
                     agent_id=pairing.throwaway_agent()) as hub:
            for attempt in range(2):
                entries = hub.board_list(prefix=PREFIX)
                read_at = clock()
                try:
                    peer, own, skipped = _parse(entries, record)
                    with _editing(store) as doc:
                        writes = _apply(doc, name, record, peer, own, skipped,
                                        read_at=read_at, now=local,
                                        peer_active_at=resolution.peer_active_at)
                except PairingError:
                    raise  # the share store itself, which is every pair at once
                except Exception as exc:  # noqa: BLE001 - the peer's records cost this pair
                    raise _Unappliable(exc.__class__.__name__) from exc
                out.live = peer
                landed: list[_Write] = []
                failure: BaseException | None = None
                for write in writes:
                    try:
                        hub.board_set(_record_key(record.role, write.rid), write.value,
                                      ttl=RECORD_TTL, if_revision=write.revision)
                    except _hub_errors() as exc:
                        failure = exc
                        break
                    landed.append(write)
                out.wrote = out.wrote or bool(landed)
                if landed or failure is None:
                    with _editing(store) as doc:
                        for write in landed:
                            mine = doc["own"].setdefault(f"{name}/{write.rid}", {})
                            mine.update(th=record.transcript, at=write.value["at"])
                            if write.value.get("left"):
                                # From here the pair can read it (`_acknowledge`).
                                mine.setdefault("left_landed", local)
                        if failure is None:
                            # Over only now, with everything owed written: a
                            # room that reads but refuses every write is still
                            # out, and its one note per outage is still one.
                            doc["outage"].pop(name, None)
                if failure is None:
                    break
                if _is_conflict(failure) and attempt == 0:
                    # Another process here wrote this side's slot after our
                    # read. Its write came from the same local state; read
                    # again and recompute rather than write over it.
                    continue
                raise failure
    except _Unappliable as exc:
        return _cannot(store, out, f"its share records could not be applied ({exc}); "
                       "suspect whoever can write that pair room", local)
    except _hub_errors() as exc:
        status = getattr(exc, "status", None)
        _wake_if(out, resolution.config, connect)
        return _cannot(store, out, f"{exc.__class__.__name__}"
                       f"{f' {status}' if status else ''}: {exc}", local)
    _wake_if(out, resolution.config, connect)
    return out


def _wake_if(out: _Outcome, config: ClientConfig, connect: Connect) -> None:
    """Wake whoever is parked in the pair's work room — best effort.

    The body is the tag and nothing else, from an id used once: a listener
    here wakes on it too, and finds nothing new, which is the cheapest way
    to make a forged or self-made wake cost nothing.
    """
    if not out.wrote:
        return
    with contextlib.suppress(*_hub_errors()):
        with connect(config, agent_id=pairing.throwaway_agent()) as hub:
            hub.post(pairing.CHANNEL, {"t": pairing.TAG_SHARED})


def _targets(doc: dict[str, Any], pairs: list[str] | None, rooms: list[str] | None,
             names: Callable[[], list[str]]) -> list[str]:
    if pairs is not None:
        return list(dict.fromkeys(pairs))
    if rooms is not None:
        found: list[str] = []
        for name in rooms:
            room = doc["rooms"].get(name)
            if room is not None:
                found += [e["pair"] for e in room.get("list", [])]
            found += [e["pair"] for t in doc["tombstones"].values() if t.get("name") == name
                      for e in t.get("pairs", [])]
        return list(dict.fromkeys(found))
    referenced = {e["pair"] for r in doc["rooms"].values() for e in r.get("list", [])}
    referenced |= {o["pair"] for o in doc["offers"].values()}
    referenced |= {e["pair"] for t in doc["tombstones"].values() for e in t.get("pairs", [])}
    referenced |= {k.split("/", 1)[0] for k in doc["seen"]}
    try:
        present = set(names())
    except (PairingError, OSError):
        present = set()
    return sorted(present | referenced)


def _touch(base: ClientConfig | None, *, pairs: list[str] | None, rooms: list[str] | None,
           store: Store, pair_store: pairing.Store, connect: Connect,
           now: float | None) -> tuple[Touched, dict[str, _Outcome]]:
    store.require()
    local = _now(now)
    doc = _snapshot(store)
    seq = doc["seq"]
    outcomes: dict[str, _Outcome] = {}
    clock = _read_clock(now)
    for name in _targets(doc, pairs, rooms, pair_store.names):
        outcomes[name] = _touch_pair(name, base, store=store, pair_store=pair_store,
                                     connect=connect, now=now, clock=clock)
    touched = Touched()
    for name, out in outcomes.items():
        if out.state == "ok":
            touched.reached.append(name)
        else:
            touched.unreached[name] = out.reason
            touched.states[name] = out.state
        if out.wrote:
            touched.written.append(name)
    with _editing(store) as doc:
        reached, tried = set(touched.reached), set(outcomes)
        for room in doc["rooms"].values():
            listed = {e["pair"] for e in room.get("list", [])}
            if listed <= reached:
                room["refreshed_at"] = local
            if listed <= tried:
                # Tried, whether or not every pair answered: one pair that
                # stays unreachable must not make every command in the room
                # read all of them again, and wait out its timeout, each time.
                room["refresh_tried_at"] = local
        _prune(doc, local)
        touched.notes = [dict(n) for n in doc["notes"] if int(n.get("id") or 0) > seq]
    return touched, outcomes


def touch(base: ClientConfig | None = None, *, pairs: list[str] | None = None,
          rooms: list[str] | None = None, store: Store, pair_store: pairing.Store,
          connect: Connect = _default_connect, now: float | None = None) -> Touched:
    """Read and reconcile pairs: what every share command, the listener and the
    MCP sweep run.

    `pairs` names them; `rooms` means the pairs on those rooms' lists (and
    still owed a `left` for them); neither means every pair on this machine,
    since an offer can arrive over any of them. Fail-soft per pair: one pair
    whose room cannot be read is one note per outage, and costs only itself.
    """
    return _touch(base, pairs=pairs, rooms=rooms, store=store, pair_store=pair_store,
                  connect=connect, now=now)[0]


# --- new, accept, leave --------------------------------------------------------------


@dataclass
class Shared:
    """A room `share_new` minted, and exactly who it reached."""

    name: str
    workspace: str
    offered: list[str] = field(default_factory=list)
    #: Pairs on the list whose room could not be reached; the offer lands on
    #: a later touch.
    unreached: dict[str, str] = field(default_factory=dict)
    #: Pairs that were on `from_room`'s list and are not on this one: they
    #: left it, or the pair is over.
    dropped: list[str] = field(default_factory=list)
    from_room: str | None = None
    #: Leases this agent holds in `from_room`, which exclude nobody here.
    left_behind: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def room(self) -> str:
        return handle(self.name)

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "room": self.room, "workspace": self.workspace,
                "offered": list(self.offered),
                "unreached": [{"pair": p, "reason": r} for p, r in sorted(self.unreached.items())],
                "dropped": list(self.dropped), "from": self.from_room,
                "left_behind": list(self.left_behind), "warnings": list(self.warnings),
                "notes": [_note_view(n) for n in self.notes]}


def _pair_for_sharing(pair_store: pairing.Store, name: str, base: ClientConfig | None, *,
                      connect: Connect, now: float | None) -> PairRecord:
    """A pair this machine may share over, or the refusal with that pair's state.

    A responder's record reads unconfirmed until a resolution has seen the
    initiator's proof, so an unconfirmed one is resolved once before it is
    refused — and a pair that is over is refused with the state it ended in.
    """
    pairing.validate_name(name)
    try:
        record = pair_store.get(name)
    except OSError as exc:
        raise pairing.store_failure(exc) from exc
    if record is None:
        raise SharingError(f"no pair called {name!r} on this machine — `switchboard pair "
                           "status` lists them. Nothing was shared.", state="unknown")
    if not record.confirmed:
        try:
            resolution = pairing.resolve(name, base, store=pair_store, connect=connect,
                                         now=now)
        except PairingError as exc:
            raise SharingError(f"{exc} Nothing was shared.", state=exc.state) from exc
        record = resolution.record
        if resolution.state != "paired":
            raise SharingError(
                f"pair {name!r} is not confirmed yet, and nothing is shared over a pair "
                f"until it is: wait for it (`switchboard pair wait {name}`) and compare "
                "the SAS first. Nothing was shared.", state="unconfirmed")
    return record


def _room_config(room: dict[str, Any], base: ClientConfig | None) -> ClientConfig:
    """A held room as a `ClientConfig`, with the token rule of pairing.

    The offer's token if it carried one; else this environment's own, only
    when the room is on this environment's hub; else none. Never `token or
    config.token`: the URL came from another machine.
    """
    token, source = pairing.token_for(room.get("token"), room["url"], base)
    if room.get("token"):
        source = "shared"
    if base is None:
        return ClientConfig(url=room["url"], url_source="shared", token=token,
                            token_source=source, workspace=room["workspace"],
                            workspace_source="shared", key=room["key"],
                            write_key=room["write_key"], peer_log="", stash_db="")
    return replace(base, url=room["url"], url_source="shared", token=token,
                   token_source=source, workspace=room["workspace"],
                   workspace_source="shared", key=room["key"],
                   write_key=room["write_key"], room_problem=None)


def share_new(name: str, base: ClientConfig, *, pairs: list[str] | None = None,
              from_room: str | None = None, note: str = "", with_token: bool = False,
              store: Store, pair_store: pairing.Store, connect: Connect = _default_connect,
              agent_id: str | None = None, now: float | None = None) -> Shared:
    """Mint a write-protected room on `base`'s hub and offer it to a list of pairs.

    The list is `pairs` if given, else `from_room`'s current list — read
    *after* a touch of those pairs, so a `left` already written is honoured
    rather than offered a new room. The new list belongs to the new room;
    `from_room`'s list and records are never changed by it. The room is saved
    before any offer is written, so a hub that fails part-way loses nothing:
    every later touch writes what is still owed.
    """
    store.require()
    if base is None:
        raise SharingError("share_new needs the configuration of the hub to mint the "
                           "room on.", state="invalid")
    name = validate_name(name)
    note = _own_note(note)
    local = _now(now)
    doc = _snapshot(store)
    if name in doc["rooms"]:
        raise SharingError(f"this machine already holds a shared room called {name!r}; "
                           "choose another name.", state="name_taken")
    if from_room is not None and from_room not in doc["rooms"]:
        raise SharingError(f"no shared room called {from_room!r} is held here to take a "
                           "list from — `share status` lists them.", state="unknown")
    before: list[str] = []
    if pairs:
        chosen: list[dict[str, Any]] | None = []
        for pair in dict.fromkeys(pairs):
            record = _pair_for_sharing(pair_store, pair, base, connect=connect, now=now)
            chosen.append({"pair": pair, "th": record.transcript})
        targets = [e["pair"] for e in chosen]
    elif from_room is not None:
        chosen = None
        before = [e["pair"] for e in doc["rooms"][from_room].get("list", [])]
        targets = before
    else:
        raise SharingError("say who is offered the room: one or more pairs with --with "
                           "PAIR, or --from ROOM for that room's list.", state="invalid")

    first, outcomes = _touch(base, pairs=targets, rooms=None, store=store,
                             pair_store=pair_store, connect=connect, now=now)
    if chosen:
        for entry in chosen:
            out = outcomes[entry["pair"]]
            if out.state in ("gone", "unconfirmed"):
                raise SharingError(f"{out.reason}. Nothing was shared.",
                                   state=out.error_state or out.state)
    dropped: list[str] = []
    if chosen is None:
        # S1 is why the touch came first, so a pair it could not read is a
        # pair whose `left` may be sitting unread in its room: kept on, it is
        # handed the next room by the first touch that reaches it. And with no
        # add, leaving it off quietly is no answer either.
        unread = sorted(p for p in targets
                        if outcomes[p].state in ("unreachable", "unconfirmed"))
        if unread:
            why = "; ".join(f"{p}: {outcomes[p].reason}" for p in unread)
            raise SharingError(
                f"whether {', '.join(unread)} left {from_room} could not be read ({why}), "
                f"so {from_room}'s list cannot be taken as it stands. Nothing was shared: "
                "try again when that pair room can be reached, or name the pairs with "
                "--with.", state="unreachable")
        source = _snapshot(store)["rooms"].get(from_room or "")
        if source is None:
            raise SharingError(f"shared room {from_room!r} was left on this machine "
                               "meanwhile; nothing was shared.", state="unknown")
        chosen = [dict(e) for e in source.get("list", [])]
        dropped = sorted(set(before) - {e["pair"] for e in chosen})
    if not chosen:
        raise SharingError(f"nobody left to offer it to: every pair on {from_room}'s "
                           "list has left it or ended. Name pairs with --with.",
                           state="empty")

    from .crypto import generate_key
    from .writekey import RoomWriteKey, generate_write_key

    write_key = generate_write_key()
    workspace = RoomWriteKey.from_seed(write_key).workspace
    url = base.url.rstrip("/")
    room = {"rid": room_id(url, workspace), "url": url, "workspace": workspace,
            "key": generate_key(), "write_key": write_key,
            "token": base.token if with_token and base.token else None,
            "origin": "created", "list": chosen, "from": from_room, "note": note,
            "created_at": local, "last_used_at": local, "refreshed_at": None}
    taken = False
    with _editing(store) as doc:
        taken = name in doc["rooms"]
        if not taken:
            doc["rooms"][name] = room
    if taken:
        raise SharingError(f"another process on this machine took the name {name!r} "
                           "just now; nothing was shared.", state="name_taken")

    second, _ = _touch(base, pairs=[e["pair"] for e in chosen], rooms=None, store=store,
                       pair_store=pair_store, connect=connect, now=now)
    result = Shared(name=name, workspace=workspace, from_room=from_room, dropped=dropped,
                    offered=[e["pair"] for e in chosen if e["pair"] in second.reached],
                    unreached={e["pair"]: second.unreached[e["pair"]] for e in chosen
                               if e["pair"] in second.unreached},
                    notes=first.notes + second.notes)
    if result.unreached:
        result.warnings.append(
            f"not offered yet to {', '.join(sorted(result.unreached))}: their pair room "
            "could not be reached. The offer is written by the next touch that reaches "
            "it (`share status`, the listener, the MCP bridge).")
    if with_token and not base.token:
        result.warnings.append("--with-token: this environment has no hub token, so the "
                               "offer carries none.")
    if from_room is not None:
        result.left_behind = _leases_in(_snapshot(store)["rooms"].get(from_room), base,
                                        connect=connect, agent_id=agent_id)
        result.warnings.append(
            f"{from_room} runs on in parallel: claims, board entries and unread messages "
            f"there do not carry over to {name}, and nothing in {from_room} was revoked.")
        if result.left_behind:
            # A count, and where to look, because that is all there is: a
            # shared room is encrypted, so the hub lists each lease by a
            # blinded token, and the name never left this agent unsealed
            # (heal says the same). `claims` there shows each with its note.
            result.warnings.append(
                f"{len(result.left_behind)} lease(s) this agent holds in {from_room} stay "
                f"there and exclude nobody in {name}: claim again with {handle(name)} "
                f"what you still need (`--room {handle(from_room)} claims` lists them).")
    return result


def _leases_in(room: dict[str, Any] | None, base: ClientConfig | None, *,
               connect: Connect, agent_id: str | None) -> list[str]:
    """Leases this agent holds in a room — heal's `left_behind`, for a room
    that is not moving but being worked around."""
    if room is None or not agent_id:
        return []
    try:
        with connect(_room_config(room, base), agent_id=agent_id) as hub:
            mine = hub.agent_id
            return [str(lease.get("resource")) for lease in hub.leases()
                    if lease.get("holder") == mine]
    except _hub_errors():
        return []


@dataclass
class Kept:
    """A room `accept` took: under which name, and whether the sharer was told."""

    name: str
    pair: str
    #: The room was already held here: accepting put `pair` on its list.
    joined: bool = False
    #: Whether this side's record carrying the offer back reached the pair
    #: room — which is how the sharer sees "accepted".
    offered_back: bool = False
    warnings: list[str] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def room(self) -> str:
        return handle(self.name)

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "room": self.room, "pair": self.pair,
                "joined": self.joined, "offered_back": self.offered_back,
                "warnings": list(self.warnings),
                "notes": [_note_view(n) for n in self.notes]}


def _find_offer(doc: dict[str, Any], target: str) -> tuple[str, dict[str, Any]] | None:
    """The pending offer `target` names — `<name>` or `<pair>/<name>` — or None.

    Two pairs can each offer a room they call `core`. Choosing either would
    put this machine in a room it did not mean, so that is refused, listing
    both by the form that is not ambiguous.
    """
    if "/" in target:
        pair, _, name = target.partition("/")
        found = [(k, o) for k, o in doc["offers"].items()
                 if o.get("pair") == pair and o.get("name") == name]
    else:
        found = [(k, o) for k, o in doc["offers"].items() if o.get("name") == target]
    if len(found) > 1:
        names = ", ".join(sorted(f"{o['pair']}/{o['name']}" for _, o in found))
        raise SharingError(f"more than one pair offers a room called {target!r}: {names}. "
                           "Name the one you mean.", state="ambiguous")
    return found[0] if found else None


def accept(offer: str, base: ClientConfig | None = None, *, name: str | None = None,
           store: Store, pair_store: pairing.Store, connect: Connect = _default_connect,
           now: float | None = None) -> Kept:
    """Keep a room a pair offered, under its name here or `name`.

    Offers are kept without their keys, so the live offer is read again from
    the pair room; one that is gone is refused as `withdrawn`. The sharer
    goes on this room's list here, which makes this side's record to it
    carry the offer back — the sharer's "accepted", and what lets a fresh
    container of the sharer find the room again.
    """
    store.require()
    local = _now(now)
    found = _find_offer(_snapshot(store), offer)
    if found is None:
        # Not seen here yet: read first, rather than send the agent to run
        # `share status` for an offer that is sitting in a pair room.
        sender = offer.partition("/")[0] if "/" in offer else None
        touch(base, pairs=[sender] if sender else None, store=store,
              pair_store=pair_store, connect=connect, now=now)
    doc = _snapshot(store)
    found = _find_offer(doc, offer)
    if found is None:
        held = doc["rooms"].get(offer)
        raise SharingError(
            (f"{offer!r} is already held here — `--room {handle(offer)}` uses it."
             if held else f"no offer called {offer!r} is waiting on this machine — "
             "`share status` lists the offers (after reading every pair)."),
            state="unknown")
    key, pending = found
    pair, rid, th = pending["pair"], pending["rid"], pending["from"]["th"]
    record = _pair_for_sharing(pair_store, pair, base, connect=connect, now=now)
    if record.transcript != th:
        raise SharingError(f"pair {pair!r} now names a different pairing than the one "
                           "that made this offer; nothing was accepted.", state="withdrawn")
    held_name, held = _room_by_rid(doc, rid)
    local_name = held_name if held is not None else validate_name(name or pending["name"])
    if held is None and local_name in doc["rooms"]:
        raise SharingError(f"this machine already holds a shared room called "
                           f"{local_name!r}; accept this one under another name with "
                           "--name.", state="name_taken")

    first, outcomes = _touch(base, pairs=[pair], rooms=None, store=store,
                             pair_store=pair_store, connect=connect, now=now)
    out = outcomes[pair]
    if out.state != "ok":
        raise SharingError(f"could not read pair {pair}'s room to fetch the offer "
                           f"({out.reason}); nothing was accepted.",
                           state=out.error_state or out.state)
    live = out.live.get(rid)
    refused: SharingError | None = None
    joined = False
    with _editing(store) as doc:
        current = doc["offers"].get(key)
        if current is None or live is None or live.offer is None \
                or _unusable(live.offer) or _fp(live.offer) != current.get("fp"):
            refused = SharingError(
                f"{pair}'s offer of {pending['name']} is no longer there — withdrawn, "
                "lapsed, or the room was left. Nothing was accepted.", state="withdrawn")
        else:
            held_name, held = _room_by_rid(doc, rid)
            if held is not None:
                if not _has(held["list"], pair, th):
                    held["list"].append({"pair": pair, "th": th})
                local_name, joined = held_name or local_name, True
            elif local_name in doc["rooms"]:
                refused = SharingError(f"another process here took the name "
                                       f"{local_name!r} just now; accept with --name.",
                                       state="name_taken")
            else:
                offered = live.offer
                doc["rooms"][local_name] = {
                    "rid": rid, "url": str(offered["url"]).rstrip("/"),
                    "workspace": offered["workspace"], "key": offered["key"],
                    "write_key": offered["write_key"], "token": offered.get("token") or None,
                    "origin": {"pair": pair, "th": th, "their_name": live.name,
                               "note": live.note},
                    "list": [{"pair": pair, "th": th}], "from": None, "note": "",
                    "created_at": local, "last_used_at": local, "refreshed_at": None}
            if refused is None:
                del doc["offers"][key]
    if refused is not None:
        raise refused
    second, _ = _touch(base, pairs=[pair], rooms=None, store=store, pair_store=pair_store,
                       connect=connect, now=now)
    kept = Kept(name=local_name, pair=pair, joined=joined,
                offered_back=pair in second.reached, notes=first.notes + second.notes)
    if not kept.offered_back:
        kept.warnings.append(
            f"{pair} has not been told yet ({second.unreached.get(pair, 'unreachable')}); "
            "the next touch that reaches its pair room tells it.")
    kept.warnings.append(f"claims, board entries and messages in other rooms do not "
                         f"carry into {local_name}.")
    return kept


@dataclass
class Left:
    """A room or offer `leave` dropped, and which pairs know so far."""

    name: str
    kind: str  # room | offer
    told: list[str] = field(default_factory=list)
    #: Pairs owed the `left`, with why they have not got it: every touch
    #: writes it until each acknowledges.
    untold: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "told": list(self.told),
                "untold": [{"pair": p, "reason": r} for p, r in sorted(self.untold.items())],
                "warnings": list(self.warnings),
                "notes": [_note_view(n) for n in self.notes]}


def _tombstone(doc: dict[str, Any], rid: str, name: str, note: str,
               pairs: list[dict[str, Any]], now: float) -> dict[str, Any]:
    tomb = doc["tombstones"].get(rid) or {"name": name, "pairs": [], "told": []}
    tomb.update(name=name, note=note or tomb.get("note", ""), at=now,
                pairs=_union(tomb.get("pairs", []), pairs),
                told=_union(tomb.get("told", []), pairs))
    tomb.pop("done_at", None)
    doc["tombstones"][rid] = tomb
    return tomb


def leave(target: str, base: ClientConfig | None = None, *, note: str = "",
          store: Store, pair_store: pairing.Store, connect: Connect = _default_connect,
          now: float | None = None) -> Left:
    """Drop a held room here, or refuse an offer, and ask the pairs to drop this machine.

    The room goes from the store first, with a tombstone in its place, so a
    hub that is down costs only time: every touch writes `left` to each pair
    still owed it until that pair acknowledges. Told are the pairs on the
    room's list, and any other pair that offered the same room here. Running
    it again for a room already left retries the ones not told yet.

    `target` is a held room's name, a waiting offer's (`<name>` or
    `<pair>/<name>`), or `shared:<name>` for the held room alone. A bare name
    that is both a room held here and an offer of another room is refused,
    naming both forms: a peer can offer a room under any name, and "refuse
    that offer" must not drop the room — for one this machine created and
    nobody accepted yet, the only copy of its keys.
    """
    store.require()
    note = _own_note(note)
    local = _now(now)
    kind = "room"
    handled = parse_handle(target)
    room_name = handled if handled is not None else target
    with _editing(store) as doc:
        if room_name in doc["rooms"]:
            if handled is None:
                rid = doc["rooms"][room_name]["rid"]
                others = sorted(f"{o['pair']}/{o['name']}" for o in doc["offers"].values()
                                if o.get("name") == target and o.get("rid") != rid)
                if others:
                    raise SharingError(
                        f"{target!r} is both a room held here and an offer of another "
                        f"room waiting from {', '.join(others)}. Name the one you mean: "
                        f"{handle(target)} for the room, {' or '.join(others)} for the "
                        "offer. Nothing was left.", state="ambiguous")
            room = doc["rooms"].pop(room_name)
            rid = room["rid"]
            offered_here = [o["from"] for o in doc["offers"].values() if o.get("rid") == rid]
            still_offering = [{"pair": k.split("/")[0], "th": s["th"]}
                              for k, s in doc["seen"].items()
                              if k.split("/")[1] == rid and s.get("kind") == "offer"]
            for key in [k for k, o in doc["offers"].items() if o.get("rid") == rid]:
                del doc["offers"][key]
            tomb = _tombstone(doc, rid, room_name, note,
                              _union(room.get("list", []), offered_here, still_offering),
                              local)
            name = room_name
        else:
            found = None if handled is not None else _find_offer(doc, target)
            if found is not None:
                key, pending = found
                del doc["offers"][key]
                rid, name, kind = pending["rid"], pending["name"], "offer"
                tomb = _tombstone(doc, rid, name, note, [pending["from"]], local)
            else:
                again = [(r, t) for r, t in doc["tombstones"].items()
                         if t.get("name") == room_name]
                if not again:
                    raise SharingError(f"no shared room or offer called {target!r} on this "
                                       "machine — `share status` lists them.",
                                       state="unknown")
                rid, tomb = again[0]
                name = room_name
    owed = [e["pair"] for e in tomb.get("pairs", [])]
    touched, _ = _touch(base, pairs=owed, rooms=None, store=store, pair_store=pair_store,
                        connect=connect, now=now)
    result = Left(name=name, kind=kind, notes=touched.notes,
                  told=[p for p in owed if p in touched.reached],
                  untold={p: touched.unreached[p] for p in owed if p in touched.unreached})
    if result.untold:
        result.warnings.append(
            f"{', '.join(sorted(result.untold))} not told yet: `left` is written to each "
            "on every touch until it acknowledges, and until then its next `share new "
            f"--from` could still include this machine.")
    if kind == "room":
        result.warnings.append(f"nothing was revoked: everyone who holds {name} still "
                               "does, and this machine keeps no copy of it.")
    return result


# --- status, handles, the listener ------------------------------------------------


def status(base: ClientConfig | None = None, *, name: str | None = None, store: Store,
           pair_store: pairing.Store, connect: Connect = _default_connect,
           now: float | None = None) -> dict[str, Any]:
    """Every room held, offer waiting and `left` still owed, after a touch.

    Each list says who has the room *per the live records*: a pair is shown
    as having it while its own record carries the offer back. Every note of
    the last seven days is included, every time — there is no "seen" flag,
    because several sessions share this store and the first to look would
    hide them from the rest. No key, write key or token appears in it.
    """
    store.require()
    local = _now(now)
    doc = _snapshot(store)
    named = name is not None and (name in doc["rooms"] or any(
        t.get("name") == name for t in doc["tombstones"].values()))
    touched = touch(base, rooms=[name] if named else None, store=store,
                    pair_store=pair_store, connect=connect, now=now)
    with _editing(store) as doc:
        if name is not None and name in doc["rooms"]:
            doc["rooms"][name]["last_used_at"] = local
        _prune(doc, local)
        view = _view(doc, name)
    view["unreached"] = [{"pair": p, "state": touched.states.get(p), "reason": r}
                         for p, r in sorted(touched.unreached.items())]
    return view


def _view(doc: dict[str, Any], name: str | None) -> dict[str, Any]:
    rooms = []
    for room_name, room in sorted(doc["rooms"].items()):
        if name is not None and room_name != name:
            continue
        rid = room["rid"]
        origin = room.get("origin")
        rooms.append({
            "name": room_name, "room": handle(room_name), "workspace": room["workspace"],
            "url": room["url"],
            "origin": ({"pair": origin.get("pair"), "name": origin.get("their_name")}
                       if isinstance(origin, dict) else "created"),
            "from": room.get("from"), "note": room.get("note", ""),
            "token_carried": bool(room.get("token")),
            "created_at": pairing.iso(room.get("created_at")),
            "last_used_at": pairing.iso(room.get("last_used_at")),
            "refreshed_at": pairing.iso(room.get("refreshed_at")),
            "list": [{"pair": e["pair"],
                      "has": (_seen(doc, e["pair"], rid) or {}).get("kind") == "offer",
                      "offered": f"{e['pair']}/{rid}" in doc["own"]
                      and not doc["own"][f"{e['pair']}/{rid}"].get("left")}
                     for e in room.get("list", [])],
        })
    offers = []
    for offer in sorted(doc["offers"].values(), key=lambda o: (o["name"], o["pair"])):
        if name is not None and offer["name"] != name:
            continue
        held, _ = _room_by_rid(doc, offer["rid"])
        offers.append({"offer": f"{offer['pair']}/{offer['name']}", "pair": offer["pair"],
                       "name": offer["name"], "note": offer.get("note", ""),
                       "at": pairing.iso(offer.get("at")), "held": held})
    left = [{"name": t["name"], "waiting": sorted(e["pair"] for e in t.get("pairs", [])),
             "at": pairing.iso(t.get("at")), "note": t.get("note", "")}
            for t in sorted(doc["tombstones"].values(), key=lambda t: t["name"])
            if t.get("pairs") and (name is None or t["name"] == name)]
    return {"rooms": rooms, "offers": offers, "left": left,
            "notes": [_note_view(n) for n in doc["notes"]]}


@dataclass
class SharedRoom:
    """A `shared:<name>` handle, resolved from the store on this use."""

    name: str
    config: ClientConfig = field(repr=False)
    #: The room's records were last refreshed over a day ago (or never), and
    #: no refresh was tried in the last REFRESH_RETRY: the caller should run
    #: `touch(rooms=[name])` so a machine that only ever works in the room
    #: still keeps its records alive and reads its peers'.
    refresh_due: bool = False
    pairs: list[str] = field(default_factory=list)

    @property
    def room(self) -> str:
        return handle(self.name)

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "room": self.room, "workspace": self.config.workspace,
                "url": self.config.url, "pairs": list(self.pairs),
                "refresh_due": self.refresh_due}


def resolve_room(name: str, base: ClientConfig | None = None, *, store: Store,
                 now: float | None = None) -> SharedRoom:
    """The room `shared:<name>` names right now, from the store, with no hub call.

    Read on every use rather than cached, because a terminal on this machine
    can leave the room, or accept another under the same name, while a
    bridge or listener is running. Stamps `last_used_at`, which is how a
    listener knows to park there.
    """
    store.require()
    local = _now(now)
    with _editing(store) as doc:
        room = doc["rooms"].get(name)
        if room is not None:
            room["last_used_at"] = local
            room = dict(room)
        left = any(t.get("name") == name for t in doc["tombstones"].values())
        offered = any(o.get("name") == name for o in doc["offers"].values())
    if room is None:
        if left:
            raise SharingError(f"this machine left shared room {name!r}; it is not held "
                               "here any more.", state="left")
        if offered:
            raise SharingError(f"shared room {name!r} is offered to this machine but not "
                               f"accepted: `share accept {name}` keeps it.", state="offered")
        raise SharingError(f"no shared room called {name!r} on this machine — `share "
                           "status` lists them.", state="unknown")
    refreshed, tried = room.get("refreshed_at"), room.get("refresh_tried_at")
    due = (refreshed is None or local - float(refreshed) > REFRESH) \
        and (tried is None or local - float(tried) > REFRESH_RETRY)
    return SharedRoom(name=name, config=_room_config(room, base), refresh_due=due,
                      pairs=[e["pair"] for e in room.get("list", [])])


def recent_rooms(base: ClientConfig | None = None, *, store: Store,
                 within: float = RECENT, now: float | None = None
                 ) -> list[tuple[str, ClientConfig]]:
    """Shared rooms used here lately, for a listener to park in.

    Fail-soft, like `pairing.active`: an unreadable store parks nowhere
    rather than stopping the listener. Does not stamp `last_used_at` —
    parking is not use, or a listener would keep every room recent forever.
    """
    local = _now(now)
    try:
        doc = _snapshot(store)
    except (PairingError, OSError):
        return []
    return [(name, _room_config(room, base)) for name, room in sorted(doc["rooms"].items())
            if local - float(room.get("last_used_at") or 0) <= within]


def active_pairs(*, store: Store, pair_store: pairing.Store | None = None) -> list[str]:
    """Pairs a listener counts as active: on a held room's list, or offering one.

    With `pair_store`, an entry whose pair is gone or now another pairing is
    left out — it would only be dropped by the next touch anyway.
    """
    try:
        doc = _snapshot(store)
    except (PairingError, OSError):
        return []
    entries = [e for r in doc["rooms"].values() for e in r.get("list", [])]
    entries += [o["from"] for o in doc["offers"].values()]
    names: set[str] = set()
    for entry in entries:
        if pair_store is not None:
            try:
                record = pair_store.get(entry["pair"])
            except (PairingError, OSError):
                record = None
            if record is None or record.transcript != entry["th"]:
                continue
        names.add(entry["pair"])
    return sorted(names)


def notes(*, store: Store, after: int | None = None,
          now: float | None = None) -> list[dict[str, Any]]:
    """The notes of the last seven days, oldest first; `after` an id, only newer.

    Each has an `id` that only grows, so a surface that must say each note
    once — the MCP bridge's `share_notes` — tracks the last id it returned,
    in memory, instead of the store keeping a "seen" flag for everyone.
    """
    local = _now(now)
    doc = _snapshot(store)
    return [_note_view(n) for n in doc["notes"]
            if local - float(n.get("at") or 0) <= NOTE_KEEP
            and (after is None or int(n.get("id") or 0) > after)]
