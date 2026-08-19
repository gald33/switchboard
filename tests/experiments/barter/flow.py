"""The order of play, with nothing in it that knows about models.

Extracted so it can be tested for free. Every design error this experiment has
had lived here — production committed before anyone had spoken, one turn per
agent for both proposing and answering, a clock that ate an instalment — and
every one was found by paying for a run and reading the wreckage. A loop that
costs money and an hour to exercise is a loop nobody exercises.

``play`` takes a ``take_turn`` callable and never imports an SDK, so the whole
order of play runs against a scripted stand-in in milliseconds.

The round is a **wall clock**, not a sequence of turns. Every agent runs for the
whole of it, continuously, doing as much as it can fit. What the manager accepts
opens as the round goes and never closes inside it:

    minute 1   talk, and commit this round's labour
    minute 2   ...and propose, and withdraw your own proposals
    minute 3   ...and approve

Each capability waits on the one before it having had time: you cannot offer
what nobody has made, and you cannot settle before offers exist.

Time-boxing rather than turn-boxing is what makes the deadline real. An agent
still thinking when a window closes does not hold the round open, a fast agent
gets more turns than a slow one, and an agent that misses a window has missed
it. That variance is the first time pressure this experiment has had, and it is
worth measuring rather than designing away.

Talking is at every level because talking never reaches the manager — it goes
straight to the hub channel. The stages that used to gate it were only denying
everything else, which is a job a clock does better and with fewer moving parts.
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

    talk: int = 3
    produce: int = 3
    offer: int = 6
    settle: int = 4
    #: Most turns one agent may take in one window. The clock is the real
    #: limit — a model turn takes most of a minute, so an agent gets one or two
    #: — but a turn that returns instantly would otherwise spin the loop as
    #: fast as the event loop allows. That is unbounded spend if an agent starts
    #: erroring out quickly, and it is what a scripted stand-in does by nature:
    #: one took seventeen thousand turns in a twenty-millisecond window.
    turns_per_window: int = 8


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
    #: "`produce` is accepted once per round..." The rule the agent is planning
    #: against. Off, the mechanism is unchanged and the agent has to discover
    #: the shape of it by trying — which is a real difference in what agents do
    #: and so has to be separable from the mechanism itself.
    labour_rule: bool = True
    #: Whether labour actually rolls. Not a telling but a fact about the world,
    #: kept here so the prose can match it. Setting this without the manager's
    #: own ``rolling`` would be telling agents something untrue.
    rolling: bool = False
    #: How much labour one agent has left, 0..1. Injected rather than read,
    #: because this module must not know what a ``Manager`` is.
    labour: Callable[[str], float] | None = None

    def rule_note(self) -> str:
        """The labour rule, in the words of whichever game is actually running.

        The two modes are different games and an agent told the wrong one plans
        for the wrong game, so this reads off ``rolling`` rather than being set
        alongside it.
        """
        if not self.labour_rule:
            return ""
        if self.rolling:
            return ("`produce` is accepted once per round, and commits one "
                    "instalment of your one unit of labour. Each instalment's "
                    "shares must sum to 1, and an instalment you do not spend "
                    "is not carried over.")
        return ("`produce` is accepted once, and commits your whole unit of "
                "labour in that one call, with shares that sum to 1. There is "
                "no second call.")

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
    #: How many (agent, window) pairs went by without that agent starting a
    #: single turn in that window. A slow agent whose turn straddles a whole
    #: window has missed it, and that is the clock applying pressure rather than
    #: the agent deciding anything — counting it with refusals would credit the
    #: harness's deadline to the agents' judgement.
    missed: int = 0

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
    window: float = 60.0,
    budgets: Budgets | None = None,
    drain: Callable[[], Any] | None = None,
    notes: Notes | None = None,
    on_round: Callable[[int, str], Any] | None = None,
) -> Played:
    """Run one island. Returns the transcript and diagnostics.

    ``window`` is how long each of the three windows lasts, in seconds, so a
    round is three of them. Tests pass a tiny value and run the whole order of
    play in milliseconds; a real island passes sixty.
    """
    import anyio

    from .manager import LEVEL_OFFER, LEVEL_PRODUCE, LEVEL_SETTLE

    budgets = budgets or Budgets()
    notes = notes or Notes()
    played = Played()
    order = list(manager.agents)
    round_no = 0
    #: (round, level, agent) for every window an agent actually got into.
    reached: set[tuple[int, int, str]] = set()

    def budget_for(level: int) -> int:
        return {LEVEL_PRODUCE: budgets.produce, LEVEL_OFFER: budgets.offer,
                LEVEL_SETTLE: budgets.settle}[level]

    def note_for(agent_id: str, level: int) -> str:
        opens = {
            LEVEL_PRODUCE: "Window 1 of 3. You can talk and you can `produce`. "
                           "Proposing opens next window, approving the one after.",
            LEVEL_OFFER: "Window 2 of 3. Proposing is now open, and so is "
                         "withdrawing your own offers. Nothing can be approved "
                         "yet. You can still `produce` and still talk.",
            LEVEL_SETTLE: "Window 3 of 3. Everything is open: approve what "
                          "should stand, withdraw what should not. Anything "
                          "left when the round ends expires.",
        }[level]
        parts = [opens, notes.rule_note(),
                 notes.labour_note(agent_id) if notes.rolling else "",
                 notes.horizon_note(rounds - round_no)]
        return " ".join(part for part in parts if part)

    async def live(agent_id: str, ends_at: float) -> None:
        """One agent, acting for the whole round rather than taking a turn.

        It keeps going until the clock says stop. A fast agent fits more in
        than a slow one, and an agent whose turn is still running when the
        round ends does not hold it open — the round is over and whatever it
        was about to do is late.
        """
        taken = 0
        while anyio.current_time() < ends_at and taken < budgets.turns_per_window * 3:
            level = manager.level
            label = manager.phase
            reached.add((round_no, level, agent_id))
            taken += 1
            said = await take_turn(agent_id, round_no=round_no, label=label,
                                   note=note_for(agent_id, level),
                                   budget=budget_for(level))
            # The window the turn *started* in, not the one it finished in: a
            # turn that outlives its window is exactly the case being measured,
            # and labelling it by where it landed would hide that.
            played.transcript.append({"round": round_no, "pass": label,
                                      "agent": agent_id, "text": said})

    async def clock(started: float) -> None:
        """Raise what is open, on the wall clock, whatever the agents are doing."""
        for level in (LEVEL_OFFER, LEVEL_SETTLE):
            await anyio.sleep_until(started + window * (level - 1))
            manager.open(level)
            if drain is not None:
                drain()

    for _ in range(rounds):
        round_no += 1
        manager.open(LEVEL_PRODUCE)
        started = anyio.current_time()
        ends_at = started + window * 3
        rng.shuffle(order)
        async with anyio.create_task_group() as group:
            group.start_soon(clock, started)
            for agent_id in order:
                group.start_soon(live, agent_id, ends_at)
        if drain is not None:
            drain()
        # Everyone had every window, so anything still pending was seen by all.
        for trade in manager.trades.values():
            if trade.status == "pending":
                played.seen_by.setdefault(trade.id, set()).update(order)
        played.missed += sum(
            1 for level in (LEVEL_PRODUCE, LEVEL_OFFER, LEVEL_SETTLE)
            for agent_id in order if (round_no, level, agent_id) not in reached)
        manager.next_round()
        manager.check_conservation()
        if on_round is not None:
            row = on_round(round_no, "round")
            if row is not None:
                played.trajectory.append(row)

    manager.close()
    manager.check_conservation()
    played.rounds_played = round_no
    return played
