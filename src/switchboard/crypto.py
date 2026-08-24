"""End-to-end encryption, so a hub can coordinate agents it cannot read.

The hub never sees the key. Payloads are sealed with AES-256-GCM before they
leave the agent and opened after they come back; identifiers the hub must
*match* — channel names, lease resources, blackboard keys — are replaced by a
deterministic HMAC so the hub can still compare them for equality without
learning what they say.

That split is the whole design:

    encrypted   what the hub only stores      message bodies, board values,
                                              lease notes, agent name/branch/task
    blinded     what the hub must compare     channels, resources, board keys,
                                              agent ids
    visible     what the hub must operate on  workspace, timestamps, TTLs,
                                              sequence numbers, sizes

**The hub requires no changes to support any of this.** Bodies are already
arbitrary JSON and identifiers are already opaque strings, so a hub cannot
tell an encrypted workspace from a plaintext one, and cannot be misconfigured
into weakening it. Nothing server-side has to be trusted to get this right —
which is the property that makes a *managed* hub worth using.

What is still visible, stated plainly because overclaiming here would be worse
than not doing it at all
------------------------------------------------------------------------------
Blinding is deterministic, so the hub learns which blinded identifiers are
*equal* even though it cannot read them. From that plus timing it can infer:
how many distinct channels a workspace uses, how busy each is, when agents are
active, how many agents there are, roughly how long each message is, and which
agent is talking to which. It cannot read a single word of content, a resource
name, or a branch name.

That is metadata, and it is the honest cost of letting the hub route and
enforce leases at all. If it matters for your threat model, run your own hub —
which is why self-hosting is the primary deployment and always will be.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the no-extra install
    AVAILABLE = False

#: Marker on a sealed value. Its presence is how a client tells an envelope
#: from an ordinary JSON body.
#: How often the payload key rotates, in seconds. Set
#: SWITCHBOARD_KEY_EPOCH_PERIOD=0 to write everything under epoch 0 — the key
#: the cipher has always used — which produces bytes a pre-epoch reader opens.
#:
#: Fifteen minutes: short enough that a leaked derived key dies quickly, long
#: enough that a day is ~96 cached subkeys.
#:
#: On by default, which is a deliberate choice about *when*. Reading honours
#: whatever epoch a message names, so upgrading a reader is always safe; it is
#: writing epochs that a not-yet-upgraded peer cannot follow. Enabling it while
#: the protocol has no users costs nothing and avoids a second migration later.
#: The one way to be caught by it is an environment pinned to an older build —
#: note that the `uvx` bootstrap caches, so `uvx --refresh` may be needed.
DEFAULT_EPOCH_PERIOD = 900


def _default_period() -> int:
    raw = os.environ.get("SWITCHBOARD_KEY_EPOCH_PERIOD")
    if not raw:
        return DEFAULT_EPOCH_PERIOD
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_EPOCH_PERIOD


ENVELOPE_KEY = "$swb"
ENVELOPE_VERSION = 1

#: Marks an envelope sealed pairwise, by `seal_to_peer`, rather than under the
#: workspace key. Both ciphers use the same `ENVELOPE_KEY` so `is_sealed`/
#: `looks_sealed` still recognise the value as "this is sealed" everywhere
#: that already relies on them — the viewer included, which has no reason to
#: know `ask` exists to keep telling "empty" from "sealed" apart. The marker
#: is what lets `WorkspaceCipher.unseal` refuse one with a useful message
#: instead of an opaque AEAD failure, and `unseal_from_peer` refuse the
#: opposite mix-up the same way.
ASK_MARKER = "ask"

#: Pad plaintext up to a bucket before sealing, so ciphertext length stops
#: reporting plaintext length. AEAD preserves length exactly: measured on real
#: traffic, 18- and 19-character messages produced 143- and 144-byte rows, so
#: an operator can read message lengths to the byte. Buckets are powers of two
#: from 64 up to 4096, then multiples of 4096 — the storage cost is trivial
#: (everything expires within a day) and the leak it closes is not.
PAD_MIN = 64
PAD_MAX_POWER = 4096


def pad_bucket(length: int) -> int:
    """The size a plaintext of ``length`` bytes is padded up to."""
    if length <= PAD_MIN:
        return PAD_MIN
    if length >= PAD_MAX_POWER:
        return ((length // PAD_MAX_POWER) + 1) * PAD_MAX_POWER
    size = PAD_MIN
    while size < length:
        size *= 2
    return size


#: 16 bytes = 128 bits of HMAC output. Long enough that a collision between two
#: channel names is not a thing that happens; short enough to stay readable in
#: a log line.
BLIND_BYTES = 16

#: What a blinded id looks like once base64url-encoded, which is how a
#: hub-form agent id is told apart from a local alias in `blind_channel`.
#: Derived from BLIND_BYTES rather than written as a literal, so changing the
#: token size cannot silently stop DM addressing from recognising its own
#: output.
_HUB_FORM_ID = re.compile(r"[A-Za-z0-9_-]{%d}" % -(-BLIND_BYTES * 4 // 3))

_MISSING = (
    "end-to-end encryption needs the crypto extra: "
    "pip install 'agent-switchboard[crypto]'"
)


class CryptoError(Exception):
    """Encryption is misconfigured, or a value failed to open."""


class DecryptionError(CryptoError):
    """A value could not be opened: wrong key, tampering, or wrong context."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _seal_bytes(key: bytes, plaintext: bytes, aad: bytes) -> dict[str, Any]:
    """The AEAD step shared by every envelope this module writes.

    Factored out so `WorkspaceCipher.seal` and `seal_to_peer` cannot drift on
    the one thing that must never differ between them: the envelope shape a
    reader recognises via `is_sealed`/`looks_sealed`.
    """
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return {ENVELOPE_KEY: ENVELOPE_VERSION, "n": _b64e(nonce), "c": _b64e(ciphertext)}


