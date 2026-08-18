"""Reading prices back out of a run, and asking whether they agree.

The efficiency numbers say how well an arm did. They do not say *why*, and for
the quoting arms the why turned out to be one number: whether the traders were
actually holding the same prices. `built` had a median in every reply and
finished 27x apart on cloth. That is the finding, so the code that computes it
belongs here with a test, not in a scratch file — the last thing computed once
and never gated silently threw away a paid run.

The awkward part, and the reason this is not a one-liner
--------------------------------------------------------
The arms keep their prices in different places, and that asymmetry *is* the
experiment. ``built`` and ``bound`` have a quote board, so a price is a number
under a key. ``told`` has only prose, so its prices exist inside sentences and
the only way to compare the arms is to read them back out the way a
counterparty would have to.

That extraction is a heuristic and is documented as one. It is deliberately
biased *toward* finding agreement rather than against it: it takes each agent's
most recent parseable quote, and it requires several goods to be named with
numbers before it will call something a price list at all — so a trade offer
("0.15 timber for grain") is not mistaken for a quote. If it errs, it errs by
missing a disagreement, which makes a large measured spread the conservative
reading.
"""

from __future__ import annotations

import re
from typing import Any

from .economy import GOOD_NAMES

#: A message has to name at least this many goods with numbers before it counts
#: as a price list. Two is not enough: "trading 0.6 salt for 1.3 fish" names two
#: goods and two numbers and is an offer, not a quote.
_MIN_GOODS_FOR_A_QUOTE = 3

#: Prices outside this range are parse errors rather than quotes — a trade id
#: ("t14") or a holdings readout should not become a price.
_PLAUSIBLE = (0.0, 1e4)


def prices_from_prose(messages: list[dict[str, Any]],
                      goods: tuple[str, ...] = GOOD_NAMES) -> dict[str, dict[str, float]]:
    """Best-effort price vectors from what agents said, keyed by agent.

    Keeps each agent's *last* parseable quote, because agents revise and the
    question is what they finished believing. Returns only agents that produced
    something quote-shaped at all — how many did is itself worth reporting, and
    in the ``told`` island it was three of four.
    """
    latest: dict[str, dict[str, float]] = {}
    for message in messages:
        text = str(message.get("text", ""))
        who = str(message.get("from", "?"))
        found: dict[str, float] = {}
        for good in goods:
            # "cloth 0.21", "cloth: 0.21", "cloth=0.21" — the forms that
            # actually showed up. Anything more elaborate is not worth
            # pretending we can parse reliably.
            hit = re.search(rf"\b{good}\b\s*[:=]?\s*([0-9]*\.?[0-9]+)", text, re.I)
            if not hit:
                continue
            try:
                value = float(hit.group(1))
            except ValueError:
                continue
            if _PLAUSIBLE[0] < value < _PLAUSIBLE[1]:
                found[good] = value
        if len(found) >= _MIN_GOODS_FOR_A_QUOTE:
            latest[who] = found
    return latest


def price_spread(vectors: dict[str, dict[str, float]]) -> dict[str, float]:
    """Max/min per good across traders. 1.0 means everybody agrees.

    A ratio rather than a variance because prices here span orders of magnitude
    and the interesting statement is "these two traders are 27x apart", which no
    dispersion measure in absolute units can say across goods of different
    scales.

    Goods only one trader quoted are omitted: a price nobody else named is not
    an agreement or a disagreement.
    """
    goods = {good for prices in vectors.values() for good in prices}
    out: dict[str, float] = {}
    for good in sorted(goods):
        values = [prices[good] for prices in vectors.values() if good in prices]
        if len(values) > 1 and min(values) > 0:
            out[good] = max(values) / min(values)
    return out


def specialisation(island: Any, shares: list[list[float]],
                   prices: dict[str, float], goods: tuple[str, ...]) -> float | None:
    """How much like a price-taker the island is producing, at ``prices``.

    Revenue actually earned by the labour spent, over the most it could have
    earned. 1.0 means every unit of labour went to the good paying best for that
    agent; the autarky spread scores well below it.

    This is the production half of the convergence question, and the half no arm
    has ever moved. It is measured *at the prices agents currently believe*
    rather than at the true equilibrium, deliberately — the question is whether
    agents act on what they have agreed, not whether what they agreed was right.
    """
    earned = 0.0
    best = 0.0
    for i in range(island.n_agents):
        spent = sum(shares[i])
        if spent <= 1e-12:
            continue
        rates = [prices.get(goods[g], 0.0) * island.capacity[i][g]
                 for g in range(island.n_goods)]
        if max(rates) <= 0:
            continue
        earned += sum(rates[g] * shares[i][g] for g in range(island.n_goods))
        best += max(rates) * spent
    return earned / best if best > 1e-12 else None


