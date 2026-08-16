"""Tier 1: what does an agreed convention buy a market that can already talk?

Not collected by pytest (no ``test_`` prefix) — a seeded, offline experiment,
not a gate. The gates live in ``tests/test_barter.py``. Run it directly::

    python tests/experiments/barter_experiment.py
    python tests/experiments/barter_experiment.py --islands 40 --json out.json

The setting
-----------
An island of 12 agents and 5 goods. Every agent has an independent production
capacity for every good and one unit of labour to split between them, so what
it makes is a choice; every agent has Cobb-Douglas tastes, so it wants some of
everything. A manager holds all the state and is the only thing that can move a
quantity: a buyer proposes a trade and gets an id back, the named seller
approves that id, and only then do goods change hands.

The question
------------
Not "does trade help" — of course it does. The question is what specifically
has to be *shared* for it to help, and the arms are an information ladder that
takes one thing away at a time:

    A  silent     agents can call the manager and nothing else
    B  disclose   a public channel, everyone posts their marginal values
    C  price      same channel, same information, plus an agreed way to read it
                  into one public price
    D  money      arm C plus one clause: settle in the numeraire, and take it
                  past your own appetite

B against C holds information constant and varies only the protocol. C against
D holds the protocol constant and varies only what the numeraire is *for*.

Why the scoring is a bracket
----------------------------
Each outcome is scored by how far it sits inside the Pareto frontier: 1.0 means
no reallocation could make everyone better off, 0.5 means everyone could have
had twice as much of everything. The number comes back as a certified interval,
proved from an allocation on one side and from prices on the other, so a run
that failed to converge is visible as a wide bracket instead of a confident
wrong number.

Two things are reported next to it and neither is optional. **Where on the
frontier** an arm lands is a different question from how close it got, and a
convention moves the two independently — an arm can be perfectly efficient and
still have wrecked somebody, which is what ``worst`` is for. And **ruin** is
reported as a count, never averaged in: an agent holding none of some good has
zero Cobb-Douglas utility, and a mean that swallows a zero is a mean that hides
the only outcome anyone would actually care about.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from barter.economy import autarky, draw_island, efficiency, exchange_ceiling  # noqa: E402
from barter.run import run_island  # noqa: E402

ARMS = ("A", "B", "C", "D")
ARM_NAMES = {"A": "silent", "B": "disclose", "C": "price", "D": "money"}


def sweep(islands: int, *, agents: int, goods: int, seed0: int = 1,
          rounds: int | None = None) -> dict:
    rows: dict[str, list] = {arm: [] for arm in ARMS}
    floors, ceilings = [], []
    for step in range(islands):
        seed = seed0 + step
        island = draw_island(agents, goods, seed=seed)
        _, autarky_utils = autarky(island)
        floors.append(efficiency(island, autarky_utils).lower)
        ceilings.append(exchange_ceiling(island).lower)
        for arm in ARMS:
            rows[arm].append(run_island(island, arm, seed=seed, trade_rounds=rounds))
        print(f"  island {step + 1}/{islands} (seed {seed})", end="\r", file=sys.stderr)
    print(" " * 40, end="\r", file=sys.stderr)
    return {"rows": rows, "floors": floors, "ceilings": ceilings}


#: Round budgets for the convergence table. Chosen to span from "barely enough
#: to swap once" to "long past where anything is still moving".
ROUND_BUDGETS = (15, 30, 60, 120, 240)


def rounds_sweep(islands: int, *, agents: int, goods: int, seed0: int = 1) -> str:
    """Trace each arm against the trading-round budget.

    The single most important thing this table shows is *which failures heal
    with time and which do not*. An arm that stalls at a fixed ruin rate however
    long it runs has a structural problem — there is a trade it needs that no
    amount of patience will produce. An arm whose ruin rate falls steadily is
    merely slow. Those two look identical at any single budget, which is exactly
    why quoting one is not good enough.
    """
    lines = [
        "",
        "Against the round budget",
        "------------------------",
        "Median efficiency over unruined islands, and islands with any ruin.",
        "",
        f"{'ROUNDS':>7}" + "".join(f"{arm + ' ' + ARM_NAMES[arm]:>18}" for arm in ARMS),
    ]
    for budget in ROUND_BUDGETS:
        result = sweep(islands, agents=agents, goods=goods, seed0=seed0, rounds=budget)
        cells = []
        for arm in ARMS:
            rows = result["rows"][arm]
            clean = [r.efficiency.lower for r in rows if not r.efficiency.ruined]
            ruined = sum(1 for r in rows if r.efficiency.ruined)
            med = f"{statistics.median(clean):.3f}" if clean else "  -  "
            cells.append(f"{med} ruin {ruined}/{len(rows)}".rjust(18))
        lines.append(f"{budget:>7}" + "".join(cells))
        print(f"  rounds {budget} done", file=sys.stderr)
    return "\n".join(lines)


def _stat(values: list[float]) -> tuple[float, float, float]:
    if not values:
        return (float("nan"),) * 3
    ordered = sorted(values)
    return (ordered[0], statistics.median(ordered), ordered[-1])


def report(result: dict, *, agents: int, goods: int, islands: int) -> str:
    out = [
        f"Island barter — {agents} agents, {goods} goods, {islands} islands",
        "",
        f"  autarky floor    {statistics.median(result['floors']):.3f}"
        "   (nobody trades)",
        f"  exchange ceiling {statistics.median(result['ceilings']):.3f}"
        "   (trade perfectly, but keep making what you would have made alone)",
        "  frontier         1.000",
        "",
        "Efficiency is distance to the Pareto frontier; ruin means an agent ended",
        "with none of some good, which is zero utility and is never averaged in.",
        "",
        f"{'ARM':<12} {'EFFICIENCY (med)':>17} {'min':>6} {'max':>6} "
        f"{'RUINED':>7} {'WORST':>6} {'TRADES':>7} {'MSGS':>6}",
    ]
    for arm in ARMS:
        rows = result["rows"][arm]
        clean = [r for r in rows if not r.efficiency.ruined]
        effs = [r.efficiency.lower for r in clean]
        lo, med, hi = _stat(effs)
        ruined = sum(1 for r in rows if r.efficiency.ruined)
        worst = min((r.worst_ratio for r in clean), default=float("nan"))
        out.append(
            f"{arm + ' ' + ARM_NAMES[arm]:<12} {med:>17.3f} {lo:>6.3f} {hi:>6.3f} "
            f"{f'{ruined}/{len(rows)}':>7} {worst:>6.2f} "
            f"{statistics.median([r.executed for r in rows]):>7.0f} "
            f"{statistics.median([r.messages for r in rows]):>6.0f}"
        )
    out += [
        "",
        "EFFICIENCY  median over islands where nobody was ruined. Ruined islands are",
        "            excluded from the median and counted in RUINED instead.",
        "RUINED      islands where at least one agent finished holding none of some",
        "            good. This is the cost of specialising on a promise.",
        "WORST       worst single agent's final utility as a multiple of its autarky",
        "            utility, across all islands. Below 1.0 means somebody would have",
        "            done better never trading.",
    ]
    return "\n".join(out)


def commentary(result: dict) -> str:
    rows = result["rows"]

    def med(arm: str, attr: str) -> float:
        clean = [r for r in rows[arm] if not r.efficiency.ruined]
        return statistics.median([getattr(r, attr) for r in clean]) if clean else float("nan")

    def med_own(arm: str) -> float:
        clean = [r for r in rows[arm] if not r.exchange_efficiency.ruined]
        values = [r.exchange_efficiency.lower for r in clean]
        return statistics.median(values) if values else float("nan")

    ruin = {arm: sum(1 for r in rows[arm] if r.efficiency.ruined) for arm in ARMS}
    verdict_b = ("better" if med("B", "capture_lo") > med("A", "capture_lo")
                 else "no better, often worse")
    n = len(rows["A"])
    lines = [
        "",
        "What the arms say",
        "-----------------",
        f"A silent    swaps almost perfectly — {med_own('A'):.2f} of the best possible",
        "            allocation of what it chose to make — and still lands near the",
        "            exchange ceiling, because it never changes what it makes. Trading",
        "            skill is not the binding constraint; knowing what to produce is.",
        f"B disclose  publishes real information and does {verdict_b},",
        f"            settling {med('B', 'executed'):.0f} trades against A's"
        f" {med('A', 'executed'):.0f}. Disclosure without an",
        "            agreed reading gives every agent a slightly different price, so",
        "            they specialise on beliefs that do not match and then cannot",
        "            trade with each other. Talking is not free.",
        "C price     reaches the frontier exactly when it works, and ruins somebody on",
        f"            {ruin['C']}/{n} islands. A shared price is what turns disclosure into",
        "            specialisation that pays — and specialisation is a commitment,",
        "            so when settlement then fails the loss is total rather than small.",
        f"D money     ruins somebody on {ruin['D']}/{n} islands. The clause it adds over C is",
        "            not about prices at all: it is that the numeraire is accepted",
        "            past the point of wanting it. A common price says what a fair",
        "            swap is; it does not make the agent holding the fish want your",
        "            cloth. Only a medium of exchange removes the need for it to.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--islands", type=int, default=12)
    parser.add_argument("--agents", type=int, default=12)
    parser.add_argument("--goods", type=int, default=5)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=None,
                        help="trading rounds per island (default: the module default)")
    parser.add_argument("--rounds-sweep", action="store_true",
                        help="also trace every arm against the round budget")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    result = sweep(args.islands, agents=args.agents, goods=args.goods, seed0=args.seed,
                   rounds=args.rounds)
    print(report(result, agents=args.agents, goods=args.goods, islands=args.islands))
    print(commentary(result))
    if args.rounds_sweep:
        print(rounds_sweep(args.islands, agents=args.agents, goods=args.goods,
                           seed0=args.seed))

    if args.json:
        args.json.write_text(json.dumps({
            "agents": args.agents, "goods": args.goods, "islands": args.islands,
            "floors": result["floors"], "ceilings": result["ceilings"],
            "arms": {
                arm: [{
                    "seed": r.seed, "efficiency": [r.efficiency.lower, r.efficiency.upper],
                    "own_plan": [r.exchange_efficiency.lower, r.exchange_efficiency.upper],
                    "ruined": list(r.efficiency.ruined), "worst_ratio": r.worst_ratio,
                    "executed": r.executed, "proposed": r.proposed,
                    "rejected": r.rejected, "messages": r.messages,
                } for r in result["rows"][arm]]
                for arm in ARMS
            },
        }, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
