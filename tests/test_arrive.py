"""Arriving in a room, and the two ways agents got that wrong.

On 2026-08-17 two agents in this project's own dogfooding spent half an hour
missing each other in one room. Neither was absent and neither was on the wrong
key. One read the roster and the channel list; the other read its inbox. Both
concluded the room was empty, and a board entry sat between them the whole
time — twenty-four minutes old when the second agent declared the room empty.

The inbox check is the sharpest version: in a room you just joined it can only
ever come back empty, because nobody has sent anything to an id you have not
published. It answers a question that has one possible answer.

So `arrive` reads all three surfaces, says which it read, and leaves something
behind that outlives the turn — presence lapses in two minutes, and a handoff
between sessions that do not overlap cannot live there.
"""

from __future__ import annotations

import json

import pytest

from switchboard.cli import ARRIVE_PREFIX, main
from switchboard.crypto import generate_key
from switchboard.testing import BASE_URL, hub

WS = "arrive-ws"


@pytest.fixture
def cli_hub(monkeypatch):
    """Encrypted, because that is where this runs and where identifiers blind."""
    import switchboard.cli as cli_module

    key = generate_key()
    with hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        monkeypatch.setenv("SWITCHBOARD_KEY", key)
        handle.workspace_key = key
        yield handle


def _arrive(capsys, *args):
    code = main(["--url", BASE_URL, "-w", WS, "--json", "arrive", *args])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_arriving_reads_all_three_surfaces_and_says_so(cli_hub, capsys, monkeypatch):
    """The whole point. A report that does not name what it checked lets
    "I looked in one place" render as "nobody is here"."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    out = _arrive(capsys, "reviewing the lexer")

    assert set(out["checked"]) == {"roster", "board", "inbox"}
    assert out["alone"] is True


def test_a_board_entry_alone_means_the_room_is_not_empty(cli_hub, capsys, monkeypatch):
    """The measured failure, in one test.

    Nobody is present and nothing is in the inbox — the two surfaces the real
    agents checked. The room still has somebody's intent in it, and saying
    "empty" here is how half an hour was lost.
    """
    cli_hub.client("bob").board_set("coord/reports/bob", {"doing": "the parser"})
    cli_hub.advance(600)  # bob's presence is long gone

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    out = _arrive(capsys, "looking for whoever owns the parser")

    assert out["checked"]["roster"] == 0, "nobody is present — the misleading half"
    assert out["checked"]["inbox"] == 0, "and the inbox is empty, as it always is"
    assert out["checked"]["board"] == 1, "but the room is plainly in use"
    assert out["alone"] is False


def test_an_empty_room_says_it_checked_rather_than_just_going_quiet(
    cli_hub, capsys, monkeypatch
):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    assert main(["--url", BASE_URL, "-w", WS, "arrive", "first in"]) == 0

    printed = capsys.readouterr().out
    assert "checked 0 peer(s)" in printed
    assert "really is unused, not merely unchecked" in printed


def test_arriving_leaves_something_that_outlives_the_turn(cli_hub, capsys, monkeypatch):
    """Presence lapses in two minutes and the hub is cheap to lose by design,
    so a handoff between non-overlapping sessions has to live on the board."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _arrive(capsys, "porting the store")

    cli_hub.advance(3600)  # an hour later, alice is long gone

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "bob")
    out = _arrive(capsys, "wondering if anyone is on the store")

    assert out["checked"]["roster"] == 0, "alice is not here"
    intents = [e["value"]["intent"] for e in out["board"]
               if str(e["key"]).startswith(ARRIVE_PREFIX)]
    assert "porting the store" in intents, "but alice's intent is"


def test_an_agent_does_not_report_its_own_note_back_to_itself(
    cli_hub, capsys, monkeypatch
):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _arrive(capsys, "first pass")
    out = _arrive(capsys, "second pass")

    mine = [e for e in out["board"] if str(e["key"]).startswith(ARRIVE_PREFIX)]
    assert mine == [], "your own arrival note is not a peer"


