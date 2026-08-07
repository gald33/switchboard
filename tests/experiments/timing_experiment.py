"""Tier 1 experiment: is a published timing forecast worth anything?

Not collected by pytest (no ``test_`` prefix) — this is a seeded, offline
experiment, not a gate. Run it directly::

    uv run python tests/experiments/timing_experiment.py

The question
------------
An agent W declares what it is about to do and publishes a forecast of
when it will next look at its inbox. A collaborator C wants to notice W's
reply promptly without burning polls. Does the forecast let C place its
polls better than the best *tuned* fixed interval?

Why a frontier and not a number
-------------------------------
Fewer polls is trivially achievable by polling less, and lower latency by
polling constantly. Any single metric is gameable, so each policy is swept
over its tuning knob to trace a (polls, latency) curve, and the curves are
compared. The claim under test is Pareto dominance: at equal latency,
fewer polls.

Three arms, because a plain A/B cannot tell you whether the *learning*
earned its complexity — on a cold start a forecast is just a constant:

    A  fixed interval, no forecast          (control)
    B  forecast, frozen bootstrap prior     (does any hint help?)
    C  forecast, learned local history      (does learning beat a constant?)

B vs C is the arm that judges the estimator. If B and C land on the same
curve, the statistics engine is delivering a constant and should be
deleted.

Two things this deliberately does *not* rig
-------------------------------------------
* The latent world is a heavy-tailed mixture (usually quick, occasionally
  very slow), which is nothing like the smooth family an estimator might
  assume — and is realistic for agent work.
* The control is swept and compared at its *best*, not at a strawman
  interval.

What this cannot tell you: whether a language model actually makes good
use of the metadata. That needs a Tier 2, LLM-in-the-loop run. This tier
exists to falsify cheaply first.
"""

from __future__ import annotations

import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from switchboard.timing import _BOOTSTRAP_SECONDS, TimingModel  # noqa: E402

# --- the latent world -------------------------------------------------------
# Ground truth W does not know and never reports: how long it actually goes
# between looking at its inbox. Each profile is a two-component lognormal
# mixture — a fast mode it hits most of the time, and a heavy tail.
# (weight, median_seconds, sigma)
# Two worlds, because they answer different questions and only running one
# would be misleading.
#
# ORDERLY: effort alone predicts the scale well, and this agent is
# uniformly ~4x faster than the generic bootstrap prior. A per-episode
# cadence tuned by one global factor can absorb a *uniform* error like
# that for free, so learning has little left to add here.
WORLD_ORDERLY: dict[tuple[str, str], list[tuple[float, float, float]]] = {
    ("coding", "low"): [(0.85, 8.0, 0.5), (0.15, 90.0, 0.8)],
    ("coding", "medium"): [(0.80, 25.0, 0.6), (0.20, 300.0, 0.9)],
    ("research", "high"): [(0.70, 150.0, 0.7), (0.30, 1200.0, 1.0)],
    ("waiting", "low"): [(0.95, 5.0, 0.4), (0.05, 60.0, 0.7)],
}

# IDIOSYNCRATIC: the scenario the feature is actually *for*. Effort is the
# model's rough guess, but this particular agent's real timings depend on
# the class too and deviate from the generic prior in different directions
# — fast at coding, glacial at review, at the same declared effort. No
# single global factor can fix a prior that is wrong non-uniformly, so
# this is where a per-(class, effort) learned model should earn its keep.
WORLD_IDIOSYNCRATIC: dict[tuple[str, str], list[tuple[float, float, float]]] = {
    ("coding", "medium"): [(0.80, 20.0, 0.6), (0.20, 200.0, 0.9)],
    ("review", "medium"): [(0.75, 420.0, 0.7), (0.25, 2500.0, 0.9)],
    ("research", "high"): [(0.70, 130.0, 0.7), (0.30, 900.0, 1.0)],
    ("waiting", "high"): [(0.90, 2400.0, 0.5), (0.10, 9000.0, 0.8)],
}


