"""Tier 2: put a language model in the trader's seat and see what it says.

Tier 1 ran scripted policies, so it could not discover that communication helps
— it was written in. What it *did* establish is the thing that makes this run
interpretable: a ladder of benchmarks (autarky, exchange ceiling, frontier), a
scorer that agrees with the First Welfare Theorem, and the knowledge that a
shared price reaches the frontier through this exact manager, so anything short
of it is about the agents rather than the protocol.

The question here is the one no simulation can answer. Given a channel and no
instructions about what to do with it, do models invent something that works?

Design
------
**Only the traders are models. The manager is not**, and must not be. It
enforces non-negativity, conservation and two-phase settlement, and an LLM
cannot be relied on to enforce an invariant it can be argued out of. It is a
state machine reached over Switchboard messages, exactly as in Tier 1 — so a
model cannot cheat by being persuasive, only by being right.

**Every agent sees only its own state.** Capacities, tastes and holdings come
back from ``my_state`` for the calling agent alone. Everything an agent knows
about anyone else it has to have been told, which is what puts the channel under
test instead of the prompt.

**Arms are tool surfaces, not instructions.** The prompt never suggests posting
prices, choosing a numeraire, or using anything as money. ``silent`` simply has
no channel tool; ``free`` has ``say`` and ``listen`` and nothing about what to
put in them. If a convention appears in ``free`` it was invented, not followed —
and the Tier 1 arms give it a scale to be measured against.

**Everything told is a switch.** The named arms below are combinations of
``Telling`` fields, not primitives. Each rung of the ladder used to add two
things at once — storage *and* aggregation, a deviation report *and* quote
expiry — so a rung that moved could never say which half moved it. Any switch
can now be flipped on its own, which is the difference between attributing a
result to a mechanism and attributing it to a name.

The ladder
----------
``free`` answered its question: given a channel and no guidance, Haiku traders
built a want-board rather than a market — fourteen messages, not one of them
naming a price, a rate or a unit. So the next question is *what was missing*,
and it splits in two:

    silent   no channel at all
    free     a channel, nothing said about what to put in it
    told     the numeraire convention, stated in words. Same tools as `free`.
    built    the same words, plus machinery that implements the convention:
             a structured quote board with validation and aggregation.
    bound    the same words and board, but the board pushes back: it names your
             distance from the median, and a quote you stop renewing expires.

All three quoting arms share a system prompt **byte for byte**, and a test
asserts it. What separates them is only what the tools do, which is the
difference between telling agents how to coordinate and building them something
to coordinate *with* — a claim about what a coordination substrate should offer,
not about what a prompt should say.

``free`` against ``told`` asked whether this is a convention models will not
invent but will happily adopt. The answer was worse than either: they adopted the
*vocabulary* completely and the *agreement* not at all, quoting confidently while
holding cloth 30x apart, and finished below the arm that said nothing.

``told`` against ``built`` asked whether aggregation was the missing piece. It
was not. Four traders had a median in every reply and still ended 27x apart on
cloth — the only good they agreed on was the one the board pinned for them.

``built`` against ``bound`` is what is left: whether the missing piece is
**obligation** rather than aggregation. Same board, same median, but now it
reports your own deviation instead of leaving you to notice it, and a quote you
stop renewing falls off. If that is enough, what a convention needs from its
substrate is pressure, not information.

Cost
----
Every agent-turn is a model call, so an island of 6 agents over 8 rounds is on
the order of a hundred calls. Defaults are small on purpose and the runner
prints its spend. ``--model`` selects the tier; the harness is model-agnostic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .economy import Island
from .manager import Manager

#: Kept deliberately free of trading advice. It says what the world is, what
#: the agent is scored on, and how settlement works — because an agent that
#: does not know a seller must approve will simply fail at the mechanics and
#: tell us nothing about conventions. It does not say what to produce, how to
#: value anything, what to talk about, or that prices are a thing.
BRIEF = """\
You are {agent_id}, a trader on an island with {n_others} other traders.

There are {n_goods} goods: {goods}.

Your score at the end is the product of your final holdings raised to your
taste exponents{ruin}

{labour}

Trading is two-phase and the manager is the only thing that can move goods:
  - you call `propose_trade` naming one other trader, what you give and what
    you want. You get a trade id back, and what you offered is held in escrow
    from that moment — you cannot offer it to anyone else while it is open.
  - that named trader calls `approve_trade` with the id to settle it. Until
    they do, nothing has happened. Offers expire if nobody approves them.
  - check `pending_trades` for offers waiting on you.

Quantities are never negative. You cannot offer what you do not hold.
{flow}"""

#: The consequence of the scoring rule, spelled out. A separate switch because
#: it is not the rule — it is an *inference from* the rule that we currently
#: make on the agent's behalf, and "does a trader work out that a zero holding
#: is fatal, or does it have to be told" is a question with an answer.
RUIN_CLAUSE = """ — so a good you hold NONE of makes your score zero, however
much of everything else you have. You want some of every good."""

#: The labour paragraph, in the two worlds it can describe. Which one an agent
#: gets is not a convention and not a hint: it is the world it is actually in,
#: and telling a rolling agent it spends its labour "once, at the start" is
#: simply false. That was live for one paid run, and the sentence agents read
#: contradicted the manager they were calling.
ONCE_LABOUR = """\
You have one unit of labour and one chance to spend it: a single `produce` call
naming the fraction you put on each good. Your capacity for each good is how
much you get for spending your whole unit on it, and it differs between goods
and between traders. `produce` is open in every window of every round, so *when*
you call it is yours to decide — but you call it once, and after that you cannot
make anything more. The only way to change what you hold is to trade, and every
round you spend deciding is a round you are not trading what you made."""

ROLLING_LABOUR = """\
You have one unit of labour and you spend it in instalments: in each round you
may call `produce` once, with the fraction of *that round's* instalment you put
on each good. It is open in every window of the round. Your capacity for each
good is how much you get for spending your whole unit on it, and it differs
between goods and between traders. An instalment you do not spend is not
carried over — it is a round you did not work — and once the unit is gone the
only way to change what you hold is to trade."""

#: The shape of a round. Not a telling and not switchable, for the same reason
#: the window line in the turn note is not: an agent that does not know
#: approving has opened cannot approve, so an arm without this would be
#: measuring the harness rather than the convention. What is *not* here is how
#: long a window lasts in seconds — agents know there is a deadline and not its
#: size — and how many rounds there are, which is the `horizon` switch's to give
#: or withhold in the turn note.
FLOW_BRIEF = """
The island runs in rounds, and a round is three windows that open in order:

  1. you can{talk} `produce`.
  2. ...and you can `propose_trade`, and withdraw your own offers.
  3. ...and you can `approve_trade`.

