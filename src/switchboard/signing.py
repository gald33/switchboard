"""Per-agent signing identity.

Everything else in this package protects a *workspace*: the workspace key
seals what leaves the machine, and every member holds it. That is the right
shape for confidentiality and the wrong shape for attribution — AEAD proves
"someone with the workspace key wrote this", never which member. `agent_id` is
self-asserted and the hub does not check it, so inside a room any agent can
post as another, release another's leases, or advance another's read cursor.

This module is the other half: a keypair that is *not* shared, so peers can
tell each other apart. The hub learns nothing — the public key is sealed like
any other content, so only key-holders ever see it.

The private key is generated per process and held in memory. It is never
written to a file and never put in the environment, for a specific reason:
the agents in one workspace are usually sibling processes on one machine
sharing a filesystem, so a key in `.claude/settings.local.json` — or any file
— is readable by exactly the peers this exists to distinguish. The workspace
key *must* be shared; this one must not, and not persisting it is the only way
to be sure.

The honest limit: the OS user is the real boundary. A process running as the
same user can read another's memory, so this is not a defence against a
hostile local process. What it buys is that there is no file to copy, nothing
to leak into a subprocess environment, a log, a backup or a synced directory,
and no accidental sharing between agents that happen to read the same settings
file. Most real leaks are copies.

Identity ends when the process does, deliberately. Memory clears and that is a
different agent — which matches a system where everything expires within a day
anyway. What this provides is unforgeability *within a live conversation*: the
thing that stops one agent impersonating another while both are working. It is
not long-term reputation, and a rogue agent can shed its identity by
restarting.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

try:  # pragma: no cover - exercised by the minimal-install CI job
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class SigningIdentity:
    """An in-memory Ed25519 keypair for one agent process.

    Ed25519 rather than anything configurable: one curve, no parameters to get
    wrong, 32-byte public keys and 64-byte signatures small enough to ride
    along on every message without anyone noticing.
    """

    _private: object = field(repr=False)

    @classmethod
    def generate(cls) -> SigningIdentity:
        return cls(_private=Ed25519PrivateKey.generate())

    @property
    def public_key(self) -> str:
        """The public half, as a short string safe to put in a payload."""
        from cryptography.hazmat.primitives import serialization

        raw = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _b64e(raw)

    def sign(self, message: bytes) -> str:
        return _b64e(self._private.sign(message))

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let the private half reach a log, a traceback or a repr in a
        # crash report. The public key is enough to tell two identities apart.
        return f"<SigningIdentity {self.public_key[:12]}…>"


def verify(public_key: str, message: bytes, signature: str) -> bool:
    """Whether `signature` over `message` was made by `public_key`.

    Returns False rather than raising for a bad signature — a forged or
    corrupted one is an expected input here, not an exceptional condition.
    Malformed key or signature material is also False for the same reason: a
    caller cannot act differently on "wrong shape" than on "wrong signer".
    """
    try:
        key = Ed25519PublicKey.from_public_bytes(_b64d(public_key))
        key.verify(_b64d(signature), message)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
