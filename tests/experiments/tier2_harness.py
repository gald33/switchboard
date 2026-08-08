"""Tier 2 harness: does a *language model* use a timing forecast well?

Not collected by pytest. See tests/test_tier2_harness.py for the smoke
tests, and timing_experiment.py for Tier 1.

What Tier 1 could not answer
----------------------------
Tier 1 used scripted pollers, so it measured the machinery: given a
forecast, does *some* policy beat a tuned metronome? Answer: yes, but only
the cadence reading — treating p50/p95 as two individual poll times was
worse than no forecast at all (-6%).

That leaves the actual product question open. The protocol deliberately
does not prescribe how a receiver reads the metadata, so everything rests
on models picking a good reading on their own. A one-sentence advisory
now ships in the `inbox`/`checkin` tool descriptions on the strength of a
*simulation*. Whether it changes real model behaviour has never been
tested. This harness tests it.

Design
------
**Only the coordinator is a model.** The worker is scripted, drawing its
true time-to-next-look from the same latent worlds as Tier 1. That is not
a shortcut for its own sake: the open question is coordinator-side, a
scripted worker gives deterministic ground truth instead of a second
source of model noise, and it halves the cost per episode.

**Virtual clock.** The coordinator spends time via `work_for(seconds)`
rather than really sleeping, so an episode with a 40-minute latent gap
costs a handful of model turns instead of 40 minutes. Cost is counted in
`check_messages()` calls, which is what a real agent actually pays.

The two-tool surface (`work_for` / `check_messages`) is the honest shape
of the decision under test. A real agent coordinating asynchronously does
not sit in a poll loop; it does something else for a while and then looks.
`inbox(wait=)` caps at 25s and would turn a long gap into ~100 forced
calls, measuring the cap rather than the model's judgement.

Arms
----
    A  no forecast, no advisory      does the model do fine without any hint?
    B  forecast, no advisory         does the raw signal help on its own?
    C  forecast + advisory           does the advisory earn its tokens?

**B vs C is the arm that judges what shipped in #38.** If they match, the
advisory is costing context for nothing. If B is *worse* than A, the raw
signal is actively misleading models and the advisory is load-bearing
rather than decorative.
"""

from __future__ import annotations

import random
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from timing_experiment import (  # noqa: E402
    WORLD_IDIOSYNCRATIC,
    WORLD_ORDERLY,
    sample_gap,
)

from switchboard.mcp_server import _FORECAST_ADVICE  # noqa: E402
from switchboard.timing import TimingModel  # noqa: E402

WORLDS = {"orderly": WORLD_ORDERLY, "idiosyncratic": WORLD_IDIOSYNCRATIC}

#: Hard stop per episode. A model that never checks would otherwise run
#: forever; a model that checks compulsively would run up a bill. Episodes
#: that hit either bound are reported, not silently dropped — a truncated
#: episode is a finding about the policy, not noise to hide.
MAX_CHECKS = 40
MAX_VIRTUAL_SECONDS = 12 * 3600.0


@dataclass
class Episode:
    """One coordination scenario with known ground truth."""

    world: str
    execution_class: str
    effort: str
    #: True seconds until the worker looks at its inbox and replies.
    true_gap: float
    #: What the worker published, as seconds from the episode start.
    #: Stored relative, not as absolute timestamps: the worker's timing
    #: model runs on its own clock while each episode has its own virtual
    #: start, and mixing the two epochs silently produced negative
    #: intervals. Absolute stamps are rendered at episode time instead.
    forecast_seconds: tuple[float, float] | None
    #: Human-readable task, so the model has real semantic context.
    task: str


@dataclass
class Result:
    episode: Episode
    checks: int
    #: Virtual seconds between the worker replying and the coordinator
    #: noticing. None when the coordinator never noticed.
    latency: float | None
    truncated: bool = False
    #: Virtual times at which the coordinator checked, for diagnosing
    #: *which* reading it used.
    check_times: list[float] = field(default_factory=list)

    @property
    def relative_latency(self) -> float | None:
        """Lateness in units of the task's own length — scale-free, for the
        same reason Tier 1 needed it: averaging absolute lateness lets a
        coarse policy hide being 900s late on a 20s task."""
        if self.latency is None:
            return None
        return self.latency / self.episode.true_gap


