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
from barter.llm import ARMS as ARMS_ALL  # noqa: E402
from barter.llm import Wire, brief_for, telling_for, tool_names  # noqa: E402
from barter.manager import (  # noqa: E402
    LEVEL_PRODUCE,
    LEVEL_SETTLE,
    BoardService,
    Manager,
    ManagerService,
)

from switchboard.testing import hub  # noqa: E402


@pytest.fixture
def island():
    return draw_island(3, 5, seed=4)


def _flat(text: str) -> str:
    """Prose with its line breaks collapsed.

    Assertions about what an agent is told should not be hostage to where a
    paragraph happens to wrap — a reflow is not a change in what was said, and a
    gate that fails on one is a gate people learn to edit rather than read.
    """
    return " ".join(text.split())


def _wire(handle, manager, agent_id, telling, run="llm", board=None):
    """One agent's tool surface, wired to the board rather than to the manager.

    Nothing sends the manager a request any more, so a test that exercises a
    tool has to have something sweeping the board. These tests are
    single-threaded, so the sweep happens from inside the wait — see
    `Wire.sweep_while_waiting`, and `test_barter.py` for the threaded sweeper a
    real run uses.
    """
    return Wire(agent_id=agent_id, client=handle.client(agent_id),
                telling=telling, floor_channel=f"barter/{run}/floor",
                order_prefix=f"barter/{run}/order/",
                result_prefix=f"barter/{run}/result/",
                clock_key=f"barter/{run}/clock",
                quote_prefix=f"barter/{run}/quote/", goods=tuple(manager.goods),
                sweep_while_waiting=board.sweep if board is not None else None,
                poll_every=0.0, poll_for=5.0)


def _wired(handle, manager, arm, run="llm"):
    service = ManagerService(handle.client("manager"), manager, run=run)
    service.claim()
    board = BoardService(handle.client("manager"), manager, run=run)
    wires = {agent_id: _wire(handle, manager, agent_id, telling_for(arm),
                             run=run, board=board)
             for agent_id in manager.agents}
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
        manager.open(LEVEL_SETTLE)

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
        manager.open(LEVEL_SETTLE)
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
        manager.open(LEVEL_SETTLE)
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
        manager.open(LEVEL_SETTLE)
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
    manager.open(LEVEL_SETTLE)
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
        "mcp__island-a1__say", "mcp__island-a1__listen",
        "mcp__island-a1__history"}


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
        manager.open(LEVEL_SETTLE)
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
        # The clock a quote is stamped against is the one on the board, not the
        # one in this process: agents read it there and the sweep puts it there.
        BoardService(handle.client("manager"), manager, run="llm").sweep()

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


def _scripted_island(island, *, rounds=3, plan=None, window=0.02):
    """Run the real order of play with a scripted stand-in for the model.

    No SDK, no network, milliseconds. This exists because both flow errors so
    far — production committed before anyone spoke, and one turn per agent for
    both proposing and answering — were found by paying for a run and reading
    the wreckage. A loop nobody can exercise is a loop nobody exercises.
    """
    import random

    from barter.flow import Budgets, play

    manager = Manager(island=island)
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
        await anyio.sleep(window / 4)
        return f"{agent_id} did {label}"

    import anyio
    from barter.analysis import snapshot
    played = anyio.run(lambda: play(
        manager, take_turn, rounds=rounds, window=window,
        rng=random.Random(0), budgets=Budgets(),
        on_round=lambda round_no, label: snapshot(
            island, manager, None, round_no=round_no, label=label)))
    return manager, played, log


def test_a_round_opens_what_it_opens_on_the_clock(island):
    """The round is a wall clock, not a sequence of turns.

    Every agent runs for the whole of it and what the manager accepts opens as
    it goes: produce from the start, propose after a window, approve after two.
    Each waits on the one before it having had time — you cannot offer what
    nobody has made, and you cannot settle before offers exist.
    """
    import random

    import anyio
    from barter.flow import play

    manager = Manager(island=island)
    seen: list[tuple[int, str]] = []
    refused: list[str] = []

    async def take_turn(agent_id, *, round_no, label, note, budget):
        seen.append((round_no, label))
        # Try the thing that only opens later, every single turn.
        reply = manager.dispatch(agent_id, {
            "op": "propose", "seller": next(a for a in manager.agents if a != agent_id),
            "give": {"fish": 0.001}, "want": {"grain": 0.001}})
        if not reply.get("ok"):
            refused.append(label)
        await anyio.sleep(0.01)
        if label == "produce":
            manager.dispatch(agent_id, {"op": "produce", "plan": {"fish": 1.0}})
        return ""

    anyio.run(lambda: play(manager, take_turn, rounds=1,
                           rng=random.Random(0), window=0.05))

    windows = {label for _, label in seen}
    assert windows == {"produce", "offer", "settle"}, windows
    # Proposing was refused in the first window and only there.
    assert "produce" in refused
    assert "offer" not in refused and "settle" not in refused
    assert manager.phase == "closed"
    manager.check_conservation()


