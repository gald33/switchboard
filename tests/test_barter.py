"""Gates for the barter experiment.

The experiment itself (``tests/experiments/barter_experiment.py``) is not
collected — it is a seeded study, not a gate. What is collected is everything
that would make its numbers meaningless if it broke: the manager's invariants,
the scorer's agreement with economic theory, and the claim that running the
market over a real hub gives the same answer as running it in process.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))

from barter.economy import (  # noqa: E402
    autarky,
    draw_island,
    efficiency,
    exchange_ceiling,
    planner,
    utility,
    walras,
)
from barter.manager import Manager, ManagerRPC, ManagerService, TradeError  # noqa: E402
from barter.run import run_island  # noqa: E402

from switchboard.testing import hub  # noqa: E402


@pytest.fixture
def island():
    return draw_island(4, 5, seed=7)


@pytest.fixture
def manager(island):
    return Manager(island=island)


def _produce_all(manager: Manager) -> None:
    for agent_id, state in manager.agents.items():
        manager.op_produce(agent_id, {g: state.alpha[i] for i, g in enumerate(manager.goods)})
    manager.open_trading()


# --- the manager's invariants ----------------------------------------------


def test_quantities_never_go_negative(manager):
    _produce_all(manager)
    with pytest.raises(TradeError, match="negative"):
        manager.op_propose("a1", "a2", {"fish": -1.0}, {"grain": 1.0})


def test_you_cannot_offer_what_you_do_not_have(manager):
    _produce_all(manager)
    with pytest.raises(TradeError, match="cannot cover"):
        manager.op_propose("a1", "a2", {"fish": 1e9}, {"grain": 1.0})


def test_a_proposal_escrows_the_buyers_side_immediately(manager):
    """The whole point of returning an id: by the time the seller can act, the
    buyer's goods are already committed and cannot be promised elsewhere."""
    _produce_all(manager)
    before = manager.agents["a1"].holdings[0]
    manager.op_propose("a1", "a2", {"fish": before * 0.5}, {"grain": 0.1})
    assert manager.agents["a1"].holdings[0] == pytest.approx(before * 0.5)
    # ...and the escrowed half cannot be offered a second time.
    with pytest.raises(TradeError, match="cannot cover"):
        manager.op_propose("a1", "a3", {"fish": before * 0.9}, {"cloth": 0.1})


def test_only_the_named_seller_can_approve(manager):
    _produce_all(manager)
    trade = manager.op_propose("a1", "a2", {"fish": 0.05}, {"grain": 0.01})
    with pytest.raises(TradeError, match="addressed to a2"):
        manager.op_approve("a3", trade["trade_id"])
    assert manager.trades[trade["trade_id"]].status == "pending"


def test_a_trade_settles_exactly_once(manager):
    _produce_all(manager)
    trade = manager.op_propose("a1", "a2", {"fish": 0.05}, {"grain": 0.01})
    manager.op_approve("a2", trade["trade_id"])
    with pytest.raises(TradeError, match="already executed"):
        manager.op_approve("a2", trade["trade_id"])


def test_goods_actually_change_hands_on_approval(manager):
    _produce_all(manager)
    buyer_grain = manager.agents["a1"].holdings[1]
    seller_fish = manager.agents["a2"].holdings[0]
    trade = manager.op_propose("a1", "a2", {"fish": 0.05}, {"grain": 0.01})
    manager.op_approve("a2", trade["trade_id"])
    assert manager.agents["a1"].holdings[1] == pytest.approx(buyer_grain + 0.01)
    assert manager.agents["a2"].holdings[0] == pytest.approx(seller_fish + 0.05)
    manager.check_conservation()


def test_a_seller_who_cannot_cover_returns_the_escrow(manager):
    _produce_all(manager)
    fish = manager.agents["a1"].holdings[0]
    trade = manager.op_propose("a1", "a2", {"fish": fish * 0.5}, {"grain": 1e9})
    with pytest.raises(TradeError, match="cannot cover"):
        manager.op_approve("a2", trade["trade_id"])
    assert manager.agents["a1"].holdings[0] == pytest.approx(fish)
    assert manager.trades[trade["trade_id"]].status == "rejected"
    manager.check_conservation()


