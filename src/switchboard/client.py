"""HTTP client for a Switchboard hub.

Depends only on ``httpx`` so an agent can talk to a hub without installing the
server. Both a sync and an async client are provided; the CLI uses the sync
one, the MCP bridge uses the async one.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from . import signing
from .config import ClientConfig
from .crypto import DecryptionError, WorkspaceCipher, is_sealed
from .signing import SigningIdentity

__all__ = [
    "SwitchboardError",
    "LeaseHeld",
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


# --- identity ---------------------------------------------------------------


@dataclass
class Identity:
    """Who this agent is, inferred from the environment it is running in."""

    agent_id: str
    name: str
    kind: str
    branch: str | None
    meta: dict[str, Any]


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

    if agent_id is None:
        agent_id = os.environ.get("SWITCHBOARD_AGENT_ID")
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
    return Identity(agent_id=agent_id, name=name, kind=kind, branch=branch, meta=meta)


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text}
    detail = payload.get("detail") or payload.get("error") or response.text
    if payload.get("error") == "lease_conflict":
        raise LeaseHeld(str(detail), status=response.status_code, payload=payload)
    raise SwitchboardError(str(detail), status=response.status_code, payload=payload)


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


#: Which body fields, at which paths, are payloads to seal — and under what
#: context label. The context is bound into the ciphertext, so a hub cannot
#: move a sealed value from one field to another and have it still open.
_SEAL_BODY: dict[str, dict[str, str]] = {
    "/agents/register": {"name": "agent.name", "branch": "agent.branch",
                         "task": "agent.task", "pubkey": "agent.pubkey"},
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
               "pubkey": "agent.pubkey"},
    "agent": {"name": "agent.name", "branch": "agent.branch", "task": "agent.task",
              "pubkey": "agent.pubkey"},
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
#: Everywhere else stays strict. A message body we cannot open never reaches us
#: in the first place (a mismatched sender blinds to a different channel), so
#: an unopenable one is genuinely suspicious and should still raise.
_TOLERATE_UNREADABLE = {"agents", "agent"}


def _looks_sealed(value: Any) -> bool:
    """Is this value an envelope, in either form it travels in?

    Payload fields carry an envelope dict; text fields (an agent's name, a
    lease note) carry the envelope *serialized to a string*, because the wire
    schema types them as strings. A check that only knew about the dict form
    silently returned False for every text field — which is exactly the form
    an unencrypted client meets when it looks at an encrypted peer.
    """
    if is_sealed(value):
        return True
    if isinstance(value, str) and value.startswith("{"):
        try:
            return is_sealed(json.loads(value))
        except ValueError:
            return False
    return False


def _is_labelled(opened: Any) -> bool:
    """Is this an opened message body carrying its own channel label?

    Checked structurally rather than assumed, so a body that happens to be a
    dict from an older client still round-trips instead of being mangled.
    """
    return isinstance(opened, dict) and set(opened) == {"b", "ch"} \
        and isinstance(opened["ch"], str)


#: Sentinel distinguishing "caller did not pass a cipher" from "caller
#: explicitly passed None" (meaning: this one call is unencrypted). Needed
#: because ``WorkspaceCipher | None`` already uses ``None`` for the latter.
_UNSET: Any = object()


class _Base:
    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None,
                 key: str | None = None) -> None:
        self.config = config or ClientConfig.from_env()
        self.workspace = self.config.workspace
        local = agent_id or self.config.agent_id or f"agent-{uuid.uuid4().hex[:12]}"

        key = key if key is not None else self.config.key
        self.cipher = WorkspaceCipher.from_key(key, self.workspace) if key else None
        #: What this agent calls itself. Never leaves the process when encrypting.
        self.local_agent_id = local
        #: What the hub knows this agent as. Blinding here rather than at each
        #: call site means every existing `agent_id` reference — DM channels,
        #: lease holders, read cursors — is blinded automatically and
        #: consistently, and a roster entry can be handed straight back to
        #: `dm()` because it is already in hub form.
        self.agent_id = self.cipher.blind(local, "agent") if self.cipher else local
        #: This process's signing identity, generated here and never persisted.
        #: See signing.py for why it must not touch a file: the peers it exists
        #: to distinguish are usually sibling processes sharing a filesystem.
        #: One per Client, so two clients in one process are two agents — which
        #: is what they are.
        self.signing = SigningIdentity.generate() if signing.AVAILABLE else None

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
        """
        out = []
        for a in agents:
            if a.get("agent_id") == self.agent_id:
                continue
            if self.cipher:
                # We encrypt; a peer whose fields we cannot open does not
                # share our key — or holds none at all.
                if a.get("unreadable"):
                    out.append(a)
            elif _looks_sealed(a.get("name")) or _looks_sealed(a.get("branch")):
                # We do NOT encrypt but this peer does. Same partition, seen
                # from the other side — and the side more likely to be the
                # one that is misconfigured.
                out.append(a)
        return out

    def _blind_channel(self, channel: str, cipher: Any = _UNSET) -> str:
        if cipher is _UNSET:
            cipher = self.cipher
        return cipher.blind_channel(channel) if cipher else channel

    def _blind(self, value: str, domain: str, cipher: Any = _UNSET) -> str:
        if cipher is _UNSET:
            cipher = self.cipher
        return cipher.blind(value, domain) if cipher else value

    @property
    def public_key(self) -> str | None:
        """What peers verify this agent's signatures against.

        Published at registration, sealed like any other content, so the hub
        stores an opaque string and learns nothing from it.
        """
        return self.signing.public_key if self.signing else None

    def _seal_request(
        self, path: str, kwargs: dict[str, Any], cipher: WorkspaceCipher | None,
    ) -> dict[str, Any]:
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
                body["body"] = {"b": body.get("body"), "ch": body["channel"]}
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
        if isinstance(params, dict) and params.get("channel"):
            params = dict(params)
            channels = params["channel"]
            params["channel"] = (
                [self._blind_channel(c, cipher) for c in channels]
                if isinstance(channels, list) else self._blind_channel(channels, cipher)
            )
            kwargs["params"] = params
        return kwargs

    def _open_response(self, payload: Any, cipher: WorkspaceCipher | None) -> Any:
        if cipher is None or not isinstance(payload, dict):
            return payload
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
                        if key not in _TOLERATE_UNREADABLE:
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
                        opened = opened["b"]
                    item[field] = opened
        return payload