def test_a_missed_window_is_the_clock_not_the_agent(island):
    """An agent still thinking when a window closes does not hold it open.

    That is the point of time-boxing, and it needs to stay distinguishable from
    an agent proposing something impossible — one is the harness applying
    pressure and the other is a result about the agent, and a run that counted
    them together would credit the clock to the agents' judgement. So a window
    an agent never got into is counted on its own axis and nowhere else.
    """
    import random

    import anyio
    from barter.flow import play

    manager = Manager(island=island)

    async def take_turn(agent_id, *, round_no, label, note, budget):
        # Slower than the whole round, so each agent gets into the first
        # window and sleeps through both of the others.
        await anyio.sleep(0.1)
        return ""

    played = anyio.run(lambda: play(manager, take_turn, rounds=1,
                                    rng=random.Random(0), window=0.02))
    # Three windows each, one reached, so two missed apiece.
    assert played.missed == island.n_agents * 2
    # Nobody worked, so close() hands every agent its autarky plan rather than
    # leaving it starved -- "never engaged" and "wiped out" are different
    # failures and score identically without this.
    assert all(state.spent > 0 for state in manager.agents.values())
    manager.check_conservation()


def test_every_offer_gets_a_settle_pass_in_its_own_round(island):
    """The second flow error: with one turn per agent, roughly half of all
    offers could not be answered until the following round, and a three-tick
    expiry gave a proposal one or two real chances at being seen. The settle
    window means every offer is seen by its seller in the round it was made."""
    manager, played, _ = _scripted_island(island, rounds=2)
    assert played.expired_unseen(manager) == 0
    assert any(t.status == "executed" for t in manager.trades.values())


def _notes_for(island, rolling, **switches):
    """Every note the flow shows an agent, for one labour mode."""
    import random

    import anyio
    from barter.flow import Notes, play

    manager = Manager(island=island,
                      labour_per_round=0.5 if rolling else 1.0, rolling=rolling)
    notes = []

    async def take_turn(agent_id, *, round_no, label, note, budget):
        notes.append(note)
        if label == "produce":
            manager.dispatch(agent_id, {"op": "produce", "plan": {"fish": 1.0}})
        await anyio.sleep(0.005)
        return ""

    setup = Notes(rolling=rolling,
                  labour=lambda who: max(0.0, 1.0 - manager.agents[who].spent),
                  **switches)
    anyio.run(lambda: play(manager, take_turn, rounds=2, rng=random.Random(0),
                           window=0.02, notes=setup))
    return " ".join(notes).lower()


def test_the_notes_never_promise_the_wrong_labour_rule(island):
    """The two labour modes are different games, and an agent told the wrong one
    plans for the wrong game. `once` must never invite a second `produce`, and
    `rolling` must never call the decision final."""
    once = _notes_for(island, rolling=False)
    assert "instalment" not in once
    assert "there is no second call" in once

    rolling = _notes_for(island, rolling=True)
    assert "instalment" in rolling
    assert "no second call" not in rolling
    # Use-it-or-lose-it has to be said out loud, because it is the rule that
    # costs an agent something it cannot get back. A run once lost a third of
    # its labour to share vectors that summed short, with nothing in the note
    # saying the remainder would not wait.
    assert "not carried over" in rolling and "sum to 1" in rolling


def test_every_sentence_in_the_turn_note_can_be_switched_off(island):
    """A sentence that is always present is a sentence nobody can attribute.

    Rolling labour was measured once with the remaining balance reachable only
    through `my_state`, which made "did responsiveness help" partly a question
    about whether agents bothered to look it up. Telling them outright is the
    obvious fix and it is also a *second* change riding along with the first —
    so it is its own switch, and so is the horizon it sits next to, and so is
    the rule they both describe.

    What cannot be switched off is which window is open. That is not a telling
    about the world, it is the world: an agent that does not know approving has
    opened cannot approve, and an arm without it would be measuring the harness.
    """
    full = _notes_for(island, rolling=True, horizon=True, labour_left=True)
    assert "labour still to spend" in full or "fully committed" in full
    assert "round(s) remain" in full
    assert "not carried over" in full

    without_labour = _notes_for(island, rolling=True, labour_left=False)
    assert "still to spend" not in without_labour
    assert "round(s) remain" in without_labour

    without_horizon = _notes_for(island, rolling=True, horizon=False)
    assert "round(s) remain" not in without_horizon
    assert "still to spend" in without_horizon or "fully committed" in without_horizon

    without_rule = _notes_for(island, rolling=True, labour_rule=False)
    assert "not carried over" not in without_rule
    assert "round(s) remain" in without_rule

    # The window is structural and stays in all three.
    for note in (full, without_labour, without_horizon, without_rule):
        assert "window 1 of 3" in note

    # The balance is a real number off the manager, not a fixed phrase: an agent
    # that has worked one instalment of two has half a unit left, and a note
    # that said "1.00" all game would be worse than saying nothing.
    assert "0.50" in full


