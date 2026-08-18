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

What the turn note says is not fixed here. Every sentence an agent is handed is
one switch on ``Notes``, because a sentence that is always present is a sentence
whose contribution can never be measured — see ``barter/llm.py`` for the same
rule applied to the system prompt and the tool surface.
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
class Notes:
    """What the turn note tells an agent, one switch per thing told.

    The turn note is the second channel into an agent, after the system prompt,
    and it had accumulated the same problem: several distinct pieces of
    information welded into one paragraph that was either present or absent as a
    block. A run could then say that agents did better with rolling labour, and
    could not say whether that was the *mechanism* — labour actually being
    spendable later — or the *sentence* telling them so.

    So each is separate. ``rolling`` is the mechanism and lives in the manager;
    the fields around it are only what the agent is told about the world it is
    already in, and any of them can be switched off while the world stays put.
    """

    #: "N round(s) remain after this one." A horizon to plan against. Off, an
    #: agent knows the game ends but not when, which is the difference between
    #: a deadline and an open-ended one.
    horizon: bool = True
    #: "You have X of your one unit of labour left." Under rolling labour this
    #: is the single most decision-relevant number an agent holds, and until now
    #: it was only reachable by spending a tool call on ``my_state`` — which
    #: made "did rolling labour help" partly a question about whether agents
    #: bothered to look.
    labour_left: bool = True
    #: Whether labour actually rolls. Not a telling but a fact about the world,
    #: kept here so the prose can match it. Setting this without the manager's
    #: own ``rolling`` would be telling agents something untrue.
    rolling: bool = False
    #: How much labour one agent has left, 0..1. Injected rather than read,
    #: because this module must not know what a ``Manager`` is.
    labour: Callable[[str], float] | None = None

    def horizon_note(self, left: int) -> str:
        return f"{left} round(s) remain after this one." if self.horizon else ""

    def labour_note(self, agent_id: str) -> str:
        if not self.labour_left or self.labour is None:
            return ""
        left = self.labour(agent_id)
        if left <= 1e-9:
            return "Your labour is fully committed."
        return f"You have {left:.2f} of your one unit of labour still to spend."


@dataclass
class Played:
    transcript: list[dict[str, Any]] = field(default_factory=list)
    #: One row per round: where the island stood after it. A trajectory rather
    #: than a point, which is both how the price/production lag becomes readable
    #: and how one island stops being a single noisy observation.
    trajectory: list[dict[str, Any]] = field(default_factory=list)
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
    notes: Notes | None = None,
    on_round: Callable[[int, str], Any] | None = None,
) -> Played:
    """Run one island's order of play. Returns the transcript and diagnostics."""
    budgets = budgets or Budgets()
    notes = notes or Notes()
    played = Played()
    order = list(manager.agents)
    total = total_label if total_label is not None else discovery + 1 + rounds
    round_no = 0
    rolling = notes.rolling

    async def pass_over(label: str, base: str, budget: int, *, left: int | None = None) -> None:
        """One turn each, in a shuffled order.

        The note is assembled per agent rather than per pass, because the one
        thing that differs between agents in the same round — how much labour
        each has left — is exactly the thing worth telling them.
        """
        nonlocal round_no
        rng.shuffle(order)
        for agent_id in order:
            parts = [base, notes.labour_note(agent_id) if rolling else ""]
            if left is not None:
                parts.append(notes.horizon_note(left))
            note = " ".join(part for part in parts if part)
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
        if on_round is not None:
            row = on_round(round_no, label)
            if row is not None:
                played.trajectory.append(row)

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
    if rolling:
        # The manager refuses two commitments in one tick and the opening
        # instalment was committed at this one. Without this the first trading
        # round's `produce` comes back "you have already worked this round",
        # quietly costing every agent a slice of labour and making the arm look
        # like a failure of nerve rather than of plumbing.
        manager.advance()

    for _ in range(rounds):
        round_no += 1
        left = total - round_no
        await pass_over(
            "offer",
            ("Trading is open, and you can still `produce` once more this round "
             "if you want to. " if rolling else "Trading is open. "),
            budgets.offer, left=left)
        await pass_over("settle",
                        "Same round, second pass: answer what is waiting on you.",
                        budgets.settle, left=left)
        manager.advance()
        manager.check_conservation()

    manager.close()
    manager.check_conservation()
    played.rounds_played = round_no
    return played
