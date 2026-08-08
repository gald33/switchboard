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
* **Pilot scale.** 30 episodes, one world, three models, three arms. Sized
  to reveal whether any separation exists, not to power a conclusion.
"""

from __future__ import annotations

import json
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
SEED = 4242
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
    for arm in ARMS:
        episodes = build_episodes(WORLD, EPISODES, seed=SEED, arm=arm)
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
        (OUT / f"prompt_{arm}.txt").write_text(
            header + "\n" + "\n\n".join(blocks) + "\n"
        )
    print(f"wrote prompts for arms {ARMS} to {OUT}", file=sys.stderr)


def score() -> None:
    print(f"Tier 2 pilot — world={WORLD} episodes={EPISODES} seed={SEED}")
    print("Batched per (model, arm); see module docstring for what that costs.\n")

    by_arm: dict[str, list] = {}
    # Scripted floor first, so the model rows have something to be read against.
    ref = build_episodes(WORLD, EPISODES, seed=SEED, arm="B")
    by_arm["-- scripted cadence"] = [
        run_episode(e, "B", cadence_coordinator()) for e in ref
    ]
    for interval in (300.0, 900.0):
        control = build_episodes(WORLD, EPISODES, seed=SEED, arm="A")
        by_arm[f"-- scripted fixed {interval:.0f}s"] = [
            run_episode(e, "A", fixed_coordinator(interval)) for e in control
        ]

    for model in MODELS:
        for arm in ARMS:
            path = OUT / f"response_{model}_{arm}.json"
            if not path.exists():
                continue
            schedules = json.loads(path.read_text())
            episodes = build_episodes(WORLD, EPISODES, seed=SEED, arm=arm)
            results = []
            for i, episode in enumerate(episodes):
                offsets = schedules.get(str(i)) or schedules.get(i) or []
                results.append(
                    run_episode(episode, arm, schedule_coordinator(offsets))
                )
            by_arm[f"{model} arm {arm}"] = results

    print(compare_arms(by_arm))

    print("\n  Per-model read (A -> B -> C):")
    for model in MODELS:
        cells = {a: by_arm.get(f"{model} arm {a}") for a in ARMS}
        if not any(cells.values()):
            continue
        parts = []
        for arm in ARMS:
            results = cells[arm]
            if not results:
                parts.append(f"{arm}: —")
                continue
            s = summarise(results)
            late = s["median_relative_latency"]
            parts.append(
                f"{arm}: {s['mean_checks']:.1f} checks / "
                f"{'—' if late is None else f'{late:.2f}'} late"
            )
        print(f"    {model:<8} " + "   ".join(parts))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "score":
        score()
    else:
        emit()