def _unseal_bytes(key: bytes, envelope: dict[str, Any], aad: bytes, context: str) -> bytes:
    """The inverse of `_seal_bytes`. Raises `DecryptionError`, never the raw
    AEAD exception — a caller should never have to know this is AES-GCM."""
    try:
        return AESGCM(key).decrypt(_b64d(envelope["n"]), _b64d(envelope["c"]), aad)
    except Exception as exc:  # InvalidTag and malformed input alike
        raise DecryptionError(
            f"could not open the value at {context!r}: wrong key, tampering, "
            f"or a mismatched context"
        ) from exc


def generate_key() -> str:
    """A fresh workspace key, as a shareable string.

    Share it the way you would any other secret. It never reaches the hub, so
    losing it means losing the workspace's history — which, given everything
    expires within a day, costs less here than almost anywhere else.
    """
    return _b64e(secrets.token_bytes(32))


@dataclass
class WorkspaceCipher:
    """Seals payloads and blinds identifiers for one workspace.

    Two subkeys are derived from the workspace key with HKDF so that the key
    used to encrypt is never the key used to blind. Sharing one key across two
    primitives is the sort of thing that is fine until it suddenly is not.
    """

    workspace: str
    _payload_key: bytes
    _blind_key: bytes
    #: The raw key, kept so a payload subkey can be derived for any epoch. The
    #: blind subkey above is derived once and never rotates: the hub *compares*
    #: blinded channel names, lease resources and agent ids, so a rotating
    #: blind key would stop a channel matching itself across a boundary.
    _raw: bytes = b""
    #: Seconds per key epoch, or 0 to write everything under epoch 0.
    #: Reading always honours whatever epoch a message names, whatever this is.
    _period: int = 0
    _subkeys: dict[int, bytes] = field(default_factory=dict, repr=False)
    #: Pad plaintext to a size bucket before sealing. On by default: the point
    #: of encrypting is privacy, and an unpadded ciphertext announces its
    #: plaintext length to the byte.
    pad: bool = True

    @classmethod
    def from_key(cls, key: str | bytes, workspace: str,
                 pad: bool = True, epoch_period: int | None = None) -> WorkspaceCipher:
        if not AVAILABLE:
            raise CryptoError(_MISSING)
        raw = key if isinstance(key, bytes) else _decode_key(key)
        if len(raw) < 32:
            raise CryptoError(
                f"workspace key must be at least 32 bytes, got {len(raw)}; "
                "generate one with `switchboard keygen`"
            )
        if len(set(raw)) <= 2:
            # A placeholder like hex:0000... passes the length check and
            # produces a perfectly valid-looking cipher with no secrecy at all.
            # No amount of salting rescues a key with no entropy; refusing it
            # is the only thing that helps.
            raise CryptoError(
                "workspace key looks like a placeholder (almost no distinct "
                "bytes); generate a real one with `switchboard keygen`"
            )
        return cls(
            workspace=workspace,
            _payload_key=_derive(raw, b"switchboard/v1/payload", workspace),
            _blind_key=_derive(raw, b"switchboard/v1/identifier", workspace),
            _raw=raw,
            _period=epoch_period if epoch_period is not None else _default_period(),
            pad=pad,
        )

    def _payload_key_for(self, epoch: int) -> bytes:
        """The payload subkey for one epoch, derived once and cached.

        Epoch 0 is the original derivation, unchanged, so everything written
        before epochs existed still opens with exactly the same key.
        """
        if epoch == 0:
            return self._payload_key
        cached = self._subkeys.get(epoch)
        if cached is None:
            cached = _derive(
                self._raw, b"switchboard/v1/payload", f"{self.workspace}\0{epoch}",
            )
            self._subkeys[epoch] = cached
        return cached

    def current_epoch(self, now: float | None = None) -> int:
        """Which epoch to write under. 0 disables rotation entirely."""
        if self._period <= 0:
            return 0
        return int((time.time() if now is None else now) // self._period)

    # --- payloads ---

    def seal(self, value: Any, context: str) -> dict[str, Any]:
        """Encrypt any JSON-serializable value into an envelope.

        ``context`` is bound in as additional authenticated data, so a hub
        cannot take a sealed value from one place and present it as another —
        moving a message body onto a blackboard key, say. Without it the
        ciphertext would be authentic but relocatable, which is its own bug.
        """
        plaintext = _pad(json.dumps(value, separators=(",", ":")).encode()) \
            if self.pad else json.dumps(value, separators=(",", ":")).encode()
        epoch = self.current_epoch()
        envelope = _seal_bytes(self._payload_key_for(epoch), plaintext, self._aad(context))
        if epoch:
            # Omitted at epoch 0 so that a client with rotation switched off
            # writes bytes an older reader still understands. A reader that
            # knows about epochs treats a missing field as 0, which is the
            # same key it always used.
            envelope["e"] = epoch
        return envelope

    def unseal(self, envelope: Any, context: str) -> Any:
        """Open an envelope. Raises rather than falling back to plaintext.

        Refusing an unsealed value is deliberate. If a cipher is configured,
        accepting plaintext would let a hub strip the encryption and be
        believed — a downgrade attack that would look, from the agent's side,
        exactly like everything working.
        """
        if not is_sealed(envelope):
            raise DecryptionError(
                f"expected an encrypted value at {context!r} but found plaintext; "
                "refusing it, because accepting it would let the hub downgrade "
                "this workspace out of encryption"
            )
        version = envelope[ENVELOPE_KEY]
        if version != ENVELOPE_VERSION:
            raise DecryptionError(f"unsupported envelope version {version!r}")
        if envelope.get("m") == ASK_MARKER:
            # Right shape, wrong key entirely: this was sealed pairwise to one
            # recipient's exchange key by `seal_to_peer`, not to the workspace
            # key every member holds. Opening it here would either fail on an
            # opaque AEAD mismatch or — worse, if the pair keys ever collided
            # by coincidence — succeed and hand back garbage. Naming the
            # mistake is strictly more useful than either.
            raise DecryptionError(
                f"the value at {context!r} is sealed to one peer with `ask`, "
                "not to the workspace; open it with `unseal_from_peer` instead"
            )
        # The epoch comes from the message, never from our own clock: a
        # message written seconds before a boundary must stay readable by
        # someone already past it, and a reader joining later must be able to
        # open history. Both fall out of following the writer.
        epoch = envelope.get("e", 0)
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise DecryptionError(f"invalid key epoch {epoch!r}")
        plaintext = _unseal_bytes(
            self._payload_key_for(epoch), envelope, self._aad(context), context
        )
        # Padding is detected from the payload itself rather than from this
        # client's setting, so a padded and an unpadded agent interoperate.
        return json.loads(_unpad(plaintext))

    def seal_text(self, text: str | None, context: str) -> str | None:
        """Seal a value that has to stay a string on the wire (notes, names)."""
        if text is None:
            return None
        return json.dumps(self.seal(text, context), separators=(",", ":"))

    def unseal_text(self, text: str | None, context: str) -> str | None:
        if text is None:
            return None
        try:
            envelope = json.loads(text)
        except ValueError as exc:
            raise DecryptionError(
                f"expected an encrypted string at {context!r}, found plain text"
            ) from exc
        return self.unseal(envelope, context)

    # --- identifiers ---

    def blind(self, identifier: str, domain: str) -> str:
        """Map an identifier to a stable opaque token.

        Deterministic on purpose: two agents must derive the same token for
        the same channel, and the hub must be able to compare tokens for
        equality to enforce a lease. ``domain`` keeps namespaces apart, so a
        channel called "build" and a lease resource called "build" do not blind
        to the same token and leak that they share a name.
        """
        if not AVAILABLE:
            raise CryptoError(_MISSING)
        import hashlib
        import hmac

        digest = hmac.new(
            self._blind_key, f"{domain}\x00{identifier}".encode(), hashlib.sha256
        ).digest()
        return _b64e(digest[:BLIND_BYTES])

    def blind_channel(self, channel: str) -> str:
        """Blind a named channel, and resolve a direct-message channel.

        A ``@`` channel carries an agent id, and it arrives in one of two
        forms. A *hub-form* id — what the roster hands back, already blinded —
        must pass through untouched: blinding again would produce
        `blind(blind(id))`, which no recipient's inbox resolves to. A *local
        alias*, the name an agent calls itself, must be blinded, because that
        is what the recipient's own inbox is keyed on.

        Passing both through unchanged is what made `dm("thinker")` vanish
        silently while `dm(roster_entry["agent_id"])` worked — identical at
        the call site, and only one of them delivered (#90).

        The two are told apart by shape rather than by asking the hub, so a
        DM to an agent that is between turns and absent from the roster still
        lands: `blind` is deterministic, so the message waits in exactly the
        channel that agent will resolve to when it comes back. A hub-form id
        is `BLIND_BYTES` of base64url and nothing else; a local alias that
        happened to be exactly that shape would be misread, which is the one
        case this cannot see — and is why an agent should hand out the id
        `whoami` reports rather than inventing one.

        The ``@`` prefix stays legible so the hub can resolve an agent's own
        inbox. It reveals only that a message is a DM, which the hub could
        infer from the traffic pattern anyway.
        """
        if channel.startswith("@"):
            target = channel[1:]
            if _HUB_FORM_ID.fullmatch(target):
                return channel
            return "@" + self.blind(target, "agent")
        return self.blind(channel, "channel")

    def _aad(self, context: str) -> bytes:
        # The workspace is bound in too, so a ciphertext cannot be replayed
        # into a different workspace on a shared hub.
        return f"switchboard/v1\x00{self.workspace}\x00{context}".encode()


# --- sealed to one peer, not to the workspace --------------------------------
#
# Everything above protects a *workspace*: one key, shared by every member, so
# any of them can read any of it. That is the right shape for coordination and
# the wrong shape the moment one agent wants to say something to one specific
# peer that the rest of the room — same workspace, same key — cannot open.
#
# `custom_scope` already covers "a private conversation among a subset of the
# room", but it needs a fresh (key, workspace) pair minted and handed out of
# band before the first word can be exchanged. What follows needs none of
# that: every agent already publishes an X25519 exchange key alongside its
# Ed25519 signing key (`signing.SigningIdentity.exchange_key`), sealed into
# the roster like any other field, so anyone who has seen a peer on the
# roster already has what ECDH needs. No key to mint, no out-of-band channel,
# and it works even in a workspace with no `WorkspaceCipher` configured at
# all — the pairwise secret never depends on one.


def _ask_aad(context: str) -> bytes:
    # No workspace to bind — the pair key already ties this to exactly two
    # identities — but `context` is bound the same way `WorkspaceCipher._aad`
    # binds it, for the same reason: without it a hub could take a sealed ask
    # body and relocate it onto another field, and the ciphertext would still
    # look authentic there.
    return f"switchboard/v1/ask\x00{context}".encode()


def _derive_ask_key(my_identity: Any, peer_exchange_key: str) -> bytes:
    """The per-pair AES-256-GCM key two identities agree on without a hub.

    ECDH already gives both ends an identical shared secret — that symmetry
    is the whole point of Diffie-Hellman, and it is why nothing here needs a
    negotiation. What HKDF's ``info`` adds is binding, not direction: sorting
    the two exchange keys into it (never into the ECDH itself, which already
    does not care about order) ties the derived key deterministically to
    *this unordered pair* rather than to whichever side happened to call
    first, so A deriving "my key and B's" and B deriving "B's key and mine"
    land on the same 32 bytes without either needing to know who initiated.
    Sender authenticity for an ask rides on the same Ed25519 signature every
    other message carries, not on this key being direction-specific.
    """
    if not AVAILABLE:
        raise CryptoError(_MISSING)
    secret = my_identity.derive_shared_secret(peer_exchange_key)
    pair = "\x00".join(sorted([my_identity.exchange_key, peer_exchange_key]))
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b"switchboard/v1/ask\x00" + pair.encode(),
    ).derive(secret)


