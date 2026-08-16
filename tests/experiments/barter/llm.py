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
channel tool; arm B has ``say`` and ``listen`` and nothing about what to put in
them. If a convention appears in arm B it was invented, not followed — and the
Tier 1 arms give it a scale to be measured against.

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
    calls: list[dict[str, Any]] = field(default_factory=list)
    said: list[str] = field(default_factory=list)
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

    if wire.arm != "A":
        @tool("say", "Post a message all traders can read.", {"text": str})
        async def say(args: Any) -> dict[str, Any]:
            return _text(wire.post(str(args.get("text", ""))))

        @tool("listen", "Everything posted since you last called this.", {})
        async def listen(_: Any) -> dict[str, Any]:
            return _text(wire.read())

        tools += [say, listen]

    return create_sdk_mcp_server(name=f"island-{wire.agent_id}", tools=tools)


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def tool_names(arm: str, agent_id: str) -> list[str]:
    names = ["my_state", "produce", "propose_trade", "approve_trade",
             "pending_trades", "cancel_trade"]
    if arm != "A":
        names += ["say", "listen"]
    return [f"mcp__island-{agent_id}__{name}" for name in names]


def brief_for(island: Island, manager: Manager, agent_id: str, arm: str) -> str:
    text = BRIEF.format(
        agent_id=agent_id, n_others=island.n_agents - 1, n_goods=island.n_goods,
        goods=", ".join(manager.goods),
    )
    return text + (CHANNEL_BRIEF if arm != "A" else "")
