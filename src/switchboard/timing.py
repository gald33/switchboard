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
import uuid
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

#: Sanity ceiling on a single observation. Originally 6h, chosen as a
#: crude proxy for "this gap is really a restart, not behaviour" — but
#: restarts are now detected exactly (see _RUNTIME_ID), so that job is
#: done properly elsewhere and the ceiling's only remaining effect was to
#: truncate agents that are genuinely slow. Truncating from above biases
#: the upper quantile low, which is the one direction that misleads, so
#: the ceiling is now set where a forecast stops meaning anything at all:
#: past MAX_MESSAGE_TTL the message being coordinated over has expired.
#: What it does drop is counted (see `dropped` table) rather than
#: discarded silently, so the remaining bias stays measurable.
MAX_OBSERVATION_SECONDS = 24 * 3600.0

#: Per-bucket retention. Observations are a moving window rather than a
#: permanent archive: an agent that got faster should not carry its old
#: slow samples forever, and the table should not grow without bound. The
#: execution-class shortlist already decays for the same reason; this is
#: the timing distribution's version of it, kept as a simple window
#: because a weighted-quantile estimator is easy to swap in later.
MAX_OBSERVATIONS_PER_BUCKET = 500

#: Below this many samples, a bucket is considered too sparse to trust and
#: the estimator falls back to a coarser one.
MIN_SAMPLES = 5

#: Weight of the bootstrap prior, in units of "equivalent observations".
#: An estimate from n samples is blended with the prior at n / (n + this),
#: so a bucket needs real evidence before it fully overrides the wide
#: default. Guards the upper quantile, whose small-sample error is
#: one-sided: an unsampled tail always reads as shorter than it is.
PRIOR_STRENGTH = 8.0

#: Observations required before the runtime will correct its own
#: quantiles. Below this the measured error is mostly noise and
#: "correcting" for it would inject more error than it removes.
MIN_RECALIBRATION_SAMPLES = 25

#: Bounds on the self-correction multiplier. A learned model that is off
#: by 4x should be corrected; one that appears off by 100x is far more
#: likely to be a bug or a regime change than a calibration problem, and
#: quietly applying that would turn a small fault into a useless forecast.
RECALIBRATION_BOUNDS = (0.25, 4.0)

#: Recency half-life for the timing estimator itself. The retention
#: window bounds growth with a hard cliff at 500; this weights within it,
#: so an agent that changed speed converges smoothly instead of waiting
#: for old samples to fall off the end.
TIMING_HALF_LIFE_SECONDS = 7 * 86400.0

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


#: Identifies this process. A declaration is only scored by a look from
#: the same run: if the agent died mid-task and came back, the elapsed
#: wall-clock time measures the outage, not the agent's behaviour, and a
#: restart soon after a declaration produces a plausible-looking number
#: that the outlier ceiling has no way to catch.
_RUNTIME_ID = uuid.uuid4().hex


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
    #: The estimate before self-correction, and the multipliers applied.
    #: Local diagnostics; never shared, never part of as_message_meta().
    raw_p50_seconds: float | None = None
    raw_p95_seconds: float | None = None
    correction: tuple[float, float] = (1.0, 1.0)

    def as_message_meta(self) -> dict[str, str]:
        """The sparse, shareable summary. No internals leak past this."""
        return {
            "p50": _iso(self.now + self.p50_seconds),
            "p95": _iso(self.now + self.p95_seconds),
        }


# --- the wire contract, shared by both surfaces -----------------------------
#
# `as_message_meta` above defines what travels; these define how it rides on a
# message and what the sender gets back. The MCP bridge and the CLI both need
# exactly this, and having grown their own copies is what let them drift:
# adaptive timing shipped bridge-only, and the CLI's later copy had to be
# checked against `Bridge._msg` by a test asserting two implementations agreed.
# One implementation is the point.
#
# Deliberately NOT folded here: how an elapsed forecast is presented. The
# bridge flags the whole forecast off p95, the CLI reports each checkpoint as
# "already due" as it passes. That is two considered answers to "what does a
# reader need", not one behaviour duplicated, so unifying it would be a
# behaviour change wearing a refactor's clothes.


