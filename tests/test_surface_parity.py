"""What each surface owes an agent: the same writes, and the same warning.

Two asymmetries, reported from opposite directions. A downstream project found
that only MCP tells an agent something is waiting (`unread_dms`), so an agent on
the CLI could be whispered at and never find out — in a real game, an entrant
wrote a plan three times, perceived no reply, and lost the round to it. Auditing
the other direction found MCP to be the *thin* surface for writes: no way to
subscribe to a channel, delete a board entry, renew one lease, or leave.

The tests that matter here are the negative ones. A round trip passes just as
happily if the count is a hardcoded zero, and a `subscribe` tool passes if it
sets a field nobody reads — so each is checked against the behaviour it exists
to produce, not against its own return value.
"""

from __future__ import annotations

import pytest

from switchboard.crypto import generate_key
from switchboard.testing import hub as make_hub

WS = "parity-ws"


@pytest.fixture
def key():
    return generate_key()


# --- the count rides along on responses the client already gets --------------

def test_posting_reports_a_whisper_waiting(key):
    """The reporter's first test, adopted as written: non-zero with one
    waiting, zero with none."""
    with make_hub(workspace=WS, key=key) as h:
        alice, bob = h.client("alice"), h.client("bob")
        alice.register(name="alice")
        bob.register(name="bob")
        alice.agents()
        bob.agents()

        bob.post("general", "nothing for alice yet")
        assert bob.unread_dms == 0

        alice.whisper(bob.agent_id, "not settled: your key took no seat")
        bob.post("general", "PRODUCE salt=0.7")
        assert bob.unread_dms == 1


def test_reading_the_count_does_not_consume_the_message(key):
    """Their second and third tests: the count is not a read, and the message
    is still there afterwards with its unread state intact."""
    with make_hub(workspace=WS, key=key) as h:
        alice, bob = h.client("alice"), h.client("bob")
        alice.register(name="alice")
        bob.register(name="bob")
        alice.agents()
        bob.agents()
        alice.whisper(bob.agent_id, "a slice of what the sealing was for")

        bob.post("general", "first")
        assert bob.unread_dms == 1
        bob.post("general", "second")
        assert bob.unread_dms == 1, "posting twice consumed the message"

        [got] = [m for m in bob.inbox() if m.get("type") == "whisper"]
        assert got["body"] == "a slice of what the sealing was for"


def test_the_count_drops_once_the_message_is_actually_read(key):
    with make_hub(workspace=WS, key=key) as h:
        alice, bob = h.client("alice"), h.client("bob")
        alice.register(name="alice")
        bob.register(name="bob")
        alice.agents()
        bob.agents()
        alice.whisper(bob.agent_id, "read me")

        bob.post("general", "before")
        assert bob.unread_dms == 1
        bob.inbox()
        assert bob.unread_dms == 0, "inbox reports what is still waiting, not what it just gave you"


def test_an_unrelated_command_does_not_disturb_a_held_lease():
    """Their fourth test. `_touch()` refuses to renew leases as a side effect;
    nothing added here may start.

    Plaintext workspace on purpose: an encrypted one blinds lease resources, so
    asserting on the name would be asserting on an HMAC and would pass whether
    or not the right lease survived.
    """
    with make_hub(workspace=WS) as h:
        alice = h.client("alice")
        alice.register(name="alice")
        alice.acquire("backend/migrations")

        alice.post("general", "unrelated")
        held = alice.leases()
        assert [le["resource"] for le in held] == ["backend/migrations"]


# --- the writes each surface was missing -------------------------------------

def test_an_mcp_agent_can_subscribe_and_then_sees_the_room(key):
    """The gap that made an MCP agent uncontactable: registration passed no
    channels and no tool set them, so `inbox` returned silence in a busy room
    while `roster` listed the peers.
    """
    with make_hub(workspace=WS, key=key) as h:
        speaker = h.client("speaker")
        speaker.register(name="speaker")
        listener = h.client("listener")
        listener.register(name="listener")          # no channels: the old default

        speaker.post("general", "anyone there?")
        assert listener.inbox() == [], "precondition: unsubscribed agents hear nothing"

        listener.register(name="listener", channels=["general"])
        speaker.post("general", "second try")
        bodies = [m["body"] for m in listener.inbox()]
        assert "second try" in bodies


