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


def test_production_is_committed_once(manager):
    manager.op_produce("a1", {"fish": 1.0})
    with pytest.raises(TradeError, match="already produced"):
        manager.op_produce("a1", {"grain": 1.0})


def test_labour_is_bounded(manager):
    with pytest.raises(TradeError, match="1.0 unit of labour"):
        manager.op_produce("a1", {"fish": 0.7, "grain": 0.7})


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
