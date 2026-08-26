"""FastAPI application exposing the Switchboard hub over HTTP.

Run it with::

    switchboard serve --db ./switchboard.db --token "$SWITCHBOARD_TOKEN"

Auth is one optional shared bearer token, and it is a perimeter rather than
authorization: every admitted caller can reach every room whose identifier it
knows. Do not expose a hub publicly without one set.

What protects a room is not the hub. A room identifier is
``hash(workspace_token)`` — unguessable unless someone tells you — and its
contents are sealed with a key the hub never receives. See ``auth.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from .auth import Perimeter
from .config import (
    DEFAULT_AGENT_TTL,
    DEFAULT_BOARD_TTL,
    DEFAULT_LEASE_TTL,
    DEFAULT_MESSAGE_TTL,
    MAX_AGENT_TTL,
    MAX_AWAY_SECONDS,
    MAX_BOARD_TTL,
    MAX_LEASE_TTL,
    MAX_MESSAGE_TTL,
    MAX_WAIT_SECONDS,
    POLL_INTERVAL_SECONDS,
    ServerConfig,
    clamp_ttl,
)
from .load import CLASS_ADMIT, CLASS_READ, CLASS_WRITE, Admission, LoadMeter, Rejected
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

#: Reported in `GET /health` and the OpenAPI schema. Reuses the package's own
#: __version__ rather than a second hardcoded copy — this project has hit that
#: exact class of drift bug (two independently-maintained copies of one value)
#: more than once already.
API_VERSION = __version__


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# --- wire models ------------------------------------------------------------


class RegisterIn(BaseModel):
    workspace: str = "default"
    agent_id: str | None = None
    name: str
    #: The agent's signing public key, sealed by the client like any other
    #: content. Opaque here: the hub stores and echoes it, and cannot verify
    #: anything with it — verification is between peers, who hold the key.
    pubkey: str | None = None
    #: The agent's X25519 exchange key, sealed the same way `pubkey` is. Also
    #: opaque here: the hub stores and echoes it and does nothing with it —
    #: it exists so a peer can seal a `whisper` to this agent alone, which is a
    #: property between the two peers and never involves the hub.
    exchange_key: str | None = None
    kind: str = "unknown"
    branch: str | None = None
    task: str | None = None
    channels: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    ttl: float | None = Field(default=None, gt=0)
    #: Seconds from now until this agent expects to be back, or None if it is
    #: making no promise. Presence still lapses on `ttl`; this only keeps the
    #: row listed afterwards, as `away`, so a peer arriving into an otherwise
    #: empty roster can tell "nobody is coming" from "someone is mid-turn".
    back_in: float | None = Field(default=None, gt=0)


class HeartbeatIn(BaseModel):
    workspace: str = "default"
    agent_id: str
    task: str | None = None
    ttl: float | None = Field(default=None, gt=0)
    renew_leases: bool = True
    lease_ttl: float | None = Field(default=None, gt=0)
    #: Seconds from now until this agent expects to be back, or None if it is
    #: making no promise. Presence still lapses on `ttl`; this only keeps the
    #: row listed afterwards, as `away`, so a peer arriving into an otherwise
    #: empty roster can tell "nobody is coming" from "someone is mid-turn".
    back_in: float | None = Field(default=None, gt=0)


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
        "pubkey": a.pubkey,
        "exchange_key": a.exchange_key,
        "registered_at": iso(a.registered_at),
        "last_seen_at": iso(a.last_seen_at),
        "present_until": iso(a.present_until),
        "expected_back": iso(a.expected_back),
        "expires_at": iso(a.expires_at),
        "expires_in": round(a.expires_at - now, 1),
        "stale": a.last_seen_at < now - 60,
        # The third value presence never had. `away` is not a weaker `stale`:
        # stale is a guess from how long ago someone was last seen, while this
        # is the agent's own statement that it is between turns and returning.
        # A row can only be away because it said so — otherwise it is gone.
        "away": now > a.present_until,
        "back_in": (
            round(a.expected_back - now, 1) if a.expected_back is not None else None
        ),
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


def _expected_back(back_in: float | None, now: float) -> float | None:
    """Absolute return time from a relative promise, bounded.

    Clamped rather than rejected: an agent guessing badly about its own next
    turn should not have its registration fail, and a promise further out than
    MAX_AWAY_SECONDS is a plan rather than a rendezvous.
    """
    if back_in is None:
        return None
    return now + min(back_in, MAX_AWAY_SECONDS)



# --- app --------------------------------------------------------------------


def _work_class(request: Request) -> str:
    """Which reservation this request draws on.

    Coarse on purpose. What matters is that a flood of one kind cannot starve
    another, and the kind worth protecting is a room the hub has not seen —
    otherwise flooding messages blocks new rooms, which is the cheap attack.
    """
    path = request.url.path
    if path.startswith("/agents/register"):
        return CLASS_ADMIT
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        return CLASS_WRITE
    return CLASS_READ


def create_app(
    config: ServerConfig | None = None,
    store: Store | None = None,
    perimeter: Perimeter | None = None,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    """Build the hub application.

    ``clock`` is the one source of "now" for every request this app serves,
    defaulting to the wall clock. It exists because the whole model is TTLs,
    and a test that wants to see a lease expire otherwise has to really wait
    fifteen minutes. Injected per app rather than patched into the module, so
    two hubs in one process keep two independent clocks — see
    ``switchboard.testing``.

    Every store call that takes a ``now`` is given one. The store would fall
    back to ``time.time()`` on its own, and that fallback is exactly the leak
    that makes a shifted clock inconsistent: presence expiring on hub time
    while a sweep runs on wall time is worse than no control at all. (The
    three that take none — deregister, release, board delete — are
    unconditional deletes with no expiry to compare against.)
    """
    config = config or ServerConfig.from_env()
    now_fn: Callable[[], float] = clock or time.time
    store = store or Store(config.db_path)
    notifier = Notifier()
    meter = LoadMeter()
    admission = Admission(meter, target_ms=config.load_target_ms)
    perimeter = perimeter or Perimeter(config.token)

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

    async def require_admission(
        authorization: str | None = Header(default=None)
    ) -> None:
        """The whole of the hub's access check.

        There is no per-workspace authorization left to do. A room identifier
        is `hash(workspace_token)` — derived, not owned — so there is nobody to
        check ownership against, and the two things that actually protect a
        room are knowing its identifier and holding its key. Neither is the
        hub's to enforce.

        What is left is a perimeter: with a token configured, present it; with
        none, the hub is open. Every caller who gets through can reach every
        room they can name, which is why the docstring in auth.py insists this
        is not authorization.
        """
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[len("Bearer "):]
        if not perimeter.admits(token):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(config.sweep_interval)
            with contextlib.suppress(Exception):
                await run_in_threadpool(store.sweep, now=now_fn())

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

    if config.cors_origins:
        # Off unless asked for. A browser refuses a cross-origin read before
        # it is sent, whatever credentials the reader holds, so a page served
        # from anywhere but the hub needs this — and a hub that serves only
        # agents should not carry it.
        #
        # `allow_credentials` stays False deliberately: this API is
        # authenticated by an `Authorization` header the page sets itself, not
        # by a cookie a browser would attach on its own. Leaving credentials
        # off is what keeps `*` from meaning "any page can act as whoever is
        # logged in" — there is nothing to be logged in as.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            max_age=600,
        )

    @app.middleware("http")
    async def measure(request: Request, call_next):
        """Hold a slot for the duration of the request.

        A long-poll gives its slot back while parked, so the number this
        produces is work in progress rather than connections held — see
        load.py for why that distinction decides whether the measurement is
        usable at all.
        """
        work_class = _work_class(request)
        with meter.serving():
            try:
                with admission.admit(work_class):
                    return await call_next(request)
            except Rejected as shed:
                # Shedding is a scheduling decision, not an error: say which
                # class and when to return, so a caller backs off rather than
                # retrying into the same wall.
                return JSONResponse(
                    status_code=429,
                    content={"error": "busy", "work_class": shed.work_class,
                             "retry_after": shed.retry_after},
                    headers={"Retry-After": str(int(shed.retry_after) or 1)},
                )

    app.state.store = store
    app.state.config = config
    app.state.notifier = notifier
    guard = [Depends(require_admission)]

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
                "expires_in": round(exc.expires_at - now_fn(), 1),
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

    # --- meta --------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        # Whether a caller has to get through the door at all. Not a claim
        # about rooms: a hub reporting `auth: true` still lets every admitted
        # caller reach every room they can name.
        auth_required = not perimeter.open
        # `self_issued_keys` used to appear here, reporting whether a client
        # could bind its own token to a workspace. Nothing binds anything now:
        # a room identifier is hash(workspace_token), so it is derived rather
        # than claimed and there is no registry to consult. Kept absent rather
        # than reported false, so a client written against the old field reads
        # "unknown" instead of a confident wrong answer.
        return {
            "ok": True,
            "version": API_VERSION,
            "auth": auth_required,
        }

    @app.get("/stats", dependencies=guard)
    def stats(workspace: str | None = None) -> dict[str, Any]:
        payload = store.stats(workspace=workspace, now=now_fn())
        # Held long-poll connections, not row counts, are what a hub actually
        # runs out of — see docs/managed-hub.md. Reporting it is what lets an
        # operator autoscale on the real constraint instead of a proxy for it.
        payload["waiting_readers"] = notifier.waiting
        # The numbers a load target would be chosen from. Reported rather than
        # acted on: #72 asks for the target to come from measurement, and
        # nobody has measured yet.
        load = meter.snapshot()
        payload["load"] = {
            "active": load.active,
            "parked": load.parked,
            "peak_active": load.peak_active,
            "delay_p50_ms": load.delay_p50_ms,
            "delay_p95_ms": load.delay_p95_ms,
            "samples": load.samples,
            "admission": admission.snapshot(),
        }
        return payload

    # --- presence ----------------------------------------------------------

    @app.post("/agents/register", dependencies=guard)
    def register(payload: RegisterIn) -> dict[str, Any]:
        now = now_fn()
        agent = store.register_agent(
            workspace=payload.workspace,
            agent_id=payload.agent_id,
            name=payload.name,
            kind=payload.kind,
            branch=payload.branch,
            task=payload.task,
            channels=payload.channels,
            meta=payload.meta,
            pubkey=payload.pubkey,
            exchange_key=payload.exchange_key,
            ttl=clamp_ttl(payload.ttl, DEFAULT_AGENT_TTL, MAX_AGENT_TTL),
            expected_back=_expected_back(payload.back_in, now),
            now=now,
        )
        return {"agent": dump_agent(agent, now)}

    @app.post("/agents/heartbeat", dependencies=guard)
    def heartbeat(payload: HeartbeatIn) -> dict[str, Any]:
        now = now_fn()
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
            expected_back=_expected_back(payload.back_in, now),
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
        now = now_fn()
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
        now = now_fn()
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
        now = now_fn()
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
        now = now_fn()
        found = store.list_leases(workspace=workspace, holder=holder, now=now)
        return {"leases": [dump_lease(le, now) for le in found], "count": len(found)}

    @app.get("/leases/{resource:path}", dependencies=guard)
    def get_lease(resource: str, workspace: str = "default") -> dict[str, Any]:
        now = now_fn()
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
            now=now_fn(),
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
            agent = store.get_agent(workspace=workspace, agent_id=agent_id, now=now_fn())
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
                now=now_fn(),
            )

        if wait <= 0:
            messages = await run_in_threadpool(drain)
        else:
            if agent_id:
                # Waiting is not absence. Presence lapses after
                # DEFAULT_AGENT_TTL, and a long poll is the one call an agent
                # makes while deliberately doing nothing else — so an agent
                # that sat in a wait loop for a peer dropped off the roster
                # *while waiting for them*. The protocol then tells the peer,
                # correctly, not to wait on somebody who is not listed. Both
                # sides follow the convention into mutual invisibility, and
                # this is the only place that can tell the difference.
                #
                # Once per request, not per loop turn: the loop already runs
                # inside a bounded MAX_WAIT_SECONDS, and repeating it would
                # trade a real fix for load. Leases are left alone — renewing
                # what you hold is a claim to be working, and waiting is not.
                await run_in_threadpool(
                    lambda: store.heartbeat(
                        workspace=workspace, agent_id=agent_id,
                        ttl=DEFAULT_AGENT_TTL, renew_leases=False, now=now_fn(),
                    )
                )
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
                        #
                        # Parked, so an idle agent waiting here does not read
                        # as load. This is the normal state of a quiet
                        # workspace, and counting it would drown everything.
                        with meter.parked():
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
        found = store.list_channels(workspace=workspace, now=now_fn())
        return {"channels": found, "count": len(found)}

    @app.get("/channels/{channel:path}", dependencies=guard)
    def channel_history(channel: str, workspace: str = "default",
                        limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        msgs = store.peek(workspace=workspace, channel=channel, limit=limit, now=now_fn())
        return {"messages": [dump_message(m) for m in msgs], "count": len(msgs)}

    # --- blackboard --------------------------------------------------------

    @app.put("/board", dependencies=guard)
    def board_set(payload: BoardIn) -> dict[str, Any]:
        now = now_fn()
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
        now = now_fn()
        entries = store.board_list(workspace=workspace, prefix=prefix, now=now)
        return {"entries": [dump_board(e, now) for e in entries], "count": len(entries)}

    @app.get("/board/{key:path}", dependencies=guard)
    def board_get(key: str, workspace: str = "default") -> dict[str, Any]:
        now = now_fn()
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
        return {"swept": store.sweep(now=now_fn())}

    return app


app = None  # populated lazily by `switchboard serve`


def get_app() -> FastAPI:
    """Factory for `uvicorn switchboard.server:get_app --factory`."""
    return create_app()
