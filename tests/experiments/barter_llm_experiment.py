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
import contextlib
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from barter.analysis import render as render_comparison  # noqa: E402
from barter.analysis import snapshot, trajectory_table  # noqa: E402
from barter.economy import autarky, draw_island, efficiency, exchange_ceiling  # noqa: E402
from barter.flow import play  # noqa: E402
from barter.llm import (  # noqa: E402
    ARMS,
    TURN,
    Telling,
    Wire,
    brief_for,
    build_tools,
    compose,
    tool_names,
)
from barter.manager import Manager, ManagerService  # noqa: E402
from barter.run import score  # noqa: E402

from switchboard.testing import hub  # noqa: E402

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _only(allowed: list[str]) -> Any:
    """Permission callback: yes to this island's tools, no to everything else.

    The island is meant to be an agent's whole world — no filesystem, no shell,
    no network beyond the manager. `allowed_tools` expresses that, but on its
    own it only means "do not offer these"; something the model reaches for
    anyway becomes a question, and a question with no terminal behind it is a
    hang rather than a refusal. Denying in-process turns that into an ordinary
    tool error the agent can read and route around.
    """
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    permitted = set(allowed)

    async def decide(tool_name: str, _input: Any, _ctx: Any) -> Any:
        if tool_name in permitted:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=f"{tool_name} does not exist on this island. "
                    f"You have: {', '.join(sorted(n.rsplit('__', 1)[-1] for n in permitted))}.")

    return decide


