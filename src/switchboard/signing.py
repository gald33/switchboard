"""Per-agent signing identity.

Everything else in this package protects a *workspace*: the workspace key
seals what leaves the machine, and every member holds it. That is the right
shape for confidentiality and the wrong shape for attribution — AEAD proves
"someone with the workspace key wrote this", never which member. `agent_id` is
self-asserted and the hub does not check it, so inside a room any agent can
post as another, release another's leases, or advance another's read cursor.

This module is the other half: two keypairs that are *not* shared, so peers
can tell each other apart. The hub learns nothing — both public keys are
sealed like any other content, so only key-holders ever see them.

Two keypairs, not one, and for the same reason `WorkspaceCipher` derives two
subkeys instead of reusing one: Ed25519 signs, X25519 seals, and sharing a
single key across both primitives is the kind of thing that is fine until it
suddenly is not. The Ed25519 half is `public_key`/`sign` — attribution within
a room. The X25519 half is `exchange_key`/`derive_shared_secret` — a
pairwise secret with one specific peer, which is what `crypto.seal_to_peer`
uses to seal something even fellow workspace-key holders cannot open.

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
import contextlib
import hashlib
import json
import os
import socket as _socket
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised by the minimal-install CI job
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey,
        X25519PublicKey,
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
    """An in-memory Ed25519 + X25519 keypair pair for one agent process.

    Ed25519 rather than anything configurable: one curve, no parameters to get
    wrong, 32-byte public keys and 64-byte signatures small enough to ride
    along on every message without anyone noticing. X25519 alongside it for
    the same reason and on the same curve family, but deliberately a
    *separate* keypair — see the module docstring for why signing and sealing
    must not share one key.
    """

    _private: object = field(repr=False)
    _x25519_private: object = field(repr=False)

    @classmethod
    def generate(cls) -> SigningIdentity:
        return cls(_private=Ed25519PrivateKey.generate(),
                   _x25519_private=X25519PrivateKey.generate())

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

    @property
    def exchange_key(self) -> str:
        """The X25519 public half, as a short string safe to put in a payload.

        Publishing this is what lets a peer seal something to this identity
        alone with `crypto.seal_to_peer` — the ECDH exchange needs the other
        side's public key, and this is how it becomes available without a
        second out-of-band handoff.
        """
        from cryptography.hazmat.primitives import serialization

        raw = self._x25519_private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _b64e(raw)

    def derive_shared_secret(self, peer_exchange_key: str) -> bytes:
        """The raw 32-byte ECDH output shared with one peer's exchange key.

        Both sides compute the same value from their own private half and the
        other's public half — that symmetry is the entire point of ECDH, and
        it is why `crypto.seal_to_peer`/`unseal_from_peer` need no key
        exchange protocol beyond "read it off the roster".

        Raises rather than crashing with a low-level traceback on malformed
        input, mirroring how `verify()` below turns bad key material into a
        clean error instead of letting the caller find out from cryptography's
        own exception type.
        """
        try:
            peer_key = X25519PublicKey.from_public_bytes(_b64d(peer_exchange_key))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"malformed peer exchange key: {peer_exchange_key!r}") from exc
        return self._x25519_private.exchange(peer_key)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let either private half reach a log, a traceback or a repr in
        # a crash report. The public keys are enough to tell two identities
        # apart.
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


def message_payload(*, sender: str, channel: str, seq: int, body: Any) -> bytes:
    """The exact bytes a message signature covers.

    Binds four things, and each for a reason:

    - ``sender`` is the agent id *as the hub knows it* — blinded, when
      encrypting — so a reader can look the key up from the roster against the
      same string the message arrived with, rather than inverting a one-way
      blind.
    - ``channel`` stops a signed message being replayed into another channel.
    - ``seq`` makes gaps visible: signatures prove authenticity, and only a
      counter gives any grip on a hub that selectively withholds.
    - ``body`` is the content itself.

    Serialized with sorted keys and no whitespace so that two processes agree
    on the bytes. ``default=str`` keeps an exotic value from raising here —
    the body has already been accepted by the caller, and refusing to sign
    something we are about to send would be the wrong end to fail at.
    """
    return json.dumps(
        {"by": sender, "ch": channel, "n": seq, "b": body},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode()


# --- one identity across the processes that make up one agent ----------------
#
# An agent is not one process. The MCP server is long-lived and holds the
# keypair; the lifecycle hooks and every `switchboard` command are separate
# processes that would each generate their own — so an agent would appear as a
# stream of one-message strangers, and `release` from the Stop hook, which is
# an impersonation target, would be signed by nobody in particular.
#
# The key does not move. The MCP server signs on behalf of the others over a
# unix socket, so it stays in one process's memory and still never touches
# disk. Both sides can compute the path without a handoff because `agent_id` is
# already derived deterministically from branch, host and session.
#
# This grants any process running as the same user the ability to sign as this
# agent. That is not a new exposure: such a process could already read the
# server's memory. The OS user was always the boundary — see the module
# docstring — and the socket does not move it.

#: Kept short: a unix socket path is limited to ~100 bytes on most platforms,
#: and an agent id can be 96 characters on its own.
_SOCKET_NAME_LEN = 16


def socket_path(agent_id: str) -> Path:
    """Where this agent's signer listens.

    Under `XDG_RUNTIME_DIR` when there is one — it is user-owned, already the
    right place for per-session sockets, and cleaned up on logout. Otherwise a
    per-user directory under the system temp dir.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    tag = hashlib.sha256(agent_id.encode("utf-8", "replace")).hexdigest()[:_SOCKET_NAME_LEN]
    return Path(base) / "switchboard" / f"{tag}.sock"


