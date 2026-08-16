"""Gates for the model-facing side of the barter experiment.

No model is called here and no network is touched. What is under test is the
wiring a model would act through: that a tool call reaches the manager over the
hub, that identity is the hub's and not the model's to claim, and that arm A
genuinely cannot talk rather than merely being asked not to.

That last one is the whole reason this file exists. If arm A's silence were a
line in a prompt, the experiment would be measuring instruction-following and
reporting it as a result about communication.
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
                       arm=arm, floor_channel=f"barter/{run}/floor")
        for agent_id in manager.agents
    }
    return service, wires


def test_a_tool_call_reaches_the_manager_and_comes_back(island):
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "B")
        state = wires["a1"].manager_call("state")
        assert state["ok"] and state["you"] == "a1"
        assert set(state["capacity"]) == set(manager.goods)


def test_an_agent_only_ever_sees_its_own_state(island):
    """Everything an agent knows about anyone else it had to be told. That is
    what puts the channel under test rather than the prompt."""
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "B")
        blob = repr(wires["a1"].manager_call("state"))
        assert "a2" not in blob and "a3" not in blob


def test_the_two_phase_trade_works_through_the_tool_surface(island):
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "B")
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
        _, wires = _wired(handle, manager, "B")
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
        _, wires = _wired(handle, manager, "B")
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
        _, wires = _wired(handle, manager, "B")
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
        _, wires = _wired(handle, manager, "B")
        for agent_id, state in manager.agents.items():
            wires[agent_id].manager_call(
                "produce", plan={g: state.alpha[i] for i, g in enumerate(manager.goods)})
        manager.open_trading()
        offered = wires["a1"].manager_call(
            "propose", seller="a2", give={"fish": 0.02}, want={"grain": 0.005})
        wires["a2"].manager_call("approve", trade_id=offered["trade_id"])
        manager.close()

    outcome = score(island, manager, arm="B", seed=4, messages=3)
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

    outcome = score(island, manager, arm="A", seed=4)
    assert outcome.efficiency.ruined == tuple(range(island.n_agents))
    assert outcome.efficiency.lower == 0.0


def test_the_silent_arm_has_no_channel_tool_at_all(island):
    """Arm A's silence is the absence of a tool, not an instruction. A prompt
    that merely asked for silence would make this an obedience experiment."""
    silent = tool_names("A", "a1")
    talkative = tool_names("B", "a1")
    assert not any(name.endswith(("__say", "__listen")) for name in silent)
    assert set(talkative) - set(silent) == {
        "mcp__island-a1__say", "mcp__island-a1__listen"}


def test_the_brief_never_hands_over_a_convention(island):
    """The prompt explains mechanics and scoring. If it mentioned prices, a
    numeraire or money, arm B could not be said to have invented anything."""
    manager = Manager(island=island)
    for arm in ("A", "B"):
        brief = brief_for(island, manager, "a1", arm).lower()
        for leak in ("price", "numeraire", "money", "currency", "exchange rate",
                     "specialis", "specializ", "market"):
            assert leak not in brief, f"arm {arm} brief leaks {leak!r}"
    assert "say" in brief_for(island, manager, "a1", "B").lower()
    assert "say" not in brief_for(island, manager, "a1", "A").lower()
