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
from barter.llm import Wire, brief_for, telling_for, tool_names  # noqa: E402
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
                       telling=telling_for(arm), floor_channel=f"barter/{run}/floor",
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


def _scripted_island(island, *, discovery=2, rounds=3, plan=None):
    """Run the real order of play with a scripted stand-in for the model.

    No SDK, no network, milliseconds. This exists because both flow errors so
    far — production committed before anyone spoke, and one turn per agent for
    both proposing and answering — were found by paying for a run and reading
    the wreckage. A loop nobody can exercise is a loop nobody exercises.
    """
    import random

    from barter.flow import Budgets, play

    manager = Manager(island=island, phase="discovery")
    log = []

    async def take_turn(agent_id, *, round_no, label, note, budget):
        log.append({"round": round_no, "label": label, "agent": agent_id,
                    "budget": budget, "phase": manager.phase})
        if label == "produce":
            manager.dispatch(agent_id, {"op": "produce",
                                        "plan": plan or {"fish": 0.4, "grain": 0.6}})
        elif label == "offer":
            manager.dispatch(agent_id, {
                "op": "propose", "seller": next(a for a in manager.agents if a != agent_id),
                "give": {"fish": 0.01}, "want": {"grain": 0.005}})
        elif label == "settle":
            waiting = manager.dispatch(agent_id, {"op": "pending"})
            for trade in waiting.get("awaiting_your_approval", []):
                manager.dispatch(agent_id, {"op": "approve", "trade_id": trade["id"]})
        return f"{agent_id} did {label}"

    import anyio
    from barter.analysis import snapshot
    played = anyio.run(lambda: play(
        manager, take_turn, discovery=discovery, rounds=rounds,
        rng=random.Random(0), budgets=Budgets(),
        on_round=lambda round_no, label: (
            snapshot(island, manager, None, round_no=round_no, label=label)
            if label in ("talk", "produce", "settle") else None)))
    return manager, played, log


def test_the_order_of_play_deliberates_then_produces_then_trades(island):
    manager, played, log = _scripted_island(island, discovery=2, rounds=3)

    labels = [entry["label"] for entry in log]
    n = island.n_agents
    # Two talk rounds, one produce round, then three trade rounds of two passes.
    assert labels[:2 * n] == ["talk"] * (2 * n)
    assert labels[2 * n:3 * n] == ["produce"] * n
    assert labels[3 * n:] == (["offer"] * n + ["settle"] * n) * 3

    # ...and during every talk turn the manager really did refuse everything.
    assert {e["phase"] for e in log if e["label"] == "talk"} == {"discovery"}
    assert manager.phase == "closed"
    manager.check_conservation()


def test_nothing_can_be_committed_before_the_talking_is_done(island):
    """The point of the discovery phase, asserted at the flow level rather than
    trusted to the prompt: an eager agent that tries to produce during talk is
    refused by the manager, not merely discouraged."""
    manager = Manager(island=island, phase="discovery")
    assert manager.dispatch("a1", {"op": "produce", "plan": {"fish": 1.0}})["ok"] is False
    assert manager.dispatch("a1", {"op": "propose", "seller": "a2",
                                   "give": {"fish": 1.0},
                                   "want": {"grain": 1.0}})["ok"] is False


def test_every_offer_gets_a_settle_pass_in_its_own_round(island):
    """The second flow error: with one turn per agent, roughly half of all
    offers could not be answered until the following round, and a three-tick
    expiry gave a proposal one or two real chances at being seen. Two passes
    mean every offer is seen by its seller in the round it was made."""
    manager, played, _ = _scripted_island(island, discovery=1, rounds=2)
    assert played.expired_unseen(manager) == 0
    assert any(t.status == "executed" for t in manager.trades.values())