def sample_gap(profiles: dict, profile: tuple[str, str],
               rng: random.Random) -> float:
    """Draw W's true time-until-next-look."""
    roll = rng.random()
    cumulative = 0.0
    for weight, median, sigma in profiles[profile]:
        cumulative += weight
        if roll <= cumulative:
            return median * math.exp(sigma * rng.gauss(0.0, 1.0))
    weight, median, sigma = profiles[profile][-1]
    return median * math.exp(sigma * rng.gauss(0.0, 1.0))


# --- polling policies -------------------------------------------------------
# Each returns (polls_spent, latency) for one episode: how many times C had
# to look before it caught the reply, and how stale the reply was when it
# finally did.


def poll_fixed(gap: float, interval: float) -> tuple[int, float]:
    """C knows nothing; it polls on a metronome."""
    polls = max(1, math.ceil(gap / interval))
    return polls, polls * interval - gap


def poll_forecast(gap: float, p50: float, p95: float,
                  tail: float) -> tuple[int, float]:
    """C spends its first two polls on the published checkpoints, then
    falls back to a metronome if the forecast turned out to be wrong.

    This is *a* reasonable reading of a p50/p95 pair, not the only one —
    every result below is conditional on this policy.
    """
    if gap <= p50:
        return 1, p50 - gap
    if gap <= p95:
        return 2, p95 - gap
    extra = math.ceil((gap - p95) / tail)
    return 2 + extra, p95 + extra * tail - gap


def poll_scaled(gap: float, p50: float, factor: float) -> tuple[int, float]:
    """C uses the forecast to set its *cadence* rather than to place two
    individual polls: poll every ``p50 * factor`` seconds.

    This is the steelman of the treatment, and the fairer test. The control
    must pick one interval to cover every profile at once — from a 5-second
    'waiting' to a 20-minute research task — whereas a forecast tells C the
    scale of *this* episode. That per-episode adaptation, not the two
    checkpoints, is where a win should come from if there is one.
    """
    interval = max(1e-6, p50 * factor)
    polls = max(1, math.ceil(gap / interval))
    return polls, polls * interval - gap


# --- arms -------------------------------------------------------------------


class Arm:
    """One experimental condition. Owns W's forecasting behaviour."""

    def __init__(self, name: str, learns: bool) -> None:
        self.name = name
        self.learns = learns
        self.model = TimingModel(":memory:")
        self.agent, self.workspace = "w", "sim"

    def forecast(self, profile: tuple[str, str], now: float) -> tuple[float, float]:
        execution_class, effort = profile
        if self.learns:
            f = self.model.declare(self.agent, self.workspace, execution_class,
                                    effort, now=now)
            return f.p50_seconds, f.p95_seconds
        # Frozen prior: the same constant forever, whatever the history says.
        return _BOOTSTRAP_SECONDS[effort]

    def observe(self, now: float) -> None:
        if self.learns:
            self.model.note_look(self.agent, self.workspace, now=now)


def episodes(arm: Arm, world: dict, n: int, rng: random.Random,
             clock: float = 1_000_000.0) -> list[tuple[tuple[float, float], float]]:
    """Play n episodes, returning [((p50, p95), true_gap), ...].

    The model keeps learning as it goes, which is what would really happen;
    the forecast used for each episode is the one available at that moment.
    """
    out = []
    profiles = list(world)
    for _ in range(n):
        profile = profiles[rng.randrange(len(profiles))]
        prediction = arm.forecast(profile, clock)
        gap = sample_gap(world, profile, rng)
        arm.observe(clock + gap)
        out.append((prediction, gap))
        clock += gap + 1.0
    return out


# --- sweeps -----------------------------------------------------------------


def summarise(results: list[tuple[int, float]], gaps: list[float],
              knob: float) -> tuple[float, float, float]:
    """(latency, polls, knob), where latency is *relative* to the gap.

    Scale-free on purpose. Averaging absolute latency across profiles that
    span two orders of magnitude lets a coarse metronome hide being 900s
    late on a 20s task behind the slow tasks' average — it looks cheap
    because ceil(gap/interval) is 1 poll, and the lateness disappears into
    the mean. Relative lateness ("how many task-lengths late was I?")
    prices that properly and puts every profile on the same footing.
    """
    return (
        statistics.mean(latency / gap for (_, latency), gap in zip(results, gaps, strict=True)),
        statistics.mean(polls for polls, _ in results),
        knob,
    )


