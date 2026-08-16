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
prices, choosing a numeraire, or using anything as money. Arm A simply has no
channel tool; ``free`` has ``say`` and ``listen`` and nothing about what to put
in them. If a convention appears in ``free`` it was invented, not followed — and
the Tier 1 arms give it a scale to be measured against.

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
from dataclasses import dataclass, field
from typing import Any

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
taste exponents — so a good you hold NONE of makes your score zero, however
much of everything else you have. You want some of every good.

You have one unit of labour. You spend it once, at the start, by calling
`produce` with the fraction you put on each good. Your capacity for each good
is how much you get for spending your whole unit on it, and it differs between
goods and between traders. After that you cannot make anything more — the only
way to change what you hold is to trade.

Trading is two-phase and the manager is the only thing that can move goods:
  - you call `propose_trade` naming one other trader, what you give and what
    you want. You get a trade id back, and what you offered is held in escrow
    from that moment — you cannot offer it to anyone else while it is open.
  - that named trader calls `approve_trade` with the id to settle it. Until
    they do, nothing has happened. Offers expire if nobody approves them.
  - check `pending_trades` for offers waiting on you.

Quantities are never negative. You cannot offer what you do not hold.
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

#: Arms, in order of how much is handed over. Named rather than lettered because
#: the Tier 1 arms are lettered and mean different things — Tier 1's D is money,
#: which is a different ladder.
#:
#: The design rule every one of these obeys: **a tool may calculate or report,
#: and may never commit anybody.** The quote board is a noticeboard; `pay` is a
#: calculator that builds a trade at the going rate. Neither binds a
#: counterparty — the manager is the only thing in the experiment that moves a
#: quantity, and it still knows nothing about prices. That is what keeps all of
#: this a *convention with a calculator* rather than an exchange with rules, and
#: it is why a standing-offer book is deliberately absent: a pre-committed offer
#: would settle without its owner acting, which is a commitment mechanism and a
#: different experiment.
ARMS = ("silent", "free", "told", "built", "bound", "spend", "paid")

#: How many manager ticks a quote stays on the board under ``bound``. Two means
#: a quote survives the round it was posted and one more, so an agent that never
#: comes back drops off rather than sitting there being read as current.
QUOTE_TTL_TICKS = 2

#: Index of the numeraire in the goods tuple. Which good is arbitrary; that
#: everyone uses the *same* one is the entire convention.
NUMERAIRE_INDEX = 0


def speaks(arm: str) -> bool:
    return arm != "silent"


def has_quote_board(arm: str) -> bool:
    return arm in ("built", "bound", "spend", "paid")


def uses_money(arm: str) -> bool:
    """``spend`` and ``paid``: the numeraire is a medium of exchange too."""
    return arm in ("spend", "paid")


def has_pay_tool(arm: str) -> bool:
    """``paid``: a calculator for money trades, not a commitment mechanism.

    It looks up the going rate, does the arithmetic and constructs the offer.
    The seller still has to approve it, exactly as for any other trade — an
    affordance that settled by itself would destroy the voluntariness every
    "nobody was made worse off" claim in this experiment rests on.
    """
    return arm == "paid"


