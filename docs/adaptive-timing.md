# Adaptive timing forecasts

A local, learned primitive that lets an agent attach a cheap, advisory
check-in forecast to a message it is already sending — so collaborators can
decide for themselves whether to poll soon, wait, or ignore timing
entirely, without spending tokens negotiating it.

## Design principle

Standardize the known coordination arithmetic; leave judgment to the model.

This is not a scheduler. It never decides when an agent may act, a forecast
is never a commitment, and no agent is required to obey another's forecast.

## Two layers

1. **Model judgment (per send, optional).** The model supplies
   `execution_class` (a short free-form label, e.g. `"coding"` —
   deliberately no fixed taxonomy, it emerges from use) and `effort`
   (`low` / `medium` / `high`). It never estimates seconds.
2. **Local learned timing model (automatic).** `timing.TimingModel`
   converts that pair into a forecast using this agent's own history —
   raw history and model parameters never leave the process.

## Where it lives in the existing architecture

- `src/switchboard/timing.py` — new module, local SQLite (default
  `~/.switchboard/timing.db`, `SWITCHBOARD_TIMING_DB`/`ClientConfig.timing_db`
  to override). Sibling in scope to `notify.py`, but purely client-side —
  there is no server/hub involvement at all.
- `mcp_server.Bridge` owns a `TimingModel` instance alongside its `Client`.
  The `say`/`dm` tools gained two optional parameters, `execution_class`
  and `effort`; everything past that (recording the observation, looking up
  history, computing percentiles, attaching the result) is automatic —
  matching the existing shape of `Bridge.say`/`Bridge.dm` as the one place
  every outgoing message already passes through.
- The forecast rides inside the message `body` (`{"text": ...,
  "timing_forecast": {...}}`) rather than as a new column/field on the wire
  message model — `type`/`thread` are the only structured message metadata
  today and neither fits, while `body` is already arbitrary JSON with no
  server-side schema change required. `Bridge._msg` unwraps this back to a
  plain `body` + top-level `timing_forecast` for the receiver, so an agent
  that ignores the feature entirely still sees the message text unchanged.

## Observation model

An observation is the elapsed time between one *classified* send (a
`say`/`dm` call that included `execution_class`/`effort`) and this agent's
*next* activity of any kind (classified or not) in the same
`(agent_id, workspace)`. `TimingModel` tracks the open "pending" window in
its own `pending` table and closes it — recording a `delta_seconds` row
against the *prior* classification — on the next send. Deltas above
`MAX_OBSERVATION_SECONDS` (6h) are dropped as restarts/overnight gaps
rather than genuine work time; this is the one outlier heuristic v1 needs,
and the schema (one row per observation, with class/effort/timestamp) is
intentionally raw enough to layer richer detection (active vs. waiting vs.
external-call time, interruption tagging, calibration audits) on top later
without a migration.

## Fallback hierarchy

```
(execution_class, effort) -> execution_class -> effort -> agent-wide -> bootstrap
```

Each tier needs `MIN_SAMPLES` (5) observations before it's trusted; the
first tier with enough data wins. Bootstrap priors are three fixed
`(p50, p95)` pairs keyed by effort (`low`/`medium`/`high`), wide on purpose
so an honest-but-vague first forecast beats a falsely precise one.

## Wire format

Deliberately sparse — two ISO-8601 timestamps, nothing else:

```json
{"p50": "2026-08-07T15:08:24+00:00", "p95": "2026-08-07T15:09:41+00:00"}
```

No percentile in between is published; a receiver that wants roughly p70
can interpolate between p50 and p95 itself. No raw durations, sample
counts, execution class, or effort are exposed — those stay local
(`Forecast` keeps them for local diagnostics; `as_message_meta()` is the
only thing that crosses the process boundary).

## Graceful degradation

- No classification on a send → no forecast attached, message behaves
  exactly as before (this is the default/existing path, untouched).
- No history yet → bootstrap prior, still produces a usable forecast.
- `TimingModel` I/O failure (permissions, disk issue, corrupt file) is
  caught around the one call site in `Bridge._body_with_forecast` and
  treated as "no forecast" — it can never block a send.
- A missed, stale, or wrong forecast has no protocol consequence; nothing
  reads it back to enforce anything.

## Tests

- `tests/test_timing.py` — the estimator in isolation: bootstrap values,
  effort ordering, observation recording/closing, fallback tiers, outlier
  dropping, per-(agent, workspace) isolation, sparse wire format.
- `tests/test_mcp.py` — `say`/`dm` end to end through the bridge: no hint →
  no forecast; hint → bootstrap forecast attached and correctly unwrapped
  on the receiving side; enough local history → the specific bucket is
  used instead of the bootstrap prior.

## Out of scope (by design)

Turn-taking, claims, leases, yielding, retries, ownership, a shared/global
timing model, and any mechanism that would let a forecast block or gate
another agent's action.
