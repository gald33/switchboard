"""Adaptive timing forecasts — local, per-agent, learned check-in timing.

This is infrastructure, not a scheduler. It never decides when an agent is
allowed to act and a forecast is never a commitment: it is one agent's own
estimate of when *it* is likely to check in again, offered to collaborators
as a hint.

Design boundary (see docs/adaptive-timing.md for the full writeup):

* A model only ever supplies two cheap semantic judgments — an
  ``execution_class`` (a short free-form label like "coding" or "research";
  no fixed taxonomy is enforced, it emerges from use) and an ``effort``
  level (``low`` / ``medium`` / ``high``). It never estimates seconds.
* Everything else — recording the observation, consulting history,
  estimating a distribution, deriving percentile checkpoints — is
  deterministic runtime behaviour, done here.
* All of this is local: raw timing history and the learned distributions
  never leave this process/machine. Only the two resulting timestamps
  (p50, p95) are ever attached to an outgoing message.
* The database is opened lazily and is safe to be missing, empty, or
  corrupt-and-recreated — nothing about coordination correctness depends
  on it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

#: The only ordinal scale a model needs to reason about. Kept small and
#: closed because "how big is this, roughly" is a judgment agents are
#: consistently good at; a wider scale would just add noise.
EFFORT_LEVELS = ("low", "medium", "high")

#: Conservative bootstrap priors (seconds) used before any local history
#: exists for a given effort level. Deliberately wide — a bad-but-honest
#: prior is safer than a falsely tight one while the model has no data.
_BOOTSTRAP_SECONDS: dict[str, tuple[float, float]] = {
    "low": (30.0, 180.0),
    "medium": (120.0, 900.0),
    "high": (600.0, 3600.0),
}
_DEFAULT_BOOTSTRAP = _BOOTSTRAP_SECONDS["medium"]

#: An observed delta this large is almost certainly a restart, an
#: overnight gap, or an unrelated event rather than genuine work time, so
#: it is dropped rather than poisoning the distribution.
MAX_OBSERVATION_SECONDS = 6 * 3600.0

#: Below this many samples, a bucket is considered too sparse to trust and
#: the estimator falls back to a coarser one.
MIN_SAMPLES = 5


def _now() -> float:
    return time.time()


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


@dataclass
class Forecast:
    p50_seconds: float
    p95_seconds: float
    now: float
    source: str  # which fallback tier produced it — local diagnostics only
    samples: int

    def as_message_meta(self) -> dict[str, str]:
        """The sparse, shareable summary. No internals leak past this."""
        return {
            "p50": _iso(self.now + self.p50_seconds),
            "p95": _iso(self.now + self.p95_seconds),
        }


class TimingModel:
    """Local (never-shared) store of one agent's check-in timing history.

    Keyed by (agent_id, workspace) so a single machine can host multiple
    agent identities without their histories mixing. Nothing here is sent
    to the hub or to any other agent — see module docstring.
    """

    def __init__(self, db_path: str = "~/.switchboard/timing.db") -> None:
        self.db_path = os.path.expanduser(db_path)
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            directory = os.path.dirname(self.db_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id        TEXT NOT NULL,
                    workspace       TEXT NOT NULL,
                    execution_class TEXT NOT NULL,
                    effort          TEXT NOT NULL,
                    delta_seconds   REAL NOT NULL,
                    observed_at     REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observations_lookup
                    ON observations(agent_id, workspace, execution_class, effort);

                CREATE TABLE IF NOT EXISTS pending (
                    agent_id        TEXT NOT NULL,
                    workspace       TEXT NOT NULL,
                    execution_class TEXT NOT NULL,
                    effort          TEXT NOT NULL,
                    since           REAL NOT NULL,
                    PRIMARY KEY (agent_id, workspace)
                );
                """
            )
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- observation -----------------------------------------------------

    def _record(self, agent_id: str, workspace: str, execution_class: str,
                effort: str, delta_seconds: float, now: float) -> None:
        if not (0 < delta_seconds <= MAX_OBSERVATION_SECONDS):
            return
        conn = self._connection()
        conn.execute(
            "INSERT INTO observations "
            "(agent_id, workspace, execution_class, effort, delta_seconds, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, workspace, execution_class, effort, delta_seconds, now),
        )
        conn.commit()

    def _pending(self, agent_id: str, workspace: str) -> tuple[str, str, float] | None:
        row = self._connection().execute(
            "SELECT execution_class, effort, since FROM pending "
            "WHERE agent_id = ? AND workspace = ?",
            (agent_id, workspace),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def _set_pending(self, agent_id: str, workspace: str, execution_class: str | None,
                      effort: str | None, now: float) -> None:
        conn = self._connection()
        conn.execute(
            "DELETE FROM pending WHERE agent_id = ? AND workspace = ?", (agent_id, workspace),
        )
        if execution_class or effort:
            conn.execute(
                "INSERT INTO pending (agent_id, workspace, execution_class, effort, since) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, workspace, execution_class or "unspecified", effort or "medium", now),
            )
        conn.commit()

    def observe_and_classify(
        self, agent_id: str, workspace: str,
        execution_class: str | None, effort: str | None,
        now: float | None = None,
    ) -> Forecast | None:
        """Record the gap since the agent's last classified activity, then —
        if this activity is itself classified — return a fresh forecast for
        it. This is the one entry point the send path needs to call.

        An observation is "time from one classified check-in to the next
        agent activity of any kind" (see docs/adaptive-timing.md for why
        raw wall-clock deltas alone are not always trustworthy, and how
        that can be refined later without changing this interface).
        """
        now = _now() if now is None else now
        prior = self._pending(agent_id, workspace)
        if prior is not None:
            prior_class, prior_effort, since = prior
            self._record(agent_id, workspace, prior_class, prior_effort, now - since, now)

        self._set_pending(agent_id, workspace, execution_class, effort, now)

        if not execution_class and not effort:
            return None
        return self.forecast(agent_id, workspace, execution_class, effort, now=now)

    # --- estimation --------------------------------------------------------

    def _samples(self, agent_id: str, workspace: str,
                 execution_class: str | None, effort: str | None) -> list[float]:
        clauses = ["agent_id = ?", "workspace = ?"]
        params: list[object] = [agent_id, workspace]
        if execution_class is not None:
            clauses.append("execution_class = ?")
            params.append(execution_class)
        if effort is not None:
            clauses.append("effort = ?")
            params.append(effort)
        rows = self._connection().execute(
            f"SELECT delta_seconds FROM observations WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _quantile(sorted_values: list[float], q: float) -> float:
        if not sorted_values:
            raise ValueError("empty sample")
        idx = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
        return sorted_values[idx]

    def _estimate(self, agent_id: str, workspace: str,
                   execution_class: str | None, effort: str | None,
                   source: str) -> tuple[float, float, str, int] | None:
        values = self._samples(agent_id, workspace, execution_class, effort)
        if len(values) < MIN_SAMPLES:
            return None
        values.sort()
        return self._quantile(values, 0.50), self._quantile(values, 0.95), source, len(values)

    def forecast(
        self, agent_id: str, workspace: str,
        execution_class: str | None, effort: str | None,
        now: float | None = None,
    ) -> Forecast:
        """Estimate (p50, p95) seconds until this agent's next check-in.

        Falls back through a hierarchy from the most specific bucket this
        agent has enough history for, down to a fixed bootstrap prior:

            (execution_class, effort) -> execution_class -> effort
                -> agent-wide -> bootstrap default
        """
        now = _now() if now is None else now
        candidates = []
        if execution_class and effort:
            candidates.append((execution_class, effort, "class+effort"))
        if execution_class:
            candidates.append((execution_class, None, "class"))
        if effort:
            candidates.append((None, effort, "effort"))
        candidates.append((None, None, "agent-wide"))

        for cls, eff, source in candidates:
            result = self._estimate(agent_id, workspace, cls, eff, source)
            if result is not None:
                p50, p95, source, n = result
                return Forecast(p50_seconds=p50, p95_seconds=p95, now=now,
                                 source=source, samples=n)

        p50, p95 = _BOOTSTRAP_SECONDS.get(effort or "", _DEFAULT_BOOTSTRAP)
        return Forecast(p50_seconds=p50, p95_seconds=p95, now=now, source="bootstrap", samples=0)


_default_model: TimingModel | None = None


def default_model(db_path: str = "~/.switchboard/timing.db") -> TimingModel:
    """Process-wide singleton so repeated calls share one open connection."""
    global _default_model
    if _default_model is None or _default_model.db_path != os.path.expanduser(db_path):
        if _default_model is not None:
            _default_model.close()
        _default_model = TimingModel(db_path)
    return _default_model