def obliges_revision(arm: str) -> bool:
    """``bound``: the board pushes back instead of merely storing.

    ``built`` answered the aggregation question and the answer was no — four
    traders had a median in every reply and ended 27x apart on cloth, because a
    number you are shown is not a number you act on. So ``bound`` changes what
    the board *does* rather than what it holds, in the two smallest ways that
    turn a display into a demand:

    * it reports **your** price against the median, per good, as a deviation —
      the comparison every agent could have made and none did;
    * a quote **goes stale**, so staying on the board is something you have to
      keep doing rather than something you did once.

    Both are still only about quoting. Nothing obliges an agent to trade at any
    price, and the manager remains ignorant of prices entirely.
    """
    return arm in ("bound", "spend", "paid")

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
    arm: str
    floor_channel: str
    #: Blackboard prefix for the `built` arm's quote board. One key per trader,
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
        # The tick is stored for every arm and only *read* under `bound`. One
        # storage shape keeps the arms comparable: `built` must behave exactly
        # as it did when its island was run, or the pair stops being a pair.
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

        Under ``bound`` two things are added, and they are the whole difference
        between the arms: stale quotes drop off, and the reply names *your*
        distance from the median rather than leaving you to notice it. ``built``
        gets exactly the reply it always got.
        """
        board = self._board()
        if obliges_revision(self.arm):
            now = self._tick()
            board = {who: (prices, tick) for who, (prices, tick) in board.items()
                     if now - tick < QUOTE_TTL_TICKS}
        quotes = {who: prices for who, (prices, _) in board.items()}
        medians = {}
        for good in self.goods:
            values = [q[good] for q in quotes.values() if good in q]
            if values:
                medians[good] = self._median(values)

        reply: dict[str, Any] = {"quotes": quotes, "median_price": medians,
                                 "traders_quoting": len(quotes)}
        if not obliges_revision(self.arm):
            return reply

        mine = quotes.get(self.agent_id)
        reply["your_quote"] = mine
        reply["quote_is_live"] = mine is not None
        if mine is None:
            reply["notice"] = ("You have no live quote. Quotes expire after "
                               f"{QUOTE_TTL_TICKS} rounds; post one to stay on the board.")
        else:
            # Ratio, not difference: prices span orders of magnitude here, and
            # "3.2x the median" is the form an agent can act on.
            reply["your_deviation_from_median"] = {
                good: round(mine[good] / medians[good], 3)
                for good in mine if medians.get(good)
            }
        return reply


def build_tools(wire: Wire) -> Any:
    """The MCP server one agent sees. Tool text is the whole interface.

    Descriptions state mechanics and nothing strategic. A description that said
    "post your prices" would be the convention, handed over, and arm B would
    stop being an experiment.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("my_state", "Your capacities, tastes, holdings and current score. "
                      "Nobody else's — this is private to you.", {})
    async def my_state(_: Any) -> dict[str, Any]:
        return _text(wire.manager_call("state"))

    @tool("produce",
          "Spend your one unit of labour. Pass shares per good, each >= 0, "
          "summing to at most 1. You may only do this once.",
          {"plan": dict})
    async def produce(args: Any) -> dict[str, Any]:
        return _text(wire.manager_call("produce", plan=args.get("plan")))

    @tool("propose_trade",
          "Offer a trade to one named trader. `give` is what you hand over and "
          "is escrowed immediately; `want` is what you are asking for. Returns "
          "a trade id. Nothing settles until that trader approves the id.",
          {"seller": str, "give": dict, "want": dict, "note": str})
    async def propose_trade(args: Any) -> dict[str, Any]:
        return _text(wire.manager_call(
            "propose", seller=args.get("seller"), give=args.get("give"),
            want=args.get("want"), note=args.get("note", "")))

    @tool("approve_trade", "Settle a trade that was offered to you, by its id.",
          {"trade_id": str})
    async def approve_trade(args: Any) -> dict[str, Any]:
        return _text(wire.manager_call("approve", trade_id=args.get("trade_id")))

    @tool("pending_trades", "Offers waiting on your approval, and your own open offers.", {})
    async def pending_trades(_: Any) -> dict[str, Any]:
        return _text(wire.manager_call("pending"))

    @tool("cancel_trade", "Withdraw one of your own open offers and release its escrow.",
          {"trade_id": str})
    async def cancel_trade(args: Any) -> dict[str, Any]:
        return _text(wire.manager_call("cancel", trade_id=args.get("trade_id")))

    tools = [my_state, produce, propose_trade, approve_trade, pending_trades, cancel_trade]

    if speaks(wire.arm):
        @tool("say", "Post a message all traders can read.", {"text": str})
        async def say(args: Any) -> dict[str, Any]:
            return _text(wire.post(str(args.get("text", ""))))

        @tool("listen", "Everything posted since you last called this.", {})
        async def listen(_: Any) -> dict[str, Any]:
            return _text(wire.read())

        tools += [say, listen]

    if has_quote_board(wire.arm):
        # Descriptions state mechanics only, for the same reason as everything
        # else here. What the convention is comes from the brief, which every
        # quoting arm has word for word; what these add is somewhere to put it.
        # `bound`'s wording differs because its machinery differs — that is the
        # affordance describing itself, which is the one place the arms are
        # allowed to diverge.
        stale = (f" Quotes expire after {QUOTE_TTL_TICKS} rounds; re-post to stay "
                 "on the board." if obliges_revision(wire.arm) else "")

        @tool("post_quote",
              "Publish your prices on the shared quote board, in fish per unit. "
              "Replaces your previous quote. Every trader can read it." + stale,
              {"prices": dict})
        async def post_quote(args: Any) -> dict[str, Any]:
            return _text(wire.post_quote(args.get("prices")))

        read_doc = ("The quote board: every trader's latest posted prices, and the "
                    "median price for each good across everyone quoting.")
        if obliges_revision(wire.arm):
            read_doc += (" Also reports how far your own quote sits from the median "
                         "on each good, as a multiple, and whether your quote is "
                         "still live. Expired quotes are not on the board.")

        @tool("read_quotes", read_doc, {})
        async def read_quotes(_: Any) -> dict[str, Any]:
            return _text(wire.read_quotes())

        tools += [post_quote, read_quotes]

    if has_pay_tool(wire.arm):
        @tool("pay",
              "Offer to buy `qty` of `good` from one named trader, paying in "
              "fish at the quote board's median price. Works out the fish amount "
              "for you and proposes the trade. The seller still has to approve "
              "it, exactly as with any other offer.",
              {"seller": str, "good": str, "qty": float})
        async def pay(args: Any) -> dict[str, Any]:
            return _text(wire.pay(args.get("seller"), args.get("good"), args.get("qty")))

        tools += [pay]

    return create_sdk_mcp_server(name=f"island-{wire.agent_id}", tools=tools)


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def tool_names(arm: str, agent_id: str) -> list[str]:
    names = ["my_state", "produce", "propose_trade", "approve_trade",
             "pending_trades", "cancel_trade"]
    if speaks(arm):
        names += ["say", "listen"]
    if has_quote_board(arm):
        names += ["post_quote", "read_quotes"]
    if has_pay_tool(arm):
        names += ["pay"]
    return [f"mcp__island-{agent_id}__{name}" for name in names]


def brief_for(island: Island, manager: Manager, agent_id: str, arm: str) -> str:
    """The system prompt for one agent.

    ``told`` and ``built`` must return byte-identical text — the machinery is
    the only thing separating them, and a stray sentence pointing at the quote
    tools would turn the comparison into one about prompts again. The tools
    announce themselves through their own descriptions, which is what an
    affordance is.
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {', '.join(ARMS)}")
    text = BRIEF.format(
        agent_id=agent_id, n_others=island.n_agents - 1, n_goods=island.n_goods,
        goods=", ".join(manager.goods),
    )
    if speaks(arm):
        text += CHANNEL_BRIEF
    if arm in ("told", "built", "bound", "spend", "paid"):
        text += NUMERAIRE_BRIEF
    if uses_money(arm):
        text += MONEY_BRIEF
    return text