class Trader:
    """One agent, one session, for the whole island.

    Each turn used to be a fresh ``query()``, which was a mistake with two
    costs. The obvious one is money: the system prompt and all ten tool schemas
    were re-sent sixty times an island, cached against nothing.

    The one that matters is that the agent had **amnesia**. It could not recall
    the price it posted last round, the trade it proposed, or anything a peer
    had said — every turn it woke up with the brief and had to rediscover the
    world through tools. Worse, ``listen`` is a cursor: messages read in round
    three were gone permanently, because nothing remembered them and the cursor
    had moved past. An agent that cannot remember a convention cannot keep one,
    and results collected that way are about forgetting at least as much as
    about coordination.

    Statelessness was never a design decision here; it fell out of reaching for
    the one-shot helper. A session per agent is what the experiment always
    meant.
    """

    def __init__(self, agent_id: str, options: Any) -> None:
        self.agent_id = agent_id
        self.options = options
        self.client: Any = None
        self.cost = 0.0
        self.turns = 0

    async def connect(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        self.client = ClaudeSDKClient(self.options)
        await self.client.connect()

    async def close(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.disconnect()

    async def take_turn(self, prompt: str) -> str:
        """One turn on the running conversation.

        Running out of turns ends this turn, not the island: an agent that spent
        its budget on tool calls has used its time like any other, and the goods
        it moved before running out are real.
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        said = []
        try:
            await self.client.query(prompt)
            async for message in self.client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            said.append(block.text)
                elif isinstance(message, ResultMessage):
                    # Assigned, not accumulated. On a persistent session
                    # `total_cost_usd` is the running total for the whole
                    # session, not the cost of this turn — a probe showed one
                    # agent reporting 0.0211 then 0.0299 across two turns.
                    # Adding those would have double-counted every island's
                    # spend, and increasingly so the longer the session ran.
                    self.cost = message.total_cost_usd or self.cost
        except Exception as exc:  # noqa: BLE001 - the turn is over either way
            if "maximum number of turns" not in str(exc):
                raise
            said.append("[turn budget exhausted]")
        self.turns += 1
        return "\n".join(said)


async def run_llm_island(
    *,
    arm: str,
    telling: Telling,
    agents: int,
    goods: int,
    rounds: int,
    seed: int,
    model: str,
    max_turns: int = 240,
    verbose: bool,
    discovery: int = 3,
) -> dict:
    """One island. ``telling`` is the whole information setup; ``arm`` only names it.

    Everything an agent is handed comes off ``telling`` — the prompt paragraphs,
    the tool surface, and the sentences in the turn note — so the record can say
    which switches were on rather than only which rung of the ladder was run.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    island = draw_island(agents, goods, seed=seed)
    # Rolling labour spreads the *same* one unit across the production round and
    # every trading round, so the frontier and both benchmarks are untouched and
    # a rolling island is directly comparable to a one-shot one.
    rolling = telling.rolling
    instalments = 1 + rounds if rolling else 1
    manager = Manager(
        island=island,
        phase="discovery" if discovery else "production",
        labour_per_round=1.0 / instalments,
        rolling=rolling,
    )
    rng = random.Random(seed)
    run = f"llm{seed}{arm}"

    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run=run)
        service.claim()

        wires, traders = {}, {}
        for agent_id in manager.agents:
            wire = Wire(agent_id=agent_id, client=handle.client(agent_id), service=service,
                        telling=telling, floor_channel=f"barter/{run}/floor",
                        quote_prefix=f"barter/{run}/quote/", goods=tuple(manager.goods))
            wires[agent_id] = wire
            traders[agent_id] = Trader(agent_id, ClaudeAgentOptions(
                model=model,
                system_prompt=brief_for(island, manager, agent_id, telling),
                mcp_servers={f"island-{agent_id}": build_tools(wire)},
                # An allowlist of exactly this agent's island tools. No
                # filesystem, no shell, no web — the island is the whole world
                # an agent can act on, so anything it achieves came through the
                # manager or through talking to a peer. `setting_sources=[]`
                # keeps the surrounding repo's own settings, skills and hooks
                # out of the run, which would otherwise vary the experiment with
                # whatever happens to be checked out.
                allowed_tools=tool_names(telling, agent_id),
                # Answer permission questions in-process. Without this the CLI
                # asks about anything outside the allowlist and waits on a stdin
                # nobody is attached to — the session simply stops, with the
                # event loop idle and no error to read. That is survivable when
                # each turn is a throwaway subprocess and fatal once a session
                # has to live for a whole island.
                can_use_tool=_only(tool_names(telling, agent_id)),
                # Session-wide, not per turn: the session is now the whole
                # island. Sized so no agent can starve itself early, with the
                # per-phase shaping moved into the turn note instead.
                max_turns=max_turns,
                setting_sources=[],
            ))
        for trader in traders.values():
            await trader.connect()

        async def take_turn(agent_id: str, *, round_no: int, label: str,
                            note: str, budget: int) -> str:
            hint = (f" Keep this turn to about {budget} tool calls."
                    if budget else "")
            prompt = TURN.format(round_no=round_no,
                                 rounds=discovery + 1 + rounds,
                                 phase_note=note + hint)
            text = await traders[agent_id].take_turn(prompt)
            if verbose:
                print(f"  [{round_no}/{label}] {agent_id}: {text[:140]}", file=sys.stderr)
            return text

        def observe(round_no: int, label: str) -> dict | None:
            # Only after a pass that could have changed something. Snapshotting
            # inside a pass would read a half-applied round.
            if label not in ("talk", "produce", "settle"):
                return None
            live = {}
            for entry in handle.client("reader").board_list(prefix=f"barter/{run}/quote/"):
                value = entry.get("value")
                if isinstance(value, dict) and isinstance(value.get("prices"), dict):
                    live[str(entry["key"]).rsplit("/", 1)[-1]] = value["prices"]
            if not live:
                # Boardless arms keep their prices in sentences, so read them
                # back out the way a counterparty would have to.
                from barter.analysis import prices_from_prose
                said = [m["body"] for m in
                        handle.client("reader").history(f"barter/{run}/floor", limit=500)
                        if isinstance(m.get("body"), dict)]
                live = prices_from_prose(said)
            return snapshot(island, manager, live, round_no=round_no, label=label)

        # The order of play lives in `barter/flow.py`, with nothing in it that
        # knows about models, so it can be exercised offline in milliseconds.
        # Both previous flow errors were found by paying for a run.
        try:
            # `labour` is injected rather than read: `flow` must not know what a
            # Manager is, and this is the one per-agent number the turn note
            # carries.
            notes = telling.to_notes()
            notes.labour = lambda who: max(0.0, 1.0 - manager.agents[who].spent)
            played = await play(manager, take_turn, discovery=discovery, rounds=rounds,
                                rng=rng, drain=service.drain, notes=notes,
                                on_round=observe)
        finally:
            for trader in traders.values():
                await trader.close()
        transcript = played.transcript
        cost = sum(t.cost for t in traders.values())
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
        # The switches, not just the name. A record that says only "bound" cannot
        # be pooled with one run at `bound` minus `expiry`, and attributing a
        # result to a switch is the entire reason they are switches.
        "telling": {name: getattr(telling, name) for name in telling.__dataclass_fields__},
        "switches": list(telling.switches()),
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
        "trajectory": played.trajectory,
        "flow": {"discovery": discovery, "trade_rounds": rounds, "passes": 2,
                 "labour": "rolling" if rolling else "once",
                 "instalments": instalments},
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
        f"{result['flow']['trade_rounds']}x2 trade, labour "
        f"{result['flow']['labour']} ({result['flow']['instalments']} instalment(s))",
        f"  lost to flow     {result['expired_unseen']} offer(s) expired with the "
        f"seller never having had a turn",
        f"  switches         {', '.join(result.get('switches') or ['none'])}",
        f"  said             {len(result['said'])} message(s)"
        + (f", {result['quotes_posted']} quote(s) posted" if result.get("quotes_posted") else ""),
    ]
    if result.get("quote_board"):
        lines.append("\n  final quote board (fish per unit)")
        for who, prices in sorted(result["quote_board"].items()):
            shown = "  ".join(f"{g} {v:g}" for g, v in prices.items())
            lines.append(f"    {who:>4}: {shown}")
    if result.get("trajectory"):
        lines.append("\n  trajectory")
        lines += ["    " + line for line in trajectory_table(result["trajectory"]).splitlines()]
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
    parser.add_argument("--labour", choices=["once", "rolling"], default="once",
                        help="one-shot commitment, or the same total labour in "
                             "instalments across rounds")
    parser.add_argument("--with", dest="switch_on", nargs="+", default=[], metavar="SWITCH",
                        help="turn these switches on, on top of the named arm")
    parser.add_argument("--without", dest="switch_off", nargs="+", default=[],
                        metavar="SWITCH",
                        help="turn these switches off. `--arms bound --without expiry` "
                             "isolates the deviation report from staleness, which no "
                             "rung of the ladder does on its own")
    parser.add_argument("--discovery", type=int, default=3,
                        help="rounds of talk before any labour is committed")
    parser.add_argument("--islands", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-turns", type=int, default=240,
                        help="turns for the whole session, not per round — an agent "
                             "holds one session for the entire island")
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

    # `--labour rolling` is a switch like any other; it keeps its own flag only
    # because it is also a fact about the manager rather than only a sentence.
    switch_on = list(args.switch_on) + (["rolling"] if args.labour == "rolling" else [])

    results = []
    for arm in args.arms:
        telling = compose(arm, on=switch_on, off=args.switch_off)
        label = arm
        if switch_on or args.switch_off:
            label += "".join(f"+{s}" for s in switch_on)
            label += "".join(f"-{s}" for s in args.switch_off)
        for step in range(args.islands):
            result = asyncio.run(run_llm_island(
                arm=label, telling=telling,
                agents=args.agents, goods=args.goods, rounds=args.rounds,
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
