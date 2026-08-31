"""SQLite-backed storage for Switchboard.

Design notes
------------
*Everything expires.* Every row carries an ``expires_at`` (unix seconds).
Reads always filter on it, so correctness never depends on the sweeper having
run — the sweeper only reclaims disk.

*Writes are serialized by SQLite.* Operations that read-then-write (lease
acquisition, most notably) run inside ``BEGIN IMMEDIATE``, which takes the
write lock up front. That makes "is this resource free? then take it" a single
atomic step, which is the whole point of a lease.

*Time is unix seconds as REAL.* No timezone ambiguity in the store; the API
layer renders ISO-8601 on the way out.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from .config import DEFAULT_CURSOR_TTL

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agents (
    workspace     TEXT NOT NULL,
    id            TEXT NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'unknown',
    branch        TEXT,
    task          TEXT,
    channels      TEXT NOT NULL DEFAULT '[]',
    meta          TEXT NOT NULL DEFAULT '{}',
    pubkey        TEXT,
    exchange_key  TEXT,
    registered_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    -- When presence lapses: "is this agent at the keyboard right now".
    present_until REAL NOT NULL DEFAULT 0,
    -- When the agent said it expects to be back, or NULL if it said nothing.
    -- Only this keeps a row listed past `present_until`, which is what lets
    -- absence carry information instead of being a blank.
    expected_back REAL,
    -- When the row goes away entirely: max(present_until, expected_back).
    expires_at    REAL NOT NULL,
    PRIMARY KEY (workspace, id)
);
CREATE INDEX IF NOT EXISTS idx_agents_expiry ON agents(expires_at);

CREATE TABLE IF NOT EXISTS leases (
    workspace   TEXT NOT NULL,
    resource    TEXT NOT NULL,
    holder      TEXT NOT NULL,
    note        TEXT,
    fence       INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    renewed_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    PRIMARY KEY (workspace, resource)
);
CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases(expires_at);
CREATE INDEX IF NOT EXISTS idx_leases_holder ON leases(workspace, holder);

CREATE TABLE IF NOT EXISTS messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    workspace  TEXT NOT NULL,
    channel    TEXT NOT NULL,
    sender     TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'note',
    body       TEXT NOT NULL DEFAULT '{}',
    thread     TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_read ON messages(workspace, channel, seq);
CREATE INDEX IF NOT EXISTS idx_messages_expiry ON messages(expires_at);

CREATE TABLE IF NOT EXISTS cursors (
    workspace  TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    channel    TEXT NOT NULL,
    last_seq   INTEGER NOT NULL,
    expires_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace, agent_id, channel)
);
-- idx_cursors_expiry is created in Store.__post_init__, not here: a database
-- from before this column existed needs an ALTER TABLE first, and an index
-- creation on a not-yet-existing column would fail on that upgrade path.

CREATE TABLE IF NOT EXISTS board (
    workspace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    revision   INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (workspace, key)
);
CREATE INDEX IF NOT EXISTS idx_board_expiry ON board(expires_at);

-- Monotonic counter for lease fencing tokens. A holder that pauses long
-- enough to lose its lease can compare fences and discover it was superseded.
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- There is no key_bindings table. A long comment describing one used to sit
-- here, in the present tense, after the table itself was removed in #73/#76 --
-- prose about a credential store, inside the live schema, for a store that does
-- not exist. Room identifiers are hash(workspace_token), derived rather than
-- claimed, so nothing binds a token to a workspace and there is nothing to keep.
-- Removed 2026-08-31, after the same class of stale text in docs/managed-hub.md
-- was read as current and cost a live hub its perimeter. History is in git.
"""


class StoreError(Exception):
    """Base class for expected, caller-visible storage failures."""


class LeaseConflict(StoreError):
    """A lease is held by another agent."""

    def __init__(self, resource: str, holder: str, expires_at: float) -> None:
        super().__init__(f"resource {resource!r} is held by {holder!r}")
        self.resource = resource
        self.holder = holder
        self.expires_at = expires_at


class NotLeaseHolder(StoreError):
    """The caller tried to renew or release a lease it does not hold."""


