"""Tier 2 runner: models trade on the island, over a real Switchboard hub.

Not collected by pytest — it costs money and needs a working model client. The
mechanics are gated offline by ``tests/test_barter_llm.py``, which drives the
whole harness with a scripted stand-in model and never makes a network call.

    python tests/experiments/barter_llm_experiment.py --arm B
    python tests/experiments/barter_llm_experiment.py --arms A B --agents 6 --rounds 8

What is being measured
----------------------
The same numbers as Tier 1, against the same benchmarks, so the two are directly
comparable: distance to the Pareto frontier, distance to the frontier of the
production plan the agents actually chose, who ended up worse off than never
trading, and how many agents were ruined outright.

Read them against the Tier 1 ladder, which is why that had to exist first:

    autarky floor      nobody trades
    exchange ceiling   trade perfectly, produce as if alone
    scripted arm C     a shared price, hand-coded — reaches 1.000
    frontier           1.000

An arm B run that lands near the exchange ceiling means the models traded but
never coordinated production. One that lands well above it means they found
something, and the transcript says what. That is the whole point of keeping
every message: the *content* of what agents invent is the finding here, and no
aggregate can carry it.

Honest limits
-------------
* **Small.** Defaults are a handful of agents over a handful of rounds, because
  every agent-turn is a model call. One island is an anecdote; the seed sweep
  that would make it evidence is the expensive part, and ``--islands`` exists
  for when it is worth paying for.
* **Turn-based.** Agents act in a shuffled order, one turn each per round. Real
  agents on a hub are concurrent, and concurrency is exactly where a
  coordination protocol is hardest. This measures the easier problem.
* **A ruined agent is not noise.** Cobb-Douglas utility is zero at a zero
  holding, so an agent that never acquired some good scores zero and the island
  is reported as ruined rather than averaged into a mean.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from barter.economy import autarky, draw_island, efficiency, exchange_ceiling  # noqa: E402
from barter.llm import TURN, Wire, brief_for, build_tools, tool_names  # noqa: E402
from barter.manager import Manager, ManagerService  # noqa: E402
from barter.run import score  # noqa: E402

from switchboard.testing import hub  # noqa: E402

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


async def _drive(options: object, prompt: str) -> tuple[str, float]:
    """One agent turn. Returns its final text and what the turn cost.

    Running out of turns ends the agent's round; it does not end the run. An
    agent that spent its whole budget calling tools has simply used its time,
    the same as an agent that stopped early, and the goods it moved before
    running out are real. Letting that abort the island would silently bias the
    experiment toward whichever arm happens to be terser — and arm B, which has
    two extra tools to spend turns on, is exactly the talkative one.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

    said, cost = [], 0.0
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        said.append(block.text)
            elif isinstance(message, ResultMessage):
                cost += message.total_cost_usd or 0.0
    except Exception as exc:  # noqa: BLE001 - the turn is over either way
        if "maximum number of turns" not in str(exc):
            raise
        said.append("[turn budget exhausted]")
    return "\n".join(said), cost


