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

**``told`` against ``built`` is the arm that matters, and the two share a system
prompt byte for byte.** A test asserts that. The only difference is whether the
convention has an affordance or is merely described, which is the difference
between telling agents how to coordinate and building them something to
coordinate *with*. If ``told`` matches ``built``, the machinery is ceremony and
the words were doing the work. If ``built`` wins, then knowing a convention and
being able to run one are different things — and that is a claim about what a
coordination substrate should offer, not about what a prompt should say.

``free`` against ``told`` is the cheaper prior question: is the convention
something models will not invent but will happily adopt?

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

#: Arms, in order of how much is handed over. Named rather than lettered because
#: the Tier 1 arms are lettered and mean different things — Tier 1's D is money,
#: which is not on this ladder at all.
ARMS = ("silent", "free", "told", "built")


def speaks(arm: str) -> bool:
    return arm != "silent"


def has_quote_board(arm: str) -> bool:
    return arm == "built"

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
        self.client.board_set(f"{self.quote_prefix}{self.agent_id}", clean)
        self.quotes_posted += 1
        return {"posted": clean}

    def read_quotes(self) -> dict[str, Any]:
        """Every trader's latest quote, and the median for each good.

        The median is the part that is genuinely machinery and not just
        storage. Turning a scatter of individual quotes into one number
        everybody computes identically is the step a price convention actually
        needs, and it is the step ``told`` leaves each agent to do in its head
        from prose — which is where a shared price stops being shared.
        """
        entries = self.client.board_list(prefix=self.quote_prefix)
        quotes: dict[str, dict[str, float]] = {}
        for entry in entries:
            who = str(entry["key"]).rsplit("/", 1)[-1]
            if isinstance(entry.get("value"), dict):
                quotes[who] = entry["value"]
        medians = {}
        for good in self.goods:
            values = sorted(q[good] for q in quotes.values() if good in q)
            if values:
                mid = len(values) // 2
                medians[good] = (values[mid] if len(values) % 2
                                 else (values[mid - 1] + values[mid]) / 2)
        return {"quotes": quotes, "median_price": medians, "traders_quoting": len(quotes)}


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
        # else here. What the convention is comes from the brief, which `told`
        # has word for word; what these add is somewhere to put it.
        @tool("post_quote",
              "Publish your prices on the shared quote board, in fish per unit. "
              "Replaces your previous quote. Every trader can read it.",
              {"prices": dict})
        async def post_quote(args: Any) -> dict[str, Any]:
            return _text(wire.post_quote(args.get("prices")))

        @tool("read_quotes",
              "The quote board: every trader's latest posted prices, and the "
              "median price for each good across everyone quoting.", {})
        async def read_quotes(_: Any) -> dict[str, Any]:
            return _text(wire.read_quotes())

        tools += [post_quote, read_quotes]

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
    if arm in ("told", "built"):
        text += NUMERAIRE_BRIEF
    return text
