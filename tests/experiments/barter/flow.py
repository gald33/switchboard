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
from typing import Any, Awaitable, Callable, Protocol, Sequence


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

    #: A tool call is how an agent *batches*: `propose_trade` takes a list of
    #: deals and `approve_trade` a list of ids, so one call offers or settles as
    #: many as the agent means to. That is what makes a window only have to fit
    #: one turn -- what fits in a turn is not one action, it is one whole
    #: deliberation and everything it decided.
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
class Muster:
    """Getting everybody to the same start line before anything opens.

    The island used to simply begin. Every agent found out what was open by
    trying, and an agent whose first turn began forty seconds into a
    sixty-second window had no way to know it — across seven islands the settle
    window drew between one and five turns in total, and the arms that settled
    nothing were the arms whose agents never got a turn inside it. That reads
    as a coordination failure and is not one; it is the harness starting a race
    without telling anybody when the gun goes.

    So: the manager posts the whole timetable in absolute times, everyone
    acknowledges it, and only then does anything open. The acknowledgement is
    the part that matters. Publishing a schedule nobody has confirmed reading
    would move the same problem one step back — the island would still start
    with an agent mid-thought, and now with a timetable it had not looked at.
    """

    #: Seconds between posting a schedule and the island opening on it. Long
    #: enough that an agent which acknowledges late is still ready in time.
    lead: float = 120.0
    #: Seconds an agent has to acknowledge before the schedule is re-posted.
    #: Shorter than ``lead``, so the gap between the last ack and the start is
    #: time everyone can be ready *for* rather than time somebody is still
    #: agreeing to.
    ack_within: float = 90.0
    #: How many schedules to post before starting without the stragglers. Not
    #: unbounded: an agent that never acknowledges anything would otherwise
    #: hold the island open for ever, and "one trader never turned up" is a
    #: result to record rather than a reason to hang.
    attempts: int = 3


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
    #: Turns abandoned because the round hit its published end while they were
    #: still running. The published schedule is only worth publishing if the
    #: harness keeps to it, and before this a turn in flight held the island
    #: open: one round overran its announced end by 82 seconds.
    cut: int = 0
    #: Turns not started because too little of the round was left to finish
    #: one. A deliberate idle rather than a missed window -- starting a turn
    #: that will certainly be cut spends money for a result nobody can use.
    held_back: int = 0
    #: One row per schedule posted: its version, who acknowledged it, and
    #: whether it was the one the island ran on. How long an island took to
    #: agree on when to start is a coordination result in its own right.
    musters: list[dict[str, Any]] = field(default_factory=list)
    #: Agents that never acknowledged the schedule the island started on.
    #: Distinct from anything they decided: a trader that never turned up is
    #: not a trader that declined.
    absent: list[str] = field(default_factory=list)
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
    window: float | Sequence[float] = 60.0,
    budgets: Budgets | None = None,
    drain: Callable[[], Any] | None = None,
    notes: Notes | None = None,
    on_round: Callable[[int, str], Any] | None = None,
    muster: Muster | None = None,
    staged: bool = True,
) -> Played:
    """Run one island. Returns the transcript and diagnostics.

    ``window`` is how long each of the three windows lasts, in seconds, so a
    round is three of them. Tests pass a tiny value and run the whole order of
    play in milliseconds; a real island passes sixty.

    ``muster`` posts the timetable and waits for everyone to acknowledge it
    before anything opens. Without it the island simply begins, which is how it
    used to work and is the thing that made a settle window an agent could miss
    entirely without ever being told it existed.

    ``staged`` is whether the round opens in three windows or all at once. The
    staging existed so traders could deliberate before committing — offer only
    after everyone had made something, settle only after offers existed. What
    the runs showed is that they do not deliberate separately: the offers *are*
    the negotiation, so withholding offering withholds the negotiating. Worse,
    a turn now spans windows, and an action decided inside one is judged
    against whatever is open when it is swept — a trader announced on the floor
    that it had approved a trade the manager had refused as early, and never
    got another turn to find out. Unstaged, everything is open for the whole
    round and reality does the gating: nobody can approve what was never
    proposed, and nobody can offer what they do not hold.
    """
    import anyio

    from .manager import LEVEL_OFFER, LEVEL_PRODUCE, LEVEL_SETTLE, Agenda

    # One duration per window. A scalar means all three the same, which is what
    # tests want and what the runner used to do; a triple sizes them to the job,
    # which is what the measurements asked for -- a production turn ran 18-33
    # seconds and a trading turn 68-169, against windows that were all sixty.
    if isinstance(window, (int, float)):
        windows = (float(window),) if not staged else (float(window),) * 3
    else:
        windows = tuple(float(w) for w in window)
    if staged and len(windows) != 3:
        raise ValueError("window must be one number or three, one per window")
    if not staged:
        # Unstaged is one window, however the durations arrived: an island that
        # opens everything at once has one deadline, and three names for the
        # same span would be three ways to describe nothing.
        windows = (sum(windows),)

    budgets = budgets or Budgets()
    notes = notes or Notes()
    played = Played()
    order = list(manager.agents)
    round_no = 0
    #: (round, level, agent) for every window an agent actually got into.
    reached: set[tuple[int, int, str]] = set()

    def budget_for(level: int) -> int:
        if not staged:
            # One window has to hold the whole job, so it gets the whole budget
            # rather than the settle window's share of it.
            return budgets.produce + budgets.offer + budgets.settle
        return {LEVEL_PRODUCE: budgets.produce, LEVEL_OFFER: budgets.offer,
                LEVEL_SETTLE: budgets.settle}[level]

    def label_for(level: int) -> str:
        return manager.phase if staged else "open"

    def note_for(agent_id: str, level: int) -> str:
        if not staged:
            opens = ("Everything is open for the whole of this round: talk, "
                     "`produce`, offer, withdraw, approve. Nothing opens later "
                     "and nothing closes early. Anything still unapproved when "
                     "the round ends expires.")
            parts = [opens, notes.rule_note(),
                     notes.labour_note(agent_id) if notes.rolling else "",
                     notes.horizon_note(rounds - round_no)]
            return " ".join(part for part in parts if part)
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

    started_at = 0.0

    async def live(agent_id: str, ends_at: float) -> None:
        """One agent, acting for the whole round rather than taking a turn.

        It keeps going until the clock says stop. A fast agent fits more in
        than a slow one, and an agent whose turn is still running when the
        round ends does not hold it open — the round is over and whatever it
        was about to do is late.
        """
        taken = 0
        typical = 0.0
        while anyio.current_time() < ends_at and taken < budgets.turns_per_window * 3:
            # Do not start a turn the round cannot contain. `typical` is the
            # longest turn this agent has taken so far, so the estimate is its
            # own pace rather than an average over agents that think at
            # different speeds. Starting one anyway would spend a model call on
            # a result that gets cut before it lands.
            left = ends_at - anyio.current_time()
            if typical and left < typical:
                played.held_back += 1
                break
            level = manager.level
            label = label_for(level)
            reached.add((round_no, level, agent_id))
            taken += 1
            began = anyio.current_time()
            # The published end of the round is the published end of the round.
            # A turn still running when it arrives is abandoned rather than
            # awaited: a schedule of exact times that the harness itself does
            # not keep is worse than no schedule, and this one overran its
            # announced end by 82 seconds the first time it was measured.
            said = ""
            with anyio.move_on_after(max(0.0, ends_at - began)) as scope:
                said = await take_turn(agent_id, round_no=round_no, label=label,
                                       note=note_for(agent_id, level),
                                       budget=budget_for(level))
            if scope.cancelled_caught:
                played.cut += 1
                played.transcript.append({"round": round_no, "pass": label,
                                          "agent": agent_id, "text": "",
                                          "at": round(began - started_at, 2),
                                          "took": round(anyio.current_time() - began, 2),
                                          "cut": True})
                break
            typical = max(typical, anyio.current_time() - began)
            # The window the turn *started* in, not the one it finished in: a
            # turn that outlives its window is exactly the case being measured,
            # and labelling it by where it landed would hide that.
            #
            # `at` and `took` are seconds from the start of the round. Agents
            # run concurrently, so "how many turns started in the settle
            # window" cannot on its own distinguish an agent that was busy from
            # an agent that was waiting on somebody -- and those are a result
            # about model latency and a bug in this file respectively. With a
            # start and a duration per turn the overlap is readable directly.
            played.transcript.append({"round": round_no, "pass": label,
                                      "agent": agent_id, "text": said,
                                      "at": round(began - started_at, 2),
                                      "took": round(anyio.current_time() - began, 2)})

    async def clock(started: float) -> None:
        """Raise what is open, on the wall clock, whatever the agents are doing."""
        at = started
        for level in (LEVEL_OFFER, LEVEL_SETTLE):
            at += windows[level - 2]
            await anyio.sleep_until(at)
            manager.open(level)
            if drain is not None:
                drain()

    if muster is not None:
        await _muster(manager, take_turn, played, muster=muster, windows=windows,
                      rounds=rounds, budgets=budgets, drain=drain,
                      agenda_cls=Agenda)

    for _ in range(rounds):
        round_no += 1
        manager.open(LEVEL_SETTLE if not staged else LEVEL_PRODUCE)
        started = anyio.current_time()
        started_at = started
        ends_at = started + sum(windows)
        rng.shuffle(order)
        async with anyio.create_task_group() as group:
            if staged:
                group.start_soon(clock, started)
            for agent_id in order:
                group.start_soon(live, agent_id, ends_at)
        if drain is not None:
            drain()
        # Everyone had every window, so anything still pending was seen by all.
        for trade in manager.trades.values():
            if trade.status == "pending":
                played.seen_by.setdefault(trade.id, set()).update(order)
        levels = (LEVEL_PRODUCE, LEVEL_OFFER, LEVEL_SETTLE) if staged else (LEVEL_SETTLE,)
        played.missed += sum(
            1 for level in levels
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


async def _muster(manager: Any, take_turn: TakeTurn, played: Played, *,
                  muster: Muster, windows: tuple[float, ...], rounds: int,
                  budgets: Budgets, drain: Callable[[], Any] | None,
                  agenda_cls: Any) -> None:
    """Post the schedule, collect acknowledgements, start when everyone has one.

    Re-posts on a short deadline rather than waiting indefinitely. An agent
    that has not acknowledged by ``ack_within`` is not necessarily refusing —
    it is more often still inside a turn that began before the schedule
    existed — and the fix for that is a later start time, not a longer wait.
    Each re-post is a fresh version with fresh times, and acks of the old one
    are dropped, because an island where two agents hold two timetables has not
    started together in any sense that matters.
    """
    import anyio

    origin = anyio.current_time()
    for attempt in range(1, muster.attempts + 1):
        now = anyio.current_time() - origin
        agenda = manager.post_agenda(agenda_cls(
            version=attempt, posted_at=now, acks_by=now + muster.ack_within,
            starts_at=now + muster.lead, windows=windows, rounds=rounds))
        if drain is not None:
            drain()
        deadline = origin + agenda.acks_by

        # `agenda` and `deadline` are bound as defaults rather than closed
        # over: this runs once per attempt, and a closure over the loop
        # variable would ask agents to acknowledge whichever schedule happened
        # to be current when the coroutine finally ran.
        async def ask(agent_id: str, agenda: Any = agenda,
                      deadline: float = deadline) -> None:
            """Keep asking one agent until it acks or the deadline passes."""
            while anyio.current_time() < deadline and agent_id not in manager.acked:
                said = await take_turn(
                    agent_id, round_no=0, label="muster",
                    note=_muster_note(agenda, manager),
                    budget=budgets.talk)
                played.transcript.append({"round": 0, "pass": "muster",
                                          "agent": agent_id, "text": said,
                                          "at": round(anyio.current_time() - origin, 2),
                                          "took": 0.0})
                if drain is not None:
                    drain()

        async with anyio.create_task_group() as group:
            for agent_id in manager.agents:
                group.start_soon(ask, agent_id)
            await anyio.sleep_until(deadline)
            group.cancel_scope.cancel()
        if drain is not None:
            drain()

        acked = sorted(manager.acked)
        played.musters.append({"version": attempt, "acked": acked,
                               "of": len(manager.agents),
                               "starts_at": round(agenda.starts_at, 1)})
        if manager.all_acked() or attempt == muster.attempts:
            played.absent = sorted(set(manager.agents) - manager.acked)
            # Wait for the time that was announced, even when everybody
            # acknowledged early. Starting sooner than the published schedule
            # would make the times advisory, and the times are the whole point.
            await anyio.sleep_until(origin + agenda.starts_at)
            return


def _muster_note(agenda: Any, manager: Any) -> str:
    """What an agent is told while the island is forming up."""
    rows = "\n".join(
        f"  round {r['round']} window {r['window']}: "
        f"t={r['opens_at']:g}s to t={r['closes_at']:g}s — {r['you_may']}"
        for r in agenda.rows())
    waiting = sorted(set(manager.agents) - manager.acked)
    return (
        f"Before the island opens. Here is the whole schedule, in seconds from "
        f"now being t={agenda.posted_at:g}s. Nothing is open yet and nothing "
        f"you do will move goods.\n\n{rows}\n\n"
        f"Trading opens at t={agenda.starts_at:g}s and not before. Call `ack` "
        f"with version {agenda.version} to confirm you have read this. If "
        f"everyone has not acknowledged by t={agenda.acks_by:g}s the schedule "
        f"is withdrawn and a new one posted with later times. "
        f"Still to acknowledge: {', '.join(waiting) if waiting else 'nobody'}.")