Nothing closes again inside a round: what has opened stays open until the round
ends. Every trader is acting at the same time, and each window ends on a clock
rather than when anyone is finished — so a decision you are still making when
the round ends is a decision you did not make.
"""

CHANNEL_BRIEF = """
You can also talk. `say` posts a message every trader sees; `listen` returns
what has been posted since you last looked. Nobody is obliged to tell the truth
and nobody is obliged to read.
"""

#: Given verbatim to both `told` and `built`, which is what makes their
#: comparison mean anything: same words, different affordances.
#:
#: It describes the convention and stops. It does not say to specialise, does
#: not say a shared price is worth having, and does not say what to produce —
#: because whether a unit of account leads agents to those things on their own
#: is the question, not the setup. The last line is load-bearing in the other
#: direction: a convention nobody enforces is exactly what a convention is, and
#: an agent that thought the manager was checking prices would be following a
#: rule rather than keeping an agreement.
NUMERAIRE_BRIEF = """
The island has a convention for talking about value.

Fish is the unit of account. A good's price is how many fish one unit of it is
worth, so fish is priced at 1 by definition.

State your prices for the other goods in fish, and revise them as you see what
others are stating.

This is only a way of speaking. The manager knows nothing about prices, enforces
none of this, and will settle any trade the two of you agree to.
"""

#: The money clause, added to ``spend`` and ``paid`` on top of the numeraire
#: convention. Two sentences, and the second is the load-bearing one.
#:
#: "Settle in fish" is only the visible consequence. What makes money money is
#: accepting it *past your own appetite* — if a trader takes fish only up to the
#: fish it wants to eat, it stops selling once it is full and the market is back
#: to barter. So the clause asks an agent to hold a bundle that is worse by its
#: own utility function right now, on a belief about what everyone else will do
#: later. That is self-fulfilling and unavailable to an agent reasoning alone,
#: which is exactly what makes it a convention rather than a preference.
#:
#: It costs something real here: fish is a consumption good with a Cobb-Douglas
#: weight, so excess fish is genuinely suboptimal rather than a free token.
MONEY_BRIEF = """
Fish is also the medium of exchange. Trades are settled in fish rather than by
swapping goods directly.