def test_an_unanswered_proposal_expires_and_releases_its_escrow(manager):
    """The lease argument applied to goods: the release is the half that gets
    dropped, so nothing may depend on anybody remembering to do it."""
    _produce_all(manager)
    fish = manager.agents["a1"].holdings[0]
    trade = manager.op_propose("a1", "a2", {"fish": fish * 0.5}, {"grain": 0.01})
    for _ in range(4):
        manager.advance()
    assert manager.trades[trade["trade_id"]].status == "expired"
    assert manager.agents["a1"].holdings[0] == pytest.approx(fish)
    manager.check_conservation()


def test_conservation_holds_through_a_whole_run(island):
    """Trade moves goods and never creates them. An arm that appeared to beat
    the frontier would fail here first."""
    for arm in "ABCD":
        outcome = run_island(island, arm, seed=3, trade_rounds=8)
        assert outcome.executed >= 0


def test_discovery_comes_before_anything_is_committed(island):
    """Deliberation has to precede manufacturing, or it cannot plan for it.

    The Tier 2 runs exposed this in themselves: production was committed in the
    first round, before anybody had spoken, so every convention on the ladder
    could only describe the world after the one irreversible decision was
    already behind it. Implied production quality came out at the exchange
    ceiling in every arm — nobody specialised, because nobody could.
    """
    manager = Manager(island=island, phase="discovery")
    with pytest.raises(TradeError, match="production is discovery"):
        manager.op_produce("a1", {"fish": 1.0})
    with pytest.raises(TradeError, match="trading is discovery"):
        manager.op_propose("a1", "a2", {"fish": 1.0}, {"grain": 1.0})

    manager.open_production()
    assert manager.op_produce("a1", {"fish": 1.0})["produced"]["fish"] > 0
    manager.open_trading()
    manager.check_conservation()


def test_a_phase_cannot_be_skipped_or_replayed(island):
    """The manager owns the phase, so no agent can advance it early — and the
    operator cannot accidentally reopen one either."""
    manager = Manager(island=island, phase="discovery")
    with pytest.raises(TradeError, match="cannot open trading from discovery"):
        manager.open_trading()
    manager.open_production()
    with pytest.raises(TradeError, match="cannot open production from production"):
        manager.open_production()
    manager.open_trading()
    with pytest.raises(TradeError, match="cannot open production from trading"):
        manager.open_production()


def test_tier_one_still_starts_in_production(island):
    """Tier 1 does its price discovery outside the manager entirely, so the new
    phase defaults off and its runs are unchanged."""
    assert Manager(island=island).phase == "production"


def test_one_shot_labour_is_spent_once_and_cannot_be_unwound(manager):
    """The default: everything staked before any price exists.

    This is what made specialisation a bet — and, when settlement then failed,
    what made ruin total rather than marginal.
    """
    manager.op_produce("a1", {"fish": 1.0})
    with pytest.raises(TradeError, match="already worked this round"):
        manager.op_produce("a1", {"grain": 1.0})
    manager.advance()
    with pytest.raises(TradeError, match="no labour left"):
        manager.op_produce("a1", {"grain": 1.0})


def test_labour_is_bounded(manager):
    with pytest.raises(TradeError, match="must sum to at most 1"):
        manager.op_produce("a1", {"fish": 0.7, "grain": 0.7})


def test_rolling_labour_spreads_the_same_endowment_over_rounds(island):
    """Instalments, not a bigger economy.

    Total labour is still exactly one unit per agent, so the frontier, the
    autarky floor and the exchange ceiling are all untouched and a rolling run
    is directly comparable to a one-shot one. The only thing that varies is
    whether a commitment can be revised after seeing what prices do.
    """
    manager = Manager(island=island, labour_per_round=0.25, rolling=True)
    for _ in range(4):
        manager.op_produce("a1", {"fish": 1.0})
        manager.advance()
    state = manager.agents["a1"]
    assert state.spent == pytest.approx(1.0)
    assert sum(state.shares) == pytest.approx(1.0)
    # Four quarter-instalments on fish is exactly one whole unit on fish.
    assert state.holdings[0] == pytest.approx(state.capacity[0])
    with pytest.raises(TradeError, match="no labour left"):
        manager.op_produce("a1", {"grain": 1.0})