def _notes_for(island, rolling, **switches):
    """Every note the flow shows an agent, for one labour mode."""
    import random

    import anyio
    from barter.flow import Notes, play

    manager = Manager(island=island, phase="discovery",
                      labour_per_round=0.5 if rolling else 1.0, rolling=rolling)
    notes = []

    async def take_turn(agent_id, *, round_no, label, note, budget):
        notes.append(note)
        if label == "produce":
            manager.dispatch(agent_id, {"op": "produce", "plan": {"fish": 1.0}})
        return ""

    setup = Notes(rolling=rolling,
                  labour=lambda who: max(0.0, 1.0 - manager.agents[who].spent),
                  **switches)
    anyio.run(lambda: play(manager, take_turn, discovery=1, rounds=1,
                           rng=random.Random(0), notes=setup))
    return " ".join(notes).lower()


def test_the_notes_never_promise_the_wrong_labour_rule(island):
    """The two labour modes are different games, and an agent told the wrong one
    plans for the wrong game. `once` must never invite a second `produce`, and
    `rolling` must never call the decision final."""
    once = _notes_for(island, rolling=False)
    assert "labour once" in once and "instalment" not in once

    rolling = _notes_for(island, rolling=True)
    assert "instalment" in rolling and "labour once" not in rolling
    # ...and rolling has to actually tell them the option is still open later.
    assert "produce` once more" in rolling


def test_every_sentence_in_the_turn_note_can_be_switched_off(island):
    """A sentence that is always present is a sentence nobody can attribute.

    Rolling labour was measured once with the remaining balance reachable only
    through `my_state`, which made "did responsiveness help" partly a question
    about whether agents bothered to look it up. Telling them outright is the
    obvious fix and it is also a *second* change riding along with the first —
    so it is its own switch, and so is the horizon it sits next to.
    """
    full = _notes_for(island, rolling=True, horizon=True, labour_left=True)
    assert "labour still to spend" in full or "fully committed" in full
    assert "round(s) remain" in full

    without_labour = _notes_for(island, rolling=True, labour_left=False)
    assert "still to spend" not in without_labour
    assert "round(s) remain" in without_labour

    without_horizon = _notes_for(island, rolling=True, horizon=False)
    assert "round(s) remain" not in without_horizon
    assert "still to spend" in without_horizon or "fully committed" in without_horizon

    # The balance is a real number off the manager, not a fixed phrase: an agent
    # that has worked one instalment of two has half a unit left, and a note
    # that said "1.00" all game would be worse than saying nothing.
    assert "0.50" in full


def test_each_phase_gets_its_own_turn_budget(island):
    """Budgets differ by phase because the phases are not the same size of job,
    and a trading budget spent on every phase is most of what made the previous
    flow expensive."""
    from barter.flow import Budgets

    _, _, log = _scripted_island(island, discovery=1, rounds=1)
    got = {e["label"]: e["budget"] for e in log}
    assert got == {"talk": Budgets().talk, "produce": Budgets().produce,
                   "offer": Budgets().offer, "settle": Budgets().settle}


def test_a_flow_artefact_is_not_counted_as_a_refusal(island):
    """`expired_unseen` is the guard on this experiment's own honesty: an offer
    the seller never had a chance to look at must not be reported next to one it
    considered and declined."""
    from barter.flow import Played

    manager = Manager(island=island, phase="discovery")
    manager.open_production()
    for agent_id, state in manager.agents.items():
        manager.op_produce(agent_id, {g: state.alpha[i]
                                      for i, g in enumerate(manager.goods)})
    manager.open_trading()
    trade = manager.op_propose("a1", "a2", {"fish": 0.01}, {"grain": 0.005})
    for _ in range(4):
        manager.advance()
    assert manager.trades[trade["trade_id"]].status == "expired"

    never_looked = Played()
    assert never_looked.expired_unseen(manager) == 1
    looked = Played(seen_by={trade["trade_id"]: {"a2"}})
    assert looked.expired_unseen(manager) == 0


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