class Client(_Base):
    """Synchronous client."""

    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None,
                 timeout: float = 40.0, key: str | None = None) -> None:
        super().__init__(config, agent_id=agent_id, key=key)
        self._http = httpx.Client(
            base_url=self.config.url, headers=_headers(self.config.token), timeout=timeout
        )

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, *, cipher: Any = _UNSET, **kwargs: Any
              ) -> dict[str, Any]:
        if cipher is _UNSET:
            cipher = self.cipher
        kwargs = self._seal_request(path, kwargs, cipher)
        response = self._http.request(method, path, **kwargs)
        _raise_for(response)
        return self._open_response(response.json(), cipher)

    # --- meta ---
    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health")

    def stats(self, workspace: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/stats", params={"workspace": self._ws(workspace)})

    # --- presence ---
    def register(self, *, name: str, kind: str = "unknown", branch: str | None = None,
                 task: str | None = None, channels: Sequence[str] = (),
                 meta: dict[str, Any] | None = None, ttl: float | None = None,
                 workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/agents/register", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "name": name,
            "kind": kind, "branch": branch, "task": task, "channels": list(channels),
            "meta": meta or {}, "pubkey": self.public_key, "ttl": ttl,
        })["agent"]

    def heartbeat(self, *, task: str | None = None, ttl: float | None = None,
                  renew_leases: bool = True, workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/agents/heartbeat", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "task": task,
            "ttl": ttl, "renew_leases": renew_leases,
        })

    def agents(self, workspace: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", "/agents", params={"workspace": self._ws(workspace)})["agents"]

    def deregister(self, workspace: str | None = None) -> bool:
        return self._call("DELETE", f"/agents/{self.agent_id}",
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
        return self._call("GET", "/inbox", cipher=cipher, params=params)["messages"]

    def history(self, channel: str, *, limit: int = 50,
                workspace: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", f"/channels/{self._blind_channel(channel)}",
                          params={"workspace": self._ws(workspace),
                                  "limit": limit})["messages"]

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
        params: dict[str, Any] = {"workspace": self._ws(workspace)}
        if prefix:
            params["prefix"] = prefix
        return self._call("GET", "/board", params=params)["entries"]

    def board_delete(self, key: str, *, workspace: str | None = None) -> bool:
        return self._call("DELETE", f"/board/{self._blind(key, 'board')}",
                          params={"workspace": self._ws(workspace)})["deleted"]


class AsyncClient(_Base):
    """Asynchronous client — same surface as :class:`Client`."""

    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None,
                 timeout: float = 40.0, key: str | None = None) -> None:
        super().__init__(config, agent_id=agent_id, key=key)
        self._http = httpx.AsyncClient(
            base_url=self.config.url, headers=_headers(self.config.token), timeout=timeout
        )

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, *, cipher: Any = _UNSET, **kwargs: Any
                    ) -> dict[str, Any]:
        if cipher is _UNSET:
            cipher = self.cipher
        kwargs = self._seal_request(path, kwargs, cipher)
        response = await self._http.request(method, path, **kwargs)
        _raise_for(response)
        return self._open_response(response.json(), cipher)

    async def health(self) -> dict[str, Any]:
        return await self._call("GET", "/health")

    async def stats(self, workspace: str | None = None) -> dict[str, Any]:
        return await self._call("GET", "/stats", params={"workspace": self._ws(workspace)})

    async def register(self, *, name: str, kind: str = "unknown", branch: str | None = None,
                       task: str | None = None, channels: Sequence[str] = (),
                       meta: dict[str, Any] | None = None, ttl: float | None = None,
                       workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("POST", "/agents/register", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "name": name,
            "kind": kind, "branch": branch, "task": task, "channels": list(channels),
            "meta": meta or {}, "ttl": ttl,
        })
        return result["agent"]

    async def heartbeat(self, *, task: str | None = None, ttl: float | None = None,
                        renew_leases: bool = True,
                        workspace: str | None = None) -> dict[str, Any]:
        return await self._call("POST", "/agents/heartbeat", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "task": task,
            "ttl": ttl, "renew_leases": renew_leases,
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
        return result["messages"]

    async def history(self, channel: str, *, limit: int = 50,
                      workspace: str | None = None) -> list[dict[str, Any]]:
        result = await self._call("GET", f"/channels/{self._blind_channel(channel)}",
                                  params={"workspace": self._ws(workspace), "limit": limit})
        return result["messages"]

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
        params: dict[str, Any] = {"workspace": self._ws(workspace)}
        if prefix:
            params["prefix"] = prefix
        result = await self._call("GET", "/board", params=params)
        return result["entries"]

    async def board_delete(self, key: str, *, workspace: str | None = None) -> bool:
        result = await self._call("DELETE", f"/board/{self._blind(key, 'board')}",
                                  params={"workspace": self._ws(workspace)})
        return result["deleted"]