def test_rolling_labour_can_be_redirected_as_prices_appear(island):
    """The point of instalments: an agent may change its mind."""
    manager = Manager(island=island, labour_per_round=0.5, rolling=True)
    manager.op_produce("a1", {"fish": 1.0})
    manager.advance()
    manager.op_produce("a1", {"grain": 1.0})
    state = manager.agents["a1"]
    assert state.shares[0] == pytest.approx(0.5)
    assert state.shares[1] == pytest.approx(0.5)
    manager.check_conservation()


def test_rolling_labour_still_only_works_once_a_round(island):
    manager = Manager(island=island, labour_per_round=0.25, rolling=True)
    manager.op_produce("a1", {"fish": 1.0})
    with pytest.raises(TradeError, match="already worked this round"):
        manager.op_produce("a1", {"grain": 1.0})


def test_labour_stays_shut_during_trading_however_the_labour_rolls(island):
    """The stage deadline is real, and rolling is not an exception to it.

    It used to be one: ``op_produce`` let a rolling island commit labour during
    trading, because rolling had no production stage of its own to commit it in.
    Now every round opens one, so the allowance is not merely unnecessary — it
    would make the deadline advisory, and a deadline agents can work past is a
    deadline the manager is not enforcing.
    """
    for rolling in (False, True):
        manager = Manager(island=island, rolling=rolling,
                          labour_per_round=0.5 if rolling else 1.0)
        manager.op_produce("a1", {"fish": 1.0})
        manager.open_trading()
        with pytest.raises(TradeError, match="production is trading, not open"):
            manager.op_produce("a2", {"fish": 1.0})

    # ...and the way a rolling island gets its next instalment is by the round
    # coming round again, which reopens the stage properly.
    rolling = Manager(island=island, labour_per_round=0.5, rolling=True)
    rolling.op_produce("a1", {"fish": 1.0})
    rolling.open_trading()
    rolling.advance()
    rolling.open_discovery()
    rolling.open_production()
    assert rolling.op_produce("a1", {"grain": 1.0})["labour_left"] == 0.0
    rolling.open_trading()
    rolling.check_conservation()


def test_a_bad_request_costs_a_turn_and_nothing_more(manager):
    """Agent error must not raise out of the serving loop — one agent sending
    nonsense cannot be allowed to take the run down with it."""
    reply = manager.dispatch("a1", {"op": "propose", "seller": "nobody",
                                    "give": {"fish": 1}, "want": {"grain": 1}})
    assert reply["ok"] is False and "error" in reply
    assert manager.dispatch("a1", {"op": "wat"})["ok"] is False


def test_state_is_private(manager):
    """An agent sees itself and nothing about anyone else. This is what leaves
    room for communication to matter at all."""
    view = manager.op_state("a1")
    blob = repr(view)
    assert "a2" not in blob and "a3" not in blob


# --- the scorer agrees with theory -----------------------------------------


def test_competitive_equilibrium_is_pareto_optimal(island):
    """The First Welfare Theorem, used as a self-test on the scorer.

    ``walras`` finds the equilibrium from the primal side (a Negishi fixed point
    on welfare weights); ``efficiency`` bounds it from the dual side (prices and
    an expenditure argument). They share no code path, so agreeing at 1.0 is a
    real check rather than a tautology.
    """
    point = walras(island)
    score = efficiency(island, point.utilities)
    assert score.lower > 0.99
    assert score.upper == pytest.approx(1.0, abs=1e-3)


def test_the_efficiency_bracket_is_a_real_bracket(island):
    _, autarky_utils = autarky(island)
    for utils in (autarky_utils, walras(island).utilities):
        score = efficiency(island, utils)
        assert score.lower <= score.upper + 1e-9
        assert 0.0 <= score.lower <= 1.0


def test_autarky_is_inside_the_frontier(island):
    """If this ever hit 1.0 there would be nothing to study."""
    _, autarky_utils = autarky(island)
    assert efficiency(island, autarky_utils).upper < 0.95