@dataclass
class Agent:
    workspace: str
    id: str
    name: str
    kind: str
    branch: str | None
    task: str | None
    channels: list[str]
    meta: dict[str, Any]
    #: The agent's signing public key, sealed by the client. Opaque to the
    #: hub: stored and echoed, never verified against — verification happens
    #: between peers, who are the only ones holding the workspace key.
    pubkey: str | None
    #: The agent's X25519 exchange key, sealed by the client. Opaque to the
    #: hub in the same way `pubkey` is — stored and echoed, never used for
    #: anything here. What it enables (`ask`, in crypto.py's `seal_to_peer`)
    #: happens entirely between the two peers holding a workspace key or not.
    exchange_key: str | None
    registered_at: float
    last_seen_at: float
    #: When presence lapses. Equal to `expires_at` unless the agent promised a
    #: return, which is the only thing that separates them.
    present_until: float
    #: When the agent said it would be back, or None. Absent means the roster
    #: can say nothing about it, which is the pre-existing behaviour.
    expected_back: float | None
    expires_at: float


@dataclass
class Lease:
    workspace: str
    resource: str
    holder: str
    note: str | None
    fence: int
    acquired_at: float
    renewed_at: float
    expires_at: float


@dataclass
class Message:
    seq: int
    id: str
    workspace: str
    channel: str
    sender: str
    type: str
    body: Any
    thread: str | None
    created_at: float
    expires_at: float


@dataclass
class BoardEntry:
    workspace: str
    key: str
    value: Any
    revision: int
    updated_by: str
    updated_at: float
    expires_at: float


def _loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _agent(row: sqlite3.Row) -> Agent:
    return Agent(
        workspace=row["workspace"],
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        branch=row["branch"],
        task=row["task"],
        channels=_loads(row["channels"], []),
        meta=_loads(row["meta"], {}),
        # Databases created before this column exists still answer here, so
        # read it defensively rather than by subscript.
        pubkey=row["pubkey"] if "pubkey" in row.keys() else None,
        # Same defensive read, same reason: a database predating this column.
        exchange_key=row["exchange_key"] if "exchange_key" in row.keys() else None,
        registered_at=row["registered_at"],
        last_seen_at=row["last_seen_at"],
        # Same defensive read as `pubkey` above, for the same reason.
        present_until=(
            row["present_until"] if "present_until" in row.keys() and row["present_until"]
            else row["expires_at"]
        ),
        expected_back=row["expected_back"] if "expected_back" in row.keys() else None,
        expires_at=row["expires_at"],
    )


