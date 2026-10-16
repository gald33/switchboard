"""Pairing: two machines, one code shared once, and rooms neither ever sends.

The problem this solves is the one an invite cannot. An invite (`invite.py`)
is the room itself — hub, token and key in one string — so it is exactly as
secret as the room for as long as the room lives. That is fine for a room
that lives an afternoon. It is the wrong shape for two machines that will
work together for weeks, because the string has to be carried somewhere
first: a chat, an email, a ticket. Whatever carried it keeps it, and anyone
who reads that paper trail later holds the room.

So pairing uses three throwaway rooms, none of them the team key's:

1. **The handshake room.** Its code is the one thing a human carries, so it is
   the thing most likely to be disclosed later. It is short-lived, and it
   carries nothing that would help whoever finds it.
2. **The pair room.** Long-lived, and used for nothing but keeping the pair
   in step: which work room is current, and whether either side has left.
3. **The work room.** Where the two sides actually coordinate. When its key is
   exposed, the pair room heals the connection by moving both sides to a new
   one — `heal`.

Three decisions shape all of it.

**The pair secret is agreed, never sent.** The code carries a random
handshake secret `S` and the initiator's *ephemeral* X25519 public key. The
responder answers with its own ephemeral public key, sealed under a key
derived from `S`, and both sides compute the pair secret from the
Diffie-Hellman result with `S` mixed in as a pre-shared key — the shape of
Noise `NNpsk0`, built from the primitives `crypto.py` already uses. Both
ephemeral private keys are then dropped. A later leak of the code *and* the
hub's entire history yields two public keys and nothing else: the hub here is
untrusted by construction (`docs/model.md`: encryption is the boundary), so
"the handshake message was short-lived" would be a promise from the hub,
not a property. This makes it one.

**Every room is derived.** The handshake room from `S`; the pair room from
the pair secret `P`; work room *n* from `P` and *n*. Nothing has to be minted
or transmitted to create a room, so both sides are in work room 0 the moment
they hold `P`, without waiting on each other, and a heal is one integer
rather than a new key. Every room is write-protected (`ws_…`,
`writekey.py`), so knowing a room's identifier — which the hub does — is not
enough to write into it.

**Everything a side says, it says about itself.** The pair room holds one
sealed record per side — its generation, whether it has ended the pair, and
for the initiator the proof that it completed the handshake. Each side only
ever writes its own, so there is nothing to compare-and-set and nothing to
race: the current work room is the highest generation either side has
written, a heal is writing a higher one, and the other side's record moving
up *is* its acknowledgement.

What the handshake promises, and what it cannot:

- A code is a **live bearer credential** until the initiator collects the
  answer or the code expires. Whoever holds it in that window can answer in
  place of the intended peer. That is detected rather than prevented — the
  answer channel is append-only, two different answers abort the pairing on
  both sides, and a late answerer is told the code was already used — and
  the initiator's only defence against losing the race outright is comparing
  the short authentication string (`sas`) with the other side.
- The hub can withhold, delay, replay and refuse. It cannot read anything, or
  forge anything a pair secret holder would accept; it *can* serve a stale
  record, which is why a heal is acknowledged rather than assumed.
- A heal rescues a work room whose key leaked. It does nothing for a machine
  whose pair secret leaked: every generation derives from `P`. That is the
  extension point the record's `s` (strategy) field exists for.

See `docs/pairing.md` for the walk-through and `docs/model.md` for where this
sits in the model. **The derivation labels below are frozen** for protocol 1:
two machines that paired on one release must keep landing in the same rooms
after either upgrades, so a change is a new protocol with its own prefix,
never an edit here.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .config import MAX_BOARD_TTL, MAX_MESSAGE_TTL, ClientConfig
from .crypto import CryptoError

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
    )
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    AVAILABLE = True
except ImportError:  # pragma: no cover - cryptography is a base dependency
    AVAILABLE = False

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no advisory locks
    fcntl = None  # type: ignore[assignment]

#: The one protocol this module speaks. It rides in the code, the store
#: records and the export, so an older client refuses a newer pair loudly
#: instead of deriving rooms nobody else is in.
PROTOCOL = 1

#: What the human carries. Deliberately *not* `swb1_`: `Invite.decode` ignores
#: fields it does not know, so a pairing code dressed as an invite would be
#: joined as an ordinary room by every older surface — the viewer, `join`,
#: the hosted bridge — and the handshake secret would sit in each of them.
CODE_PREFIX = "swbp1_"
#: A pair secret as `pair export` prints it. Its own prefix, unlike the code's
#: in more than one letter, because this one must never be pasted into a
#: conversation and the code is pasted into nothing else.
SECRET_PREFIX = "swbsecret1_"

#: How long a code can be redeemed, by default and at most. Short by default
#: because the code is the part expected to leak; the ceiling leaves room for
#: a human who is carrying it across time zones.
DEFAULT_TTL = 900
MIN_TTL = 60
MAX_TTL = 12 * 3600
#: How long after a code stops being redeemable the initiator may still
#: *collect* an answer that arrived in time. Agents are turn-based: the
#: initiator printed a code and ended its turn, and nothing promises it runs
#: again within minutes. A frozen constant rather than a field, so the
#: responder can compute the same deadline from the code alone.
COLLECT_WINDOW = 12 * 3600
#: Clock disagreement tolerated where two machines compare times.
SKEW = 120
#: How far past its expiry a responder will still *try* a code, by its own
#: clock. Lenient on purpose: the verdict that counts is the hub's timestamp
#: on the answer, which both sides read the same way, and a responder whose
#: clock runs fast must not refuse a code the initiator would accept.
LOCAL_SLACK = 900

#: The channel every pairing message travels on, in every room. Fixed, so a
#: listener parked in a work room wakes for the other side without either
#: knowing the other's agent id — which, for a cloud session, changes every
#: session.
CHANNEL = "pair"

#: A side record lives as long as the hub allows, and is rewritten at most
#: once a day while in use, so it outlives any gap shorter than a week.
SIDE_TTL = MAX_BOARD_TTL
SIDE_REFRESH = 86400
SIDE_PREFIX = "coord/pair/side/"
#: No sign of the other side for this long, and its record gone from the hub:
#: the pair room has forgotten what generation it was on. See `_lapse`.
STALE_AFTER = 6 * 86400
#: How far a lapsed side jumps. Far enough that no run of single-step heals by
#: the other side, in the week the hub forgot, could have reached it — so the
#: room jumped to is guaranteed never to have been retired.
LAPSE_JUMP = 1 << 20

#: The healing strategies this release understands. A record naming another
#: one is refused rather than guessed at, which is what lets a later release
#: add one without an older peer silently downgrading it.
STRATEGIES = ("derive",)

INITIATOR = "initiator"
RESPONDER = "responder"
ROLES = (INITIATOR, RESPONDER)

#: Where pairs are kept: one directory, owner-only, per machine.
DEFAULT_DIR = "~/.switchboard/pairs"
#: Overrides the directory; the empty string disables pairing on this machine.
ENV = "SWITCHBOARD_PAIRS"
#: A pair carried in the environment instead, for a container that does not
#: keep its home directory: `SWITCHBOARD_PAIR_<NAME>=swbsecret1_…`.
ENV_PAIR_PREFIX = "SWITCHBOARD_PAIR_"

#: Room handles: `pair:<name>`, the same string on every surface.
HANDLE_PREFIX = "pair:"

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

# Sealed value tags. Bound inside each value because the board's AEAD context
# is the same for every key in a room, so without them a hub could serve one
# record where another was asked for and it would open.
_T_HELLO = "swbp1/hello"
_T_ANSWER = "swbp1/answer"
_T_CONSUMED = "swbp1/consumed"
_T_ABORTED = "swbp1/aborted"
_T_SIDE = "swbp1/side"
_T_PAIRED = "swbp1/paired"
_T_MOVED = "swbp1/moved"
_T_ENDED = "swbp1/ended"

# --- frozen derivation labels (protocol 1) ----------------------------------
_L = "switchboard/pair/v1/"
_L_PSK = (_L + "psk").encode()
_L_HS_WRITE = (_L + "handshake/write").encode()
_L_HS_KEY = (_L + "handshake/key").encode()
_L_TRANSCRIPT = (_L + "transcript").encode()
_L_PAIR = (_L + "pair").encode()
_L_CONFIRM = (_L + "confirm").encode()
_L_SAS = (_L + "sas").encode()
_L_COMMIT = (_L + "commit").encode()
_L_NAME = (_L + "name").encode()
_L_PAIR_WRITE = (_L + "pair-room/write").encode()
_L_PAIR_KEY = (_L + "pair-room/key").encode()
_L_WORK_WRITE = (_L + "work-room/write").encode()
_L_WORK_KEY = (_L + "work-room/key").encode()


class PairingError(Exception):
    """A pairing that cannot proceed, said as a sentence with the way out.

    Not a `ValueError`, on purpose: the MCP bridge reports a stray
    `ValueError` as an unknown tool, and a pairing refusal is never that.
    `state` is the machine-readable half, for surfaces that return data.
    """

    def __init__(self, message: str, *, state: str | None = None) -> None:
        super().__init__(message)
        self.state = state


class AlreadyRedeemed(PairingError):
    """Somebody other than this caller answered the code.

    Its own class because it is the one refusal that means *the code leaked*
    rather than *try again*, and a surface should say so differently — the
    CLI exits with its conflict code, not its generic one.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, state="already_redeemed")


