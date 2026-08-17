"""The order of play, with nothing in it that knows about models.

Extracted so it can be tested for free. The flow is where the last two design
errors lived — production committed before anyone had spoken, and one turn per
agent for both proposing and answering — and both were found by paying for a
run and reading the wreckage afterwards. A loop that costs $2 and fifty minutes
to exercise is a loop nobody exercises.

``play`` takes a ``take_turn`` callable and never imports an SDK, so the whole
order of play runs against a scripted stand-in in milliseconds. What remains in
the runner is the part that genuinely needs a model.

The shape, which is Tier 1's:

    discovery x D    talk, quote, read — the manager accepts nothing
    produce   x 1    labour committed, now with something to go on
    trade     x T    two passes: everyone offers, then everyone answers
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


class TakeTurn(Protocol):
    """One agent acting once. Returns whatever it said, for the transcript."""

    def __call__(self, agent_id: str, *, round_no: int, label: str,
                 note: str, budget: int) -> Awaitable[str]: ...


@dataclass
class Budgets:
    """Tool calls allowed per turn, per phase.

    Not one number, because the phases are not the same size of job. Talking
    needs a handful of calls; a trading pass needs to look around and act.
    Spending a trading budget on every phase is most of what made the previous
    flow expensive, and paying for the extra settle pass out of that slack is
    close to cost-neutral.
    """

    talk: int = 8
    produce: int = 8
    offer: int = 14
    settle: int = 8


@dataclass
class Played:
    transcript: list[dict[str, Any]] = field(default_factory=list)
    #: For each trade id, which agents have had a turn while it was pending.
    #: An offer that expires without its seller ever having had a turn is the
    #: harness losing a trade, not a refusal, and the two must never be counted
    #: together — one is a result about the arm and the other is a result about
    #: this file.
    seen_by: dict[str, set[str]] = field(default_factory=dict)
    rounds_played: int = 0

    def expired_unseen(self, manager: Any) -> int:
        return sum(
            1 for trade in manager.trades.values()
            if trade.status == "expired"
            and trade.seller not in self.seen_by.get(trade.id, set())
        )


async def play(
    manager: Any,
    take_turn: TakeTurn,
    *,
    discovery: int,
    rounds: int,
    rng: random.Random,
    budgets: Budgets | None = None,
    drain: Callable[[], Any] | None = None,
    total_label: int | None = None,
    rolling: bool = False,
) -> Played:
    """Run one island's order of play. Returns the transcript and diagnostics."""
    budgets = budgets or Budgets()
    played = Played()
    order = list(manager.agents)
    total = total_label if total_label is not None else discovery + 1 + rounds
    round_no = 0

    async def pass_over(label: str, note: str, budget: int) -> None:
        nonlocal round_no
        rng.shuffle(order)
        for agent_id in order:
            said = await take_turn(agent_id, round_no=round_no, label=label,
                                   note=note, budget=budget)
            played.transcript.append({"round": round_no, "pass": label,
                                      "agent": agent_id, "text": said})
            # Record *after* the turn: an agent that proposed to itself during
            # this turn has still had its chance to answer what was already open.
            for trade in manager.trades.values():
                if trade.status == "pending":
                    played.seen_by.setdefault(trade.id, set()).add(agent_id)
        if drain is not None:
            drain()

    for _ in range(discovery):
        round_no += 1
        await pass_over(
            "talk",
            f"Nothing is committed yet. Production opens in "
            f"{discovery - round_no + 1} round(s), and "
            + ("you spend your labour in instalments from then on, so you can "
               "revise as you learn. " if rolling else "you spend your labour once. ")
            + "Use this time however you think best.",
            budgets.talk,
        )

    round_no += 1
    if manager.phase == "discovery":
        manager.open_production()
    await pass_over(
        "produce",
        ("Production is open. You have a share of your labour to spend now and "
         "more in each round that follows, so you can change your mind as you "
         "learn. Trading opens next round."
         if rolling else
         "Production is open and this is the only round in which you can call "
         "`produce`. Trading opens next round."),
        budgets.produce,
    )
    manager.check_conservation()
    manager.open_trading()
    manager.check_conservation()

    for _ in range(rounds):
        round_no += 1
        left = total - round_no
        await pass_over(
            "offer",
            ("Trading is open, and you still have labour to spend — `produce` "
             "once more this round if you want to, and `my_state` shows how much "
             "is left. " if rolling else "Trading is open. ")
            + f"{left} round(s) remain after this one.",
            budgets.offer)
        await pass_over("settle",
                        "Same round, second pass: answer what is waiting on you. "
                        f"{left} round(s) remain after this one.",
                        budgets.settle)
        manager.advance()
        manager.check_conservation()

    manager.close()
    manager.check_conservation()
    played.rounds_played = round_no
    return played
