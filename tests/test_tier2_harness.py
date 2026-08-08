"""Smoke tests for the Tier 2 (LLM-in-the-loop) harness.

The harness itself is not collected by pytest — running it for real costs
model calls. These tests cover its mechanics with scripted policies so it
cannot rot between runs, and pin the epoch bug that made the reference
policy look useless the first time it was run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))

from tier2_harness import (  # noqa: E402
    MAX_CHECKS,
    Episode,
    VirtualWorld,
    build_episodes,
    cadence_coordinator,
    fixed_coordinator,
    render_message,
    run_episode,
    summarise,
)


def an_episode(gap=100.0, forecast=(50.0, 400.0)) -> Episode:
    return Episode(world="orderly", execution_class="coding", effort="medium",
                    true_gap=gap, forecast_seconds=forecast, task="coding work")


def test_virtual_clock_only_reveals_the_reply_once_time_has_passed():
    world = VirtualWorld(an_episode(gap=100.0))
    assert world.check_messages() is False
    world.work_for(50.0)
    assert world.check_messages() is False
    world.work_for(60.0)
    assert world.check_messages() is True
    assert world.checks == 3


def test_latency_is_measured_from_the_reply_not_the_start():
    world = VirtualWorld(an_episode(gap=100.0))
    world.work_for(130.0)
    world.check_messages()
    result = world.result()
    assert result.latency == 30.0
    assert result.relative_latency == 0.3
    assert result.truncated is False


def test_an_episode_that_is_never_noticed_is_reported_not_dropped():
    def never_checks(prompt, world):
        world.work_for(10.0)

    result = run_episode(an_episode(gap=100.0), "B", never_checks)
    assert result.latency is None
    assert result.truncated is True
    assert summarise([result])["never_noticed"] == 1


def test_a_runaway_coordinator_is_bounded():
    def checks_forever(prompt, world):
        while not world.exhausted():
            world.check_messages()

    result = run_episode(an_episode(gap=1e9), "B", checks_forever)
    assert result.checks == MAX_CHECKS
    assert result.truncated is True


def test_forecast_is_stored_relative_and_rendered_at_episode_time():
    """The bug this pins: the worker's timing model runs on its own clock
    while each episode has its own virtual start. Storing absolute stamps
    mixed the two epochs, and the cadence policy silently degraded to
    checking every second."""
    episode = an_episode(forecast=(50.0, 400.0))
    world = VirtualWorld(episode)
    rendered = render_message(episode, world.now, "B")
    assert "timing_forecast" in rendered
    # The interval the policy derives must be on the episode's scale.
    cadence_coordinator(factor=0.5)("", world)
    assert world.check_times[0] == 25.0


def test_arm_a_hides_the_forecast_and_arm_c_adds_the_advisory():
    episodes_a = build_episodes("orderly", 3, seed=1, arm="A")
    episodes_b = build_episodes("orderly", 3, seed=1, arm="B")
    assert all(e.forecast_seconds is None for e in episodes_a)
    assert all(e.forecast_seconds is not None for e in episodes_b)
    # Same seed => same ground truth, so arms are paired not merely similar.
    assert [e.true_gap for e in episodes_a] == [e.true_gap for e in episodes_b]

    start = 1_700_000_000.0
    assert "timing_forecast" not in render_message(episodes_a[0], start, "A")
    assert "guidance:" not in render_message(episodes_b[0], start, "B")
    assert "guidance:" in render_message(episodes_b[0], start, "C")


def test_cadence_reference_beats_a_metronome_on_lateness():
    """Sanity floor for reading a real run: a model that cannot beat this
    is not using the signal."""
    episodes = build_episodes("idiosyncratic", 120, seed=3, arm="B")
    cadence = [run_episode(e, "B", cadence_coordinator()) for e in episodes]
    fixed = [run_episode(e, "A", fixed_coordinator(300.0)) for e in episodes]
    assert (summarise(cadence)["median_relative_latency"]
            < summarise(fixed)["median_relative_latency"])