def test_specialisation_reads_one_when_labour_follows_prices(island):
    """The production half of convergence, and the half no arm has moved.

    Revenue earned by the labour spent over the most it could have earned at the
    prices agents currently hold. It is measured at *believed* prices on purpose:
    the question is whether agents act on what they agreed, not whether what
    they agreed was right.
    """
    from barter.analysis import specialisation

    goods = tuple(draw_island(2, 5, seed=1).good_ids())
    two = draw_island(2, 5, seed=1)
    prices = {g: 1.0 for g in goods}
    # With every price at 1, the best good is simply the highest capacity.
    best = [max(range(5), key=lambda g: two.capacity[i][g]) for i in range(2)]
    all_in = [[1.0 if g == best[i] else 0.0 for g in range(5)] for i in range(2)]
    assert specialisation(two, all_in, prices, goods) == pytest.approx(1.0)

    # Autarky spreads labour by taste and scores well short of it.
    spread = [list(two.alpha[i]) for i in range(2)]
    assert specialisation(two, spread, prices, goods) < 0.95
    # Nobody has worked yet: undefined, not zero.
    assert specialisation(two, [[0.0] * 5, [0.0] * 5], prices, goods) is None


def test_concentration_is_a_price_free_check_on_specialisation():
    """Reported alongside because `specialisation` is measured at prices agents
    may simply have wrong. This one says whether they committed to anything."""
    from barter.analysis import concentration

    assert concentration([[0.2] * 5]) == pytest.approx(0.2)          # even = 1/k
    assert concentration([[1.0, 0, 0, 0, 0]]) == pytest.approx(1.0)  # all in
    assert concentration([[0.0] * 5]) is None                        # no labour


def test_a_snapshot_reports_not_yet_scoreable_rather_than_zero(island):
    """Early rounds have agents holding none of something, which is Cobb-Douglas
    zero. A trajectory that plotted that as 0.0 would show a dramatic climb that
    is really just the goods arriving."""
    from barter.analysis import snapshot

    manager = Manager(island=island, phase="discovery")
    row = snapshot(island, manager, None, round_no=1, label="talk")
    assert row["efficiency"] is None
    assert row["holding_nothing"] == island.n_agents
    assert row["labour_spent"] == 0.0

    manager.open_production()
    for agent_id, state in manager.agents.items():
        manager.op_produce(agent_id, {g: state.alpha[i]
                                      for i, g in enumerate(manager.goods)})
    scored = snapshot(island, manager, None, round_no=2, label="produce")
    assert 0.0 < scored["efficiency"] < 1.0
    assert scored["holding_nothing"] == 0
    assert scored["labour_spent"] == pytest.approx(1.0)


def test_a_board_is_collapsed_to_one_vector_before_specialisation(island):
    """`specialisation` needs a single price vector; a board is one per trader.

    Without the collapse it looked goods up in a dict keyed by agent, found
    none, and returned None every round — an empty column in the trajectory and
    no error anywhere. That is the failure mode this whole experiment keeps
    hitting: a paid run that reports nothing about the thing it measures.
    """
    from barter.analysis import consensus, snapshot, specialisation

    board = {"a1": {"fish": 1.0, "cloth": 15.0}, "a2": {"fish": 1.0, "cloth": 8.0}}
    assert consensus(board) == {"fish": 1.0, "cloth": 11.5}
    # A flat vector passes through untouched.
    assert consensus({"fish": 1.0, "cloth": 3.0}) == {"fish": 1.0, "cloth": 3.0}
    assert consensus({}) == {}

    goods = tuple(island.good_ids())
    shares = [[1.0] + [0.0] * 4 for _ in range(island.n_agents)]
    assert specialisation(island, shares, board, goods) is None       # unusable shape
    assert specialisation(island, shares, consensus(board), goods) is not None

    manager = Manager(island=island)
    for agent_id, state in manager.agents.items():
        manager.op_produce(agent_id, {g: state.alpha[i]
                                      for i, g in enumerate(manager.goods)})
    row = snapshot(island, manager, board, round_no=1, label="produce")
    assert row["specialisation"] is not None


