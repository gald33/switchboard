"""Session handoff: carry a whole conversation to another agent through the hub.

Switchboard already has a rule for moving work between agents — *payload on the
blackboard, pointer in a message* — and this module applies it to the largest
payload an agent has: its own session. The sender packages the harness's
native transcript into a capsule (``switchboard.claude_session`` does that for
Claude Code), this module puts the capsule on the board under a guessable key
and sends the recipient a small pointer, and the receiver's tool collects the
capsule, installs it where the harness will find it, and deletes the board
entry. Then ``claude --resume <id>`` picks the conversation up where it left
off, on the other machine.

Four things are deliberate and worth stating because they are easy to undo:

*The hub knows nothing.* A capsule is an ordinary board value and the pointer
is an ordinary direct message. In a keyed room both are sealed client-side
like everything else, so the hub holds ciphertext of a payload it cannot even
name — the pointer's discriminator travels *inside* the sealed body, and the
message type stays the default, so not even the row type says what moved.
No endpoint, no schema, no fifth primitive.

*Nothing here needs a model.* Every step is a function a human, a hook, a
parked listener or an LLM can call the same way: ``publish`` does export,
board write and pointer in one call; ``receive`` does read, verify, claim,
install and acknowledge in one call. The CLI and the MCP bridge are both thin
over these.

*The hub is a wire, not a store.* A transcript is everything the session read,
and every ``board_list`` in a keyed room fetches every value for as long as it
lives — the prefix cannot hide it there, because keys are blinded. So a
capsule asks for a short TTL, ten minutes by default, and the receiver
deletes it as the very first step of collecting it. A handoff nobody picked
up is re-sent, not recovered.

*A transcript is instructions.* Whoever resumes it runs a conversation that
someone else wrote, with their own credentials and repo. So a capsule is not
installed from a pointer unless the pointer's signature verifies against a
key on the roster and the capsule is the bytes that signed pointer announced;
a plaintext room is refused rather than warned about; and nothing here ever
starts ``claude`` on its own — resuming is a separate, explicit step with its
own allowlist on the CLI.

What this does *not* protect against, and says so: any holder of the room key
can delete or replace a board entry (the hub has no owners), so a handoff can
be made to vanish in transit. That is the room's existing trust model, not a
new one — the signature check above means it cannot be *substituted* without
the receiver noticing.

Harness specifics stay behind ``HARNESSES``: the capsule's ``source_harness``
names the module that can install it, and a second harness is an entry here,
not a change to the transport.
"""

from __future__ import annotations

from typing import Any

from . import claude_session, holds
from .client import Client, SwitchboardError
from .config import MAX_BOARD_TTL

#: Where a capsule in transit lives. Its own prefix rather than ``coord/`` so
#: an *unkeyed* ``arrive`` (which lists ``coord/``) never pulls it; in a keyed
#: room every lister pays for it regardless, which the short TTL bounds.
#: Documented in the skill's key-shape table.
PREFIX = "sessions/"

#: The discriminators, all inside sealed bodies.
ENVELOPE_TYPE = "session-capsule/1"
POINTER_TYPE = "session-handoff/1"
RECEIPT_TYPE = "session-received/1"

#: How long a capsule waits to be collected. Short on purpose — see above.
DEFAULT_TTL = 600.0

#: Modules that can package and install a capsule, by the harness name the
#: capsule carries. Free-form on the wire (core enumerates nothing); this is
#: only what *this* installation knows how to resume.
HARNESSES: dict[str, Any] = {claude_session.HARNESS: claude_session}

MEANS = (
    "a session in transit between agents: a whole conversation, packaged. Collect it "
    "with `switchboard session receive` or the session_import tool; do not board_get "
    "it (it is large and useless as text). It expires on its own and is deleted the "
    "moment it is collected."
)

PLAINTEXT_REFUSAL = (
    "this room is not encrypted, so the hub would hold the whole transcript — every "
    "file and secret the session read — in the clear. Hand off inside a keyed room, "
    "or pass allow_plaintext / --allow-plaintext if the hub is yours and local."
)


class HandoffError(Exception):
    """A handoff could not be published or collected.

    Its own class so the MCP bridge can name it, and because ``ValueError``
    is what the bridge reports as ``unknown_tool``.
    """


def key_for(session_id: str) -> str:
    return f"{PREFIX}{session_id}"


