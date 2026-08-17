"""Gates for the model-facing side of the barter experiment.

No model is called here and no network is touched. What is under test is the
wiring a model would act through: that a tool call reaches the manager over the
hub, that identity is the hub's and not the model's to claim, and — the two that
decide whether the experiment measures what it claims — that the `silent` arm
genuinely *cannot* talk rather than being asked not to, and that `told` and
`built` share a system prompt byte for byte.

Both of those are about keeping instructions and affordances apart. If silence
were a line in a prompt, the silent arm would measure obedience; if `built`'s
prompt mentioned its quote tools, the instruction-versus-machinery comparison
would collapse back into one about wording.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))

from barter.economy import draw_island  # noqa: E402
from barter.llm import Wire, brief_for, tool_names  # noqa: E402
from barter.manager import Manager, ManagerService  # noqa: E402

from switchboard.testing import hub  # noqa: E402


@pytest.fixture
def island():
    return draw_island(3, 5, seed=4)


def _wired(handle, manager, arm, run="llm"):
    service = ManagerService(handle.client("manager"), manager, run=run)
    service.claim()
    wires = {
        agent_id: Wire(agent_id=agent_id, client=handle.client(agent_id), service=service,
                       arm=arm, floor_channel=f"barter/{run}/floor",
                       quote_prefix=f"barter/{run}/quote/", goods=tuple(manager.goods))
        for agent_id in manager.agents
    }
    return service, wires


def test_a_tool_call_reaches_the_manager_and_comes_back(island):
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "free")
        state = wires["a1"].manager_call("state")
        assert state["ok"] and state["you"] == "a1"
        assert set(state["capacity"]) == set(manager.goods)


def test_an_agent_only_ever_sees_its_own_state(island):
    """Everything an agent knows about anyone else it had to be told. That is
    what puts the channel under test rather than the prompt."""
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "free")
        blob = repr(wires["a1"].manager_call("state"))
        assert "a2" not in blob and "a3" not in blob


def test_the_two_phase_trade_works_through_the_tool_surface(island):
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "free")
        for agent_id, state in manager.agents.items():
            plan = {g: state.alpha[i] for i, g in enumerate(manager.goods)}
            assert wires[agent_id].manager_call("produce", plan=plan)["ok"]
        manager.open_trading()

        offered = wires["a1"].manager_call(
            "propose", seller="a2", give={"fish": 0.02}, want={"grain": 0.005})
        assert offered["ok"]
        trade_id = offered["trade_id"]

        waiting = wires["a2"].manager_call("pending")
        assert [t["id"] for t in waiting["awaiting_your_approval"]] == [trade_id]

        assert wires["a2"].manager_call("approve", trade_id=trade_id)["ok"]
        manager.check_conservation()


def test_an_agent_cannot_approve_a_trade_addressed_to_someone_else(island):
    """Identity is the hub's `from`, never anything the agent could put in the
    body — so there is no argument a model can make that makes it a2."""
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "free")
        for agent_id, state in manager.agents.items():
            wires[agent_id].manager_call(
                "produce", plan={g: state.alpha[i] for i, g in enumerate(manager.goods)})
        manager.open_trading()
        offered = wires["a1"].manager_call(
            "propose", seller="a2", give={"fish": 0.02}, want={"grain": 0.005})

        refused = wires["a3"].manager_call("approve", trade_id=offered["trade_id"])
        assert not refused["ok"] and "a2" in refused["error"]
        assert manager.trades[offered["trade_id"]].status == "pending"


def test_a_model_error_is_answered_not_raised(island):
    """A model will propose impossible things. Each must cost that agent a turn
    and nothing more — an exception here would take the whole island down."""
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "free")
        manager.open_trading()
        for bad in (
            {"seller": "a2", "give": {"fish": -5}, "want": {"grain": 1}},
            {"seller": "a2", "give": {"unobtainium": 1}, "want": {"grain": 1}},
            {"seller": "nobody", "give": {"fish": 0.01}, "want": {"grain": 1}},
            {"seller": "a1", "give": {"fish": 0.01}, "want": {"grain": 1}},
        ):
            reply = wires["a1"].manager_call("propose", **bad)
            assert reply["ok"] is False and reply["error"]


def test_the_floor_carries_messages_between_agents(island):
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "free")
        wires["a1"].post("I have salt and want cloth")
        heard = wires["a2"].read()
        assert [m["from"] for m in heard] == ["a1"]
        assert "salt" in heard[0]["text"]
        # ...and `listen` is a cursor, so nothing is heard twice.
        assert wires["a2"].read() == []


def test_a_finished_island_can_be_scored(island):
    """The scoring path, exercised without a model.

    A Tier 2 island costs real money to produce, so a run that completes and
    *then* falls over on the way to a number throws away the whole thing. That
    is not hypothetical — it happened, because this logic was written twice.
    There is now one scorer and this is its gate.
    """
    from barter.run import score

    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "free")
        for agent_id, state in manager.agents.items():
            wires[agent_id].manager_call(
                "produce", plan={g: state.alpha[i] for i, g in enumerate(manager.goods)})
        manager.open_trading()
        offered = wires["a1"].manager_call(
            "propose", seller="a2", give={"fish": 0.02}, want={"grain": 0.005})
        wires["a2"].manager_call("approve", trade_id=offered["trade_id"])
        manager.close()

    outcome = score(island, manager, arm="free", seed=4, messages=3)
    assert 0.0 < outcome.efficiency.lower <= outcome.efficiency.upper <= 1.0
    assert outcome.executed == 1
    assert outcome.messages == 3
    # Every field the Tier 2 report reads must survive being formatted.
    assert f"{outcome.worst_ratio:.2f}" and f"{outcome.exchange_efficiency.lower:.3f}"


def test_a_ruined_island_still_scores_rather_than_raising(island):
    """An island where somebody holds nothing is the outcome most worth seeing,
    so it has to survive scoring instead of blowing up the report."""
    from barter.run import score

    manager = Manager(island=island)
    for agent_id in manager.agents:
        manager.op_produce(agent_id, {"fish": 1.0})  # everyone makes only fish
    manager.open_trading()
    manager.close()

    outcome = score(island, manager, arm="silent", seed=4)
    assert outcome.efficiency.ruined == tuple(range(island.n_agents))
    assert outcome.efficiency.lower == 0.0


def test_the_silent_arm_has_no_channel_tool_at_all(island):
    """`silent`'s silence is the absence of a tool, not an instruction. A prompt
    that merely asked for silence would make this an obedience experiment."""
    silent = tool_names("silent", "a1")
    talkative = tool_names("free", "a1")
    assert not any(name.endswith(("__say", "__listen")) for name in silent)
    assert set(talkative) - set(silent) == {
        "mcp__island-a1__say", "mcp__island-a1__listen"}


def test_the_unguided_briefs_never_hand_over_a_convention(island):
    """`silent` and `free` explain mechanics and scoring only. If either
    mentioned prices, a numeraire or money, a convention appearing in `free`
    could not be said to have been invented."""
    manager = Manager(island=island)
    for arm in ("silent", "free"):
        brief = brief_for(island, manager, "a1", arm).lower()
        for leak in ("price", "numeraire", "money", "currency", "exchange rate",
                     "specialis", "specializ", "market"):
            assert leak not in brief, f"arm {arm} brief leaks {leak!r}"
    assert "say" in brief_for(island, manager, "a1", "free").lower()
    assert "say" not in brief_for(island, manager, "a1", "silent").lower()


def test_told_and_built_share_a_brief_byte_for_byte(island):
    """The load-bearing assertion of the instruction-vs-machinery comparison.

    `told` and `built` are supposed to differ in exactly one way: whether the
    convention has an affordance. A single extra sentence in `built`'s prompt
    pointing at the quote tools would turn the result back into a claim about
    wording, which is the claim the pair exists to rule out. The tools announce
    themselves through their own descriptions instead.
    """
    manager = Manager(island=island)
    told = brief_for(island, manager, "a1", "told")
    built = brief_for(island, manager, "a1", "built")
    assert told == built
    # ...and the convention really is in there, or the pair tests nothing.
    assert "unit of account" in told and "fish" in told
    # ...while the machinery is only in one of them.
    assert set(tool_names("built", "a1")) - set(tool_names("told", "a1")) == {
        "mcp__island-a1__post_quote", "mcp__island-a1__read_quotes"}


def test_the_convention_is_described_but_never_enforced(island):
    """A convention the manager policed would be a rule, not a convention.

    The brief says so explicitly, and it has to be true: the manager has no
    concept of a price and will settle any trade both sides agree to, at any
    rate at all.
    """
    manager = Manager(island=island)
    told = " ".join(brief_for(island, manager, "a1", "told").split())
    assert "enforces none of this" in told
    assert not any("price" in op for op in manager.OPS)

    with hub() as handle:
        _, wires = _wired(handle, manager, "built")
        for agent_id, state in manager.agents.items():
            wires[agent_id].manager_call(
                "produce", plan={g: state.alpha[i] for i, g in enumerate(manager.goods)})
        manager.open_trading()
        # A wildly off-convention trade settles exactly like any other.
        offered = wires["a1"].manager_call(
            "propose", seller="a2", give={"fish": 0.05}, want={"grain": 0.0001})
        assert wires["a2"].manager_call("approve", trade_id=offered["trade_id"])["ok"]


def test_the_quote_board_validates_and_aggregates(island):
    """The machinery, without a model. Storage is the easy half; the median is
    the step that turns scattered quotes into one number everyone computes the
    same way, which is where `told` leaves each agent on its own."""
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "built")

        assert wires["a1"].post_quote({"grain": 2.0, "cloth": 4.0})["posted"]["grain"] == 2.0
        assert wires["a2"].post_quote({"grain": 3.0, "cloth": 6.0})["posted"]["cloth"] == 6.0
        assert wires["a3"].post_quote({"grain": 10.0})["posted"]["grain"] == 10.0

        board = wires["a1"].read_quotes()
        assert board["traders_quoting"] == 3
        assert board["median_price"]["grain"] == 3.0          # median of 2, 3, 10
        assert board["median_price"]["cloth"] == 5.0          # median of 4, 6
        # Fish is 1 by definition, so two traders cannot quote on different
        # scales while appearing to agree.
        assert board["median_price"]["fish"] == 1.0
        assert all(q["fish"] == 1.0 for q in board["quotes"].values())


def test_bound_reports_your_own_distance_from_the_median(island):
    """`bound`'s first half: the comparison every `built` agent could have made
    and none did. A number you are shown is not a number you act on, so the
    board names the deviation instead of leaving it to be noticed."""
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "bound")
        wires["a1"].post_quote({"cloth": 10.0})
        wires["a2"].post_quote({"cloth": 2.0})
        wires["a3"].post_quote({"cloth": 1.0})

        board = wires["a1"].read_quotes()
        assert board["median_price"]["cloth"] == 2.0
        assert board["quote_is_live"] is True
        # a1 is quoting cloth at five times what everyone else is.
        assert board["your_deviation_from_median"]["cloth"] == 5.0
        # ...and fish is pinned, so it is never a source of apparent deviation.
        assert board["your_deviation_from_median"]["fish"] == 1.0


def test_bound_lets_a_quote_go_stale(island):
    """`bound`'s second half: staying on the board is something you keep doing.

    A quote nobody renews is not a price anybody is offering, and leaving it up
    lets a trader who stopped participating go on setting the median.
    """
    from barter.llm import QUOTE_TTL_TICKS

    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "bound")
        wires["a1"].post_quote({"cloth": 10.0})
        wires["a2"].post_quote({"cloth": 2.0})
        assert wires["a1"].read_quotes()["traders_quoting"] == 2

        for _ in range(QUOTE_TTL_TICKS):
            manager.advance()

        # a2 keeps its quote current; a1 does not and drops off.
        wires["a2"].post_quote({"cloth": 2.0})
        board = wires["a1"].read_quotes()
        assert board["traders_quoting"] == 1
        assert board["quote_is_live"] is False
        assert "expire" in board["notice"]
        assert board["median_price"]["cloth"] == 2.0


def test_built_is_untouched_by_the_bound_machinery(island):
    """`built` must behave exactly as it did when its island was run.

    The two share storage, and a stored shape that changed `built`'s replies
    would retroactively break the comparison against a result already recorded.
    `built` sees no deviation, no staleness, and no expiry.
    """
    from barter.llm import QUOTE_TTL_TICKS

    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "built")
        wires["a1"].post_quote({"cloth": 10.0})
        wires["a2"].post_quote({"cloth": 2.0})
        for _ in range(QUOTE_TTL_TICKS + 3):
            manager.advance()

        board = wires["a1"].read_quotes()
        assert set(board) == {"quotes", "median_price", "traders_quoting"}
        assert board["traders_quoting"] == 2      # nothing expires under `built`
        assert board["quotes"]["a1"]["cloth"] == 10.0


def test_every_quoting_arm_shares_one_brief(island):
    """`told`, `built` and `bound` differ only in what their tools do.

    Three arms now hang off this, so it is worth more than the pair: any
    difference between them is a difference in affordance, and a sentence
    drifting into one prompt would quietly turn the whole ladder into a study
    of wording.
    """
    manager = Manager(island=island)
    briefs = {arm: brief_for(island, manager, "a1", arm)
              for arm in ("told", "built", "bound")}
    assert len(set(briefs.values())) == 1
    assert "unit of account" in briefs["bound"]
    # The machinery differences live in the tool surface only.
    assert tool_names("built", "a1") == tool_names("bound", "a1")


def test_the_money_arms_differ_from_bound_by_exactly_one_clause(island):
    """`spend` flips the ladder's axis and should do so cleanly.

    Everything up to `bound` held the words fixed and varied machinery. The
    money clause cannot be machinery — it is a disposition — so `spend` holds
    the machinery fixed and varies the words instead. That is a legitimate but
    *different* comparison, and it is only interpretable if the difference is
    exactly the clause and nothing else has drifted.
    """
    from barter.llm import MONEY_BRIEF

    manager = Manager(island=island)
    bound = brief_for(island, manager, "a1", "bound")
    spend = brief_for(island, manager, "a1", "spend")
    assert spend == bound + MONEY_BRIEF
    # ...and `paid` varies machinery against `spend` with the words held fixed,
    # which restores the original axis one rung up.
    assert brief_for(island, manager, "a1", "paid") == spend
    assert set(tool_names("paid", "a1")) - set(tool_names("spend", "a1")) == {
        "mcp__island-a1__pay"}


def test_the_money_clause_asks_for_acceptance_not_just_settlement(island):
    """The load-bearing half is "accept past wanting it", not "settle in fish".

    A trader that takes the numeraire only up to what it wants to consume stops
    selling once full, and the market is back to barter. If that sentence ever
    goes missing the arm still looks like money and cannot work like it.
    """
    manager = Manager(island=island)
    spend = " ".join(brief_for(island, manager, "a1", "spend").split())
    assert "medium of exchange" in spend
    assert "Accept fish past the point of wanting it for itself" in spend


def test_pay_prices_at_the_median_and_still_needs_approval(island):
    """`pay` is a calculator. It does the arithmetic and commits nobody."""
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "paid")
        for agent_id, state in manager.agents.items():
            wires[agent_id].manager_call(
                "produce", plan={g: state.alpha[i] for i, g in enumerate(manager.goods)})
        manager.open_trading()

        for who, price in (("a1", 2.0), ("a2", 4.0), ("a3", 6.0)):
            wires[who].post_quote({"grain": price})

        out = wires["a1"].pay("a2", "grain", 0.01)
        assert out["median_used"] == 4.0
        # 0.01 grain at 4 fish/grain = 0.04 fish.
        assert out["offered"]["pay"] == pytest.approx(0.04)
        assert out["result"]["ok"]

        # The seller has not agreed to anything yet — this is the property that
        # keeps the tool a calculator rather than an exchange.
        trade_id = out["result"]["trade_id"]
        assert manager.trades[trade_id].status == "pending"
        assert wires["a2"].manager_call("approve", trade_id=trade_id)["ok"]
        assert manager.trades[trade_id].status == "executed"
        manager.check_conservation()


def test_pay_declines_when_there_is_no_price_to_pay(island):
    """No quotes, no median, no made-up number."""
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "paid")
        manager.open_trading()
        assert "error" in wires["a1"].pay("a2", "grain", 1.0)
        wires["a2"].post_quote({"grain": 2.0})
        for bad in (0, -1, "lots"):
            assert "error" in wires["a1"].pay("a2", "grain", bad)


def test_prose_prices_are_read_the_way_a_counterparty_would():
    """`told` keeps its prices in sentences, so comparing it to a board arm
    means reading them back out. That extraction is the measurement, so it is
    gated rather than trusted."""
    from barter.analysis import prices_from_prose

    said = [
        {"from": "a2",
         "text": "a2 prices: grain 1.01, cloth 0.21, timber 1.11, salt 1.47 per fish"},
        {"from": "a4", "text": "a4 prices: grain 0.5, cloth 1.8, timber 1.2, salt 0.5 per fish"},
        {"from": "a3", "text": "Looking for fish and salt - have grain and timber to trade."},
    ]
    prices = prices_from_prose(said)
    assert prices["a2"]["cloth"] == 0.21
    assert prices["a4"]["timber"] == 1.2
    # a3 named goods but no numbers, so it has no quote — and "3 of 4 agents
    # ever posted a price list" is a finding, not something to paper over.
    assert "a3" not in prices


def test_a_trade_offer_is_not_mistaken_for_a_price_list():
    """The extraction has to be biased toward missing a disagreement rather
    than inventing one, or a large measured spread means nothing."""
    from barter.analysis import prices_from_prose

    said = [
        {"from": "a1", "text": "I'll trade 0.15 timber for grain. Also offering 0.6 salt."},
        {"from": "a1", "text": "Updated: fish 1.0, grain 0.62, cloth 1.02, timber 0.48, salt 0.08"},
    ]
    prices = prices_from_prose(said)
    # The offer names two goods with numbers and is not a quote; the later
    # message is, and it is the one kept because agents revise.
    assert prices["a1"]["grain"] == 0.62
    assert prices["a1"]["cloth"] == 1.02


def test_price_spread_says_how_far_apart_the_traders_are():
    from barter.analysis import price_spread

    assert price_spread({
        "a1": {"cloth": 2.0, "salt": 1.0},
        "a2": {"cloth": 54.0, "salt": 1.0},
    }) == {"cloth": 27.0, "salt": 1.0}
    # A good only one trader named is neither agreement nor disagreement.
    assert price_spread({"a1": {"cloth": 2.0}, "a2": {"salt": 1.0}}) == {}


def test_a_board_arm_and_a_prose_arm_land_on_the_same_axis():
    """The whole point of the extraction: `told` has no board and `built` has
    one, and they still have to be comparable on price agreement."""
    from barter.analysis import summarise

    boardless = summarise({
        "arm": "told", "efficiency": [0.316, 0.320], "own_plan": [0.771, 0.771],
        "worst_ratio": 0.77, "summary": {"executed": 2, "proposed": 25},
        "said": [
            {"from": "a1", "text": "grain 1.0, cloth 0.2, timber 1.1, salt 1.5"},
            {"from": "a2", "text": "grain 1.0, cloth 6.0, timber 1.1, salt 1.5"},
        ],
    })
    boarded = summarise({
        "arm": "built", "efficiency": [0.368, 0.370], "own_plan": [0.898, 0.898],
        "worst_ratio": 0.93, "summary": {"executed": 3, "proposed": 16}, "said": [],
        "quote_board": {"a1": {"cloth": 2.0, "fish": 1.0},
                        "a2": {"cloth": 60.0, "fish": 1.0}},
    })
    assert boardless["price_source"] == "prose"
    assert boarded["price_source"] == "board"
    assert boardless["worst_spread"] == 30.0
    assert boarded["worst_spread"] == 30.0


def test_an_arm_that_never_quoted_reports_no_spread_rather_than_agreement():
    """`silent` and `free` must not read as 'perfectly agreed' just because
    there is nothing to disagree about."""
    from barter.analysis import summarise

    row = summarise({
        "arm": "free", "efficiency": [0.386, 0.389], "own_plan": [0.949, 0.949],
        "worst_ratio": 0.93, "summary": {"executed": 5, "proposed": 30},
        "said": [{"from": "a1", "text": "I need fish and have salt"}],
    })
    assert row["worst_spread"] is None
    assert row["traders_quoting"] == 0


def test_a_bad_quote_is_answered_not_stored(island):
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "built")
        for bad in ({}, {"grain": -1}, {"grain": "cheap"}, {"unobtainium": 2}, {"grain": 0}):
            assert "error" in wires["a1"].post_quote(bad)
        assert wires["a1"].read_quotes()["traders_quoting"] == 0
