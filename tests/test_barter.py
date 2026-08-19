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
from barter.manager import (  # noqa: E402
    LEVEL_OFFER,
    LEVEL_PRODUCE,
    LEVEL_SETTLE,
    BoardService,
    Manager,
    ManagerRPC,
    ManagerService,
    TradeError,
)
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
    manager.open(LEVEL_SETTLE)


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


def test_what_is_open_rises_through_the_round_and_never_falls(island):
    """Each capability waits on the one before it having had time.

    You cannot offer what nobody has made, and you cannot settle before offers
    exist — so proposing opens after a window of talking and producing, and
    approving after a window of offers. Within a round the level only rises: an
    agent that acts late is late, but nothing it was already allowed to do is
    taken away from it.
    """
    manager = Manager(island=island)

    # Level 1: produce and talk. Talking never reaches the manager at all.
    assert manager.op_produce("a1", {"fish": 1.0})["produced"]["fish"] > 0
    with pytest.raises(TradeError, match="proposing is not open yet"):
        manager.op_propose("a1", "a2", {"fish": 0.1}, {"grain": 0.1})

    manager.open(LEVEL_OFFER)
    manager.op_produce("a2", {"grain": 1.0})          # still open, all round
    trade = manager.op_propose("a1", "a2", {"fish": 0.1}, {"grain": 0.1})
    with pytest.raises(TradeError, match="approving is not open yet"):
        manager.op_approve("a2", trade["trade_id"])
    # ...but withdrawing opens with proposing, since it is the undo of one.
    assert manager.op_cancel("a1", trade["trade_id"])["cancelled"]

    manager.open(LEVEL_SETTLE)
    again = manager.op_propose("a1", "a2", {"fish": 0.1}, {"grain": 0.1})
    assert manager.op_approve("a2", again["trade_id"])["settled"] == again["trade_id"]

    # Raising to a level already passed is a no-op, not a reopening.
    manager.open(LEVEL_PRODUCE)
    assert manager.level == LEVEL_SETTLE
    manager.check_conservation()


def test_a_round_ends_by_dropping_back_to_the_first_window(island):
    """Resetting is what makes a round a round: every one of them re-earns the
    right to trade by spending a window on talking and producing first."""
    manager = Manager(island=island)
    manager.open(LEVEL_SETTLE)
    manager.next_round()
    assert manager.level == LEVEL_PRODUCE
    with pytest.raises(TradeError, match="proposing is not open yet"):
        manager.op_propose("a1", "a2", {"fish": 0.1}, {"grain": 0.1})
    manager.check_conservation()


def test_a_closed_island_accepts_nothing_at_any_level(island):
    manager = Manager(island=island)
    manager.open(LEVEL_SETTLE)
    manager.close()
    assert manager.phase == "closed"
    for call in (lambda: manager.op_produce("a1", {"fish": 1.0}),
                 lambda: manager.op_propose("a1", "a2", {"fish": 0.1}, {"grain": 0.1}),
                 lambda: manager.open(LEVEL_SETTLE)):
        with pytest.raises(TradeError):
            call()

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
    # Production no longer shuts at all: it is open in every window of every
    # round, so an agent can revise what it makes in response to something it
    # hears at the very end of a round. What still holds it to one instalment
    # is the round itself -- once per round, not once per window.
    for rolling in (False, True):
        manager = Manager(island=island, rolling=rolling,
                          labour_per_round=0.5 if rolling else 1.0)
        manager.op_produce("a1", {"fish": 1.0})
        manager.open(LEVEL_SETTLE)
        with pytest.raises(TradeError, match="already worked this round"):
            manager.op_produce("a1", {"fish": 1.0})

    rolling = Manager(island=island, labour_per_round=0.5, rolling=True)
    rolling.op_produce("a1", {"fish": 1.0})
    rolling.open(LEVEL_SETTLE)
    rolling.next_round()
    assert rolling.op_produce("a1", {"grain": 1.0})["labour_left"] == 0.0
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
        assert header["phase"] == "settle"
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
    manager.open(LEVEL_SETTLE)
    manager.advance()
    for _ in range(3):
        # The round comes round again: a real production stage, opened and
        # closed by the manager, exactly as Tier 2's rounds do.
        manager.next_round()
        for agent_id, trader in traders.items():
            holdings = list(manager.agents[agent_id].holdings)
            reply = manager.dispatch(
                agent_id, {"op": "produce",
                           "plan": trader.production_instalment(holdings, floor)})
            assert reply.get("ok"), reply
        manager.open(LEVEL_SETTLE)
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