def concentration(shares: list[list[float]]) -> float | None:
    """How specialised production is, without reference to any price.

    A labour-weighted Herfindahl: 1/k when an agent spreads evenly across k
    goods, 1.0 when it puts everything on one. Reported beside
    ``specialisation`` because that one is measured at prices agents may simply
    have wrong — this one says whether they committed to *anything*, which is a
    different question and a check on the first.
    """
    total = 0.0
    weight = 0.0
    for row in shares:
        spent = sum(row)
        if spent <= 1e-12:
            continue
        total += spent * sum((s / spent) ** 2 for s in row)
        weight += spent
    return total / weight if weight > 1e-12 else None


def snapshot(island: Any, manager: Any, prices: dict[str, float] | None,
             *, round_no: int, label: str = "") -> dict[str, Any]:
    """One row of the trajectory: where the island stands right now.

    The point of recording this per round rather than once at the end is the
    conflation that rolling labour introduces. Price convergence and production
    convergence become coupled, and the only way to keep that legible is to
    watch both and read the *lag* between them — "production followed prices two
    rounds later" is a finding, where a single end-state number would just be a
    confound.

    It also turns one island into a trajectory rather than a point, which is
    some help against the run-to-run variance that made single-arm comparisons
    unusable.
    """
    from .economy import efficiency as _efficiency

    goods = tuple(manager.goods)
    ordered = sorted(manager.agents.values(), key=lambda s: s.index)
    shares = [list(s.shares) for s in ordered]
    utilities = manager.utilities()

    scored = _efficiency(island, utilities)
    settled = sum(1 for t in manager.trades.values() if t.status == "executed")
    return {
        "round": round_no,
        "label": label,
        # None rather than 0.0 while anybody still holds none of something: a
        # zero here is "not yet scoreable", not "scored badly", and averaging
        # the two together is how a trajectory starts lying.
        "efficiency": None if scored.ruined else round(scored.lower, 4),
        "holding_nothing": len(scored.ruined),
        "price_agreement": _price_agreement(prices),
        "specialisation": (round(s, 4) if (s := specialisation(
            island, shares, consensus(prices), goods)) is not None else None),
        "concentration": (round(c, 4) if (c := concentration(shares)) is not None else None),
        "labour_spent": round(sum(st.spent for st in ordered) / len(ordered), 4),
        "settled": settled,
    }


def consensus(prices: Any) -> dict[str, float]:
    """One price vector from whatever shape arrived.

    A board is one vector per trader and a settled convention is a single
    vector; ``specialisation`` needs exactly one, so a board is collapsed to its
    per-good median — the same number the ``bound`` machinery shows agents.

    This exists because the collapse was missing. ``snapshot`` handed the board
    straight through, ``specialisation`` looked up goods in a dict keyed by
    agent, found none, and returned ``None`` every round. The trajectory came
    back with an empty specialisation column and no error, which is the quiet
    kind of wrong: a run that costs money and reports nothing about the one
    thing it was built to measure.
    """
    if not prices:
        return {}
    if not isinstance(next(iter(prices.values())), dict):
        return dict(prices)
    goods: dict[str, list[float]] = {}
    for vector in prices.values():
        for good, value in vector.items():
            goods.setdefault(good, []).append(float(value))
    out = {}
    for good, values in goods.items():
        ordered = sorted(values)
        mid = len(ordered) // 2
        out[good] = (ordered[mid] if len(ordered) % 2
                     else (ordered[mid - 1] + ordered[mid]) / 2)
    return out