def build_episodes(world: str, n: int, seed: int, arm: str) -> list[Episode]:
    """Deterministic episode set. Identical across arms for the same seed,
    so arms are paired rather than merely comparable."""
    rng = random.Random(seed)
    profiles = list(WORLDS[world])
    model = TimingModel(":memory:")
    # Warm the worker's model so arm B/C forecasts reflect real history
    # rather than the cold prior — the learned case is what ships.
    clock = 1_000_000.0
    for _ in range(400):
        profile = profiles[rng.randrange(len(profiles))]
        model.declare("worker", "sim", *profile, now=clock)
        gap = sample_gap(WORLDS[world], profile, rng)
        model.note_look("worker", "sim", now=clock + gap)
        clock += gap + 1.0

    episodes = []
    for _ in range(n):
        execution_class, effort = profiles[rng.randrange(len(profiles))]
        forecast = model.declare("worker", "sim", execution_class, effort, now=clock)
        gap = sample_gap(WORLDS[world], (execution_class, effort), rng)
        model.note_look("worker", "sim", now=clock + gap)
        clock += gap + 1.0
        episodes.append(Episode(
            world=world, execution_class=execution_class, effort=effort,
            true_gap=gap,
            forecast_seconds=(
                None if arm == "A"
                else (forecast.p50_seconds, forecast.p95_seconds)
            ),
            task=f"{execution_class} work ({effort} effort)",
        ))
    return episodes


def render_message(episode: Episode, start: float, arm: str) -> str:
    """What the coordinator sees when the worker hands off."""
    lines = [
        f'worker: "Taking this on — {episode.task}. I\'ll report back."',
        f"now: {_iso(start)}",
    ]
    if episode.forecast_seconds is not None:
        p50, p95 = episode.forecast_seconds
        lines.append(
            "timing_forecast: "
            f"p50={_iso(start + p50)} p95={_iso(start + p95)}"
        )
    if arm == "C":
        lines.append("guidance:" + _FORECAST_ADVICE)
    return "\n".join(lines)


class VirtualWorld:
    """The tool surface the coordinator acts against, on a virtual clock."""

    def __init__(self, episode: Episode, start: float = 1_700_000_000.0) -> None:
        self.episode = episode
        self.start = start
        self.now = start
        self.checks = 0
        self.check_times: list[float] = []
        self.noticed_at: float | None = None

    @property
    def reply_time(self) -> float:
        return self.start + self.episode.true_gap

    def work_for(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))

    def check_messages(self) -> bool:
        """Returns True once the worker's reply is visible."""
        self.checks += 1
        self.check_times.append(self.now - self.start)
        if self.now >= self.reply_time:
            if self.noticed_at is None:
                self.noticed_at = self.now
            return True
        return False

    def exhausted(self) -> bool:
        return (self.checks >= MAX_CHECKS
                or self.now - self.start >= MAX_VIRTUAL_SECONDS)

    def result(self) -> Result:
        latency = None if self.noticed_at is None else self.noticed_at - self.reply_time
        return Result(
            episode=self.episode, checks=self.checks, latency=latency,
            truncated=self.noticed_at is None, check_times=self.check_times,
        )


def run_episode(episode: Episode, arm: str, coordinator) -> Result:
    """Drive one episode. `coordinator` is any callable implementing the
    policy — a scripted stub in tests, a model in a real run — called as
    coordinator(prompt, world) and expected to drive world.work_for /
    world.check_messages until it notices or the world is exhausted.
    """
    world = VirtualWorld(episode)
    prompt = render_message(episode, world.now, arm)
    coordinator(prompt, world)
    return world.result()


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# --- reporting ---------------------------------------------------------------


def summarise(results: list[Result]) -> dict[str, float | int | None]:
    noticed = [r for r in results if r.latency is not None]
    relative = [r.relative_latency for r in noticed]
    return {
        "episodes": len(results),
        "mean_checks": statistics.mean(r.checks for r in results) if results else None,
        "median_relative_latency": statistics.median(relative) if relative else None,
        "never_noticed": sum(1 for r in results if r.truncated),
    }


