"""Adaptive timing forecasts — local, per-agent, learned check-in timing.

This is infrastructure, not a scheduler. It never decides when an agent is
allowed to act and a forecast is never a commitment.

What is being predicted, precisely: **when this agent will next come and
look for messages.** It is not an estimate of when a task will finish, nor
of when the agent will next post. The question a collaborator actually has
is "if I leave this here, when will they see it?", and that is answered by
the agent's next *read* of its inbox. So the semantic classification
describes the work about to be done only insofar as that work determines
how long the agent will be heads-down before looking up again.

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
from typing import Any

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

#: Seed execution classes offered before an agent has used enough of its
#: own. Deliberately tiny — the real taxonomy is meant to emerge from use,
#: and anything an agent actually uses displaces these in the offer set.
DEFAULT_CLASSES = ("coding", "research", "review", "waiting")

#: How many execution classes to offer the model at a time. The offer is a
#: convenience, never a constraint: a custom label is always accepted.
TOP_K_CLASSES = 6

#: Recency half-life for ranking execution classes. A class the agent has
#: stopped using fades out of the offer set rather than lingering forever,
#: so the suggestions track what this agent is doing *now*.
CLASS_HALF_LIFE_SECONDS = 14 * 86400.0


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
                    observed_at     REAL NOT NULL,
                    -- What we predicted at the time this window opened, kept
                    -- so calibration ("did 95% of events land under p95?")
                    -- stays answerable after the fact. Null only for rows
                    -- written before these columns existed.
                    predicted_p50   REAL,
                    predicted_p95   REAL,
                    predicted_from  TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_observations_lookup
                    ON observations(agent_id, workspace, execution_class, effort);

                CREATE TABLE IF NOT EXISTS pending (
                    agent_id        TEXT NOT NULL,
                    workspace       TEXT NOT NULL,
                    execution_class TEXT NOT NULL,
                    effort          TEXT NOT NULL,
                    since           REAL NOT NULL,
                    predicted_p50   REAL,
                    predicted_p95   REAL,
                    predicted_from  TEXT,
                    PRIMARY KEY (agent_id, workspace)
                );
                """
            )
            self._add_missing_columns(conn)
            conn.commit()
            self._conn = conn
        return self._conn

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """Additively migrate a database created by an earlier version.

        Only ever adds nullable columns, so an old file keeps working and
        old rows simply have no calibration data attached.
        """
        for table in ("observations", "pending"):
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in (
                ("predicted_p50", "REAL"), ("predicted_p95", "REAL"),
                ("predicted_from", "TEXT"),
            ):
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- observation -----------------------------------------------------

    def _record(self, agent_id: str, workspace: str, execution_class: str,
                effort: str, delta_seconds: float, now: float,
                predicted: tuple[float | None, float | None, str | None]) -> None:
        if not (0 < delta_seconds <= MAX_OBSERVATION_SECONDS):
            return
        conn = self._connection()
        conn.execute(
            "INSERT INTO observations "
            "(agent_id, workspace, execution_class, effort, delta_seconds, observed_at, "
            " predicted_p50, predicted_p95, predicted_from) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, workspace, execution_class, effort, delta_seconds, now, *predicted),
        )
        conn.commit()

    def _pending(self, agent_id: str, workspace: str) -> tuple | None:
        return self._connection().execute(
            "SELECT execution_class, effort, since, predicted_p50, predicted_p95, "
            "predicted_from FROM pending WHERE agent_id = ? AND workspace = ?",
            (agent_id, workspace),
        ).fetchone()

    def _clear_pending(self, agent_id: str, workspace: str) -> None:
        conn = self._connection()
        conn.execute(
            "DELETE FROM pending WHERE agent_id = ? AND workspace = ?", (agent_id, workspace),
        )
        conn.commit()

    def _set_pending(self, agent_id: str, workspace: str, execution_class: str | None,
                      effort: str | None, now: float, forecast: Forecast | None) -> None:
        conn = self._connection()
        conn.execute(
            "DELETE FROM pending WHERE agent_id = ? AND workspace = ?", (agent_id, workspace),
        )
        if execution_class or effort:
            conn.execute(
                "INSERT INTO pending (agent_id, workspace, execution_class, effort, since, "
                "predicted_p50, predicted_p95, predicted_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, workspace, execution_class or "unspecified", effort or "medium", now,
                 forecast.p50_seconds if forecast else None,
                 forecast.p95_seconds if forecast else None,
                 forecast.source if forecast else None),
            )
        conn.commit()

    def note_look(self, agent_id: str, workspace: str,
                  now: float | None = None) -> None:
        """Record that the agent just checked for messages.

        This is the event the whole feature predicts. A collaborator asking
        "when will they see what I sent?" is asking when this agent next
        *reads its inbox* — not when it next posts. So the only things that
        close an open window are the tools that actually read: ``inbox``
        and ``checkin``. Posting a message (``say``/``dm``) does not,
        because an agent can talk without ever looking.

        Closes the open window, recording how long the agent's last
        declaration actually took to be followed by a look. A no-op when no
        declaration is outstanding — an unforecast look is not an
        observation of anything.
        """
        now = _now() if now is None else now
        prior = self._pending(agent_id, workspace)
        if prior is None:
            return
        self._record(agent_id, workspace, prior[0], prior[1], now - prior[2], now,
                      (prior[3], prior[4], prior[5]))
        self._clear_pending(agent_id, workspace)

    def declare(
        self, agent_id: str, workspace: str,
        execution_class: str | None, effort: str | None,
        now: float | None = None,
    ) -> Forecast | None:
        """Open a forecast window: "here is the work I am about to do, so
        here is when I expect to next come looking for messages."

        Returns None when nothing was declared. A new declaration replaces
        any outstanding one without recording an observation — the agent
        revised its estimate before the look happened, so the superseded
        prediction was never actually tested and must not count against
        calibration.
        """
        if not (execution_class or effort):
            return None
        now = _now() if now is None else now
        forecast = self.forecast(agent_id, workspace, execution_class, effort, now=now)
        self._set_pending(agent_id, workspace, execution_class, effort, now, forecast)
        return forecast

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

    # --- taxonomy ----------------------------------------------------------

    def top_classes(self, agent_id: str, workspace: str, k: int = TOP_K_CLASSES,
                    now: float | None = None) -> list[str]:
        """This agent's most-used execution classes, recency-weighted.

        Used only to *offer* the model a shortlist — a custom label is
        always accepted, so this never constrains the taxonomy, it just
        saves the model from reinventing a label it already uses. Each
        observation contributes ``0.5 ** (age / CLASS_HALF_LIFE_SECONDS)``,
        so classes the agent has moved on from decay out of the offer set
        instead of accumulating forever.

        Padded with DEFAULT_CLASSES so there is always something to offer,
        including on a completely cold start.
        """
        now = _now() if now is None else now
        weights: dict[str, float] = {}
        for cls, observed_at in self._connection().execute(
            "SELECT execution_class, observed_at FROM observations "
            "WHERE agent_id = ? AND workspace = ?", (agent_id, workspace),
        ):
            if cls == "unspecified":
                continue
            age = max(0.0, now - observed_at)
            weights[cls] = weights.get(cls, 0.0) + 0.5 ** (age / CLASS_HALF_LIFE_SECONDS)

        ranked = sorted(weights, key=lambda c: (-weights[c], c))[:k]
        for fallback in DEFAULT_CLASSES:
            if len(ranked) >= k:
                break
            if fallback not in ranked:
                ranked.append(fallback)
        return ranked

    # --- calibration -------------------------------------------------------

    def calibration(self, agent_id: str, workspace: str) -> dict[str, Any]:
        """How well this agent's past forecasts matched what actually
        happened. Local diagnostics only — never shared.

        A well-calibrated model puts roughly 50% of outcomes under its p50
        and roughly 95% under its p95. This reads the predictions stored
        with each observation, so it stays answerable retroactively.
        """
        rows = self._connection().execute(
            "SELECT delta_seconds, predicted_p50, predicted_p95 FROM observations "
            "WHERE agent_id = ? AND workspace = ? AND predicted_p50 IS NOT NULL",
            (agent_id, workspace),
        ).fetchall()
        if not rows:
            return {"samples": 0, "p50_hit_rate": None, "p95_hit_rate": None}
        return {
            "samples": len(rows),
            "p50_hit_rate": sum(d <= p50 for d, p50, _ in rows) / len(rows),
            "p95_hit_rate": sum(d <= p95 for d, _, p95 in rows) / len(rows),
        }