def test_swapping_alone_cannot_reach_the_frontier(island):
    """The gains sit mostly in producing differently, not in swapping better.

    This is the claim that decides what the agents ought to be talking about, so
    it gets a gate rather than a footnote.
    """
    _, autarky_utils = autarky(island)
    floor = efficiency(island, autarky_utils).lower
    ceiling = exchange_ceiling(island).lower
    assert floor < ceiling < 1.0
    assert (ceiling - floor) < 0.5 * (1.0 - floor)


def test_a_zero_holding_is_reported_as_ruin_not_averaged(island):
    """Cobb-Douglas utility is zero when any good is missing. Letting that
    become a small number in a mean would hide the outcome that matters most."""
    _, autarky_utils = autarky(island)
    wrecked = list(autarky_utils)
    wrecked[0] = 0.0
    assert efficiency(island, wrecked).ruined == (0,)


def test_the_planner_traces_the_frontier_not_one_point(island):
    """Different welfare weights must give genuinely different allocations, or
    the 'frontier' is a single point and the whole measure is vacuous."""
    n = island.n_agents
    biased = [0.7] + [0.3 / (n - 1)] * (n - 1)
    even = planner(island, [1.0 / n] * n)
    tilted = planner(island, biased)
    assert tilted.utilities[0] > even.utilities[0] * 1.2
    # ...and both are on the frontier.
    assert efficiency(island, tilted.utilities).lower > 0.99
    assert efficiency(island, even.utilities).lower > 0.99


def test_an_island_cannot_have_goods_nobody_can_name():
    """Goods are named because the names end up in prompts and on a ledger.
    Running out of names would hand agents an island they cannot discuss."""
    with pytest.raises(ValueError, match="n_goods must be"):
        draw_island(4, 9, seed=1)
    with pytest.raises(ValueError, match="at least two agents"):
        draw_island(1, 5, seed=1)


def test_utility_is_homogeneous_of_degree_one(island):
    """Doubling a bundle doubles utility. Every metric here is a ratio, which
    only means anything because of this."""
    bundle = [1.0, 2.0, 3.0, 4.0, 5.0]
    doubled = [2 * x for x in bundle]
    assert utility(island.alpha[0], doubled) == pytest.approx(
        2 * utility(island.alpha[0], bundle))


# --- the arms behave as the experiment claims -------------------------------


def test_voluntary_trade_never_leaves_the_silent_arm_worse_off(island):
    """Arm A produces its autarky bundle and only settles trades both sides
    scored as gains, so nobody can finish below where they started. If this
    fails, an arm is committing agents to losses and every comparison is off."""
    outcome = run_island(island, "A", seed=11, trade_rounds=20)
    assert outcome.worst_ratio >= 1.0 - 1e-6


def test_the_silent_arm_swaps_well_but_stays_near_the_exchange_ceiling(island):
    """The decomposition the whole experiment rests on: blind bilateral trading
    is good at swapping and cannot fix what was produced."""
    outcome = run_island(island, "A", seed=11, trade_rounds=40)
    assert outcome.exchange_efficiency.lower > 0.85
    assert outcome.efficiency.upper < 0.85


def test_a_shared_price_reaches_the_frontier_when_it_settles():
    """Arm C's claim, on an island where it does settle. The point is that the
    ceiling really is reachable by agents trading bilaterally through the
    manager — so anything short of it is about the agents, not the protocol."""
    island = draw_island(12, 5, seed=2)
    outcome = run_island(island, "C", seed=2, trade_rounds=60)
    assert not outcome.efficiency.ruined
    assert outcome.efficiency.lower > 0.95


# --- the hub carries the market without changing it -------------------------


def test_hub_and_direct_transports_agree():
    """Same seed, same island, same arm: over a real hub and in process.

    This is the load-bearing claim about Switchboard in the whole experiment.
    Requests go over real messages, replies come back correlated by request id,
    state is mirrored to the blackboard — and the economics is bit-identical to
    calling the state machine directly.
    """
    island = draw_island(4, 5, seed=5)
    direct = run_island(island, "D", seed=5, trade_rounds=6)
    with hub() as handle:
        over_hub = run_island(island, "D", seed=5, trade_rounds=6, hub=handle, run="r1")
    assert over_hub.utilities == pytest.approx(direct.utilities)
    assert (over_hub.executed, over_hub.proposed) == (direct.executed, direct.proposed)


