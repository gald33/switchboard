"""The manager: the only thing on the island that may change a quantity.

This is an *application* built on the four Switchboard primitives, not a fifth
primitive. Nothing here needs the hub to know what a trade is:

    blackboard   the authoritative state, one key per agent plus a run header.
                 Survives a manager restart and can be read by a viewer.
    messages     the request/reply transport. An agent DMs ``@manager``; the
                 manager DMs back. A public ledger channel carries executed
                 trades when the arm allows it.
    leases       the mutex on state. One manager at a time, and it expires if
                 the manager dies rather than wedging the run.
    presence     who is still trading.

Why a manager at all
--------------------
Because the interesting question is about *conventions*, and a convention is
only meaningful against a rule that cannot be talked around. If agents kept
their own balances, an agent could report a trade that never happened, or spend
the same fish twice, and a run that ended above the Pareto frontier would be a
bookkeeping bug rather than a finding. Every quantity in this experiment moves
through one state machine that enforces:

* **Non-negativity.** No holding, no escrow, no proposed quantity is ever
  negative. Checked on every mutation, not at the end.
* **Conservation.** Holdings plus escrow equals everything ever produced.
  Trade moves goods; it never creates them.
* **Two-phase settlement.** A trade is proposed by the buyer, which returns a
  trade id and *escrows the buyer's side immediately*, and executes only when
  the named seller approves that id. Nobody else can approve it, and it can
  only execute once.

Escrow is the design decision worth defending. Without it a buyer can promise
the same ten fish to five sellers and four of them discover it at settlement,
which turns every proposal into a lottery ticket and makes a posted price
meaningless — you cannot study conventions in a market where a quote is not
binding. With escrow a proposal is a commitment, so the cost of proposing badly
is borne by the proposer, and an unaccepted offer ties up goods until it
expires. That tradeoff is itself something the arms can be judged on.

Everything expires, including trades. A proposal nobody answers releases its
escrow on its own, which is the Switchboard lease argument applied to goods: the
release is the half that gets dropped, so nothing should depend on it happening.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .economy import GOOD_NAMES, Island, mrs, utility

#: Proposals expire after this many manager ticks. A tick is one round of the
#: run, not a wall-clock second: the experiment must be reproducible, so the
#: manager owns a logical clock rather than reading the wall.
TRADE_TTL_TICKS = 3

#: Quantities below this are treated as zero when checking coverage, so a float
#: subtraction cannot leave an agent unable to spend what it visibly holds.
_EPS = 1e-9


class TradeError(Exception):
    """A rejected request. The message is shown to the agent verbatim."""


@dataclass
class Trade:
    id: str
    buyer: str
    seller: str
    #: What the buyer hands over. Escrowed at propose time.
    give: dict[str, float]
    #: What the buyer wants back. Taken from the seller at approve time.
    want: dict[str, float]
    note: str = ""
    status: str = "pending"
    opened_tick: int = 0
    closed_tick: int | None = None
    reason: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "buyer": self.buyer, "seller": self.seller,
            "give": dict(self.give), "want": dict(self.want),
            "status": self.status, "note": self.note,
        }


@dataclass
class AgentState:
    agent_id: str
    index: int
    alpha: tuple[float, ...]
    capacity: tuple[float, ...]
    holdings: list[float]
    produced: bool = False
    shares: tuple[float, ...] | None = None


@dataclass
class Manager:
    """Authoritative state for one island. Pure — no hub, no clock, no I/O.

    Split from the serving loop on purpose: every invariant worth asserting is
    assertable here, in-process and instantly, and the transport cannot be the
    reason a run balances.
    """

    island: Island
    #: ``discovery`` accepts neither labour plans nor trades — it is time to talk
    #: before anything is committed; ``production`` accepts labour plans;
    #: ``trading`` accepts trades; ``closed`` accepts neither. The manager owns
    #: the phase because a phase agents could advance is a phase one agent can
    #: advance early.
    #:
    #: ``discovery`` exists because of a flaw the Tier 2 runs exposed in
    #: themselves. Production was committed in the first round, before any agent
    #: had said anything, so every convention on the ladder could only ever
    #: describe the world after the one irreversible decision was behind it.
    #: Implied production quality came out at ~0.41 in every arm — the exchange
    #: ceiling — because no arm had the information to specialise, whatever it
    #: had been told. Deliberation has to come before manufacturing or it cannot
    #: plan for it.
    #:
    #: It defaults off. Tier 1 constructs its manager without it and starts in
    #: ``production``, exactly as before, because its scripted agents do their
    #: price discovery outside the manager entirely.
    phase: str = "production"
    tick: int = 0
    agents: dict[str, AgentState] = field(default_factory=dict)
    trades: dict[str, Trade] = field(default_factory=dict)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 1
    #: Rejected requests, kept because "how often did agents propose something
    #: impossible" is a measure of how well a convention is working.
    rejections: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.agents:
            for i, agent_id in enumerate(self.island.agent_ids()):
                self.agents[agent_id] = AgentState(
                    agent_id=agent_id, index=i,
                    alpha=self.island.alpha[i], capacity=self.island.capacity[i],
                    holdings=[0.0] * self.island.n_goods,
                )

    # --- helpers ------------------------------------------------------------

    @property
    def goods(self) -> list[str]:
        return list(GOOD_NAMES[: self.island.n_goods])

    def _agent(self, agent_id: str) -> AgentState:
        state = self.agents.get(agent_id)
        if state is None:
            raise TradeError(f"unknown agent {agent_id!r}")
        return state

    def _index(self, good: str) -> int:
        try:
            return self.goods.index(good)
        except ValueError:
            raise TradeError(f"unknown good {good!r}; goods are {', '.join(self.goods)}") from None

    def _bundle(self, raw: Any, label: str) -> dict[str, float]:
        """Validate a ``{good: qty}`` map. Non-negativity starts here."""
        if not isinstance(raw, dict) or not raw:
            raise TradeError(f"{label} must be a non-empty object like {{\"fish\": 2.0}}")
        out: dict[str, float] = {}
        for good, qty in raw.items():
            self._index(str(good))
            try:
                amount = float(qty)
            except (TypeError, ValueError):
                raise TradeError(f"{label}[{good!r}] is not a number") from None
            if amount != amount or amount in (float("inf"), float("-inf")):
                raise TradeError(f"{label}[{good!r}] is not finite")
            if amount < 0:
                raise TradeError(f"{label}[{good!r}] is negative; quantities must be >= 0")
            if amount > _EPS:
                out[str(good)] = amount
        if not out:
            raise TradeError(f"{label} is all zeros; a trade must move something")
        return out

    def _covers(self, state: AgentState, bundle: dict[str, float]) -> str | None:
        for good, qty in bundle.items():
            have = state.holdings[self._index(good)]
            if have + _EPS < qty:
                return f"{state.agent_id} holds {have:.4f} {good}, needs {qty:.4f}"
        return None

    def _move(self, state: AgentState, bundle: dict[str, float], sign: int) -> None:
        for good, qty in bundle.items():
            g = self._index(good)
            after = state.holdings[g] + sign * qty
            # The invariant, enforced where it can actually be violated. A
            # negative holding here is a bug in this file, not bad input --
            # input was validated on the way in.
            if after < -_EPS:
                raise AssertionError(f"holding went negative: {state.agent_id}/{good} -> {after}")
            state.holdings[g] = max(after, 0.0)

    # --- operations ---------------------------------------------------------

    def op_state(self, agent_id: str) -> dict[str, Any]:
        """Everything an agent knows without talking to anyone.

        Deliberately private. An agent sees its own capacities, tastes, holdings
        and marginal values, and nothing at all about anyone else — that is what
        leaves room for communication to matter. The one public number is the
        list of goods.
        """
        state = self._agent(agent_id)
        rates = mrs(state.alpha, [max(h, 1e-9) for h in state.holdings])
        return {
            "you": agent_id,
            "phase": self.phase,
            "tick": self.tick,
            "goods": self.goods,
            "capacity": {g: round(state.capacity[i], 4) for i, g in enumerate(self.goods)},
            "taste": {g: round(state.alpha[i], 4) for i, g in enumerate(self.goods)},
            "holdings": {g: round(state.holdings[i], 4) for i, g in enumerate(self.goods)},
            "utility": round(utility(state.alpha, state.holdings), 6),
            "value_per_unit": {g: round(rates[i], 4) for i, g in enumerate(self.goods)},
            "produced": state.produced,
            "escrowed": self._escrow_of(agent_id),
        }

    def _escrow_of(self, agent_id: str) -> dict[str, float]:
        held: dict[str, float] = {}
        for trade in self.trades.values():
            if trade.status == "pending" and trade.buyer == agent_id:
                for good, qty in trade.give.items():
                    held[good] = round(held.get(good, 0.0) + qty, 6)
        return held

    def op_produce(self, agent_id: str, plan: Any) -> dict[str, Any]:
        """Commit a labour allocation. Once, and only while production is open.

        ``plan`` is ``{good: share}`` with shares >= 0 summing to at most 1 --
        one unit of labour. Shares are not renormalised if they sum to less than
        one: idle labour is a choice an agent is allowed to make badly.
        """
        if self.phase != "production":
            raise TradeError(f"production is {self.phase}, not open")
        state = self._agent(agent_id)
        if state.produced:
            raise TradeError("you have already produced; the plan is committed for the run")

        shares = [0.0] * self.island.n_goods
        if not isinstance(plan, dict) or not plan:
            raise TradeError('plan must be an object like {"fish": 0.5, "grain": 0.5}')
        for good, share in plan.items():
            g = self._index(str(good))
            try:
                value = float(share)
            except (TypeError, ValueError):
                raise TradeError(f"share for {good!r} is not a number") from None
            if value != value or value < 0:
                raise TradeError(f"share for {good!r} must be >= 0")
            shares[g] += value
        total = sum(shares)
        if total > 1.0 + 1e-6:
            raise TradeError(f"shares sum to {total:.4f}; you have 1.0 unit of labour")

        state.shares = tuple(shares)
        state.produced = True
        for g in range(self.island.n_goods):
            state.holdings[g] += state.capacity[g] * shares[g]
        return {
            "produced": {g: round(state.capacity[i] * shares[i], 4)
                         for i, g in enumerate(self.goods)},
            "idle_labour": round(max(0.0, 1.0 - total), 4),
            "holdings": {g: round(state.holdings[i], 4) for i, g in enumerate(self.goods)},
        }

    def op_propose(self, buyer: str, seller: str, give: Any, want: Any,
                   note: str = "") -> dict[str, Any]:
        """Buyer offers ``give`` for ``want``. Returns a trade id.

        The buyer's side is escrowed here, not at settlement. That is what makes
        the id worth anything to the seller: by the time it can approve, the
        goods are already out of the buyer's hands.
        """
        if self.phase != "trading":
            raise TradeError(f"trading is {self.phase}, not open")
        buyer_state = self._agent(buyer)
        if seller == buyer:
            raise TradeError("you cannot trade with yourself")
        self._agent(seller)

        give_bundle = self._bundle(give, "give")
        want_bundle = self._bundle(want, "want")
        shortfall = self._covers(buyer_state, give_bundle)
        if shortfall:
            free = self._escrow_of(buyer)
            raise TradeError(
                f"you cannot cover this offer: {shortfall}"
                + (f" (escrowed in open proposals: {free})" if free else "")
            )

        trade = Trade(
            id=f"t{self._next_id}", buyer=buyer, seller=seller,
            give=give_bundle, want=want_bundle, note=str(note)[:200],
            opened_tick=self.tick,
        )
        self._next_id += 1
        self.trades[trade.id] = trade
        self._move(buyer_state, give_bundle, -1)  # into escrow
        return {
            "trade_id": trade.id,
            "escrowed": {g: round(q, 4) for g, q in give_bundle.items()},
            "expires_tick": trade.opened_tick + TRADE_TTL_TICKS,
            "note": "the seller must approve this id before anything settles",
        }

    def op_approve(self, seller: str, trade_id: str) -> dict[str, Any]:
        """Seller settles a trade by id. The only way goods change hands."""
        if self.phase != "trading":
            raise TradeError(f"trading is {self.phase}, not open")
        trade = self.trades.get(str(trade_id))
        if trade is None:
            raise TradeError(f"no trade {trade_id!r}")
        if trade.seller != seller:
            # Named-seller check. Without it any agent could settle any offer,
            # and "propose to a specific counterparty" would carry no meaning.
            raise TradeError(f"trade {trade.id} is addressed to {trade.seller}, not you")
        if trade.status != "pending":
            raise TradeError(f"trade {trade.id} is already {trade.status}")

        seller_state = self._agent(seller)
        buyer_state = self._agent(trade.buyer)
        shortfall = self._covers(seller_state, trade.want)
        if shortfall:
            self._settle(trade, "rejected", f"seller short: {shortfall}")
            raise TradeError(f"you cannot cover this trade: {shortfall}; the offer was returned")

        # Atomic in the only sense that matters here: every check has passed
        # before the first quantity moves, so there is no half-executed trade.
        self._move(seller_state, trade.want, -1)
        self._move(seller_state, trade.give, +1)  # escrow releases to the seller
        self._move(buyer_state, trade.want, +1)
        trade.status = "executed"
        trade.closed_tick = self.tick
        record = {
            "tick": self.tick, "id": trade.id, "buyer": trade.buyer, "seller": trade.seller,
            "give": dict(trade.give), "want": dict(trade.want),
        }
        self.ledger.append(record)
        return {
            "settled": trade.id,
            "you_gave": {g: round(q, 4) for g, q in trade.want.items()},
            "you_received": {g: round(q, 4) for g, q in trade.give.items()},
            "holdings": {g: round(seller_state.holdings[i], 4) for i, g in enumerate(self.goods)},
            "utility": round(utility(seller_state.alpha, seller_state.holdings), 6),
        }

    def op_cancel(self, buyer: str, trade_id: str) -> dict[str, Any]:
        trade = self.trades.get(str(trade_id))
        if trade is None:
            raise TradeError(f"no trade {trade_id!r}")
        if trade.buyer != buyer:
            raise TradeError(f"trade {trade.id} is not yours to cancel")
        if trade.status != "pending":
            raise TradeError(f"trade {trade.id} is already {trade.status}")
        self._settle(trade, "cancelled", "withdrawn by buyer")
        return {"cancelled": trade.id, "returned": {g: round(q, 4) for g, q in trade.give.items()}}

    def op_pending(self, agent_id: str) -> dict[str, Any]:
        """Open trades this agent is party to, split by who must act."""
        self._agent(agent_id)
        awaiting = [t.public() for t in self.trades.values()
                    if t.status == "pending" and t.seller == agent_id]
        mine = [t.public() for t in self.trades.values()
                if t.status == "pending" and t.buyer == agent_id]
        return {"awaiting_your_approval": awaiting, "your_open_offers": mine}

    def _settle(self, trade: Trade, status: str, reason: str) -> None:
        """Close a pending trade without executing it, returning the escrow."""
        self._move(self._agent(trade.buyer), trade.give, +1)
        trade.status = status
        trade.reason = reason
        trade.closed_tick = self.tick
        if status in ("rejected", "expired"):
            self.rejections.append({"tick": self.tick, "id": trade.id, "status": status,
                                    "reason": reason})

    # --- clock and phases ---------------------------------------------------

    def advance(self) -> list[str]:
        """One tick. Expires stale proposals and returns their escrow."""
        self.tick += 1
        expired = []
        for trade in self.trades.values():
            if trade.status == "pending" and self.tick - trade.opened_tick >= TRADE_TTL_TICKS:
                self._settle(trade, "expired", "nobody approved it in time")
                expired.append(trade.id)
        return expired

    def open_production(self) -> None:
        """Close deliberation, let labour be committed.

        Only reachable from ``discovery``. Production remains a one-shot
        decision — what changes is that agents have had rounds to talk, quote
        and read a board before making it, rather than making it into silence.
        """
        if self.phase != "discovery":
            raise TradeError(f"cannot open production from {self.phase}")
        self.phase = "production"

    def open_trading(self) -> None:
        """Close production, open the floor. Agents that never produced get
        their autarky-optimal plan rather than nothing, so a silent agent shows
        up as *not trading* rather than as a starved one — two different
        failures that would otherwise be impossible to tell apart."""
        if self.phase != "production":
            raise TradeError(f"cannot open trading from {self.phase}")
        for state in self.agents.values():
            if not state.produced:
                plan = {g: state.alpha[i] for i, g in enumerate(self.goods)}
                self.op_produce(state.agent_id, plan)
                state.shares = tuple(state.alpha)
        self.phase = "trading"

    def close(self) -> None:
        """Close the floor and return every outstanding escrow."""
        for trade in self.trades.values():
            if trade.status == "pending":
                self._settle(trade, "expired", "the floor closed")
        self.phase = "closed"

    # --- accounting ---------------------------------------------------------

    def utilities(self) -> list[float]:
        """Final utility per agent, in island index order."""
        return [utility(s.alpha, s.holdings)
                for s in sorted(self.agents.values(), key=lambda s: s.index)]

    def check_conservation(self) -> None:
        """Holdings plus escrow equals everything produced. Raises if not.

        Called at every phase boundary and after every run. Trade moves goods;
        an arm that appeared to beat the Pareto frontier would fail here first,
        which is the point of asserting it rather than trusting the arithmetic.
        """
        produced = [0.0] * self.island.n_goods
        for state in self.agents.values():
            if state.shares is not None:
                for g in range(self.island.n_goods):
                    produced[g] += state.capacity[g] * state.shares[g]
        held = [0.0] * self.island.n_goods
        for state in self.agents.values():
            for g in range(self.island.n_goods):
                held[g] += state.holdings[g]
        for trade in self.trades.values():
            if trade.status == "pending":
                for good, qty in trade.give.items():
                    held[self._index(good)] += qty
        for g, good in enumerate(self.goods):
            if abs(produced[g] - held[g]) > 1e-6:
                raise AssertionError(
                    f"conservation broken for {good}: produced {produced[g]:.6f}, "
                    f"accounted {held[g]:.6f}"
                )

    def summary(self) -> dict[str, Any]:
        executed = sum(1 for t in self.trades.values() if t.status == "executed")
        return {
            "phase": self.phase, "tick": self.tick,
            "proposed": len(self.trades), "executed": executed,
            "cancelled": sum(1 for t in self.trades.values() if t.status == "cancelled"),
            "expired": sum(1 for t in self.trades.values() if t.status == "expired"),
            "rejected": sum(1 for t in self.trades.values() if t.status == "rejected"),
            "utilities": [round(u, 6) for u in self.utilities()],
        }

    # --- dispatch -----------------------------------------------------------

    #: The whole agent-facing surface. Anything not here is not a thing an agent
    #: can do to the state, and that list being short is deliberate.
    OPS = ("state", "produce", "propose", "approve", "cancel", "pending")

    def dispatch(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Apply one request. Never raises for agent error — returns ``ok: False``.

        An exception here would kill the serving loop and take the run with it;
        a bad request from one agent must cost that agent a turn and nothing
        more. Programming errors (a negative holding, broken conservation) are
        ``AssertionError`` and deliberately *do* escape.
        """
        op = str(request.get("op", ""))
        try:
            if op == "state":
                result = self.op_state(agent_id)
            elif op == "produce":
                result = self.op_produce(agent_id, request.get("plan"))
            elif op == "propose":
                result = self.op_propose(
                    agent_id, str(request.get("seller", "")), request.get("give"),
                    request.get("want"), str(request.get("note", "")),
                )
            elif op == "approve":
                result = self.op_approve(agent_id, str(request.get("trade_id", "")))
            elif op == "cancel":
                result = self.op_cancel(agent_id, str(request.get("trade_id", "")))
            elif op == "pending":
                result = self.op_pending(agent_id)
            else:
                raise TradeError(f"unknown op {op!r}; try one of {', '.join(self.OPS)}")
        except TradeError as exc:
            self.rejections.append({"tick": self.tick, "agent": agent_id, "op": op,
                                    "reason": str(exc)})
            return {"ok": False, "op": op, "error": str(exc)}
        return {"ok": True, "op": op, **result}