def test_each_window_gets_its_own_turn_budget(island):
    """Budgets differ by window because the windows are not the same size of
    job, and a trading budget spent on every one of them is most of what made
    the previous flow expensive."""
    from barter.flow import Budgets

    _, _, log = _scripted_island(island, rounds=1)
    got = {e["label"]: e["budget"] for e in log}
    assert got == {"produce": Budgets().produce,
                   "offer": Budgets().offer, "settle": Budgets().settle}


def test_a_flow_artefact_is_not_counted_as_a_refusal(island):
    """`expired_unseen` is the guard on this experiment's own honesty: an offer
    the seller never had a chance to look at must not be reported next to one it
    considered and declined."""
    from barter.flow import Played

    manager = Manager(island=island)
    for agent_id, state in manager.agents.items():
        manager.op_produce(agent_id, {g: state.alpha[i]
                                      for i, g in enumerate(manager.goods)})
    manager.open(LEVEL_SETTLE)
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
        manager.open(LEVEL_SETTLE)

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
        manager.open(LEVEL_SETTLE)
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

    manager = Manager(island=island)
    row = snapshot(island, manager, None, round_no=1, label="talk")
    assert row["efficiency"] is None
    assert row["holding_nothing"] == island.n_agents
    assert row["labour_spent"] == 0.0
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
    """One row per round, taken after the round has closed — snapshotting
    mid-round would read a half-applied state, and there is no longer a stage
    boundary inside a round to hang one on."""
    manager, played, _ = _scripted_island(island, rounds=3)
    assert [row["label"] for row in played.trajectory] == ["round"] * 3
    assert [row["round"] for row in played.trajectory] == [1, 2, 3]
    # Labour is fully committed by the end of the first round under `once`.
    assert played.trajectory[0]["labour_spent"] == pytest.approx(1.0)


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
        "ruin_warning", "horizon", "labour_left", "labour_rule",
        "own_value", "own_score")
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
            return _wire(handle, manager, "a1", telling, run="sw",
                         board=BoardService(handle.client("manager"),
                                            manager, run="sw"))

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
    once = _flat(brief_for(island, manager, "a1", "told"))
    rolling = _flat(brief_for(island, manager, "a1", compose("told", on=["rolling"])))
    assert "one chance to spend it" in once
    assert "one chance to spend it" not in rolling
    assert "instalments" in rolling
    assert "not carried over" in rolling
    # The rest of the world is unchanged — this is a switch, not a rewrite.
    assert rolling.count("propose_trade") == once.count("propose_trade")


def test_the_brief_never_says_labour_is_committed_before_anyone_has_spoken(island):
    """The stale sentence the window model made false.

    It said the unit was spent "once, at the start". `produce` is open for the
    whole of every round now, so an agent may listen for a round and commit
    after — which is the entire point of opening a round with talking, and a
    brief that told them to commit at the start would have suppressed the
    behaviour the redesign exists to make possible.

    What must survive is that it is still *one* call. "You may wait" and "you
    may work twice" are different worlds and the manager only implements one.
    """
    manager = Manager(island=island)
    for arm in ("silent", "told", "paid"):
        text = _flat(brief_for(island, manager, "a1", arm))
        assert "at the start" not in text, arm
        assert "when* you call it is yours to decide" in text, arm
        assert "you call it once" in text, arm
        # ...and the cost of waiting is stated, because it is real and an agent
        # that waits until the last round has nothing left to trade with.
        assert "not trading what you made" in text, arm


def test_the_brief_says_what_opens_when(island):
    """The window shape is the world, not a hint, so every arm gets it.

    An agent that does not know approving opens in the third window cannot plan
    to approve in it, and an arm missing that would be measuring the harness
    rather than its convention — which is why this is not a switch.

    Two things stay out. How long a window lasts is not here: agents know there
    is a deadline and not its size. Nor is the number of rounds, which belongs
    to `horizon` in the turn note — putting it here would hand it over in every
    arm and make that switch unattributable.
    """
    manager = Manager(island=island)
    for arm in ("silent", "free", "paid"):
        text = _flat(brief_for(island, manager, "a1", arm))
        assert "three windows that open in order" in text, arm
        assert "`propose_trade`, and withdraw" in text, arm
        assert "`approve_trade`" in text, arm
        assert "Nothing closes again inside a round" in text, arm
        assert "ends on a clock" in text, arm
        # No wall-clock length, and no round count.
        assert "second" not in text.lower().replace("seconds", "@"), arm
        assert "rounds" in text and "3 rounds" not in text, arm


def test_a_silent_island_is_not_told_it_can_talk_in_the_first_window(island):
    """The window shape describes what an arm actually has.

    `silent` has no `say` and no `listen`, so a first window advertised as a
    talking window would promise a channel that is not there — in the one arm
    whose whole purpose is the absence of it.
    """
    quiet = _flat(brief_for(island, manager := Manager(island=island), "a1", "silent"))
    loud = _flat(brief_for(island, manager, "a1", "free"))
    assert "talk" not in quiet.lower()
    assert "you can `produce`" in quiet
    assert "you can talk, and you can `produce`" in loud


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