def compare_arms(by_arm: dict[str, list[Result]]) -> str:
    lines = [
        "",
        f"  {'arm':<28} {'episodes':>9} {'checks':>8} {'late(x)':>9} {'missed':>7}",
    ]
    for arm, results in by_arm.items():
        s = summarise(results)
        late = "—" if s["median_relative_latency"] is None else \
            f"{s['median_relative_latency']:.2f}"
        checks = "—" if s["mean_checks"] is None else f"{s['mean_checks']:.2f}"
        lines.append(
            f"  {arm:<28} {s['episodes']:>9} {checks:>8} {late:>9} {s['never_noticed']:>7}"
        )
    lines += [
        "",
        "  Both columns matter and neither alone is meaningful: fewer checks",
        "  is trivially achievable by checking less, and lower lateness by",
        "  checking constantly. An arm only wins by improving one without",
        "  giving back the other.",
        "",
        "  A vs B: does the raw forecast help a model at all?",
        "  B vs C: does the shipped advisory earn its context?",
        "          (if B is worse than A, the raw signal misleads and the",
        "           advisory is load-bearing rather than decorative)",
        "  'missed' counts episodes where the coordinator never noticed —",
        "  reported, never silently dropped.",
    ]
    return "\n".join(lines)


# --- a scripted reference policy ---------------------------------------------
# Not an arm. Used by the smoke tests, and as a sanity floor when reading a
# real run: a model that cannot beat this is not using the signal.


def cadence_coordinator(factor: float = 0.5):
    """The Tier 1 winner: size the checking interval to the forecast."""
    def run(prompt: str, world: VirtualWorld) -> None:
        forecast = world.episode.forecast_seconds
        interval = 60.0 if forecast is None else max(1.0, forecast[0] * factor)
        while not world.exhausted():
            world.work_for(interval)
            if world.check_messages():
                return
    return run


def fixed_coordinator(interval: float = 120.0):
    """The Tier 1 control: a metronome that ignores any forecast."""
    def run(prompt: str, world: VirtualWorld) -> None:
        while not world.exhausted():
            world.work_for(interval)
            if world.check_messages():
                return
    return run


def main() -> None:
    print(__doc__.strip().split("\n")[0])
    print("\nThis file is the harness only. Running it against a real model")
    print("needs three decisions that cost money and should be deliberate:")
    print("  * which model(s) to test")
    print("  * episodes per arm (paired across arms by seed)")
    print("  * whether to include the scripted reference policies as a floor")
    print("\nScripted reference policies, for calibration of expectations:")
    for world in WORLDS:
        by_arm: dict[str, list[Result]] = {}
        episodes = build_episodes(world, 200, seed=7, arm="B")
        by_arm[f"{world}: cadence (scripted)"] = [
            run_episode(e, "B", cadence_coordinator()) for e in episodes
        ]
        control = build_episodes(world, 200, seed=7, arm="A")
        for interval in (60.0, 300.0, 900.0):
            by_arm[f"{world}: fixed {interval:.0f}s (scripted)"] = [
                run_episode(e, "A", fixed_coordinator(interval)) for e in control
            ]
        print(compare_arms(by_arm))


if __name__ == "__main__":
    main()


def schedule_coordinator(offsets: list[float]):
    """Execute a checking schedule the model committed to up front.

    A stated plan ("I'll look back in about X, then Y") is what an agent
    actually produces when handing off asynchronously, and it is the
    decision under test. It is not the same as fully adaptive in-situ
    behaviour — see tier2_run.py for why that trade was made and what it
    costs in validity.
    """
    def run(prompt: str, world: VirtualWorld) -> None:
        last = 0.0
        for offset in sorted(float(o) for o in offsets):
            if world.exhausted():
                return
            world.work_for(max(0.0, offset - last))
            last = offset
            if world.check_messages():
                return
        # Plan exhausted without finding it: fall back to a slow sweep
        # rather than scoring the model as "never noticed", which would
        # conflate a short plan with a broken one.
        while not world.exhausted():
            world.work_for(max(60.0, last * 0.5))
            if world.check_messages():
                return
    return run
