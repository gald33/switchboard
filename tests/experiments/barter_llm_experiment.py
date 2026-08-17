"""Tier 2 runner: models trade on the island, over a real Switchboard hub.

Not collected by pytest — it costs money and needs a working model client. The
mechanics are gated offline by ``tests/test_barter_llm.py``, which drives the
whole harness with a scripted stand-in model and never makes a network call.

    python tests/experiments/barter_llm_experiment.py --arms told built
    python tests/experiments/barter_llm_experiment.py --arms silent free --agents 6

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

A run that lands near the exchange ceiling means the models traded but never
coordinated production. One that lands well above it means they found something,
and the transcript says what. That is the whole point of keeping every message
and every quote: the *content* of what agents do with a convention is the
finding here, and no aggregate can carry it.

The four arms are `silent`, `free`, `told` and `built` — see `barter/llm.py`.
`told` and `built` share a system prompt byte for byte and differ only in
whether the convention has machinery, so running them as a pair is the point;
running either alone measures very little.

Honest limits
-------------
* **Small.** Defaults are a handful of agents over a handful of rounds, because
  every agent-turn is a model call, and one arm of one island costs about two
  dollars and the better part of an hour. One island is an anecdote; the seed
  sweep that would make it evidence is the expensive part, and ``--islands``
  exists for when it is worth paying for.
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
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from barter.analysis import render as render_comparison  # noqa: E402
from barter.economy import autarky, draw_island, efficiency, exchange_ceiling  # noqa: E402
from barter.flow import play  # noqa: E402
from barter.llm import ARMS, TURN, Wire, brief_for, build_tools, tool_names  # noqa: E402
from barter.manager import Manager, ManagerService  # noqa: E402
from barter.run import score  # noqa: E402

from switchboard.testing import hub  # noqa: E402

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


async def _drive(options: object, prompt: str, budget: int | None = None) -> tuple[str, float]:
    """One agent turn. Returns its final text and what the turn cost.

    Running out of turns ends the agent's round; it does not end the run. An
    agent that spent its whole budget calling tools has simply used its time,
    the same as an agent that stopped early, and the goods it moved before
    running out are real. Letting that abort the island would silently bias the
    experiment toward whichever arm happens to be terser — and arm B, which has
    two extra tools to spend turns on, is exactly the talkative one.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

    if budget is not None:
        # Per-phase budgets, because the phases are not the same size of job.
        # Talking needs a handful of calls; a trading pass needs to look around
        # and act. Spending a trading budget on every phase was most of what
        # made the old flow expensive.
        options = replace(options, max_turns=budget)
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
    discovery: int = 3,
    turns_talk: int = 8,
    turns_produce: int = 8,
    turns_offer: int = 14,
    turns_settle: int = 8,
) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions

    island = draw_island(agents, goods, seed=seed)
    manager = Manager(island=island, phase="discovery" if discovery else "production")
    rng = random.Random(seed)
    run = f"llm{seed}{arm}"

    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run=run)
        service.claim()

        wires, options = {}, {}
        for agent_id in manager.agents:
            wire = Wire(agent_id=agent_id, client=handle.client(agent_id), service=service,
                        arm=arm, floor_channel=f"barter/{run}/floor",
                        quote_prefix=f"barter/{run}/quote/", goods=tuple(manager.goods))
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

        async def take_turn(agent_id: str, *, round_no: int, label: str,
                            note: str, budget: int) -> str:
            nonlocal cost
            prompt = TURN.format(round_no=round_no,
                                 rounds=discovery + 1 + rounds, phase_note=note)
            text, spent = await _drive(options[agent_id], prompt, budget)
            cost += spent
            if verbose:
                print(f"  [{round_no}/{label}] {agent_id}: {text[:140]}", file=sys.stderr)
            return text

        # The order of play lives in `barter/flow.py`, with nothing in it that
        # knows about models, so it can be exercised offline in milliseconds.
        # Both previous flow errors were found by paying for a run.
        played = await play(manager, take_turn, discovery=discovery, rounds=rounds,
                            rng=rng, drain=service.drain)
        transcript = played.transcript
        service.publish()

        floor = handle.client("reader").history(f"barter/{run}/floor", limit=500)
        board = {}
        for e in handle.client("reader").board_list(prefix=f"barter/{run}/quote/"):
            value = e.get("value")
            if isinstance(value, dict) and isinstance(value.get("prices"), dict):
                board[str(e["key"]).rsplit("/", 1)[-1]] = value["prices"]

    # Same scorer as Tier 1, against the same benchmarks, so the two tiers are
    # directly comparable rather than merely similar-looking.
    # Separate "the seller declined" from "the seller never got a turn". Only
    # the first is an economic result; the second is the harness losing trades
    # for us, and reporting them together would credit a flow artefact to the
    # arm's design.
    unseen = played.expired_unseen(manager)

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
        "quote_board": board,
        "quotes_posted": sum(w.quotes_posted for w in wires.values()),
        "expired_unseen": unseen,
        "flow": {"discovery": discovery, "trade_rounds": rounds, "passes": 2},
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
        f"  flow             {result['flow']['discovery']} talk + produce + "
        f"{result['flow']['trade_rounds']}x2 trade",
        f"  lost to flow     {result['expired_unseen']} offer(s) expired with the "
        f"seller never having had a turn",
        f"  said             {len(result['said'])} message(s)"
        + (f", {result['quotes_posted']} quote(s) posted" if result.get("quotes_posted") else ""),
    ]
    if result.get("quote_board"):
        lines.append("\n  final quote board (fish per unit)")
        for who, prices in sorted(result["quote_board"].items()):
            shown = "  ".join(f"{g} {v:g}" for g, v in prices.items())
            lines.append(f"    {who:>4}: {shown}")
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
    parser.add_argument("--arms", nargs="+", default=["told", "built"], choices=list(ARMS))
    parser.add_argument("--agents", type=int, default=5)
    parser.add_argument("--goods", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=8,
                        help="trading rounds (each is a propose pass and a settle pass)")
    parser.add_argument("--discovery", type=int, default=3,
                        help="rounds of talk before any labour is committed")
    parser.add_argument("--islands", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=18)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--compare", nargs="+", type=Path, default=None,
                        help="compare finished run records instead of running anything")
    args = parser.parse_args(argv)

    if args.compare:
        records = []
        for path in args.compare:
            records.extend(json.loads(path.read_text()))
        print(render_comparison(records))
        return 0

    results = []
    for arm in args.arms:
        for step in range(args.islands):
            result = asyncio.run(run_llm_island(
                arm=arm, agents=args.agents, goods=args.goods, rounds=args.rounds,
                seed=args.seed + step, model=args.model, max_turns=args.max_turns,
                verbose=args.verbose, discovery=args.discovery,
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