# --- the Switchboard side ---------------------------------------------------


class ManagerService:
    """Serves a :class:`Manager` over a hub, using only the published client.

    The loop is the boring part and that is the claim being made: request/reply
    over ``@manager``, state mirrored onto the blackboard, a lease so a second
    manager cannot start writing, and a ledger channel. No new hub concept was
    needed to run a market on it.
    """

    def __init__(self, client: Any, manager: Manager, *, run: str,
                 publish_ledger: bool = True, lease_ttl: float = 900.0) -> None:
        self.client = client
        self.manager = manager
        self.run = run
        self.publish_ledger = publish_ledger
        self.lease_ttl = lease_ttl
        self.served = 0

    @property
    def ledger_channel(self) -> str:
        return f"barter/{self.run}/ledger"

    def claim(self) -> None:
        """Take the state lease. Two managers writing one ledger is the failure
        this prevents, and the lease expiring is how a crashed manager lets the
        next one take over without anybody releasing anything."""
        self.client.acquire(f"barter/{self.run}/state", note="authoritative state",
                            ttl=self.lease_ttl)

    def publish(self) -> None:
        """Mirror state to the blackboard: one key per agent, one run header.

        Per-agent keys rather than one blob because they are read individually
        and a single blob would make every read a read of everyone's private
        holdings.
        """
        self.client.board_set(f"barter/{self.run}/run", {
            "goods": self.manager.goods, "phase": self.manager.phase,
            "tick": self.manager.tick, "agents": sorted(self.manager.agents),
            **self.manager.summary(),
        })
        for agent_id, state in self.manager.agents.items():
            self.client.board_set(f"barter/{self.run}/agent/{agent_id}", {
                "holdings": {g: round(state.holdings[i], 6)
                             for i, g in enumerate(self.manager.goods)},
                "produced": state.produced,
            })

    def drain(self, *, wait: float = 0.0, limit: int = 200) -> int:
        """Serve every request waiting in the inbox. Returns how many.

        Identity is the hub's ``from`` on the envelope, never anything in the
        body: an agent cannot claim to be another agent by writing a name into
        its own message, so the named-seller rule on approvals actually holds.
        """
        served = 0
        for message in self.client.inbox(wait=wait, limit=limit):
            body = message.get("body")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except ValueError:
                    body = {"op": body}
            if not isinstance(body, dict) or "op" not in body:
                continue
            sender = message.get("from") or ""
            reply = self.manager.dispatch(sender, body)
            if "req" in body:
                reply["req"] = body["req"]
            self.client.send(sender, reply, type="result")
            if self.publish_ledger and reply.get("ok") and body.get("op") == "approve":
                self.client.post(self.ledger_channel, self.manager.ledger[-1], type="note")
            served += 1
        self.served += served
        return served