def test_a_snapshot_takes_prices_from_a_board_or_from_one_vector(island):
    """Boards are per-agent and a settled convention is one vector. Both have to
    land on the same `price_agreement` axis or the trajectory is not a line."""
    from barter.analysis import snapshot

    manager = Manager(island=island)
    board = snapshot(island, manager, {"a1": {"fish": 1.0, "cloth": 2.0},
                                       "a2": {"fish": 1.0, "cloth": 8.0}},
                     round_no=1)
    assert board["price_agreement"] == pytest.approx(4.0)
    agreed = snapshot(island, manager, {"fish": 1.0, "cloth": 2.0}, round_no=1)
    assert agreed["price_agreement"] == pytest.approx(1.0)


def test_the_trajectory_is_recorded_once_per_round(island):
    """One row per round, after a pass that could have changed something —
    snapshotting mid-round would read a half-applied state."""
    manager, played, _ = _scripted_island(island, discovery=2, rounds=3)
    labels = [row["label"] for row in played.trajectory]
    assert labels == ["talk", "talk", "produce"] + ["settle"] * 3
    assert [row["round"] for row in played.trajectory] == [1, 2, 3, 4, 5, 6]
    # Labour is fully committed at the produce round and stays put under `once`.
    assert played.trajectory[2]["labour_spent"] == pytest.approx(1.0)


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


# --- the switches -----------------------------------------------------------
#
# The named arms came first and were the wrong shape: each rung of the ladder
# added two things at once, so a rung that moved could never say which half
# moved it. These gates are about the fix — that every thing told to an agent is
# separable, that the presets still reproduce the runs already banked, and that
# a combination nobody could interpret is refused rather than quietly run.


def test_every_named_arm_is_a_combination_of_switches_and_nothing_more(island):
    """The ladder must survive the refactor exactly, or the banked runs are lost.

    Six paid islands were run under these names. If a preset now means something
    slightly different, every one of those records is a measurement of a setup
    that no longer exists — so the presets are pinned here switch by switch.
    """
    from barter.llm import ARMS, PRESETS, telling_for

    assert PRESETS["silent"].switches() == (
        "ruin_warning", "horizon", "labour_left", "own_value", "own_score")
    assert "channel" in PRESETS["free"].switches()
    assert not PRESETS["free"].numeraire
    assert PRESETS["told"].numeraire and not PRESETS["told"].board
    assert PRESETS["built"].board and PRESETS["built"].median
    assert not (PRESETS["built"].deviation or PRESETS["built"].expiry)
    assert PRESETS["bound"].deviation and PRESETS["bound"].expiry
    assert PRESETS["spend"].money and not PRESETS["spend"].pay_tool
    assert PRESETS["paid"].pay_tool

    # Each rung adds to the one below it and takes nothing away. A ladder whose
    # rungs subtracted would not be a ladder.
    for lower, upper in zip(ARMS, ARMS[1:], strict=False):
        below, above = set(telling_for(lower).switches()), set(telling_for(upper).switches())
        assert below <= above, f"{upper} drops {below - above} from {lower}"


def test_a_combination_nobody_could_interpret_is_refused(island):
    """Not tidiness. A board is denominated in fish and pins fish at 1, so a
    board with no numeraire convention quotes on a scale nobody was told about;
    a `pay` tool with no median has no rate to price at. Either would run
    happily and produce a number that means nothing, which is worse than a
    crash."""
    from barter.llm import Telling

    for bad in ({"board": True}, {"median": True}, {"deviation": True},
                {"expiry": True}, {"money": True}, {"pay_tool": True},
                {"numeraire": True}):
        with pytest.raises(ValueError):
            Telling(**bad)

    # ...and the deviation report specifically needs a median under it, not just
    # a board, because it is a ratio to one.
    with pytest.raises(ValueError):
        Telling(channel=True, numeraire=True, board=True, deviation=True)