def test_a_rolling_island_spends_every_instalment_it_is_offered(island):
    """The tick collision is now impossible by construction, not by a fix.

    Under the old shape the produce pass and the first trading round shared a
    tick, so every agent's first attempt to work during trading came back "you
    have already worked this round" — one whole instalment of the island's
    labour, thrown away by the clock rather than by any decision. A run lost
    14% of its labour that way and it read in the results as agents declining
    to work.

    Now `produce` is open for the whole round, so an instalment can only be
    lost by an agent choosing to lose it. This asserts the guarantee directly:
    work in every round and you finish having spent exactly one unit. The
    second half asserts the other edge of the same rule — the instalment is per
    round, so a second `produce` inside one round is still refused.
    """
    import random

    import anyio
    from barter.flow import Notes, play

    rounds = 3
    manager = Manager(island=island, rolling=True,
                      labour_per_round=1.0 / rounds)
    refused, twice = [], []
    worked: set[tuple[int, str]] = set()

    async def take_turn(agent_id, *, round_no, label, note, budget):
        first = (round_no, agent_id) not in worked
        worked.add((round_no, agent_id))
        reply = manager.dispatch(agent_id, {"op": "produce", "plan": {"fish": 1.0}})
        if first and not reply.get("ok"):
            refused.append((round_no, label, agent_id, reply.get("error")))
        if not first:
            twice.append(reply.get("ok"))
        await anyio.sleep(0.005)
        return ""

    notes = Notes(rolling=True,
                  labour=lambda who: max(0.0, 1.0 - manager.agents[who].spent))
    anyio.run(lambda: play(manager, take_turn, rounds=rounds,
                           rng=random.Random(0), window=0.02, notes=notes))

    assert refused == [], refused
    assert twice and not any(twice)
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
            return _wire(handle, manager, "a1", telling, run="calc",
                         board=BoardService(handle.client("manager"),
                                            manager, run="calc"))

        full = wire_for(telling_for("free")).state()
        assert "value_per_unit" in full and "utility" in full

        quiet = wire_for(compose("free", off=["own_value", "own_score"])).state()
        assert "value_per_unit" not in quiet and "utility" not in quiet
        # ...and nothing else went with them. Capacities, tastes and holdings are
        # the world, not a hint about it.
        assert set(full) - set(quiet) == {"value_per_unit", "utility"}

        # The manager itself is untouched — it answered in full both times.
        assert "value_per_unit" in manager.op_state("a1")


def test_seeing_a_collision_and_knowing_what_to_do_are_separate_switches(island):
    """`crossings` shows the pair; `tiebreak` says which one gives way.

    They are separable on purpose. A trader that can see it has both proposed
    and been proposed the same swap still has to decide who withdraws, and that
    decision has no right answer on its merits — the failure mode is not
    choosing badly but choosing *differently*, so it is a pure coordination
    problem and the smallest one this experiment contains. Bundling the two
    would make "the board helped" unattributable between noticing and knowing,
    which is the mistake the whole switch refactor exists to prevent.
    """
    from barter.llm import TIEBREAK_BRIEF, compose

    manager = Manager(island=island)
    seeing = compose("built", on=["crossings"])
    knowing = compose("built", on=["tiebreak"])

    # Seeing is a tool change and must not move a word of the prompt.
    assert brief_for(island, manager, "a1", seeing) == brief_for(island, manager, "a1", "built")
    # Knowing is words, and only its own paragraph.
    assert brief_for(island, manager, "a1", knowing) == (
        brief_for(island, manager, "a1", "built") + TIEBREAK_BRIEF)
    # Neither is on by default anywhere on the ladder.
    for arm in ARMS_ALL:
        assert not telling_for(arm).crossings and not telling_for(arm).tiebreak


def test_the_stated_tiebreak_admits_that_the_rule_itself_is_arbitrary(island):
    """If the brief argued that first-proposed is *better*, the arm would be
    testing whether models accept an argument. It is testing whether they adopt
    a convention, so the text has to say the choice does not matter and the
    agreement does."""
    from barter.llm import TIEBREAK_BRIEF

    text = " ".join(TIEBREAK_BRIEF.split())
    assert "does not matter" in text
    assert "both pick the same one" in text


def test_the_silent_arm_has_no_way_to_send_text_at_all(island):
    """Silence has to mean silence, and it did not.

    The existing gate checks that `say` and `listen` are absent, and
    `propose_trade` walked straight past it: it carried a free-text `note` that
    the manager stores and hands to the seller in `pending_trades`. Every arm
    had it, including the one whose entire purpose is having no way to
    communicate — a directed, escrow-costing, 200-character channel to anybody
    you propose to. `silent` against `free` is the comparison that establishes
    whether communication helps at all, and it was not measuring that.

    So the note is its own switch now. `silent` + `trade_note` becomes a real
    arm rather than an accident: is a costly, private, one-to-one channel
    enough, when a free public one is not?
    """
    from barter.llm import build_tool_list, telling_for

    for arm, expected in (("silent", False), ("free", True)):
        assert telling_for(arm).trade_note is expected

    manager = Manager(island=island)
    with hub() as handle:
        service, wires = _wired(handle, manager, "silent")
        schemas = {t.name: t.input_schema for t in build_tool_list(wires["a1"])}
        assert "note" not in schemas["propose_trade"], schemas["propose_trade"]

        # ...and nothing else on a silent island takes free text either. The
        # string arguments a silent island may legitimately have are all
        # *identifiers* — an agent id, a trade id, a good name — every one of
        # which the manager validates against a closed set, so none can carry a
        # message. Anything else that is a string is a channel, and listing the
        # exceptions by name means a new one has to be argued for rather than
        # arriving unnoticed, which is exactly how `note` did.
        identifiers = {"seller", "trade_id", "good"}
        for name, schema in schemas.items():
            text = [k for k, v in schema.items() if v is str and k not in identifiers]
            assert not text, f"{name} accepts free text: {text}"