def sender_forecast(forecast: Forecast) -> dict[str, Any]:
    """What the *sender* gets back about its own forecast.

    Everything that travels, plus relative seconds: the sender knows "now" is
    this exact moment, so a countdown is more use than two timestamps it would
    have to difference itself.
    """
    return {
        **forecast.as_message_meta(),
        "p50_in_seconds": round(forecast.p50_seconds),
        "p95_in_seconds": round(forecast.p95_seconds),
    }


def wrap_body(text: Any, forecast: Forecast | None) -> Any:
    """Fold a forecast into an outgoing message body.

    No forecast means the body goes out untouched — a bare string stays a bare
    string, so agents that ignore the feature see nothing new.
    """
    if forecast is None:
        return text
    return {"text": text, "timing_forecast": forecast.as_message_meta()}


def unwrap_body(body: Any) -> tuple[Any, dict[str, Any] | None]:
    """Inverse of `wrap_body`.

    The key check is deliberately conservative: an ordinary dict body that
    happens to carry a `text` key is a message, not an envelope, and must come
    back out unchanged.
    """
    if (isinstance(body, dict) and set(body.keys()) <= {"text", "timing_forecast"}
            and "timing_forecast" in body):
        return body.get("text"), body.get("timing_forecast")
    return body, None


def declare_safely(model: TimingModel | None, agent_id: str, workspace: str,
                   execution_class: str | None, effort: str | None) -> Forecast | None:
    """Open a forecast window, or give up quietly.

    The whole feature is advisory, so a missing or broken local store costs a
    hint and never the coordination call it was riding on.
    """
    if model is None:
        return None
    try:
        return model.declare(agent_id, workspace, execution_class, effort)
    except Exception:
        return None


def note_look_safely(model: TimingModel | None, agent_id: str, workspace: str) -> None:
    """Close the open window: the agent read its inbox, which is the one event
    every forecast predicts. Never raises, same reasoning as `declare_safely`.
    """
    if model is None:
        return
    try:
        model.note_look(agent_id, workspace)
    except Exception:
        pass