Accept fish past the point of wanting it for itself. It can be spent again.
"""

#: The tie-break, stated. Off by default and deliberately so: the scripted arms
#: are *given* a shared rule, which is what makes them a benchmark, and the
#: question for a model island is whether it arrives at one unaided — and whether
#: its counterparty arrives at the same one.
#:
#: The rule named here is arbitrary and says so. That is the whole nature of the
#: problem: newest-survives would work exactly as well, and the failure mode is
#: not choosing badly but choosing differently. Two traders who both defer cancel
#: both offers; two who both insist swap twice.
TIEBREAK_BRIEF = """
When you and another trader have each proposed the same swap, both sides are
held in escrow and only one of the two offers should stand. Keep the one that
was proposed first — the lower trade id — and withdraw the other. Which of the
two survives does not matter. That you both pick the same one does.
"""

#: Everything the harness hands an agent, one switch at a time.
#:
#: The named arms below came first and were the wrong shape. Each was a bundle
#: — ``bound`` added a deviation report *and* quote expiry in the same step,
#: ``built`` added storage *and* aggregation — so when an arm moved, the run
#: could say that the bundle mattered and could never say which part of it did.
#: Every result on this ladder was therefore an attribution to a name rather
#: than to a mechanism.
#:
#: So each thing told to an agent is now its own field, the arms are named
#: combinations of them, and a switch can be flipped on its own. That is the
#: only way "the median was the missing piece" or "obligation is what a
#: convention needs from its substrate" can be a finding rather than a story
#: about a bundle.
#:
#: The design rule every one of these still obeys: **a switch may calculate or
#: report, and may never commit anybody.** The quote board is a noticeboard;
#: ``pay`` is a calculator that builds a trade at the going rate. Neither binds a
#: counterparty — the manager is the only thing in the experiment that moves a
#: quantity, and it still knows nothing about prices. That is what keeps all of
#: this a *convention with a calculator* rather than an exchange with rules, and
#: it is why a standing-offer book is deliberately absent: a pre-committed offer
#: would settle without its owner acting, which is a commitment mechanism and a
#: different experiment.
@dataclass(frozen=True)
class Telling:
    """One island's information setup. Every field is independently switchable.

    Fields fall into three kinds and it is worth keeping them apart:

    * **Words.** ``numeraire``, ``money``, ``ruin_warning`` add a paragraph to
      the system prompt and change nothing an agent can *do*.
    * **Affordances.** ``channel``, ``board``, ``median``, ``deviation``,
      ``expiry``, ``pay_tool`` change the tool surface. The prompt never
      mentions them; a tool announces itself through its own description, which
      is what makes "telling agents how to coordinate" and "building them
      something to coordinate with" separable at all.
    * **Replies.** ``own_value`` and ``own_score`` decide what ``my_state``
      passes on, and ``horizon``, ``labour_left`` and ``labour_rule`` what
      the turn note says.
      Neither changes the world; both change what an agent has to work out for
      itself, which is the same kind of help a tool gives and was the easiest
      kind to hand over without noticing.
    * **The world.** ``rolling`` is not a telling — it is whether the manager
      actually lets labour be committed in instalments. It appears here only so
      the prompt can describe the world truthfully, and setting it without
      setting the manager's own flag would be a lie to the agent.

    All of it in one object, so that "what did this island's agents know" has a
    single answer that a run record can store.
    """

    #: `say` and `listen`. Without it an agent's only counterparty contact is a
    #: trade proposal.
    channel: bool = False
    #: The numeraire convention, stated in words. Fish is the unit of account.
    numeraire: bool = False
    #: A structured quote board: `post_quote` validates, `read_quotes` returns
    #: everyone's latest. Storage only — the aggregation is the next switch.
    board: bool = False
    #: The board also reports the median price per good. This is the step a
    #: price convention actually needs and the step prose leaves each agent to
    #: do in its head, which is where a shared price stops being shared.
    median: bool = False
    #: The board reports *your* distance from the median, per good, as a
    #: multiple. The comparison every agent could have made and none did.
    deviation: bool = False
    #: Quotes go stale. Staying on the board becomes something you keep doing
    #: rather than something you did once.
    expiry: bool = False
    #: The money clause: settle in the numeraire, and accept it past the point
    #: of wanting it for itself.
    money: bool = False
    #: `pay`: a calculator that sizes a money trade at the board median and
    #: proposes it. The seller still has to approve.
    pay_tool: bool = False
    #: Whether labour is actually committed in instalments. A fact, not a hint.
    rolling: bool = False
    #: Spell out that a zero holding scores zero.
    ruin_warning: bool = True
    #: Tell an agent how many rounds remain.
    horizon: bool = True
    #: Tell an agent how much labour it has left, in the turn note, rather than
    #: leaving it to spend a tool call finding out.
    labour_left: bool = True
    #: State the labour rule in the turn note — once, or once per round, and
    #: what happens to an instalment nobody claims. Separate from ``rolling``
    #: because the mechanism and the sentence describing it are two changes, and
    #: a run that moved both cannot say which one the agents responded to.
    labour_rule: bool = True
    #: `my_state` reports `value_per_unit`: what one more of each good is worth
    #: to you right now. This is a calculator, and a substantive one — it is the
    #: marginal rate an agent would otherwise have to work out from its own
    #: exponents and holdings, and it is most of what a trader needs to price a
    #: swap. Handing it over silently made every arm's prompt look more austere
    #: than the island actually was.
    own_value: bool = True
    #: `my_state` reports your current score. Cheap to compute and impossible to
    #: unsee: an agent watching a number go up is playing a different game from
    #: one that only knows the rule.
    own_score: bool = True
    #: `pending_trades` points out when you and a counterparty have both
    #: proposed the same swap, so both sides sit escrowed and approving both
    #: would trade twice. The information is already in the reply — both trades
    #: are listed, one in each direction — so this switch is purely whether the
    #: collision is *named* or left to be noticed. That is the cheapest possible
    #: version of the question the whole ladder asks, and it defaults off
    #: because noticing unaided is the interesting outcome.
    crossings: bool = False
    #: `propose_trade` carries a free-text `note` that the seller reads. A
    #: directed, escrow-costing channel to one counterparty is a genuinely
    #: different affordance from a broadcast one, so it is its own switch — and
    #: `silent` must not have it. It did: the arm whose whole point is having no
    #: way to communicate shipped with a 200-character line to anybody it
    #: proposed to, and the gate asserting "no `say`, no `listen`" walked
    #: straight past it. `silent` + `trade_note` is now a real arm rather than
    #: an accident: is a costly, private, one-to-one channel enough?
    trade_note: bool = False
    #: A tie-break rule for crossed offers, stated in words. The scripted arms
    #: are handed one; this is what it costs to hand one to a model island, and
    #: `crossings` against `tiebreak` separates *seeing* the collision from
    #: *knowing what to do about it* — two things the same paragraph would
    #: otherwise deliver together.
    tiebreak: bool = False

    #: What each switch needs underneath it. These are not style rules: a board
    #: is denominated in fish and pins fish at 1, so a board without the
    #: numeraire convention would be quoting on a scale nobody was told about,
    #: and a `pay` tool without a median has no rate to price at. An island
    #: built from an incoherent combination would still run and would produce a
    #: number nobody could interpret, which is worse than a crash.
    _REQUIRES = (
        ("numeraire", ("channel", "board"), "somewhere to state a price"),
        ("board", ("numeraire",), "a unit to quote in"),
        ("median", ("board",), "quotes to aggregate"),
        ("deviation", ("median",), "a median to deviate from"),
        ("expiry", ("board",), "a board to expire from"),
        ("money", ("numeraire",), "a numeraire to settle in"),
        # Two rows rather than one: a tuple is satisfied by *any* of its
        # entries, and `pay` needs both — a median to price at and the money
        # clause that makes paying rather than swapping the thing to do.
        ("pay_tool", ("median",), "a rate to pay at"),
        ("pay_tool", ("money",), "a reason to pay rather than swap"),
    )

    def __post_init__(self) -> None:
        for field_name, needs, why in self._REQUIRES:
            if getattr(self, field_name) and not any(getattr(self, n) for n in needs):
                raise ValueError(
                    f"{field_name} needs {' or '.join(needs)}: {why}")

    def switches(self) -> tuple[str, ...]:
        """The switches that are on, in declaration order. The record's label."""
        return tuple(name for name in _SWITCHES if getattr(self, name))

    def to_notes(self) -> Any:
        """The turn-note half of this setup, for ``flow.play``."""
        from .flow import Notes

        return Notes(horizon=self.horizon, labour_left=self.labour_left,
                     labour_rule=self.labour_rule, rolling=self.rolling)