def test_a_dropped_note_is_dropped_and_not_merely_undocumented(island):
    """An argument missing from the schema can still be sent. This is the one
    field whose whole significance is that it reaches another agent, so the
    handler discards it rather than trusting the schema to keep it out."""
    manager = Manager(island=island)
    for agent_id in manager.agents:
        manager.op_produce(agent_id, {"fish": 0.5, "grain": 0.5})
    manager.open(LEVEL_SETTLE)
    with hub() as handle:
        service, wires = _wired(handle, manager, "silent", run="quiet")
        reply = wires["a1"].manager_call(
            "propose", seller="a2", give={"fish": 0.01}, want={"grain": 0.01},
            note="")
        assert reply.get("ok")
        assert manager.trades[reply["trade_id"]].note == ""


def test_the_produce_tool_never_contradicts_the_labour_rule(island):
    """The same bug as the brief's, one layer down and missed when that was
    fixed: a fixed description telling rolling agents "you may only do this
    once" while their system prompt correctly said they spend in instalments,
    and while the manager accepted one call per round."""
    from barter.llm import build_tool_list, compose

    manager = Manager(island=island)
    with hub() as handle:
        service, wires = _wired(handle, manager, "told", run="doc")
        wires["a1"].telling = compose("told", on=["rolling"])
        rolling = {t.name: t.description for t in build_tool_list(wires["a1"])}
        wires["a1"].telling = compose("told")
        once = {t.name: t.description for t in build_tool_list(wires["a1"])}

    assert "only do this once" in once["produce"]
    assert "only do this once" not in rolling["produce"]
    assert "once per round" in rolling["produce"].lower()
    assert "not carried over" in rolling["produce"]