def _price_agreement(prices: Any) -> float | None:
    """Worst max/min ratio across traders. ``None`` when nobody has quoted.

    Two shapes arrive here and they mean different things. A *board* is one
    vector per trader, and the spread across them is the measurement. A single
    flat vector is one price everybody holds — a convention that has already
    settled — and its agreement is 1.0 by construction, not undefined. Returning
    ``None`` for that case would make a fully converged island look like an
    island where nobody spoke.
    """
    if not prices:
        return None
    if not isinstance(next(iter(prices.values())), dict):
        return 1.0
    spread = price_spread(prices)
    return max(spread.values()) if spread else None


def trajectory_table(rows: list[dict[str, Any]]) -> str:
    """The convergence pair, side by side, which is the whole point of it."""
    out = [f"{'round':>6}{'pass':>9}{'agree':>8}{'special':>9}{'concen':>8}"
           f"{'labour':>8}{'eff':>8}{'settled':>9}"]
    for row in rows:
        agree = f"{row['price_agreement']:.1f}x" if row.get("price_agreement") else "-"
        out.append(
            f"{row['round']:>6}{row.get('label', ''):>9}{agree:>8}"
            f"{_num(row.get('specialisation')):>9}{_num(row.get('concentration')):>8}"
            f"{_num(row.get('labour_spent')):>8}{_num(row.get('efficiency')):>8}"
            f"{row.get('settled', 0):>9}"
        )
    return "\n".join(out)


def _num(value: Any) -> str:
    return f"{value:.3f}" if isinstance(value, (int, float)) else "-"


def quoted_prices(record: dict[str, Any]) -> tuple[dict[str, dict[str, float]], str]:
    """Whatever prices this run has, and where they came from.

    Prefers the quote board when the arm had one, since a board is what an agent
    actually posted rather than what a regex thinks it said. Falls back to prose
    so that a boardless arm lands on the same axis.
    """
    board = record.get("quote_board") or {}
    if board:
        return board, "board"
    return prices_from_prose(record.get("said") or []), "prose"


def summarise(record: dict[str, Any]) -> dict[str, Any]:
    """One arm's outcome, with the price-agreement measure beside it."""
    vectors, source = quoted_prices(record)
    spread = price_spread(vectors)
    summary = record.get("summary") or {}
    return {
        "arm": record.get("arm"),
        "efficiency": None if record.get("ruined") else (record.get("efficiency") or [None])[0],
        "ruined": len(record.get("ruined") or []),
        "own_plan": (record.get("own_plan") or [None])[0],
        "worst_ratio": record.get("worst_ratio"),
        "executed": summary.get("executed"),
        "proposed": summary.get("proposed"),
        "messages": len(record.get("said") or []),
        "quotes_posted": record.get("quotes_posted", 0),
        "price_source": source,
        "traders_quoting": len(vectors),
        "spread": spread,
        # The single number the quoting arms turn on. None when nobody quoted,
        # which is itself the answer for `silent` and `free`.
        "worst_spread": max(spread.values()) if spread else None,
    }


def render(records: list[dict[str, Any]]) -> str:
    """Side-by-side table, plus each arm's price agreement broken out."""
    rows = [summarise(r) for r in records]
    lines = [
        f"{'arm':<8}{'eff':>8}{'own plan':>10}{'worst':>7}{'settled':>10}"
        f"{'msgs':>6}{'quotes':>8}{'agree':>9}",
    ]
    for row in rows:
        eff = f"ruined {row['ruined']}" if row["ruined"] else f"{row['efficiency']:.3f}"
        settled = f"{row['executed']}/{row['proposed']}"
        agree = f"{row['worst_spread']:.1f}x" if row["worst_spread"] else "-"
        lines.append(
            f"{row['arm']:<8}{eff:>8}{row['own_plan']:>10.3f}{row['worst_ratio']:>7.2f}"
            f"{settled:>10}{row['messages']:>6}{row['quotes_posted']:>8}{agree:>9}"
        )
    lines += [
        "",
        "agree = worst max/min price ratio across traders. 1.0x would be a genuinely",
        "        shared price; '-' means nobody quoted anything parseable.",
    ]
    for row in rows:
        if not row["spread"]:
            continue
        detail = "  ".join(f"{g} {v:.1f}x" for g, v in sorted(row["spread"].items()))
        lines.append(f"\n{row['arm']} ({row['price_source']}, "
                     f"{row['traders_quoting']} quoting): {detail}")
    return "\n".join(lines)