def seal_to_peer(
    value: Any, *, my_identity: Any, peer_exchange_key: str, context: str, pad: bool = True,
) -> dict[str, Any]:
    """Seal `value` so that only `peer_exchange_key`'s holder can open it.

    Not even a fellow holder of the workspace key can — the key used here
    never touches the workspace at all, which is the entire reason this
    exists next to `WorkspaceCipher`: sometimes "everyone in this room" is
    the wrong audience for one message, and minting a whole second (key,
    workspace) pair with `switchboard keygen` for a single question is more
    ceremony than the moment deserves.

    Uses the same envelope shape `WorkspaceCipher.seal` does — `is_sealed`/
    `looks_sealed` need no changes to keep telling "sealed" from "empty" —
    with one added field, `"m": "ask"`, that tells the two apart so neither
    cipher can be handed the other's envelope and misread it as its own.
    """
    if not AVAILABLE:
        raise CryptoError(_MISSING)
    key = _derive_ask_key(my_identity, peer_exchange_key)
    plaintext = json.dumps(value, separators=(",", ":")).encode()
    if pad:
        plaintext = _pad(plaintext)
    envelope = _seal_bytes(key, plaintext, _ask_aad(context))
    envelope["m"] = ASK_MARKER
    return envelope


def unseal_from_peer(
    envelope: Any, *, my_identity: Any, peer_exchange_key: str, context: str,
) -> Any:
    """Open an envelope `seal_to_peer` sealed to `my_identity`.

    `peer_exchange_key` is the *sender's* exchange key — ECDH needs one
    identity's private half and the other's public half, and here that is
    ours and theirs respectively, the mirror image of the call that sealed
    it.

    Refuses an envelope that is not ask-marked, symmetrically with
    `WorkspaceCipher.unseal` refusing one that is: the two ciphers protect
    different things, and silently accepting either envelope in the other's
    place would make a hub's or a peer's mix-up look like it worked.
    """
    if not AVAILABLE:
        raise CryptoError(_MISSING)
    if not is_sealed(envelope):
        raise DecryptionError(
            f"expected a value sealed with `ask` at {context!r} but found "
            "plaintext or an ordinary value; refusing it"
        )
    version = envelope[ENVELOPE_KEY]
    if version != ENVELOPE_VERSION:
        raise DecryptionError(f"unsupported envelope version {version!r}")
    if envelope.get("m") != ASK_MARKER:
        raise DecryptionError(
            f"the value at {context!r} is sealed to the workspace, not to you "
            "specifically with `ask`; open it with `WorkspaceCipher.unseal` instead"
        )
    key = _derive_ask_key(my_identity, peer_exchange_key)
    plaintext = _unseal_bytes(key, envelope, _ask_aad(context), context)
    return json.loads(_unpad(plaintext))