def test_the_session_budget_is_computed_from_the_round_not_guessed(island):
    """It was a hand-set 240, chosen when a round was two passes. The round is
    six now, so the island asked for roughly three times what agents had and
    they would have gone quiet somewhere in the back half — with no error, since
    running out just ends a turn. Computing it means the next change to the
    order of play cannot silently outgrow it."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))
    from barter.flow import Budgets
    from barter_llm_experiment import turns_needed

    budgets = Budgets()
    # The property, not a magnitude: the figure must exceed the old hand-set
    # 240 for the default island and must move with the round count. Pinning a
    # multiple instead tied this gate to whatever the tool budgets happened to
    # be, and it broke the moment they were trimmed — which is the budgets
    # doing their job, not the sizing being wrong.
    assert turns_needed(8, 0, budgets) > 240
    assert turns_needed(8, 0, budgets) > turns_needed(4, 0, budgets)
    # ...and it never drops below the old floor for a very short island.
    assert turns_needed(1, 0, budgets) >= 240


def test_a_window_gives_every_agent_the_same_clock(island):
    """Turn-taking is most of an island's wall clock, and agents wait on each
    other for nothing: a window should cost the slowest agent, not the sum of
    all of them.

    The thing that must not change is what the manager sees. Agents act on
    different tasks now, so two can be inside `dispatch` at once, both moving
    goods — conservation is the assertion that says they did not corrupt each
    other, and it is checked at every round boundary by `play` itself.
    """
    import random

    import anyio
    from barter.flow import Notes, play

    started, finished = [], []

    async def take_turn(agent_id, *, round_no, label, note, budget):
        started.append(agent_id)
        # Every agent parks here. Sequentially this deadlocks the assertion
        # below; concurrently they all arrive before any of them leaves.
        await anyio.sleep(0.02)
        finished.append(agent_id)
        if label == "produce":
            manager.dispatch(agent_id, {"op": "produce", "plan": {"fish": 1.0}})
        return f"{agent_id}"

    manager = Manager(island=island)
    played = anyio.run(lambda: play(
        manager, take_turn, rounds=1, rng=random.Random(0), window=0.1,
        notes=Notes()))

    n = island.n_agents
    # Every agent started before any of them finished.
    assert set(started[:n]) == set(manager.agents)
    assert set(finished[:n]) == set(manager.agents)
    # Nobody was starved of a window, and the transcript holds every turn
    # anybody took rather than one row per agent per stage.
    assert played.missed == 0
    assert len(played.transcript) >= n * 3
    assert {row["agent"] for row in played.transcript} == set(manager.agents)
    manager.check_conservation()
    assert manager.phase == "closed"


def test_the_tool_surface_can_be_read_without_a_model_client(island):
    """The gates that read the tool surface must run where no SDK is installed.

    They are the ones that caught the silent arm's free-text channel and the
    `produce` description contradicting the labour rule — both shipped past
    every other check — and CI has no model client, so for a while they were the
    two gates that only ever ran on a laptop.

    The stand-in is only allowed to be a stand-in if it is the same shape as the
    real thing, so this checks it against the SDK wherever the SDK is there to
    check against.
    """
    from barter.llm import _tool_decorator, build_tool_list

    manager = Manager(island=island)
    with hub() as handle:
        wire = _wire(handle, manager, "a1", telling_for("built"))
        real = build_tool_list(wire)

        sdk = pytest.importorskip("claude_agent_sdk",
                                  reason="no SDK here, so the stand-in is what ran")
        assert _tool_decorator() is sdk.tool

        made = _tool_decorator()("x", "does x", {"a": str})(lambda _: None)
        for field in ("name", "description", "input_schema", "handler"):
            assert hasattr(made, field), field
        assert all(hasattr(t, "input_schema") and hasattr(t, "description")
                   for t in real)


def test_a_finished_island_can_be_written_up_without_a_model(island):
    """The record is built after every model call has been paid for.

    An exception there loses the island rather than degrading it, and it is the
    one part of the runner the offline gates never reached — they all stopped at
    `play`. A local named `board` shadowing the `BoardService` took a
    nine-minute island down at the very last statement of the record, for a
    bill and nothing else.

    So the write-up is a function of a finished manager and a finished `Played`,
    and this drives it end to end with no model: every field the renderer reads
    has to be there, and `render` has to survive its own output.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "experiments"))
    from barter_llm_experiment import record_for, render

    manager, played, _ = _scripted_island(island, rounds=2)
    record = record_for(
        island=island, manager=manager, played=played,
        telling=telling_for("bound"), arm="bound", seed=4, model="none",
        cost=0.0, floor=[], quotes={"a1": {"fish": 1.0, "grain": 2.0}},
        quotes_posted=1, sweeps=7, sweep_errors=[], transcript=played.transcript,
        rounds=2, window=(0.02, 0.05, 0.05), sweep_every=1.0, muster=None,
        rolling=False, instalments=1)

    # The clock is in the record, because two runs at different windows are not
    # the same market and a record that omitted it could not say so.
    assert record["flow"]["window_seconds"] == [0.02, 0.05, 0.05]
    assert record["flow"]["sweep_every"] == 1.0
    assert record["missed_windows"] == played.missed
    assert record["sweeps"] == 7
    # The quote board is the quotes, not the sweeper that shadowed it.
    assert record["quote_board"] == {"a1": {"fish": 1.0, "grain": 2.0}}
    assert record["switches"] == list(telling_for("bound").switches())
    # Who never turned up is recorded even when it is nobody, because absent
    # and "declined everything" score identically and are not the same result.
    assert record["absent"] == [] and record["musters"] == []

    # And the renderer reads only what the record actually has.
    text = render(record)
    assert "bound" in text and "window" in text


def test_nothing_opens_until_everyone_has_read_the_schedule(island):
    """The muster, which is the fix for a failure that read as a bad result.

    The island used to simply begin. An agent whose first turn started forty
    seconds into a sixty-second window had no way to know it, and across seven
    paid islands the settle window drew between one and five turns in total —
    the arms that settled nothing were the arms whose agents never got a turn
    inside it. That is the harness starting a race without firing a gun.

    So the manager posts the whole timetable in absolute times and nothing
    opens until every trader has acknowledged it.
    """
    import random

    import anyio
    from barter.flow import Muster, play

    manager = Manager(island=island)
    seen: list[str] = []
    levels_during_muster: list[int] = []

    async def take_turn(agent_id, *, round_no, label, note, budget):
        seen.append(label)
        if label == "muster":
            levels_during_muster.append(manager.level)
            # The schedule is in the note, in absolute times, before anything
            # has opened -- which is the whole point of posting it.
            assert "round 1 window 3" in note and "approve_trade" in note
            version = manager.agenda.version
            assert manager.dispatch(agent_id, {"op": "ack", "version": version})["ok"]
        elif label == "produce":
            manager.dispatch(agent_id, {"op": "produce", "plan": {"fish": 1.0}})
        await anyio.sleep(0.005)
        return ""

    played = anyio.run(lambda: play(
        manager, take_turn, rounds=1, rng=random.Random(0), window=0.05,
        muster=Muster(lead=0.4, ack_within=0.3, attempts=2)))

    assert "muster" in seen
    # Nothing was open while the island was forming up.
    assert set(levels_during_muster) == {LEVEL_PRODUCE}
    assert played.musters and played.musters[-1]["acked"] == sorted(manager.agents)
    assert played.absent == []
    manager.check_conservation()