def harness_for(name: str | None) -> Any:
    try:
        return HARNESSES[name or ""]
    except KeyError:
        known = ", ".join(sorted(HARNESSES)) or "none"
        raise HandoffError(
            f"no harness here can resume {name!r} sessions (known: {known})"
        ) from None


def _summary(capsule: dict[str, Any]) -> dict[str, Any]:
    harness = harness_for((capsule.get("source_harness") or {}).get("name"))
    return harness.summary(capsule)


# -------------------------------------------------------------- sending ---

def package_current(
    session_id: str | None = None,
    *,
    harness: str = claude_session.HARNESS,
    config_dir: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """The capsule for this process's own session, or for ``session_id``.

    Inside a Claude Code session the id comes from the environment, which
    stdio MCP servers and hook commands inherit; nothing else can tell a tool
    which conversation it is part of.
    """
    module = harness_for(harness)
    sid = session_id or module.current_session_id()
    if not sid:
        raise HandoffError(
            f"no session id: pass one, or run this inside a session where "
            f"{module.SESSION_ID_VAR} is set"
        )
    try:
        return module.package(sid, config_dir=config_dir, cwd=cwd)
    except module.CapsuleError as exc:
        raise HandoffError(str(exc)) from exc


def held_resources(hub: Client) -> list[str]:
    """What this agent holds, as the hub lists it (blinded tokens under a key)."""
    return [str(lease["resource"]) for lease in hub.leases(holder=hub.agent_id)
            if lease.get("resource")]


def release_all(hub: Client) -> list[str]:
    """Drop every lease this agent holds, and its own declarations with them.

    What the Stop hook does when a session ends. Not the default at handoff:
    the sender is still running when it calls this, the receiver may never
    appear, and a lease nobody holds is exactly the window a third agent walks
    into. Opt in when the handoff really is this side's last act.
    """
    released: list[str] = []
    for resource in held_resources(hub):
        try:
            if hub.release(resource):
                released.append(resource)
            holds.clear_own_declaration(hub, resource)
        except SwitchboardError:
            continue
    return released


def publish(
    hub: Client,
    capsule: dict[str, Any],
    *,
    to: str | None = None,
    ttl: float | None = None,
    release_leases: bool = False,
    allow_plaintext: bool = False,
) -> dict[str, Any]:
    """Put a capsule on the board and, if ``to`` is given, point an agent at it.

    ``to`` is a hub-form agent id from the roster (the CLI resolves names and
    branches before calling this; the library does not, because a message to
    a name is accepted by the hub and read by nobody). Without ``to`` the
    capsule is simply published — a checkpoint that anyone holding the key
    can collect by session id while it lasts, on their own say-so, since no
    pointer vouches for it.

    Overwrites an earlier capsule for the same session: a session has one
    current state and the newer one is it.
    """
    if not hub.encrypted and not allow_plaintext:
        raise HandoffError(PLAINTEXT_REFUSAL)
    summary = _summary(capsule)
    session_id = summary["session_id"]
    ttl = DEFAULT_TTL if ttl is None else min(float(ttl), MAX_BOARD_TTL)
    if ttl <= 0:
        raise HandoffError("ttl must be positive")
    key = key_for(session_id)
    sha256 = capsule["files"][0]["sha256"]
    held = held_resources(hub)
    envelope = {
        "t": ENVELOPE_TYPE,
        "harness": summary["harness"],
        "session_id": session_id,
        "from": {"agent_id": hub.agent_id, "who": hub.local_agent_id},
        "to": to,
        "bytes": summary["bytes"],
        "sha256": sha256,
        "summary": summary,
        "means": MEANS,
        "capsule": capsule,
    }
    entry = hub.board_set(key, envelope, ttl=ttl)
    released = release_all(hub) if release_leases else []
    out: dict[str, Any] = {
        "session_id": session_id,
        "key": key,
        "revision": entry.get("revision"),
        "expires_at": entry.get("expires_at"),
        "expires_in": entry.get("expires_in"),
        "bytes": summary["bytes"],
        "files": summary["files"],
        "to": to,
        "held_leases": [r for r in held if r not in released],
        "released_leases": released,
        "encrypted": hub.encrypted,
    }
    if to:
        # Small, and signed like every message: the receiver checks the
        # signature against the roster and the capsule against this hash, so
        # what it installs is what a known sender announced. No imperative
        # text: the receiver's tooling knows what to do with a pointer, and a
        # sender-authored instruction in a DM is a channel into a model.
        pointer = {
            "t": POINTER_TYPE,
            "session_id": session_id,
            "key": key,
            "harness": summary["harness"],
            "bytes": summary["bytes"],
            "files": summary["files"],
            "sha256": sha256,
            "expires_at": entry.get("expires_at"),
            "from": hub.local_agent_id,
            "branch": summary.get("git_branch"),
            "cwd": summary.get("original_working_directory"),
            "held_leases": out["held_leases"],
            "released_leases": released,
        }
        msg = hub.send(to, pointer, ttl=ttl)
        out["seq"] = msg.get("seq")
    return out


def handoff(
    hub: Client,
    *,
    to: str | None = None,
    session_id: str | None = None,
    ttl: float | None = None,
    release_leases: bool = False,
    allow_plaintext: bool = False,
    config_dir: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Export this session and publish it, in one step."""
    capsule = package_current(session_id, config_dir=config_dir, cwd=cwd)
    return publish(hub, capsule, to=to, ttl=ttl, release_leases=release_leases,
                   allow_plaintext=allow_plaintext)


# ------------------------------------------------------------ receiving ---

def _is_pointer(message: dict[str, Any]) -> bool:
    body = message.get("body")
    return (isinstance(body, dict) and body.get("t") == POINTER_TYPE
            and bool(body.get("session_id")) and bool(body.get("sha256")))


def take_delivery(hub: Client, *, wait: float = 0.0) -> dict[str, Any]:
    """Read this agent's direct messages and pick the handoff pointers out.

    A committed read of the ``@me`` channel only — not a peek. A peeked
    pointer comes back on every read for its whole TTL, keeps ``unread_dms``
    raised, and wakes a parked listener again the instant it parks; a handoff
    would be collected once and *re-attempted* for ten minutes. Everything
    else that came out with the pointers is returned as ``other`` so the
    caller can show it rather than lose it — a receiver that runs unattended
    should be an agent id nothing else writes to.

    The roster is read first so signatures can be checked: a pointer whose
    sender is not on the roster, or whose signature does not verify, is
    ``unverified`` and is never installed on its own.
    """
    hub.agents()
    messages = hub.inbox(channels=[f"@{hub.agent_id}"], wait=wait, limit=1000)
    pointers: dict[str, dict[str, Any]] = {}
    other: list[dict[str, Any]] = []
    for message in messages:
        if not _is_pointer(message):
            other.append(message)
            continue
        body = message["body"]
        verdict = message.get("signature") or {}
        pointers[str(body["session_id"])] = {
            **body,
            "seq": message.get("seq"),
            "from_agent": message.get("from"),
            "verified": verdict.get("status") == "verified",
            "signature": verdict.get("status", "unsigned"),
        }
    return {"pointers": list(pointers.values()), "other": other}


def fetch(hub: Client, session_id: str) -> dict[str, Any] | None:
    """The envelope for ``session_id``, or ``None`` if none is on the board."""
    entry = hub.board_entry(key_for(session_id))
    if entry is None:
        return None
    envelope = entry.get("value")
    if not isinstance(envelope, dict) or envelope.get("t") != ENVELOPE_TYPE:
        raise HandoffError(f"the board entry at {key_for(session_id)} is not a session capsule")
    if not isinstance(envelope.get("capsule"), dict):
        raise HandoffError(f"the capsule at {key_for(session_id)} is malformed")
    envelope["_entry"] = {k: entry.get(k) for k in ("revision", "updated_by", "expires_in")}
    return envelope


def consume(hub: Client, session_id: str) -> bool:
    """Take the capsule off the board. True for exactly one caller."""
    return hub.board_delete(key_for(session_id))


def _not_installed(session_id: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"session_id": session_id, "installed": False, "reason": reason, **extra}


def receive_one(
    hub: Client,
    session_id: str,
    *,
    pointer: dict[str, Any] | None = None,
    config_dir: str | None = None,
    cwd: str | None = None,
    force: bool = False,
    unverified: bool = False,
    keep: bool = False,
    acknowledge: bool = True,
) -> dict[str, Any]:
    """Collect one capsule: fetch, verify, claim, install, acknowledge.

    With a ``pointer`` the capsule must be what the pointer's verified sender
    announced (same sha256). Without one — collecting a checkpoint by id —
    there is nobody vouching, so the caller is trusting the room; that is
    reported as ``verified: None`` rather than hidden.

    The board delete comes *before* the install and doubles as the claim:
    the hub answers it inside one transaction, so of two receivers exactly
    one gets ``True`` and the other is told the capsule was claimed. If the
    install then fails, the capsule is put back for whatever TTL it had left.

    Every outcome that is an answer rather than a failure comes back as data
    with ``installed: False`` and a reason: expired, claimed elsewhere,
    unverified, not what was announced.
    """
    if pointer is not None and not pointer.get("verified") and not unverified:
        return _not_installed(
            session_id,
            f"the pointer's signature is {pointer.get('signature', 'unsigned')}, so nobody on "
            f"the roster vouches for this capsule; pass unverified=True to collect it anyway",
            signature=pointer.get("signature"),
        )
    envelope = fetch(hub, session_id)
    if envelope is None:
        return _not_installed(
            session_id,
            "no capsule on the board: it expired, was already collected, or was never published",
        )
    if pointer is not None and envelope.get("sha256") != pointer.get("sha256"):
        return _not_installed(
            session_id,
            "the capsule on the board is not the one the pointer announced (sha256 differs); "
            "it was republished or replaced — ask the sender for a fresh pointer",
        )
    harness = harness_for(envelope.get("harness"))
    capsule = envelope["capsule"]
    try:
        harness.validate(capsule)
    except harness.CapsuleError as exc:
        raise HandoffError(str(exc)) from exc

    claimed = False
    if not keep:
        if not consume(hub, session_id):
            return _not_installed(session_id, "claimed by another receiver first")
        claimed = True
    try:
        result = harness.install(capsule, config_dir=config_dir, cwd=cwd, force=force)
    except harness.CapsuleError as exc:
        if claimed:
            remaining = (envelope.get("_entry") or {}).get("expires_in")
            envelope.pop("_entry", None)
            with_ttl = float(remaining) if remaining and remaining > 0 else DEFAULT_TTL
            try:
                hub.board_set(key_for(session_id), envelope, ttl=with_ttl)
            except SwitchboardError:
                pass
        raise HandoffError(str(exc)) from exc

    sender = (envelope.get("from") or {}).get("agent_id")
    acknowledged = False
    if acknowledge and sender:
        try:
            hub.send(sender, {
                "t": RECEIPT_TYPE, "session_id": session_id,
                "by": hub.local_agent_id, "resume": result.get("resume"),
            })
            acknowledged = True
        except SwitchboardError:
            acknowledged = False
    return {
        **result,
        "installed": True,
        "verified": pointer.get("verified") if pointer is not None else None,
        "harness": envelope.get("harness"),
        "from": envelope.get("from"),
        "bytes": envelope.get("bytes"),
        "held_leases": (pointer or {}).get("held_leases") or [],
        "deleted_from_board": claimed,
        "acknowledged": acknowledged,
    }


def receive(
    hub: Client,
    *,
    session_id: str | None = None,
    wait: float = 0.0,
    config_dir: str | None = None,
    cwd: str | None = None,
    force: bool = False,
    unverified: bool = False,
    keep: bool = False,
) -> dict[str, Any]:
    """Collect what was handed to this agent.

    With ``session_id``, exactly that capsule, on this caller's own say-so.
    Without, every capsule a verified pointer in the inbox names — waiting up
    to ``wait`` seconds for one to arrive — so a parked receiver can run this
    in a loop with no model in it.
    """
    other: list[dict[str, Any]] = []
    if session_id:
        results = [receive_one(hub, session_id, config_dir=config_dir, cwd=cwd, force=force,
                               unverified=unverified, keep=keep)]
        seen = 0
    else:
        delivery = take_delivery(hub, wait=wait)
        other = delivery["other"]
        seen = len(delivery["pointers"])
        results = [
            receive_one(hub, p["session_id"], pointer=p, config_dir=config_dir, cwd=cwd,
                        force=force, unverified=unverified, keep=keep)
            for p in delivery["pointers"]
        ]
    installed = [r for r in results if r.get("installed")]
    return {
        "listening_as": hub.agent_id,
        "installed": installed,
        "missing": [r for r in results if not r.get("installed")],
        "pointers": seen,
        "other": other,
        "resume": [r["resume"] for r in installed],
    }