_SWITCHES: tuple[str, ...] = (
    "channel", "numeraire", "board", "median", "deviation", "expiry",
    "money", "pay_tool", "rolling", "ruin_warning", "horizon", "labour_left",
    "labour_rule",
    "own_value", "own_score", "crossings", "tiebreak", "trade_note",
)

#: The named arms, as combinations. They are kept because the merged results are
#: reported under these names and a rung of the ladder should stay one word —
#: but nothing reads a name any more, only the switches it stands for.
PRESETS: dict[str, Telling] = {
    "silent": Telling(),
    "free": Telling(channel=True, trade_note=True),
    "told": Telling(channel=True, numeraire=True, trade_note=True),
    "built": Telling(channel=True, numeraire=True, board=True, median=True,
                     trade_note=True),
    "bound": Telling(channel=True, numeraire=True, board=True, median=True,
                     deviation=True, expiry=True, trade_note=True),
    "spend": Telling(channel=True, numeraire=True, board=True, median=True,
                     deviation=True, expiry=True, money=True, trade_note=True),
    "paid": Telling(channel=True, numeraire=True, board=True, median=True,
                    deviation=True, expiry=True, money=True, pay_tool=True,
                    trade_note=True),
}

ARMS = tuple(PRESETS)


def telling_for(spec: str | Telling) -> Telling:
    """A ``Telling`` from either a preset name or one already built."""
    if isinstance(spec, Telling):
        return spec
    if spec not in PRESETS:
        raise ValueError(f"unknown arm {spec!r}; expected one of {', '.join(ARMS)}")
    return PRESETS[spec]


def compose(base: str | Telling, *, on: Iterable[str] = (), off: Iterable[str] = ()) -> Telling:
    """A preset with individual switches flipped.

    This is the whole point of the refactor at the command line: ``bound``
    without ``expiry`` isolates the deviation report, ``built`` with ``expiry``
    isolates staleness, and neither has a name on the ladder because neither is
    a rung — they are the differences *between* rungs, which is what an
    attribution needs and what a ladder of bundles cannot give.
    """
    changes: dict[str, bool] = {}
    for name in on:
        _check_switch(name)
        changes[name] = True
    for name in off:
        _check_switch(name)
        changes[name] = False

    # Turning a switch off takes its dependents with it. "An island with no
    # quote board" cannot coherently still report a median, and refusing the
    # request would only mean spelling out the same cascade by hand at the
    # command line. Nothing is hidden by this: the record stores the resolved
    # switch set, so what an island actually had is always what it says it had.
    resolved = dict(changes)
    fields = {name: changes.get(name, getattr(telling_for(base), name))
              for name in _SWITCHES}
    for _ in range(len(Telling._REQUIRES) + 1):
        for name, needs, _why in Telling._REQUIRES:
            if fields[name] and not any(fields[n] for n in needs):
                if name in resolved and resolved[name]:
                    raise ValueError(
                        f"{name} was switched on but needs {' or '.join(needs)}, "
                        "which this island does not have")
                fields[name] = False
    return replace(telling_for(base), **fields)


def _check_switch(name: str) -> None:
    if name not in _SWITCHES:
        raise ValueError(f"unknown switch {name!r}; expected one of {', '.join(_SWITCHES)}")


#: How many manager ticks a quote stays on the board when ``expiry`` is on. Two
#: means a quote survives the round it was posted and one more, so an agent that
#: never comes back drops off rather than sitting there being read as current.
QUOTE_TTL_TICKS = 2

#: Index of the numeraire in the goods tuple. Which good is arbitrary; that
#: everyone uses the *same* one is the entire convention.
NUMERAIRE_INDEX = 0


TURN = """\
Round {round_no} of {rounds}. {phase_note}

Everyone is acting at the same time, and the round ends on a clock rather than
when you are finished. Do what you think is best now and then stop; you will be
asked again while the round lasts.
"""


