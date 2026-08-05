"""FastAPI application exposing the Switchboard hub over HTTP.

Run it with::

    switchboard serve --db ./switchboard.db --token "$SWITCHBOARD_TOKEN"

By default auth is a single shared bearer token and every caller may use every
workspace. For a hub coordinating agents that already share a codebase that is
the right shape: per-agent identity is for *telling them apart*, not for
keeping them apart. Do not expose such a hub publicly without a token set.

For a hub shared between parties that do not trust each other, pass a
different :class:`~switchboard.auth.PrincipalResolver` to :func:`create_app`.
Workspaces then become a boundary rather than a namespace, enforced once in
``require_principal`` for every guarded route. See ``auth.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Sequence

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .auth import (
    Principal,
    PrincipalResolver,
    SelfIssuedKeyResolver,
    SharedTokenResolver,
    hash_token,
)
from .config import (
    DEFAULT_AGENT_TTL,
    DEFAULT_BOARD_TTL,
    DEFAULT_LEASE_TTL,
    DEFAULT_MESSAGE_TTL,
    MAX_AGENT_TTL,
    MAX_BOARD_TTL,
    MAX_LEASE_TTL,
    MAX_MESSAGE_TTL,
    MAX_WAIT_SECONDS,
    POLL_INTERVAL_SECONDS,
    ServerConfig,
    clamp_ttl,
)
from .notify import Notifier
from .store import (
    Agent,
    BoardEntry,
    Lease,
    LeaseConflict,
    Message,
    NotLeaseHolder,
    Store,
    StoreError,
)

API_VERSION = "0.1.0"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# --- wire models ------------------------------------------------------------


class RegisterIn(BaseModel):
    workspace: str = "default"
    agent_id: str | None = None
    name: str
    kind: str = "unknown"
    branch: str | None = None
    task: str | None = None
    channels: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    ttl: float | None = Field(default=None, gt=0)


class HeartbeatIn(BaseModel):
    workspace: str = "default"
    agent_id: str
    task: str | None = None
    ttl: float | None = Field(default=None, gt=0)
    renew_leases: bool = True
    lease_ttl: float | None = Field(default=None, gt=0)


class LeaseIn(BaseModel):
    workspace: str = "default"
    resource: str
    agent_id: str
    note: str | None = None
    ttl: float | None = Field(default=None, gt=0)
    steal_expired: bool = True


class ReleaseIn(BaseModel):
    workspace: str = "default"
    resource: str
    agent_id: str
    force: bool = False


class PostIn(BaseModel):
    workspace: str = "default"
    channel: str
    agent_id: str
    body: Any = None
    type: str = "note"
    thread: str | None = None
    ttl: float | None = Field(default=None, gt=0)


class BoardIn(BaseModel):
    workspace: str = "default"
    key: str
    value: Any = None
    agent_id: str
    ttl: float | None = Field(default=None, gt=0)
    if_revision: int | None = None


class KeyRegisterIn(BaseModel):
    workspace: str


# --- serialization ----------------------------------------------------------


def dump_agent(a: Agent, now: float) -> dict[str, Any]:
    return {
        "workspace": a.workspace,
        "agent_id": a.id,
        "name": a.name,
        "kind": a.kind,
        "branch": a.branch,
        "task": a.task,
        "channels": a.channels,
        "meta": a.meta,
        "registered_at": iso(a.registered_at),
        "last_seen_at": iso(a.last_seen_at),
        "expires_at": iso(a.expires_at),
        "expires_in": round(a.expires_at - now, 1),
        "stale": a.last_seen_at < now - 60,
    }


def dump_lease(lease: Lease, now: float) -> dict[str, Any]:
    return {
        "workspace": lease.workspace,
        "resource": lease.resource,
        "holder": lease.holder,
        "note": lease.note,
        "fence": lease.fence,
        "acquired_at": iso(lease.acquired_at),
        "renewed_at": iso(lease.renewed_at),
        "expires_at": iso(lease.expires_at),
        "expires_in": round(lease.expires_at - now, 1),
    }


def dump_message(m: Message) -> dict[str, Any]:
    return {
        "seq": m.seq,
        "id": m.id,
        "workspace": m.workspace,
        "channel": m.channel,
        "from": m.sender,
        "type": m.type,
        "body": m.body,
        "thread": m.thread,
        "created_at": iso(m.created_at),
        "expires_at": iso(m.expires_at),
    }


def dump_board(e: BoardEntry, now: float) -> dict[str, Any]:
    return {
        "workspace": e.workspace,
        "key": e.key,
        "value": e.value,
        "revision": e.revision,
        "updated_by": e.updated_by,
        "updated_at": iso(e.updated_at),
        "expires_at": iso(e.expires_at),
        "expires_in": round(e.expires_at - now, 1),
    }


# --- app --------------------------------------------------------------------


def create_app(
    config: ServerConfig | None = None,
    store: Store | None = None,
    resolver: PrincipalResolver | None = None,
) -> FastAPI:
    config = config or ServerConfig.from_env()
    store = store or Store(config.db_path)
    notifier = Notifier()
    resolver = resolver or SharedTokenResolver(config.token)

    async def _requested_workspace(request: Request) -> str | None:
        """The workspace this request is about, from wherever it carries it.

        Reading the body here is safe: Starlette caches it on the Request, so
        FastAPI's own parsing of the same request still sees it.
        """
        if "workspace" in request.query_params:
            return request.query_params["workspace"]
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if body:
                try:
                    payload = json.loads(body)
                except ValueError:
                    return None
                if isinstance(payload, dict) and isinstance(payload.get("workspace"), str):
                    return payload["workspace"]
        return None

    async def require_principal(
        request: Request, authorization: str | None = Header(default=None)
    ) -> Principal:
        """Authenticate, then authorize the workspace — for every guarded route.

        Doing the workspace check inside the shared dependency rather than in
        each handler is deliberate. There are ~19 endpoints that take a
        workspace; a per-handler check is one forgotten line away from a
        cross-tenant read that nothing would ever surface. Here it cannot be
        forgotten, and `test_auth.py` walks the whole route table to prove it.
        """
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[len("Bearer "):]
        principal = resolver.resolve(token)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

        if principal.unrestricted:
            return principal

        workspace = await _requested_workspace(request)
        if workspace is None:
            # A scoped key may not ask questions that span every tenant, which
            # is what an absent workspace means (e.g. GET /stats).
            raise HTTPException(
                status_code=403,
                detail="this key is scoped to specific workspaces; name one explicitly",
            )
        if not principal.may_access(workspace):
            raise HTTPException(
                status_code=403,
                detail=f"key {principal.key_id!r} has no access to workspace {workspace!r}",
            )
        return principal

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(config.sweep_interval)
            with contextlib.suppress(Exception):
                await run_in_threadpool(store.sweep)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            store.close()

    app = FastAPI(
        title="Switchboard",
        version=API_VERSION,
        description="An ephemeral orchestration hub for AI coding agents.",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.config = config
    app.state.notifier = notifier
    guard = [Depends(require_principal)]

    @app.exception_handler(LeaseConflict)
    async def _lease_conflict(_, exc: LeaseConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": "lease_conflict",
                "detail": str(exc),
                "resource": exc.resource,
                "holder": exc.holder,
                "expires_at": iso(exc.expires_at),
                "expires_in": round(exc.expires_at - time.time(), 1),
            },
        )

    @app.exception_handler(NotLeaseHolder)
    async def _not_holder(_, exc: NotLeaseHolder) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"error": "not_lease_holder", "detail": str(exc)}
        )

    @app.exception_handler(StoreError)
    async def _store_error(_, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})

    # --- self-issued keys ---------------------------------------------------

    # Only mounted when the hub actually runs this resolver — a hub using
    # SharedTokenResolver or StaticKeyResolver has no concept of a client
    # registering its own key, and the route should not exist there, not
    # just reject calls to it. See auth.SelfIssuedKeyResolver.
    if isinstance(resolver, SelfIssuedKeyResolver):

        @app.post("/keys/register")
        def register_key(
            payload: KeyRegisterIn, authorization: str | None = Header(default=None)
        ) -> dict[str, Any]:
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="missing bearer token")
            token = authorization[len("Bearer "):]
            if len(token) < 16:
                raise HTTPException(
                    status_code=400,
                    detail="token too short — generate one with "
                           "python -c 'import secrets;print(secrets.token_urlsafe(32))'",
                )
            workspace = store.bind_key(token_hash=hash_token(token), workspace=payload.workspace)
            return {"workspace": workspace}

    # --- meta --------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        # Whether a caller needs to authenticate at all — true unless the
        # resolver is explicitly open (SharedTokenResolver with no token).
        # `bool(config.token)` alone was wrong for every other resolver: a
        # StaticKeyResolver or SelfIssuedKeyResolver hub always requires
        # auth, but neither one sets config.token, so it reported False.
        auth_required = not getattr(resolver, "open", False)
        return {"ok": True, "version": API_VERSION, "auth": auth_required}

    @app.get("/stats", dependencies=guard)
    def stats(workspace: str | None = None) -> dict[str, Any]:
        payload = store.stats(workspace=workspace)
        # Held long-poll connections, not row counts, are what a hub actually
        # runs out of — see docs/managed-hub.md. Reporting it is what lets an
        # operator autoscale on the real constraint instead of a proxy for it.
        payload["waiting_readers"] = notifier.waiting
        return payload

    # --- presence ----------------------------------------------------------

    @app.post("/agents/register", dependencies=guard)
    def register(payload: RegisterIn) -> dict[str, Any]:
        now = time.time()
        agent = store.register_agent(
            workspace=payload.workspace,
            agent_id=payload.agent_id,
            name=payload.name,
            kind=payload.kind,
            branch=payload.branch,
            task=payload.task,
            channels=payload.channels,
            meta=payload.meta,
            ttl=clamp_ttl(payload.ttl, DEFAULT_AGENT_TTL, MAX_AGENT_TTL),
            now=now,
        )
        return {"agent": dump_agent(agent, now)}

    @app.post("/agents/heartbeat", dependencies=guard)
    def heartbeat(payload: HeartbeatIn) -> dict[str, Any]:
        now = time.time()
        agent, leases = store.heartbeat(
            workspace=payload.workspace,
            agent_id=payload.agent_id,
            ttl=clamp_ttl(payload.ttl, DEFAULT_AGENT_TTL, MAX_AGENT_TTL),
            task=payload.task,
            renew_leases=payload.renew_leases,
            lease_ttl=(
                clamp_ttl(payload.lease_ttl, DEFAULT_LEASE_TTL, MAX_LEASE_TTL)
                if payload.lease_ttl is not None
                else None
            ),
            now=now,
        )
        if agent is None:
            raise HTTPException(
                status_code=404,
                detail="unknown or expired agent; call /agents/register again",
            )
        # An agent's own DM channel is always `@<its id>`, unblinded even when
        # encrypted — see WorkspaceCipher.blind_channel — so this needs no
        # help from the client to compute correctly either way.
        unread_dms = store.count_unread(
            workspace=payload.workspace, channel=f"@{payload.agent_id}",
            agent_id=payload.agent_id, now=now,
        )
        return {
            "agent": dump_agent(agent, now),
            "leases": [dump_lease(le, now) for le in leases],
            "unread_dms": unread_dms,
        }

    @app.get("/agents", dependencies=guard)
    def list_agents(workspace: str = "default") -> dict[str, Any]:
        now = time.time()
        agents = store.list_agents(workspace=workspace, now=now)
        return {"agents": [dump_agent(a, now) for a in agents], "count": len(agents)}

    @app.delete("/agents/{agent_id}", dependencies=guard)
    def deregister(agent_id: str, workspace: str = "default",
                   release_leases: bool = True) -> dict[str, Any]:
        removed = store.deregister_agent(
            workspace=workspace, agent_id=agent_id, release_leases=release_leases
        )
        return {"removed": removed}

    # --- leases ------------------------------------------------------------

    @app.post("/leases/acquire", dependencies=guard)
    def acquire(payload: LeaseIn) -> dict[str, Any]:
        now = time.time()
        lease = store.acquire_lease(
            workspace=payload.workspace,
            resource=payload.resource,
            holder=payload.agent_id,
            ttl=clamp_ttl(payload.ttl, DEFAULT_LEASE_TTL, MAX_LEASE_TTL),
            note=payload.note,
            steal_expired=payload.steal_expired,
            now=now,
        )
        return {"lease": dump_lease(lease, now), "acquired": True}

    @app.post("/leases/renew", dependencies=guard)
    def renew(payload: LeaseIn) -> dict[str, Any]:
        now = time.time()
        lease = store.renew_lease(
            workspace=payload.workspace,
            resource=payload.resource,
            holder=payload.agent_id,
            ttl=clamp_ttl(payload.ttl, DEFAULT_LEASE_TTL, MAX_LEASE_TTL),
            now=now,
        )
        return {"lease": dump_lease(lease, now)}

    @app.post("/leases/release", dependencies=guard)
    def release(payload: ReleaseIn) -> dict[str, Any]:
        released = store.release_lease(
            workspace=payload.workspace,
            resource=payload.resource,
            holder=payload.agent_id,
            force=payload.force,
        )
        return {"released": released}

    @app.get("/leases", dependencies=guard)
    def leases(workspace: str = "default", holder: str | None = None) -> dict[str, Any]:
        now = time.time()
        found = store.list_leases(workspace=workspace, holder=holder, now=now)
        return {"leases": [dump_lease(le, now) for le in found], "count": len(found)}

    @app.get("/leases/{resource:path}", dependencies=guard)
    def get_lease(resource: str, workspace: str = "default") -> dict[str, Any]:
        now = time.time()
        lease = store.get_lease(workspace=workspace, resource=resource, now=now)
        return {"lease": dump_lease(lease, now) if lease else None, "held": lease is not None}

    # --- messages ----------------------------------------------------------

    # async, not sync: the write goes to a worker thread, but waking the
    # waiters touches asyncio futures and must happen on the loop thread.
    @app.post("/messages", dependencies=guard)
    async def post_message(payload: PostIn) -> dict[str, Any]:
        msg = await run_in_threadpool(
            store.post,
            workspace=payload.workspace,
            channel=payload.channel,
            sender=payload.agent_id,
            body=payload.body,
            type=payload.type,
            thread=payload.thread,
            ttl=clamp_ttl(payload.ttl, DEFAULT_MESSAGE_TTL, MAX_MESSAGE_TTL),
        )
        notifier.notify(payload.workspace, payload.channel)
        return {"message": dump_message(msg)}

    def _resolve_channels(workspace: str, agent_id: str | None,
                          channels: Sequence[str] | None) -> list[str]:
        """An agent's inbox is its own @-channel plus its registered channels."""
        if channels:
            return list(dict.fromkeys(channels))
        resolved: list[str] = []
        if agent_id:
            resolved.append(f"@{agent_id}")
            agent = store.get_agent(workspace=workspace, agent_id=agent_id)
            if agent:
                resolved.extend(agent.channels)
        return list(dict.fromkeys(resolved))

    @app.get("/inbox", dependencies=guard)
    async def inbox(
        workspace: str = "default",
        agent_id: str | None = None,
        channel: list[str] | None = Query(default=None),
        since: int | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        include_own: bool = False,
        peek: bool = False,
        wait: float = Query(default=0.0, ge=0.0),
    ) -> dict[str, Any]:
        """Drain new messages for an agent, optionally long-polling for them.

        With ``wait`` > 0 this holds the connection open until a message shows
        up or the deadline passes — that is what lets an idle agent block on
        the hub instead of hammering it.
        """
        channels = _resolve_channels(workspace, agent_id, channel)
        if not channels:
            raise HTTPException(
                status_code=400, detail="provide agent_id and/or at least one channel"
            )

        def drain() -> list[Message]:
            return store.read(
                workspace=workspace,
                channels=channels,
                agent_id=agent_id,
                since=since,
                limit=limit,
                include_own=include_own,
                commit_cursor=not peek,
            )

        if wait <= 0:
            messages = await run_in_threadpool(drain)
        else:
            # Register interest BEFORE the first read. A write landing between
            # the read and the sleep resolves this future, so the sleep returns
            # at once and we re-drain — rather than sleeping through a message
            # that arrived microseconds too early.
            waiter = notifier.register(workspace, channels)
            try:
                messages = await run_in_threadpool(drain)
                deadline = time.monotonic() + min(wait, MAX_WAIT_SECONDS)
                while not messages:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        # Woken by a writer, or by the slow floor. The floor is
                        # what makes this correct across multiple workers,
                        # where an in-process notifier sees nothing.
                        await asyncio.wait_for(
                            asyncio.shield(waiter),
                            timeout=min(remaining, POLL_INTERVAL_SECONDS),
                        )
                    except asyncio.TimeoutError:
                        pass
                    messages = await run_in_threadpool(drain)
                    if not messages and waiter.done():
                        # Consumed this wakeup without finding anything of
                        # ours (another channel, or our own message filtered
                        # out). Re-arm so the next write can still wake us.
                        notifier.unregister(workspace, channels, waiter)
                        waiter = notifier.register(workspace, channels)
            finally:
                notifier.unregister(workspace, channels, waiter)
        return {
            "messages": [dump_message(m) for m in messages],
            "count": len(messages),
            "channels": channels,
        }

    @app.get("/channels", dependencies=guard)
    def channels(workspace: str = "default") -> dict[str, Any]:
        found = store.list_channels(workspace=workspace)
        return {"channels": found, "count": len(found)}

    @app.get("/channels/{channel:path}", dependencies=guard)
    def channel_history(channel: str, workspace: str = "default",
                        limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        msgs = store.peek(workspace=workspace, channel=channel, limit=limit)
        return {"messages": [dump_message(m) for m in msgs], "count": len(msgs)}

    # --- blackboard --------------------------------------------------------

    @app.put("/board", dependencies=guard)
    def board_set(payload: BoardIn) -> dict[str, Any]:
        now = time.time()
        entry = store.board_set(
            workspace=payload.workspace,
            key=payload.key,
            value=payload.value,
            updated_by=payload.agent_id,
            ttl=clamp_ttl(payload.ttl, DEFAULT_BOARD_TTL, MAX_BOARD_TTL),
            if_revision=payload.if_revision,
            now=now,
        )
        return {"entry": dump_board(entry, now)}

    @app.get("/board", dependencies=guard)
    def board_list(workspace: str = "default", prefix: str | None = None) -> dict[str, Any]:
        now = time.time()
        entries = store.board_list(workspace=workspace, prefix=prefix, now=now)
        return {"entries": [dump_board(e, now) for e in entries], "count": len(entries)}

    @app.get("/board/{key:path}", dependencies=guard)
    def board_get(key: str, workspace: str = "default") -> dict[str, Any]:
        now = time.time()
        entry = store.board_get(workspace=workspace, key=key, now=now)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no live board entry at {key!r}")
        return {"entry": dump_board(entry, now)}

    @app.delete("/board/{key:path}", dependencies=guard)
    def board_delete(key: str, workspace: str = "default") -> dict[str, Any]:
        return {"deleted": store.board_delete(workspace=workspace, key=key)}

    # --- maintenance -------------------------------------------------------

    @app.post("/sweep", dependencies=guard)
    def sweep() -> dict[str, Any]:
        return {"swept": store.sweep()}

    return app


app = None  # populated lazily by `switchboard serve`


def get_app() -> FastAPI:
    """Factory for `uvicorn switchboard.server:get_app --factory`."""
    return create_app()
