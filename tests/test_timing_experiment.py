"""Smoke tests for the Tier 1 timing experiment harness.

The experiment itself (tests/experiments/timing_experiment.py) is not
collected by pytest — it is a long, offline run, not a gate. These tests
cover its arithmetic so it cannot silently rot, and they encode the two
methodology traps that already bit once during development.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))

from timing_experiment import (  # noqa: E402
    WORLD_IDIOSYNCRATIC,
    WORLD_ORDERLY,
    Arm,
    episodes,
    frontier_fixed,
    frontier_scaled,
    poll_fixed,
    poll_scaled,
    polls_at_latency,
    sample_gap,
)


def test_fixed_policy_counts_polls_and_lateness():
    # gap 10, interval 4 -> polls at 4, 8, 12; caught at 12, so 3 polls.
    assert poll_fixed(10.0, 4.0) == (3, 2.0)
    # A gap shorter than one interval still costs one poll.
    assert poll_fixed(1.0, 4.0) == (1, 3.0)


def test_scaled_policy_uses_the_forecast_as_a_cadence():
    # p50 20, factor 0.5 -> interval 10; gap 25 -> polls at 10, 20, 30.
    assert poll_scaled(25.0, 20.0, 0.5) == (3, 5.0)


def test_latency_is_measured_relative_to_the_gap():
    """The trap that inverted the first result: averaging *absolute*
    lateness lets a coarse metronome hide being 900s late on a 20s task
    behind the slow tasks in the mean."""
    gaps = [10.0, 1000.0]
    (latency, polls, interval), = frontier_fixed(gaps, [1000.0])
    # One poll each, but the short task is 99x late and must be priced so.
    assert polls == 1.0
    assert latency == (99.0 + 0.0) / 2


def test_polls_at_latency_refuses_to_extrapolate():
    """The other trap: a control sweep that does not span the treatment's
    range silently drops the points where the treatment wins."""
    curve = [(1.0, 10.0, 0.0), (2.0, 5.0, 0.0)]
    assert polls_at_latency(curve, 1.5) == 7.5
    assert polls_at_latency(curve, 0.5) is None
    assert polls_at_latency(curve, 9.0) is None


def test_sample_gap_is_seeded_and_positive():
    a = [sample_gap(WORLD_ORDERLY, ("coding", "low"), random.Random(1))
         for _ in range(5)]
    b = [sample_gap(WORLD_ORDERLY, ("coding", "low"), random.Random(1))
         for _ in range(5)]
    assert a == b
    assert all(g > 0 for g in a)


def test_learned_arm_beats_the_static_prior_where_the_prior_is_skewed():
    """The headline finding, at small scale: when the generic prior is
    wrong non-uniformly, learning is what recovers the win."""
    learned, static = Arm("C", learns=True), Arm("B", learns=False)
    episodes(learned, WORLD_IDIOSYNCRATIC, 300, random.Random(7))
    learned_eps = episodes(learned, WORLD_IDIOSYNCRATIC, 800, random.Random(8))
    static_eps = episodes(static, WORLD_IDIOSYNCRATIC, 800, random.Random(8))

    factors = [0.25, 0.5, 1.0, 2.0]
    best = lambda eps: min(  # noqa: E731
        polls for _, polls, _ in frontier_scaled(eps, factors)
    )
    assert best(learned_eps) < best(static_eps)


def test_calibration_gate_is_satisfied_by_the_estimator():
    arm = Arm("C", learns=True)
    episodes(arm, WORLD_ORDERLY, 1500, random.Random(3))
    report = arm.model.calibration(arm.agent, arm.workspace)
    assert 0.35 <= report["p50_hit_rate"] <= 0.65
    assert report["p95_hit_rate"] >= 0.85
