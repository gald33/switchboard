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
import secrets
from dataclasses import dataclass
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
ENVELOPE_KEY = "$swb"
ENVELOPE_VERSION = 1

#: 16 bytes = 128 bits of HMAC output. Long enough that a collision between two
#: channel names is not a thing that happens; short enough to stay readable in
#: a log line.
BLIND_BYTES = 16

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

    @classmethod
    def from_key(cls, key: str | bytes, workspace: str) -> WorkspaceCipher:
        if not AVAILABLE:
            raise CryptoError(_MISSING)
        raw = key if isinstance(key, bytes) else _decode_key(key)
        if len(raw) < 32:
            raise CryptoError(
                f"workspace key must be at least 32 bytes, got {len(raw)}; "
                "generate one with `switchboard keygen`"
            )
        return cls(
            workspace=workspace,
            _payload_key=_derive(raw, b"switchboard/v1/payload", workspace),
            _blind_key=_derive(raw, b"switchboard/v1/identifier", workspace),
        )

    # --- payloads ---

    def seal(self, value: Any, context: str) -> dict[str, Any]:
        """Encrypt any JSON-serializable value into an envelope.

        ``context`` is bound in as additional authenticated data, so a hub
        cannot take a sealed value from one place and present it as another —
        moving a message body onto a blackboard key, say. Without it the
        ciphertext would be authentic but relocatable, which is its own bug.
        """
        plaintext = json.dumps(value, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._payload_key).encrypt(
            nonce, plaintext, self._aad(context)
        )
        return {
            ENVELOPE_KEY: ENVELOPE_VERSION,
            "n": _b64e(nonce),
            "c": _b64e(ciphertext),
        }

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
        try:
            plaintext = AESGCM(self._payload_key).decrypt(
                _b64d(envelope["n"]), _b64d(envelope["c"]), self._aad(context)
            )
        except Exception as exc:  # InvalidTag and malformed input alike
            raise DecryptionError(
                f"could not open the value at {context!r}: wrong workspace key, "
                f"tampering, or a mismatched context"
            ) from exc
        return json.loads(plaintext)

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
        """Blind a named channel, and pass a direct-message channel through.

        A ``@`` channel already carries a *hub-form* agent id: ids are blinded
        once when the client is constructed, and a roster hands them back in
        that same form, so `dm(roster_entry["agent_id"])` addresses the right
        agent. Blinding again here would produce `blind(blind(id))`, which no
        recipient's inbox resolves to — a DM that vanishes silently, which is
        the worst way for this to fail.

        The ``@`` prefix stays legible so the hub can resolve an agent's own
        inbox. It reveals only that a message is a DM, which the hub could
        infer from the traffic pattern anyway.
        """
        if channel.startswith("@"):
            return channel
        return self.blind(channel, "channel")

    def _aad(self, context: str) -> bytes:
        # The workspace is bound in too, so a ciphertext cannot be replayed
        # into a different workspace on a shared hub.
        return f"switchboard/v1\x00{self.workspace}\x00{context}".encode()


def is_sealed(value: Any) -> bool:
    return isinstance(value, dict) and ENVELOPE_KEY in value


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