def test_the_bundle_bound_shipped_as_one_step_can_be_run_apart(island):
    """`bound` added a deviation report *and* quote expiry together, and its
    result could only ever be attributed to the pair. This is the whole point of
    the refactor: either half can now be run alone."""
    from barter.llm import compose

    deviation_only = compose("bound", off=["expiry"])
    assert deviation_only.deviation and not deviation_only.expiry
    stale_only = compose("built", on=["expiry"])
    assert stale_only.expiry and not stale_only.deviation

    manager = Manager(island=island)
    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run="sw")
        service.claim()

        def wire_for(telling):
            return Wire(agent_id="a1", client=handle.client("a1"), service=service,
                        telling=telling, floor_channel="barter/sw/floor",
                        quote_prefix="barter/sw/quote/", goods=tuple(manager.goods))

        wire_for(deviation_only).post_quote({"grain": 2.0})
        assert "your_deviation_from_median" in wire_for(deviation_only).read_quotes()
        assert "quote_is_live" not in wire_for(deviation_only).read_quotes()

        stale = wire_for(stale_only).read_quotes()
        assert "quote_is_live" in stale
        assert "your_deviation_from_median" not in stale

        # And aggregation off means no median at all — the switch `built` bundled
        # with mere storage.
        bare = wire_for(compose("built", off=["median"])).read_quotes()
        assert "median_price" not in bare and bare["traders_quoting"] == 1


def test_switching_an_affordance_never_moves_a_word_of_the_prompt(island):
    """The claim the whole ladder rests on, now stated over every tool switch
    rather than over one pair of arms. If turning the median on changed the
    prompt, "machinery beats instruction" would be a claim about wording."""
    from barter.llm import compose

    manager = Manager(island=island)
    base = brief_for(island, manager, "a1", "paid")
    for switch in ("board", "median", "deviation", "expiry", "pay_tool", "channel"):
        flipped = compose("paid", off=[switch])
        if switch == "channel":
            continue  # `channel` is an affordance *and* a paragraph; see below
        assert brief_for(island, manager, "a1", flipped) == base, switch


def test_the_word_switches_each_remove_exactly_their_own_paragraph(island):
    from barter.llm import CHANNEL_BRIEF, MONEY_BRIEF, NUMERAIRE_BRIEF, RUIN_CLAUSE, compose

    manager = Manager(island=island)
    full = brief_for(island, manager, "a1", "paid")
    assert all(part in full for part in (CHANNEL_BRIEF, NUMERAIRE_BRIEF, MONEY_BRIEF))

    no_money = compose("paid", off=["money", "pay_tool"])
    assert MONEY_BRIEF not in brief_for(island, manager, "a1", no_money)
    assert CHANNEL_BRIEF not in brief_for(island, manager, "a1", compose("paid", off=["channel"]))

    # The ruin clause is an *inference from* the scoring rule that we make on the
    # agent's behalf. Switching it off must leave the rule itself standing.
    quiet = brief_for(island, manager, "a1", compose("paid", off=["ruin_warning"]))
    assert RUIN_CLAUSE not in quiet
    assert "product of your final holdings raised to your" in quiet


def test_a_rolling_island_is_never_told_it_spends_its_labour_once(island):
    """A live bug rather than a hypothetical: the brief said "You spend it once,
    at the start" while the manager was accepting instalments, so every rolling
    agent read a sentence its own tools contradicted."""
    from barter.llm import compose

    manager = Manager(island=island)
    once = brief_for(island, manager, "a1", "told")
    rolling = brief_for(island, manager, "a1", compose("told", on=["rolling"]))
    assert "spend it once, at the start" in once
    assert "spend it once, at the start" not in rolling
    assert "instalments" in rolling
    assert "not carried over" in rolling
    # The rest of the world is unchanged — this is a switch, not a rewrite.
    assert rolling.count("propose_trade") == once.count("propose_trade")


