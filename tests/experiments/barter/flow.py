"""The order of play, with nothing in it that knows about models.

Extracted so it can be tested for free. The flow is where the last two design
errors lived — production committed before anyone had spoken, and one turn per
agent for both proposing and answering — and both were found by paying for a
run and reading the wreckage afterwards. A loop that costs $2 and fifty minutes
to exercise is a loop nobody exercises.

``play`` takes a ``take_turn`` callable and never imports an SDK, so the whole
order of play runs against a scripted stand-in in milliseconds. What remains in
the runner is the part that genuinely needs a model.

The shape. Every round is the same six stages, each with a deadline the manager
enforces rather than the agents observing:

    1  discovery   talk. Nothing may be committed.
    2  produce     this round's instalment of labour.
    3  deal        talk again — now everyone knows what everyone holds.
    4  offer       propose. Every offer escrows its side as it is made.
    5  resolve     talk again — offers are on the table, none has settled, and
                   some of them cross. Decide together which ones stand.
    6  settle      approve what should stand, withdraw what should not.

Three stages of talk per round, because they are three different conversations.
In stage 1 an agent is guessing what it will hold and can still coordinate *what
to make*; by stage 3 it knows, and so does everyone else, so terms can be agreed
against real inventory instead of intent; by stage 5 it has escrow on the table
and a collision to settle with somebody specific. The original shape had all the
talking up front with the whole production decision behind it, which made every
later word a negotiation about goods nobody could change.

Stage 5 exists because of what stage 4 creates. Offers escrow as they are made,
so two agents who agreed a swap and both proposed it now hold mirror-image
trades and one has to give way. Every route out — approve one and cancel the
other, approve both and swap twice, cancel both — needs the two of them to
choose the *same* one, and none is right on its own merits. It is a pure
tie-break, and the failure mode is not choosing badly but choosing differently.
Scripted agents are handed a shared rule so their crossings resolve by
convention; a model agent gets the stage and no rule, and whether it invents one
its counterparty also arrives at is the measurement.

Offering and answering stay separate passes. That is not decoration: with one
turn per agent, roughly half of all offers cannot be answered until the
following round, and a three-tick expiry gives a proposal one or two real
chances at being seen. Collapsing them would re-introduce a bug this file
already paid for once.

Labour is spent per round by construction here, so the tick collision that used
to eat the first trading round's instalment cannot arise — production and
trading are separate stages, and the clock moves between rounds.

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
    rounds: int,
    rng: random.Random,
    lead_in: int = 0,
    budgets: Budgets | None = None,
    drain: Callable[[], Any] | None = None,
    notes: Notes | None = None,
    on_round: Callable[[int, str], Any] | None = None,
) -> Played:
    """Run one island's order of play. Returns the transcript and diagnostics.

    ``lead_in`` prepends talk-only rounds before the first production stage, for
    runs that want deliberation before any commitment at all. It defaults to
    none, because every round already opens with a talk stage.
    """
    budgets = budgets or Budgets()
    notes = notes or Notes()
    played = Played()
    order = list(manager.agents)
    round_no = 0

    async def pass_over(label: str, base: str, budget: int) -> None:
        """One turn each, in a shuffled order.

        The note is assembled per agent rather than per pass, because the one
        thing that differs between agents in the same round — how much labour
        each has left — is exactly the thing worth telling them.
        """
        rng.shuffle(order)
        for agent_id in order:
            parts = [base, notes.labour_note(agent_id) if notes.rolling else ""]
            parts.append(notes.horizon_note(rounds - round_no))
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

    for _ in range(lead_in):
        manager.open_discovery()
        await pass_over(
            "talk",
            "Nothing is committed yet and nothing can be. Production has not "
            "opened. Use this time however you think best.",
            budgets.talk)

    for _ in range(rounds):
        round_no += 1
        manager.open_discovery()
        await pass_over(
            "plan",
            "Stage 1 of 6, planning. Neither `produce` nor any trade is "
            "accepted right now — production opens at the end of this stage, "
            "and what you say here is the only thing that can change what "
            "anyone makes.",
            budgets.talk)

        manager.open_production()
        await pass_over(
            "produce",
            ("Stage 2 of 6, production. This is the only stage this round in "
             "which `produce` is accepted, and this round's instalment is not "
             "carried over — a share you do not spend now is a share nobody "
             "works. Your split is a fraction of *this instalment*, so it "
             "should sum to 1."
             if notes.rolling else
             "Stage 2 of 6, production. This is the only stage in which "
             "`produce` is accepted. Your split should sum to 1."),
            budgets.produce)
        manager.check_conservation()

        manager.open_deal()
        await pass_over(
            "deal",
            "Stage 3 of 6, dealing. Labour is spent and everyone now knows "
            "what they actually hold, but nothing has been swapped. "
            "`propose_trade` is CLOSED and will be refused — this stage is for "
            "agreeing terms in words, and the offers themselves come next.",
            budgets.talk)

        manager.open_trading()
        await pass_over(
            "offer",
            "Stage 4 of 6, trading, first pass: make your offers. You may make "
            "as many as you like, but each one escrows your side the moment you "
            "propose it — the goods are out of your hands until the offer "
            "settles, expires or you `cancel_trade` it — so offering the same "
            "goods twice is offering goods you no longer have.",
            budgets.offer)
        manager.open_resolve()
        await pass_over(
            "resolve",
            "Stage 5 of 6, resolving. Every offer is on the table and none has "
            "settled. Nothing can be proposed or approved right now — this "
            "stage is only for working out, with the traders you are dealing "
            "with, which of the offers on the table should stand and which "
            "should be withdrawn.",
            budgets.talk)

        manager.open_trading()
        await pass_over(
            "settle",
            "Stage 6 of 6, trading, second pass: approve what should stand and "
            "`cancel_trade` what should not. Anything still open when this "
            "stage closes expires, and its escrow comes back then and not "
            "before.",
            budgets.settle)

        manager.advance()
        manager.check_conservation()

    manager.close()
    manager.check_conservation()
    played.rounds_played = round_no
    return played
