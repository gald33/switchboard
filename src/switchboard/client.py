"""HTTP client for a Switchboard hub.

Depends only on ``httpx`` so an agent can talk to a hub without installing the
server. Both a sync and an async client are provided; the CLI uses the sync
one, the MCP bridge uses the async one.
"""

from __future__ import annotations

import hashlib
import os
import platform
import secrets
import socket
import subprocess
import uuid
from dataclasses import dataclass, replace
from typing import Any, Sequence

import httpx

from . import peers, rooms, signing
from .config import ClientConfig
from .crypto import (
    CryptoError,
    DecryptionError,
    WorkspaceCipher,
    looks_sealed,
    seal_to_peer,
    unseal_from_peer,
)
from .invite import PROBE_SENTINEL, Invite
from .signing import SigningIdentity
from .writekey import RoomWriteKey, WriteKeyError

__all__ = [
    "SwitchboardError",
    "LeaseHeld",
    "UnknownPeerExchangeKey",
    "Client",
    "AsyncClient",
    "Identity",
    "detect_identity",
]


class SwitchboardError(RuntimeError):
    """A hub returned an error response."""

    def __init__(self, message: str, *, status: int | None = None,
                 payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class LeaseHeld(SwitchboardError):
    """Someone else holds the lease you asked for."""

    @property
    def holder(self) -> str | None:
        return self.payload.get("holder")

    @property
    def expires_in(self) -> float | None:
        return self.payload.get("expires_in")


class ReadOnlyRoom(SwitchboardError):
    """The hub refused a write: this room is write-protected and this client
    holds no write key for it (or a stale signature was replayed).

    Reading still works — that is the point of the room. To write, the
    client needs the room's write key (`SWITCHBOARD_WRITE_KEY`, or
    `write_key` on the config or the custom scope), which whoever minted the
    room holds. See `writekey.py`.
    """


class UnknownPeerExchangeKey(SwitchboardError):
    """`whisper()` was called for a peer whose exchange key this client has not
    seen yet.

    Distinct from every other `SwitchboardError`: nothing was sent to the
    hub, and there is nothing to retry — the fix is a roster read. Raising a
    dedicated type rather than falling back to a plain `send()` is the point;
    silently downgrading a `whisper` into an unsealed message would hand the
    caller exactly the confidentiality it explicitly asked to avoid.
    """


# --- identity ---------------------------------------------------------------


@dataclass
class Identity:
    """Who this agent is, inferred from the environment it is running in."""

    agent_id: str
    name: str
    kind: str
    branch: str | None
    meta: dict[str, Any]
    #: Where `agent_id` came from: `"argument"`, `"SWITCHBOARD_AGENT_ID"`, or
    #: `"derived"`. Reported rather than inferred by callers because under a
    #: workspace key the id is blinded before anyone sees it, so a pin that
    #: worked and a pin that was ignored produce two opaque tokens that look
    #: equally unlike the string you set.
    id_source: str = "derived"
    #: Whether the directory this identity was derived from was inside a git
    #: checkout. False means the repo and branch components are absent, so the
    #: derived id is not the one the same agent gets from its own repo root.
    in_repo: bool = True


def _git(*args: str, cwd: str | None = None) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


#: Environment variables that identify one editor session, most specific
#: first. Hashed, never sent as-is — see `session_suffix`.
_SESSION_ID_VARS = (
    "SWITCHBOARD_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_HOST_SESSION_ID",
    "TERM_SESSION_ID",
)

#: Last-resort session tag, generated once per process. Module-level so every
#: call in this process agrees; a new process is a new session, which is the
#: honest answer when nothing else identifies one.
_PROCESS_SESSION_ID = secrets.token_hex(16)


def session_suffix() -> str:
    """A short tag distinguishing one session from another on this machine.

    `agent_id` was `kind-branch-host`, which is identical for two sessions on
    one machine and branch — two editor tabs in one worktree shared a read
    cursor and could release each other's leases. Not by impersonation: by
    construction.

    The session id is the right input because it satisfies both properties the
    old derivation traded against. It differs between concurrent sessions, and
    it survives a resume — so the deliberate behaviour in the docstring below,
    a resumed session reclaiming its own leases rather than waiting out their
    TTL, still holds.

    Hashed rather than passed through: `agent_id` is blinded when encryption is
    on, but on an unencrypted hub it reaches the operator in the clear, and a
    host session id is not ours to hand over.

    With nothing to go on, a per-process random. That gives up the resume
    property, which is the correct trade in that order: a duplicate identity is
    silently wrong, while a fresh one merely waits for a lease to expire.
    """
    for var in _SESSION_ID_VARS:
        value = os.environ.get(var)
        if value:
            return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
    return hashlib.sha256(_PROCESS_SESSION_ID.encode()).hexdigest()[:8]


def detect_identity(
    *,
    agent_id: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    cwd: str | None = None,
) -> Identity:
    """Infer a stable-ish identity for the current session.

    The agent id is stable across restarts of the same session, so a resumed
    session reclaims its own leases instead of waiting for them to expire — and
    distinct between concurrent sessions, so two of them never share one.
    """
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    repo = _git("rev-parse", "--show-toplevel", cwd=cwd)
    repo_name = os.path.basename(repo) if repo else os.path.basename(os.getcwd())
    host = socket.gethostname()

    if kind is None:
        if os.environ.get("GITHUB_ACTIONS"):
            kind = "ci"
        elif os.environ.get("CLAUDE_CODE_REMOTE") or os.environ.get("CODESPACES"):
            kind = "cloud"
        else:
            kind = "local"

    id_source = "argument" if agent_id is not None else "derived"
    if agent_id is None:
        agent_id = os.environ.get("SWITCHBOARD_AGENT_ID")
        if agent_id is not None:
            id_source = "SWITCHBOARD_AGENT_ID"
    if agent_id is None:
        slug = (branch or "detached").replace("/", "-")
        # Truncate the descriptive part, never the suffix: it is what makes
        # the id unique, and a long branch name must not be able to trim it
        # off and silently reintroduce the collision.
        suffix = session_suffix()
        head = f"{kind}-{slug}-{host}"[: 96 - len(suffix) - 1]
        agent_id = f"{head}-{suffix}"

    if name is None:
        name = os.environ.get("SWITCHBOARD_AGENT_NAME") or f"{repo_name}:{branch or 'detached'}"

    meta = {
        "host": host,
        "repo": repo_name,
        "platform": platform.system(),
        "pid": os.getpid(),
    }
    if os.environ.get("GITHUB_RUN_ID"):
        meta["github_run_id"] = os.environ["GITHUB_RUN_ID"]
    return Identity(agent_id=agent_id, name=name, kind=kind, branch=branch, meta=meta,
                    id_source=id_source, in_repo=repo is not None)


def identity_drift_warning(identity: Identity) -> str | None:
    """Text warning that this agent's id was derived outside a git checkout.

    An unpinned id is built from repo + branch + session, so the directory a
    command runs in is part of who you are. Run one command from the checkout
    and the next from a temp directory and you have published under two
    identities — two roster rows, two inboxes, two sets of leases that do not
    exclude each other — while believing you are one agent. Nothing raises,
    because both ids are perfectly valid; they are just not each other.

    This is not hypothetical. It bit both agents in this project's own
    cross-session dogfooding: one wrote its handoff from a scratch directory
    and its status from the repo root, and told a peer to reply to an id that
    was not the one signing the message.

    Silent when the id is pinned, because then the directory does not matter —
    which is also the fix. Silent inside a checkout, because that is the case
    the derivation is designed for.
    """
    if identity.id_source != "derived" or identity.in_repo:
        return None
    return (
        f"warning: agent id {identity.agent_id} was derived outside a git "
        "checkout, so it is not the id this session gets from its repo root.\n"
        "Anything said under it lands in a second identity: a separate roster "
        "row, a separate inbox, and leases that do not exclude the ones your "
        "other id holds.\n"
        "Set SWITCHBOARD_AGENT_ID (or pass --agent-id) to pin one id across "
        "every directory this session runs in."
    )


#: How much of an unparseable body is worth repeating. Enough to recognise a
#: proxy's error page, not enough to paste one into somebody's terminal.
_BODY_EXCERPT = 200


def _describe_unparseable(response: httpx.Response) -> str:
    """What to say when the body is not the hub's JSON.

    Something between the client and the hub can answer instead of it — a
    proxy, a load balancer, a captive portal — and it answers in HTML. Printing
    that verbatim put `</div></body></html>` in an agent's output twice in one
    day, which reads as the tool malfunctioning rather than as the hub being
    briefly unreachable through something in the way.
    """
    kind = response.headers.get("content-type", "").split(";")[0] or "an unreadable body"
    body = " ".join(response.text.split())[:_BODY_EXCERPT]
    if "html" in kind:
        return (f"the hub returned an HTML error page ({response.status_code}), "
                "not its own reply — something between here and it answered")
    return f"{response.status_code} with {kind}: {body}" if body else f"{response.status_code}"


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": _describe_unparseable(response)}
    detail = payload.get("detail") or payload.get("error") or payload.get("detail")
    if payload.get("error") == "lease_conflict":
        raise LeaseHeld(str(detail), status=response.status_code, payload=payload)
    if payload.get("error") == "read_only":
        raise ReadOnlyRoom(str(detail), status=response.status_code, payload=payload)
    raise SwitchboardError(str(detail), status=response.status_code, payload=payload)


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


#: Which body fields, at which paths, are payloads to seal — and under what
#: context label. The context is bound into the ciphertext, so a hub cannot
#: move a sealed value from one field to another and have it still open.
_SEAL_BODY: dict[str, dict[str, str]] = {
    "/agents/register": {"name": "agent.name", "branch": "agent.branch",
                         "task": "agent.task", "pubkey": "agent.pubkey",
                         "exchange_key": "agent.exchange_key"},
    "/agents/heartbeat": {"task": "agent.task"},
    "/leases/acquire": {"note": "lease.note"},
    "/leases/renew": {"note": "lease.note"},
    "/messages": {"body": "message.body"},
    "/board": {"value": "board.value"},
}

#: Which body fields are identifiers the hub must *compare* but need not read,
#: and which blinding domain each belongs to.
_BLIND_BODY: dict[str, dict[str, str]] = {
    "/agents/register": {"channels": "channel"},
    "/leases/acquire": {"resource": "resource"},
    "/leases/renew": {"resource": "resource"},
    "/leases/release": {"resource": "resource"},
    "/messages": {"channel": "channel"},
    "/board": {"key": "board"},
}

#: Response keys that carry sealed fields, and the context each was sealed under.
_OPEN_RESPONSE: dict[str, dict[str, str]] = {
    "agents": {"name": "agent.name", "branch": "agent.branch", "task": "agent.task",
               "pubkey": "agent.pubkey", "exchange_key": "agent.exchange_key"},
    "agent": {"name": "agent.name", "branch": "agent.branch", "task": "agent.task",
              "pubkey": "agent.pubkey", "exchange_key": "agent.exchange_key"},
    "leases": {"note": "lease.note"},
    "lease": {"note": "lease.note"},
    "messages": {"body": "message.body"},
    "message": {"body": "message.body"},
    "entries": {"value": "board.value"},
    "entry": {"value": "board.value"},
}

#: Fields that are arbitrary JSON rather than strings, so they travel as an
#: envelope dict instead of a serialized string.
_JSON_VALUED = {"message.body", "board.value"}

#: Response keys where a value we cannot open is EXPECTED rather than alarming,
#: and must not fail the whole call.
#:
#: The roster is the one place agents holding different keys legitimately meet
#: — that is what makes it the right place to detect a key mismatch. Raising
#: there defeats the purpose twice over: it blocks the diagnostic, and it makes
#: the roster unusable for the peers whose key *is* correct.
#:
#: The board and the lease table are the other two. Both are per-workspace and
#: workspace routing is plaintext, so a peer on a different key writes into the
#: same listing we read — its board keys and lease resources blind to tokens we
#: do not recognise and its values do not open. Raising there took out the whole
#: listing *including our own entries*, which is worse than it sounds: the
#: coordination convention opens with `board_list prefix="coord/"`, so one
#: mismatched newcomer disabled handoff discovery for the agents whose key was
#: right. Plural only — see below.
#:
#: Everywhere else stays strict. A message body we cannot open never reaches us
#: in the first place (a mismatched sender blinds to a different channel), so
#: an unopenable one is genuinely suspicious and should still raise. The
#: singular forms stay strict for the same reason in a different shape: a
#: `board_get`/`claims` lookup blinds the key with *our* key, so a peer's entry
#: answers 404 rather than arriving unopenable. One that does arrive unopenable
#: is not a key mismatch, and silently returning None for it would hide that.
_TOLERATE_UNREADABLE = {"agents", "agent", "entries", "leases"}


#: One definition, in crypto.py, where the envelope is. This name stays
#: because it is used throughout this module.
_looks_sealed = looks_sealed

#: The message `type` a whisper carries on the wire. 0.11.0 sent `"ask"`
#: under the old name; both open, so a mixed room stays readable in the
#: direction that matters — reading an older peer's whisper.
WHISPER_TYPE = "whisper"
WHISPER_TYPES = frozenset({WHISPER_TYPE, "ask"})

_WHISPER_MISSING = (
    "whisper needs a signing identity, which needs the crypto extra: "
    "pip install 'agent-switchboard[crypto]'"
)


def _is_labelled(opened: Any) -> bool:
    """Is this an opened message body carrying its own channel label?

    Checked structurally rather than assumed, so a body that happens to be a
    dict from an older client still round-trips instead of being mangled.
    """
    return (
        isinstance(opened, dict)
        and set(opened) in ({"b", "ch"}, {"b", "ch", "s"})
        and isinstance(opened["ch"], str)
    )


#: Marker distinguishing a board envelope from a value that merely happens to
#: be a dict. The message envelope above is recognised structurally and accepts
#: the small risk of a body shaped like `{"b": …, "ch": …}`; a board value is
#: far more often a plain dict written by an application, so this one carries
#: an explicit tag rather than relying on its field names being unusual.
_BOARD_ENVELOPE = "sbk1"


def _board_envelope(key: str, value: Any) -> dict[str, Any]:
    """Carry the plaintext key inside the ciphertext, beside the value.

    Blinding is one-way, so `blind("coord/reports/auth")` cannot be turned
    back into a key by anyone — including the agent that wrote it. That is
    correct for routing and useless for reading, which is the same bind
    message channels were in, and this is the same answer: the hub keeps the
    opaque token it compares on, and the readable name travels sealed.
    """
    return {"t": _BOARD_ENVELOPE, "k": key, "v": value}


def _board_labelled(opened: Any) -> bool:
    return (
        isinstance(opened, dict)
        and set(opened) == {"t", "k", "v"}
        and opened.get("t") == _BOARD_ENVELOPE
        and isinstance(opened["k"], str)
    )


#: Sentinel distinguishing "caller did not pass a cipher" from "caller
#: explicitly passed None" (meaning: this one call is unencrypted). Needed
#: because ``WorkspaceCipher | None`` already uses ``None`` for the latter.
_UNSET: Any = object()


#: How many pages `read_channels` walks looking for the tail before it gives
#: up. A runaway guard rather than a policy: messages expire, so a live room
#: does not grow without bound, and at the default `limit` this covers five
#: thousand messages in the busiest channel.
_MAX_TAIL_PAGES = 100


def _peek_params(*, workspace: str, agent_id: str | None,
                 channels: Sequence[str], since: int, limit: int) -> dict[str, Any]:
    """One page of a room-wide peek. Shared so the two clients cannot drift."""
    return {
        "workspace": workspace, "agent_id": agent_id, "channel": list(channels),
        "since": since, "peek": True, "include_own": True, "limit": limit,
    }


class _Tail:
    """Walk a peek forward and keep the newest `limit` messages per channel.

    The hub reads in one direction: `since=N` answers with the `limit`
    messages *after* N, oldest first. A viewer wants the other end of the
    room, and the only route there is to keep asking — so this holds the
    window while the caller pages, and hands back the tail when the paging
    stops.

    One `since` covers every channel in the request, so it may only advance to
    the lowest of the pages that came back full: anything further steps over
    the next message in a quieter channel, which is a message silently lost
    rather than a page saved. The overlap that follows is why messages are
    kept by sequence instead of appended.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._kept: dict[str, dict[int, dict[str, Any]]] = {}

    def absorb(self, page: Sequence[dict[str, Any]]) -> int | None:
        """Take one page in; return the next `since`, or None at the tail."""
        seen: dict[str, list[int]] = {}
        for message in page:
            channel = message.get("hub_channel") or message.get("channel") or ""
            seq = int(message.get("seq") or 0)
            kept = self._kept.setdefault(channel, {})
            kept[seq] = message
            for old in sorted(kept)[: -self._limit]:
                del kept[old]
            seen.setdefault(channel, []).append(seq)
        full = [max(seqs) for seqs in seen.values() if len(seqs) >= self._limit]
        return min(full) if full else None

    def messages(self) -> list[dict[str, Any]]:
        """The window, merged and in hub order."""
        out = [m for kept in self._kept.values() for m in kept.values()]
        out.sort(key=lambda m: int(m.get("seq") or 0))
        return out


@dataclass(frozen=True)
class RoomCheck:
    """What an invite's proof-of-room turned out to say.

    Reaching a hub proves nothing about being in the right room: a wrong key
    connects, registers, and lists you on a roster beside the peers you cannot
    read. Only opening a value the inviter sealed distinguishes "same room"
    from "same hub", which is what a probe is for.
    """

    #: Opened the inviter's proof. False for every other outcome, including
    #: the ones that are nobody's fault, because a caller deciding whether to
    #: start work wants one question answered.
    ok: bool
    #: `verified`, `wrong_room`, or `no_probe`.
    verdict: str
    #: A sentence for a human, already saying what to do about it.
    detail: str


class _Base:
    #: The invite this client was built from, when it was. Carries the probe
    #: `verify()` checks, and is what makes a joined room re-describable.
    invite: Invite | None = None

    @classmethod
    def from_invite(cls, blob: str | Invite, *, agent_id: str | None = None,
                    **kwargs: Any) -> Any:
        """A client for a room somebody sent you.

        The whole of joining, from one string: hub, workspace, token and key
        arrive together, so the four values that must each match a peer's
        cannot be assembled wrongly one at a time. That is the failure this
        exists to remove — every one of them fails *silently*, leaving you
        alone in a room that looks quiet.

        Deliberately does no I/O. Verification is a round trip and belongs to
        the caller's event loop, not a constructor: see `verify()`, which the
        sync and async clients each implement in their own idiom.

        Everything else about this process is kept — identity, timing history,
        peer log — because those are properties of *you*, not of the room you
        were invited to.
        """
        invite = blob if isinstance(blob, Invite) else Invite.decode(blob)
        env = ClientConfig.from_env()
        # A field the invite omits means "you already hold this" — that is what
        # `invite --no-key` and `--no-token` are for — so it falls back to the
        # environment rather than clearing it. Wiping a key this process has
        # would put it into an encrypted room in the clear: alone, on a roster,
        # reading nothing. The silent failure again, wearing the fix's clothes.
        #
        # `resolve_key` is what turns that fallback from a guess into a lookup
        # when the invite *names* the key it left out, and refuses instead of
        # falling back to whichever key happens to be exported.
        key = invite.resolve_key(env.key)
        config = replace(
            env, url=invite.url, url_source="invite", workspace=invite.workspace,
            token=invite.token or env.token, key=key,
            # Carried by an invite meant for a peer who will work in the room;
            # left out of one meant for a viewer, which is then read-only in
            # fact rather than by good behaviour. Falls back to the
            # environment like the key does, and is dropped by the client
            # when it names some other room.
            write_key=invite.resolve_write_key(env.write_key),
        )
        client = cls(config, agent_id=agent_id, key=key, **kwargs)
        client.invite = invite
        return client

    _VERIFIED = RoomCheck(
        True, "verified",
        "opened a sealed value the inviter left in that room, which proves the "
        "hub, the workspace and the key all match — something a roster listing "
        "you both does not")
    #: The same verdict, for a room with no key in it. Worth its own wording
    #: rather than reusing the one above, which would claim a key matched when
    #: there was none to match: in a plaintext room the probe is readable by
    #: anyone who can reach that hub and name that workspace, so `verified`
    #: here is a strictly weaker statement and should say so.
    _VERIFIED_PLAIN = RoomCheck(
        True, "verified",
        "read the value the inviter left, which proves the hub and the "
        "workspace match. This room is not encrypted, so there was no key to "
        "check — anyone who can reach that hub and name that workspace can "
        "read it too")
    _WRONG_ROOM = RoomCheck(
        False, "wrong_room",
        "reached that hub and workspace but could not read the proof the "
        "inviter left, so your key does not match theirs: you would appear on "
        "each other's roster and be able to exchange nothing. Ask for a fresh "
        "invite rather than editing settings")
    _PROBE_GONE = RoomCheck(
        False, "probe_gone",
        "the proof the inviter left is not on the blackboard. Everything here "
        "expires, so it has most likely aged out — nothing suggests your key is "
        "wrong. Ask for a fresh invite if you want this checked")

    def _read_proof(self, value: Any, board: list[dict[str, Any]]) -> RoomCheck:
        """The verdict, once the probe and the board are in hand.

        Two failures look identical from `board_get` alone, and they call for
        opposite responses. A wrong key blinds the probe's name to a token
        nobody stored, so the hub 404s — exactly as it does for a probe that
        has expired. Answering "wrong key" to both sends somebody off to
        re-key a room whose key was fine.

        What separates them is the rest of the board: an entry whose *name*
        did not come back is one this key could not open, and a key that
        cannot open the room's other entries is the explanation. A board that
        reads cleanly and simply lacks the probe is a probe that expired.

        Shared by the sync and async clients so one security answer cannot
        have two wordings, and costs a second read only when the first fails.
        """
        if value == PROBE_SENTINEL:
            return self._verified()
        return self._WRONG_ROOM if self._holds_unreadable(board) else self._PROBE_GONE

    def _verified(self) -> RoomCheck:
        """The pass, worded for what was actually proved."""
        return self._VERIFIED if self.encrypted else self._VERIFIED_PLAIN

    def _holds_unreadable(self, board: list[dict[str, Any]]) -> bool:
        """Whether any board entry came back under a name this key could not
        recover. Meaningless without encryption, where every key is its own
        plaintext — hence the guard, not an oversight."""
        if not self.encrypted:
            return False
        return any(e.get("key") == e.get("hub_key") for e in board)

    def _no_probe(self) -> RoomCheck:
        return RoomCheck(False, "no_probe",
                         "this invite carries no proof-of-room, so nothing here "
                         "can check that these settings reach the room it describes")

    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None,
                 key: str | None = None) -> None:
        self.config = config or ClientConfig.from_env()
        self.workspace = self.config.workspace
        local = agent_id or self.config.agent_id or f"agent-{uuid.uuid4().hex[:12]}"

        key = key if key is not None else self.config.key
        self.cipher = WorkspaceCipher.from_key(key, self.workspace) if key else None
        #: Write keys by the workspace each one names, for `_sign`. The
        #: ambient room's goes in here at construction; a `custom_scope` that
        #: carries one adds its own. Looked up per request by the workspace
        #: the request names, so no call site has to say which to use.
        self._writers: dict[str, RoomWriteKey] = {}
        #: The write key for this client's own room, or None: either the room
        #: is not write-protected, and needs none, or it is and this client is
        #: a reader. `can_write` is the question to ask.
        self.writer = self._writer_for(self.workspace, self.config.write_key)
        #: What this agent calls itself. Never leaves the process when encrypting.
        self.local_agent_id = local
        #: What the hub knows this agent as. Blinding here rather than at each
        #: call site means every existing `agent_id` reference — DM channels,
        #: lease holders, read cursors — is blinded automatically and
        #: consistently, and a roster entry can be handed straight back to
        #: `dm()` because it is already in hub form.
        self.agent_id = self.cipher.blind(local, "agent") if self.cipher else local
        #: DMs waiting for this agent as of the last hub response that
        #: mentioned it. Starts at 0 rather than None so a caller can read it
        #: without a hasattr dance; it means "nothing known to be waiting",
        #: which is also what it means before the first call.
        self.unread_dms = 0
        #: This process's signing identity, generated here and never persisted.
        #: See signing.py for why it must not touch a file: the peers it exists
        #: to distinguish are usually sibling processes sharing a filesystem.
        #: One per Client, so two clients in one process are two agents — which
        #: is what they are.
        # An agent is not one process. If this session's MCP server is
        # listening, sign through it so the hooks, the CLI and the model all
        # speak as one agent rather than as a stream of one-message strangers.
        # The key stays in that one process; only signatures cross.
        self.signing = signing.attach(local) if signing.AVAILABLE else None
        if self.signing is None and signing.AVAILABLE:
            self.signing = SigningIdentity.generate()
        #: Monotonic per-process message counter, so a reader can see gaps.
        self._seq = 0
        #: Signing keys seen for each peer, and the highest sequence read from
        #: each. Both are per-process: identity here does not outlive a process
        #: by design, so neither should the memory of it.
        self._peer_keys: dict[str, set[str]] = {}
        self._peer_seq: dict[tuple[str, str], int] = {}
        #: Whether each peer looked alive when last seen, and which peers have
        #: had their published key change while it did. Per process, like the
        #: identities themselves.
        self._peer_live: dict[str, bool] = {}
        self._peer_key_swaps: set[str] = set()
        #: Keys witnessed by THIS process only. Swap detection reads this and
        #: never the persisted log: an agent without a long-lived signer mints
        #: a keypair per process, so a key changing between processes is
        #: ordinary and says nothing.
        self._peer_keys_seen_here: dict[str, set[str]] = {}
        #: The same witnessing, persisted across processes (see peers.py). What
        #: a peer's key *was* is an observation about somebody else, not this
        #: agent's identity, so unlike the two dicts above it may outlive the
        #: process — and has to, or a turn-based CLI agent can never notice a
        #: swap at all. None disables it entirely, which is what tests and any
        #: caller who wants no on-disk footprint pass.
        peer_db = getattr(config, "peer_log", peers.DEFAULT_PATH)
        self._peer_log = peers.PeerKeyLog(peer_db) if peer_db else None
        #: Peer exchange keys learned from a roster read, keyed by hub-form
        #: agent id. Per-process only, unlike `_peer_log` above: this exists
        #: purely so `whisper()` and inbox's auto-open can find a key they have
        #: already seen, and no security property rides on it persisting —
        #: it is a convenience cache, not a trust store.
        self._peer_exchange_keys: dict[str, str] = {}

    def _writer_for(self, workspace: str, seed: str | None) -> RoomWriteKey | None:
        """Adopt a write key for one workspace, if it is that workspace's.

        A write key names its room, so a key that names another one is not
        "a different key" — it is a misconfiguration, and in a write-
        protected room a loud one: every write would 403 with a message that
        cannot say why. Refused here instead, where the two identifiers can
        be printed side by side. In a room that is not write-protected the
        key is simply irrelevant — a `SWITCHBOARD_WRITE_KEY` exported for one
        room must not stop `-w other` from working — so it is dropped and
        nothing is signed, which also keeps its public half off a hub that
        has no use for it.
        """
        if not seed:
            return None
        writer = RoomWriteKey.from_seed(seed)
        if writer.workspace != workspace:
            if rooms.is_write_protected(workspace):
                raise WriteKeyError(
                    f"the write key names room {writer.workspace}, but this client is "
                    f"in {workspace}. One is derived from the other, so they cannot "
                    "both be right — check SWITCHBOARD_WRITE_KEY against the room "
                    "you meant, or drop the workspace and let the key choose it."
                )
            return None
        self._writers[workspace] = writer
        return writer

    @property
    def can_write(self) -> bool:
        """Whether the hub will accept this client's writes to its own room.

        True in any room that is not write-protected, and in a write-
        protected one exactly when a write key for it is held. A reader in a
        `ws_…` room gets every read and a `ReadOnlyRoom` on every write.
        """
        return not rooms.is_write_protected(self.workspace) or self.writer is not None

    def _sign(self, request: httpx.Request, kwargs: dict[str, Any]) -> None:
        """Sign one outgoing request, when its room is one this client writes.

        Over exactly what goes on the wire — method, path, query and body as
        httpx built them — so the hub verifies the same bytes. Done after
        sealing: the body hash is over ciphertext, which is what lets a hub
        that cannot read the body still check who wrote it.
        """
        body = kwargs.get("json")
        params = kwargs.get("params")
        workspace = None
        if isinstance(body, dict) and isinstance(body.get("workspace"), str):
            workspace = body["workspace"]
        elif isinstance(params, dict) and isinstance(params.get("workspace"), str):
            workspace = params["workspace"]
        writer = self._writers.get(workspace or "")
        if writer is None:
            return
        request.headers.update(writer.sign_request(
            request.method, request.url.path, request.url.query.decode(), request.content,
        ))

    @property
    def encrypted(self) -> bool:
        """Whether this client seals what it sends and opens what it reads.

        The one thing an application cannot infer from a response: an
        encrypted room and a plaintext one look identical on the wire, by
        design. A reader needs it to know whether an identifier it was handed
        — a lease resource, a board key — is a name or a blinded token, and
        those must not be presented the same way.
        """
        return self.cipher is not None

    def _ws(self, workspace: str | None) -> str:
        return workspace or self.workspace

    def _resolve_scope(
        self, custom_scope: dict[str, str] | None, workspace: str | None,
    ) -> tuple[str, WorkspaceCipher | None, str]:
        """Workspace, cipher and blinded agent id to use for one call.

        ``custom_scope`` is a complete, atomic override of all three — set it
        only when you and the specific peers you are coordinating with have
        already agreed, *outside* Switchboard, on a shared workspace and key
        for a private conversation. An agreement one side does not know about
        is not one; never invent a scope unilaterally and expect a peer to
        find their way into it.

        It replaces the ambient workspace and key for this call only. The
        hub and token are unchanged — a custom scope is a different
        namespace and confidentiality boundary on the same hub, not a
        different hub.
        """
        if custom_scope is None:
            return self._ws(workspace), self.cipher, self.agent_id
        ws = custom_scope.get("workspace")
        if not ws:
            raise TypeError("custom_scope requires a non-empty 'workspace'")
        key = custom_scope.get("key")
        cipher = WorkspaceCipher.from_key(key, ws) if key else None
        # A side room minted by `keygen` is write-protected like any other
        # new room, so the scope carries its write key too; `_sign` finds it
        # by the workspace the request names.
        if ws not in self._writers:
            self._writer_for(ws, custom_scope.get("write_key"))
        agent_id = cipher.blind(self.local_agent_id, "agent") if cipher else self.local_agent_id
        return ws, cipher, agent_id

    # --- the encryption boundary -------------------------------------------
    #
    # Done once here, at the transport edge, rather than in each of the ~28
    # client methods. Two copies of that logic — one sync, one async — would be
    # two chances to forget a field, and a forgotten field is plaintext on the
    # wire that nothing would ever flag.

    def key_mismatches(self, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Roster entries whose key differs from ours.

        Detection is simply *can we open this peer's name*, which every agent
        publishes on registration. Nothing extra is transmitted to make this
        work, and that is the point — an earlier version published a key
        fingerprint in the roster, which turned out to be strictly worse:

        * It caught a subset. An agent running with NO key publishes no
          fingerprint at all, so a plaintext agent in an encrypted workspace
          went unflagged — while its unopenable-to-us name catches it.
        * It was a self-asserted claim rather than a demonstration. Opening a
          peer's ciphertext proves shared key possession; a fingerprint field
          can simply be copied by a peer that does not hold the key.
        * It told the hub which agents share a key, and when a key changed,
          neither of which the hub had any way to know before.

        A mismatch is a partition: their messages never reach our inbox, ours
        never reach theirs, and neither side's leases exclude the other. None
        of that raises, so somebody has to look.

        One flag, both directions. We encrypt and cannot open this peer's
        fields; or we do not encrypt and this peer's fields arrived sealed —
        the same partition seen from its two sides, and the second side is the
        one more likely to be misconfigured. Both are marked `unreadable` as
        the response is opened, so this no longer asks the question twice in
        two different ways.
        """
        return [
            a for a in agents
            if a.get("agent_id") != self.agent_id and a.get("unreadable")
        ]

    def _blind_channel(self, channel: str, cipher: Any = _UNSET) -> str:
        if cipher is _UNSET:
            cipher = self.cipher
        return cipher.blind_channel(channel) if cipher else channel

    def peer_id(self, who: str) -> str:
        """The hub-form id a DM to ``who`` would actually be routed to.

        Public because callers need to answer "is this recipient real?", and
        they cannot: `send` blinds whatever string it is given, so a typo and a
        peer between turns produce the same accepted message and the same
        silence. Comparing this against the roster is the only check available.

        Takes either form, exactly as `blind_channel` does — a hub-form id from
        the roster passes through, a local alias is blinded.
        """
        return self._blind_channel(f"@{who}").lstrip("@")

    def _blind(self, value: str, domain: str, cipher: Any = _UNSET) -> str:
        if cipher is _UNSET:
            cipher = self.cipher
        return cipher.blind(value, domain) if cipher else value

    def _sign_message(self, channel: str, body: Any) -> dict[str, Any] | None:
        """The signature block for one outgoing message, or None unsigned."""
        if self.signing is None:
            return None
        self._seq += 1
        payload = signing.message_payload(
            sender=self.agent_id, channel=channel, seq=self._seq, body=body,
        )
        return {"by": self.agent_id, "n": self._seq, "sig": self.signing.sign(payload)}

    def note_peer_keys(self, agents: list[dict[str, Any]]) -> None:
        """Learn signing keys from a roster read, and notice a swap.

        Keys accumulate rather than replace. An agent that restarts publishes a
        new key, and its earlier messages are still legitimately signed by the
        old one — dropping it would turn a normal restart into a wall of
        apparent forgeries.

        The roster entry is a noticeboard entry: an announcement upserts on
        ``(workspace, agent_id)`` and nothing validates it, so anyone may
        announce as anyone and replace the published key. That is what a
        noticeboard without an authority means, and it is not the hub's job to
        adjudicate — a first-writer-wins column there would be a registry in
        disguise, which is the thing that was deliberately removed.

        So the noticing happens here, where the witnessing already lives. A
        *restart* is ordinary: the previous entry had gone stale first. A key
        changing while the same id is still being heartbeated is not, and that
        is the distinction worth surfacing — one an authority is not needed to
        draw.
        """
        log = self._peer_log
        workspace = self.workspace
        for agent in agents:
            agent_id = agent.get("agent_id", "")
            exchange_key = agent.get("exchange_key")
            if isinstance(exchange_key, str) and exchange_key and agent_id != self.agent_id:
                self._peer_exchange_keys[agent_id] = exchange_key
            key = agent.get("pubkey")
            if not isinstance(key, str) or not key:
                continue
            if agent_id == self.agent_id:
                # Never judge ourselves. `key_mismatches` already skips self;
                # this did not, so an agent whose own commands are separate
                # processes — every CLI agent — reported *itself* as having
                # been announced over. Observed in this project's dogfooding
                # within hours of the persisted log shipping.
                continue
            # Two different questions, and conflating them is what produced a
            # detector that fired on ordinary behaviour.
            #
            # For VERIFYING a signature, every key this machine has ever seen
            # this peer use is useful, including ones learned by an earlier
            # process — that is what lets a turn-based CLI agent check a
            # signature at all. Keys accumulate; more is strictly better.
            #
            # For SPOTTING A SWAP, only what *this* process witnessed counts.
            # An agent with no long-lived signer mints a fresh keypair per
            # process (`signing.attach` finds no socket, so the client falls
            # back to `SigningIdentity.generate`), so across processes a key
            # change is the normal case and carries no information. Comparing
            # against the persisted set turned every CLI peer into a permanent
            # false positive the moment it was observed twice.
            seen_here = self._peer_keys_seen_here.setdefault(agent_id, set())
            known = self._peer_keys.setdefault(agent_id, set())
            if log is not None:
                known |= log.known_keys(workspace, agent_id)
            if seen_here and key not in seen_here and self._peer_live.get(agent_id):
                # Seen alive under a different key, in this run, and now another.
                self._peer_key_swaps.add(agent_id)
            if agent_id in self._peer_key_swaps:
                agent["key_changed_while_live"] = True
            seen_here.add(key)
            known.add(key)
            # "Live" as the roster itself reports it, rather than a clock this
            # process keeps: `stale` is the hub's own 60-second judgement and
            # is what a reader would go by.
            live = not agent.get("stale", False)
            self._peer_live[agent_id] = live
            if log is not None:
                log.record(workspace, agent_id, key, live=live)

    def _verify_message(self, item: dict[str, Any], block: Any) -> None:
        """Attach a verdict to one message. Never raises: an unverifiable
        message is information, not an error."""
        sender = item.get("from")
        if not isinstance(block, dict) or "sig" not in block:
            item["signature"] = {"status": "unsigned"}
            return
        known = self._peer_keys.get(sender, set())
        if not known and self._peer_log is not None and sender:
            # Nothing witnessed in this process — the ordinary case for a
            # turn-based agent, which drains an inbox without ever reading a
            # roster. Keys this machine learned earlier are exactly what makes
            # a signature checkable here at all, and this is the only path that
            # reaches them: `note_peer_keys` runs on a roster read, which this
            # caller never made.
            known = self._peer_log.known_keys(self.workspace, sender)
            if known:
                self._peer_keys[sender] = set(known)
        if not known:
            # No key for this sender — usually just a roster we have not read.
            # Distinct from a bad signature, and must not be reported as one.
            item["signature"] = {"status": "unknown", "seq": block.get("n")}
            return
        payload = signing.message_payload(
            sender=block.get("by"), channel=item.get("channel"),
            seq=block.get("n"), body=item.get("body"),
        )
        match = next(
            (k for k in known if signing.verify(k, payload, block["sig"])), None,
        )
        if match is None or block.get("by") != sender:
            item["signature"] = {"status": "mismatch", "seq": block.get("n")}
            return
        verdict: dict[str, Any] = {"status": "verified", "seq": block.get("n"), "key": match}
        # Gaps are per signer *and* per key: a restart resets the counter, and
        # reading that as 40 missing messages would be worse than saying
        # nothing.
        seen = self._peer_seq.get((sender, match))
        if isinstance(seen, int) and isinstance(block.get("n"), int):
            if block["n"] > seen + 1:
                verdict["missing"] = block["n"] - seen - 1
        if isinstance(block.get("n"), int):
            self._peer_seq[(sender, match)] = max(seen or 0, block["n"])
        item["signature"] = verdict

    @property
    def public_key(self) -> str | None:
        """What peers verify this agent's signatures against.

        Published at registration, sealed like any other content, so the hub
        stores an opaque string and learns nothing from it.
        """
        return self.signing.public_key if self.signing else None

    @property
    def exchange_key(self) -> str | None:
        """What a peer seals a `whisper` to. Published at registration, sealed
        like `public_key` above and for the same reason."""
        return self.signing.exchange_key if self.signing else None

    def _peer_exchange_key_for(self, to_agent: str) -> str:
        """The cached exchange key `whisper(to_agent, ...)` would seal to, or a
        clear, actionable error.

        Never falls back to an unsealed `send` — a caller that reached for
        `whisper` explicitly wanted the peer-only property, and silently handing
        back something weaker would be the one failure mode worse than
        raising.
        """
        hub_id = self.peer_id(to_agent)
        key = self._peer_exchange_keys.get(hub_id)
        if key is None:
            raise UnknownPeerExchangeKey(
                f"no exchange key known for {to_agent!r} ({hub_id[:22]}…). "
                "Call agents() to read this peer's exchange key from the "
                "roster before whispering to them — a peer you have never "
                "seen there cannot be whispered to yet; `say`/`dm` them first."
            )
        return key

    def _note_unread(self, result: dict[str, Any]) -> dict[str, Any]:
        """Remember any `unread_dms` the hub volunteered, and pass it through.

        The hub attaches this to responses a client already asks for — posting,
        reading the inbox, reading the roster — so that knowing something is
        waiting costs no extra round trip. Recorded here rather than returned,
        because every caller of `post()` expects a message record back and
        changing that to a pair would break them all for a field most do not
        read.

        The MCP surface has always been told this on every tool call
        (`mcp_server._touch`). Nothing else was, which is why an agent on the
        CLI could be whispered at and never find out.
        """
        if isinstance(result, dict) and "unread_dms" in result:
            value = result["unread_dms"]
            if isinstance(value, int):
                self.unread_dms = value
        return result

    def _seal_whisper_body(self, to_agent: str, body: Any) -> dict[str, Any]:
        """The sealed envelope `whisper` sends as its message body."""
        if self.signing is None:
            raise CryptoError(_WHISPER_MISSING)
        peer_key = self._peer_exchange_key_for(to_agent)
        return seal_to_peer(
            body, my_identity=self.signing, peer_exchange_key=peer_key, context="ask.body",
        )

    def _open_whispers(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Auto-open every whisper-typed message this client currently can.

        Both `"whisper"` and `"ask"` count: `ask` is what 0.11.0 sent, and a
        peer still on it goes on being readable here rather than silently
        arriving as `unreadable`.

        Mirrors the convention `key_mismatches`/`_mark_unopened` already use
        for a workspace-key mismatch: a message this process cannot yet open
        — no signing identity, or a sender whose exchange key has not been
        seen on a roster read — is not an error, so it is never dropped and
        `inbox` never raises for it. It stays in the result with its raw
        sealed envelope as `body` and `unreadable: True`, the same shape
        every other "sealed but I cannot currently open it" case in this file
        already uses.
        """
        for message in messages:
            if message.get("type") not in WHISPER_TYPES:
                continue
            sender = message.get("from")
            peer_key = (
                self._peer_exchange_keys.get(sender)
                if self.signing is not None and sender else None
            )
            if peer_key is None:
                message["unreadable"] = True
                continue
            try:
                message["body"] = unseal_from_peer(
                    message["body"], my_identity=self.signing,
                    peer_exchange_key=peer_key, context="ask.body",
                )
            except DecryptionError:
                message["unreadable"] = True
        return messages

    def _seal_request(
        self, path: str, kwargs: dict[str, Any], cipher: WorkspaceCipher | None,
        *, blind_params: bool = True,
    ) -> dict[str, Any]:
        """Seal and blind an outgoing request.

        `blind_params` is off for the one caller that already holds hub-form
        channel identifiers — `read_channels`. Blinding those again produces
        `blind(blind(c))`, which matches nothing, and the failure is a silent
        undercount rather than an error: the hub simply has no such channel.
        """
        if cipher is None:
            return kwargs
        body = kwargs.get("json")
        if isinstance(body, dict):
            body = dict(body)
            # Blinding is one-way, so a reader cannot recover "deploys" from
            # the token the hub stores. Carry the label inside the ciphertext
            # instead: the hub still sees only the blinded token for routing,
            # and the recipient still gets a readable channel name. Without
            # this, every message an agent reads is labelled with 22 opaque
            # characters, which defeats the point of a coordination tool.
            if path == "/messages" and isinstance(body.get("channel"), str):
                labelled = {"b": body.get("body"), "ch": body["channel"]}
                # Signed here, *before* sealing, so the signature travels
                # inside the ciphertext. A hub cannot read it, alter it, or
                # strip it without breaking the AEAD tag — which is the point:
                # a signature the transport can quietly remove proves nothing.
                block = self._sign_message(body["channel"], body.get("body"))
                if block is not None:
                    labelled["s"] = block
                body["body"] = labelled
            # The same move for the blackboard, and for the same reason: the
            # key the hub stores is `blind(key)`, so without this a listing
            # comes back labelled with 22 opaque characters that cannot be fed
            # back to `board_get` (which would blind them a second time and
            # match nothing) and cannot be filtered by prefix at all.
            if path == "/board" and isinstance(body.get("key"), str):
                body["value"] = _board_envelope(body["key"], body.get("value"))
            for field, context in _SEAL_BODY.get(path, {}).items():
                if body.get(field) is not None:
                    body[field] = (
                        cipher.seal(body[field], context)
                        if context in _JSON_VALUED
                        else cipher.seal_text(body[field], context)
                    )
            for field, domain in _BLIND_BODY.get(path, {}).items():
                value = body.get(field)
                if isinstance(value, str):
                    body[field] = (self._blind_channel(value, cipher) if domain == "channel"
                                   else self._blind(value, domain, cipher))
                elif isinstance(value, list):
                    body[field] = [
                        self._blind_channel(v, cipher) if domain == "channel"
                        else self._blind(v, domain, cipher) for v in value
                    ]
            kwargs["json"] = body

        params = kwargs.get("params")
        if blind_params and isinstance(params, dict) and params.get("channel"):
            params = dict(params)
            channels = params["channel"]
            params["channel"] = (
                [self._blind_channel(c, cipher) for c in channels]
                if isinstance(channels, list) else self._blind_channel(channels, cipher)
            )
            kwargs["params"] = params
        return kwargs

    @staticmethod
    def _keep_hub_channel(payload: dict[str, Any]) -> None:
        """Record what the hub calls each channel, before we rename it.

        Opening a body restores the plaintext channel name over the top of the
        identifier the hub routed on, which is the right thing to show and the
        wrong thing to *lose*: a reader that asked for several channels at once
        can no longer tell which one a message came back from, and neither can
        anything correlating messages with `channels()`. Both names, always —
        in a plaintext room they are simply equal.
        """
        for key in ("messages", "message"):
            target = payload.get(key)
            if not target:
                continue
            for item in (target if isinstance(target, list) else [target]):
                if isinstance(item, dict) and "channel" in item:
                    item.setdefault("hub_channel", item["channel"])

    @staticmethod
    def _keep_hub_key(payload: dict[str, Any]) -> None:
        """The same for board keys, which are about to be renamed the same way.

        `hub_key` is what a delete or a conditional write has to quote back,
        and what two clients comparing listings can actually match on.
        """
        for key in ("entries", "entry"):
            target = payload.get(key)
            if not target:
                continue
            for item in (target if isinstance(target, list) else [target]):
                if isinstance(item, dict) and "key" in item:
                    item.setdefault("hub_key", item["key"])

    def _board_query(
        self, prefix: str | None, workspace: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Split a prefix into the part the hub can do and the part it cannot.

        In a plaintext room the hub matches the prefix itself, which is both
        cheaper and what it has always done. Under encryption it stores
        `blind(key)`, and blinding is not prefix-preserving — a plaintext
        `coord/` compared against opaque tokens matches **nothing**, so the
        hub answered every prefixed listing with an empty list. That is the
        worst possible shape for this failure: the convention's opening move
        is `board_list prefix="coord/"`, and an empty result reads as an empty
        room rather than as a broken query.

        So under encryption the prefix is not sent at all — which also stops
        leaking it to a hub that could not use it anyway — and the filtering
        happens here, against the keys restored from inside the ciphertext.
        """
        params: dict[str, Any] = {"workspace": self._ws(workspace)}
        if not prefix:
            return params, None
        if self.cipher is None:
            params["prefix"] = prefix
            return params, None
        return params, prefix

    @staticmethod
    def _by_prefix(entries: list[dict[str, Any]], prefix: str | None) -> list[dict[str, Any]]:
        """Filter restored entries, keeping the ones we cannot classify.

        An entry is classifiable only if its readable key came back from
        inside the ciphertext — which is exactly when `key` no longer equals
        the `hub_key` it arrived as. The two that fail that test are a peer on
        a different key, and an entry written by a client older than the
        envelope. Neither can be matched against a prefix, and **dropping them
        would be the silent wrong answer this function exists to remove**, so
        they stay, already marked `unreadable` where that applies and counted
        for the caller by the usual reporter.
        """
        if not prefix:
            return entries
        return [
            e for e in entries
            if str(e.get("key", "")).startswith(prefix)
            or e.get("key") == e.get("hub_key")
        ]

    def _mark_unopened(self, payload: dict[str, Any],
                       tolerate: frozenset[str]) -> dict[str, Any]:
        """Flag values this client has no key for, in the places it may.

        A client with no key does not attempt decryption, so nothing raises
        and the envelope arrives intact — which reads, to anything rendering
        it, as an ordinary dict body. Marking it is what lets a caller tell
        "this message is empty" from "this message is sealed and I hold no
        key", and those two must never be shown the same way.
        """
        for key, fields in _OPEN_RESPONSE.items():
            if key not in tolerate or not payload.get(key):
                continue
            target = payload[key]
            for item in (target if isinstance(target, list) else [target]):
                if not isinstance(item, dict):
                    continue
                for field in fields:
                    if looks_sealed(item.get(field)):
                        item[field] = None
                        item["unreadable"] = True
        return payload

    def _open_response(self, payload: Any, cipher: WorkspaceCipher | None,
                       tolerate: frozenset[str] = frozenset()) -> Any:
        """Open what this key can open, and decide what to do about the rest.

        `tolerate` names response keys where a value we cannot open is the
        caller's problem to display rather than an error — the roster always,
        because that is where agents on different keys legitimately meet, plus
        whatever the calling method adds. `read_channels` adds messages: it
        reads channels nobody subscribed it to, so meeting a foreign one is
        expected there in a way it never is in an inbox.
        """
        tolerate = tolerate | _TOLERATE_UNREADABLE
        if not isinstance(payload, dict):
            return payload
        self._keep_hub_channel(payload)
        self._keep_hub_key(payload)
        if cipher is None:
            # Nothing to open, but a keyless reader in an encrypted room still
            # gets envelopes back, and "empty" must not look like "sealed".
            #
            # Roster fields still get a look, below, even here: `pubkey` and
            # `exchange_key` travel in the clear in a genuinely plaintext
            # workspace (there is no cipher on either side to seal them with),
            # so a keyless reader is not "no key", it is "no workspace key" —
            # and `whisper` explicitly needs to keep working without one.
            return self._note_roster(self._mark_unopened(payload, tolerate))
        for key, fields in _OPEN_RESPONSE.items():
            if key not in payload or payload[key] is None:
                continue
            target = payload[key]
            items = target if isinstance(target, list) else [target]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for field, context in fields.items():
                    if item.get(field) is None:
                        continue
                    try:
                        opened = (
                            cipher.unseal(item[field], context)
                            if context in _JSON_VALUED
                            else cipher.unseal_text(item[field], context)
                        )
                    except DecryptionError:
                        if key not in tolerate:
                            raise
                        # A peer on a different key. Mark it and keep going, so
                        # the roster still lists everyone and the mismatch can
                        # be reported precisely instead of as a raw crypto error.
                        item[field] = None
                        item["unreadable"] = True
                        continue
                    if context == "message.body" and _is_labelled(opened):
                        # Restore the readable channel name that travelled
                        # sealed alongside the body.
                        item["channel"] = opened["ch"]
                        block = opened.get("s")
                        opened = opened["b"]
                        item[field] = opened
                        self._verify_message(item, block)
                        continue
                    if context == "board.value" and _board_labelled(opened):
                        # Restore the readable key that travelled sealed
                        # alongside the value; `hub_key` still holds the token.
                        item["key"] = opened["k"]
                        item[field] = opened["v"]
                        continue
                    item[field] = opened

        # Only now: `pubkey` is sealed like every other field, so learning
        # keys before this loop would cache ciphertext. Doing it here rather
        # than in `agents()` means every path returning agents teaches the
        # verifier — heartbeat included, which is what an idle agent calls.
        return self._note_roster(payload)

    def _note_roster(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Learn peer keys from any response that happens to carry a roster
        entry, opened or not. Shared by both branches of `_open_response`
        above — see the comment on the keyless branch for why an unopened
        roster still has something worth learning."""
        for roster_key in ("agents", "agent"):
            value = payload.get(roster_key)
            if isinstance(value, list):
                self.note_peer_keys([a for a in value if isinstance(a, dict)])
            elif isinstance(value, dict):
                self.note_peer_keys([value])
        return payload


class Client(_Base):
    """Synchronous client."""

    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None,
                 timeout: float = 40.0, key: str | None = None,
                 http: httpx.Client | None = None) -> None:
        """``http`` replaces the transport this client would otherwise build.

        The one supported use is reaching a hub that is not on a socket —
        an app running in this same process, via Starlette's ``TestClient``.
        See ``switchboard.testing``, which is the intended way to get one;
        passing your own means you own its lifetime, so ``close()`` leaves it
        open.
        """
        super().__init__(config, agent_id=agent_id, key=key)
        self._owns_http = http is None
        self._http = http if http is not None else httpx.Client(
            base_url=self.config.url,
            headers=_headers(self.config.effective_token()),
            timeout=timeout,
        )

    def verify(self) -> RoomCheck:
        """Check the invite's proof-of-room, if it carried one.

        One board read. Cheap enough to do at join and worth doing there: the
        alternative is finding out an hour later, from an empty room that
        looks like a quiet one.
        """
        if self.invite is None or not self.invite.probe:
            return self._no_probe()
        value = self.board_get(self.invite.probe)
        if value == PROBE_SENTINEL:
            return self._verified()
        return self._read_proof(value, self.board_list())

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _call(self, method: str, path: str, *, cipher: Any = _UNSET,
              blind_params: bool = True, tolerate: frozenset[str] = frozenset(),
              **kwargs: Any) -> dict[str, Any]:
        if cipher is _UNSET:
            cipher = self.cipher
        kwargs = self._seal_request(path, kwargs, cipher, blind_params=blind_params)
        request = self._http.build_request(method, path, **kwargs)
        self._sign(request, kwargs)
        response = self._http.send(request)
        _raise_for(response)
        return self._note_unread(self._open_response(response.json(), cipher, tolerate))

    # --- meta ---
    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health")

    def stats(self, workspace: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/stats", params={"workspace": self._ws(workspace)})

    # --- presence ---
    def register(self, *, name: str, kind: str = "unknown", branch: str | None = None,
                 task: str | None = None, channels: Sequence[str] | None = None,
                 meta: dict[str, Any] | None = None, ttl: float | None = None,
                 back_in: float | None = None,
                 workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/agents/register", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "name": name,
            "kind": kind, "branch": branch, "task": task,
            # None travels as None: "leave my subscriptions alone", as
            # distinct from [] meaning "clear them".
            "channels": None if channels is None else list(channels),
            "meta": meta or {}, "pubkey": self.public_key, "exchange_key": self.exchange_key,
            "ttl": ttl, "back_in": back_in,
        })["agent"]

    def heartbeat(self, *, task: str | None = None, ttl: float | None = None,
                  renew_leases: bool = True, back_in: float | None = None,
                  workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/agents/heartbeat", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "task": task,
            "ttl": ttl, "renew_leases": renew_leases, "back_in": back_in,
        })

    def agents(self, workspace: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", "/agents", params={"workspace": self._ws(workspace)})["agents"]

    def deregister(self, workspace: str | None = None, *,
                   agent_id: str | None = None) -> bool:
        """Drop an agent off the roster. Yourself by default.

        `agent_id` is a **wire** id — the one the roster prints — and is used
        verbatim rather than blinded, because it has already been through that
        transformation. Blinding it a second time is what made a ghost
        unretirable: an agent whose identity had drifted read its old id off
        the roster, passed it back, and was told it was not there while the
        roster went on printing it.
        """
        target = agent_id or self.agent_id
        return self._call("DELETE", f"/agents/{target}",
                          params={"workspace": self._ws(workspace)})["removed"]

    # --- leases ---
    def acquire(self, resource: str, *, note: str | None = None, ttl: float | None = None,
                workspace: str | None = None,
                custom_scope: dict[str, str] | None = None) -> dict[str, Any]:
        ws, cipher, agent_id = self._resolve_scope(custom_scope, workspace)
        return self._call("POST", "/leases/acquire", cipher=cipher, json={
            "workspace": ws, "resource": resource,
            "agent_id": agent_id, "note": note, "ttl": ttl,
        })["lease"]

    def renew(self, resource: str, *, ttl: float | None = None,
              workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/leases/renew", json={
            "workspace": self._ws(workspace), "resource": resource,
            "agent_id": self.agent_id, "ttl": ttl,
        })["lease"]

    def release(self, resource: str, *, force: bool = False,
                workspace: str | None = None,
                custom_scope: dict[str, str] | None = None) -> bool:
        ws, cipher, agent_id = self._resolve_scope(custom_scope, workspace)
        return self._call("POST", "/leases/release", cipher=cipher, json={
            "workspace": ws, "resource": resource,
            "agent_id": agent_id, "force": force,
        })["released"]

    def leases(self, *, holder: str | None = None,
               workspace: str | None = None) -> list[dict[str, Any]]:
        params = {"workspace": self._ws(workspace)}
        if holder:
            params["holder"] = holder
        return self._call("GET", "/leases", params=params)["leases"]

    # --- messages ---
    def post(self, channel: str, body: Any, *, type: str = "note", thread: str | None = None,
             ttl: float | None = None, workspace: str | None = None,
             custom_scope: dict[str, str] | None = None) -> dict[str, Any]:
        ws, cipher, agent_id = self._resolve_scope(custom_scope, workspace)
        return self._call("POST", "/messages", cipher=cipher, json={
            "workspace": ws, "channel": channel, "agent_id": agent_id,
            "body": body, "type": type, "thread": thread, "ttl": ttl,
        })["message"]

    def send(self, to_agent: str, body: Any, **kwargs: Any) -> dict[str, Any]:
        """Direct message — sugar for posting to the recipient's ``@`` channel."""
        return self.post(f"@{to_agent}", body, **kwargs)

    def whisper(self, to_agent: str, body: Any, *, type: str = WHISPER_TYPE,
                ttl: float | None = None,
                workspace: str | None = None) -> dict[str, Any]:
        """A direct message only `to_agent` can read — sealed pairwise with
        their published X25519 exchange key, not with the workspace key.

        Unlike `send`, every other holder of this workspace's key (which, by
        construction, is everyone else in the room) sees the outer message —
        sender, recipient, size, timing — but not the body: it opens for
        `to_agent` alone. See `crypto.seal_to_peer` for how, and
        `docs/encryption.md` for what this costs relative to `custom_scope`.

        The outer transport is still the ordinary workspace-encrypted `send`:
        in an encrypted room the sealed-to-peer envelope is wrapped a second
        time, hiding from the hub even that a whisper happened; in a
        plaintext room only the inner seal protects the content, and that
        confidentiality against fellow workspace members is real either way.

        Raises `UnknownPeerExchangeKey` if `to_agent`'s exchange key has not
        been learned yet — call `agents()` first. Never falls back to an
        unsealed `send`.
        """
        envelope = self._seal_whisper_body(to_agent, body)
        return self.send(to_agent, envelope, type=type, ttl=ttl, workspace=workspace)

    def ask(self, to_agent: str, body: Any, **kwargs: Any) -> dict[str, Any]:
        """Deprecated alias for `whisper`, the name this shipped under in
        0.11.0. Kept because renaming a method one release after publishing
        it is not a reason to break the callers who took it up."""
        return self.whisper(to_agent, body, **kwargs)

    def inbox(self, *, channels: Sequence[str] | None = None, wait: float = 0.0,
              limit: int = 100, peek: bool = False, include_own: bool = False,
              workspace: str | None = None,
              custom_scope: dict[str, str] | None = None) -> list[dict[str, Any]]:
        ws, cipher, agent_id = self._resolve_scope(custom_scope, workspace)
        params: dict[str, Any] = {
            "workspace": ws, "agent_id": agent_id,
            "wait": wait, "limit": limit, "peek": peek, "include_own": include_own,
        }
        if channels:
            params["channel"] = list(channels)
        messages = self._call("GET", "/inbox", cipher=cipher, params=params)["messages"]
        return self._open_whispers(messages)

    def history(self, channel: str, *, limit: int = 50,
                workspace: str | None = None) -> list[dict[str, Any]]:
        """Recent messages on a channel you can name. Cursor untouched.

        For channels you *cannot* name — the identifiers `channels()` hands
        back in an encrypted room — use `read_channels`.
        """
        return self._call("GET", f"/channels/{self._blind_channel(channel)}",
                          params={"workspace": self._ws(workspace),
                                  "limit": limit})["messages"]

    def read_channels(self, channels: Sequence[str], *, limit: int = 50,
                      workspace: str | None = None) -> list[dict[str, Any]]:
        """Every live message across `channels`, in one request.

        The channels are named the way the *hub* names them — the identifiers
        `channels()` returns, which in an encrypted room are blinded tokens
        nobody can turn back into names. That is the whole reason this exists
        alongside `history`: the argument there is a name you chose, the
        argument here is an identifier you were handed, and one method taking
        both is a method that silently reads the wrong thing when a caller
        confuses them. Nothing is blinded on the way out.

        For reading a room you are not part of — a viewer, an audit, a
        dashboard. Three properties follow from that:

        - **Nothing is disturbed.** Every read is a peek with the cursor
          untouched, so no agent's `inbox` loses a message to it, and
          `include_own` is on because an observer wants everything.
        - **It ends at the newest.** The hub only reads forward, so reaching
          the tail of a long room means paging to it: what comes back is the
          last `limit` messages, not the first. A dashboard that polls this
          follows the room instead of re-rendering its opening minutes.
        - **`limit` is per channel**, as it is on the hub. Messages come back
          merged and in hub order regardless.
        - **A channel you cannot open does not fail the call.** Reading a room
          means reading channels nobody subscribed you to, so meeting one
          under a different key is expected: those messages come back with
          `body: None` and `unreadable: True` rather than raising, which is
          what `inbox` still does and should.
        """
        if not channels:
            return []
        tail, since = _Tail(limit), 0
        for _ in range(_MAX_TAIL_PAGES):
            page = self._call(
                "GET", "/inbox", blind_params=False,
                tolerate=frozenset({"messages"}),
                params=_peek_params(
                    workspace=self._ws(workspace), agent_id=self.agent_id,
                    channels=channels, since=since, limit=limit),
            )["messages"]
            nxt = tail.absorb(page)
            if nxt is None or nxt <= since:
                break
            since = nxt
        return tail.messages()

    def channels(self, workspace: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", "/channels",
                          params={"workspace": self._ws(workspace)})["channels"]

    # --- blackboard ---
    def board_set(self, key: str, value: Any, *, ttl: float | None = None,
                  if_revision: int | None = None,
                  workspace: str | None = None) -> dict[str, Any]:
        return self._call("PUT", "/board", json={
            "workspace": self._ws(workspace), "key": key, "value": value,
            "agent_id": self.agent_id, "ttl": ttl, "if_revision": if_revision,
        })["entry"]

    def board_get(self, key: str, *, default: Any = None,
                  workspace: str | None = None) -> Any:
        try:
            entry = self._call("GET", f"/board/{self._blind(key, 'board')}",
                               params={"workspace": self._ws(workspace)})["entry"]
        except SwitchboardError as exc:
            if exc.status == 404:
                return default
            raise
        return entry["value"]

    def board_entry(self, key: str, *, workspace: str | None = None) -> dict[str, Any] | None:
        try:
            return self._call("GET", f"/board/{self._blind(key, 'board')}",
                              params={"workspace": self._ws(workspace)})["entry"]
        except SwitchboardError as exc:
            if exc.status == 404:
                return None
            raise

    def board_list(self, *, prefix: str | None = None,
                   workspace: str | None = None) -> list[dict[str, Any]]:
        params, local = self._board_query(prefix, workspace)
        entries = self._call("GET", "/board", params=params)["entries"]
        return self._by_prefix(entries, local)

    def board_delete(self, key: str, *, workspace: str | None = None) -> bool:
        return self._call("DELETE", f"/board/{self._blind(key, 'board')}",
                          params={"workspace": self._ws(workspace)})["deleted"]


class AsyncClient(_Base):
    """Asynchronous client — same surface as :class:`Client`."""

    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None,
                 timeout: float = 40.0, key: str | None = None,
                 http: httpx.AsyncClient | None = None) -> None:
        """``http`` replaces the transport, as on :class:`Client`.

        The async case has a native answer — ``httpx.ASGITransport`` speaks to
        an app object directly — so this takes a fully built
        ``httpx.AsyncClient`` and, as there, does not close what it did not
        open.
        """
        super().__init__(config, agent_id=agent_id, key=key)
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(
            base_url=self.config.url,
            headers=_headers(self.config.effective_token()),
            timeout=timeout,
        )

    async def verify(self) -> RoomCheck:
        """The async half of `Client.verify`. Same verdicts, same wording."""
        if self.invite is None or not self.invite.probe:
            return self._no_probe()
        value = await self.board_get(self.invite.probe)
        if value == PROBE_SENTINEL:
            return self._verified()
        return self._read_proof(value, await self.board_list())

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _call(self, method: str, path: str, *, cipher: Any = _UNSET,
                    blind_params: bool = True, tolerate: frozenset[str] = frozenset(),
                    **kwargs: Any) -> dict[str, Any]:
        if cipher is _UNSET:
            cipher = self.cipher
        kwargs = self._seal_request(path, kwargs, cipher, blind_params=blind_params)
        request = self._http.build_request(method, path, **kwargs)
        self._sign(request, kwargs)
        response = await self._http.send(request)
        _raise_for(response)
        return self._note_unread(self._open_response(response.json(), cipher, tolerate))

    async def health(self) -> dict[str, Any]:
        return await self._call("GET", "/health")

    async def stats(self, workspace: str | None = None) -> dict[str, Any]:
        return await self._call("GET", "/stats", params={"workspace": self._ws(workspace)})

    async def register(self, *, name: str, kind: str = "unknown", branch: str | None = None,
                       task: str | None = None, channels: Sequence[str] | None = None,
                       meta: dict[str, Any] | None = None, ttl: float | None = None,
                       back_in: float | None = None,
                       workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("POST", "/agents/register", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "name": name,
            "kind": kind, "branch": branch, "task": task,
            # None travels as None: "leave my subscriptions alone", as
            # distinct from [] meaning "clear them".
            "channels": None if channels is None else list(channels),
            "meta": meta or {}, "pubkey": self.public_key, "exchange_key": self.exchange_key,
            "ttl": ttl, "back_in": back_in,
        })
        return result["agent"]

    async def heartbeat(self, *, task: str | None = None, ttl: float | None = None,
                        renew_leases: bool = True, back_in: float | None = None,
                        workspace: str | None = None) -> dict[str, Any]:
        return await self._call("POST", "/agents/heartbeat", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "task": task,
            "ttl": ttl, "renew_leases": renew_leases, "back_in": back_in,
        })

    async def agents(self, workspace: str | None = None) -> list[dict[str, Any]]:
        result = await self._call("GET", "/agents", params={"workspace": self._ws(workspace)})
        return result["agents"]

    async def deregister(self, workspace: str | None = None) -> bool:
        result = await self._call("DELETE", f"/agents/{self.agent_id}",
                                  params={"workspace": self._ws(workspace)})
        return result["removed"]

    async def acquire(self, resource: str, *, note: str | None = None, ttl: float | None = None,
                      workspace: str | None = None,
                      custom_scope: dict[str, str] | None = None) -> dict[str, Any]:
        ws, cipher, agent_id = self._resolve_scope(custom_scope, workspace)
        result = await self._call("POST", "/leases/acquire", cipher=cipher, json={
            "workspace": ws, "resource": resource,
            "agent_id": agent_id, "note": note, "ttl": ttl,
        })
        return result["lease"]

    async def renew(self, resource: str, *, ttl: float | None = None,
                    workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("POST", "/leases/renew", json={
            "workspace": self._ws(workspace), "resource": resource,
            "agent_id": self.agent_id, "ttl": ttl,
        })
        return result["lease"]

    async def release(self, resource: str, *, force: bool = False,
                      workspace: str | None = None,
                      custom_scope: dict[str, str] | None = None) -> bool:
        ws, cipher, agent_id = self._resolve_scope(custom_scope, workspace)
        result = await self._call("POST", "/leases/release", cipher=cipher, json={
            "workspace": ws, "resource": resource,
            "agent_id": agent_id, "force": force,
        })
        return result["released"]

    async def leases(self, *, holder: str | None = None,
                     workspace: str | None = None) -> list[dict[str, Any]]:
        params = {"workspace": self._ws(workspace)}
        if holder:
            params["holder"] = holder
        result = await self._call("GET", "/leases", params=params)
        return result["leases"]

    async def post(self, channel: str, body: Any, *, type: str = "note",
                   thread: str | None = None, ttl: float | None = None,
                   workspace: str | None = None,
                   custom_scope: dict[str, str] | None = None) -> dict[str, Any]:
        ws, cipher, agent_id = self._resolve_scope(custom_scope, workspace)
        result = await self._call("POST", "/messages", cipher=cipher, json={
            "workspace": ws, "channel": channel, "agent_id": agent_id,
            "body": body, "type": type, "thread": thread, "ttl": ttl,
        })
        return result["message"]

    async def send(self, to_agent: str, body: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.post(f"@{to_agent}", body, **kwargs)

    async def whisper(self, to_agent: str, body: Any, *, type: str = WHISPER_TYPE,
                      ttl: float | None = None,
                      workspace: str | None = None) -> dict[str, Any]:
        """The async half of `Client.whisper`. Same envelope, same guarantee."""
        envelope = self._seal_whisper_body(to_agent, body)
        return await self.send(to_agent, envelope, type=type, ttl=ttl, workspace=workspace)

    async def ask(self, to_agent: str, body: Any, **kwargs: Any) -> dict[str, Any]:
        """Deprecated alias for `whisper`. See `Client.ask`."""
        return await self.whisper(to_agent, body, **kwargs)

    async def inbox(self, *, channels: Sequence[str] | None = None, wait: float = 0.0,
                    limit: int = 100, peek: bool = False, include_own: bool = False,
                    workspace: str | None = None,
                    custom_scope: dict[str, str] | None = None) -> list[dict[str, Any]]:
        ws, cipher, agent_id = self._resolve_scope(custom_scope, workspace)
        params: dict[str, Any] = {
            "workspace": ws, "agent_id": agent_id,
            "wait": wait, "limit": limit, "peek": peek, "include_own": include_own,
        }
        if channels:
            params["channel"] = list(channels)
        result = await self._call("GET", "/inbox", cipher=cipher, params=params)
        return self._open_whispers(result["messages"])

    async def history(self, channel: str, *, limit: int = 50,
                      workspace: str | None = None) -> list[dict[str, Any]]:
        """Recent messages on a channel you can name. Cursor untouched."""
        result = await self._call("GET", f"/channels/{self._blind_channel(channel)}",
                                  params={"workspace": self._ws(workspace), "limit": limit})
        return result["messages"]

    async def read_channels(self, channels: Sequence[str], *, limit: int = 50,
                            workspace: str | None = None) -> list[dict[str, Any]]:
        """Every live message across `channels`, in one request.

        See `Client.read_channels`, including the paging to the tail. The two
        clients must not drift: an async application reads a room it is not
        part of for the same reasons and meets the same wall in the same
        place."""
        if not channels:
            return []
        tail, since = _Tail(limit), 0
        for _ in range(_MAX_TAIL_PAGES):
            result = await self._call(
                "GET", "/inbox", blind_params=False,
                tolerate=frozenset({"messages"}),
                params=_peek_params(
                    workspace=self._ws(workspace), agent_id=self.agent_id,
                    channels=channels, since=since, limit=limit),
            )
            nxt = tail.absorb(result["messages"])
            if nxt is None or nxt <= since:
                break
            since = nxt
        return tail.messages()

    async def channels(self, workspace: str | None = None) -> list[dict[str, Any]]:
        result = await self._call("GET", "/channels",
                                  params={"workspace": self._ws(workspace)})
        return result["channels"]

    async def board_set(self, key: str, value: Any, *, ttl: float | None = None,
                        if_revision: int | None = None,
                        workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("PUT", "/board", json={
            "workspace": self._ws(workspace), "key": key, "value": value,
            "agent_id": self.agent_id, "ttl": ttl, "if_revision": if_revision,
        })
        return result["entry"]

    async def board_get(self, key: str, *, default: Any = None,
                        workspace: str | None = None) -> Any:
        try:
            result = await self._call("GET", f"/board/{self._blind(key, 'board')}",
                                      params={"workspace": self._ws(workspace)})
        except SwitchboardError as exc:
            if exc.status == 404:
                return default
            raise
        return result["entry"]["value"]

    async def board_list(self, *, prefix: str | None = None,
                         workspace: str | None = None) -> list[dict[str, Any]]:
        params, local = self._board_query(prefix, workspace)
        result = await self._call("GET", "/board", params=params)
        return self._by_prefix(result["entries"], local)

    async def board_delete(self, key: str, *, workspace: str | None = None) -> bool:
        result = await self._call("DELETE", f"/board/{self._blind(key, 'board')}",
                                  params={"workspace": self._ws(workspace)})
        return result["deleted"]
