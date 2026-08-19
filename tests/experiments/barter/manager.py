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
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from .economy import GOOD_NAMES, Island, mrs, utility

#: What is open, as a level that only rises within a round and resets with it.
#:
#: The five-phase chain this replaces -- discovery, production, deal, trading,
#: resolve -- was a sequence of *exclusive* states, so opening one closed the
#: last, and every capability had to be granted and withdrawn in the right
#: order. Three of those five existed only to deny actions during a
#: conversation, which is a thing a wall clock does better.
#:
#: Permissions now open and never close inside a round:
#:
#:     1  talk, and commit this round's labour
#:     2  ...and propose, and withdraw your own proposals
#:     3  ...and approve
#:
#: Each waits on the one before it having had time: you cannot offer what
#: nobody has made, and you cannot settle before offers exist. Talking is at
#: every level because talking never reaches the manager at all -- it goes
#: straight to the hub channel -- so the manager was never gating it, and the
#: stages that pretended to were only denying everything else.
LEVEL_PRODUCE = 1
LEVEL_OFFER = 2
LEVEL_SETTLE = 3


@dataclass(frozen=True)
class Agenda:
    """The schedule, posted before anything opens, in absolute times.

    The island used to simply begin, and every agent discovered what was open
    by trying. That reads as a coordination failure and is not one: an agent
    whose first turn started forty seconds into a sixty-second window had no
    way to know it, because nothing had told it when anything was happening.
    Across seven islands the settle window drew between one and five turns in
    total -- fewer, several times, than there were agents -- and the arms that
    settled nothing were the arms whose agents never got a turn inside it.

    So the manager publishes the whole timetable first, in wall-clock seconds
    from the start of the run, and nothing opens until every agent has said it
    has read it. A trader that knows approving opens at t=180 and the round
    ends at t=240 can keep its turn short at t=170 and spend it at t=185. One
    that has to infer the schedule from refusals cannot.

    ``version`` rises each time the schedule is re-posted. An ack names the
    version it is acking, so an agent cannot acknowledge a timetable that has
    since been replaced -- which is the whole content of "everyone starts
    together".
    """

    version: int
    #: Run-clock seconds. Everything below is on the same scale, so an agent
    #: only ever needs one number -- "what time is it" -- to place itself.
    posted_at: float
    #: Acknowledge by this time or the schedule is re-posted.
    acks_by: float
    #: When round 1, window 1 opens. Deliberately later than ``acks_by``: the
    #: gap is what makes the start a time everybody can be ready *for* rather
    #: than a time somebody is still agreeing to.
    starts_at: float
    #: One duration per window, in seconds. Three of them, and deliberately not
    #: all the same: a production turn was measured at 18-33 seconds and a
    #: trading turn at 68-169, so equal windows meant every trading turn
    #: outlived the window it began in. A window has to be wide enough for one
    #: whole turn or it is not a window, it is an interruption.
    windows: tuple[float, ...]
    rounds: int

    @property
    def round_seconds(self) -> float:
        return sum(self.windows)

    def rows(self) -> list[dict[str, Any]]:
        """Every window of the run, with the time it opens and what it opens.

        One window means nothing is staged: everything is open for the whole
        round. The staging was there so traders could deliberate before
        committing, and the measurements said they do not deliberate separately
        -- the offers *are* the negotiation, and a ladder that withholds
        offering withholds the negotiating.
        """
        if len(self.windows) == 1:
            opens = ["talk, `produce`, `propose_trade`, `approve_trade`, "
                     "`cancel_trade` — everything, for the whole round"]
        else:
            opens = ["talk, and `produce`",
                     "...and `propose_trade`, and withdraw your own offers",
                     "...and `approve_trade`"]
        out = []
        for round_no in range(1, self.rounds + 1):
            at = self.starts_at + (round_no - 1) * self.round_seconds
            for slot, what in enumerate(opens):
                out.append({"round": round_no, "window": slot + 1,
                            "opens_at": round(at, 1),
                            "closes_at": round(at + self.windows[slot], 1),
                            "seconds": self.windows[slot], "you_may": what})
                at += self.windows[slot]
        return out

    def public(self) -> dict[str, Any]:
        return {"version": self.version, "posted_at": round(self.posted_at, 1),
                "acknowledge_by": round(self.acks_by, 1),
                "starts_at": round(self.starts_at, 1),
                "window_seconds": list(self.windows),
                "ends_at": round(self.starts_at + self.rounds * self.round_seconds, 1),
                "rounds": self.rounds, "schedule": self.rows()}


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
    #: Cumulative labour shares, one entry per good, summing to at most 1 across
    #: the whole run. A list rather than a one-shot tuple because labour can be
    #: committed in instalments — see ``Manager.labour_per_round``.
    shares: list[float] = field(default_factory=list)
    #: Labour committed so far, 0..1.
    spent: float = 0.0
    #: Labour offered to this agent and never claimed, 0..1. An instalment is a
    #: split of *this round's* share, so a plan whose fractions sum to less than
    #: 1 leaves the remainder unworked -- and it is not carried, so it is gone.
    #: Tracked because a run once finished having spent 0.67 of its labour and
    #: nothing in the record could say whether agents had declined to work or
    #: had simply handed in vectors that summed short. Those are opposite
    #: findings and they looked identical.
    idle: float = 0.0
    #: The tick of the most recent commitment, so labour cannot be spent twice
    #: in one round.
    last_spent_tick: int | None = None

    @property
    def produced(self) -> bool:
        return self.spent > 1e-12