def test_a_schedule_nobody_answered_is_posted_again_with_later_times(island):
    """An agent that has not acknowledged is usually not refusing — it is still
    inside a turn that began before the schedule existed. The fix for that is a
    later start, not a longer wait, so the schedule is withdrawn and re-posted.

    Acks of the withdrawn one are dropped. An island where two traders hold two
    timetables has not started together in any sense that matters.
    """
    import random

    import anyio
    from barter.flow import Muster, play

    manager = Manager(island=island)
    versions: list[int] = []

    async def take_turn(agent_id, *, round_no, label, note, budget):
        if label == "muster":
            versions.append(manager.agenda.version)
            # Only a1 ever answers, so the muster runs out of attempts.
            if agent_id == "a1":
                manager.dispatch(agent_id, {"op": "ack",
                                            "version": manager.agenda.version})
        await anyio.sleep(0.005)
        return ""

    played = anyio.run(lambda: play(
        manager, take_turn, rounds=1, rng=random.Random(0), window=0.05,
        muster=Muster(lead=0.25, ack_within=0.15, attempts=3)))

    # Three schedules posted, each with later times than the last.
    assert [m["version"] for m in played.musters] == [1, 2, 3]
    starts = [m["starts_at"] for m in played.musters]
    assert starts == sorted(starts) and starts[0] < starts[-1]
    assert max(versions) > 1
    # It started anyway rather than hanging, and said who never turned up.
    assert played.absent == sorted(set(manager.agents) - {"a1"})
    assert played.rounds_played == 1


def test_an_ack_of_a_withdrawn_schedule_is_refused(island):
    """The version is checked rather than trusted.

    An agent that read v1, thought for a while and acknowledged after v2 was
    posted has agreed to times that no longer exist. Accepting it would start
    the island with traders working from different clocks, which is exactly
    what the muster is for.
    """
    from barter.manager import Agenda

    manager = Manager(island=island)
    manager.post_agenda(Agenda(version=1, posted_at=0.0, acks_by=90.0,
                               starts_at=120.0, windows=(60.0, 60.0, 60.0),
                               rounds=3))
    assert manager.dispatch("a1", {"op": "ack", "version": 1})["ok"]
    assert manager.acked == {"a1"}

    manager.post_agenda(Agenda(version=2, posted_at=90.0, acks_by=180.0,
                               starts_at=210.0, windows=(60.0, 60.0, 60.0),
                               rounds=3))
    # Re-posting drops the old acks: a1 agreed to times that are gone.
    assert manager.acked == set()
    stale = manager.dispatch("a1", {"op": "ack", "version": 1})
    assert not stale["ok"] and "v2" in stale["error"]
    assert manager.dispatch("a1", {"op": "ack", "version": 2})["ok"]
    assert not manager.all_acked()

    for agent_id in manager.agents:
        manager.dispatch(agent_id, {"op": "ack", "version": 2})
    assert manager.all_acked()


def test_the_schedule_and_the_time_are_readable_without_queueing_for_them(island):
    """"What time is it" must not go through the order board.

    An agent that queued for the time would be handed a time that had already
    passed, which on a schedule of absolute times is worse than no answer.
    """
    from barter.manager import Agenda, BoardService

    manager = Manager(island=island)
    with hub() as handle:
        board = BoardService(handle.client("manager"), manager, run="ag")
        board.now = lambda: 12.5
        manager.post_agenda(Agenda(version=1, posted_at=0.0, acks_by=90.0,
                                   starts_at=120.0, windows=(60.0, 120.0, 120.0),
                                   rounds=2))
        manager.dispatch("a1", {"op": "ack", "version": 1})
        board.sweep()

        wire = _wire(handle, manager, "a1", telling_for("silent"), run="ag")
        wire.clock_key, wire.agenda_key = board.clock, board.agenda_key
        reading = wire.clock()

    assert reading["now"] == 12.5
    assert reading["agenda_version"] == 1 and reading["acked"] == ["a1"]
    schedule = reading["agenda"]["schedule"]
    assert len(schedule) == 6  # two rounds of three windows
    assert schedule[0]["opens_at"] == 120.0
    # Unequal by design: a trading turn runs three to six times longer than a
    # production one, so a round is 60 + 120 + 120 rather than three sixties.
    assert [w["seconds"] for w in schedule[:3]] == [60.0, 120.0, 120.0]
    assert schedule[-1]["closes_at"] == 120.0 + 2 * 300.0
    # The last window of each round is the one that settles.
    assert "approve_trade" in schedule[2]["you_may"]