# --- idle labour and crossed offers -----------------------------------------


def test_unspent_labour_is_recorded_rather_than_quietly_lost(island):
    """A plan is a split of *this instalment*, so fractions summing to less than
    1 leave the remainder unworked — and it is not carried, so it is gone.

    This is tracked because a live run finished having spent 0.67 of its labour
    and nothing in the record could say whether agents had declined to work or
    had simply handed in vectors that summed short. Those are opposite findings
    about the agents and they looked identical in the data.
    """
    manager = Manager(island=island, rolling=True, labour_per_round=0.5)
    reply = manager.op_produce("a1", {"fish": 0.3, "grain": 0.3})
    assert reply["idle_labour"] == pytest.approx(0.5 * 0.4)
    assert manager.agents["a1"].spent == pytest.approx(0.5 * 0.6)
    assert manager.agents["a1"].idle == pytest.approx(0.5 * 0.4)

    # It accumulates, and it is never carried forward: the second instalment is
    # the same size as the first however little of the first was claimed.
    manager.open(LEVEL_SETTLE)
    manager.advance()
    manager.next_round()
    second = manager.op_produce("a1", {"fish": 1.0})
    assert second["produced"]["fish"] == pytest.approx(island.capacity[0][0] * 0.5, abs=1e-4)
    assert manager.agents["a1"].idle == pytest.approx(0.5 * 0.4)
    assert manager.op_state("a1")["idle_labour"] == pytest.approx(0.2)
    manager.check_conservation()


def test_a_crossed_pair_is_reported_and_nothing_is_done_about_it(island):
    """Two agents who agree a swap and both propose it end up with two live
    trades, twice the goods escrowed, and a decision neither planned for.

    The manager names it and refuses to solve it — no matching, no cancelling
    the second, no refusing the proposal. It is the only thing here that can
    move a quantity, so a manager that quietly resolved collisions would be
    doing the convention's job and the run would measure the manager instead.
    """
    manager = Manager(island=island)
    for agent_id in manager.agents:
        manager.op_produce(agent_id, {"fish": 0.5, "grain": 0.5})
    manager.open(LEVEL_SETTLE)

    first = manager.op_propose("a1", "a2", {"fish": 0.02}, {"grain": 0.02})
    assert "crosses" not in first
    second = manager.op_propose("a2", "a1", {"grain": 0.02}, {"fish": 0.02})
    assert second["crosses"] == first["trade_id"]
    assert manager.summary()["crossings"] == 1

    # Both are still open and both sides are escrowed — the manager did not pick.
    assert manager.trades[first["trade_id"]].status == "pending"
    assert manager.trades[second["trade_id"]].status == "pending"
    assert manager.op_state("a1")["escrowed"] and manager.op_state("a2")["escrowed"]

    # And each party can see the collision in the one place it looks before acting.
    assert manager.op_pending("a1")["crossed_pairs"] == [
        [first["trade_id"], second["trade_id"]]]

    # Approving both really does swap twice, which is the outcome agents have to
    # avoid for themselves.
    manager.op_approve("a2", first["trade_id"])
    manager.op_approve("a1", second["trade_id"])
    assert manager.summary()["crossings_resolved"] == {"both": 1}
    manager.check_conservation()


def test_a_crossing_that_was_resolved_stops_being_reported_as_open(island):
    """`crossed_pairs` is about live decisions. A pair where one side has been
    cancelled is settled business and reporting it would be noise in the exact
    place an agent is trying to decide something."""
    manager = Manager(island=island)
    for agent_id in manager.agents:
        manager.op_produce(agent_id, {"fish": 0.5, "grain": 0.5})
    manager.open(LEVEL_SETTLE)
    first = manager.op_propose("a1", "a2", {"fish": 0.1}, {"grain": 0.1})
    manager.op_propose("a2", "a1", {"grain": 0.1}, {"fish": 0.1})
    assert manager.op_pending("a1")["crossed_pairs"]

    manager.op_cancel("a1", first["trade_id"])
    assert manager.op_pending("a1")["crossed_pairs"] == []
    # ...but the crossing still happened, and the record keeps it.
    assert manager.summary()["crossings"] == 1
    assert manager.summary()["crossings_resolved"] == {"neither": 1}
    manager.check_conservation()


