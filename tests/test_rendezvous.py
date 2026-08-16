"""First contact: finding a peer before there is any history with them.

Every other timing signal here is built *from* contact — a forecast comes from
your own history in a workspace and rides on a message — so none of it helps
before the first exchange. These tests cover the three things that do: a note
that outlives presence, a slot both sides derive without talking, and a look
that escalates rather than quitting after two minutes.
"""

from __future__ import annotations

import json

import pytest

from switchboard import rendezvous
from switchboard.cli import main
from switchboard.testing import BASE_URL, hub

WS = "rendezvous-ws"


# --- the shared slot --------------------------------------------------------


def test_two_agents_derive_the_same_slot_without_talking():
    """The whole point: convergence with zero communication.

    Both sides hold the workspace token and nobody else does, so hashing it
    gives them the same phase. Nothing is exchanged, so nothing can be missed.
    """
    now = 1_000_000.0
    a = rendezvous.next_slot("shared-token", "design-review", now)
    b = rendezvous.next_slot("shared-token", "design-review", now)
    assert a == b
    assert a > now


def test_different_topics_do_not_pile_onto_one_minute():
    """Otherwise every rendezvous on a busy hub becomes a thundering herd."""
    now = 1_000_000.0
    slots = {
        rendezvous.next_slot("shared-token", topic, now)
        for topic in ("design-review", "migration", "release", "triage")
    }
    assert len(slots) > 1


def test_different_workspaces_do_not_share_a_slot():
    now = 1_000_000.0
    assert rendezvous.next_slot("token-a", "t", now) != rendezvous.next_slot(
        "token-b", "t", now
    )


def test_the_slot_is_stable_across_a_cadence_and_then_advances():
    token, topic = "shared-token", "design-review"
    now = 1_000_000.0
    first = rendezvous.next_slot(token, topic, now)
    # Anywhere before the slot, both sides still agree on the same one.
    assert rendezvous.next_slot(token, topic, first - 1) == first
    # Past it, the next one is exactly one cadence later.
    assert rendezvous.next_slot(token, topic, first + 1) == first + rendezvous.SLOT_SECONDS


def test_clock_skew_is_what_the_hub_anchor_exists_to_remove():
    """Stated as a test because it is the failure mode being prevented.

    Two machines minutes apart computing against their own clocks land on
    different slots and never overlap — the original miss, rebuilt one layer
    down. Anchoring both on the hub's `now` is what keeps them together.
    """
    token, topic = "shared-token", "design-review"
    hub_now = 1_000_000.0
    skewed = hub_now + 240  # a peer whose clock runs four minutes fast

    assert rendezvous.next_slot(token, topic, hub_now) == rendezvous.next_slot(
        token, topic, hub_now
    ), "same clock, same slot"
    assert rendezvous.next_slot(token, topic, skewed) != rendezvous.next_slot(
        token, topic, hub_now
    ), "different clocks diverge — which is why the hub's is used"


# --- the escalating look ----------------------------------------------------


def test_the_backoff_covers_far_more_ground_than_uniform_polling():
    """Five uniform 25s polls cover two minutes. The same count of these
    covers twenty, which is the difference between catching a peer who is
    arriving and concluding the room is empty."""
    full = rendezvous.schedule(10_000)
    assert sum(full) >= 300, f"only covers {sum(full)}s"
    assert full == sorted(full), "gaps must grow, not jitter"


def test_a_small_budget_keeps_the_early_checks():
    """Truncated, not scaled: catching a peer who is already here is worth
    more than spreading thinly across whatever time was allowed."""
    short = rendezvous.schedule(20)
    assert short[0] == 0.0, "always look immediately"
    assert sum(short) <= 20


def test_a_zero_budget_still_looks_once():
    assert rendezvous.schedule(0) == [0.0]


# --- intent -----------------------------------------------------------------


def test_intent_round_trips():
    note = rendezvous.Intent(
        agent_id="alice", topic="design-review", want="a reviewer",
        since=100.0, looking_until=2000.0, next_slot=300.0,
    )
    again = rendezvous.Intent.from_json(json.loads(json.dumps(note.as_json())))
    assert again == note


def test_a_note_whose_author_gave_up_is_not_worth_answering():
    """Sending a newcomer to wait on somebody who stopped hours ago is the
    same wasted turn this whole command exists to prevent."""
    note = rendezvous.Intent(
        agent_id="alice", topic="t", want="", since=0.0,
        looking_until=1000.0, next_slot=0.0,
    )
    assert note.still_looking(999.0)
    assert not note.still_looking(1001.0)


def test_junk_on_the_board_is_ignored_rather_than_crashing():
    for junk in (None, "a string", {}, {"agent_id": ""}, [1, 2]):
        assert rendezvous.Intent.from_json(junk) is None


# --- end to end through the CLI ---------------------------------------------


@pytest.fixture
def cli_hub(monkeypatch):
    import switchboard.cli as cli_module

    with hub(workspace=WS) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        yield handle


def _run(args, capsys):
    code = main(["--url", BASE_URL, "-w", WS, "--json", "rendezvous", *args])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_the_first_arrival_leaves_a_note_and_reports_the_slot(cli_hub, capsys, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    out = _run(["design-review", "--want", "a reviewer", "--wait", "0"], capsys)
    assert out["met"] is False
    assert out["next_slot_in"] > 0


def test_a_later_arrival_finds_the_note_when_the_roster_is_empty(
    cli_hub, capsys, monkeypatch
):
    """The case the whole feature is for. Presence lapses in two minutes; the
    note lasts a day, so an agent arriving after the other has gone quiet still
    learns that somebody is looking for it."""
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _run(["design-review", "--want", "need a reviewer", "--wait", "0"], capsys)

    cli_hub.advance(600)  # alice's presence is long gone

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "bob")
    out = _run(["design-review", "--wait", "0"], capsys)
    assert out["met"] is True, "an empty roster must not read as an empty room"
    wants = [n["want"] for n in out["notes"]]
    assert "need a reviewer" in wants


def test_an_expired_note_is_not_presented_as_a_live_peer(cli_hub, capsys, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _run(["design-review", "--want", "gone", "--wait", "0", "--until", "60"], capsys)

    cli_hub.advance(600)

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "bob")
    out = _run(["design-review", "--wait", "0"], capsys)
    assert out["notes"] == [], "alice stopped looking; do not send bob to wait on her"


def test_an_agent_does_not_find_itself(cli_hub, capsys, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
    _run(["design-review", "--want", "hello", "--wait", "0"], capsys)
    out = _run(["design-review", "--wait", "0"], capsys)
    assert out["notes"] == []