class ManagerRPC:
    """The agent side of the same conversation. One call, one reply.

    Correlates on a ``req`` id rather than assuming the next message in the
    inbox is the answer. On a shared hub it very often is not — the manager may
    be answering several agents at once, and ``inbox`` advances a cursor, so a
    reply skipped is a reply lost. Anything read but not matched is kept in
    ``_spare`` and re-examined on the next call.

    ``pump`` is how a single-threaded caller lets the manager work. In a live
    deployment the manager is its own process and there is nothing to pump; in
    the experiment it is a ``ManagerService`` in the same thread, so the caller
    passes ``service.drain`` and the request is served between send and receive.
    """

    #: Polls before giving up. With a pump the answer arrives on the first one;
    #: without one this is how long an agent waits on a manager that may be busy.
    _MAX_POLLS = 40

    def __init__(self, client: Any, *, manager_id: str = "manager",
                 pump: Any = None) -> None:
        self.client = client
        self.manager_id = manager_id
        self.pump = pump
        self._seq = 0
        self._spare: list[dict[str, Any]] = []

    def call(self, op: str, *, wait: float = 0.0, **kwargs: Any) -> dict[str, Any]:
        self._seq += 1
        req = f"{self.client.agent_id}-{self._seq}"
        self.client.send(self.manager_id, {"op": op, "req": req, **kwargs})
        for _ in range(self._MAX_POLLS):
            if self.pump is not None:
                self.pump()
            waiting = self._spare + self.client.inbox(
                wait=wait, channels=[f"@{self.client.agent_id}"])
            self._spare = []
            for message in waiting:
                body = message.get("body")
                if isinstance(body, dict) and body.get("req") == req:
                    self._spare = [m for m in waiting if m is not message]
                    return body
            self._spare = waiting
        return {"ok": False, "op": op, "error": "manager did not reply"}