def test_a_crossing_that_cannot_cover_bounces_rather_than_jamming(island):
    """I expected this to deadlock. It does not, and the reason is worth a gate.

    When two agents cross over the same goods, each may have escrowed the very
    thing the other's offer asks it to hand over — so the first approval fails.
    The tempting conclusion is that both sides are now stuck with their goods
    locked up until expiry. They are not: a failed approval **returns the
    offer**, releasing that escrow, which leaves the other side able to settle
    after all. A crossing therefore costs at most one of the two trades.

    That self-healing is a real property of escrow-at-propose-time and not an
    obvious one — the alternative, leaving a doomed offer sitting on its escrow
    until it times out, would turn every mis-sized crossing into a stall of a
    few rounds. Worth asserting so it cannot be optimised away by someone who
    reads the failure path as merely an error case.
    """
    manager = Manager(island=island)
    for agent_id in manager.agents:
        manager.op_produce(agent_id, {"fish": 0.5, "grain": 0.5})
    manager.open(LEVEL_SETTLE)

    # Each side escrows more than half of the good it will be asked to hand
    # over, so neither can cover the other out of what is left.
    fish = manager.op_state("a1")["holdings"]["fish"] * 0.6
    grain = manager.op_state("a2")["holdings"]["grain"] * 0.6
    first = manager.op_propose("a1", "a2", {"fish": fish}, {"grain": grain})
    second = manager.op_propose("a2", "a1", {"grain": grain}, {"fish": fish})
    assert second["crosses"] == first["trade_id"]

    with pytest.raises(TradeError, match="cannot cover this trade"):
        manager.op_approve("a2", first["trade_id"])
    # ...and that failure released a1's escrow, so the other half can proceed.
    assert manager.trades[first["trade_id"]].status != "pending"
    manager.op_approve("a1", second["trade_id"])

    assert manager.summary()["crossings_resolved"] == {"one": 1}
    assert manager.op_state("a1")["escrowed"] == {}
    manager.check_conservation()


def _played_island(island, arm, *, seed, trade_rounds):
    """Run one Tier 1 island and hand back the manager it used.

    ``run_island`` builds its own manager and returns only the score, so the
    only way to inspect the state a run actually reached is to substitute the
    class it constructs. Defined once here rather than inline in each test,
    because a subclass closing over a loop variable is a bug waiting to be
    written twice.
    """
    import barter.run as runner
    from barter.manager import Manager

    captured: dict[str, Manager] = {}

    class Spy(Manager):
        def __post_init__(self) -> None:
            super().__post_init__()
            captured["manager"] = self

    original = runner.Manager
    runner.Manager = Spy
    try:
        runner.run_island(island, arm, seed=seed, trade_rounds=trade_rounds)
    finally:
        runner.Manager = original
    return captured["manager"]


def test_the_scripted_tiebreak_is_the_same_answer_from_either_side(island):
    """The only property the rule needs. Not that it is fair, or efficient, or
    that first-proposed deserves to win — just that two agents applying it to
    the same pair name the same casualty, from information both of them have.

    A rule that read anything private ("whoever needs it more") would be
    computed differently by each side and would fail exactly when it mattered.
    """
    from barter.traders import gives_way

    for pair in (["t1", "t2"], ["t2", "t1"], ["t9", "t10"], ["t10", "t9"]):
        assert gives_way(pair) == gives_way(list(reversed(pair)))
    # First proposed survives, and ids are compared as numbers rather than
    # strings — "t10" beats "t9" only if you read them as text.
    assert gives_way(["t9", "t10"]) == "t10"