def frontier_fixed(gaps: list[float], intervals: list[float]
                   ) -> list[tuple[float, float, float]]:
    """(relative latency, polls, interval) for the control."""
    return sorted(
        summarise([poll_fixed(g, interval) for g in gaps], gaps, interval)
        for interval in intervals
    )


def frontier_forecast(episodes_: list[tuple[tuple[float, float], float]],
                      tails: list[float]) -> list[tuple[float, float, float]]:
    gaps = [gap for _, gap in episodes_]
    return sorted(
        summarise([poll_forecast(gap, p50, p95, tail)
                   for (p50, p95), gap in episodes_], gaps, tail)
        for tail in tails
    )


def frontier_scaled(episodes_: list[tuple[tuple[float, float], float]],
                    factors: list[float]) -> list[tuple[float, float, float]]:
    gaps = [gap for _, gap in episodes_]
    return sorted(
        summarise([poll_scaled(gap, p50, factor) for (p50, _), gap in episodes_],
                   gaps, factor)
        for factor in factors
    )


def polls_at_latency(curve: list[tuple[float, float, float]],
                     latency: float) -> float | None:
    """Linearly interpolate a curve's poll cost at a given latency.

    None when the latency falls outside the swept range — extrapolating a
    frontier would manufacture a comparison that was never measured.
    """
    if not curve or latency < curve[0][0] or latency > curve[-1][0]:
        return None
    for (lo_lat, lo_polls, _), (hi_lat, hi_polls, _) in zip(curve, curve[1:], strict=False):
        if lo_lat <= latency <= hi_lat:
            if hi_lat == lo_lat:
                return min(lo_polls, hi_polls)
            span = (latency - lo_lat) / (hi_lat - lo_lat)
            return lo_polls + span * (hi_polls - lo_polls)
    return curve[-1][1]


def compare(name: str, treatment: list[tuple[float, float, float]],
            control: list[tuple[float, float, float]]) -> None:
    """Report poll saving at equal latency, the only fair single number."""
    print(f"\n  {name} vs control, matched on latency")
    print(f"    {'late(x)':>10} {'polls':>8} {'control':>9} {'saving':>9}")
    savings = []
    for latency, polls, _ in treatment:
        baseline = polls_at_latency(control, latency)
        if baseline is None:
            print(f"    {latency:>10.2f} {polls:>8.2f} {'off-curve':>9} {'—':>9}")
            continue
        saving = (baseline - polls) / baseline
        savings.append(saving)
        print(f"    {latency:>10.2f} {polls:>8.2f} {baseline:>9.2f} {saving:>8.1%}")
    if savings:
        print(f"    median poll saving at equal latency: "
              f"{statistics.median(savings):.1%}")


# --- main -------------------------------------------------------------------

WARMUP = 400
TRIALS = 6000
# The control sweep must span the treatment's whole operating range, or
# the points where the treatment wins get silently dropped as "off-curve"
# and the comparison is truncated in the treatment's favour.
INTERVALS = [2, 4, 8, 15, 25, 40, 60, 100, 160, 250, 400, 650, 1000,
             1600, 2500, 4000, 6500, 10000, 16000]
TAILS = [5, 10, 20, 40, 80, 150, 300, 600, 1200]
FACTORS = [0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5, 2.5, 4.0]


def median_saving(curve: list[tuple[float, float, float]],
                  control: list[tuple[float, float, float]]) -> float | None:
    savings = []
    for latency, polls, _ in curve:
        baseline = polls_at_latency(control, latency)
        if baseline is not None:
            savings.append((baseline - polls) / baseline)
    return statistics.median(savings) if savings else None