#: Marks a padded plaintext. Chosen as a byte that cannot begin a JSON
#: document, so an unpadded payload from another client is unambiguous.
_PAD_MARKER = 0x00


def _pad(plaintext: bytes) -> bytes:
    """``\\x00`` + 4-byte length + data + filler, out to a size bucket."""
    framed = len(plaintext).to_bytes(4, "big") + plaintext
    target = pad_bucket(len(framed) + 1)
    return bytes([_PAD_MARKER]) + framed + bytes(target - len(framed) - 1)


def _unpad(plaintext: bytes) -> bytes:
    if not plaintext or plaintext[0] != _PAD_MARKER:
        return plaintext          # written by a client with padding off
    length = int.from_bytes(plaintext[1:5], "big")
    if length > len(plaintext) - 5:
        raise DecryptionError("padded payload declares a length beyond its own size")
    return plaintext[5:5 + length]


def is_sealed(value: Any) -> bool:
    return isinstance(value, dict) and ENVELOPE_KEY in value


def looks_sealed(value: Any) -> bool:
    """Is this value an envelope, in either form it travels in?

    Payload fields carry an envelope dict; text fields (an agent's name, a
    lease note) carry the envelope *serialized to a string*, because the wire
    schema types them as strings. A check that only knew about the dict form
    silently returned False for every text field — which is exactly the form
    an unencrypted client meets when it looks at an encrypted peer.

    Public because that meeting is not only the client's business. Anything
    reading a room it might not hold the key to — the viewer in
    `switchboard_viewer/viewer.py` is the first — has to tell "this is empty" from "this
    is sealed and I cannot open it", and those two must never render the same.
    """
    if is_sealed(value):
        return True
    if isinstance(value, str) and value.startswith("{"):
        try:
            return is_sealed(json.loads(value))
        except ValueError:
            return False
    return False


def _decode_key(key: str) -> bytes:
    """Accept the shapes people actually paste: base64url, base64, or hex."""
    key = key.strip()
    if key.startswith("hex:"):
        return bytes.fromhex(key[4:])
    try:
        return _b64d(key)
    except Exception:
        pass
    try:
        return bytes.fromhex(key)
    except ValueError as exc:
        raise CryptoError(
            "workspace key must be base64url or hex; "
            "generate one with `switchboard keygen`"
        ) from exc


def _derive(raw: bytes, info: bytes, workspace: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        # No salt: the input is already a full-entropy random key, not a
        # passphrase. A passphrase would need scrypt and a shared salt, which
        # is a different and much easier thing to get wrong — hence
        # `switchboard keygen` rather than accepting one.
        salt=None,
        info=info + b"\x00" + workspace.encode(),
    ).derive(raw)
