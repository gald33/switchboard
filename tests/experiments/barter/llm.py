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
from .manager import Manager, ManagerService

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
"""

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
You have one unit of labour. You spend it once, at the start, by calling
`produce` with the fraction you put on each good. Your capacity for each good
is how much you get for spending your whole unit on it, and it differs between
goods and between traders. After that you cannot make anything more — the only
way to change what you hold is to trade."""

ROLLING_LABOUR = """\
You have one unit of labour and you spend it in instalments: in each round you
may call `produce` once, with the fraction of *that round's* instalment you put
on each good. Your capacity for each good is how much you get for spending your
whole unit on it, and it differs between goods and between traders. An
instalment you do not spend is not carried over — it is a round you did not
work — and once the unit is gone the only way to change what you hold is to
trade."""

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
      passes on, and ``horizon`` and ``labour_left`` what the turn note says.
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
                     rolling=self.rolling)


_SWITCHES: tuple[str, ...] = (
    "channel", "numeraire", "board", "median", "deviation", "expiry",
    "money", "pay_tool", "rolling", "ruin_warning", "horizon", "labour_left",
    "own_value", "own_score",
)

#: The named arms, as combinations. They are kept because the merged results are
#: reported under these names and a rung of the ladder should stay one word —
#: but nothing reads a name any more, only the switches it stands for.
PRESETS: dict[str, Telling] = {
    "silent": Telling(),
    "free": Telling(channel=True),
    "told": Telling(channel=True, numeraire=True),
    "built": Telling(channel=True, numeraire=True, board=True, median=True),
    "bound": Telling(channel=True, numeraire=True, board=True, median=True,
                     deviation=True, expiry=True),
    "spend": Telling(channel=True, numeraire=True, board=True, median=True,
                     deviation=True, expiry=True, money=True),
    "paid": Telling(channel=True, numeraire=True, board=True, median=True,
                    deviation=True, expiry=True, money=True, pay_tool=True),
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

Do what you think is best. When you are done for this round, stop.
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
    service: ManagerService
    telling: Telling
    floor_channel: str
    #: Blackboard prefix for the quote board. One key per trader,
    #: so a quote is a value anyone can read rather than a message somebody
    #: might have scrolled past.
    quote_prefix: str = ""
    goods: tuple[str, ...] = ()
    calls: list[dict[str, Any]] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
    quotes_posted: int = 0
    _cursor: int = 0

    def manager_call(self, op: str, **kwargs: Any) -> dict[str, Any]:
        self.client.send("manager", {"op": op, **kwargs})
        self.service.drain()
        for message in self.client.inbox(channels=[f"@{self.agent_id}"]):
            body = message.get("body")
            if isinstance(body, dict) and body.get("op") == op:
                self.calls.append({"op": op, "ok": body.get("ok")})
                return body
        return {"ok": False, "error": "manager did not reply"}

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

    def post(self, text: str) -> dict[str, Any]:
        self.said.append(text)
        self.client.post(self.floor_channel, {"from": self.agent_id, "text": text})
        return {"posted": True}

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
        reply = self.manager_call("propose", seller=str(seller),
                                  give={numeraire: round(fish, 6)},
                                  want={name: round(amount, 6)},
                                  note=f"at the board median, {medians[name]:g} {numeraire}/{name}")
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

    def _tick(self) -> int:
        return int(self.service.manager.tick)

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
    """The MCP server one agent sees. Tool text is the whole interface.

    Descriptions state mechanics and nothing strategic. A description that said
    "post your prices" would be the convention, handed over, and the island that
    was supposed to invent one would stop being an experiment.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    telling = wire.telling

    state_doc = "Your capacities, tastes and holdings"
    state_doc += (", and current score." if telling.own_score else ".")
    state_doc += " Nobody else's — this is private to you."

    @tool("my_state", state_doc, {})
    async def my_state(_: Any) -> dict[str, Any]:
        return _text(await _off(wire.state))

    @tool("produce",
          "Spend your one unit of labour. Pass shares per good, each >= 0, "
          "summing to at most 1. You may only do this once.",
          {"plan": dict})
    async def produce(args: Any) -> dict[str, Any]:
        return _text(await _off(lambda: wire.manager_call("produce", plan=args.get("plan"))))

    @tool("propose_trade",
          "Offer a trade to one named trader. `give` is what you hand over and "
          "is escrowed immediately; `want` is what you are asking for. Returns "
          "a trade id. Nothing settles until that trader approves the id.",
          {"seller": str, "give": dict, "want": dict, "note": str})
    async def propose_trade(args: Any) -> dict[str, Any]:
        return _text(await _off(lambda: wire.manager_call(
            "propose", seller=args.get("seller"), give=args.get("give"),
            want=args.get("want"), note=args.get("note", ""))))

    @tool("approve_trade", "Settle a trade that was offered to you, by its id.",
          {"trade_id": str})
    async def approve_trade(args: Any) -> dict[str, Any]:
        trade_id = args.get("trade_id")
        return _text(await _off(lambda: wire.manager_call("approve", trade_id=trade_id)))

    @tool("pending_trades", "Offers waiting on your approval, and your own open offers.", {})
    async def pending_trades(_: Any) -> dict[str, Any]:
        return _text(await _off(wire.manager_call, "pending"))

    @tool("cancel_trade", "Withdraw one of your own open offers and release its escrow.",
          {"trade_id": str})
    async def cancel_trade(args: Any) -> dict[str, Any]:
        return _text(await _off(lambda: wire.manager_call("cancel", trade_id=args.get("trade_id"))))

    tools = [my_state, produce, propose_trade, approve_trade, pending_trades, cancel_trade]

    if telling.channel:
        @tool("say", "Post a message all traders can read.", {"text": str})
        async def say(args: Any) -> dict[str, Any]:
            return _text(await _off(wire.post, str(args.get("text", ""))))

        @tool("listen", "Everything posted since you last called this.", {})
        async def listen(_: Any) -> dict[str, Any]:
            return _text(await _off(wire.read))

        tools += [say, listen]

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

    return create_sdk_mcp_server(name=f"island-{wire.agent_id}", tools=tools)


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
    names = ["my_state", "produce", "propose_trade", "approve_trade",
             "pending_trades", "cancel_trade"]
    if telling.channel:
        names += ["say", "listen"]
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

    Only ``numeraire``, ``money``, ``ruin_warning`` and ``rolling`` reach this
    text at all; every other switch is a tool.
    """
    telling = telling_for(spec)
    text = BRIEF.format(
        agent_id=agent_id, n_others=island.n_agents - 1, n_goods=island.n_goods,
        goods=", ".join(manager.goods),
        ruin=RUIN_CLAUSE if telling.ruin_warning else ".",
        labour=ROLLING_LABOUR if telling.rolling else ONCE_LABOUR,
    )
    if telling.channel:
        text += CHANNEL_BRIEF
    if telling.numeraire:
        text += NUMERAIRE_BRIEF
    if telling.money:
        text += MONEY_BRIEF
    return text
