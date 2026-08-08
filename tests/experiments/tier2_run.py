"""Runner for the Tier 2 pilot: emit prompts, score model responses.

Usage:
    python tests/experiments/tier2_run.py emit   > /dev/null   # writes prompts
    python tests/experiments/tier2_run.py score               # after responses land

Design compromises, stated plainly because they bound what the pilot can
conclude:

* **Batched, not per-episode.** No API key is available in this
  environment, so model decisions come from subagents, and one subagent
  per episode is not affordable. Each subagent sees all episodes for one
  (model, arm) cell. That permits cross-episode calibration a real agent
  would not have. The bias runs *against* the hypothesis under test:
  batching helps arm B most (it can infer a good forecast-to-cadence
  mapping unaided), which makes the advisory look less necessary than it
  is. A positive C-over-B result survives this; a null one does not
  settle the question.
* **Stated plan, not in-situ adaptation.** The model commits to a
  checking schedule up front. That is the shape of a real asynchronous
  handoff, and it is the decision under test, but it does not measure
  re-planning after a surprise.
* **Replicated across seeds.** Three independent 30-episode sets per
  (model, arm). One seed cannot distinguish a real effect from a lucky
  draw, so the spread across seeds is reported next to the mean and is
  the honest measure of how much any single number is worth.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from tier2_harness import (  # noqa: E402
    build_episodes,
    cadence_coordinator,
    compare_arms,
    fixed_coordinator,
    render_message,
    run_episode,
    schedule_coordinator,
    summarise,
)

from switchboard.mcp_server import _FORECAST_ADVICE  # noqa: E402

OUT = Path(__file__).resolve().parent / "tier2_pilot"
WORLD = "idiosyncratic"  # the world the feature exists for
EPISODES = 30
SEEDS = (4242, 7777, 31337)
ARMS = ("A", "B", "C")
MODELS = ("opus", "sonnet", "haiku")

INSTRUCTIONS = """\
You are coordinating with another agent asynchronously. You delegated work
to it and it has acknowledged. You now need to notice its reply reasonably
promptly WITHOUT wasting calls: every time you check for messages costs
you a tool call and tokens, but noticing late costs the collaboration
latency.

For each scenario below, decide when you will check for messages. Express
your decision as a list of offsets in SECONDS from now, in increasing
order. You may list as many or as few checks as you judge right. You stop
checking once you find the reply.

Decide each scenario on its own merits.

Return ONLY a JSON object mapping scenario id to a list of numbers, e.g.
{"0": [30, 90, 300], "1": [600, 1800]}
No prose, no code fences.
"""


def emit() -> None:
    OUT.mkdir(exist_ok=True)
    start = 1_700_000_000.0
    for seed in SEEDS:
      for arm in ARMS:
        episodes = build_episodes(WORLD, EPISODES, seed=seed, arm=arm)
        # Render scenarios without the advisory and attach it once up
        # front. In the product it lives in a tool description the model
        # reads once, not stapled to every message; repeating it 30 times
        # would test repetition rather than the guidance itself.
        body_arm = "B" if arm == "C" else arm
        blocks = [
            f"--- scenario {i} ---\n{render_message(e, start, body_arm)}"
            for i, e in enumerate(episodes)
        ]
        header = INSTRUCTIONS
        if arm == "C":
            header += "\nGuidance on reading 'timing_forecast':" + _FORECAST_ADVICE + "\n"
        (OUT / f"prompt_{arm}_s{seed}.txt").write_text(
            header + "\n" + "\n\n".join(blocks) + "\n"
        )
    print(f"wrote prompts for {len(SEEDS)} seeds x {ARMS} to {OUT}", file=sys.stderr)


def _cell(model: str, arm: str, seed: int):
    path = OUT / f"response_{model}_{arm}_s{seed}.json"
    if not path.exists():
        return None
    schedules = json.loads(path.read_text())
    episodes = build_episodes(WORLD, EPISODES, seed=seed, arm=arm)
    return [
        run_episode(e, arm, schedule_coordinator(
            schedules.get(str(i)) or schedules.get(i) or []))
        for i, e in enumerate(episodes)
    ]


def score() -> None:
    print(f"Tier 2 — world={WORLD} episodes={EPISODES} seeds={SEEDS}")
    print("Batched per (model, arm, seed); see module docstring for what that costs.\n")

    by_arm: dict[str, list] = {}
    for seed in SEEDS:
        ref = build_episodes(WORLD, EPISODES, seed=seed, arm="B")
        by_arm.setdefault("-- scripted cadence", []).extend(
            run_episode(e, "B", cadence_coordinator()) for e in ref)
        control = build_episodes(WORLD, EPISODES, seed=seed, arm="A")
        by_arm.setdefault("-- scripted fixed 300s", []).extend(
            run_episode(e, "A", fixed_coordinator(300.0)) for e in control)

    per_seed: dict[tuple[str, str], list[dict]] = {}
    for model in MODELS:
        for arm in ARMS:
            pooled = []
            for seed in SEEDS:
                results = _cell(model, arm, seed)
                if results is None:
                    continue
                pooled.extend(results)
                per_seed.setdefault((model, arm), []).append(summarise(results))
            if pooled:
                by_arm[f"{model} arm {arm}"] = pooled

    print(compare_arms(by_arm))

    # The point of replicating across seeds: a single number is worth
    # little without knowing how much it moves when only the draw changes.
    print("\n  Per-model, pooled across seeds, with per-seed range:")
    print(f"    {'model':<8} {'arm':<4} {'checks':>16} {'late(x)':>18}")
    for model in MODELS:
        for arm in ARMS:
            seeds = per_seed.get((model, arm))
            if not seeds:
                continue
            checks = [s["mean_checks"] for s in seeds]
            lates = [s["median_relative_latency"] for s in seeds
                     if s["median_relative_latency"] is not None]
            c = f"{statistics.mean(checks):.2f} [{min(checks):.2f}-{max(checks):.2f}]"
            le = (f"{statistics.mean(lates):.2f} [{min(lates):.2f}-{max(lates):.2f}]"
                  if lates else "—")
            print(f"    {model:<8} {arm:<4} {c:>16} {le:>18}")

    print("\n  Arm deltas per seed (negative = better than arm A):")
    for model in MODELS:
        for arm in ("B", "C"):
            base, other = per_seed.get((model, "A")), per_seed.get((model, arm))
            if not base or not other:
                continue
            dc = [o["mean_checks"] - b["mean_checks"] for b, o in zip(base, other, strict=True)]
            dl = [o["median_relative_latency"] - b["median_relative_latency"]
                  for b, o in zip(base, other, strict=True)]
            sign = "consistent" if all(x < 0 for x in dc) or all(x > 0 for x in dc) else "MIXED"
            print(f"    {model:<8} {arm} vs A  checks "
                  f"{[round(x, 2) for x in dc]}  late {[round(x, 2) for x in dl]}  ({sign})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "score":
        score()
    else:
        emit()