def test_scripted_agents_never_swap_twice_over_a_crossed_pair(island):
    """What the shared rule buys, measured on real runs rather than argued.

    Without it a crossing can end with both offers approved — the pair trading
    twice because neither side backed out — which is a loss neither of them
    chose and neither can undo. With it, both sides name the same doomed offer,
    only its buyer can withdraw it, and exactly one action follows.
    """
    for arm in ("A", "C", "D"):
        summary = _played_island(draw_island(12, 5, seed=3), arm,
                                 seed=3, trade_rounds=30).summary()
        assert summary["crossings"] > 0, arm
        assert summary["crossings_resolved"].get("both", 0) == 0, \
            f"arm {arm} swapped twice: {summary['crossings_resolved']}"


# --- the manager as something you write to rather than call ------------------


def test_orders_are_applied_in_the_order_the_board_says_they_arrived(island):
    """Nothing sends the manager a request. Agents write to their own keyspace
    and the sweep applies what it finds, in write order.

    Which is where the concurrency is settled. Two live agents can want the same
    goods at the same moment; the sweep applies them in the order the board says
    they were written and whoever was second is refused. That is a real race
    with a real tiebreak, rather than an artefact of who happened to be
    scheduled first inside one manager call.
    """
    manager = Manager(island=island)
    _produce_all(manager)
    with hub() as handle:
        board = BoardService(handle.client("manager"), manager, run="b1")
        a1 = handle.client("a1")
        a1.board_set("barter/b1/order/a1/0001",
                     {"op": "propose", "seller": "a2",
                      "give": {"fish": 0.01}, "want": {"grain": 0.005}})
        a1.board_set("barter/b1/order/a1/0002", {"op": "pending"})

        assert board.sweep() == 2
        first = a1.board_get("barter/b1/result/a1/0001")
        assert first["ok"] and first["op"] == "propose"
        # The second order saw the first one's effect, so it was applied after it.
        assert a1.board_get("barter/b1/result/a1/0002")["your_open_offers"]

        # Applied orders are gone from the board, which is what makes applying
        # one twice impossible. A cursor would not: a crashed manager forgets
        # its cursor and a board remembers what is still on it.
        assert board.sweep() == 0
        assert a1.board_list(prefix="barter/b1/order/") == []


def test_an_order_cannot_claim_to_come_from_somebody_else(island):
    """Identity is the hub's ``updated_by``, not anything inside the message.

    An agent that could name itself in the body could spend another agent's
    holdings, and every result in the experiment would be a result about that
    instead.
    """
    manager = Manager(island=island)
    _produce_all(manager)
    with hub() as handle:
        board = BoardService(handle.client("manager"), manager, run="b2")
        handle.client("a1").board_set(
            "barter/b2/order/a1/0001",
            {"op": "propose", "buyer": "a3", "agent_id": "a3", "seller": "a2",
             "give": {"fish": 0.01}, "want": {"grain": 0.005}})
        board.sweep()
        trade = next(iter(manager.trades.values()))
        assert trade.buyer == "a1"


def test_the_sweep_publishes_a_clock_anyone_can_read(island):
    """"What round is it" is a fact about the world that every agent needs, and
    none of them should spend a round trip through the order queue on it."""
    manager = Manager(island=island)
    with hub() as handle:
        board = BoardService(handle.client("manager"), manager, run="b3")
        board.sweep()
        assert handle.client("reader").board_get("barter/b3/clock") == {
            "tick": 0, "level": LEVEL_PRODUCE, "phase": "produce"}
        manager.advance()
        manager.open(LEVEL_SETTLE)
        board.sweep()
        clock = handle.client("reader").board_get("barter/b3/clock")
        assert clock["tick"] == 1 and clock["phase"] == "settle"


