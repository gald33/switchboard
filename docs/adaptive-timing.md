# Adaptive timing forecasts

A local, learned primitive that lets an agent attach a cheap, advisory
check-in forecast to a message it is already sending — so collaborators can
decide for themselves whether to poll soon, wait, or ignore timing
entirely, without spending tokens negotiating it.

## What is being predicted

**When this agent will next come and look for messages.** Not when its
task will finish, and not when it will next post. The question a
collaborator actually has is *"if I leave this here, when will they see
it?"*, and that is answered by the agent's next read of its inbox.

This distinction drives the whole observation model below: an agent can
talk endlessly without ever looking, so posting is not evidence about when
it will notice anything. The semantic classification describes the work
ahead only insofar as that work determines how long the agent stays
heads-down before looking up.

## Design principle

Standardize the known coordination arithmetic; leave judgment to the model.

This is not a scheduler. It never decides when an agent may act, a forecast
is never a commitment, and no agent is required to obey another's forecast.

## Two layers

1. **Model judgment (optional, on any of say/dm/checkin/inbox).** The model
   supplies `execution_class` (a short free-form label, e.g. `"coding"`)
   and `effort` (`low` / `medium` / `high`) for the stretch of work ahead.
   It never estimates seconds.
2. **Local learned timing model (automatic).** `timing.TimingModel`
   converts that pair into a forecast using this agent's own history —
   raw history and model parameters never leave the process.

## Where it lives in the existing architecture

- `src/switchboard/timing.py` — new module, local SQLite (default
  `~/.switchboard/timing.db`, `SWITCHBOARD_TIMING_DB`/`ClientConfig.timing_db`
  to override). Sibling in scope to `notify.py`, but purely client-side —
  there is no server/hub involvement at all.
- `mcp_server.Bridge` owns a `TimingModel` instance alongside its `Client`.
  `say`, `dm`, `checkin` and `inbox` gained two optional parameters,
  `execution_class` and `effort`; everything past that (closing the
  previous window, looking up history, computing percentiles, attaching the
  result) is automatic. `Bridge._declare` and `Bridge._note_look` are the
  only two call sites, and both swallow errors — see degradation below.
- The forecast rides inside the message `body` (`{"text": ...,
  "timing_forecast": {...}}`) rather than as a new column/field on the wire
  message model — `type`/`thread` are the only structured message metadata
  today and neither fits, while `body` is already arbitrary JSON with no
  server-side schema change required. `Bridge._msg` unwraps this back to a
  plain `body` + top-level `timing_forecast` for the receiver, so an agent
  that ignores the feature entirely still sees the message text unchanged.

## Observation model

An observation is the elapsed time from **declaring** to **looking**:

- **Declare** (`TimingModel.declare`) opens a window — "here is the work
  I'm about to do, so here is when I expect to next check messages."
  Available on `say`, `dm`, `checkin`, and `inbox`.
- **Look** (`TimingModel.note_look`) closes it, scoring that declaration.
  Only the tools that actually *read the inbox* count: `checkin` (which
  long-polls it) and `inbox` (including `peek=true` — the agent saw the
  messages either way).

`say`/`dm` deliberately never close a window. `_touch()`, which they call,
explicitly does not drain the inbox — it only returns an unread count — so
posting is not a look, and treating it as one would train the model on the
wrong event entirely.

Two consequences worth stating:

- An agent that posts three updates without reading records **no**
  observations, and its outstanding forecast stays open. That is correct:
  it still hasn't looked.
- Re-declaring replaces the open window **without** recording an
  observation. The agent revised its estimate before the look happened, so
  the superseded prediction was never tested and must not count against
  calibration.

`checkin` and `inbox` both accept the optional pair so an agent looping on
either can re-declare as its sense of the work changes, getting a fresh
forecast back each time.

`TimingModel` tracks the open window in its own `pending` table. The
schema is intentionally raw enough to layer richer detection (active vs.
waiting vs. external-call time, interruption tagging) on top later.

### The outlier ceiling, and why it moved

`MAX_OBSERVATION_SECONDS` was originally 6h, chosen as a crude proxy for
"this gap is really a restart, not behaviour". Runtime identity now
detects restarts *exactly*, so that job is done properly elsewhere — which
left the ceiling doing nothing but truncating agents that are genuinely
slow. Truncation from above biases the upper quantile low, and low is the
one direction that misleads a collaborator.

It is now 24h: the point past which a forecast is meaningless anyway,
since the message being coordinated over has already expired
(`MAX_MESSAGE_TTL`). What it still rejects is *counted* per bucket in the
`dropped` table and surfaced by `calibration()` as `dropped_as_outliers`,
because every censored observation is a gap longer than anything the
estimator was allowed to see. A high count means p95 is optimistic and the
reported hit rate overstates how calibrated that agent really is — a bias
worth being able to see rather than one silently absorbed.

