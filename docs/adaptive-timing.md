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

1. **Model judgment (per send or check-in, optional).** The model supplies
   `execution_class` (a short free-form label, e.g. `"coding"`) and
   `effort` (`low` / `medium` / `high`). It never estimates seconds.
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

An observation is the elapsed time from declaring a classification to the
next time the agent **surfaces** — a `say`, a `dm`, or a `checkin`. Those
three are what a collaborator would recognise as a check-in, and all three
both close the open window and may declare a new one. An `inbox` poll
deliberately does *not* close the window: the agent looking is not the
agent reporting, and letting a poll consume the window would bias the
distribution short.

`checkin` closing the window is what keeps the forecast honest about its
own claim. An agent that heartbeats through a long task produces several
short observations rather than one giant message-to-message gap, so the
learned curve tracks how often it actually surfaces — and it can re-declare
`(execution_class, effort)` at each check-in as its sense of the work
changes, getting a fresh forecast back each time.

`TimingModel` tracks the open window in its own `pending` table. Deltas
above `MAX_OBSERVATION_SECONDS` (6h) are dropped as restarts/overnight
gaps rather than genuine work time; this is the one outlier heuristic v1
needs, and the schema is intentionally raw enough to layer richer
detection (active vs. waiting vs. external-call time, interruption
tagging) on top later.

### Calibration data

Each observation also stores the forecast that was issued when its window
opened — `predicted_p50`, `predicted_p95`, `predicted_from` (which
fallback tier produced it). Without that, "did ~95% of outcomes land under
p95?" is unanswerable after the fact, since the prediction is gone by the
time the outcome arrives. `TimingModel.calibration()` reads it back as hit
rates; the same rows support breakdowns by class, effort, tier, and drift
over time later. Rows written before these columns existed are simply
excluded from calibration rather than migrated.

## Execution-class taxonomy

The taxonomy is never fixed and never centrally curated. `execution_class`
is an open string — any label is accepted, always — so a model can coin
one that fits work nobody anticipated.

What the runtime does supply is a *shortlist*, so the model isn't
reinventing a label it already uses. `tools/list` fills the
`execution_class` description with this agent's top `TOP_K_CLASSES` (6)
labels, ranked by recency-weighted use: each past observation contributes
`0.5 ** (age / CLASS_HALF_LIFE_SECONDS)`, a 14-day half-life. So classes
the agent has moved on from decay out of the offer rather than
accumulating forever, and a new label that catches on climbs in on its
own. A cold start pads with `DEFAULT_CLASSES` — four seeds, deliberately
too few to be a taxonomy, just enough that the first offer isn't empty.

Two properties this preserves on purpose: the field never becomes an
`enum` (that would make the shortlist a constraint rather than a
convenience), and the ranking is computed from the agent's *own* local
observations, so no agent's vocabulary is imposed on another.

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

### The "now" anchor

An absolute timestamp is only useful if the reader also has a trustworthy
"now" to compare it against — a model cannot be assumed to know its own
wall-clock time. Neither the sender nor the receiver gets one for free, so
every tool response that carries a forecast or a message also carries a
top-level `now` (current UTC time, `mcp_server._now_iso()`): `say`, `dm`,
`inbox`, `history`, `checkin`. This makes both sides of the exchange able
to interpret `timing_forecast` without guessing at the clock — the
receiver diffs `now` against the message's forecast, and the sender diffs
`now` against its own.

The sender also gets a same-call convenience the wire format deliberately
omits: alongside the two absolute checkpoints, its own tool response
includes `p50_in_seconds`/`p95_in_seconds` — a ready-to-use countdown, since
it already knows "now" was the instant it sent the message and would
otherwise just re-derive this by subtracting `now` from `p50`/`p95`
itself. This stays local to the response; only `{p50, p95}` ever rides in
the message body other agents read.

## Graceful degradation

- No classification on a send → no forecast attached, message behaves
  exactly as before (this is the default/existing path, untouched).
- No history yet → bootstrap prior, still produces a usable forecast.
- `TimingModel` I/O failure (permissions, disk issue, corrupt file) is
  caught at every call site — `Bridge._body_with_forecast`, `checkin`, and
  `Bridge.tools()` — and treated as "no forecast" / "static tool list". It
  can never block a send or fail `tools/list`.
- A missed, stale, or wrong forecast has no protocol consequence; nothing
  reads it back to enforce anything.
- An older timing database is migrated additively (nullable columns only),
  so an existing file keeps working and its rows are simply excluded from
  calibration.

## Tests

- `tests/test_timing.py` — the estimator in isolation: bootstrap values,
  effort ordering, observation recording/closing, fallback tiers, outlier
  dropping, per-(agent, workspace) isolation, sparse wire format,
  prediction storage, calibration hit rates, and class ranking/decay/cold
  start/custom labels.
- `tests/test_mcp.py` — end to end through the bridge: no hint → no
  forecast; hint → bootstrap forecast attached and correctly unwrapped on
  the receiving side; enough local history → the specific bucket is used;
  `checkin` closes the window with and without a new declaration;
  `tools/list` offers the agent's own top classes without becoming an
  enum; and a deliberately broken timing store still sends the message.

## Out of scope (by design)

Turn-taking, claims, leases, yielding, retries, ownership, a shared/global
timing model, and any mechanism that would let a forecast block or gate
another agent's action.