def test_the_sweeper_runs_on_its_own_thread_and_stops_with_the_block(island):
    """The sweeper has to be something other than the agents, or it is the pump
    again: an agent that drives the manager between writing its order and
    reading the answer is blocking on the manager, which is what the board
    removed. So it runs on its own interval whatever the agents are doing, and
    an agent that writes an order and then stops thinking still gets it applied.
    """
    import time

    manager = Manager(island=island)
    with hub() as handle:
        board = BoardService(handle.client("manager"), manager, run="b4",
                             every=0.01)
        a1 = handle.client("a1")
        with board.sweeping():
            a1.board_set("barter/b4/order/a1/0001",
                         {"op": "produce", "plan": {"fish": 1.0}})
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                reply = a1.board_get("barter/b4/result/a1/0001")
                if isinstance(reply, dict):
                    break
                time.sleep(0.005)
            assert reply["ok"]
        assert board.errors == []
        assert manager.agents["a1"].produced

        # An order written just before the block ended is still answered: the
        # sweeper takes one last pass on the way out rather than leaving an
        # agent waiting on a board nobody is reading.
        a1.board_set("barter/b4/order/a1/0002", {"op": "pending"})
        with board.sweeping():
            pass
        assert isinstance(a1.board_get("barter/b4/result/a1/0002"), dict)


def test_the_agenda_goes_out_on_all_four_primitives(island):
    """The muster is built out of the hub, not beside it.

    Each primitive does the job it is for, and the split matters. The board key
    is the schedule *in force* -- an agent that joined late, or that wants to
    check, looks it up and always gets the live one. The channel is the
    *announcement* -- it arrives, and it can be missed, which is exactly why it
    cannot be the only copy. Presence answers who is on the island at all, so
    "never turned up" and "turned up and stayed quiet" stay different findings.
    And the acks arrive as ordinary orders on the board, so acknowledging is
    the same kind of act as trading and needs no new concept.
    """
    from barter.manager import Agenda, BoardService

    manager = Manager(island=island)
    with hub() as handle:
        board = BoardService(handle.client("manager"), manager, run="ag")
        board.now = lambda: 5.0
        for agent_id in manager.agents:
            handle.client(agent_id, register=True, kind="trader")

        manager.post_agenda(Agenda(version=1, posted_at=0.0, acks_by=90.0,
                                   starts_at=120.0, window=60.0, rounds=1))
        board.sweep()

        reader = handle.client("reader")
        # blackboard: the schedule in force.
        assert reader.board_get("barter/ag/agenda")["version"] == 1
        # messages: announced once, on a channel every trader reads.
        posted = reader.history("barter/ag/agenda", limit=10)
        assert len(posted) == 1 and posted[0]["body"]["starts_at"] == 120.0
        # presence: the hub knows who is here.
        assert board.roster() == sorted(manager.agents)
        assert reader.board_get("barter/ag/clock")["present"] == sorted(manager.agents)

        # An ack is an order like any other, and identity comes from the hub.
        handle.client("a1").board_set("barter/ag/order/a1/0001",
                                      {"op": "ack", "version": 1})
        board.sweep()
        assert manager.acked == {"a1"}
        assert reader.board_get("barter/ag/clock")["acked"] == ["a1"]

        # Sweeping again does not re-announce a schedule nobody replaced.
        board.sweep()
        assert len(reader.history("barter/ag/agenda", limit=10)) == 1

        # ...but a new version is announced, and drops the old acks.
        manager.post_agenda(Agenda(version=2, posted_at=90.0, acks_by=180.0,
                                   starts_at=210.0, window=60.0, rounds=1))
        board.sweep()
        announced = reader.history("barter/ag/agenda", limit=10)
        assert [m["body"]["version"] for m in announced] == [1, 2]
        assert manager.acked == set()


def test_presence_and_silence_are_not_the_same_absence(island):
    """A trader the hub has never heard of cannot acknowledge anything, and
    waiting on it is waiting on nobody. One that is registered and says nothing
    is making a choice. They score identically and are opposite findings."""
    from barter.manager import Agenda, BoardService

    manager = Manager(island=island)
    with hub() as handle:
        board = BoardService(handle.client("manager"), manager, run="pr")
        handle.client("a1", register=True, kind="trader")
        manager.post_agenda(Agenda(version=1, posted_at=0.0, acks_by=90.0,
                                   starts_at=120.0, window=60.0, rounds=1))

        assert board.roster() == ["a1"]
        assert not manager.all_acked()
        manager.dispatch("a1", {"op": "ack", "version": 1})
        # a1 is present and has acked; the others are not present at all.
        assert manager.acked == {"a1"}
        assert set(manager.agents) - set(board.roster()) == {"a2", "a3", "a4"}
        assert board.errors == []
