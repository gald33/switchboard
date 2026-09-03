"""Room write keys: the one thing the hub enforces, and how it does so while
holding nothing.

Everything else in this package keeps the hub out of the loop on purpose — the
workspace key seals content it never sees, and a room identifier is derived
rather than registered, so there is nobody at the hub to ask who owns what.
That is the right shape for *confidentiality*, and it has one consequence a
viewer link makes visible: anyone who can read a room can also write into it,
because reading and writing take the same symmetric key and the hub cannot
tell a reader from a writer.

A write key is the narrowest change that fixes that without reversing the
rest. It is an Ed25519 keypair whose **public half names the room**: the
room's workspace token is the public key (`rooms.write_token`), and its wire
identifier is a truncated hash of a domain string, a version byte and that key
(`rooms.workspace_for`). Both steps are one-way, so the identifier is a
commitment to a key that only its minter holds. To write, a client signs the
request and presents the public key; the hub checks that the key hashes to the
room the request names and that the signature verifies under it. A reader
holds the identifier and the workspace key, and no keypair that hashes to the
identifier — so it cannot produce a request the hub accepts.

What the hub stores for this: nothing. There is no table of writers, no
first-to-bind race, and no credential the operator could replay — a public
key verifies and cannot sign. That is the difference between this and the
per-workspace binding that was removed once already (`store.py`'s schema
comment, `docs/managed-hub.md`): this is a permission, and it is enforced by
the hub because only the hub can refuse a request, but it is stateless, it is
derived from the room's own name, and it hands the operator nothing.

The signature covers the method, the path, the query string, a timestamp and
a hash of the body — the sealed body, when the room is encrypted, so the hub
verifies over bytes it cannot read. It is placed *outside* the ciphertext
because the hub is the verifier; the per-process signature in `signing.py`
stays *inside* it for the opposite reason, because there the verifiers are
peers and the hub must not be able to strip it. Two signatures, two audiences.

What it does not do, stated plainly: it makes a reader's writes *impossible*
at the hub, not merely untrusted, and that is the whole point — but it is one
key for the whole room. Every writer holds the same seed, so it says "a
writer", never which one; attribution is still `signing.py`'s job. Revoking
one writer means minting a new room, exactly as it does for the workspace key.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import rooms

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the minimal-install CI job
    AVAILABLE = False

#: The public key a signed request presents, as the room's workspace token
#: (`pk1_…`). Naming the room by its token rather than by a bare key is what
#: lets the hub check both facts with one derivation: `workspace_for(token)`
#: must equal the workspace the request names.
KEY_HEADER = "X-Switchboard-Write-Key"
#: `<unix timestamp>.<nonce>.<base64url signature>`. The nonce is what keeps
#: two honest, identical requests in the same second — a heartbeat, a retried
#: claim — from producing one signature, which the hub's replay guard would
#: otherwise refuse the second time. Ed25519 is deterministic; this is not.
SIG_HEADER = "X-Switchboard-Write-Sig"

#: How far a request's timestamp may sit from the hub's clock, either way,
#: in seconds. Wide enough for real clock drift between machines; narrow
#: enough that a signature captured from a log is dead by the time anyone
#: reads the log. A replay inside the window is caught separately, by the hub
#: remembering the signatures it has accepted until they age out.
WINDOW = 300.0

_DOMAIN = b"switchboard/v1/write-request"


class WriteKeyError(ValueError):
    """A write key that cannot be used: malformed, or for another room."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_write_key() -> str:
    """A fresh write key, as a shareable string.

    The same shape as a workspace key, and handled the same way: a secret,
    shared with every agent that may write the room and nobody else. The hub
    never receives it — it receives the public half, which names the room.
    """
    return _b64e(secrets.token_bytes(32))