def run_world(world_name: str, world: dict, seed: int) -> dict[str, float | None]:
    print("\n" + "#" * 70)
    print(f"#  WORLD: {world_name}")
    print("#" * 70)

    learned = Arm("C learned", learns=True)
    static = Arm("B static prior", learns=False)

    # Arm C gets a warm-up so its forecasts reflect real history; arm B has
    # nothing to warm up. The control needs no forecasts at all.
    episodes(learned, world, WARMUP, random.Random(seed))

    learned_eps = episodes(learned, world, TRIALS, random.Random(seed + 1))
    static_eps = episodes(static, world, TRIALS, random.Random(seed + 1))
    gaps = [gap for _, gap in static_eps]

    # --- gate: are the learned forecasts even calibrated? -------------------
    report = learned.model.calibration(learned.agent, learned.workspace)
    print("\n  calibration gate (learned arm)")
    print(f"    samples      {report['samples']}")
    print(f"    p50 hit rate {report['p50_hit_rate']:.3f}   (target ~0.50)")
    print(f"    p95 hit rate {report['p95_hit_rate']:.3f}   (target ~0.95)")
    if not 0.35 <= report["p50_hit_rate"] <= 0.65 or report["p95_hit_rate"] < 0.85:
        print("    WARNING: badly calibrated — numbers below are not "
              "interpretable.")

    control = frontier_fixed(gaps, INTERVALS)
    print("\n  A control frontier (fixed interval)")
    print(f"    {'interval':>9} {'late(x)':>10} {'polls':>8}")
    for latency, polls, interval in control:
        print(f"    {interval:>9.0f} {latency:>10.2f} {polls:>8.2f}")

    summary: dict[str, float | None] = {}
    for policy, builder, knobs, knob_name in (
        ("checkpoints", frontier_forecast, TAILS, "tail"),
        ("cadence", frontier_scaled, FACTORS, "factor"),
    ):
        print("\n" + "=" * 68)
        if policy == "checkpoints":
            print("  POLICY 1: two checkpoints (poll at p50, then p95, then metronome)")
        else:
            print("  POLICY 2: forecast sets the cadence (poll every p50 * factor)")
        print("=" * 68)
        for label, eps in (("B static prior", static_eps), ("C learned", learned_eps)):
            curve = builder(eps, knobs)
            print(f"\n  {label} frontier")
            print(f"    {knob_name:>9} {'late(x)':>10} {'polls':>8}")
            for latency, polls, knob in curve:
                print(f"    {knob:>9.2f} {latency:>10.2f} {polls:>8.2f}")
            compare(label, curve, control)
            summary[f"{policy}/{label}"] = median_saving(curve, control)
    return summary


def main(seed: int = 20260807) -> None:
    print("Tier 1: is a published timing forecast worth anything?")
    print(f"seed={seed}  warmup={WARMUP}  trials={TRIALS}")

    orderly = run_world("ORDERLY (effort predicts scale; prior uniformly off)",
                        WORLD_ORDERLY, seed)
    idio = run_world("IDIOSYNCRATIC (prior wrong non-uniformly — the real case)",
                     WORLD_IDIOSYNCRATIC, seed + 100)

    print("\n" + "#" * 70)
    print("#  SUMMARY — median poll saving vs control at equal latency")
    print("#" * 70)
    print(f"\n  {'':<28} {'ORDERLY':>12} {'IDIOSYNCRATIC':>15}")
    for key in ("checkpoints/B static prior", "checkpoints/C learned",
                "cadence/B static prior", "cadence/C learned"):
        a, b = orderly.get(key), idio.get(key)
        fa = f"{a:.1%}" if a is not None else "—"
        fb = f"{b:.1%}" if b is not None else "—"
        print(f"  {key:<28} {fa:>12} {fb:>15}")
    print("\n  How to read it:")
    print("   * Positive = fewer polls than the best tuned fixed interval, at")
    print("     matched latency. Negative = the forecast made things worse.")
    print("   * C > A tests whether a forecast helps at all.")
    print("   * C > B tests whether the *learning* helped, or whether a fixed")
    print("     constant would have done just as well.")
    print("   * Comparing the two policies shows how much the answer depends on")
    print("     how a receiving agent chooses to use the forecast.")


if __name__ == "__main__":
    main()