def test_a_peer_on_another_key_is_reported_and_not_counted_as_company(
    cli_hub, capsys, monkeypatch
):
    """Same room, different key: you are in each other's roster and cannot
    exchange a word. Counting that as company is the forty-minute failure."""
    cli_hub.client("stranger", key=generate_key()).register(name="elsewhere")

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    out = _arrive(capsys, "looking")

    assert out["checked"]["roster"] == 0
    assert len(out["key_mismatches"]) == 1


def test_a_waiting_dm_is_surfaced_without_being_consumed(cli_hub, capsys, monkeypatch):
    """Peeked, not drained: arriving must not eat a message the agent has not
    decided how to handle yet."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _arrive(capsys, "here")  # publish alice's id so bob can address it
    cli_hub.client("bob").send("alice", "are you taking the store?")

    out = _arrive(capsys, "back")
    assert out["checked"]["inbox"] == 1

    again = _arrive(capsys, "back again")
    assert again["checked"]["inbox"] == 1, "peeking must not consume it"


# --- addressing a peer ------------------------------------------------------
#
# The other half of the same failure. Two agents were told to call each other
# `switchboard_multi` and `roadmap_sep_agent`. Only one pinned its id, so a DM
# to the other blinded an operator-side label into a channel nobody was
# listening on. The hub accepted it and printed `sent #344`.


def _dm(capsys, to, body="hello"):
    code = main(["--url", BASE_URL, "-w", WS, "dm", to, body])
    assert code == 0
    return capsys.readouterr()


def test_a_dm_to_nobody_warns_instead_of_reporting_success(
    cli_hub, capsys, monkeypatch
):
    """`sent #344` and `delivered` are not the same claim, and only one of them
    was ever true here."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _arrive(capsys, "here")

    captured = _dm(capsys, "a-label-nobody-pinned")
    assert "nobody on the roster answers to" in captured.err
    assert "switchboard agents" in captured.err


def test_a_dm_to_a_real_peer_is_quiet(cli_hub, capsys, monkeypatch):
    """The warning must not cry wolf, or it gets ignored on the run that counts."""
    bob = cli_hub.client("bob")
    bob.register(name="bob")

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    captured = _dm(capsys, bob.agent_id)
    assert "nobody on the roster" not in captured.err


def test_a_branch_resolves_to_the_id_that_peer_is_reading(
    cli_hub, capsys, monkeypatch
):
    """Ids are per-process and branches survive a restart, which is why the
    claim reaper matches on branch too. Same reason, same rule."""
    cli_hub.client("bob").register(name="bob", branch="claude/the-parser")

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    captured = _dm(capsys, "claude/the-parser")
    assert "matched the branch" in captured.err
    assert "nobody on the roster" not in captured.err


def test_an_ambiguous_branch_refuses_rather_than_picking_one(
    cli_hub, capsys, monkeypatch
):
    """Two live sessions on one branch is exactly when guessing sends the
    message to the wrong one, silently."""
    cli_hub.client("bob").register(name="bob", branch="claude/shared")
    cli_hub.client("carol").register(name="carol", branch="claude/shared")

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    with pytest.raises(SystemExit, match="matches the branch of 2"):
        main(["--url", BASE_URL, "-w", WS, "dm", "claude/shared", "hi"])


def test_an_absent_peer_that_pinned_its_id_is_still_reachable(
    cli_hub, capsys, monkeypatch
):
    """Warning, never refusing. A peer between turns is absent from the roster
    and must still be sent to — `blind` is deterministic, so the message waits
    in exactly the place they will look."""
    bob = cli_hub.client("bob", agent_id="bob")
    bob.register(name="bob")
    cli_hub.advance(600)  # bob's presence lapses

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    captured = _dm(capsys, "bob", "still here?")
    assert "nobody on the roster answers to" in captured.err

    assert [m["body"] for m in bob.inbox()] == ["still here?"], "warned, and delivered"