def test_one_call_offers_many_deals_and_a_bad_one_does_not_sink_the_rest(island):
    """Why a turn took two minutes, and the fix.

    Three offers meant three tool calls, each a write to the board, a wait for
    the next sweep and a read back, with the model thinking again in between.
    Measured, a trading turn ran 68 to 169 seconds against a sixty-second
    window. One call, one order, one sweep.

    Partial results are the part that makes it safe to use. If a batch were
    all-or-nothing, an agent whose second offer overshot by a rounding error
    would lose the two that were fine — and batching would be riskier than not
    batching, which is not a tradeoff anybody should have to reason about.
    """
    manager = Manager(island=island)
    with hub() as handle:
        _, wires = _wired(handle, manager, "free")
        for agent_id, state in manager.agents.items():
            wires[agent_id].manager_call(
                "produce", plan={g: state.alpha[i] for i, g in enumerate(manager.goods)})
        manager.open(LEVEL_SETTLE)

        reply = wires["a1"].manager_batch([
            {"op": "propose", "seller": "a2", "give": {"fish": 0.01},
             "want": {"grain": 0.005}},
            {"op": "propose", "seller": "a3", "give": {"fish": 1e9},
             "want": {"grain": 1.0}},
            {"op": "propose", "seller": "a3", "give": {"fish": 0.01},
             "want": {"cloth": 0.002}},
        ])
        assert reply["ok"] and reply["applied"] == 2
        oks = [r["ok"] for r in reply["results"]]
        assert oks == [True, False, True]
        assert "cannot cover" in reply["results"][1]["error"]

        # ...and one call settles many, the same way.
        waiting = wires["a3"].pending()["awaiting_your_approval"]
        settled = wires["a3"].manager_batch(
            [{"op": "approve", "trade_id": t["id"]} for t in waiting])
        assert settled["applied"] == len(waiting) >= 1
    manager.check_conservation()


def test_a_batch_cannot_smuggle_a_batch(island):
    """A batch inside a batch is a recursion an agent should not be able to
    start, and the bound on batch size would mean nothing if it could."""
    manager = Manager(island=island)
    reply = manager.dispatch("a1", {"op": "batch", "ops": [
        {"op": "batch", "ops": [{"op": "state"}]}]})
    assert reply["ok"] and reply["results"][0]["ok"] is False
    assert "cannot contain a batch" in reply["results"][0]["error"]

    too_many = manager.dispatch("a1", {"op": "batch",
                                       "ops": [{"op": "state"}] * 100})
    assert not too_many["ok"] and "at most" in too_many["error"]
    assert not manager.dispatch("a1", {"op": "batch", "ops": []})["ok"]


def test_the_round_ends_when_it_said_it_would(island):
    """A schedule of exact times the harness does not keep is worse than none.

    A turn still running at the published end used to be awaited, so one
    measured round overran its announced end by 82 seconds — while agents that
    had read that schedule were planning against it. Now the turn is abandoned
    and counted.
    """
    import random

    import anyio
    from barter.flow import play

    manager = Manager(island=island)

    async def take_turn(agent_id, *, round_no, label, note, budget):
        # Far longer than the whole round, so every turn is still running when
        # the published end arrives.
        await anyio.sleep(10.0)
        return "never finished"

    started = None

    async def run():
        nonlocal started
        started = anyio.current_time()
        played = await play(manager, take_turn, rounds=1, rng=random.Random(0),
                            window=0.05)
        return played, anyio.current_time() - started

    played, elapsed = anyio.run(run)
    # Three windows of 0.05s, and the round is over at 0.15 whatever anyone is
    # in the middle of.
    assert elapsed < 1.0, elapsed
    assert played.cut == island.n_agents
    assert all(row.get("cut") for row in played.transcript)
    manager.check_conservation()


def test_a_turn_the_round_cannot_contain_is_not_started(island):
    """Cutting a turn off costs a model call for a result nobody can use, so a
    turn is only started when the agent's own pace says it will fit. Its own,
    not an average: agents think at different speeds and a mean over them would
    hold back the fast one and cut the slow one."""
    import random

    import anyio
    from barter.flow import play

    manager = Manager(island=island)

    async def take_turn(agent_id, *, round_no, label, note, budget):
        await anyio.sleep(0.04)
        return ""

    played = anyio.run(lambda: play(manager, take_turn, rounds=1,
                                    rng=random.Random(0), window=0.05))
    # Turns run 0.04 against a 0.15 round, so after three of them there is not
    # enough left for a fourth and the agent stops rather than being cut.
    assert played.held_back >= 1
    assert played.cut == 0
    manager.check_conservation()


def test_the_windows_do_not_have_to_be_the_same_length(island):
    """They were all sixty seconds, and a production turn was measured at 18-33
    while a trading turn ran 68-169. A window has to be wide enough for one
    whole turn or it is not a window, it is an interruption."""
    import random

    import anyio
    from barter.flow import play

    manager = Manager(island=island)
    opened: list[tuple[str, float]] = []
    start = None

    async def take_turn(agent_id, *, round_no, label, note, budget):
        opened.append((label, anyio.current_time() - start))
        # Long enough that an agent cannot exhaust its per-round turn cap
        # before the later windows open -- which would be the cap deciding the
        # test rather than the window lengths.
        await anyio.sleep(0.02)
        return ""

    async def run():
        nonlocal start
        start = anyio.current_time()
        return await play(manager, take_turn, rounds=1, rng=random.Random(0),
                          window=(0.05, 0.2, 0.1))

    anyio.run(run)
    first_offer = min(t for label, t in opened if label == "offer")
    first_settle = min(t for label, t in opened if label == "settle")
    assert 0.05 <= first_offer < 0.2
    assert 0.25 <= first_settle < 0.35

    with pytest.raises(ValueError, match="one number or three"):
        anyio.run(lambda: play(manager, take_turn, rounds=1,
                               rng=random.Random(0), window=(1.0, 2.0)))