class TimingModel:
    """Local (never-shared) store of one agent's check-in timing history.

    Keyed by (agent_id, workspace) so a single machine can host multiple
    agent identities without their histories mixing. Nothing here is sent
    to the hub or to any other agent — see module docstring.
    """

    def __init__(self, db_path: str = "~/.switchboard/timing.db",
                 runtime_id: str | None = None) -> None:
        self.db_path = os.path.expanduser(db_path)
        self._conn: sqlite3.Connection | None = None
        #: Identifies the run that owns a pending declaration. Defaults to
        #: this process, which is right for a long-lived one (the MCP
        #: bridge): if it dies mid-task, the gap measures downtime rather
        #: than behaviour and must not be learned from.
        #:
        #: A caller whose "run" outlives the process must say so. The CLI is
        #: the case in point — one command declares, a later command looks,
        #: and they are always different processes — so process identity
        #: there would reject every observation and the model would never
        #: leave its bootstrap priors. See `cli._runtime_id`.
        self._runtime_id = runtime_id or _RUNTIME_ID

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
                    predicted_from  TEXT,
                    -- The estimate *before* self-correction. Kept so the
                    -- correction can be re-derived from scratch each time
                    -- rather than compounding on top of itself: measuring
                    -- error against an already-corrected number and then
                    -- correcting again is a feedback loop.
                    raw_p50         REAL,
                    raw_p95         REAL
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
                    raw_p50         REAL,
                    raw_p95         REAL,
                    runtime         TEXT,
                    PRIMARY KEY (agent_id, workspace)
                );

                -- Observations the ceiling rejected. Kept as counts so the
                -- truncation bias is visible instead of silent: a bucket
                -- with many drops has an upper quantile you should not
                -- trust, and that is worth being able to see.
                CREATE TABLE IF NOT EXISTS dropped (
                    agent_id        TEXT NOT NULL,
                    workspace       TEXT NOT NULL,
                    execution_class TEXT NOT NULL,
                    effort          TEXT NOT NULL,
                    count           INTEGER NOT NULL DEFAULT 0,
                    last_seconds    REAL,
                    PRIMARY KEY (agent_id, workspace, execution_class, effort)
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
        wanted = {
            "observations": (("predicted_p50", "REAL"), ("predicted_p95", "REAL"),
                              ("predicted_from", "TEXT"), ("raw_p50", "REAL"),
                              ("raw_p95", "REAL")),
            "pending": (("predicted_p50", "REAL"), ("predicted_p95", "REAL"),
                         ("predicted_from", "TEXT"), ("raw_p50", "REAL"),
                         ("raw_p95", "REAL"), ("runtime", "TEXT")),
        }
        for table, columns in wanted.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns:
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- observation -----------------------------------------------------

    def _record(self, agent_id: str, workspace: str, execution_class: str,
                effort: str, delta_seconds: float, now: float,
                predicted: tuple[float | None, float | None, str | None],
                raw: tuple[float | None, float | None] = (None, None)) -> None:
        conn = self._connection()
        if not (0 < delta_seconds <= MAX_OBSERVATION_SECONDS):
            conn.execute(
                "INSERT INTO dropped (agent_id, workspace, execution_class, effort, "
                "count, last_seconds) VALUES (?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(agent_id, workspace, execution_class, effort) DO UPDATE SET "
                "count = count + 1, last_seconds = excluded.last_seconds",
                (agent_id, workspace, execution_class, effort, delta_seconds),
            )
            conn.commit()
            return
        conn.execute(
            "INSERT INTO observations "
            "(agent_id, workspace, execution_class, effort, delta_seconds, observed_at, "
            " predicted_p50, predicted_p95, predicted_from, raw_p50, raw_p95) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_id, workspace, execution_class, effort, delta_seconds, now,
             *predicted, *raw),
        )
        # Keep the bucket to a moving window. Deleting by id keeps the most
        # recent rows, so the distribution tracks how this agent behaves now
        # rather than averaging over everything it has ever done.
        conn.execute(
            "DELETE FROM observations WHERE id IN ("
            "  SELECT id FROM observations"
            "  WHERE agent_id = ? AND workspace = ? AND execution_class = ? AND effort = ?"
            "  ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (agent_id, workspace, execution_class, effort, MAX_OBSERVATIONS_PER_BUCKET),
        )
        conn.commit()

    def _pending(self, agent_id: str, workspace: str) -> tuple | None:
        return self._connection().execute(
            "SELECT execution_class, effort, since, predicted_p50, predicted_p95, "
            "predicted_from, runtime, raw_p50, raw_p95 FROM pending "
            "WHERE agent_id = ? AND workspace = ?",
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
                "predicted_p50, predicted_p95, predicted_from, runtime, raw_p50, raw_p95) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, workspace, execution_class or "unspecified", effort or "medium", now,
                 forecast.p50_seconds if forecast else None,
                 forecast.p95_seconds if forecast else None,
                 forecast.source if forecast else None,
                 self._runtime_id,
                 forecast.raw_p50_seconds if forecast else None,
                 forecast.raw_p95_seconds if forecast else None),
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
        if prior[6] != self._runtime_id:
            # Left behind by a previous run: the agent crashed or the
            # session ended between declaring and looking. The gap measures
            # downtime, not behaviour, and a restart shortly after a
            # declaration yields a plausible-looking value the outlier
            # ceiling cannot catch. Drop it rather than learn from it.
            self._clear_pending(agent_id, workspace)
            return
        self._record(agent_id, workspace, prior[0], prior[1], now - prior[2], now,
                      (prior[3], prior[4], prior[5]), (prior[7], prior[8]))
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
            f"SELECT delta_seconds, observed_at FROM observations "
            f"WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _deltas(self, agent_id: str, workspace: str,
                execution_class: str | None, effort: str | None) -> list[float]:
        """Just the durations, without their timestamps."""
        return [v for v, _ in self._samples(
            agent_id, workspace, execution_class, effort)]

    @staticmethod
    def _weighted_quantile(pairs: list[tuple[float, float]], q: float,
                            now: float) -> float:
        """Recency-weighted quantile over (value, observed_at) pairs.

        Each observation carries weight ``0.5 ** (age / half-life)``, and
        the plotting position is the midpoint of its weight span — which
        reduces exactly to the unweighted ``(i - 0.5)/n`` when every weight
        is equal, so the small-sample correction from earlier still holds.

        The retention window already drops anything past 500 observations
        per bucket. That is a cliff: a sample matters fully until it
        vanishes. This makes the transition smooth, so an agent that got
        faster converges as its new behaviour accumulates rather than
        waiting for the old rows to fall off the end.
        """
        if not pairs:
            raise ValueError("empty sample")
        items = sorted(pairs)
        weights = [0.5 ** (max(0.0, now - at) / TIMING_HALF_LIFE_SECONDS)
                   for _, at in items]
        total = sum(weights)
        if total <= 0:
            return items[len(items) // 2][0]

        # Midpoint plotting positions, normalised.
        positions, running = [], 0.0
        for w in weights:
            positions.append((running + w / 2) / total)
            running += w

        if q <= positions[0]:
            return items[0][0]
        if q >= positions[-1]:
            return items[-1][0]
        for i in range(len(positions) - 1):
            lo, hi = positions[i], positions[i + 1]
            if lo <= q <= hi:
                span = 0.0 if hi == lo else (q - lo) / (hi - lo)
                return items[i][0] + span * (items[i + 1][0] - items[i][0])
        return items[-1][0]

    @staticmethod
    def _quantile(sorted_values: list[float], q: float) -> float:
        """Empirical quantile, interpolated between order statistics.

        Uses the (i - 0.5)/n plotting position rather than i/(n-1). The
        latter forces q=0.95 onto the sample *maximum* for any small n —
        with n=5 the "95th percentile" was literally the largest of five
        draws, which sits near the 83rd percentile of the true
        distribution. That made p95 optimistic exactly where a
        collaborator leans on it hardest.
        """
        if not sorted_values:
            raise ValueError("empty sample")
        n = len(sorted_values)
        position = q * n - 0.5
        if position <= 0:
            return sorted_values[0]
        if position >= n - 1:
            return sorted_values[-1]
        low = int(position)
        frac = position - low
        return sorted_values[low] + frac * (sorted_values[low + 1] - sorted_values[low])

    def _estimate(self, agent_id: str, workspace: str,
                   execution_class: str | None, effort: str | None,
                   source: str, effort_for_prior: str | None, now: float
                   ) -> tuple[float, float, str, int] | None:
        values = self._samples(agent_id, workspace, execution_class, effort)
        if len(values) < MIN_SAMPLES:
            return None
        n = len(values)
        p50 = self._weighted_quantile(values, 0.50, now)
        p95 = self._weighted_quantile(values, 0.95, now)

        # Shrink only the upper quantile toward the (deliberately wide)
        # bootstrap prior, with a weight that decays as evidence
        # accumulates. The small-sample error there is one-sided — you
        # cannot observe a tail you have not sampled yet, so the estimate
        # runs short — and the cost of the two directions is asymmetric:
        # an optimistic p95 misleads, a conservative one merely wastes a
        # little patience.
        #
        # The median gets no such treatment. Its small-sample error is
        # symmetric, so blending would not reduce bias, it would only drag
        # p50 toward whatever the generic prior happens to say — which for
        # an agent much faster than the default is simply wrong, and shows
        # up immediately as an over-conservative p50 in the cold regime.
        _, prior_p95 = _BOOTSTRAP_SECONDS.get(effort_for_prior or "", _DEFAULT_BOOTSTRAP)
        weight = n / (n + PRIOR_STRENGTH)
        p95 = weight * p95 + (1 - weight) * prior_p95
        return p50, max(p95, p50), source, n

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
            result = self._estimate(agent_id, workspace, cls, eff, source, effort, now)
            if result is not None:
                p50, p95, source, n = result
                return self._corrected(agent_id, workspace, p50, p95, now, source, n)

        p50, p95 = _BOOTSTRAP_SECONDS.get(effort or "", _DEFAULT_BOOTSTRAP)
        return self._corrected(agent_id, workspace, p50, p95, now, "bootstrap", 0)

    # --- self-correction ---------------------------------------------------

    def _corrected(self, agent_id: str, workspace: str, raw_p50: float,
                    raw_p95: float, now: float, source: str, n: int) -> Forecast:
        """Apply the agent's measured miscalibration to a raw estimate.

        Everything needed to know whether p95 really holds 95% of the time
        has been recorded since the forecast/outcome pair was first stored.
        Acting on it here rather than reporting it is the point: telling a
        model "your forecasts run short, compensate" hands back exactly the
        arithmetic this feature exists to absorb, and no collaborator has a
        channel to tell it either.
        """
        m50, m95 = self._correction(agent_id, workspace)
        p50, p95 = raw_p50 * m50, raw_p95 * m95
        return Forecast(
            p50_seconds=p50, p95_seconds=max(p95, p50), now=now,
            source=source, samples=n,
            raw_p50_seconds=raw_p50, raw_p95_seconds=raw_p95,
            correction=(m50, m95),
        )

    def _correction(self, agent_id: str, workspace: str) -> tuple[float, float]:
        """Multipliers that would have made past forecasts land correctly.

        Conformal in shape: for each past observation take the ratio of
        what happened to what was predicted, and read the quantile of those
        ratios at the level being targeted. If p95 should have been 1.4x
        larger to cover 95% of outcomes, that is the multiplier.

        Derived from the *raw* stored estimate, never the issued one. A
        correction measured against an already-corrected number and then
        applied again compounds every cycle; measuring against raw makes
        this a fresh calculation each time, with no memory to run away.
        """
        rows = self._connection().execute(
            "SELECT delta_seconds, raw_p50, raw_p95 FROM observations "
            "WHERE agent_id = ? AND workspace = ? AND raw_p50 IS NOT NULL "
            "AND raw_p50 > 0 AND raw_p95 > 0 "
            "ORDER BY id DESC LIMIT ?",
            (agent_id, workspace, MAX_OBSERVATIONS_PER_BUCKET),
        ).fetchall()
        if len(rows) < MIN_RECALIBRATION_SAMPLES:
            return 1.0, 1.0

        low, high = RECALIBRATION_BOUNDS
        ratios_50 = sorted(d / r50 for d, r50, _ in rows)
        ratios_95 = sorted(d / r95 for d, _, r95 in rows)
        return (
            min(high, max(low, self._quantile(ratios_50, 0.50))),
            min(high, max(low, self._quantile(ratios_95, 0.95))),
        )

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
        conn = self._connection()
        rows = conn.execute(
            "SELECT delta_seconds, predicted_p50, predicted_p95 FROM observations "
            "WHERE agent_id = ? AND workspace = ? AND predicted_p50 IS NOT NULL",
            (agent_id, workspace),
        ).fetchall()
        # Reported alongside the hit rates rather than buried, because
        # censored observations bias the upper quantile low: every one is a
        # gap longer than anything the estimator was allowed to see. A high
        # count means p95 is optimistic and the hit rate below overstates
        # how well calibrated this agent really is.
        dropped = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM dropped "
            "WHERE agent_id = ? AND workspace = ?", (agent_id, workspace),
        ).fetchone()[0]
        if not rows:
            return {"samples": 0, "p50_hit_rate": None, "p95_hit_rate": None,
                    "dropped_as_outliers": dropped}
        return {
            "samples": len(rows),
            "p50_hit_rate": sum(d <= p50 for d, p50, _ in rows) / len(rows),
            "p95_hit_rate": sum(d <= p95 for d, _, p95 in rows) / len(rows),
            "dropped_as_outliers": dropped,
        }

    def calibration_by(self, agent_id: str, workspace: str,
                        dimension: str) -> dict[str, dict[str, Any]]:
        """Calibration split along one dimension.

        An aggregate rate says *that* an agent is miscalibrated; it never
        says *where*. Those are different repairs: a single bad execution
        class is the model's to fix by labelling differently, while error
        spread evenly across every bucket is the estimator's problem. The
        rows to answer this have been stored since forecasts and outcomes
        were first recorded together — this only reads them back.

        `dimension` is 'execution_class', 'effort', or 'predicted_from'
        (which fallback tier produced the forecast).
        """
        if dimension not in {"execution_class", "effort", "predicted_from"}:
            raise ValueError(f"cannot break calibration down by {dimension!r}")
        rows = self._connection().execute(
            f"SELECT {dimension}, delta_seconds, predicted_p50, predicted_p95 "
            "FROM observations WHERE agent_id = ? AND workspace = ? "
            "AND predicted_p50 IS NOT NULL",
            (agent_id, workspace),
        ).fetchall()

        grouped: dict[str, list[tuple[float, float, float]]] = {}
        for key, delta, p50, p95 in rows:
            grouped.setdefault(key or "unspecified", []).append((delta, p50, p95))
        return {
            key: {
                "samples": len(group),
                "p50_hit_rate": sum(d <= a for d, a, _ in group) / len(group),
                "p95_hit_rate": sum(d <= b for d, _, b in group) / len(group),
            }
            for key, group in sorted(grouped.items())
        }