class SigningServer:
    """Signs on behalf of the other processes that make up this agent."""

    def __init__(self, identity: SigningIdentity, agent_id: str) -> None:
        self.identity = identity
        self.path = socket_path(agent_id)
        self._server: _socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Begin listening. False if this platform or environment cannot."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Owner-only, so the socket is not the weakest link on a shared box.
            os.chmod(self.path.parent, 0o700)
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(8)
        except (OSError, AttributeError, NotImplementedError):
            # No AF_UNIX, no writable runtime dir, a path too long. Signing
            # still works for this process; the others just sign as themselves.
            return False
        self._server = server
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return True

    def _serve(self) -> None:
        assert self._server is not None
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with contextlib.suppress(OSError, ValueError), conn:
                self._handle(conn)

    def _handle(self, conn: _socket.socket) -> None:
        data = conn.recv(65536)
        if not data:
            return
        request = json.loads(data.decode())
        op = request.get("op")
        if op == "pubkey":
            reply = {"pubkey": self.identity.public_key,
                     "exchange_key": self.identity.exchange_key}
        elif op == "sign":
            payload = _b64d(request["payload"])
            reply = {"pubkey": self.identity.public_key,
                     "sig": self.identity.sign(payload)}
        elif op == "exchange":
            try:
                secret = self.identity.derive_shared_secret(request["peer_exchange_key"])
            except (ValueError, KeyError, TypeError) as exc:
                # A bad request from the other end of this process's own
                # socket must not take the signer thread down with it — every
                # other process this agent is made of depends on it staying
                # up.
                reply = {"error": str(exc)}
            else:
                reply = {"secret": _b64e(secret)}
        else:
            reply = {"error": "unknown op"}
        conn.sendall(json.dumps(reply).encode())

    def close(self) -> None:
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
        with contextlib.suppress(OSError):
            self.path.unlink()


class RemoteSigningIdentity:
    """A signer backed by another process, exposing the same surface.

    Interchangeable with `SigningIdentity` on purpose: the caller signs without
    knowing whether the key is in this process or the session's.
    """

    def __init__(self, path: Path, public_key: str, exchange_key: str) -> None:
        self._path = path
        self._public_key = public_key
        self._exchange_key = exchange_key

    @property
    def public_key(self) -> str:
        return self._public_key

    def sign(self, message: bytes) -> str:
        reply = _ask(self._path, {"op": "sign", "payload": _b64e(message)})
        if reply is None or "sig" not in reply:
            raise OSError("the session's signer did not answer")
        return reply["sig"]

    @property
    def exchange_key(self) -> str:
        return self._exchange_key

    def derive_shared_secret(self, peer_exchange_key: str) -> bytes:
        reply = _ask(self._path, {"op": "exchange", "peer_exchange_key": peer_exchange_key})
        if reply is None or "secret" not in reply:
            detail = reply.get("error") if reply else "the session's signer did not answer"
            raise OSError(detail)
        return _b64d(reply["secret"])

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"<RemoteSigningIdentity {self._public_key[:12]}…>"


def _ask(path: Path, request: dict) -> dict | None:
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(path))
            client.sendall(json.dumps(request).encode())
            data = client.recv(65536)
    except (OSError, AttributeError, NotImplementedError):
        return None
    try:
        return json.loads(data.decode())
    except ValueError:
        return None


def attach(agent_id: str) -> RemoteSigningIdentity | None:
    """The session's signer for this agent, if one is listening.

    A short timeout and a quiet None on failure: a CLI command must not hang or
    fail because no MCP server happens to be running. It simply signs as
    itself, which is what it did before this existed.
    """
    path = socket_path(agent_id)
    if not path.exists():
        return None
    reply = _ask(path, {"op": "pubkey"})
    if (not reply or not isinstance(reply.get("pubkey"), str)
            or not isinstance(reply.get("exchange_key"), str)):
        return None
    return RemoteSigningIdentity(path, reply["pubkey"], reply["exchange_key"])