def _lease(row: sqlite3.Row) -> Lease:
    return Lease(
        workspace=row["workspace"],
        resource=row["resource"],
        holder=row["holder"],
        note=row["note"],
        fence=row["fence"],
        acquired_at=row["acquired_at"],
        renewed_at=row["renewed_at"],
        expires_at=row["expires_at"],
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        seq=row["seq"],
        id=row["id"],
        workspace=row["workspace"],
        channel=row["channel"],
        sender=row["sender"],
        type=row["type"],
        body=_loads(row["body"], None),
        thread=row["thread"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _board(row: sqlite3.Row) -> BoardEntry:
    return BoardEntry(
        workspace=row["workspace"],
        key=row["key"],
        value=_loads(row["value"], None),
        revision=row["revision"],
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


# --- store ------------------------------------------------------------------


@dataclass
class Store:
    """Thread-safe SQLite store. One instance per process is expected."""

    path: str
    _local: threading.local = field(default_factory=threading.local, repr=False)

    def __post_init__(self) -> None:
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(parent, exist_ok=True)
        # ``:memory:`` would give each thread its own empty database, which
        # silently breaks every cross-thread test. Share one instead.
        self._shared: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._shared = self._new_connection()
            self._shared_lock = threading.RLock()
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate_cursors_expiry(conn)

    def _migrate_cursors_expiry(self, conn: sqlite3.Connection) -> None:
        """Add ``cursors.expires_at`` to databases created before it existed.

        ``CREATE TABLE IF NOT EXISTS`` in SCHEMA is a no-op against an
        existing table, so a cursors table from before this column existed
        needs an explicit ALTER. On a fresh database the column is already
        there from SCHEMA, so this is just a PRAGMA check plus a
        redundant-but-harmless index creation — safe to run unconditionally
        every time the store opens.
        """
        # Same reasoning for agents.pubkey: CREATE TABLE IF NOT EXISTS will
        # not add a column to a database that predates it.
        agent_cols = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
        if "pubkey" not in agent_cols:
            conn.execute("ALTER TABLE agents ADD COLUMN pubkey TEXT")
        if "exchange_key" not in agent_cols:
            conn.execute("ALTER TABLE agents ADD COLUMN exchange_key TEXT")
        if "present_until" not in agent_cols:
            # Backfilled from expires_at, which is exactly what it meant before
            # the two were separated — an existing row has no away promise.
            conn.execute(
                "ALTER TABLE agents ADD COLUMN present_until REAL NOT NULL DEFAULT 0"
            )
            conn.execute("UPDATE agents SET present_until = expires_at")
        if "expected_back" not in agent_cols:
            conn.execute("ALTER TABLE agents ADD COLUMN expected_back REAL")
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(cursors)")}
        if "expires_at" not in cols:
            conn.execute("ALTER TABLE cursors ADD COLUMN expires_at REAL NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cursors_expiry ON cursors(expires_at)")

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=15.0,
            check_same_thread=False,
            isolation_level=None,  # explicit transaction control
        )
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._shared is not None:
            with self._shared_lock:
                yield self._shared
            return
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        yield conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that takes SQLite's write lock immediately.

        ``BEGIN IMMEDIATE`` is what makes read-then-write sequences safe under
        concurrency: without it two agents can both read "lease is free" before
        either writes.
        """
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _next_fence(self, conn: sqlite3.Connection) -> int:
        conn.execute(
            "INSERT INTO counters(name, value) VALUES('fence', 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1",
        )
        row = conn.execute("SELECT value FROM counters WHERE name='fence'").fetchone()
        return int(row["value"])

    # --- presence ----------------------------------------------------------

    def register_agent(
        self,
        *,
        workspace: str,
        agent_id: str | None,
        name: str,
        kind: str = "unknown",
        branch: str | None = None,
        task: str | None = None,
        channels: Sequence[str] = (),
        meta: dict[str, Any] | None = None,
        pubkey: str | None = None,
        exchange_key: str | None = None,
        ttl: float,
        expected_back: float | None = None,
        now: float | None = None,
    ) -> Agent:
        now = time.time() if now is None else now
        agent_id = agent_id or f"agent-{uuid.uuid4().hex[:12]}"
        chan_json = json.dumps(list(dict.fromkeys(channels)))
        meta_json = json.dumps(meta or {})
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO agents (workspace, id, name, kind, branch, task, channels, meta,
                                    pubkey, exchange_key, registered_at, last_seen_at,
                                    present_until, expected_back, expires_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace, id) DO UPDATE SET
                    name=excluded.name,
                    kind=excluded.kind,
                    branch=excluded.branch,
                    task=excluded.task,
                    channels=excluded.channels,
                    meta=excluded.meta,
                    pubkey=excluded.pubkey,
                    exchange_key=excluded.exchange_key,
                    last_seen_at=excluded.last_seen_at,
                    present_until=excluded.present_until,
                    expected_back=excluded.expected_back,
                    expires_at=excluded.expires_at
                """,
                (
                    workspace, agent_id, name, kind, branch, task, chan_json, meta_json,
                    pubkey, exchange_key, now, now, now + ttl, expected_back,
                    # The row outlives presence only as far as the promise. A
                    # promise already in the past keeps nothing alive.
                    max(now + ttl, expected_back or 0.0),
                ),
            )
            row = conn.execute(
                "SELECT * FROM agents WHERE workspace=? AND id=?", (workspace, agent_id)
            ).fetchone()
        return _agent(row)

    def heartbeat(
        self,
        *,
        workspace: str,
        agent_id: str,
        ttl: float,
        task: str | None = None,
        renew_leases: bool = True,
        lease_ttl: float | None = None,
        expected_back: float | None = None,
        now: float | None = None,
    ) -> tuple[Agent | None, list[Lease]]:
        """Refresh an agent's presence and, by default, every lease it holds.

        Folding lease renewal into the heartbeat is deliberate: an agent that
        dies stops heartbeating, and its claims fall away on their own. That is
        the property a manually-released claim never has.
        """
        now = time.time() if now is None else now
        with self._tx() as conn:
            # `expected_back` is only written when supplied: a heartbeat that
            # says nothing about coming back must not silently retract a
            # promise the agent made when it announced.
            sets = ["last_seen_at=?", "present_until=?"]
            args: list[Any] = [now, now + ttl]
            if expected_back is not None:
                sets.append("expected_back=?")
                args.append(expected_back)
                sets.append("expires_at=?")
                args.append(max(now + ttl, expected_back))
            else:
                sets.append("expires_at=MAX(?, COALESCE(expected_back, 0))")
                args.append(now + ttl)
            if task is not None:
                sets.append("task=?")
                args.append(task)
            cur = conn.execute(
                f"UPDATE agents SET {', '.join(sets)} WHERE workspace=? AND id=?",
                (*args, workspace, agent_id),
            )
            if cur.rowcount == 0:
                return None, []
            renewed: list[Lease] = []
            if renew_leases:
                extend = lease_ttl if lease_ttl is not None else None
                if extend is not None:
                    conn.execute(
                        "UPDATE leases SET renewed_at=?, expires_at=? "
                        "WHERE workspace=? AND holder=? AND expires_at > ?",
                        (now, now + extend, workspace, agent_id, now),
                    )
                else:
                    # Preserve each lease's own duration rather than imposing one.
                    conn.execute(
                        "UPDATE leases SET renewed_at=?, "
                        "expires_at = ? + (expires_at - renewed_at) "
                        "WHERE workspace=? AND holder=? AND expires_at > ?",
                        (now, now, workspace, agent_id, now),
                    )
                renewed = [
                    _lease(r)
                    for r in conn.execute(
                        "SELECT * FROM leases WHERE workspace=? AND holder=? AND expires_at > ? "
                        "ORDER BY resource",
                        (workspace, agent_id, now),
                    )
                ]
            row = conn.execute(
                "SELECT * FROM agents WHERE workspace=? AND id=?", (workspace, agent_id)
            ).fetchone()
        return _agent(row), renewed

    def list_agents(self, *, workspace: str, now: float | None = None) -> list[Agent]:
        now = time.time() if now is None else now
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM agents WHERE workspace=? AND expires_at > ? ORDER BY name, id",
                (workspace, now),
            ).fetchall()
        return [_agent(r) for r in rows]

    def get_agent(self, *, workspace: str, agent_id: str, now: float | None = None) -> Agent | None:
        now = time.time() if now is None else now
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE workspace=? AND id=? AND expires_at > ?",
                (workspace, agent_id, now),
            ).fetchone()
        return _agent(row) if row else None

    def deregister_agent(self, *, workspace: str, agent_id: str,
                         release_leases: bool = True) -> bool:
        with self._tx() as conn:
            if release_leases:
                conn.execute(
                    "DELETE FROM leases WHERE workspace=? AND holder=?", (workspace, agent_id)
                )
            cur = conn.execute(
                "DELETE FROM agents WHERE workspace=? AND id=?", (workspace, agent_id)
            )
        return cur.rowcount > 0

    # --- leases ------------------------------------------------------------

    def acquire_lease(
        self,
        *,
        workspace: str,
        resource: str,
        holder: str,
        ttl: float,
        note: str | None = None,
        steal_expired: bool = True,
        now: float | None = None,
    ) -> Lease:
        """Take an exclusive lease on ``resource``.

        Re-acquiring a lease you already hold is a renewal, not a conflict, so
        this is safe to call unconditionally at the top of a task.
        """
        now = time.time() if now is None else now
        with self._tx() as conn:
            if steal_expired:
                conn.execute(
                    "DELETE FROM leases WHERE workspace=? AND resource=? AND expires_at <= ?",
                    (workspace, resource, now),
                )
            existing = conn.execute(
                "SELECT * FROM leases WHERE workspace=? AND resource=?", (workspace, resource)
            ).fetchone()
            if existing is not None:
                if existing["holder"] != holder:
                    raise LeaseConflict(resource, existing["holder"], existing["expires_at"])
                conn.execute(
                    "UPDATE leases SET renewed_at=?, expires_at=?, note=COALESCE(?, note) "
                    "WHERE workspace=? AND resource=?",
                    (now, now + ttl, note, workspace, resource),
                )
            else:
                conn.execute(
                    "INSERT INTO leases (workspace, resource, holder, note, fence, "
                    "acquired_at, renewed_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                    (workspace, resource, holder, note, self._next_fence(conn),
                     now, now, now + ttl),
                )
            row = conn.execute(
                "SELECT * FROM leases WHERE workspace=? AND resource=?", (workspace, resource)
            ).fetchone()
        return _lease(row)

    def renew_lease(self, *, workspace: str, resource: str, holder: str, ttl: float,
                    now: float | None = None) -> Lease:
        now = time.time() if now is None else now
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM leases WHERE workspace=? AND resource=? AND expires_at > ?",
                (workspace, resource, now),
            ).fetchone()
            if row is None:
                raise NotLeaseHolder(f"no live lease on {resource!r}")
            if row["holder"] != holder:
                raise NotLeaseHolder(
                    f"lease on {resource!r} is held by {row['holder']!r}, not {holder!r}"
                )
            conn.execute(
                "UPDATE leases SET renewed_at=?, expires_at=? WHERE workspace=? AND resource=?",
                (now, now + ttl, workspace, resource),
            )
            row = conn.execute(
                "SELECT * FROM leases WHERE workspace=? AND resource=?", (workspace, resource)
            ).fetchone()
        return _lease(row)

    def release_lease(self, *, workspace: str, resource: str, holder: str,
                      force: bool = False) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM leases WHERE workspace=? AND resource=?", (workspace, resource)
            ).fetchone()
            if row is None:
                return False
            if not force and row["holder"] != holder:
                raise NotLeaseHolder(
                    f"lease on {resource!r} is held by {row['holder']!r}, not {holder!r}"
                )
            conn.execute(
                "DELETE FROM leases WHERE workspace=? AND resource=?", (workspace, resource)
            )
        return True

    def list_leases(self, *, workspace: str, holder: str | None = None,
                    now: float | None = None) -> list[Lease]:
        now = time.time() if now is None else now
        sql = "SELECT * FROM leases WHERE workspace=? AND expires_at > ?"
        args: list[Any] = [workspace, now]
        if holder:
            sql += " AND holder=?"
            args.append(holder)
        sql += " ORDER BY resource"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_lease(r) for r in rows]

    def get_lease(self, *, workspace: str, resource: str,
                  now: float | None = None) -> Lease | None:
        now = time.time() if now is None else now
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM leases WHERE workspace=? AND resource=? AND expires_at > ?",
                (workspace, resource, now),
            ).fetchone()
        return _lease(row) if row else None

    # --- messages ----------------------------------------------------------

    def post(
        self,
        *,
        workspace: str,
        channel: str,
        sender: str,
        body: Any,
        type: str = "note",
        thread: str | None = None,
        ttl: float,
        now: float | None = None,
    ) -> Message:
        now = time.time() if now is None else now
        msg_id = f"msg-{uuid.uuid4().hex[:16]}"
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO messages (id, workspace, channel, sender, type, body, thread, "
                "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (msg_id, workspace, channel, sender, type, json.dumps(body), thread,
                 now, now + ttl),
            )
            row = conn.execute(
                "SELECT * FROM messages WHERE seq=?", (cur.lastrowid,)
            ).fetchone()
        return _message(row)

    def read(
        self,
        *,
        workspace: str,
        channels: Sequence[str],
        agent_id: str | None = None,
        since: int | None = None,
        limit: int = 100,
        include_own: bool = False,
        commit_cursor: bool = True,
        cursor_ttl: float = DEFAULT_CURSOR_TTL,
        now: float | None = None,
    ) -> list[Message]:
        """Read messages on ``channels``.

        When ``since`` is None the per-(agent, channel) cursor is used, so an
        agent that polls repeatedly sees each message exactly once. Passing an
        explicit ``since`` (or ``commit_cursor=False``) makes the read a
        non-destructive peek.

        A cursor's own ``expires_at`` is independent of the agent's presence
        row — an agent that goes quiet for a while (a long turn, a gap
        between sessions) and later calls ``checkin`` must not come back to
        find its read position reset and its whole history replayed. Only an
        agent that never reads at all for ``cursor_ttl`` forfeits its place.
        """
        now = time.time() if now is None else now
        if not channels:
            return []
        out: list[Message] = []
        with self._tx() as conn:
            for channel in channels:
                if since is not None:
                    start = since
                elif agent_id:
                    row = conn.execute(
                        "SELECT last_seq FROM cursors WHERE workspace=? AND agent_id=? "
                        "AND channel=? AND expires_at > ?",
                        (workspace, agent_id, channel, now),
                    ).fetchone()
                    start = row["last_seq"] if row else 0
                else:
                    start = 0
                sql = (
                    "SELECT * FROM messages WHERE workspace=? AND channel=? AND seq > ? "
                    "AND expires_at > ?"
                )
                args: list[Any] = [workspace, channel, start, now]
                if not include_own and agent_id:
                    sql += " AND sender != ?"
                    args.append(agent_id)
                sql += " ORDER BY seq LIMIT ?"
                args.append(limit)
                rows = conn.execute(sql, args).fetchall()
                msgs = [_message(r) for r in rows]
                out.extend(msgs)
                if commit_cursor and agent_id and since is None:
                    # Advance past everything visible now, including our own
                    # messages and anything filtered out, so a skipped message
                    # is not re-scanned on every poll.
                    top = conn.execute(
                        "SELECT MAX(seq) AS m FROM messages WHERE workspace=? AND channel=? "
                        "AND seq > ? AND expires_at > ?",
                        (workspace, channel, start, now),
                    ).fetchone()["m"]
                    new_cursor = msgs[-1].seq if len(msgs) == limit else (top or start)
                    if new_cursor and new_cursor > start:
                        conn.execute(
                            "INSERT INTO cursors (workspace, agent_id, channel, last_seq, "
                            "expires_at) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(workspace, agent_id, channel) "
                            "DO UPDATE SET last_seq=excluded.last_seq, "
                            "expires_at=excluded.expires_at",
                            (workspace, agent_id, channel, new_cursor, now + cursor_ttl),
                        )
        out.sort(key=lambda m: m.seq)
        return out

    def count_unread(self, *, workspace: str, channel: str, agent_id: str,
                      now: float | None = None) -> int:
        """How many unread messages sit in one channel for one agent.

        A cheap, non-destructive signal meant to ride along on operations
        that are not themselves reading the inbox: one cursor lookup plus one
        indexed count, no message rows fetched or decrypted, and the cursor
        is never touched — calling this any number of times changes nothing
        about what a later `read()` returns.

        Must treat an expired cursor exactly as `read()` does (as if absent,
        i.e. `start=0`) — otherwise a stale cursor row past its own
        `cursor_ttl` would undercount here relative to what `read()` would
        actually return for the same agent.
        """
        now = time.time() if now is None else now
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_seq FROM cursors WHERE workspace=? AND agent_id=? "
                "AND channel=? AND expires_at > ?",
                (workspace, agent_id, channel, now),
            ).fetchone()
            start = row["last_seq"] if row else 0
            return conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE workspace=? AND channel=? "
                "AND seq > ? AND expires_at > ? AND sender != ?",
                (workspace, channel, start, now, agent_id),
            ).fetchone()["n"]

    def peek(self, *, workspace: str, channel: str, limit: int = 50,
             now: float | None = None) -> list[Message]:
        """Most recent messages on a channel, oldest-first, cursor untouched."""
        now = time.time() if now is None else now
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE workspace=? AND channel=? AND expires_at > ? "
                "ORDER BY seq DESC LIMIT ?",
                (workspace, channel, now, limit),
            ).fetchall()
        return [_message(r) for r in reversed(rows)]

    def list_channels(self, *, workspace: str, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT channel, COUNT(*) AS n, MAX(created_at) AS latest FROM messages "
                "WHERE workspace=? AND expires_at > ? GROUP BY channel ORDER BY channel",
                (workspace, now),
            ).fetchall()
        return [
            {"channel": r["channel"], "messages": r["n"], "latest_at": r["latest"]} for r in rows
        ]

    def max_seq(self, *, workspace: str, channels: Sequence[str],
                now: float | None = None) -> int:
        now = time.time() if now is None else now
        if not channels:
            return 0
        placeholders = ",".join("?" for _ in channels)
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT MAX(seq) AS m FROM messages WHERE workspace=? "  # noqa: S608
                f"AND channel IN ({placeholders}) AND expires_at > ?",
                [workspace, *channels, now],
            ).fetchone()
        return int(row["m"] or 0)

    # --- blackboard --------------------------------------------------------

    def board_set(
        self,
        *,
        workspace: str,
        key: str,
        value: Any,
        updated_by: str,
        ttl: float,
        if_revision: int | None = None,
        now: float | None = None,
    ) -> BoardEntry:
        """Write a blackboard entry.

        ``if_revision`` gives optimistic concurrency: pass the revision you
        read and the write fails if someone else got there first. Pass 0 to
        mean "only if absent".
        """
        now = time.time() if now is None else now
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM board WHERE workspace=? AND key=? AND expires_at > ?",
                (workspace, key, now),
            ).fetchone()
            current_rev = row["revision"] if row else 0
            if if_revision is not None and if_revision != current_rev:
                raise StoreError(
                    f"revision mismatch on {key!r}: expected {if_revision}, found {current_rev}"
                )
            conn.execute(
                """
                INSERT INTO board (workspace, key, value, revision, updated_by,
                                   updated_at, expires_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(workspace, key) DO UPDATE SET
                    value=excluded.value,
                    revision=excluded.revision,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
                """,
                (workspace, key, json.dumps(value), current_rev + 1, updated_by,
                 now, now + ttl),
            )
            row = conn.execute(
                "SELECT * FROM board WHERE workspace=? AND key=?", (workspace, key)
            ).fetchone()
        return _board(row)

    def board_get(self, *, workspace: str, key: str,
                  now: float | None = None) -> BoardEntry | None:
        now = time.time() if now is None else now
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM board WHERE workspace=? AND key=? AND expires_at > ?",
                (workspace, key, now),
            ).fetchone()
        return _board(row) if row else None

    def board_list(self, *, workspace: str, prefix: str | None = None,
                   now: float | None = None) -> list[BoardEntry]:
        now = time.time() if now is None else now
        sql = "SELECT * FROM board WHERE workspace=? AND expires_at > ?"
        args: list[Any] = [workspace, now]
        if prefix:
            sql += " AND key LIKE ? ESCAPE '\\'"
            args.append(prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        sql += " ORDER BY key"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_board(r) for r in rows]

    def board_delete(self, *, workspace: str, key: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM board WHERE workspace=? AND key=?", (workspace, key))
        return cur.rowcount > 0

    # --- maintenance -------------------------------------------------------

    def sweep(self, *, now: float | None = None) -> dict[str, int]:
        """Hard-delete expired rows. Reads already ignore them; this reclaims disk."""
        now = time.time() if now is None else now
        counts: dict[str, int] = {}
        with self._tx() as conn:
            for table in ("agents", "leases", "messages", "board", "cursors"):
                cur = conn.execute(f"DELETE FROM {table} WHERE expires_at <= ?", (now,))  # noqa: S608
                counts[table] = cur.rowcount
        return counts

    def stats(self, *, workspace: str | None = None, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        out: dict[str, Any] = {}
        with self._conn() as conn:
            for table in ("agents", "leases", "messages", "board"):
                if workspace:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table} "  # noqa: S608
                        f"WHERE workspace=? AND expires_at > ?",
                        (workspace, now),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE expires_at > ?",  # noqa: S608
                        (now,),
                    ).fetchone()
                out[table] = row["n"]
            # Deliberately not a list of workspace identifiers.
            #
            # A room identifier is the only thing standing between a stranger
            # and a room: `hash(workspace_token)`, unguessable, and since
            # authorization was removed (#61) it is the *whole* protection.
            # Publishing the set here handed it over — enumerate, then read and
            # post in every room on the hub. That was survivable while a token
            # was required and fatal the moment one was not.
            #
            # The count is kept because an operator wants to know how many
            # rooms are live; which ones they are is not theirs to hand out.
            rows = conn.execute("SELECT DISTINCT workspace FROM agents").fetchall()
            out["workspace_count"] = len({r["workspace"] for r in rows})
        return out

    # --- key bindings --------------------------------------------------------