def test_subscribing_adds_rather_than_replacing():
    """The shape this shipped with, and the argument for it.

    A wrong add is noise: loud, immediate, self-correcting. A wrong replace is
    silence — an agent names one channel, loses the others, and its inbox stops
    showing things with no error anywhere. Silence is what this tool exists to
    end, so it must not be how the tool fails.
    """
    from switchboard.mcp_server import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge._registered = True          # nothing to re-register against here
    bridge._ensure_registered = lambda: None
    bridge._touch = lambda: 0

    first = bridge.subscribe(["general"])
    assert first["subscribed"] == ["general"]

    second = bridge.subscribe(["build"])
    assert second["subscribed"] == ["general", "build"], "subscribing dropped a channel"
    assert second["added"] == ["build"]

    again = bridge.subscribe(["build"])
    assert again["subscribed"] == ["general", "build"]
    assert again["added"] == [], "adding what you already have should be a no-op"

    dropped = bridge.unsubscribe(["general"])
    assert dropped["subscribed"] == ["build"]
    assert dropped["removed"] == ["general"]

    absent = bridge.unsubscribe(["never-had-it"])
    assert absent["subscribed"] == ["build"], "dropping what you lack should be a no-op"


def test_subscriptions_survive_re_registration(key):
    """A presence lapse re-registers. If that dropped the subscriptions, an
    agent would go deaf on the one call that looks like nothing happened."""
    with make_hub(workspace=WS, key=key) as h:
        speaker = h.client("speaker")
        speaker.register(name="speaker")
        listener = h.client("listener")
        listener.register(name="listener", channels=["general"])
        listener.register(name="listener", channels=["general"])   # as _ensure_registered does

        speaker.post("general", "still listening?")
        assert "still listening?" in [m["body"] for m in listener.inbox()]


def test_a_board_entry_can_be_deleted_not_only_overwritten(key):
    with make_hub(workspace=WS, key=key) as h:
        agent = h.client("agent")
        agent.register(name="agent")
        agent.board_set("migration/plan", {"step": 1})
        assert agent.board_get("migration/plan") is not None

        assert agent.board_delete("migration/plan") is True
        assert agent.board_get("migration/plan") is None
        assert agent.board_delete("migration/plan") is False


def test_renewing_one_lease_leaves_the_others_to_lapse():
    """The distinction `checkin` cannot express: still working one resource,
    content to let the rest go to whoever is waiting. Plaintext for the same
    reason as above — the assertion is on which resource, by name."""
    with make_hub(workspace=WS) as h:
        agent = h.client("agent")
        agent.register(name="agent")
        agent.acquire("kept", ttl=60)
        agent.acquire("dropped", ttl=60)

        renewed = agent.renew("kept", ttl=600)
        by_resource = {le["resource"]: le for le in agent.leases()}
        assert renewed["expires_in"] > by_resource["dropped"]["expires_in"]


def test_leaving_takes_the_agent_off_the_roster(key):
    with make_hub(workspace=WS, key=key) as h:
        stayer, goer = h.client("stayer"), h.client("goer")
        stayer.register(name="stayer")
        goer.register(name="goer")
        assert goer.agent_id in [a["agent_id"] for a in stayer.agents()]

        assert goer.deregister() is True
        assert goer.agent_id not in [a["agent_id"] for a in stayer.agents()]


# --- presence lifetime, which only MCP could not state -----------------------

def test_an_mcp_agent_can_state_its_own_presence_lifetime():
    """The 120s default suits an agent that calls often. A turn-based agent on
    a ten-minute loop drops off the roster between turns, and a peer who cannot
    see it there cannot whisper to it — `whisper` needs an exchange key learned
    from the roster. Every other surface could already say so; MCP could not.
    """
    from switchboard.mcp_server import Bridge

    seen: dict[str, object] = {}

    class _Client:
        def heartbeat(self, **kw):
            seen.update(kw)
            return {"agent": {}, "leases": [], "unread_dms": 0}

        def inbox(self, **kw):
            return []

    bridge = Bridge.__new__(Bridge)
    bridge.client = _Client()
    bridge._registered = True
    bridge._ensure_registered = lambda: None
    bridge._note_look = lambda: None
    bridge._declare = lambda *a, **k: None

    bridge.checkin(ttl=900)
    assert seen["ttl"] == 900, "the ttl an agent asked for never reached the hub"

    # Remembered: a later check-in that says nothing must not silently revert
    # to the default, because presence lapsing re-registers and that is exactly
    # the call that looks like nothing happened.
    seen.clear()
    bridge.checkin()
    assert seen["ttl"] == 900, "the agent's stated cadence was forgotten"

    seen.clear()
    bridge.checkin(back_in=300)
    assert seen["back_in"] == 300, "back_in never reached the hub"