@dataclass
class Manager:
    """Authoritative state for one island. Pure — no hub, no clock, no I/O.

    Split from the serving loop on purpose: every invariant worth asserting is
    assertable here, in-process and instantly, and the transport cannot be the
    reason a run balances.
    """

    island: Island
    #: What is open right now, as one of ``LEVEL_PRODUCE``/``LEVEL_OFFER``/
    #: ``LEVEL_SETTLE``. The manager owns it because a level agents could raise
    #: is a level one agent can raise early.
    #:
    #: It only ever rises within a round and resets with it, so a capability
    #: cannot be withdrawn while the round is running and the deadline an agent
    #: is planning against is the end of the round rather than the end of its
    #: turn. The five exclusive stages this replaces had to grant and withdraw
    #: each capability in the right order, and three of the five existed only to
    #: deny actions during a conversation -- a job the wall clock in ``flow``
    #: does better and with fewer moving parts.
    #:
    #: ``LEVEL_PRODUCE`` is where a round starts, and it is not a silent stage:
    #: agents talk throughout, because talking never reaches the manager. That
    #: matters because of a flaw the Tier 2 runs exposed in themselves.
    #: Production used to be committed before any agent had said anything, so
    #: every convention on the ladder could only describe the world after the
    #: one irreversible decision was behind it -- implied production quality came
    #: out at ~0.41 in every arm, the exchange ceiling, because no arm had the
    #: information to specialise whatever it had been told. Deliberation has to
    #: be able to happen before manufacturing or it cannot plan for it.
    level: int = LEVEL_PRODUCE
    #: Set once, at the end. A closed island accepts nothing at any level.
    finished: bool = False
    #: How much of an agent's single unit of labour one ``produce`` call may
    #: commit. 1.0 is the original one-shot bet. Smaller spreads the *same* total
    #: labour over instalments, which leaves the frontier, the autarky floor and
    #: the exchange ceiling all exactly where they were — so a rolling run and a
    #: one-shot run are directly comparable and differ only in whether a
    #: commitment can be revised.
    labour_per_round: float = 1.0
    #: Whether labour may still be committed once trading has opened. Off, the
    #: production decision is a bet placed before any price exists and a wrong
    #: one cannot be unwound — which is what made specialisation dangerous and
    #: ruin total. On, an agent produces a little, watches, and produces again.
    rolling: bool = False
    tick: int = 0
    #: The schedule agents were given, or ``None`` before one is posted.
    agenda: Agenda | None = None
    #: Who has acknowledged the *current* agenda. Cleared whenever a new one is
    #: posted, because an ack of a replaced timetable is not an ack of this one.
    acked: set[str] = field(default_factory=set)
    #: One row per agenda ever posted: how many acked it and who. The record of
    #: how long an island took to agree on when to start, which is a
    #: coordination result in its own right and the only thing that separates
    #: "nobody was ready" from "nobody was asked".
    musters: list[dict[str, Any]] = field(default_factory=list)
    agents: dict[str, AgentState] = field(default_factory=dict)
    trades: dict[str, Trade] = field(default_factory=dict)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 1
    #: Rejected requests, kept because "how often did agents propose something
    #: impossible" is a measure of how well a convention is working.
    rejections: list[dict[str, Any]] = field(default_factory=list)
    #: Pairs of trade ids that were open in opposite directions between the same
    #: two agents at the moment the second was proposed.
    #:
    #: Nothing stops an agent making several offers in a trading stage, and each
    #: escrows the moment it is made -- so two agents who have agreed a swap and
    #: both propose it end up with two live trades, twice the goods locked up,
    #: and a decision neither of them planned for: approve one and cancel the
    #: other, or approve both and swap twice. It is a coordination problem the
    #: escrow creates rather than solves, and counting it is the only way to say
    #: whether a convention helps agents avoid it.
    crossings: list[dict[str, Any]] = field(default_factory=list)
    #: Serialises agent requests. Agents within a stage act concurrently -- six
    #: stages of waiting per round is most of an island's wall clock, and they
    #: are waiting on each other for no reason -- so two of them can be inside
    #: `dispatch` at once, on different threads, both moving goods. Every
    #: mutation goes through `dispatch`, so one lock there covers all of them.
    #: Phase transitions are not locked: the flow makes them between stages,
    #: when no agent is mid-request.
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.agents:
            for i, agent_id in enumerate(self.island.agent_ids()):
                self.agents[agent_id] = AgentState(
                    agent_id=agent_id, index=i,
                    alpha=self.island.alpha[i], capacity=self.island.capacity[i],
                    holdings=[0.0] * self.island.n_goods,
                    shares=[0.0] * self.island.n_goods,
                )

    # --- helpers ------------------------------------------------------------

    @property
    def phase(self) -> str:
        """The level, named. Records and analysis read this."""
        if self.finished:
            return "closed"
        return {LEVEL_PRODUCE: "produce", LEVEL_OFFER: "offer"}.get(self.level, "settle")

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
            "labour_left": round(max(0.0, 1.0 - state.spent), 4),
            "idle_labour": round(state.idle, 4),
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
        """Commit labour. ``plan`` is ``{good: share}``, shares >= 0 summing to
        at most 1 -- a split of *this instalment*, not of the whole run.

        With ``labour_per_round == 1.0`` (the default) there is one instalment
        and this is the original one-shot decision: everything is staked before
        any price exists, and a wrong bet cannot be unwound.

        With a smaller instalment and ``rolling`` on, the same total unit of
        labour is spread across rounds, so an agent produces a little, sees what
        prices do, and produces again. Total production possibilities are
        identical either way -- the frontier, the autarky floor and the exchange
        ceiling are all unchanged -- so the two are directly comparable and the
        only thing varying is whether commitment is one-shot or incremental.

        Idle labour is lost, not carried. An instalment an agent does not spend
        is a round it chose not to work, which is a choice it is allowed to make
        badly.
        """
        # No exception for rolling any more. Every round has its own production
        # stage, so labour never needs to be committed during trading -- and an
        # allowance that let it would make the stage deadline advisory rather
        # than real.
        if self.finished:
            raise TradeError("the island is closed")
        state = self._agent(agent_id)
        if state.last_spent_tick == self.tick:
            raise TradeError("you have already worked this round")
        remaining = 1.0 - state.spent
        if remaining <= 1e-9:
            raise TradeError("you have no labour left; it is all committed")
        instalment = min(self.labour_per_round, remaining)

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
            raise TradeError(
                f"shares sum to {total:.4f}; they are fractions of this round's "
                f"labour and must sum to at most 1")

        # Scale the split by the instalment, so `shares` stays a fraction of the
        # agent's whole labour endowment however many instalments there were.
        # Conservation and the own-plan frontier then need no special case.
        for g in range(self.island.n_goods):
            shares[g] *= instalment
            state.shares[g] += shares[g]
            state.holdings[g] += state.capacity[g] * shares[g]
        state.spent += instalment * total
        state.idle += instalment * max(0.0, 1.0 - total)
        state.last_spent_tick = self.tick
        return {
            "produced": {g: round(state.capacity[i] * shares[i], 4)
                         for i, g in enumerate(self.goods)},
            "labour_left": round(max(0.0, 1.0 - state.spent), 4),
            "idle_labour": round(instalment * max(0.0, 1.0 - total), 4),
            "idle_labour_total": round(state.idle, 4),
            "holdings": {g: round(state.holdings[i], 4) for i, g in enumerate(self.goods)},
        }

    def op_propose(self, buyer: str, seller: str, give: Any, want: Any,
                   note: str = "") -> dict[str, Any]:
        """Buyer offers ``give`` for ``want``. Returns a trade id.

        The buyer's side is escrowed here, not at settlement. That is what makes
        the id worth anything to the seller: by the time it can approve, the
        goods are already out of the buyer's hands.
        """
        if self.finished or self.level < LEVEL_OFFER:
            raise TradeError(
                "proposing is not open yet; it opens once everyone has had time "
                "to say what they are making")
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

        mirror = next(
            (t for t in self.trades.values()
             if t.status == "pending" and t.buyer == seller and t.seller == buyer),
            None)
        trade = Trade(
            id=f"t{self._next_id}", buyer=buyer, seller=seller,
            give=give_bundle, want=want_bundle, note=str(note)[:200],
            opened_tick=self.tick,
        )
        self._next_id += 1
        self.trades[trade.id] = trade
        self._move(buyer_state, give_bundle, -1)  # into escrow
        reply = {
            "trade_id": trade.id,
            "escrowed": {g: round(q, 4) for g, q in give_bundle.items()},
            "expires_tick": trade.opened_tick + TRADE_TTL_TICKS,
            "note": "the seller must approve this id before anything settles",
        }
        if mirror is not None:
            # Recorded, and told to the buyer -- who can see both ends of it
            # anyway, since one of the two is its own offer. The manager states
            # the fact and does nothing about it: it will not match the pair,
            # cancel either side, or refuse the second. Deciding is the agents'
            # problem and watching them decide is the point.
            self.crossings.append({"tick": self.tick, "pair": [mirror.id, trade.id],
                                   "between": sorted([buyer, seller])})
            reply["crosses"] = mirror.id
            reply["crosses_note"] = (
                f"{seller} already has an offer open to you ({mirror.id}), so both "
                "sides are now escrowed. Approving both swaps twice.")
        return reply

    def op_approve(self, seller: str, trade_id: str) -> dict[str, Any]:
        """Seller settles a trade by id. The only way goods change hands."""
        if self.finished or self.level < LEVEL_SETTLE:
            raise TradeError(
                "approving is not open yet; it opens once offers have had time "
                "to accumulate")
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
        # Opens with proposing, not with approving: withdrawing is the undo of
        # an offer, so an agent that offers at level 2 must be able to take it
        # back before anything can settle at level 3.
        if self.finished or self.level < LEVEL_OFFER:
            raise TradeError("withdrawing is not open yet")
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
        # Crossings the agent is party to, so "we both proposed the same swap"
        # is visible in the one place an agent looks before acting. Behind a
        # switch on the model-facing side: whether traders notice a collision
        # unaided is exactly the sort of thing the ladder exists to measure.
        crossed = [c["pair"] for c in self.crossings
                   if agent_id in c["between"]
                   and all(self.trades[t].status == "pending" for t in c["pair"])]
        return {"awaiting_your_approval": awaiting, "your_open_offers": mine,
                "crossed_pairs": crossed}

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

    def open(self, level: int) -> None:
        """Raise what is open. Never lowers inside a round.

        The flow calls this on a wall clock rather than after everyone has had
        a turn, which is what makes the deadline real: an agent still thinking
        when the level rises does not hold the round open, and an agent that
        misses a window has missed it.
        """
        if self.finished:
            raise TradeError("the island is closed")
        self.level = max(self.level, int(level))

    def next_round(self) -> list[str]:
        """End the round: expire stale offers, and drop back to level 1.

        Resetting is what makes a round a round. The level rises through it and
        starts again, so every round re-earns the right to trade by spending a
        window on talking first.
        """
        expired = self.advance()
        if not self.finished:
            self.level = LEVEL_PRODUCE
        return expired

    def post_agenda(self, agenda: Agenda) -> Agenda:
        """Publish a schedule and start collecting acknowledgements afresh.

        Re-posting clears the acks rather than keeping them. An agent that
        acked v1 has agreed to times that no longer exist, and carrying that
        forward would let an island start with agents holding two different
        timetables -- which is the exact failure the muster exists to prevent.
        """
        with self._lock:
            if self.agenda is not None:
                self.musters.append({"version": self.agenda.version,
                                     "acked": sorted(self.acked),
                                     "of": len(self.agents)})
            self.agenda = agenda
            self.acked = set()
        return agenda

    def all_acked(self) -> bool:
        with self._lock:
            return bool(self.agents) and set(self.agents) <= self.acked

    def op_ack(self, agent_id: str, version: Any) -> dict[str, Any]:
        """Acknowledge the current schedule.

        The version is checked rather than trusted. An agent that read v1,
        thought for a while and acked after v2 was posted has agreed to the
        wrong times, and accepting it would start the island with agents
        working from different clocks.
        """
        self._agent(agent_id)
        if self.agenda is None:
            raise TradeError("there is no agenda to acknowledge yet")
        try:
            said = int(version)
        except (TypeError, ValueError):
            raise TradeError("version must be the number on the agenda") from None
        if said != self.agenda.version:
            raise TradeError(
                f"that is agenda v{said}; the current one is "
                f"v{self.agenda.version} -- read it again and acknowledge that")
        self.acked.add(agent_id)
        return {"acknowledged": self.agenda.version,
                "waiting_on": sorted(set(self.agents) - self.acked)}

    def op_batch(self, agent_id: str, ops: Any) -> dict[str, Any]:
        """Apply a list of requests as one, in order, under one lock.

        This is what makes a turn cheap. An agent that wants to offer three
        swaps was making three separate calls, each a write to the board, a
        wait for the next sweep and a read back -- and between them the model
        was thinking again. Measured, a trading turn took 68 to 169 seconds
        against a sixty-second window, and most of that was the round trips
        rather than the reasoning.

        One call, one order, one sweep. The parts are applied in the order
        given and each gets its own reply, so a batch where the second offer
        cannot be covered still lands the first and the third -- a partial
        result rather than an all-or-nothing that would make batching riskier
        than not batching.

        Atomic against *other agents*, because the whole batch runs inside the
        manager's lock: nobody else can take the goods out from under part of
        it.
        """
        if not isinstance(ops, list) or not ops:
            raise TradeError('ops must be a non-empty list of requests')
        if len(ops) > self.MAX_BATCH:
            raise TradeError(f"at most {self.MAX_BATCH} requests in one call")
        results = []
        for item in ops:
            if not isinstance(item, dict):
                results.append({"ok": False, "error": "each request must be an object"})
                continue
            inner = str(item.get("op", ""))
            if inner == "batch":
                results.append({"ok": False, "error": "a batch cannot contain a batch"})
                continue
            results.append(self._dispatch(agent_id, item, inner))
        return {"results": results,
                "applied": sum(1 for r in results if r.get("ok"))}

    def close(self) -> None:
        """Close the floor and return every outstanding escrow.

        An agent that never worked at all is given its autarky-optimal plan
        first, so that "never engaged" shows up as not having specialised
        rather than as starvation. Those are very different failures and would
        otherwise be indistinguishable -- an agent holding nothing scores zero
        either way. It fires only for an agent still at zero after every window
        of every round, so skipping one round stays the choice it was.
        """
        for state in self.agents.values():
            if state.spent <= 1e-12:
                state.last_spent_tick = None
                self.op_produce(state.agent_id,
                                {g: state.alpha[i] for i, g in enumerate(self.goods)})
        for trade in self.trades.values():
            if trade.status == "pending":
                self._settle(trade, "expired", "the floor closed")
        self.finished = True

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
            "idle_labour": {a: round(s.idle, 4) for a, s in sorted(self.agents.items())},
            "crossings": len(self.crossings),
            # How each collision ended, which is the interesting half. "both"
            # means the pair swapped twice -- nobody backed out -- and is the
            # outcome a convention ought to prevent.
            "crossings_resolved": self._crossing_outcomes(),
        }

    def _crossing_outcomes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for cross in self.crossings:
            done = [self.trades[t].status for t in cross["pair"]]
            executed = sum(1 for s in done if s == "executed")
            key = ("both" if executed == 2 else "one" if executed == 1 else "neither")
            out[key] = out.get(key, 0) + 1
        return out

    # --- dispatch -----------------------------------------------------------

    #: The whole agent-facing surface. Anything not here is not a thing an agent
    #: can do to the state, and that list being short is deliberate.
    OPS = ("state", "produce", "propose", "approve", "cancel", "pending", "ack",
           "batch")
    #: Most requests in one batch. A bound rather than a policy: an agent that
    #: sent ten thousand offers in one call would hold the lock for the rest of
    #: the island.
    MAX_BATCH = 24

    def dispatch(self, agent_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Apply one request. Never raises for agent error — returns ``ok: False``.

        An exception here would kill the serving loop and take the run with it;
        a bad request from one agent must cost that agent a turn and nothing
        more. Programming errors (a negative holding, broken conservation) are
        ``AssertionError`` and deliberately *do* escape.
        """
        op = str(request.get("op", ""))
        with self._lock:
            return self._dispatch(agent_id, request, op)

    def _dispatch(self, agent_id: str, request: dict[str, Any], op: str) -> dict[str, Any]:
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
            elif op == "ack":
                result = self.op_ack(agent_id, request.get("version"))
            elif op == "batch":
                result = self.op_batch(agent_id, request.get("ops"))
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
        # Concurrent agents each pump this between sending and collecting, so
        # two drains can run at once and race on the inbox cursor -- a reply
        # read by the wrong drain is a reply lost, and the agent waiting for it
        # simply times out.
        self._drain_lock = threading.RLock()

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
        with self._drain_lock:
            return self._drain(wait=wait, limit=limit)

    def _drain(self, *, wait: float = 0.0, limit: int = 200) -> int:
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


class BoardService:
    """A manager that reads orders off the blackboard instead of being called.

    Nothing sends the manager a request. Agents write what they want to their
    own keyspace and read the outcome back from theirs::

        barter/{run}/order/{agent}/{n}    agent writes   {"op": "propose", ...}
        barter/{run}/result/{agent}/{n}   manager writes {"ok": false, "error": ...}

    ``sweep`` reads every order, applies it, records the outcome and **deletes
    the order** -- deletion being what makes applying one twice impossible,
    which a cursor would not, since a crashed manager forgets its cursor and a
    board remembers what is still on it.

    Two things this buys that request/reply did not.

    It removes the pump. ``ManagerRPC`` drives ``ManagerService.drain`` inline
    from inside the caller's own request, which works only because everything
    was single-threaded and one agent at a time. With a hard clock and every
    agent live at once, nobody can afford to block on the manager, and here
    nobody does: writing an order returns immediately.

    And it makes ordering explicit. Concurrent agents write concurrently, so
    two can want the same goods; the sweep applies them in the order the board
    says they were written, and whoever was second is refused. That is a real
    race with a real tiebreak rather than an artefact of who happened to be
    scheduled first, and ``updated_by`` gives the writer's identity from the
    hub rather than from anything inside the message -- so an agent still
    cannot claim to be another agent by saying so.
    """

    def __init__(self, client: Any, manager: Manager, *, run: str,
                 every: float = 1.0) -> None:
        self.client = client
        self.manager = manager
        self.run = run
        #: How often the sweep runs, in seconds. A market parameter and not an
        #: implementation detail: it is the granularity at which orders are
        #: ordered against each other, so two orders inside one interval are
        #: settled by write time and two in different intervals are not
        #: comparable at all. Recorded with the run for that reason.
        self.every = every
        self.applied = 0
        #: Anything the sweep itself threw. A sweeper that died would hang every
        #: agent on the island silently, so it never dies -- it records and
        #: carries on, and the record is here rather than in a log nobody reads.
        self.errors: list[str] = []
        self.orders = f"barter/{run}/order/"
        self.results = f"barter/{run}/result/"
        #: The manager's logical clock, readable by anyone. Published on every
        #: sweep because an agent that needs to know whether a quote has gone
        #: stale should not have to spend a round trip through the order queue
        #: to find out what round it is.
        self.clock = f"barter/{run}/clock"
        #: Where the schedule is posted. Its own key rather than part of the
        #: clock, because it changes once per muster and the clock changes
        #: every sweep.
        self.agenda_key = f"barter/{run}/agenda"
        #: ...and the channel it is *announced* on. Both, deliberately, and for
        #: the two different jobs the primitives do: the board key is the
        #: current schedule, which an agent can look up at any time and will
        #: always get the live one; the channel is the announcement, which
        #: arrives and can be missed. A schedule that existed only as an
        #: announcement would be unreadable to an agent that joined late, and
        #: one that existed only as a key would never tell anybody it changed.
        self.agenda_channel = f"barter/{run}/agenda"
        #: Versions already announced, so a re-sweep does not re-announce.
        self._announced: set[int] = set()
        #: Run-clock seconds, injected because this module must not read a wall
        #: clock -- the manager owns a logical clock and the flow owns the real
        #: one, and an agent needs both on the same key to place itself.
        self.now: Any = None

    # No lease of its own. The state lease belongs to whoever mirrors state --
    # `ManagerService.claim` -- and two holders of one lease is the failure it
    # exists to prevent, so a sweeper that also claimed it would be racing the
    # thing protecting it.

    def sweep(self, *, limit: int = 500) -> int:
        """Apply everything on the board. Returns how many orders were applied."""
        entries = [e for e in self.client.board_list(prefix=self.orders)
                   if isinstance(e.get("value"), dict)]
        # Write order, from the board's own timestamps, with the key breaking
        # ties so a sweep is deterministic given the same board.
        entries.sort(key=lambda e: (str(e.get("updated_at") or ""), str(e["key"])))
        done = 0
        for entry in entries[:limit]:
            sender = str(entry.get("updated_by") or "")
            reply = self.manager.dispatch(sender, entry["value"])
            suffix = str(entry["key"])[len(self.orders):]
            self.client.board_set(f"{self.results}{suffix}", reply, ttl=3600.0)
            self.client.board_delete(str(entry["key"]))
            done += 1
        self.applied += done
        # Published *after* the orders are applied, not before: an agent that
        # acknowledges the schedule and then reads the clock to see who is left
        # must not be shown the state from before its own ack landed.
        clock: dict[str, Any] = {"tick": self.manager.tick,
                                 "level": self.manager.level,
                                 "phase": self.manager.phase}
        if self.now is not None:
            # "What time is it" -- the one number an agent needs to place
            # itself on a schedule of absolute times.
            clock["now"] = round(float(self.now()), 1)
        agenda = self.manager.agenda
        if agenda is not None:
            clock["agenda_version"] = agenda.version
            clock["acked"] = sorted(self.manager.acked)
            clock["present"] = self.roster()
            self.client.board_set(self.agenda_key, agenda.public(), ttl=3600.0)
            if agenda.version not in self._announced:
                self._announced.add(agenda.version)
                self.client.post(self.agenda_channel, agenda.public(), type="note")
        self.client.board_set(self.clock, clock, ttl=3600.0)
        return done

    def roster(self) -> list[str]:
        """Who the hub says is on the island, from presence.

        The muster is a presence question before it is anything else, and the
        hub already answers it: an agent that never registered cannot
        acknowledge a schedule, and waiting on it is waiting on nobody. Keeping
        this separate from the acks is what lets "never turned up" and "turned
        up and stayed quiet" be different findings rather than one number.
        """
        try:
            return sorted(str(a.get("agent_id") or a.get("name") or "")
                          for a in self.client.agents()
                          if str(a.get("agent_id") or "") in self.manager.agents)
        except Exception as exc:  # noqa: BLE001 -- presence is diagnostic, not load-bearing
            self.errors.append(f"roster: {type(exc).__name__}: {exc}")
            return []

    @contextmanager
    def sweeping(self) -> Any:
        """Sweep on a thread of its own for the duration of the block.

        The sweeper has to be something other than the agents, or it is the
        pump again: an agent that drives the manager between writing its order
        and reading the answer is blocking on the manager, which is exactly what
        the board removed. So it runs on its own thread, on its own interval,
        whatever the agents are doing -- and an agent that writes an order and
        then stops thinking still gets it applied.

        ``Manager`` takes its own lock, so a sweep landing while an agent is
        mid-``dispatch`` is serialised rather than interleaved.
        """
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(self.every):
                try:
                    self.sweep()
                except Exception as exc:  # noqa: BLE001 -- see `errors`
                    self.errors.append(f"{type(exc).__name__}: {exc}")
            # One last pass, so an order written just before the block ended is
            # answered rather than left on the board with an agent waiting.
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{type(exc).__name__}: {exc}")

        thread = threading.Thread(target=loop, name=f"sweep-{self.run}", daemon=True)
        thread.start()
        try:
            yield self
        finally:
            stop.set()
            thread.join(timeout=30.0)


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