def test_the_manager_serves_requests_over_the_hub():
    """The request/reply path on its own, with nothing scripted around it."""
    island = draw_island(3, 5, seed=9)
    manager = Manager(island=island)
    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run="r2")
        service.claim()
        rpc = ManagerRPC(handle.client("a1"))

        rpc.client.send("manager", {"op": "state", "req": "x1"})
        service.drain()
        reply = next(m["body"] for m in rpc.client.inbox()
                     if isinstance(m["body"], dict) and m["body"].get("req") == "x1")
        assert reply["ok"] and reply["you"] == "a1"
        assert set(reply["capacity"]) == set(manager.goods)


def test_rpc_picks_out_its_own_reply_from_interleaved_traffic():
    """Replies are matched on a request id, and anything else in the inbox is
    kept rather than dropped.

    ``inbox`` advances a cursor, so a message read and discarded is gone. An
    agent on a shared hub is receiving peer chatter and manager replies on the
    same channel, so a naive "the next message is my answer" would lose real
    replies as soon as anybody else spoke.
    """
    island = draw_island(3, 5, seed=9)
    manager = Manager(island=island)
    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run="r7")
        service.claim()
        client = handle.client("a1")
        rpc = ManagerRPC(client, pump=service.drain)

        # A peer DMs a1 before the reply lands, so the inbox holds both.
        handle.client("a2").send("a1", {"text": "want to swap?"})
        reply = rpc.call("state")
        assert reply["ok"] and reply["you"] == "a1"

        # The peer's message was not consumed by the correlation.
        leftover = [m for m in rpc._spare if m["body"].get("text")]
        assert leftover and leftover[0]["from"] == "a2"

        # ...and a second call still works after the buffering.
        assert rpc.call("pending")["ok"]


def test_state_is_mirrored_to_the_blackboard():
    """A peer that never spoke to the manager can still read where the run got
    to — which is the argument for putting it on the board at all."""
    island = draw_island(3, 5, seed=9)
    manager = Manager(island=island)
    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run="r3")
        service.claim()
        _produce_all(manager)
        service.publish()

        observer = handle.client("observer")
        header = observer.board_get("barter/r3/run")
        assert header["phase"] == "trading"
        assert header["agents"] == sorted(manager.agents)
        assert observer.board_get("barter/r3/agent/a1")["produced"] is True


def test_a_second_manager_cannot_take_the_state_lease():
    """Two managers writing one ledger is the failure the lease prevents."""
    from switchboard.client import LeaseHeld

    island = draw_island(3, 5, seed=9)
    with hub() as handle:
        first = ManagerService(handle.client("m1"), Manager(island=island), run="r4")
        first.claim()
        second = ManagerService(handle.client("m2"), Manager(island=island), run="r4")
        with pytest.raises(LeaseHeld):
            second.claim()


def test_the_ledger_is_public_when_the_arm_allows_it():
    """Executed trades land on a channel any agent can read. Whether that is
    switched on is a convention lever, so it has to actually work."""
    island = draw_island(3, 5, seed=9)
    manager = Manager(island=island)
    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run="r5")
        service.claim()
        _produce_all(manager)
        trade = manager.op_propose("a1", "a2", {"fish": 0.02}, {"grain": 0.005})

        client = handle.client("a2")
        client.send("manager", {"op": "approve", "trade_id": trade["trade_id"], "req": "z"})
        service.drain()

        posted = handle.client("nosy").history(service.ledger_channel)
        assert any(m["body"]["id"] == trade["trade_id"] for m in posted)


def test_identity_comes_from_the_hub_not_the_message_body():
    """An agent cannot approve someone else's trade by writing a name into its
    own message — the sender is whoever the hub says it is."""
    island = draw_island(3, 5, seed=9)
    manager = Manager(island=island)
    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run="r6")
        service.claim()
        _produce_all(manager)
        trade = manager.op_propose("a1", "a2", {"fish": 0.02}, {"grain": 0.005})

        impostor = handle.client("a3")
        impostor.send("manager", {"op": "approve", "trade_id": trade["trade_id"],
                                  "agent_id": "a2", "req": "q"})
        service.drain()
        assert manager.trades[trade["trade_id"]].status == "pending"