@dataclass
class Wire:
    """One agent's tool surface, bound to its identity on the hub.

    The binding is the security property. An agent's tools close over its own
    ``agent_id``, and the manager takes identity from the hub envelope rather
    than the message body, so there is no argument a model can pass that makes
    it somebody else.
    """

    agent_id: str
    client: Any
    telling: Telling
    floor_channel: str
    #: Blackboard prefixes for the order queue. An agent writes what it wants
    #: under ``{order_prefix}{agent}/{n}`` and reads the outcome back from
    #: ``{result_prefix}{agent}/{n}``. Nothing here sends the manager a request.
    order_prefix: str = ""
    result_prefix: str = ""
    #: Where the sweep publishes the manager's logical clock.
    clock_key: str = ""
    #: Where the sweep publishes the schedule.
    agenda_key: str = ""
    #: ...and the channel it is announced on.
    agenda_channel: str = ""
    #: Blackboard prefix for the quote board. One key per trader,
    #: so a quote is a value anyone can read rather than a message somebody
    #: might have scrolled past.
    quote_prefix: str = ""
    goods: tuple[str, ...] = ()
    calls: list[dict[str, Any]] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    quotes_posted: int = 0
    #: How long to wait for a sweep to pick an order up, and how often to look.
    #: Generous against the sweep interval, because an order that has not been
    #: swept yet is not a refusal and must not be reported to the agent as one.
    poll_every: float = 0.05
    poll_for: float = 20.0
    #: Offline tests only. They are single-threaded and have no sweeper thread,
    #: so they pass one here and ``manager_call`` completes in-process. A real
    #: run leaves this ``None``: an agent that drives the manager between
    #: writing its order and reading the answer is blocking on the manager,
    #: which is the pump the board exists to remove.
    sweep_while_waiting: Any = None
    _cursor: int = 0
    _orders: int = 0

    def manager_call(self, op: str, **kwargs: Any) -> dict[str, Any]:
        """Write an order to the board and wait for the sweep to answer it.

        Deliberately not a request to the manager. Every agent is live at once
        against a hard clock, so nobody can afford to block the manager and
        nobody here does: the write returns immediately and the wait is a poll
        on this agent's own result key. What that costs is that an order is not
        applied at the instant it is written — it is applied in the order the
        board says the orders arrived, which is a real race with a real
        tiebreak rather than an artefact of who was scheduled first.

        The result key is deleted once read. It has served its purpose, and
        leaving it would grow the board with one key per tool call.
        """
        import time

        self._orders += 1
        suffix = f"{self.agent_id}/{self._orders:04d}"
        self.client.board_set(f"{self.order_prefix}{suffix}", {"op": op, **kwargs},
                              ttl=3600.0)
        deadline = time.monotonic() + self.poll_for
        while True:
            if self.sweep_while_waiting is not None:
                self.sweep_while_waiting()
            reply = self.client.board_get(f"{self.result_prefix}{suffix}")
            if isinstance(reply, dict):
                self.client.board_delete(f"{self.result_prefix}{suffix}")
                self.calls.append({"op": op, "ok": reply.get("ok")})
                return reply
            if time.monotonic() >= deadline:
                # Not "the manager refused you". An unswept order is the
                # harness being slow, and an agent told otherwise would report
                # a refusal that never happened.
                self.calls.append({"op": op, "ok": None})
                return {"ok": False, "op": op,
                        "error": "your order has not been picked up yet; try again"}
            time.sleep(self.poll_every)

    def manager_batch(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        """Several requests as one order, one sweep, one wait.

        The reason a turn was taking two minutes. Each separate call was a
        write to the board, a wait for the next sweep and a read back, with the
        model thinking again in between; three offers cost three of those. One
        order costs one, and the manager applies the parts in order under its
        own lock so nobody can take the goods out from under half of them.
        """
        if not ops:
            return {"ok": False, "error": "nothing to do: the list was empty"}
        return self.manager_call("batch", ops=ops)

    def state(self) -> dict[str, Any]:
        """This agent's own state, minus anything the switches do not hand over.

        The filtering is here rather than in the manager on purpose. The manager
        is the pure state machine both tiers share and it must not know what an
        island was told — it answers what it knows, and this decides what is
        passed on. That keeps "what an agent could see" a property of the
        experiment's setup rather than of its bookkeeping.
        """
        reply = self.manager_call("state")
        if not self.telling.own_value:
            reply.pop("value_per_unit", None)
        if not self.telling.own_score:
            reply.pop("utility", None)
        return reply

    def pending(self) -> dict[str, Any]:
        """Open trades, minus the collision flag unless this island was given it.

        Both halves of a crossing are already in the reply — one under
        ``your_open_offers`` and one under ``awaiting_your_approval`` — so what
        the switch withholds is the *naming*, not the facts. An agent can always
        work it out; the question is whether it does.
        """
        reply = self.manager_call("pending")
        if not self.telling.crossings:
            reply.pop("crossed_pairs", None)
        return reply

    def post(self, text: str) -> dict[str, Any]:
        self.said.append(text)
        self.client.post(self.floor_channel, {"from": self.agent_id, "text": text})
        return {"posted": True}

    def history(self) -> list[dict[str, Any]]:
        """The whole floor from the beginning, oldest first.

        Deliberately a *tool call* rather than something appended to every turn
        note, and that is the cache-shaped decision. An agent holds one session
        for the whole island, so its own past is already in its context and
        costs nothing to keep — but pasting the shared floor into each turn
        would re-send the entire transcript on every turn of every agent, and
        it grows with the square of the run. Fetching it on demand keeps the
        session's cached prefix append-only, which is what makes a long island
        affordable at all.

        Reading does not move the ``listen`` cursor. The two answer different
        questions — "what did I miss" and "what was ever said" — and an agent
        that catches up on the whole record should not thereby lose track of
        what it had already seen.
        """
        return [m["body"] for m in self.client.history(self.floor_channel, limit=500)
                if isinstance(m.get("body"), dict)]

    def read(self) -> list[dict[str, Any]]:
        history = self.client.history(self.floor_channel, limit=200)
        fresh = history[self._cursor:]
        self._cursor = len(history)
        return [m["body"] for m in fresh if isinstance(m.get("body"), dict)]

    def pay(self, seller: Any, good: Any, qty: Any) -> dict[str, Any]:
        """Buy ``qty`` of ``good`` from ``seller``, paying fish at the going rate.

        A calculator, and deliberately nothing more. It reads the board's median,
        multiplies, and hands the result to ``propose_trade`` — the arithmetic an
        agent would otherwise do by hand, which under ``spend`` it has to. The
        seller still has to approve, so this changes how easy a money trade is to
        *express* and not whether anyone is bound by it.

        That line is the whole design rule of this experiment. A tool may
        calculate or report; only the manager moves a quantity.
        """
        board = self.read_quotes()
        medians = board.get("median_price") or {}
        name = str(good)
        if name not in medians:
            return {"error": f"no median price for {name!r} yet; nobody has quoted it"}
        try:
            amount = float(qty)
        except (TypeError, ValueError):
            return {"error": "qty is not a number"}
        if amount != amount or amount <= 0:
            return {"error": "qty must be a positive number"}

        numeraire = self.goods[NUMERAIRE_INDEX]
        fish = amount * medians[name] / max(medians.get(numeraire, 1.0), 1e-12)
        batched = self.manager_batch([{
            "op": "propose", "seller": str(seller),
            "give": {numeraire: round(fish, 6)},
            "want": {name: round(amount, 6)},
            "note": f"at the board median, {medians[name]:g} {numeraire}/{name}"}])
        # `pay` buys one thing, so it hands back the one reply rather than a
        # list of one. The batching is how the order reaches the manager, not
        # something this tool's caller should have to unwrap.
        results = batched.get("results") if isinstance(batched, dict) else None
        reply = results[0] if isinstance(results, list) and results else batched
        return {"offered": {"pay": round(fish, 6), "for": round(amount, 6), "of": name},
                "median_used": medians[name], "result": reply}

    # --- the `built` arm's machinery ---------------------------------------

    def post_quote(self, prices: Any) -> dict[str, Any]:
        """Publish this trader's prices, in fish, on the shared board.

        Validated rather than accepted. That is a real part of what "machinery"
        means here: in the ``told`` arm a trader can say "cloth is about two,
        maybe three" and the ambiguity survives to the point of trade, whereas
        this either stores a number per good or explains why it did not. One
        key per trader on the blackboard, overwritten each time, so the board
        is a current state anyone can read rather than a history to scroll.
        """
        if not isinstance(prices, dict) or not prices:
            return {"error": 'prices must be an object like {"cloth": 2.5, "salt": 0.8}'}
        clean: dict[str, float] = {}
        for good, value in prices.items():
            name = str(good)
            if name not in self.goods:
                return {"error": f"unknown good {name!r}; goods are {', '.join(self.goods)}"}
            try:
                price = float(value)
            except (TypeError, ValueError):
                return {"error": f"price for {name!r} is not a number"}
            if price != price or price <= 0 or price == float("inf"):
                return {"error": f"price for {name!r} must be a positive number"}
            clean[name] = price
        # Fish is 1 by definition; storing anything else would let two traders
        # quote on different scales while appearing to agree.
        clean[self.goods[0]] = 1.0
        # The tick is stored whatever the switches say and only *read* when
        # `expiry` is on. One storage shape keeps islands comparable: `built`
        # must behave exactly as it did when its island was run, or the pair it
        # forms with `bound` stops being a pair.
        self.client.board_set(f"{self.quote_prefix}{self.agent_id}",
                              {"prices": clean, "tick": self._tick()})
        self.quotes_posted += 1
        return {"posted": clean}

    def clock(self) -> dict[str, Any]:
        """The run clock and the schedule, read straight off the board.

        Not asked for through the order queue. "What time is it" and "when does
        approving open" are facts about the world that every agent needs and
        none should spend a round trip on -- and an agent that had to queue for
        the time would be reading a time that had already passed.
        """
        clock = self.client.board_get(self.clock_key)
        out: dict[str, Any] = dict(clock) if isinstance(clock, dict) else {}
        if self.agenda_key:
            # The board key rather than the channel: the announcement can be
            # missed and the key cannot, so a lookup always answers with the
            # schedule that is actually in force.
            agenda = self.client.board_get(self.agenda_key)
            if isinstance(agenda, dict):
                out["agenda"] = agenda
        return out

    def _tick(self) -> int:
        """The manager's logical clock, read off the board.

        Not asked for through the order queue: "what round is it" is a fact
        about the world that every agent needs and none of them should have to
        spend a round trip on, so the sweep publishes it and anyone can read it.
        """
        clock = self.client.board_get(f"{self.clock_key}")
        return int(clock.get("tick", 0)) if isinstance(clock, dict) else 0

    def _board(self) -> dict[str, tuple[dict[str, float], int]]:
        out: dict[str, tuple[dict[str, float], int]] = {}
        for entry in self.client.board_list(prefix=self.quote_prefix):
            who = str(entry["key"]).rsplit("/", 1)[-1]
            value = entry.get("value")
            if isinstance(value, dict) and isinstance(value.get("prices"), dict):
                out[who] = (value["prices"], int(value.get("tick", 0)))
        return out

    @staticmethod
    def _median(values: list[float]) -> float:
        ordered = sorted(values)
        mid = len(ordered) // 2
        return (ordered[mid] if len(ordered) % 2
                else (ordered[mid - 1] + ordered[mid]) / 2)

    def read_quotes(self) -> dict[str, Any]:
        """Every trader's latest quote, and the median for each good.

        The median is the part that is genuinely machinery and not just
        storage. Turning a scatter of individual quotes into one number
        everybody computes identically is the step a price convention actually
        needs, and it is the step ``told`` leaves each agent to do in its head
        from prose — which is where a shared price stops being shared.

        Three switches shape the reply and each is separable. ``median`` adds
        the aggregate; ``expiry`` drops stale quotes and says whether yours is
        still live; ``deviation`` names *your* distance from the median rather
        than leaving you to notice it. ``built`` is median alone and ``bound``
        is all three, so the arms are reproduced exactly — but the two switches
        ``bound`` bundled can now be run apart, which is the only way to say
        which of them did the work.
        """
        board = self._board()
        if self.telling.expiry:
            now = self._tick()
            board = {who: (prices, tick) for who, (prices, tick) in board.items()
                     if now - tick < QUOTE_TTL_TICKS}
        quotes = {who: prices for who, (prices, _) in board.items()}

        reply: dict[str, Any] = {"quotes": quotes}
        medians: dict[str, float] = {}
        if self.telling.median:
            for good in self.goods:
                values = [q[good] for q in quotes.values() if good in q]
                if values:
                    medians[good] = self._median(values)
            reply["median_price"] = medians
        reply["traders_quoting"] = len(quotes)

        mine = quotes.get(self.agent_id)
        if self.telling.expiry:
            reply["quote_is_live"] = mine is not None
            if mine is None:
                reply["notice"] = ("You have no live quote. Quotes expire after "
                                   f"{QUOTE_TTL_TICKS} rounds; post one to stay on "
                                   "the board.")
        if self.telling.deviation:
            reply["your_quote"] = mine
            if mine is not None:
                # Ratio, not difference: prices span orders of magnitude here,
                # and "3.2x the median" is the form an agent can act on.
                reply["your_deviation_from_median"] = {
                    good: round(mine[good] / medians[good], 3)
                    for good in mine if medians.get(good)
                }
        return reply


def build_tools(wire: Wire) -> Any:
    """The MCP server one agent sees, wrapping :func:`build_tool_list`."""
    from claude_agent_sdk import create_sdk_mcp_server

    return create_sdk_mcp_server(name=f"island-{wire.agent_id}",
                                 tools=build_tool_list(wire))


def _tool_decorator() -> Any:
    """``claude_agent_sdk.tool``, or a stand-in of the same shape.

    The SDK is not installed in CI — it is a model client, and the offline gates
    never call a model — and without this the two gates that read the tool
    *surface* could not run there at all. Those are the gates that caught the
    silent arm's free-text channel and the ``produce`` description contradicting
    the labour rule, both of which shipped past every other check, so they are
    exactly the ones worth running everywhere.

    The decorator only packages four things: a name, a description, a schema and
    a handler. Where the SDK is installed that packaging is the real one and
    these gates read the real objects. Where it is not, this is the same four
    fields under the same names — enough to inspect a surface and not enough to
    serve it, which is correct, because ``build_tools`` still needs the SDK to
    serve anything and there is no model to serve it to.
    """
    try:
        from claude_agent_sdk import tool
    except ModuleNotFoundError:
        from types import SimpleNamespace

        def tool(name: str, description: str, input_schema: Any) -> Any:
            def wrap(handler: Any) -> Any:
                return SimpleNamespace(name=name, description=description,
                                       input_schema=input_schema, handler=handler)
            return wrap

    return tool


def build_tool_list(wire: Wire) -> list[Any]:
    """The tools one agent gets. Tool text is the whole interface.

    Split from the server so the surface can be *read* — the server hides its
    schemas behind an async handler, and "does a silent island offer any way to
    send free text" is a question a gate has to be able to ask. It could not,
    which is how `propose_trade`\'s note stayed a channel in the no-channel arm.

    Descriptions state mechanics and nothing strategic. A description that said
    "post your prices" would be the convention, handed over, and the island that
    was supposed to invent one would stop being an experiment.
    """
    tool = _tool_decorator()

    telling = wire.telling

    state_doc = "Your capacities, tastes and holdings"
    state_doc += (", and current score." if telling.own_score else ".")
    state_doc += " Nobody else's — this is private to you."

    @tool("my_state", state_doc, {})
    async def my_state(_: Any) -> dict[str, Any]:
        return _text(await _off(wire.state))

    # Switch-aware, because the fixed wording was false half the time. It said
    # "you may only do this once" to rolling agents whose brief, two layers up,
    # correctly told them they spend in instalments — the same contradiction
    # that was fixed in the system prompt and missed here, one level down.
    produce_doc = ("Spend this round's instalment of labour. Pass shares per "
                   "good, each >= 0, summing to at most 1 — they are fractions "
                   "of *this instalment*, and what you do not claim is not "
                   "carried over. Once per round."
                   if telling.rolling else
                   "Spend your one unit of labour. Pass shares per good, each "
                   ">= 0, summing to at most 1. You may only do this once.")

    @tool("produce", produce_doc, {"plan": dict})
    async def produce(args: Any) -> dict[str, Any]:
        return _text(await _off(lambda: wire.manager_call("produce", plan=args.get("plan"))))

    propose_doc = ("Offer trades. `trades` is a list, and one call can offer as "
                   "many as you like to as many traders as you like. For each: "
                   "`seller` is who it is addressed to, `give` is what you hand "
                   "over and is escrowed immediately, `want` is what you are "
                   "asking for. You get a trade id back for each. Nothing "
                   "settles until that trader approves the id. Offers that "
                   "cannot be covered are refused individually — the rest of "
                   "the list still stands.")
    propose_item = "{seller, give, want}"
    if telling.trade_note:
        propose_doc += " `note` is free text the seller will see."
        propose_item = "{seller, give, want, note}"
    propose_doc += f" Each entry is {propose_item}."

    @tool("propose_trade", propose_doc, {"trades": list})
    async def propose_trade(args: Any) -> dict[str, Any]:
        # Dropped rather than merely undocumented when the switch is off. An
        # argument absent from the schema can still be sent by a determined
        # caller, and this is the one field whose whole significance is that it
        # reaches another agent.
        ops = []
        for item in args.get("trades") or []:
            if not isinstance(item, dict):
                continue
            ops.append({"op": "propose", "seller": item.get("seller"),
                        "give": item.get("give"), "want": item.get("want"),
                        "note": str(item.get("note", "")) if telling.trade_note else ""})
        return _text(await _off(lambda: wire.manager_batch(ops)))

    @tool("approve_trade",
          "Settle trades that were offered to you. `trade_ids` is a list, so "
          "one call can settle as many as you mean to. Each is answered "
          "separately: one you cannot cover does not stop the others.",
          {"trade_ids": list})
    async def approve_trade(args: Any) -> dict[str, Any]:
        ops = [{"op": "approve", "trade_id": t} for t in (args.get("trade_ids") or [])]
        return _text(await _off(lambda: wire.manager_batch(ops)))

    pending_doc = "Offers waiting on your approval, and your own open offers."
    if telling.crossings:
        pending_doc += (" Also flags pairs where you and a counterparty have "
                        "each proposed the same swap, so both are escrowed.")

    @tool("pending_trades", pending_doc, {})
    async def pending_trades(_: Any) -> dict[str, Any]:
        return _text(await _off(wire.pending))

    @tool("cancel_trade",
          "Withdraw your own open offers and release their escrow. "
          "`trade_ids` is a list, so one call can withdraw several.",
          {"trade_ids": list})
    async def cancel_trade(args: Any) -> dict[str, Any]:
        ops = [{"op": "cancel", "trade_id": t} for t in (args.get("trade_ids") or [])]
        return _text(await _off(lambda: wire.manager_batch(ops)))

    @tool("ack", "Confirm you have read the posted schedule, by its version "
                 "number. Nothing opens until every trader has done this.",
          {"version": int})
    async def ack(args: Any) -> dict[str, Any]:
        return _text(await _off(lambda: wire.manager_call("ack",
                                                          version=args.get("version"))))

    @tool("clock", "What time it is on the run clock, and the schedule "
                   "everything runs to.", {})
    async def clock(_: Any) -> dict[str, Any]:
        return _text(await _off(wire.clock))

    tools = [my_state, produce, propose_trade, approve_trade, pending_trades,
             cancel_trade, ack, clock]

    if telling.channel:
        @tool("say", "Post a message all traders can read.", {"text": str})
        async def say(args: Any) -> dict[str, Any]:
            return _text(await _off(wire.post, str(args.get("text", ""))))

        @tool("listen", "Everything posted since you last called this.", {})
        async def listen(_: Any) -> dict[str, Any]:
            return _text(await _off(wire.read))

        @tool("history", "The whole public floor from the start of the run, "
                         "oldest first. Does not affect what `listen` shows you "
                         "next.", {})
        async def history(_: Any) -> dict[str, Any]:
            return _text(await _off(wire.history))

        tools += [say, listen, history]

    if telling.board:
        # Descriptions state mechanics only, for the same reason as everything
        # else here. What the convention is comes from the brief, which every
        # quoting island has word for word; what these add is somewhere to put
        # it. The wording tracks the switches because the affordance is
        # describing itself, which is the one place islands may diverge without
        # the comparison becoming one about prompts.
        stale = (f" Quotes expire after {QUOTE_TTL_TICKS} rounds; re-post to stay "
                 "on the board." if telling.expiry else "")

        @tool("post_quote",
              "Publish your prices on the shared quote board, in fish per unit. "
              "Replaces your previous quote. Every trader can read it." + stale,
              {"prices": dict})
        async def post_quote(args: Any) -> dict[str, Any]:
            return _text(await _off(wire.post_quote, args.get("prices")))

        read_doc = "The quote board: every trader's latest posted prices"
        read_doc += (", and the median price for each good across everyone quoting."
                     if telling.median else ".")
        if telling.deviation and telling.expiry:
            read_doc += (" Also reports how far your own quote sits from the median "
                         "on each good, as a multiple, and whether your quote is "
                         "still live.")
        elif telling.deviation:
            read_doc += (" Also reports how far your own quote sits from the median "
                         "on each good, as a multiple.")
        elif telling.expiry:
            read_doc += " Also reports whether your quote is still live."
        if telling.expiry:
            read_doc += " Expired quotes are not on the board."

        @tool("read_quotes", read_doc, {})
        async def read_quotes(_: Any) -> dict[str, Any]:
            return _text(await _off(wire.read_quotes))

        tools += [post_quote, read_quotes]

    if telling.pay_tool:
        @tool("pay",
              "Offer to buy `qty` of `good` from one named trader, paying in "
              "fish at the quote board's median price. Works out the fish amount "
              "for you and proposes the trade. The seller still has to approve "
              "it, exactly as with any other offer.",
              {"seller": str, "good": str, "qty": float})
        async def pay(args: Any) -> dict[str, Any]:
            return _text(await _off(wire.pay, args.get("seller"), args.get("good"),
                                    args.get("qty")))

        tools += [pay]

    return tools


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


async def _off(fn: Any, *args: Any) -> Any:
    """Run a blocking Switchboard call off the event loop.

    Every ``Wire`` method below talks to the hub over synchronous HTTP. Calling
    one directly from an ``async`` tool handler blocks the loop for the duration
    — which was survivable when each agent turn was its own short-lived
    subprocess, and is not now that every agent holds a live session for the
    whole island. One agent's blocking tool call stalls the transports of every
    other agent still connected, and the run deadlocks on the second turn.

    The switchboard client is deliberately synchronous (it depends only on
    httpx, which is the point of it), so the right place to fix this is here: do
    the blocking work in a worker thread and leave the loop free.
    """
    import anyio

    return await anyio.to_thread.run_sync(fn, *args)


def tool_names(spec: str | Telling, agent_id: str) -> list[str]:
    telling = telling_for(spec)
    # `ack` and `clock` are in every arm. They are not a convention and not an
    # affordance for coordinating -- they are how an agent finds out when the
    # island is open, which every arm needs equally and which no arm should be
    # measured on the absence of.
    names = ["my_state", "produce", "propose_trade", "approve_trade",
             "pending_trades", "cancel_trade", "ack", "clock"]
    if telling.channel:
        names += ["say", "listen", "history"]
    if telling.board:
        names += ["post_quote", "read_quotes"]
    if telling.pay_tool:
        names += ["pay"]
    return [f"mcp__island-{agent_id}__{name}" for name in names]


def brief_for(island: Island, manager: Manager, agent_id: str,
              spec: str | Telling) -> str:
    """The system prompt for one agent.

    Two islands whose only difference is an affordance must get byte-identical
    text — the machinery is then the only thing separating them, and a stray
    sentence pointing at the quote tools would turn the comparison into one
    about prompts again. The tools announce themselves through their own
    descriptions, which is what an affordance is. A test asserts it for
    ``told``/``built``, which is the pair the claim was originally made about.

    Only ``numeraire``, ``money``, ``tiebreak``, ``ruin_warning`` and
    ``rolling`` reach this text at all; every other switch is a tool.
    """
    telling = telling_for(spec)
    text = BRIEF.format(
        agent_id=agent_id, n_others=island.n_agents - 1, n_goods=island.n_goods,
        goods=", ".join(manager.goods),
        ruin=RUIN_CLAUSE if telling.ruin_warning else ".",
        labour=ROLLING_LABOUR if telling.rolling else ONCE_LABOUR,
        # A silent island must not be told it can talk. The window shape is the
        # world and every arm gets it, but the first window's contents differ by
        # what the arm actually has, and promising a channel that is not there
        # would be the one thing `silent` exists to measure the absence of.
        flow=FLOW_BRIEF.format(talk=" talk, and you can" if telling.channel
                               else ""),
    )
    if telling.channel:
        text += CHANNEL_BRIEF
    if telling.numeraire:
        text += NUMERAIRE_BRIEF
    if telling.money:
        text += MONEY_BRIEF
    if telling.tiebreak:
        text += TIEBREAK_BRIEF
    return text
