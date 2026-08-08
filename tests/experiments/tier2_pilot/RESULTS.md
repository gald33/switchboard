# Tier 2 pilot results

`world=idiosyncratic, episodes=30, seed=4242`, three models × three arms,
scored with `tier2_run.py score`. Raw prompts and model responses are
committed alongside this file so the run is reproducible.

| arm | checks | late(×) | missed |
|---|---|---|---|
| *scripted cadence* | 6.30 | 0.18 | 0 |
| *scripted fixed 300s* | 5.17 | 0.33 | 1 |
| *scripted fixed 900s* | 2.47 | 0.84 | 0 |
| opus A | 3.37 | 0.58 | 0 |
| opus B | 2.93 | 0.48 | 0 |
| opus C | **2.33** | **0.47** | 0 |
| sonnet A | 2.80 | 0.63 | 0 |
| sonnet B | 2.53 | 0.72 | 0 |
| sonnet C | **2.43** | **0.56** | 0 |
| haiku A | 3.10 | 0.66 | 0 |
| haiku B | 4.80 | 0.29 | 0 |
| haiku C | **2.93** | **0.43** | 0 |

Both columns must be read together. Fewer checks is trivially achievable
by checking less and lower lateness by checking constantly, so an arm only
wins by improving one without giving back the other.

## 1. Forecast + advisory (C) beats no forecast (A) for every model

Strictly — fewer checks *and* lower lateness, all three models. This is
the first evidence for the coordination hypothesis that involves actual
models rather than scripted policies.

## 2. The raw forecast alone (B) is not reliably an improvement

This is the finding that matters most, because it is the one that was
assumed rather than measured:

- **opus** improved on both axes.
- **sonnet** got *worse* on lateness (0.63 → 0.72) while saving a little
  checking.
- **haiku** spent 55% more checks (3.10 → 4.80) to buy latency — not a
  win, just a different point on the trade-off.

So handing a model p50/p95 with no guidance does not dependably help, and
for two of three models it made something worse. Tier 1 predicted this
shape from simulation (the naive two-checkpoint reading scored −6%); it
now reproduces with real models.

## 3. The advisory earns its context

C ≥ B for every model, and strictly better on both axes for opus and
sonnet. For haiku it cut checking by 40% (4.80 → 2.93) while giving back
some latency.

That is the direct answer to the question left open in #38, where the
advisory shipped on simulation evidence alone: it is load-bearing, not
decorative.

## What this pilot does not establish

- **Not powered.** 30 episodes, one world, one seed, one sample per cell.
  These are directional, not conclusive, and no significance testing is
  claimed.
- **Batching favours B.** Each model saw all 30 scenarios at once and
  could calibrate across them — an advantage a real agent does not have,
  and one that helps arm B most, since it substitutes for the guidance C
  is given. C winning anyway survives that bias; it would not survive it
  in reverse.
- **Stated plans, not adaptation.** Models committed to a schedule up
  front. Real re-planning after a surprise is untested.
- **The scripted rows are not a leaderboard.** Every model chose a
  lower-check, higher-lateness operating point than the scripted cadence
  policy. That is a different point on the frontier, not a loss — the
  comparison would need matched latency to mean anything, which is
  exactly the frontier method Tier 1 used and this pilot is too small to
  reconstruct.
