"""What this machine has witnessed about other agents' signing keys.

A roster entry is a noticeboard entry: registration upserts on
``(workspace, agent_id)`` and nothing validates it, so anyone may announce as
anyone. That is deliberate — a first-writer-wins column on the hub would be
the registry that was removed on purpose — and it puts the noticing on the
peer, in :meth:`switchboard.client._Base.note_peer_keys`.

Noticing needs a memory, though, and it needed one that outlives a process.
The in-process version can only fire for a client that witnesses the same peer
twice in one run: the MCP bridge does, every CLI command does not. So the
detector was structurally dead in the surface most agents actually use, and
the failure it exists to catch went unreported in this project's own
dogfooding — a spawned agent inherited its parent's session id, derived the
same agent id, announced over the parent's roster row, and nothing anywhere
said a word.

This is a *witness log*, not an identity. Identity still does not outlive a
process; what outlives it is "this machine once saw that id alive under that
key", which is an observation about somebody else. Kept local for the same
reason the timing database is (``docs/adaptive-timing.md``): it is nobody
else's business, it must never reach the hub, and it must never be committed.

Every method fails soft. A read-only home directory or a corrupt file should
cost the swap warning, never the command the user actually ran.
"""

from __future__ import annotations

import os
import sqlite3
import time

DEFAULT_PATH = "~/.switchboard/peers.db"


class PeerKeyLog:
    """Signing keys this machine has seen each peer announce under."""

    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = os.path.expanduser(path)
        self._conn: sqlite3.Connection | None = None
        self._broken = False

    def _connection(self) -> sqlite3.Connection | None:
        if self._broken:
            return None
        if self._conn is None:
            try:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                conn = sqlite3.connect(self.path, timeout=1.0)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS peer_keys (
                        workspace  TEXT NOT NULL,
                        agent_id   TEXT NOT NULL,
                        pubkey     TEXT NOT NULL,
                        first_seen REAL NOT NULL,
                        PRIMARY KEY (workspace, agent_id, pubkey)
                    );

                    -- `swapped` is sticky. A swap is worth reporting after the
                    -- fact as well as at the moment it happens: the agent that
                    -- would have seen it live is usually not the one that goes
                    -- looking afterwards.
                    CREATE TABLE IF NOT EXISTS peer_state (
                        workspace TEXT NOT NULL,
                        agent_id  TEXT NOT NULL,
                        was_live  INTEGER NOT NULL,
                        swapped   INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (workspace, agent_id)
                    );
                    """
                )
                conn.commit()
                self._conn = conn
            except (sqlite3.Error, OSError):
                self._broken = True
                return None
        return self._conn

    def known_keys(self, workspace: str, agent_id: str) -> set[str]:
        conn = self._connection()
        if conn is None:
            return set()
        try:
            rows = conn.execute(
                "SELECT pubkey FROM peer_keys WHERE workspace = ? AND agent_id = ?",
                (workspace, agent_id),
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {row[0] for row in rows}

    def state(self, workspace: str, agent_id: str) -> tuple[bool | None, bool]:
        """``(was_live, swapped)`` — ``was_live`` is None if never witnessed."""
        conn = self._connection()
        if conn is None:
            return (None, False)
        try:
            row = conn.execute(
                "SELECT was_live, swapped FROM peer_state "
                "WHERE workspace = ? AND agent_id = ?",
                (workspace, agent_id),
            ).fetchone()
        except sqlite3.Error:
            return (None, False)
        if row is None:
            return (None, False)
        return (bool(row[0]), bool(row[1]))

    def record(self, workspace: str, agent_id: str, pubkey: str, *,
               live: bool, swapped: bool = False) -> None:
        conn = self._connection()
        if conn is None:
            return
        try:
            conn.execute(
                "INSERT OR IGNORE INTO peer_keys "
                "(workspace, agent_id, pubkey, first_seen) VALUES (?, ?, ?, ?)",
                (workspace, agent_id, pubkey, time.time()),
            )
            # `swapped` is OR-ed rather than assigned, so a later quiet read
            # cannot clear a swap an earlier one established.
            conn.execute(
                "INSERT INTO peer_state (workspace, agent_id, was_live, swapped) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(workspace, agent_id) DO UPDATE SET "
                "  was_live = excluded.was_live, "
                "  swapped = MAX(peer_state.swapped, excluded.swapped)",
                (workspace, agent_id, int(live), int(swapped)),
            )
            conn.commit()
        except sqlite3.Error:
            return

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