# --- labour timing ----------------------------------------------------------
#
# The convention ladder tries to make a one-shot production bet a *better* bet:
# disclose, agree a price, circulate money. Slicing the labour attacks the same
# loss from the other side — it makes a wrong bet *unwindable*. These gates are
# about the second being a genuine alternative and not an accounting error.


def test_instalments_spend_the_same_unit_of_labour_and_no_more(island):
    """The comparison is worth nothing if a rolling island simply works harder.

    Same endowment, same frontier, same benchmarks — only the timing differs. A
    rolling island that quietly produced twice as much would beat a one-shot one
    for a reason that has nothing to do with responsiveness.
    """
    once = run_island(island, "C", seed=3, trade_rounds=6, instalments=1)
    rolling = run_island(island, "C", seed=3, trade_rounds=6, instalments=7)
    assert once.instalments == 1 and rolling.instalments == 7
    for outcome in (once, rolling):
        assert outcome.efficiency.lower <= 1.0 + 1e-6


def test_a_rolling_island_actually_works_in_every_round(island):
    """The manager refuses two commitments in one tick, and the opening
    instalment lands on the same tick as the first trading round. Without the
    clock being advanced between them the first instalment of every agent is
    silently rejected — the run still finishes, one slice of everyone's labour
    just never happens, and the arm reads as a failure of nerve."""
    from barter.manager import Manager
    from barter.traders import Floor, Trader

    manager = Manager(island=island, labour_per_round=0.25, rolling=True)
    goods = manager.goods

    traders = {}
    for agent_id, state in manager.agents.items():
        trader = Trader(agent_id, state.index, island, "A", __import__("random").Random(1))
        trader.goods = goods
        traders[agent_id] = trader

    floor = Floor(enabled=False)
    for agent_id, trader in traders.items():
        manager.dispatch(agent_id, {"op": "produce", "plan": trader.production_plan(floor)})
    manager.open_trading()
    manager.advance()
    for _ in range(3):
        # The round comes round again: a real production stage, opened and
        # closed by the manager, exactly as Tier 2's rounds do.
        manager.open_discovery()
        manager.open_production()
        for agent_id, trader in traders.items():
            holdings = list(manager.agents[agent_id].holdings)
            reply = manager.dispatch(
                agent_id, {"op": "produce",
                           "plan": trader.production_instalment(holdings, floor)})
            assert reply.get("ok"), reply
        manager.open_trading()
        manager.advance()
    for state in manager.agents.values():
        assert state.spent == pytest.approx(1.0, abs=1e-9)
    manager.check_conservation()


def test_the_first_instalment_is_exactly_the_one_shot_plan(island):
    """Rolling has to *start* where one-shot starts, or the two differ from the
    first move and the comparison is between two policies rather than between
    two timings."""
    from barter.manager import Manager
    from barter.traders import Floor, Trader

    manager = Manager(island=island)
    floor = Floor(enabled=False)
    for agent_id, state in manager.agents.items():
        trader = Trader(agent_id, state.index, island, "A", __import__("random").Random(1))
        trader.goods = manager.goods
        empty = [0.0] * island.n_goods
        assert trader.production_instalment(empty, floor) == trader.production_plan(floor)


def test_slicing_labour_trades_the_frontier_away_for_insurance(island):
    """The Tier 1 finding, pinned so it cannot drift into the opposite claim.

    Under a shared price a one-shot island either reaches the frontier or wrecks
    somebody: specialisation is a commitment, and a commitment that settlement
    fails to honour is total loss. Slicing the labour removes the ruin and gives
    up most of the specialisation to do it — an agent that keeps re-aiming at
    what it is short of stops making what it is *best at*. That is a real
    trade-off rather than a free improvement, and reporting it as a free
    improvement is the specific error this gate exists to prevent.
    """
    islands = [draw_island(8, 5, seed=20 + i) for i in range(4)]
    once = [run_island(isl, "C", seed=20 + i, trade_rounds=20, instalments=1)
            for i, isl in enumerate(islands)]
    rolling = [run_island(isl, "C", seed=20 + i, trade_rounds=20, instalments=21)
               for i, isl in enumerate(islands)]
    assert sum(1 for o in rolling if o.efficiency.ruined) \
        <= sum(1 for o in once if o.efficiency.ruined)
    clean = [i for i in range(len(islands))
             if not (once[i].efficiency.ruined or rolling[i].efficiency.ruined)]
    for i in clean:
        assert rolling[i].efficiency.lower <= once[i].efficiency.lower + 1e-6