async def run_llm_island(
    *,
    arm: str,
    agents: int,
    goods: int,
    rounds: int,
    seed: int,
    model: str,
    max_turns: int,
    verbose: bool,
) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions

    island = draw_island(agents, goods, seed=seed)
    manager = Manager(island=island)
    rng = random.Random(seed)
    run = f"llm{seed}{arm}"

    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run=run)
        service.claim()

        wires, options = {}, {}
        for agent_id in manager.agents:
            wire = Wire(agent_id=agent_id, client=handle.client(agent_id), service=service,
                        arm=arm, floor_channel=f"barter/{run}/floor")
            wires[agent_id] = wire
            options[agent_id] = ClaudeAgentOptions(
                model=model,
                system_prompt=brief_for(island, manager, agent_id, arm),
                mcp_servers={f"island-{agent_id}": build_tools(wire)},
                # An allowlist of exactly this agent's island tools. No
                # filesystem, no shell, no web — the island is the whole world
                # an agent can act on, so anything it achieves came through the
                # manager or through talking to a peer. `setting_sources=[]`
                # keeps the surrounding repo's own settings, skills and hooks
                # out of the run, which would otherwise vary the experiment with
                # whatever happens to be checked out.
                allowed_tools=tool_names(arm, agent_id),
                max_turns=max_turns,
                setting_sources=[],
            )

        cost = 0.0
        order = list(manager.agents)
        transcript: list[dict] = []

        for round_no in range(1, rounds + 1):
            if round_no == 2:
                # Production closes after round 1. Anyone who did not spend
                # their labour gets the autarky plan, so "never produced" shows
                # up as not having specialised rather than as starvation.
                manager.open_trading()
                manager.check_conservation()
            phase_note = (
                "Production is open — this is the only round in which you can call "
                "`produce`. Trading opens next round."
                if round_no == 1 else
                f"Trading is open. {rounds - round_no} round(s) remain after this one."
            )
            rng.shuffle(order)
            for agent_id in order:
                prompt = TURN.format(round_no=round_no, rounds=rounds, phase_note=phase_note)
                text, spent = await _drive(options[agent_id], prompt)
                cost += spent
                transcript.append({"round": round_no, "agent": agent_id, "text": text})
                if verbose:
                    print(f"  [{round_no}] {agent_id}: {text[:160]}", file=sys.stderr)
            service.drain()
            manager.advance()
            manager.check_conservation()

        if manager.phase == "production":
            manager.open_trading()
        manager.close()
        manager.check_conservation()
        service.publish()

        floor = handle.client("reader").history(f"barter/{run}/floor", limit=500)

    # Same scorer as Tier 1, against the same benchmarks, so the two tiers are
    # directly comparable rather than merely similar-looking.
    outcome = score(island, manager, arm=arm, seed=seed, messages=len(floor))
    _, autarky_utils = autarky(island)
    return {
        "arm": arm, "seed": seed, "model": model, "cost_usd": round(cost, 4),
        "efficiency": [outcome.efficiency.lower, outcome.efficiency.upper],
        "ruined": list(outcome.efficiency.ruined),
        "own_plan": [outcome.exchange_efficiency.lower, outcome.exchange_efficiency.upper],
        "own_plan_ruined": list(outcome.exchange_efficiency.ruined),
        "autarky_floor": efficiency(island, autarky_utils).lower,
        "exchange_ceiling": exchange_ceiling(island).lower,
        "worst_ratio": outcome.worst_ratio,
        "summary": manager.summary(),
        "rejections": manager.rejections[-40:],
        "said": [m["body"] for m in floor if isinstance(m.get("body"), dict)],
        "transcript": transcript,
    }


def _plan_note(result: dict) -> str:
    if result["own_plan_ruined"]:
        return f"ruined {len(result['own_plan_ruined'])} agent(s)"
    return f"{result['own_plan'][0]:.3f}"


def render(result: dict) -> str:
    score = result["efficiency"]
    ruined = result["ruined"]
    verdict = (f"ruined {len(ruined)} agent(s)" if ruined
               else f"{score[0]:.3f}-{score[1]:.3f}")
    summary = result["summary"]
    lines = [
        f"arm {result['arm']}  seed {result['seed']}  {result['model']}"
        f"  ${result['cost_usd']}",
        f"  autarky floor    {result['autarky_floor']:.3f}",
        f"  exchange ceiling {result['exchange_ceiling']:.3f}",
        f"  EFFICIENCY       {verdict}",
        f"  of its own plan  {_plan_note(result)}",
        f"  worst agent      {result['worst_ratio']:.2f}x autarky",
        f"  trades           {summary['executed']} settled of {summary['proposed']} proposed"
        f"  ({summary['rejected']} rejected, {summary['expired']} expired)",
        f"  said             {len(result['said'])} message(s)",
    ]
    if result["said"]:
        lines.append("\n  what they said")
        for message in result["said"][:14]:
            text = str(message.get("text", "")).replace("\n", " ")[:150]
            lines.append(f"    {message.get('from', '?'):>4}: {text}")
        if len(result["said"]) > 14:
            lines.append(f"    ... {len(result['said']) - 14} more")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arms", nargs="+", default=["B"], choices=["A", "B"])
    parser.add_argument("--agents", type=int, default=5)
    parser.add_argument("--goods", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--islands", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=18)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    results = []
    for arm in args.arms:
        for step in range(args.islands):
            result = asyncio.run(run_llm_island(
                arm=arm, agents=args.agents, goods=args.goods, rounds=args.rounds,
                seed=args.seed + step, model=args.model, max_turns=args.max_turns,
                verbose=args.verbose,
            ))
            results.append(result)
            print(render(result), flush=True)
            print(flush=True)
            # Written after *every* island, not once at the end. An island costs
            # real money and takes real wall-clock, so a later one timing out or
            # dying must not take the finished ones with it — which it did, the
            # first time this was run under a timeout.
            if args.json:
                args.json.write_text(json.dumps(results, indent=2, default=str))

    total = sum(r["cost_usd"] for r in results)
    print(f"total ${total:.2f} over {len(results)} island(s)")
    if args.json:
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