@dataclass(frozen=True)
class RoomWriteKey:
    """The private half of a room's write key, and what it derives.

    Built from the 32-byte seed `generate_write_key` produced. Deterministic:
    the same seed always yields the same public key, the same token and the
    same workspace identifier, which is what lets a writer with only the seed
    know which room it may write without being told.
    """

    _private: Any = field(repr=False)

    @classmethod
    def from_seed(cls, seed: str) -> RoomWriteKey:
        if not AVAILABLE:  # pragma: no cover
            raise WriteKeyError("write keys need the `cryptography` package")
        try:
            raw = _b64d(seed.strip())
        except (ValueError, TypeError) as exc:
            raise WriteKeyError("write key is not valid base64url") from exc
        if len(raw) != 32:
            raise WriteKeyError(
                f"write key must decode to 32 bytes, got {len(raw)} — "
                "mint one with `switchboard keygen`"
            )
        return cls(_private=Ed25519PrivateKey.from_private_bytes(raw))

    @property
    def public_key(self) -> str:
        raw = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _b64e(raw)

    @property
    def workspace_token(self) -> str:
        """The room this key writes, as the token a rooms file records."""
        return rooms.write_token(self.public_key)

    @property
    def workspace(self) -> str:
        """The room this key writes, as the identifier on the wire."""
        return rooms.workspace_for(self.workspace_token)

    def sign_request(self, method: str, path: str, query: str, body: bytes,
                     *, now: float | None = None) -> dict[str, str]:
        """The two headers that make one request a writer's."""
        ts = int(time.time() if now is None else now)
        nonce = _b64e(secrets.token_bytes(12))
        digest = request_digest(method, path, query, ts, nonce, body)
        return {
            KEY_HEADER: self.workspace_token,
            SIG_HEADER: f"{ts}.{nonce}.{_b64e(self._private.sign(digest))}",
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"<RoomWriteKey {self.workspace}>"


def request_digest(method: str, path: str, query: str, ts: int, nonce: str,
                   body: bytes) -> bytes:
    """What a request signature is over.

    Every field the hub acts on: the method and path say what is being done,
    the query carries the workspace and target on the routes that have no
    body, the timestamp bounds replay, and the body hash binds the payload —
    ciphertext included — without the signature growing with it. Joined with
    a separator no field can contain, under a domain string, so a signature
    over one request cannot be read as a signature over another.
    """
    parts = [
        _DOMAIN, method.upper().encode(), path.encode(), query.encode(),
        str(ts).encode(), nonce.encode(), hashlib.sha256(body).digest(),
    ]
    return b"\x00".join(parts)


def verify_request(workspace: str, method: str, path: str, query: str, body: bytes,
                   headers: Mapping[str, str], *, now: float | None = None,
                   window: float = WINDOW) -> tuple[bool, str]:
    """Whether a request is a valid write for `workspace`, and if not, why.

    Three checks, in the order that says the most on failure: the presented
    key must *name this room* — `workspace_for(token) == workspace`, which is
    the whole authorization — then the timestamp must be inside the window,
    then the signature must verify under that key. The reason is a short
    phrase meant for an error body, never a hint about which byte was wrong.
    """
    token = headers.get(KEY_HEADER) or headers.get(KEY_HEADER.lower())
    sig = headers.get(SIG_HEADER) or headers.get(SIG_HEADER.lower())
    if not token or not sig:
        return False, "unsigned"
    if not AVAILABLE:  # pragma: no cover
        return False, "this hub cannot verify write signatures (no cryptography package)"
    try:
        if rooms.workspace_for(token) != workspace:
            return False, "write key does not name this room"
        public = Ed25519PublicKey.from_public_bytes(rooms.write_token_public_key(token))
    except (rooms.RoomsError, ValueError):
        return False, "malformed write key"
    ts_text, nonce, signature = (sig.split(".") + ["", ""])[:3]
    try:
        ts = int(ts_text)
        raw_sig = _b64d(signature)
    except (ValueError, TypeError):
        return False, "malformed write signature"
    if not nonce or not signature:
        return False, "malformed write signature"
    current = time.time() if now is None else now
    if abs(current - ts) > window:
        return False, "write signature is outside the time window"
    try:
        public.verify(raw_sig, request_digest(method, path, query, ts, nonce, body))
    except InvalidSignature:
        return False, "write signature does not verify"
    return True, "ok"