# --- who the gains went to --------------------------------------------------
#
# Efficiency is distribution-neutral by construction: it scales every agent by
# the same factor, so it measures how much was wasted and says nothing about
# who got it. That separation is deliberate, and it means the second question
# needs its own answer. These gates are about that answer being meaningful.


def test_the_gain_ratios_are_measured_against_each_agent_s_own_autarky(island):
    """Not against each other, and the distinction is not stylistic.

    Cobb-Douglas utilities are not interpersonally comparable — each is defined
    only up to its own monotone transformation — so "agent 3 has more utility
    than agent 7" is arithmetic without meaning, and a Gini or Nash product over
    raw utilities would inherit that meaninglessness. A ratio to the agent's own
    counterfactual is a true statement about that agent, and ratios can then be
    compared because they are pure numbers against a per-agent baseline.
    """
    from barter.economy import gains

    _, autarky_utils = autarky(island)
    self_scored = gains(island, autarky_utils)
    assert all(abs(r - 1.0) < 1e-9 for r in self_scored.ratios)
    assert self_scored.below == 0 and self_scored.median == pytest.approx(1.0)

    # Doubling one agent's utility must move only that agent's ratio.
    bumped = list(autarky_utils)
    bumped[0] *= 2
    after = gains(island, bumped)
    assert after.ratios[0] == pytest.approx(2.0)
    assert after.ratios[1:] == self_scored.ratios[1:]


def test_the_competitive_equilibrium_leaves_nobody_below_autarky(island):
    """A theorem, used as a self-test on the measure.

    At equilibrium prices an agent's income is at least the value of its own
    autarky bundle — that bundle is still feasible for it — so it can always
    afford autarky and its equilibrium utility cannot be lower. If `below` were
    ever non-zero here, the measure would be wrong, not the economy.
    """
    from barter.economy import gains, walras

    for seed in (1, 5, 9):
        isl = draw_island(6, 5, seed=seed)
        shared = gains(isl, walras(isl).utilities)
        assert shared.below == 0, f"seed {seed}: {shared}"
        assert shared.worst >= 1.0 - 1e-6


def test_below_autarky_sees_harm_that_the_worst_agent_alone_cannot(island):
    """The reason this column exists at all.

    One agent at 0.5x and six agents at 0.9x are different failures — a bad
    draw against a convention that systematically harms a subgroup — and `worst`
    reports them identically. So does ruin, which only counts the agents that
    reached exactly zero.
    """
    from barter.economy import Gains

    concentrated = Gains(ratios=(0.5, 1.4, 1.4, 1.4, 1.4, 1.4), worst=0.5,
                         median=1.4, below=1)
    diffuse = Gains(ratios=(0.9, 0.9, 0.9, 0.9, 0.9, 0.5), worst=0.5,
                    median=0.9, below=6)
    assert concentrated.worst == diffuse.worst
    assert concentrated.below != diffuse.below

    # And on a real run it is genuinely not redundant with ruin: an arm can ruin
    # one agent while leaving several more under their own autarky.
    outcome = run_island(draw_island(12, 5, seed=3), "D", seed=3, trade_rounds=60)
    assert outcome.gains.below > len(outcome.efficiency.ruined)
    assert outcome.gains.worst == pytest.approx(min(outcome.gains.ratios))


def test_a_ruined_agent_scores_zero_in_the_ratios_rather_than_a_sentinel(island):
    """Here 0.0 is the true value, not the "undefined" marker efficiency uses.

    The agent's utility really is zero and its autarky utility really is
    positive, so the ratio really is zero — and unlike the efficiency bracket,
    this one stays meaningful and can be averaged.
    """
    from barter.economy import gains

    _, autarky_utils = autarky(island)
    wrecked = list(autarky_utils)
    wrecked[1] = 0.0
    shared = gains(island, wrecked)
    assert shared.ratios[1] == 0.0
    assert shared.below == 1 and shared.worst == 0.0
