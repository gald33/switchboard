"""Sealed messages this machine could not open yet, kept until it can.

**The moment a caller is guaranteed to lack a peer's key is that peer's first
message.** Opening a whisper needs the sender's exchange key, the cursor
advances whether or not the body opened, and the hub forgets the message on its
own schedule. So the default read destroyed, specifically and reliably, first
contact from anyone not already known — the messages with the least redundancy
and the highest cost to reconstruct.

Observed 2026-09-07: two whispers arrived while no key was held for the sender.
Both were consumed. Reading the roster afterwards did not bring them back, and
the sender was a cloud agent whose presence had already lapsed. The content —
a report and an authorisation — was rebuilt by hand over the following hour.

What this changes: a message that cannot be opened is written here on its way
past, and every later read tries the stash again with whatever keys are known
by then. Learning the sender's key one minute or one day later is enough to
recover it, which is the property that was missing. The cursor still advances;
the hub is still the hub. This is a local copy of something already delivered
to this agent, held because this agent could not yet read its own mail.

Kept in the home directory for the same reasons as the timing database and the
peer log: it is nobody else's business, it must never reach the hub, and it
must never be committed. The bodies here are still sealed — this store holds
ciphertext it cannot read, exactly like the hub.

Entries are pruned after `RETENTION_SECONDS`. A stash that grew forever would
contradict the one idea this project is built on, and a key that has not
arrived in a week is not arriving.

Every method fails soft. A read-only home directory or a corrupt file should
cost the recovery, never the command the user actually ran.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Callable

DEFAULT_PATH = "~/.switchboard/stash.db"

#: Long enough that a peer who reappears the next working day still rescues its
#: message; short enough that this never becomes an archive.
RETENTION_SECONDS = 7 * 24 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unopened (
    workspace  TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    message    TEXT NOT NULL,
    stashed_at REAL NOT NULL,
    PRIMARY KEY (workspace, agent_id, seq)
);
"""


class UnopenedStash:
    """Messages delivered to this agent that it could not decrypt yet."""

    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = os.path.expanduser(path)
        self._conn: sqlite3.Connection | None = None

    # -- plumbing ------------------------------------------------------------

    def _open(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            self._conn = conn
        except (OSError, sqlite3.Error):
            return None                     # see the module docstring
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # -- use -----------------------------------------------------------------

    def put(self, workspace: str, agent_id: str, message: dict[str, Any]) -> None:
        """Keep one message that would otherwise be gone once the cursor moved."""
        seq = message.get("seq")
        if seq is None:
            return
        conn = self._open()
        if conn is None:
            return
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO unopened "
                    "(workspace, agent_id, seq, message, stashed_at) VALUES (?,?,?,?,?)",
                    (workspace, agent_id, int(seq), json.dumps(message), time.time()),
                )
        except (sqlite3.Error, TypeError, ValueError):
            return

    def recover(
        self, workspace: str, agent_id: str, opener: Callable[[dict[str, Any]], bool],
    ) -> list[dict[str, Any]]:
        """Re-try every stashed message, returning the ones that open now.

        `opener` mutates a message in place and says whether it opened. What
        opens is deleted; what does not stays for the next attempt, because the
        key may still be on its way.
        """
        conn = self._open()
        if conn is None:
            return []
        try:
            self.prune()
            rows = conn.execute(
                "SELECT seq, message FROM unopened WHERE workspace=? AND agent_id=? "
                "ORDER BY seq",
                (workspace, agent_id),
            ).fetchall()
        except sqlite3.Error:
            return []

        opened: list[dict[str, Any]] = []
        done: list[int] = []
        for row in rows:
            try:
                message = json.loads(row["message"])
            except (ValueError, TypeError):
                done.append(row["seq"])     # unparseable: drop it rather than retry forever
                continue
            try:
                if opener(message):
                    message.pop("unreadable", None)
                    message["recovered"] = True
                    opened.append(message)
                    done.append(row["seq"])
            except Exception:               # noqa: BLE001 -- a failed open is not an error
                continue
        if done:
            try:
                with conn:
                    conn.executemany(
                        "DELETE FROM unopened WHERE workspace=? AND agent_id=? AND seq=?",
                        [(workspace, agent_id, s) for s in done],
                    )
            except sqlite3.Error:
                pass
        return opened

    def prune(self, now: float | None = None) -> None:
        """Forget what nobody came back for."""
        conn = self._open()
        if conn is None:
            return
        cutoff = (now if now is not None else time.time()) - RETENTION_SECONDS
        try:
            with conn:
                conn.execute("DELETE FROM unopened WHERE stashed_at < ?", (cutoff,))
        except sqlite3.Error:
            pass
