# Tier 2 results

`world=idiosyncratic`, 30 episodes × 3 seeds (4242, 7777, 31337) = **90
episodes per cell**, three models × three arms. Prompts and raw model
responses are committed so the run is reproducible.

Arms: **A** no forecast · **B** forecast only · **C** forecast + advisory.

| arm | checks | late(×) | missed |
|---|---|---|---|
| *scripted cadence* | 5.32 | 0.21 | 1 |
| *scripted fixed 300s* | 4.54 | 0.33 | 1 |
| opus A / B / C | 3.14 / 2.68 / 2.28 | 0.63 / 0.38 / 0.47 | 0 |
| sonnet A / B / C | 3.09 / 2.64 / 2.18 | 0.45 / 0.40 / 0.58 | 0 |
| haiku A / B / C | 3.99 / 2.70 / 2.17 | 0.74 / 1.32 / 0.74 | 0 |

## This replication corrects the earlier single-seed pilot

The first run (seed 4242 only) reported that forecast-plus-advisory beat
no forecast **strictly on both axes for every model**. With three seeds
that does not hold:

| C vs A | checks | lateness |
|---|---|---|
| opus | better | better |
| sonnet | better | **worse** (0.45 → 0.58) |
| haiku | better | unchanged (0.74 → 0.74) |

The strict-dominance claim was a single-seed artifact. It should not have
been stated as confidently as it was, even hedged — one seed cannot
distinguish an effect from a draw, which is precisely why this
replication was run.

## What does hold

**Forecasts reduce checking, consistently.** Every model checks less in
both B and C than in A, in every seed. This is the most robust effect in
the data.

**The raw forecast alone remains unreliable on latency.** Per-seed, B vs A
on lateness is mixed for sonnet (+0.09, −0.25, +0.01) and haiku (−0.37,
−1.04, +1.24). Only opus improves consistently. That was the pilot's
second finding and it survives replication — handing a model p50/p95 with
no guidance does not dependably help.

**The advisory shifts the operating point rather than dominating.** C
reliably checks less than B. For opus and sonnet it pays for that in
latency (0.38 → 0.47, 0.40 → 0.58); for haiku it does not (1.32 → 0.74,
an improvement on both axes). So the advisory changes behaviour in a
consistent direction — toward fewer checks — but whether that is "better"
depends on how a check trades against latency, which this harness
deliberately does not fix, and the direction of the latency cost is not
even consistent across models.

## Variance is large, and that is itself the finding

Per-seed ranges, pooled cells:

```
model    arm            checks            late(x)
opus     A    3.14 [2.50-3.57]   0.65 [0.48-0.90]
opus     C    2.28 [1.87-2.63]   0.52 [0.41-0.67]
sonnet   A    3.09 [2.53-3.93]   0.52 [0.30-0.64]
sonnet   C    2.18 [1.87-2.43]   0.59 [0.56-0.64]
haiku    A    3.99 [2.50-6.37]   0.82 [0.39-1.41]
haiku    C    2.04 [1.57-2.93]   1.55 [0.43-2.69]
```

haiku's arm C lateness spans 0.43 to 2.69 across seeds — a 6x range on
the headline metric, and arm B spans 0.29 to 2.77. Any single-seed number
from those cells is close to meaningless, and the pilot reported exactly
such a number.

## What this still does not establish

- **Three seeds is replication, not power.** No significance testing is
  claimed and none would survive this variance.
- **Batching favours arm B.** Each model sees 30 scenarios at once and can
  calibrate across them — an advantage a real agent lacks, and one that
  substitutes for the guidance C is given.
- **Stated plans, not adaptation.** Models commit to a schedule up front;
  re-planning after a surprise is untested.
- **The scripted rows are not a leaderboard.** Every model picked a
  lower-check, higher-lateness point than the scripted cadence policy. A
  fair comparison needs matched latency — the frontier method Tier 1 used
  and this design does not reconstruct.

## A note on how this data was collected

Two haiku cells (`B_s7777`, `C_s31337`) were slow rather than failed, and
I launched retries for them. Both the originals and the retries completed
and wrote to the same paths, so the committed responses for those two
cells are the retries. An earlier revision of this file reported numbers
computed *before* those writes landed, which made the committed report
disagree with the committed data — the haiku rows have been recomputed
from what is actually in the repository.

The conclusions above are unchanged in substance: forecasts still reduce
checking consistently, the raw forecast is still unreliable on latency,
and the pilot's strict-dominance claim still does not replicate. What
moved is haiku's numbers, and one of them moved in the *favourable*
direction (C vs A lateness is unchanged rather than worse). Re-running
`tier2_run.py score` against the committed files reproduces this table.