#: What a hub call can raise that means "the hub, not the protocol". Imported
#: lazily in `_hub_errors` so this module stays importable without the client.
def _hub_errors() -> tuple[type[BaseException], ...]:
    from .client import SwitchboardError

    return (SwitchboardError, OSError, httpx.HTTPError)


def hub_errors() -> tuple[type[BaseException], ...]:
    """The exceptions that mean *the hub*, not the protocol.

    Public so a surface that fails soft on the hub — a listener, the MCP
    bridge's background collection — catches exactly what this module does,
    rather than a second list that drifts from it.
    """
    return _hub_errors()


# --- encoding and primitives ------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _need_crypto() -> None:
    if not AVAILABLE:  # pragma: no cover
        raise PairingError("pairing needs the `cryptography` package")


def _hkdf(ikm: bytes, info: bytes, *, salt: bytes | None = None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(ikm)


def _mac(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"\x00".join(parts), hashlib.sha256).digest()


def _x25519_public(private: Any) -> bytes:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def _x25519_raw(private: Any) -> bytes:
    """The one place this package serializes a private key it generated.

    Only the initiator's *ephemeral* handshake key, and only because a
    turn-based initiator must survive its own process ending between printing
    the code and collecting the answer. `signing.py`'s keys are never
    serialized; that rule is about attribution and still holds.
    """
    return private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _exchange(private: Any, public: bytes) -> bytes:
    """X25519, with the low-order-point refusal turned into a sentence.

    `cryptography` already raises on an all-zero shared secret, which is what
    a small-subgroup public key produces; this only stops that surfacing as a
    bare `ValueError` an MCP caller would read as an unknown tool.
    """
    try:
        return private.exchange(X25519PublicKey.from_public_bytes(public))
    except ValueError as exc:
        raise PairingError(
            "the other side's handshake key is not a usable X25519 key — the "
            "answer was tampered with or written by something that is not a "
            "switchboard client. Nothing was paired."
        ) from exc


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _hub_time(stamp: Any) -> float | None:
    """A hub timestamp (`created_at`, ISO 8601) as unix seconds, if it parses."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clamp_ttl(seconds: float, ceiling: float) -> float:
    return float(max(60.0, min(ceiling, seconds)))


def validate_name(name: str) -> str:
    """A pair's local name, or a refusal saying what a name may be.

    Lowercase letters, digits and dashes, starting with a letter or digit, at
    most 32 characters: it becomes a file name, an environment variable
    suffix and part of a `pair:<name>` handle, and has to be safe as all
    three. Validated on both sides, because the responder's default comes
    from a code somebody else made.
    """
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise PairingError(
            f"{name!r} is not a usable pair name: use lowercase letters, digits "
            "and dashes, starting with a letter or digit, at most 32 characters."
        )
    return name


def handle(name: str) -> str:
    """The room handle for a pair's current work room: `pair:<name>`."""
    return HANDLE_PREFIX + name


def parse_handle(text: str | None) -> str | None:
    """The pair name in a `pair:<name>` handle, or None for anything else."""
    if isinstance(text, str) and text.startswith(HANDLE_PREFIX):
        return text[len(HANDLE_PREFIX):]
    return None


def env_var_for(name: str) -> str:
    """`SWITCHBOARD_PAIR_<NAME>` for a pair name (dashes become underscores)."""
    return ENV_PAIR_PREFIX + name.upper().replace("-", "_")


# --- the rooms ----------------------------------------------------------------


@dataclass(frozen=True)
class RoomSpec:
    """One derived room: everything a client needs, chosen together.

    The workspace token is the room's write key's public half and the key is
    derived beside it from the same secret, so — rule 2 of `docs/model.md` —
    the identifier and the key cannot disagree.
    """

    workspace_token: str
    workspace: str
    key: str = field(repr=False)
    write_key: str = field(repr=False)


def _room(seed: bytes, key: bytes) -> RoomSpec:
    from .writekey import RoomWriteKey

    write_key = _b64e(seed)
    writer = RoomWriteKey.from_seed(write_key)
    return RoomSpec(writer.workspace_token, writer.workspace, _b64e(key), write_key)


def handshake_room(secret: bytes) -> RoomSpec:
    """The room a code names. Anyone holding the code holds this room."""
    _need_crypto()
    return _room(_hkdf(secret, _L_HS_WRITE), _hkdf(secret, _L_HS_KEY))


def pair_room(pair_secret: bytes) -> RoomSpec:
    """The room only the two paired machines hold, for keeping in step."""
    _need_crypto()
    return _room(_hkdf(pair_secret, _L_PAIR_WRITE), _hkdf(pair_secret, _L_PAIR_KEY))


def work_room(pair_secret: bytes, generation: int) -> RoomSpec:
    """Work room `generation`. One-way from the pair secret: holding room *n*'s
    key says nothing about room *n+1*'s, which is what a heal relies on."""
    _need_crypto()
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise PairingError(f"invalid work-room generation {generation!r}")
    n = str(generation).encode()
    return _room(_hkdf(pair_secret, _L_WORK_WRITE + b"\x00" + n),
                 _hkdf(pair_secret, _L_WORK_KEY + b"\x00" + n))


def default_name(secret: bytes) -> str:
    """The name both sides give a pair unless told otherwise.

    Derived from the handshake secret, so the initiator and the responder
    arrive at the same one without agreeing it — which means the human
    relaying between two agents has one name to say for the pair, not two.
    """
    digest = hashlib.sha256(_L_NAME + b"\x00" + secret).digest()
    return "pair-" + base64.b32encode(digest).decode()[:6].lower()


def commitment(reveal: bytes) -> bytes:
    """What the code carries in place of the initiator's SAS nonce."""
    return hashlib.sha256(_L_COMMIT + b"\x00" + reveal).digest()[:16]


def transcript(initiator: bytes, responder: bytes, commit: bytes) -> bytes:
    """Everything the pair secret is bound to, in a fixed order."""
    return hashlib.sha256(
        b"\x00".join((_L_TRANSCRIPT, initiator, responder, commit))).digest()


def derive_pair_secret(shared: bytes, secret: bytes, th: bytes) -> bytes:
    """`P`: the Diffie-Hellman output, with the code's secret as the salt.

    The salt is what makes the exchange *authenticated*: without `S` nobody can
    produce an answer whose derived secret matches the initiator's, so the hub
    — or anyone else who can see the handshake room's traffic — cannot
    substitute its own key.
    """
    return _hkdf(shared, _L_PAIR + b"\x00" + th, salt=_hkdf(secret, _L_PSK))


def confirmation(pair_secret: bytes, role: str, th: bytes) -> bytes:
    """Proof that `role` derived this pair secret over this transcript."""
    return _mac(_hkdf(pair_secret, _L_CONFIRM), role.encode(), th)


def sas(pair_secret: bytes, th: bytes, reveal: bytes) -> str:
    """The short authentication string, `NNNN-NNNN`: about 26.6 bits.

    Both sides show it; comparing them is how the initiator can tell whether
    the side it paired with is the side the human meant. The reveal is the
    initiator's nonce, committed to in the code and disclosed only once the
    answer is fixed, so a responder racing for the code cannot grind its key
    until its string matches somebody else's.
    """
    digest = hmac.new(pair_secret, b"\x00".join((_L_SAS, th, reveal)),
                      hashlib.sha256).digest()
    n = int.from_bytes(digest[:4], "big") % 10**8
    return f"{n // 10**4:04d}-{n % 10**4:04d}"


# --- the code -----------------------------------------------------------------


@dataclass(frozen=True)
class PairingCode:
    """The one string a human carries, and everything the responder needs.

    Treat it as disclosed the moment it is written down. What it carries is
    chosen so that disclosure after the pairing costs nothing: the handshake
    secret opens only the handshake room, and the handshake room holds only
    public keys.
    """

    url: str
    secret: bytes = field(repr=False)
    initiator: bytes
    expires_at: int
    commit: bytes
    note: str = ""
    #: The hub token, only when the initiator asked for it to travel
    #: (`--with-token`). A code is the part expected to end up in a ticket,
    #: and a hub token that is a real secret would end up there with it.
    token: str | None = field(default=None, repr=False)
    #: The pair's name, when the initiator chose one, so both sides call it
    #: the same thing and the human relaying between them has one name to
    #: say. Validated again on the way in: it comes from somebody else.
    name: str = ""
    #: Whether the initiator left a hello in the handshake room. When it did,
    #: a responder that cannot find it is on the wrong hub, and says so
    #: instead of answering into a room the initiator will never read.
    hello: bool = False

    def encode(self) -> str:
        payload: dict[str, Any] = {
            "v": PROTOCOL, "u": self.url, "s": _b64e(self.secret),
            "e": _b64e(self.initiator), "x": int(self.expires_at),
            "c": _b64e(self.commit), "p": 1 if self.hello else 0,
        }
        if self.note:
            payload["n"] = self.note
        if self.name:
            payload["m"] = self.name
        if self.token:
            payload["t"] = self.token
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return CODE_PREFIX + _b64e(raw)

    @classmethod
    def decode(cls, blob: str) -> PairingCode:
        text = (blob or "").strip()
        if text.startswith(SECRET_PREFIX):
            raise PairingError(
                "this is a pair's long-lived secret from `pair export`, not a "
                "pairing code — and having been pasted here it should be "
                "treated as disclosed: run `switchboard pair forget <name>` on "
                "both machines and pair again."
            )
        if text.startswith("swb1_"):
            raise PairingError(
                "this is a room invite, not a pairing code — open it with "
                "`switchboard join <invite>` or `--invite <invite>`."
            )
        if text.startswith("swbp") and not text.startswith(CODE_PREFIX):
            raise PairingError(
                "this pairing code was made by a newer switchboard; upgrade "
                "agent-switchboard on this machine to redeem it."
            )
        if not text.startswith(CODE_PREFIX):
            raise PairingError(
                f"not a pairing code (expected it to start with {CODE_PREFIX!r}). "
                "Codes come from `switchboard pair start` and are pasted whole."
            )
        try:
            payload = json.loads(_b64d(text[len(CODE_PREFIX):]))
        except (ValueError, TypeError) as exc:
            raise PairingError(f"pairing code is corrupt or truncated ({exc})") from exc
        if not isinstance(payload, dict) or payload.get("v") != PROTOCOL:
            raise PairingError(
                "pairing code is not protocol 1 — made by a different "
                "switchboard release; upgrade rather than editing it."
            )
        try:
            url = payload["u"]
            secret = _b64d(payload["s"])
            initiator = _b64d(payload["e"])
            commit = _b64d(payload["c"])
            expires = payload["x"]
        except (KeyError, TypeError, ValueError) as exc:
            raise PairingError("pairing code is missing a field; paste it whole") from exc
        if (not isinstance(url, str) or not url or len(secret) != 32
                or len(initiator) != 32 or len(commit) != 16
                or not isinstance(expires, int) or isinstance(expires, bool)):
            raise PairingError("pairing code is malformed; paste it whole")
        note = payload.get("n") or ""
        token = payload.get("t") or None
        name = payload.get("m") or ""
        if name:
            validate_name(name)
        return cls(url=url.rstrip("/"), secret=secret, initiator=initiator,
                   expires_at=expires, commit=commit,
                   note=note if isinstance(note, str) else "",
                   token=token if isinstance(token, str) else None,
                   name=name, hello=bool(payload.get("p")))

    @property
    def pair_name(self) -> str:
        """What both sides call this pair unless the responder says otherwise."""
        return self.name or default_name(self.secret)

    @property
    def deadline(self) -> float:
        """Until when the initiator may collect, by the hub's clock."""
        return self.expires_at + COLLECT_WINDOW

    def redacted(self) -> str:
        return (f"pairing code for hub={self.url}, redeemable until "
                f"{_iso(self.expires_at)}, token={'carried' if self.token else 'not carried'}")


def looks_like_code(text: str | None) -> bool:
    return isinstance(text, str) and text.strip().startswith(CODE_PREFIX)


def looks_like_secret(text: str | None) -> bool:
    return isinstance(text, str) and text.strip().startswith(SECRET_PREFIX)


def iso(ts: float | None) -> str:
    """A pairing deadline as local time, the one way every surface shows it."""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


_iso = iso


# --- what each machine keeps ---------------------------------------------------


@dataclass
class PairRecord:
    """One side of one pair, as this machine keeps it.

    `secret` is the pair secret itself — the one long-lived value pairing
    creates. It never reaches the hub, a log, a repr or the conversation;
    `pair export` is the single, deliberate way out.
    """

    name: str
    role: str
    secret: str = field(repr=False)
    transcript: str
    url: str
    generation: int = 0
    #: A hub token, only when it arrived in the code. Otherwise the token is
    #: whatever the environment holds for this hub at the time of use, like
    #: every other room's.
    token: str | None = field(default=None, repr=False)
    confirmed: bool = False
    sas: str | None = None
    #: The initiator's commitment, kept by the responder to check the reveal.
    commit: str | None = None
    #: The initiator's reveal, kept by the initiator to publish it.
    reveal: str | None = field(default=None, repr=False)
    created_at: float = 0.0
    #: Until when the initiator can collect. The responder's pairing is dead
    #: if it is still unconfirmed past this.
    deadline: float = 0.0
    peer_seen_at: float | None = None
    peer_generation: int | None = None
    last_used_at: float | None = None
    note: str = ""
    peer_note: str = ""
    #: The responder's answer, kept until the hub has it, so a retry resends
    #: exactly what was derived instead of minting a key that no longer
    #: matches the saved secret.
    answer: dict[str, Any] | None = None
    sent: bool = True
    #: The responder keeps the code's secret only while unconfirmed, to read
    #: the handshake channel for the initiator's verdict — the code is the part
    #: expected to leak anyway — and drops it once confirmed.
    handshake: str | None = field(default=None, repr=False)
    code_expires_at: float | None = None
    strategy: str = "derive"
    v: int = PROTOCOL
    #: Where this record came from: the store, or an environment variable.
    #: Not written to disk.
    source: str = field(default="store", compare=False)
    env_generation: int | None = field(default=None, compare=False)

    @property
    def pair_secret(self) -> bytes:
        return _b64d(self.secret)

    @property
    def th(self) -> bytes:
        return _b64d(self.transcript)

    @property
    def peer_role(self) -> str:
        return RESPONDER if self.role == INITIATOR else INITIATOR

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source", None)
        data.pop("env_generation", None)
        return data

    @classmethod
    def from_json(cls, data: Any, *, origin: str) -> PairRecord:
        if not isinstance(data, dict):
            raise PairingError(f"{origin} is not a pair record")
        if data.get("v") != PROTOCOL:
            raise PairingError(
                f"{origin} is pairing protocol {data.get('v')!r}; this release "
                f"speaks {PROTOCOL}. Upgrade agent-switchboard rather than editing it."
            )
        known = {f.name for f in fields(cls)} - {"source", "env_generation"}
        try:
            record = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError as exc:
            raise PairingError(f"{origin} is missing a field ({exc})") from exc
        if record.role not in ROLES or record.strategy not in STRATEGIES:
            raise PairingError(
                f"{origin} names role {record.role!r} / strategy "
                f"{record.strategy!r}, which this release does not know — upgrade."
            )
        validate_name(record.name)
        return record


@dataclass
class PendingHandshake:
    """An initiator's half-finished handshake.

    Holds the code's secret and the ephemeral private key — the only private
    key this package ever writes down, for at most the code's lifetime plus
    the collect window, and deleted the moment the answer is collected.
    """

    name: str
    secret: str = field(repr=False)
    private: str = field(repr=False)
    public: str
    expires_at: float
    deadline: float
    reveal: str = field(repr=False)
    commit: str
    url: str
    token: str | None = field(default=None, repr=False)
    note: str = ""
    created_at: float = 0.0
    #: The hub's clock minus ours, learned from the hello. The deadlines are
    #: in hub time whenever the hello landed, so ours is corrected by this.
    hub_offset: float = 0.0
    hello: bool = False
    #: Set once a pair record exists, so a crash between saving it and
    #: announcing it is finished on the next touch rather than lost.
    collected: bool = False
    paired_as: str | None = None
    v: int = PROTOCOL

    @classmethod
    def from_json(cls, data: Any, *, origin: str) -> PendingHandshake:
        if not isinstance(data, dict) or data.get("v") != PROTOCOL:
            raise PairingError(f"{origin} is not a protocol {PROTOCOL} pending handshake")
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError as exc:
            raise PairingError(f"{origin} is missing a field ({exc})") from exc


class PairStore:
    """The directory this machine keeps its pairs in.

    Owner-only (0700, files 0600), written atomically — a temporary file in
    the same directory, flushed and renamed over the old one — because unlike
    every other store under `~/.switchboard` this one holds the *only* copy of
    a secret. A lost timing database costs a guess; a lost pair record costs
    the pair. So it does not fail soft on write: a pairing command that cannot
    save says so and stops, before anything is sent.

    Per machine and per OS user, like `peers.py`. Every agent running as this
    user can read it, which is the same boundary `signing.py` states: the
    pair is between machines, not between processes.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.env = os.environ if env is None else env
        raw = os.fspath(path) if path is not None else self.env.get(ENV, DEFAULT_DIR)
        self.enabled = bool(raw)
        self.path = Path(os.path.expanduser(raw)) if raw else None

    # --- plumbing ---

    def _require(self) -> Path:
        if self.path is None:
            raise PairingError(
                f"pairing is disabled on this machine: {ENV} is set to the empty "
                "string. Unset it, or point it at a directory, to keep pairs here.",
                state="disabled",
            )
        return self.path

    def _dir(self, *parts: str) -> Path:
        directory = self._require().joinpath(*parts)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        return directory

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as handle_:
                json.dump(data, handle_, indent=2, sort_keys=True)
                handle_.flush()
                os.fsync(handle_.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    @staticmethod
    def _read(path: Path) -> Any:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except ValueError as exc:
            raise PairingError(f"{path} is not valid JSON ({exc}); it holds a pair "
                               "secret, so it is not overwritten — move it aside") from exc

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Serialise read-modify-write across this machine's processes.

        The CLI, the MCP bridge and a parked listener can all touch one pair
        at once. Never held across a hub call: every step that talks to the
        hub is idempotent, so two collectors racing cost a duplicate message,
        not a pair.
        """
        if fcntl is None:  # pragma: no cover
            yield
            return
        path = self._dir() / ".lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # --- pair records ---

    def _record_path(self, name: str) -> Path:
        return self._dir() / f"{validate_name(name)}.json"

    def _env_record(self, name: str) -> PairRecord | None:
        blob = (self.env.get(env_var_for(name)) or "").strip()
        if not blob:
            return None
        record = from_secret(blob, origin=env_var_for(name))
        if record.name != name:
            record = replace(record, name=name)
        record.source = "env"
        record.env_generation = record.generation
        return record

    def get(self, name: str) -> PairRecord | None:
        """This machine's record for `name` — the store's, else the environment's.

        When both exist they must be the same pair. Different secrets under one
        name are two pairs answering to one handle, and choosing either would
        put this side in rooms the other side of the *other* pair is in.
        """
        validate_name(name)
        local = None
        if self.path is not None:
            data = self._read(self.path / f"{name}.json")
            if data is not None:
                local = PairRecord.from_json(data, origin=str(self.path / f"{name}.json"))
        carried = self._env_record(name)
        if local is None:
            return carried
        if carried is not None:
            if not hmac.compare_digest(carried.secret, local.secret):
                raise PairingError(
                    f"{env_var_for(name)} and {self.path / (name + '.json')} hold "
                    f"different pairs under the name {name!r}. Remove one of them."
                )
            local.env_generation = carried.generation
            local.generation = max(local.generation, carried.generation)
        return local

    def save(self, record: PairRecord) -> None:
        self._write(self._record_path(record.name), record.to_json())

    def delete(self, name: str) -> bool:
        if self.path is None:
            return False
        try:
            (self.path / f"{validate_name(name)}.json").unlink()
            return True
        except FileNotFoundError:
            return False

    def names(self) -> list[str]:
        found: set[str] = set()
        if self.path is not None and self.path.is_dir():
            found |= {p.stem for p in self.path.glob("*.json") if _NAME.fullmatch(p.stem)}
        for var in self.env:
            if var.startswith(ENV_PAIR_PREFIX) and (self.env.get(var) or "").strip():
                candidate = var[len(ENV_PAIR_PREFIX):].lower().replace("_", "-")
                if _NAME.fullmatch(candidate):
                    found.add(candidate)
        return sorted(found)

    # --- pending handshakes ---

    def _pending_path(self, name: str) -> Path:
        return self._dir("pending") / f"{validate_name(name)}.json"

    def pending(self, name: str) -> PendingHandshake | None:
        if self.path is None:
            return None
        path = self.path / "pending" / f"{validate_name(name)}.json"
        data = self._read(path)
        return None if data is None else PendingHandshake.from_json(data, origin=str(path))

    def save_pending(self, pending: PendingHandshake) -> None:
        self._write(self._pending_path(pending.name), asdict(pending))

    def delete_pending(self, name: str) -> bool:
        if self.path is None:
            return False
        try:
            (self.path / "pending" / f"{validate_name(name)}.json").unlink()
            return True
        except FileNotFoundError:
            return False

    def pending_names(self) -> list[str]:
        if self.path is None or not (self.path / "pending").is_dir():
            return []
        return sorted(p.stem for p in (self.path / "pending").glob("*.json")
                      if _NAME.fullmatch(p.stem))

    def taken(self, name: str) -> str | None:
        """Why `name` cannot be used for a new pair, or None if it is free."""
        if self.get(name) is not None:
            return f"this machine already has a pair called {name!r}"
        if self.pending(name) is not None:
            return f"this machine is already waiting on a pairing called {name!r}"
        return None


# --- exporting a pair to an environment ----------------------------------------


def export(record: PairRecord, *, for_peer: bool = False) -> str:
    """A pair as one string, for a secret store that outlives a container.

    `for_peer` exports the *other* side's half — the same pair secret, the
    other role — which is how the machine that stays hands a cloud
    environment its side without the secret passing through the cloud
    session's own transcript.
    """
    data = record.to_json()
    # The answer only mattered until it was sent; the reveal is public once
    # published, so an initiator's own copy keeps it to publish again.
    data.pop("answer", None)
    if for_peer:
        data["role"] = record.peer_role
        data["confirmed"] = record.role == INITIATOR or record.confirmed
        data["peer_generation"] = record.generation
        data["peer_note"], data["note"] = record.note, record.peer_note
        # Each side's handshake leftovers are its own: the responder's copy of
        # the code's secret, the initiator's SAS nonce.
        for leftover in ("handshake", "code_expires_at", "reveal", "commit"):
            data[leftover] = None
    data["sent"] = True
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    return SECRET_PREFIX + _b64e(raw)


def from_secret(blob: str, *, origin: str = "the pair secret") -> PairRecord:
    text = (blob or "").strip()
    if not text.startswith(SECRET_PREFIX):
        raise PairingError(f"{origin} is not a `pair export` value "
                           f"(expected it to start with {SECRET_PREFIX!r})")
    try:
        data = json.loads(_b64d(text[len(SECRET_PREFIX):]))
    except (ValueError, TypeError) as exc:
        raise PairingError(f"{origin} is corrupt or truncated ({exc})") from exc
    return PairRecord.from_json(data, origin=origin)


# --- hub plumbing ----------------------------------------------------------------

#: `connect(config, agent_id=...)` returns a context-managed client. The CLI
#: passes its module-level `Client` so tests can swap in a hub; the default is
#: the real one.
Connect = Callable[..., Any]


def _default_connect(config: ClientConfig, *, agent_id: str) -> Any:
    from .client import Client

    return Client(config, agent_id=agent_id)


def _token_for(carried: str | None, url: str, base: ClientConfig | None) -> tuple[str | None, str]:
    """The hub token for a pairing room, and where it came from.

    The code's, when it carried one; otherwise this environment's own, when
    it is for the same hub. Never another hub's: a token configured for one
    hub sent to a different one is a credential handed to somebody nobody
    chose, and `ClientConfig.effective_token` already supplies the managed
    hub's published one.
    """
    if carried:
        return carried, "pair"
    if base is not None and base.token and base.url.rstrip("/") == url.rstrip("/"):
        return base.token, base.token_source
    return None, "none"


def _throwaway(room: RoomSpec, url: str, token: str | None, token_source: str) -> ClientConfig:
    """A config for a handshake or pair room: no local stores, nothing remembered."""
    return ClientConfig(
        url=url, url_source="pair", token=token, token_source=token_source,
        workspace=room.workspace, workspace_source="pair",
        key=room.key, write_key=room.write_key, peer_log="", stash_db="",
    )


def _agent(prefix: str = "pair") -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def work_config(record: PairRecord, base: ClientConfig | None = None,
                generation: int | None = None) -> ClientConfig:
    """The current work room as a `ClientConfig`, for any command to use.

    A snapshot: a long-lived process that keeps it will not follow a heal.
    Resolve again (`resolve`) to follow one.
    """
    room = work_room(record.pair_secret, record.generation if generation is None
                     else generation)
    token, token_source = _token_for(record.token, record.url, base)
    if base is None:
        return _throwaway(room, record.url, token, token_source)
    return replace(
        base, url=record.url, url_source="pair", token=token, token_source=token_source,
        workspace=room.workspace, workspace_source="pair", key=room.key,
        write_key=room.write_key, room_problem=None,
    )


def _pair_config(record: PairRecord, base: ClientConfig | None) -> ClientConfig:
    token, token_source = _token_for(record.token, record.url, base)
    return _throwaway(pair_room(record.pair_secret), record.url, token, token_source)


# --- handshake channel -------------------------------------------------------------


@dataclass
class _Channel:
    """What the handshake room's channel says, read in one pass."""

    hello: bool = False
    consumed: bool = False
    #: Which transcripts the initiator marked collected — ours, or somebody's.
    consumed_th: list[str] = field(default_factory=list)
    aborted: bool = False
    #: Answers the hub stamped inside the redemption window, by public key,
    #: in hub order. More than one distinct key means more than one redeemer.
    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    late: int = 0


def _read_channel(history: list[dict[str, Any]], expires_at: float) -> _Channel:
    seen = _Channel()
    for message in history:
        body = message.get("body")
        if not isinstance(body, dict):
            continue
        tag = body.get("t")
        if tag == _T_HELLO:
            seen.hello = True
        elif tag == _T_CONSUMED:
            seen.consumed = True
            if isinstance(body.get("th"), str):
                seen.consumed_th.append(body["th"])
        elif tag == _T_ABORTED:
            seen.aborted = True
        elif tag == _T_ANSWER and isinstance(body.get("e"), str):
            stamp = _hub_time(message.get("created_at"))
            if stamp is not None and stamp > expires_at + SKEW:
                seen.late += 1
                continue
            seen.answers.setdefault(body["e"], {**body, "_at": stamp})
    return seen


# --- start ---------------------------------------------------------------------------


@dataclass
class Started:
    code: str = field(repr=False)
    name: str
    expires_at: float
    deadline: float
    #: Whether the hello landed, so a responder on another hub is refused.
    verifiable: bool
    unproven: str = ""

    @property
    def room(self) -> str:
        return handle(self.name)


def start(base: ClientConfig, *, store: PairStore, connect: Connect = _default_connect,
          name: str | None = None, note: str = "", ttl: float = DEFAULT_TTL,
          with_token: bool = False, now: float | None = None) -> Started:
    """Begin a pairing on this machine: the side that stays should run this.

    Leaves a sealed hello in the handshake room, which costs one write and
    buys two things: the code's expiry is set on the hub's clock rather than
    this machine's, and a responder who cannot see the hello knows it is on
    the wrong hub before it answers. If the hub cannot be reached the code is
    still made, unanchored, and says so.

    Saves the pending handshake *before* returning the code, so a code is
    never in anyone's hands that this machine could not collect.
    """
    _need_crypto()
    store._require()
    ttl = float(ttl)
    if not MIN_TTL <= ttl <= MAX_TTL:
        raise PairingError(f"--ttl must be between {MIN_TTL} and {MAX_TTL} seconds; "
                           f"the code is the part expected to leak, so keep it short")
    sweep_expired(store, now=now)
    secret = secrets.token_bytes(32)
    chosen = validate_name(name) if name else ""
    name = chosen or default_name(secret)
    with store.lock():
        why = store.taken(name)
    if why:
        raise PairingError(f"{why}. Choose another with --name, or `switchboard pair "
                           f"forget {name}` first.", state="name_taken")
    private = X25519PrivateKey.generate()
    public = _x25519_public(private)
    reveal = secrets.token_bytes(16)
    url = base.url.rstrip("/")
    token = base.token if with_token else None
    room = handshake_room(secret)
    local = _now(now)

    hub_now, unproven = None, ""
    try:
        token_used, token_source = _token_for(base.token, url, base)
        with connect(_throwaway(room, url, token_used, token_source),
                     agent_id=_agent()) as hub:
            # Outlives the redemption window by the responder's slack, so a
            # slightly late responder gets the precise verdict ("too late",
            # from the hub's own timestamp) rather than "wrong hub".
            posted = hub.post(CHANNEL, {"t": _T_HELLO}, type="pair.hello",
                              ttl=_clamp_ttl(ttl + LOCAL_SLACK + SKEW, MAX_MESSAGE_TTL))
            hub_now = _hub_time(posted.get("created_at"))
    except _hub_errors() + (CryptoError,) as exc:
        unproven = f"{exc.__class__.__name__}: {exc}"
    anchor = hub_now if hub_now is not None else local
    expires_at = int(anchor + ttl)
    pending = PendingHandshake(
        name=name, secret=_b64e(secret), private=_b64e(_x25519_raw(private)),
        public=_b64e(public), expires_at=expires_at,
        deadline=expires_at + COLLECT_WINDOW, reveal=_b64e(reveal),
        commit=_b64e(commitment(reveal)), url=url, token=token, note=note,
        created_at=local, hub_offset=(hub_now - local) if hub_now is not None else 0.0,
        hello=hub_now is not None,
    )
    with store.lock():
        store.save_pending(pending)
    code = PairingCode(url=url, secret=secret, initiator=public, expires_at=expires_at,
                       commit=commitment(reveal), note=note, token=token,
                       name=chosen, hello=hub_now is not None)
    return Started(code=code.encode(), name=name, expires_at=expires_at,
                   deadline=pending.deadline, verifiable=hub_now is not None,
                   unproven=unproven)


# --- accept ----------------------------------------------------------------------------


@dataclass
class Accepted:
    record: PairRecord
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def room(self) -> str:
        return handle(self.record.name)

    @property
    def state(self) -> str:
        return "paired" if self.record.confirmed else "unconfirmed"


def accept(blob: str, base: ClientConfig | None = None, *, store: PairStore,
           connect: Connect = _default_connect, name: str | None = None,
           note: str = "", agent_id: str | None = None,
           now: float | None = None) -> Accepted:
    """Redeem a code: the side that may not stay should run this.

    Optimistic: the pair is usable the moment this returns, in work room 0,
    and is *unconfirmed* until the initiator collects the answer. Saved
    before anything is sent — if this machine cannot keep the pair, the code
    is left unspent rather than spent on a pair nobody holds.
    """
    _need_crypto()
    code = PairingCode.decode(blob)
    store._require()
    local = _now(now)
    if local > code.expires_at + LOCAL_SLACK:
        raise PairingError(
            f"this pairing code expired at {_iso(code.expires_at)}. Ask for a new "
            "one — and since an expired code may already be in a paper trail, "
            "do not reuse the old one.", state="expired")
    name = validate_name(name or code.pair_name)
    token, token_source = _token_for(code.token, code.url, base)
    room = handshake_room(code.secret)

    with store.lock():
        existing = store.get(name)
        if existing is not None and not existing.sent and \
                existing.handshake != _b64e(code.secret):
            # An unsent answer saved for a *different* code under this name.
            # Resuming it would answer this code with that code's key.
            existing = replace(existing, sent=True)
        if existing is not None and existing.sent:
            raise PairingError(
                f"this machine already has a pair called {name!r}. Pass --name to "
                f"keep both, or `switchboard pair forget {name}` first.",
                state="name_taken")
        if existing is None and store.pending(name) is not None:
            raise PairingError(f"this machine is itself waiting on a pairing called "
                               f"{name!r} — a code cannot be redeemed by the machine "
                               "that made it.", state="name_taken")

    with connect(_throwaway(room, code.url, token, token_source),
                 agent_id=_agent()) as hub:
        history = hub.history(CHANNEL, limit=500)
        seen = _read_channel(history, code.expires_at)
        resuming = (existing is not None and existing.answer is not None
                    and existing.answer.get("e") in seen.answers)
        if not resuming:
            if seen.consumed or seen.aborted:
                raise AlreadyRedeemed(
                    "this code has already been used — the other side collected an "
                    "answer that was not yours. Somebody else holds the code: treat "
                    "it as leaked, tell the other side to `pair forget` what it "
                    "paired, and start again with a fresh code.")
            if seen.answers and not (existing and existing.answer
                                     and existing.answer.get("e") in seen.answers):
                raise AlreadyRedeemed(
                    "somebody else already answered this code. It leaked between "
                    "being made and reaching you; nothing was sent. Tell the other "
                    "side, and pair again with a fresh code.")
            if code.hello and not seen.hello:
                raise PairingError(
                    f"no handshake waiting on {code.url}: the code has expired, or "
                    "this machine reaches a different hub at that address than the "
                    "one it was made on. Nothing was sent.", state="wrong_hub")

        if existing is not None and existing.answer is not None:
            record = existing
        else:
            ephemeral = X25519PrivateKey.generate()
            mine = _x25519_public(ephemeral)
            th = transcript(code.initiator, mine, code.commit)
            pair_secret = derive_pair_secret(_exchange(ephemeral, code.initiator),
                                             code.secret, th)
            del ephemeral  # never written anywhere; this drops the last reference
            record = PairRecord(
                name=name, role=RESPONDER, secret=_b64e(pair_secret),
                transcript=_b64e(th), url=code.url, token=code.token,
                commit=_b64e(code.commit), created_at=local,
                deadline=code.deadline, note=note, peer_note=code.note,
                answer={"t": _T_ANSWER, "e": _b64e(mine),
                        "c": _b64e(confirmation(pair_secret, RESPONDER, th)),
                        **({"n": note[:200]} if note else {})},
                sent=False, last_used_at=local,
                handshake=_b64e(code.secret), code_expires_at=float(code.expires_at),
            )
            # The commit point. If this machine cannot keep the pair, stop
            # here: the code is unspent and can be redeemed again.
            with store.lock():
                store.save(record)

        answer_ttl = _clamp_ttl(record.deadline - local + SKEW, MAX_MESSAGE_TTL)
        ours = record.answer["e"]
        if not resuming:
            try:
                posted = hub.post(CHANNEL, record.answer, type="pair.answer",
                                  ttl=answer_ttl)
                stamp = _hub_time(posted.get("created_at"))
            except _hub_errors():
                # The outcome is unknown, not failed: the write may have landed
                # and only its response been lost. Look before deciding.
                history = hub.history(CHANNEL, limit=500)
                found = _read_channel(history, code.expires_at)
                if ours not in found.answers:
                    raise
                stamp = found.answers[ours].get("_at")
            if stamp is not None and stamp > code.expires_at + SKEW:
                with store.lock():
                    store.delete(name)
                raise PairingError(
                    "too late: the hub received the answer after the code expired, "
                    "so the other side will not accept it. Ask for a new code.",
                    state="expired")
        # Read again: a second redeemer who got in at the same moment is only
        # visible after the fact, and both sides must reach the same verdict.
        after = _read_channel(hub.history(CHANNEL, limit=500), code.expires_at)
        rivals = [e for e in after.answers if e != ours]
        if rivals or after.aborted:
            with store.lock():
                store.delete(name)
            raise AlreadyRedeemed(
                "somebody else answered this code at the same time as you. The "
                "pairing is void on both sides — the code leaked. Start again "
                "with a fresh code, shared more narrowly.")

    record.sent = True
    with store.lock():
        store.save(record)
    warnings = []
    try:
        _publish_side(record, base, connect=connect, now=local)
    except _hub_errors() as exc:
        warnings.append(f"could not leave this side's record in the pair room yet "
                        f"({exc.__class__.__name__}); it is written on next use")
    return Accepted(record=record, warnings=warnings)


# --- collect -------------------------------------------------------------------------


@dataclass
class Collected:
    name: str
    state: str  # waiting | paired | unconfirmed | expired | aborted
    record: PairRecord | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def room(self) -> str:
        return handle(self.name)


def collect(name: str, base: ClientConfig | None = None, *, store: PairStore,
            connect: Connect = _default_connect, agent_id: str | None = None,
            now: float | None = None) -> Collected:
    """Finish the initiator's side, if the answer is there. Idempotent.

    Every step that talks to the hub can be repeated: a crash anywhere leaves
    either the pending handshake (and the next touch starts over) or a saved
    pair record with the pending handshake marked collected (and the next
    touch announces it). The one destructive step — dropping the ephemeral
    private key — comes last.
    """
    _need_crypto()
    pending = store.pending(name)
    if pending is None:
        record = store.get(name)
        if record is not None:
            # Nothing to collect here — but a responder's record is not paired
            # until the initiator's proof has been read, and saying so matters.
            return Collected(name, "paired" if record.confirmed else "unconfirmed", record)
        raise PairingError(f"no pairing called {name!r} is waiting on this machine",
                           state="unknown")
    local = _now(now)
    hub_now = local + pending.hub_offset
    if pending.collected:
        record = store.get(pending.paired_as or name)
        if record is not None:
            warnings = _announce(pending, record, base, store=store, connect=connect,
                                 agent_id=agent_id, now=local)
            return Collected(record.name, "paired", record, warnings=warnings)
    if hub_now > pending.deadline:
        with store.lock():
            store.delete_pending(name)
        return Collected(name, "expired", message=(
            f"no answer was found by the deadline ({_iso(pending.deadline)}). If the "
            f"other side ran `pair accept`, its pairing is dead: it must run "
            f"`switchboard pair forget {name}`, and you start a new one."))

    secret = _b64d(pending.secret)
    room = handshake_room(secret)
    token, token_source = _token_for(pending.token, pending.url, base)
    with connect(_throwaway(room, pending.url, token, token_source),
                 agent_id=_agent()) as hub:
        seen = _read_channel(hub.history(CHANNEL, limit=500), pending.expires_at)
        warnings = []
        if seen.late:
            warnings.append(
                f"{seen.late} answer(s) reached the hub after the code expired and "
                "were ignored — the code was used late, so treat it as leaked")
        if not seen.answers:
            return Collected(name, "waiting", warnings=warnings, message=(
                f"no answer yet; the code is redeemable until "
                f"{_iso(pending.expires_at)}"))
        if len(seen.answers) > 1:
            with contextlib.suppress(*_hub_errors()):
                hub.post(CHANNEL, {"t": _T_ABORTED, "why": "two redeemers"},
                         type="pair.aborted",
                         ttl=_clamp_ttl(pending.deadline - hub_now + SKEW,
                                        MAX_MESSAGE_TTL))
            with store.lock():
                store.delete_pending(name)
            return Collected(name, "aborted", warnings=warnings, message=(
                f"{len(seen.answers)} different machines answered this code, so it "
                "leaked and nothing was paired. Start again with a fresh code."))
        [(peer_key, answer)] = seen.answers.items()
        peer = _b64d(peer_key)
        commit = _b64d(pending.commit)
        th = transcript(_b64d(pending.public), peer, commit)
        private = X25519PrivateKey.from_private_bytes(_b64d(pending.private))
        pair_secret = derive_pair_secret(_exchange(private, peer), secret, th)
        expected = confirmation(pair_secret, RESPONDER, th)
        try:
            proof = _b64d(str(answer.get("c", "")))
        except (ValueError, TypeError):
            proof = b""
        if not hmac.compare_digest(proof, expected):
            with contextlib.suppress(*_hub_errors()):
                hub.post(CHANNEL, {"t": _T_ABORTED, "why": "answer did not verify"},
                         type="pair.aborted",
                         ttl=_clamp_ttl(pending.deadline - hub_now + SKEW,
                                        MAX_MESSAGE_TTL))
            with store.lock():
                store.delete_pending(name)
            return Collected(name, "aborted", warnings=warnings, message=(
                "the answer did not prove it derived the same pair secret — "
                "tampering, or a client speaking a different protocol. Nothing was "
                "paired; start again."))

    record = PairRecord(
        name=name, role=INITIATOR, secret=_b64e(pair_secret), transcript=_b64e(th),
        url=pending.url, token=pending.token, confirmed=True,
        sas=sas(pair_secret, th, _b64d(pending.reveal)), reveal=pending.reveal,
        created_at=local, deadline=pending.deadline, peer_seen_at=local,
        note=pending.note, peer_note=str(answer.get("n") or "")[:200],
        last_used_at=local,
    )
    with store.lock():
        existing = store.get(name)
        if existing is not None and not hmac.compare_digest(existing.secret,
                                                            record.secret):
            # The name was taken after `start`. The responder is already
            # paired and waiting, so refusing would strand it: keep both.
            record.name = _free_name(store, name)
        store.save(record)
        pending.collected, pending.paired_as = True, record.name
        store.save_pending(pending)
    warnings += _announce(pending, record, base, store=store, connect=connect,
                          agent_id=agent_id, now=local)
    return Collected(record.name, "paired", record, warnings=warnings)


def _free_name(store: PairStore, name: str) -> str:
    for i in range(2, 100):
        candidate = f"{name[:28]}-{i}"
        if store.taken(candidate) is None:
            return candidate
    raise PairingError(f"no free name near {name!r}; forget some pairs")


def _announce(pending: PendingHandshake, record: PairRecord, base: ClientConfig | None,
              *, store: PairStore, connect: Connect, agent_id: str | None,
              now: float) -> list[str]:
    """Tell the hub the handshake is over, then forget the ephemeral key.

    Three writes, each repeatable. The side record carries the proof the
    responder is waiting for; the consumed marker is what tells a late
    redeemer the code was used; the hello in work room 0 wakes the responder
    if it is parked there. The pending handshake — and with it the only
    private key this package writes down — is deleted once the first two have
    landed, and kept for the next touch to retry otherwise.
    """
    warnings: list[str] = []
    done = True
    try:
        _publish_side(record, base, connect=connect, now=now)
    except _hub_errors() as exc:
        done = False
        warnings.append(f"could not publish the confirmation yet ({exc.__class__.__name__}); "
                        "retried on next use")
    hub_now = now + pending.hub_offset
    token, token_source = _token_for(pending.token, pending.url, base)
    try:
        with connect(_throwaway(handshake_room(_b64d(pending.secret)), pending.url,
                                token, token_source), agent_id=_agent()) as hub:
            hub.post(CHANNEL, {"t": _T_CONSUMED, "th": record.transcript},
                     type="pair.consumed",
                     ttl=_clamp_ttl(pending.deadline - hub_now + SKEW, MAX_MESSAGE_TTL))
    except _hub_errors() as exc:
        done = False
        warnings.append(f"could not mark the code used yet ({exc.__class__.__name__}); "
                        "retried on next use")
    try:
        with connect(work_config(record, base), agent_id=agent_id or _agent()) as hub:
            hub.post(CHANNEL, {"t": _T_PAIRED, "note": record.note}, type="pair.paired")
    except _hub_errors() as exc:
        warnings.append(f"could not say hello in the work room ({exc.__class__.__name__})")
    if done:
        with store.lock():
            store.delete_pending(pending.name)
    return warnings


# --- the pair room --------------------------------------------------------------------


def _side_key(role: str) -> str:
    return SIDE_PREFIX + role


def _side_value(record: PairRecord, *, ended: bool = False, reason: str = "",
                now: float) -> dict[str, Any]:
    value: dict[str, Any] = {
        "t": _T_SIDE, "role": record.role, "g": record.generation,
        "s": record.strategy, "at": now, "ended": ended,
    }
    if reason:
        value["why"] = reason[:200]
    if record.role == INITIATOR:
        value["ci"] = _b64e(confirmation(record.pair_secret, INITIATOR, record.th))
        if record.reveal:
            value["r"] = record.reveal
    return value


def _publish_side(record: PairRecord, base: ClientConfig | None, *, connect: Connect,
                  now: float, ended: bool = False, reason: str = "") -> None:
    with connect(_pair_config(record, base), agent_id=_agent()) as hub:
        hub.board_set(_side_key(record.role),
                      _side_value(record, ended=ended, reason=reason, now=now),
                      ttl=SIDE_TTL)


@dataclass
class _Side:
    role: str
    generation: int
    strategy: str
    at: float
    ended: bool
    reason: str
    confirm: bytes | None
    reveal: bytes | None
    expires_in: float | None


def _read_sides(entries: list[dict[str, Any]]) -> dict[str, _Side]:
    """The two side records, each checked against the slot it was read from.

    A value whose sealed role does not match its key is refused as tampering:
    the hub cannot forge a record, but it could serve the responder's under
    the initiator's name, and that must not read as the initiator speaking.
    """
    sides: dict[str, _Side] = {}
    for entry in entries:
        key = entry.get("key")
        if not isinstance(key, str) or not key.startswith(SIDE_PREFIX):
            continue
        slot = key[len(SIDE_PREFIX):]
        if slot not in ROLES:
            continue
        value = entry.get("value")
        if entry.get("unreadable") or not isinstance(value, dict):
            raise PairingError(
                "the pair room holds a record this pair's key cannot open — the "
                "hub is serving something it was not given. Nothing was changed.",
                state="tampered")
        g = value.get("g")
        if (value.get("t") != _T_SIDE or value.get("role") != slot
                or not isinstance(g, int) or isinstance(g, bool) or g < 0):
            raise PairingError(
                f"the pair room's {slot} record is not what was written there — "
                "refusing it as tampering.", state="tampered")
        strategy = value.get("s")
        if strategy not in STRATEGIES:
            raise PairingError(
                f"the other side moved this pair on with healing strategy "
                f"{strategy!r}, which this release does not know. Upgrade "
                "agent-switchboard here before using the pair again.",
                state="upgrade")

        def raw(field_: str, value: dict[str, Any] = value) -> bytes | None:
            text = value.get(field_)
            try:
                return _b64d(text) if isinstance(text, str) else None
            except (ValueError, TypeError):
                return None

        at = value.get("at")
        sides[slot] = _Side(
            role=slot, generation=g, strategy=strategy,
            at=float(at) if isinstance(at, (int, float)) else 0.0,
            ended=bool(value.get("ended")), reason=str(value.get("why") or ""),
            confirm=raw("ci"), reveal=raw("r"),
            expires_in=entry.get("expires_in"),
        )
    return sides


# --- resolution ---------------------------------------------------------------------


@dataclass
class Resolution:
    """Where a pair's work is right now, and everything worth saying about it."""

    record: PairRecord
    config: ClientConfig
    state: str  # paired | unconfirmed
    warnings: list[str] = field(default_factory=list)
    moved_from: int | None = None
    peer_generation: int | None = None
    #: False when the pair room could not be read and the generation is this
    #: machine's last known one.
    checked: bool = True

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def generation(self) -> int:
        return self.record.generation

    @property
    def room(self) -> str:
        return handle(self.record.name)

    def summary(self) -> dict[str, Any]:
        """What a surface may show. No secret appears in it."""
        return {
            "name": self.record.name, "room": self.room, "role": self.record.role,
            "state": self.state, "generation": self.record.generation,
            "peer_generation": self.peer_generation, "sas": self.record.sas,
            "confirmed": self.record.confirmed, "workspace": self.config.workspace,
            "checked": self.checked, "warnings": list(self.warnings),
            **({"moved_from": self.moved_from} if self.moved_from is not None else {}),
        }


def resolve(name: str, base: ClientConfig | None = None, *, store: PairStore,
            connect: Connect = _default_connect, agent_id: str | None = None,
            now: float | None = None) -> Resolution:
    """The pair's current work room, after one read of the pair room.

    Reads both side records, adopts the higher generation, checks the
    initiator's proof if this side is still waiting for it, notices the other
    side ending the pair or falling silent, and rewrites this side's record
    when it is missing, stale or behind — which is also how a heal by the
    other side is acknowledged. If the pair room cannot be read the last known
    generation is used and the result says so; it never fails closed on a
    flaky hub, and never pretends a stale answer is a fresh one.
    """
    _need_crypto()
    local = _now(now)
    record = store.get(name)
    if record is None:
        if store.pending(name) is not None:
            done = collect(name, base, store=store, connect=connect,
                           agent_id=agent_id, now=local)
            if done.state != "paired" or done.record is None:
                raise PairingError(
                    f"pair {name!r} is not paired yet: {done.message or done.state}",
                    state="pending" if done.state == "waiting" else done.state)
            record = done.record
        else:
            # A disabled store hides every pair it would have held; "no such
            # pair" would send somebody looking for one that was never lost.
            store._require()
            raise PairingError(
                f"no pair called {name!r} on this machine — `switchboard pair "
                "status` lists them.", state="unknown")
    elif record.role == INITIATOR and store.pending(name) is not None:
        # Collected but not yet announced: finish the announcement first, so
        # the responder's proof lands before this side starts working.
        collect(name, base, store=store, connect=connect, agent_id=agent_id, now=local)
    warnings: list[str] = []
    try:
        with connect(_pair_config(record, base), agent_id=_agent()) as hub:
            sides = _read_sides(hub.board_list(prefix=SIDE_PREFIX))
    except _hub_errors() as exc:
        warnings.append(
            f"could not read the pair room ({exc.__class__.__name__}); using "
            f"generation {record.generation}, the last this machine saw")
        state = "paired" if record.confirmed else "unconfirmed"
        return Resolution(record, work_config(record, base), state, warnings,
                          peer_generation=record.peer_generation, checked=False)

    peer = sides.get(record.peer_role)
    own = sides.get(record.role)
    if peer is not None and peer.ended:
        raise PairingError(
            f"the other side ended pair {name!r} — run `switchboard pair forget "
            f"{name}` here, and pair again if you still need to.",
            state="ended_by_peer")

    if record.role == RESPONDER and not record.confirmed:
        if peer is not None and _verify_initiator(record, peer):
            record.confirmed = True
            record.sas = sas(record.pair_secret, record.th, peer.reveal or b"")
            record.handshake = None
        elif _verdict(record, base, store=store, connect=connect) == "collected":
            warnings.append(
                "the other side collected this answer; its confirmation has not "
                "reached the pair room yet")
        elif local > record.deadline + SKEW:
            raise PairingError(
                f"pair {name!r} was never confirmed: the other side could collect "
                f"this answer only until {_iso(record.deadline)} and did not. Either "
                "it never ran again, or somebody else holding the code answered "
                f"first. Run `switchboard pair forget {name}` and pair again.",
                state="never_confirmed")
        else:
            warnings.append(
                "unconfirmed: the other side has not collected the answer yet, so "
                "nothing said in this work room is read until it does (it can until "
                f"{_iso(record.deadline)})")

    generation = record.generation
    if own is not None:
        generation = max(generation, own.generation)
    if peer is not None:
        generation = max(generation, peer.generation)
        record.peer_seen_at = local
        record.peer_generation = peer.generation
    else:
        lapsed = _lapse(record, local)
        if lapsed is not None:
            generation = max(generation, lapsed)
            warnings.append(
                "the pair room had forgotten both sides (no sign of the other side "
                f"for over {STALE_AFTER // 86400} days), so this side moved to a "
                f"fresh work room, generation {generation}. The other side follows "
                "it the next time it runs.")
    moved_from = None
    if generation > record.generation:
        moved_from = record.generation
        if peer is not None and peer.generation == generation and peer.reason:
            warnings.append(f"moved from work room {moved_from} to {generation}: "
                            f"the other side healed the pair ({peer.reason})")
        elif peer is not None and peer.generation == generation:
            warnings.append(f"moved from work room {moved_from} to {generation}: "
                            "the other side healed the pair")
        record.generation = generation
    if peer is not None and peer.generation < record.generation:
        warnings.append(
            f"the other side has not moved to work room {record.generation} yet; "
            f"it is still in {peer.generation} until it next runs")
    if record.env_generation is not None and record.generation > record.env_generation:
        warnings.append(
            f"{env_var_for(record.name)} holds generation {record.env_generation} "
            f"but the pair is at {record.generation}: re-run `switchboard pair "
            f"export {record.name}` and update that secret, or a fresh container "
            f"starts behind")

    record.last_used_at = local
    stale = (own is None or own.generation != record.generation or own.ended
             or local - own.at > SIDE_REFRESH
             or (record.role == INITIATOR and own.confirm is None))
    if stale:
        try:
            _publish_side(record, base, connect=connect, now=local)
        except _hub_errors() as exc:
            warnings.append(f"could not refresh this side's record "
                            f"({exc.__class__.__name__})")
    _keep(store, record)
    state = "paired" if record.confirmed else "unconfirmed"
    return Resolution(record, work_config(record, base), state, warnings,
                      moved_from=moved_from, peer_generation=record.peer_generation)


def _verdict(record: PairRecord, base: ClientConfig | None, *, store: PairStore,
             connect: Connect) -> str | None:
    """What the initiator said in the handshake room about this side's answer.

    `"collected"` when it marked our transcript consumed. Raises when it voided
    the pairing or collected somebody else's answer — and drops this side's
    record, which can never be confirmed — so a responder learns it lost the
    moment the initiator knows, not at the deadline. None when there is
    nothing to say yet, or the room cannot be read.
    """
    if not record.handshake or record.code_expires_at is None:
        return None
    token, token_source = _token_for(record.token, record.url, base)
    try:
        with connect(_throwaway(handshake_room(_b64d(record.handshake)), record.url,
                                token, token_source), agent_id=_agent()) as hub:
            seen = _read_channel(hub.history(CHANNEL, limit=500), record.code_expires_at)
    except _hub_errors():
        return None
    if record.transcript in seen.consumed_th:
        return "collected"
    if seen.aborted or seen.consumed:
        with store.lock():
            store.delete(record.name)
        if seen.aborted:
            raise PairingError(
                f"pair {record.name!r} was voided: more than one machine answered the "
                "code, so it leaked, and the other side paired with nobody. This "
                "side's record is gone; pair again with a fresh code.", state="aborted")
        raise PairingError(
            f"pair {record.name!r} lost: the other side collected an answer that was "
            "not this machine's, so somebody else held the code. This side's record "
            "is gone. Tell the other side — it is paired with whoever that was — "
            "and pair again.", state="superseded")
    return None


def _verify_initiator(record: PairRecord, side: _Side) -> bool:
    if side.confirm is None or side.reveal is None or record.commit is None:
        return False
    expected = confirmation(record.pair_secret, INITIATOR, record.th)
    return (hmac.compare_digest(side.confirm, expected)
            and hmac.compare_digest(commitment(side.reveal), _b64d(record.commit)))


def _lapse(record: PairRecord, now: float) -> int | None:
    """Where to go when the pair room has forgotten the other side entirely.

    Every hub record expires, the side records within a week. So a peer
    record that is *absent* is ambiguous: the other side may simply be idle —
    or it healed past us, then went idle long enough for its record to lapse,
    and resuming our last generation would put us back in the work room the
    heal abandoned because its key leaked.

    Rather than guess, jump: a generation further on than any run of heals in
    a forgotten week could reach, so it is certainly a room nobody retired.
    The other side adopts it on its next read, because the higher generation
    always wins. What is lost is nothing — everything in a work room has
    expired after a week of silence on both sides.
    """
    last = record.peer_seen_at or record.created_at
    if now - last <= STALE_AFTER:
        return None
    return record.generation + LAPSE_JUMP


def _keep(store: PairStore, record: PairRecord) -> None:
    """Save a record that changed, when this machine can keep it.

    An environment-loaded pair in a container with no usable store still works
    — it just cannot remember a heal across containers, which the resolution
    already warns about.
    """
    if not store.enabled:
        return
    with store.lock():
        try:
            store.save(record)
        except OSError:
            if record.source != "env":
                raise


# --- heal, forget, status ---------------------------------------------------------


@dataclass
class Healed:
    name: str
    from_generation: int
    to_generation: int
    resolution: Resolution
    left_behind: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def heal(name: str, base: ClientConfig | None = None, *, store: PairStore,
         connect: Connect = _default_connect, reason: str = "",
         agent_id: str | None = None, now: float | None = None) -> Healed:
    """Move the pair to a new work room, because the current one's key leaked.

    A heal is a room rotation, with everything `docs/model.md` says that
    costs: leases, board entries and unread messages in the old room do not
    follow, and until the other side reads the pair room the two sides are in
    different rooms and do not exclude each other. So it lists what this side
    held there, posts a "moved" note into the old room to wake anyone parked
    in it, and the resolution keeps saying the other side is behind until its
    record catches up.
    """
    local = _now(now)
    current = resolve(name, base, store=store, connect=connect, agent_id=agent_id,
                      now=local)
    if not current.checked:
        raise PairingError(
            "cannot heal without reading the pair room — a heal written blind could "
            "land behind the other side's. Try again when the hub is reachable.",
            state="unreachable")
    record = current.record
    old = record.generation
    #: Leases this agent held in the room being left, as the hub-form ids the
    #: listing reports — enough to count them and release them, and the only
    #: honest thing to say, because the names never left this agent unsealed.
    left: list[str] = []
    if agent_id:
        try:
            with connect(work_config(record, base, old), agent_id=agent_id) as hub:
                mine = hub.agent_id
                left = [str(lease.get("resource")) for lease in hub.leases()
                        if lease.get("holder") == mine]
        except _hub_errors():
            pass
    record.generation = max(old, record.peer_generation or 0) + 1
    _publish_side(record, base, connect=connect, now=local, reason=reason or "healed")
    _keep(store, record)
    warnings = list(current.warnings)
    try:
        with connect(work_config(record, base, old), agent_id=agent_id or _agent()) as hub:
            hub.post(CHANNEL, {"t": _T_MOVED,
                               "note": "this work room was retired; paired agents "
                                       f"continue with --room {handle(record.name)}"},
                     type="pair.moved")
    except _hub_errors() as exc:
        warnings.append(f"could not leave a moved note in the old room "
                        f"({exc.__class__.__name__})")
    warnings.append(
        f"the other side is still in work room {old} until it next reads the pair room "
        f"— any use of {handle(record.name)} does, and a listener parked in the old room "
        "wakes on the moved note. Until then the two sides are in different rooms and do "
        "not exclude each other.")
    if left:
        warnings.append(
            f"{len(left)} lease(s) this agent held in work room {old} stay there until "
            f"they expire and exclude nobody in the new one: claim again with "
            f"{handle(record.name)} what you still need.")
    resolution = Resolution(record, work_config(record, base), current.state,
                            [], moved_from=old, peer_generation=record.peer_generation)
    return Healed(name, old, record.generation, resolution, left, warnings)


@dataclass
class Forgotten:
    name: str
    peer_told: bool
    warnings: list[str] = field(default_factory=list)


def forget(name: str, base: ClientConfig | None = None, *, store: PairStore,
           connect: Connect = _default_connect, agent_id: str | None = None,
           now: float | None = None) -> Forgotten:
    """End a pair on this machine, and tell the other side it ended.

    Best effort on the telling: a pair forgotten while the hub is down is
    still forgotten here, and the result says the other side was not told,
    because the alternative is a peer waiting in a room nobody will read.
    """
    local = _now(now)
    record = store.get(name)
    pending = store.pending(name)
    if record is None and pending is None:
        raise PairingError(f"no pair called {name!r} on this machine", state="unknown")
    told, warnings = False, []
    if record is not None:
        try:
            _publish_side(record, base, connect=connect, now=local, ended=True)
            told = True
        except _hub_errors() as exc:
            warnings.append(f"the other side was not told ({exc.__class__.__name__}); "
                            f"it will keep using pair {name!r} until it is")
        if told:
            # Only a wake for a listener parked in the work room; the side
            # record above is what the other side's next resolution reads.
            with contextlib.suppress(*_hub_errors()):
                with connect(work_config(record, base),
                             agent_id=agent_id or _agent()) as hub:
                    hub.post(CHANNEL, {"t": _T_ENDED,
                                       "note": "the other side ended this pair"},
                             type="pair.ended")
    with store.lock():
        store.delete(name)
        store.delete_pending(name)
    if record is not None and record.source == "env":
        warnings.append(f"{env_var_for(name)} still holds this pair; remove it from "
                        "the environment's secrets")
    return Forgotten(name, told, warnings)


def status(base: ClientConfig | None = None, *, store: PairStore,
           connect: Connect = _default_connect, name: str | None = None,
           agent_id: str | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """Every pair and pending handshake on this machine, brought up to date.

    The one call that does everything optimistic at once: collects what is
    waiting to be collected, resolves each pair, and reports each one's state
    — including the ones that failed, as rows rather than as an exception, so
    one dead pair does not hide the others.
    """
    names = [name] if name else sorted(set(store.names()) | set(store.pending_names()))
    rows: list[dict[str, Any]] = []
    for each in names:
        pending = store.pending(each)
        if pending is not None and not pending.collected and store.get(each) is None:
            try:
                done = collect(each, base, store=store, connect=connect,
                               agent_id=agent_id, now=now)
            except PairingError as exc:
                rows.append({"name": each, "room": handle(each), "state": exc.state or
                             "error", "error": str(exc)})
                continue
            except _hub_errors() as exc:
                rows.append({"name": each, "room": handle(each), "state": "pending",
                             "role": INITIATOR, "expires_at": pending.expires_at,
                             "warnings": [f"could not check for an answer "
                                          f"({exc.__class__.__name__})"]})
                continue
            if done.state != "paired":
                rows.append({"name": each, "room": handle(each), "role": INITIATOR,
                             "state": "pending" if done.state == "waiting" else done.state,
                             "expires_at": pending.expires_at,
                             "message": done.message, "warnings": done.warnings})
                continue
            each = done.name
        try:
            rows.append(resolve(each, base, store=store, connect=connect,
                                agent_id=agent_id, now=now).summary())
        except PairingError as exc:
            rows.append({"name": each, "room": handle(each), "state": exc.state or "error",
                         "error": str(exc)})
    return rows


def sweep_expired(store: PairStore, *, now: float | None = None) -> list[str]:
    """Delete pending handshakes past their deadline; return their names.

    Nothing can be collected after the deadline, so all an expired pending
    handshake still does is keep a private key on disk. Run wherever this
    machine already passes — `start`, a parked listener — so the bound in the
    docs ("the code's lifetime plus the collect window") is a property rather
    than a hope that somebody runs `pair status`.
    """
    local = _now(now)
    dropped: list[str] = []
    try:
        for name in store.pending_names():
            pending = store.pending(name)
            if pending is not None and not pending.collected and \
                    local + pending.hub_offset > pending.deadline:
                with store.lock():
                    store.delete_pending(name)
                dropped.append(name)
    except (PairingError, OSError):
        return dropped
    return dropped


def active(store: PairStore, *, within: float = 86400.0,
           now: float | None = None) -> list[str]:
    """Pairs worth parking a listener in: used lately, or still being made.

    Failing soft, unlike the commands: a listener that cannot read the pair
    store parks where it would have anyway.
    """
    local = _now(now)
    names: list[str] = []
    try:
        for name in store.names():
            record = store.get(name)
            if record is not None and (local - (record.last_used_at or 0)) <= within:
                names.append(name)
    except (PairingError, OSError):
        return names
    return names


def pending_rooms(store: PairStore, base: ClientConfig | None = None,
                  *, now: float | None = None) -> list[tuple[str, ClientConfig]]:
    """Handshake rooms this machine is waiting in, for a listener to park in.

    The responder's answer is a message, so a listener parked here wakes the
    moment it lands — which is how a turn-based initiator collects within
    seconds instead of whenever a human next types.
    """
    local = _now(now)
    found: list[tuple[str, ClientConfig]] = []
    try:
        for name in store.pending_names():
            pending = store.pending(name)
            if pending is None or pending.collected:
                continue
            if local + pending.hub_offset > pending.deadline:
                # Past its deadline nothing can be collected, and the file holds
                # the one private key this package writes down: drop it where
                # every parked listener passes, rather than waiting for a
                # collect that a machine done pairing never runs.
                with store.lock():
                    store.delete_pending(name)
                continue
            token, token_source = _token_for(pending.token, pending.url, base)
            found.append((name, _throwaway(handshake_room(_b64d(pending.secret)),
                                           pending.url, token, token_source)))
    except (PairingError, OSError):
        return found
    return found