def test_switching_a_switch_off_takes_its_dependents_with_it(island):
    """`--without board` has to mean "no board", not a crash naming three more
    switches to spell out. The cascade is safe to make implicit only because
    the run record stores the *resolved* switch set, so an island always
    reports what it actually had rather than what was asked for."""
    from barter.llm import compose

    bare = compose("paid", off=["board"])
    assert not any((bare.board, bare.median, bare.deviation, bare.expiry, bare.pay_tool))
    # Words are not affordances: dropping the machinery leaves the convention
    # stated, which is exactly the `told` rung.
    assert bare.numeraire and bare.money

    # Asking for a switch and removing what it stands on is a contradiction, not
    # a cascade, and has to be said out loud.
    with pytest.raises(ValueError, match="was switched on"):
        compose("built", on=["pay_tool"], off=["money"])


def test_a_rolling_tier_two_island_can_work_in_its_very_first_trading_round(island):
    """The same tick collision as Tier 1's, at the flow level.

    An agent that calls `produce` in the first trading round is asking to work
    on the tick its opening instalment already used, and the manager refuses two
    commitments in one tick. The flow advances the clock across the boundary; if
    it stops doing so the run still completes and every agent silently loses a
    slice of labour, which reads in the results as agents declining to work.
    """
    import random

    import anyio
    from barter.flow import Notes, play

    rounds = 3
    manager = Manager(island=island, phase="discovery", rolling=True,
                      labour_per_round=1.0 / (1 + rounds))
    refused = []

    async def take_turn(agent_id, *, round_no, label, note, budget):
        if label in ("produce", "offer"):
            reply = manager.dispatch(agent_id, {"op": "produce", "plan": {"fish": 1.0}})
            if not reply.get("ok"):
                refused.append((round_no, label, agent_id, reply.get("error")))
        return ""

    notes = Notes(rolling=True,
                  labour=lambda who: max(0.0, 1.0 - manager.agents[who].spent))
    anyio.run(lambda: play(manager, take_turn, discovery=1, rounds=rounds,
                           rng=random.Random(0), notes=notes))

    assert refused == [], refused
    for state in manager.agents.values():
        assert state.spent == pytest.approx(1.0, abs=1e-9)
    manager.check_conservation()


def test_the_calculators_in_my_state_are_switches_too(island):
    """`my_state` hands over two things that are not facts about the world.

    `value_per_unit` is the marginal rate an agent would otherwise have to work
    out from its own exponents and holdings — most of what a trader needs to
    price a swap, computed for it. `utility` is its live score. Both were handed
    over silently in every arm, which made each prompt look more austere than
    the island actually was. The filtering happens in the tool surface and not
    in the manager: the manager is the pure state machine both tiers share and
    must not know what an island was told.
    """
    from barter.llm import compose

    manager = Manager(island=island)
    with hub() as handle:
        service = ManagerService(handle.client("manager"), manager, run="calc")
        service.claim()

        def wire_for(telling):
            return Wire(agent_id="a1", client=handle.client("a1"), service=service,
                        telling=telling, floor_channel="barter/calc/floor",
                        quote_prefix="barter/calc/quote/", goods=tuple(manager.goods))

        full = wire_for(telling_for("free")).state()
        assert "value_per_unit" in full and "utility" in full

        quiet = wire_for(compose("free", off=["own_value", "own_score"])).state()
        assert "value_per_unit" not in quiet and "utility" not in quiet
        # ...and nothing else went with them. Capacities, tastes and holdings are
        # the world, not a hint about it.
        assert set(full) - set(quiet) == {"value_per_unit", "utility"}

        # The manager itself is untouched — it answered in full both times.
        assert "value_per_unit" in manager.op_state("a1")