### Retention

Each `(class, effort)` bucket keeps its most recent
`MAX_OBSERVATIONS_PER_BUCKET` (500) observations. Timing history is a
moving window, not an archive: an agent that got faster should not be held
back by samples from when it was slow, and the table should not grow
without bound. The execution-class shortlist already decays for the same
reason — this is the timing distribution's version, kept as a simple
window because a weighted-quantile estimator is straightforward to swap in
later.

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

### Small samples and the upper tail

Two corrections keep p95 honest when a bucket has barely any history.

Quantiles use the `(i - 0.5)/n` plotting position with interpolation. The
obvious `i/(n-1)` form pins `q=0.95` to the sample *maximum* for any small
n — at `MIN_SAMPLES` the "95th percentile" was literally the largest of
five draws, which sits nearer the true 83rd. Measured on the Tier 1
harness, that made the early-history p95 hit rate 0.86 against a 0.95
target.

On top of that, p95 is shrunk toward the bootstrap prior with weight
`n / (n + PRIOR_STRENGTH)`. A tail you have not sampled yet always reads
as shorter than it is, and the two error directions cost differently: an
optimistic p95 misleads a collaborator, a conservative one wastes a little
patience.

The median is deliberately left alone. Its small-sample error is
symmetric, so shrinking it would not remove bias — it would just drag p50
toward whatever the generic prior says, which for an agent much faster
than the default is simply wrong. An earlier version blended both and the
regime-bucketed gate immediately caught it as an over-conservative p50
(0.78 against a 0.50 target) in the cold regime.

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
  caught at every call site — `Bridge._declare`, `Bridge._note_look` and
  `Bridge.tools()` — and treated as "no forecast" / "static tool list". It
  can never block a send, a read, or `tools/list`.
- A window left open by an agent that never looks again simply never
  becomes an observation, and the 6h outlier ceiling stops a stale one
  polluting the distribution if that agent does eventually return.
- A declaration is only ever scored by a look from the *same process*
  (`_RUNTIME_ID`). If an agent dies mid-task and restarts, the elapsed
  wall-clock measures downtime rather than behaviour — and a restart soon
  after a declaration produces a plausible-looking number that the outlier
  ceiling has no way to catch, so it is dropped instead of learned from.
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
  reading the inbox is what closes a window; posting repeatedly without
  reading records nothing; `checkin` closes and re-declares; `inbox` can
  declare too; `tools/list` offers the agent's own top classes without
  becoming an enum; and a deliberately broken timing store still sends.

## Closing the loop for the sender

Two things the runtime computes so the model does not have to.

**Expired forecasts are flagged.** When a message is read and its `p95` has
already passed, the forecast is annotated `expired: true`. Comparing two
timestamps is exactly the arithmetic this feature exists to keep out of
model reasoning, and the answer changes what the forecast is worth: past
p95 the predicted event has almost certainly happened, so the checkpoints
carry no remaining information and must not be used to defer a check. The
fields are annotated rather than stripped, since a reader may still want
to see what was predicted.

**An agent can see its own calibration.** `whoami` reports
`forecast_calibration` — sample count, p50/p95 hit rates, and a plain
warning when they are badly off — once there is enough history to mean
anything. Until this, the data was dark: an agent could publish
misleading forecasts indefinitely with no way to discover it, and
collaborators have no channel to tell it. The report also surfaces
`ignored_as_too_long`, because censored observations mean p95 is
optimistic by an unknown margin and the hit rate beside it flatters
itself. All local; none of it is shared.

## Advising the receiver

The protocol does not prescribe what a receiving agent does with a
forecast, and it should not — that is model judgment. But Tier 1 measured
that *how* it is used swings the value far more than the forecast's own
accuracy does: reading p50/p95 as two individual poll times was worse than
a plain fixed interval (−6%), while using them to size a checking cadence
was substantially better (+85%) in the same world.

So `inbox` and `checkin` carry one sentence of advice (`_FORECAST_ADVICE`)
where messages actually arrive: what the fields mean, that they are
predictions rather than promises, that a cadence beats two point-polls, and
that a forecast whose p95 has passed carries no information. It is a hint,
not a rule — an agent remains free to ignore it.

Tier 2 then tested that with real models, and it turned out to be
load-bearing rather than decorative. Forecast-plus-advisory beat no
forecast for every model tested, strictly on both axes. But the *raw*
forecast alone did not reliably help: of three models, one improved, one
got worse on lateness, and one spent 55% more checks. Handing a model
p50/p95 with no guidance is not dependably an improvement — which is the
whole reason the sentence stays. See
`tests/experiments/tier2_pilot/RESULTS.md`, including what that pilot is
too small to establish.

## Out of scope (by design)

Turn-taking, claims, leases, yielding, retries, ownership, a shared/global
timing model, and any mechanism that would let a forecast block or gate
another agent's action.
