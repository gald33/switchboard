"""Driving one island through production, price discovery and a trading floor.

Two transports, one set of results. ``direct`` calls the ``Manager`` state
machine in process; ``hub`` sends the identical requests as Switchboard
messages to a ``ManagerService`` and reads the replies back off the hub. The
experiment asserts the two agree exactly for the same seed
(``test_barter.py::test_hub_and_direct_transports_agree``), which is the claim
worth making about the transport: the hub carries the market without changing
it. Sweeps then run on ``direct``, because a thousand HTTP round-trips per
island buys nothing once that equality holds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .economy import Efficiency, Gains, Island, autarky, capture, efficiency, gains
from .manager import (
    LEVEL_OFFER,
    LEVEL_SETTLE,
    Manager,
    ManagerRPC,
    ManagerService,
)
from .traders import Floor, Trader, gives_way, propose_for

#: Rounds of price discovery before production closes. Arm C needs a few to
#: converge; A and B ignore them, and are charged for them anyway so the arms
#: are not separated by round count.
DISCOVERY_ROUNDS = 30

#: Default trading rounds. Each is one proposal per agent plus one pass of
#: approvals. This is a *tuning knob, not a constant*, and the arms respond to
#: it differently enough that quoting any single value would decide the result:
#: the price arm plateaus almost immediately and never improves, while the money
#: arm keeps climbing as its scarce numeraire circulates. Comparing them at one
#: budget picks a winner by picking the budget, so ``--rounds-sweep`` traces the
#: whole curve and the report shows where each arm stops moving.
TRADE_ROUNDS = 60


@dataclass(frozen=True)
class Outcome:
    arm: str
    seed: int
    utilities: tuple[float, ...]
    efficiency: Efficiency
    #: Efficiency measured against the frontier of the *production plan they
    #: chose*. High here with low overall efficiency means they swapped their
    #: goods well but made the wrong ones.
    exchange_efficiency: Efficiency
    capture_lo: float
    capture_hi: float
    #: Worst agent's final utility as a multiple of its autarky utility. Below
    #: 1.0 means somebody was made worse off by taking part, which voluntary
    #: trade alone cannot cause -- only a production bet on a price that did not
    #: materialise can.
    worst_ratio: float
    #: The whole distribution behind ``worst_ratio``. Efficiency is
    #: distribution-neutral by construction, so who the gains went to is a
    #: question it cannot answer and this one can.
    gains: Gains
    messages: int
    proposed: int
    executed: int
    rejected: int
    #: How many instalments the unit of labour was split into. 1 is the
    #: one-shot bet placed before any price exists; more spreads the *same*
    #: total labour across trading rounds, which moves neither the frontier nor
    #: either benchmark, so the two are directly comparable.
    instalments: int = 1
    #: Labour offered and never claimed, summed over agents. Non-zero only when
    #: an agent hands in a plan whose fractions sum to less than 1 — scripted
    #: policies never do, so this is a Tier 2 measure living in a shared record.
    idle: float = 0.0
    #: Offers that crossed: both agents proposing the same swap, both escrowed.
    crossings: int = 0

    def row(self) -> str:
        return (
            f"{self.arm:<11} {self.efficiency!s:>13} {self.exchange_efficiency!s:>13} "
            f"{self.capture_lo:>7.1%} {self.worst_ratio:>7.2f} "
            f"{(self.gains.below if self.gains else 0):>5} "
            f"{self.messages:>6} {self.executed:>5}/{self.proposed:<5} {self.rejected:>5}"
        )


HEADER = (
    f"{'ARM':<11} {'EFFICIENCY':>13} {'OF OWN PLAN':>13} {'CAPTURE':>7} "
    f"{'WORST':>7} {'BELOW':>5} {'MSGS':>6} {'TRADES':>11} {'REJ':>5}"
)


class _DirectPort:
    """Manager access with no transport at all."""

    def __init__(self, manager: Manager, agent_id: str) -> None:
        self.manager, self.agent_id = manager, agent_id

    def call(self, op: str, **kwargs: Any) -> dict[str, Any]:
        return self.manager.dispatch(self.agent_id, {"op": op, **kwargs})


def _hub_ports(hub: Any, manager: Manager, run: str) -> tuple[Any, dict[str, Any]]:
    """Same market, over real Switchboard messages."""
    service = ManagerService(hub.client("manager"), manager, run=run)
    service.claim()
    # `pump=service.drain` is what makes a single-threaded run work: the agent
    # sends, the manager serves its inbox, the agent collects -- all inside one
    # `call`. A manager in its own process would need no pump and the agent code
    # would be unchanged.
    ports: dict[str, Any] = {
        agent_id: ManagerRPC(hub.client(agent_id), pump=service.drain)
        for agent_id in manager.agents
    }
    return service, ports


def run_island(
    island: Island,
    arm: str,
    *,
    seed: int = 0,
    hub: Any = None,
    run: str = "barter",
    trade_rounds: int | None = None,
    instalments: int = 1,
) -> Outcome:
    """One island, one arm, start to finish.

    ``instalments`` is the labour-timing knob. At 1 the whole unit is committed
    once, before any trade has happened, and a wrong bet stands for the rest of
    the run. Above 1 the same unit is spent a slice at a time across the trading
    rounds, so an agent can see what the market actually gave it and produce
    against that. Nothing else changes: no extra messages, no extra prices, and
    the frontier, autarky floor and exchange ceiling are all untouched.
    """
    rounds = TRADE_ROUNDS if trade_rounds is None else trade_rounds
    rng = random.Random(seed * 1000 + ord(arm))
    instalments = max(1, instalments)
    manager = Manager(island=island,
                      labour_per_round=1.0 / instalments,
                      rolling=instalments > 1)
    goods = manager.goods

    floor = Floor(enabled=arm != "A")
    traders = {
        agent_id: Trader(agent_id, state.index, island, arm, random.Random(rng.random() * 1e9))
        for agent_id, state in manager.agents.items()
    }
    for trader in traders.values():
        trader.goods = goods

    # Both ports expose the same `call(op, **kwargs)`, so everything below this
    # point is written once and does not know which transport it is on. That is
    # what makes the two comparable: it is the same run, not two runs that
    # resemble each other.
    service = None
    if hub is None:
        ports: dict[str, Any] = {a: _DirectPort(manager, a) for a in manager.agents}
    else:
        service, ports = _hub_ports(hub, manager, run)

    def call(agent_id: str, op: str, **kwargs: Any) -> dict[str, Any]:
        return ports[agent_id].call(op, **kwargs)

    # --- talk, then produce -------------------------------------------------
    for round_no in range(DISCOVERY_ROUNDS):
        for trader in traders.values():
            trader.declare(round_no, floor)
        for trader in traders.values():
            trader.observe_prices(round_no, floor)
    for trader in traders.values():
        trader.adopt_own_price(floor)

    for agent_id, trader in traders.items():
        call(agent_id, "produce", plan=trader.production_plan(floor))
    manager.check_conservation()
    # Tier 1 has no wall clock: its agents are code and take no time, so
    # everything is simply open. The windows exist to give *models* time to
    # talk before committing, and scripted agents did their talking in process
    # before the manager ever heard from them.
    manager.open(LEVEL_OFFER)
    manager.open(LEVEL_SETTLE)
    manager.check_conservation()
    if instalments > 1:
        # The manager refuses two commitments in one tick, and the opening
        # instalment was committed at tick 0. Without this the first trading
        # round's instalment is rejected as "you have already worked this
        # round", silently costing every rolling agent one slice of labour.
        manager.advance()

    # --- the floor ----------------------------------------------------------
    order = list(traders)
    for _ in range(rounds):
        rng.shuffle(order)
        holdings = {a: list(manager.agents[a].holdings) for a in traders}

        # A rolling island works a little more each round, against what it now
        # holds rather than against what it hoped for -- and it does so in a
        # real production stage, opened and closed by the manager, exactly as
        # Tier 2's rounds do. Tier 1 has no talking stages, because its price
        # discovery happens in process through a floor the manager never sees,
        # but the commit/trade cycle is now the same one in both tiers.
        # Skipped entirely at one instalment, so the one-shot path is
        # byte-for-byte the run it always was and old results still reproduce.
        if instalments > 1:
            for agent_id in order:
                if manager.agents[agent_id].spent >= 1.0 - 1e-9:
                    continue
                call(agent_id, "produce",
                     plan=traders[agent_id].production_instalment(holdings[agent_id], floor))
                holdings[agent_id] = list(manager.agents[agent_id].holdings)
            manager.check_conservation()

        for agent_id in order:
            trader = traders[agent_id]
            offer = propose_for(trader, holdings[agent_id], list(traders.values()), holdings, rng)
            if offer is None:
                continue
            seller, give, want = offer
            reply = call(agent_id, "propose", seller=seller, give=give, want=want)
            if reply.get("ok"):
                holdings[agent_id] = list(manager.agents[agent_id].holdings)

        # The resolve stage. Offers are on the table and some of them cross --
        # two agents holding mirror-image trades, each escrowed, one of which
        # has to give way. Scripted agents apply a shared deterministic rule, so
        # both sides reach the same answer without saying anything and exactly
        # one of the pair is withdrawn. That is what makes them the benchmark a
        # model arm is measured against, not a claim that the rule is clever.
        for agent_id in order:
            for pair in call(agent_id, "pending").get("crossed_pairs", []):
                doomed = gives_way(pair)
                # Only the buyer can withdraw its own offer, so naming the same
                # doomed id on both sides still produces exactly one action.
                if manager.trades[doomed].buyer == agent_id:
                    call(agent_id, "cancel", trade_id=doomed)
        manager.check_conservation()

        # Approvals. A seller accepts only what raises its own utility, so every
        # settled trade is voluntary on both sides.
        for agent_id in order:
            trader = traders[agent_id]
            pending = call(agent_id, "pending")
            for trade in pending.get("awaiting_your_approval", []):
                # The seller receives what the buyer offered and hands over what
                # the buyer asked for. Approval is always the seller's own call,
                # by its own rule, so every settled trade is voluntary on both
                # sides and nobody is scripted into a loss.
                current = list(manager.agents[agent_id].holdings)
                if trader.accepts(current, trade["give"], trade["want"]):
                    call(agent_id, "approve", trade_id=trade["id"])
        # A round ends: stale offers expire and the level drops back, which for
        # a rolling island is what makes the next instalment a new round's.
        manager.next_round()
        manager.open(LEVEL_OFFER)
        manager.open(LEVEL_SETTLE)
        manager.check_conservation()

    manager.close()
    manager.check_conservation()
    if service is not None:
        service.publish()

    return score(island, manager, arm=arm, seed=seed, messages=floor.sent,
                 instalments=instalments)


def score(island: Island, manager: Manager, *, arm: str, seed: int,
          messages: int = 0, instalments: int = 1) -> Outcome:
    """Turn a finished manager into an Outcome.

    Shared by both tiers rather than written twice. A Tier 2 island costs real
    money to produce, so the one thing that must not happen is a run completing
    and then falling over on the way to a number — which is exactly what
    happened when this logic was duplicated, and is why it now has a gate that
    needs no model to exercise (``test_barter_llm.py``).
    """
    utils = manager.utilities()
    _, autarky_utils = autarky(island)
    realised = efficiency(island, utils)
    # The frontier of the production plan they actually chose. High here beside
    # a low overall score means they swapped well and made the wrong things.
    plan = [list(manager.agents[a].shares) if sum(manager.agents[a].shares) > 1e-9
            else list(island.alpha[manager.agents[a].index])
            for a in sorted(manager.agents, key=lambda a: manager.agents[a].index)]
    lo, hi = capture(realised, efficiency(island, autarky_utils))
    summary = manager.summary()
    return Outcome(
        arm=arm, seed=seed, utilities=tuple(utils), efficiency=realised,
        exchange_efficiency=efficiency(island, utils, fixed_shares=plan),
        capture_lo=lo, capture_hi=hi,
        worst_ratio=min(utils[i] / autarky_utils[i] for i in range(island.n_agents)),
        gains=gains(island, utils),
        messages=messages, proposed=summary["proposed"],
        executed=summary["executed"],
        rejected=summary["rejected"] + summary["expired"],
        instalments=instalments,
        idle=sum(summary["idle_labour"].values()),
        crossings=summary["crossings"],
    )
