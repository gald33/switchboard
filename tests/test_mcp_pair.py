"""Pairing through the MCP bridge: five tools, and one handle for all the rest.

The point of pairing on this surface is the transcript. A model's
conversation is a paper trail, and `keygen` or an invite puts a room's key in
it. Here only the code ever appears — the short-lived part, the one that is
worth nothing once redeemed — and every other tool reaches the pair's room by
name, as `room="pair:<name>"`, with the key resolved on the machine.

So these tests are mostly about what this surface can get wrong quietly: a
key in a result, a heal that leaves the agent talking in the room it
retired, a leaked code reported as a broken tool, a warning that never
reaches the model, a hosted operator's disk.

Two bridges are two machines: each has its own pair store and its own
default room, and they share one in-process hub whose clock the tests move.
The bridges read that clock too, because a pairing's deadlines and the
bridge's rechecks are compared against the hub's timestamps.
"""

from __future__ import annotations

import json

import pytest

from switchboard import mcp_server, pairing
from switchboard.client import Identity
from switchboard.crypto import generate_key
from switchboard.invite import Invite
from switchboard.mcp_server import PAIR_RECHECK, TOOLS, Bridge, handle_request
from switchboard.pairing import PairStore
from switchboard.testing import hub as make_hub
from switchboard.timing import TimingModel, unwrap_forecast

PAIR_TOOLS = ("pair_start", "pair_accept", "pair_status", "pair_heal", "pair_forget")


@pytest.fixture
def h(monkeypatch):
    with make_hub() as handle:
        # The bridge builds its handshake, pair-room and work-room clients
        # itself, from the module-level `Client`. Bind that to this hub.
        monkeypatch.setattr(mcp_server, "Client", handle.client_class())
        yield handle


def machine(h, tmp_path, label: str, *, kind: str = "local") -> Bridge:
    """A bridge on a machine of its own: its own pair store, its own team room.

    Built the way `test_mcp.make_bridge` builds one — with `__new__`, which is
    why every piece of pairing state on the bridge has a class-level default.
    """
    bridge = Bridge.__new__(Bridge)
    bridge.config = h.client_config(workspace=f"team-{label}", agent_id=label)
    bridge.identity = Identity(agent_id=label, name=f"{label} agent", kind=kind,
                               branch="main", meta={})
    bridge.client = h.client(label, workspace=f"team-{label}", agent_id=label)
    bridge.timing = TimingModel(":memory:")
    bridge._registered = False
    bridge._rooms = {}
    bridge._pair_store = PairStore(tmp_path / label / "pairs", env={})
    bridge._clock = lambda: h.now
    return bridge


@pytest.fixture
def laptop(h, tmp_path):
    return machine(h, tmp_path, "laptop")


@pytest.fixture
def cloud(h, tmp_path):
    return machine(h, tmp_path, "cloud", kind="cloud")


def call(bridge: Bridge, tool: str, /, **arguments):
    """`test_mcp.call`, with the tool positional-only: pair_heal and
    pair_forget take an argument called `name`."""
    response = handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result.get("isError", False)


def paired(laptop: Bridge, cloud: Bridge, **start) -> str:
    """A pair made with nothing but tool calls; returns its handle."""
    started, error = call(laptop, "pair_start", **start)
    assert not error, started
    accepted, _ = call(cloud, "pair_accept", code=started["code"])
    assert accepted["paired"], accepted
    status, _ = call(laptop, "pair_status")
    assert [row["state"] for row in status["pairs"]] == ["paired"], status
    return started["room"]


def work_room_bodies(h, bridge: Bridge, name: str, generation: int, channel: str) -> list:
    """What a work room holds, read straight off the hub — not through the
    handle under test, which is the thing that could be pointing elsewhere."""
    record = bridge._pair_store.get(name)
    room = pairing.work_room(record.pair_secret, generation)
    reader = h.client(f"reader-{generation}", workspace=room.workspace, key=room.key,
                      write_key="")
    return [unwrap_forecast(m["body"])[0] for m in reader.history(channel)]


# --- the round trip -------------------------------------------------------------


def test_two_bridges_pair_through_tool_calls_alone(laptop, cloud):
    started, _ = call(laptop, "pair_start", note="laptop")
    assert started["code"].startswith("swbp1_")
    assert started["state"] == "pending" and started["verifiable"] is True
    assert started["room"] == f"pair:{started['name']}"

    accepted, error = call(cloud, "pair_accept", code=started["code"])
    assert error is False and accepted["paired"] is True
    assert accepted["name"] == started["name"], "both sides should arrive at one name"
    assert accepted["room"] == started["room"]
    assert (accepted["state"], accepted["generation"]) == ("unconfirmed", 0)

    mine, _ = call(laptop, "pair_status")
    theirs, _ = call(cloud, "pair_status")
    [here], [there] = mine["pairs"], theirs["pairs"]
    assert here["state"] == there["state"] == "paired"
    assert (here["role"], there["role"]) == ("initiator", "responder")
    assert here["sas"] and here["sas"] == there["sas"], \
        "the SAS is only worth comparing if both machines show the same one"


def test_the_initiator_hears_of_the_answer_on_its_next_call(laptop, cloud):
    """It handed out a code and moved on; nothing promises it calls pair_status.
    The next ordinary tool call is where it finds out, once — and a pairing
    still waiting is not news, so it says nothing until there is some."""
    started, _ = call(laptop, "pair_start")
    quiet, _ = call(laptop, "whoami")
    assert "pairings" not in quiet

    call(cloud, "pair_accept", code=started["code"])
    laptop._pairings_checked_at = float("-inf")   # as if the recheck had come round
    news, _ = call(laptop, "whoami")
    [row] = news["pairings"]
    assert (row["name"], row["state"], row["room"]) == (
        started["name"], "paired", started["room"])
    assert row["sas"]

    again, _ = call(laptop, "whoami")
    assert "pairings" not in again, "a pairing is news once, not on every result"


def test_a_code_nobody_answers_costs_one_check_per_recheck(laptop, monkeypatch, h):
    """Unanswered, a code stays collectable for half a day. Checking on every
    tool call would put a hub round trip in front of each one for all of it."""
    calls = []
    real = pairing.collect
    monkeypatch.setattr(pairing, "collect", lambda *a, **k: calls.append(1) or real(*a, **k))

    for _ in range(3):
        call(laptop, "roster")
    assert calls == [], "with nothing pending there is nothing to ask the hub"

    call(laptop, "pair_start")
    for _ in range(5):
        call(laptop, "roster")
    assert len(calls) == 1
    h.advance(PAIR_RECHECK)
    call(laptop, "roster")
    assert len(calls) == 2


# --- the handle ---------------------------------------------------------------------


def test_the_handle_reaches_the_work_room_and_leaves_the_default_alone(laptop, cloud, h):
    """The property the handle exists for, and the one a swap could quietly
    break: work lands where it was addressed, and only there."""
    room = paired(laptop, cloud)
    name = pairing.parse_handle(room)

    sent, _ = call(laptop, "say", channel="build", message="for the pair", room=room)
    call(laptop, "say", channel="build", message="for my own room")

    assert sent["pair"] == {"name": name, "generation": 0, "state": "paired",
                            "warnings": []}
    there, _ = call(cloud, "history", channel="build", room=room)
    assert [m["body"] for m in there["messages"]] == ["for the pair"]
    assert there["pair"]["generation"] == 0
    here, _ = call(laptop, "history", channel="build")
    assert [m["body"] for m in here["messages"]] == ["for my own room"]
    theirs, _ = call(cloud, "history", channel="build")
    assert theirs["messages"] == [], "the pair's room is not the cloud's own room"
    assert work_room_bodies(h, laptop, name, 0, "build") == ["for the pair"]


def test_a_warning_from_resolving_the_pair_reaches_the_model(laptop, cloud):
    """`resolve` says what a model must pass on — here, that nobody reads the
    room until the other side collects. A result that dropped it would look
    exactly like a healthy pair."""
    started, _ = call(laptop, "pair_start")
    call(cloud, "pair_accept", code=started["code"])

    early, _ = call(cloud, "say", channel="build", message="already here",
                    room=started["room"])
    assert early["pair"]["state"] == "unconfirmed"
    assert any("unconfirmed" in w for w in early["pair"]["warnings"])

    call(laptop, "pair_status")
    found, _ = call(laptop, "history", channel="build", room=started["room"])
    assert [m["body"] for m in found["messages"]] == ["already here"]
    call(cloud, "pair_status")
    later, _ = call(cloud, "roster", room=started["room"])
    assert later["pair"]["state"] == "paired" and later["pair"]["warnings"] == []


def test_the_handle_is_resolved_at_most_once_per_recheck(laptop, cloud, monkeypatch, h):
    """A resolve reads the pair room; an agent makes many calls a minute. The
    recheck bounds that — and any pair_* tool ends it early, because it may
    just have changed what the handle should resolve to."""
    room = paired(laptop, cloud)
    calls = []
    real = pairing.resolve
    monkeypatch.setattr(pairing, "resolve", lambda *a, **k: calls.append(1) or real(*a, **k))

    for _ in range(4):
        call(laptop, "roster", room=room)
    assert len(calls) == 1
    call(laptop, "pair_start", name="another")
    call(laptop, "roster", room=room)
    assert len(calls) == 2, "a pair_* tool should make the next use re-resolve"
    h.advance(PAIR_RECHECK)
    call(laptop, "roster", room=room)
    assert len(calls) == 3


def test_a_heal_moves_the_handle_and_retires_the_old_rooms_client(laptop, cloud, h):
    room = paired(laptop, cloud)
    name = pairing.parse_handle(room)
    call(laptop, "claim", resource="db/migrations", room=room)

    healed, error = call(laptop, "pair_heal", name=name, reason="work key pasted in a log")
    assert error is False
    assert (healed["from"], healed["to"], healed["room"]) == (0, 1, room)
    assert len(healed["left_behind"]) == 1, "the lease held in the old room is reported"
    assert "did not move" in healed["next"]
    assert (name, 0) not in laptop._pair_clients, \
        "a client for the retired room must not survive the heal that retired it"

    after, _ = call(laptop, "say", channel="build", message="after the heal", room=room)
    assert after["pair"]["generation"] == 1
    assert any("not moved to work room 1" in w for w in after["pair"]["warnings"])
    assert work_room_bodies(h, laptop, name, 1, "build") == ["after the heal"]
    assert "after the heal" not in work_room_bodies(h, laptop, name, 0, "build")


def test_a_heal_by_the_other_side_is_followed_here(laptop, cloud, h):
    """The bridge holds a client for the room it last resolved. The other
    side's heal is news only the pair room carries, so the cache has to be
    keyed by generation — or this side keeps talking in the exposed room."""
    room = paired(laptop, cloud)
    name = pairing.parse_handle(room)
    before, _ = call(laptop, "roster", room=room)
    assert before["pair"]["generation"] == 0

    call(cloud, "pair_heal", name=name, reason="invite for the work room leaked")
    h.advance(PAIR_RECHECK)
    followed, _ = call(laptop, "say", channel="build", message="followed", room=room)

    assert followed["pair"]["generation"] == 1
    assert any("leaked" in w for w in followed["pair"]["warnings"]), \
        "the other side's reason should reach this side's model"
    assert set(laptop._pair_clients) == {(name, 1)}
    assert work_room_bodies(h, laptop, name, 1, "build") == ["followed"]


def test_a_pair_the_other_side_ended_stops_reaching_its_room(laptop, cloud, h):
    room = paired(laptop, cloud)
    name = pairing.parse_handle(room)
    call(cloud, "roster", room=room)

    gone, _ = call(laptop, "pair_forget", name=name)
    assert gone == {"name": name, "forgotten": True, "peer_told": True, "warnings": []}

    h.advance(PAIR_RECHECK)
    refused, error = call(cloud, "say", channel="build", message="anyone?", room=room)
    assert error is True
    assert (refused["error"], refused["state"]) == ("pairing", "ended_by_peer")
    assert "pair forget" in refused["message"]
    assert not any(key[0] == name for key in cloud._pair_clients), \
        "the ended pair's client should be closed, not kept for the next call"
    cleared, _ = call(cloud, "pair_forget", name=name)
    assert cleared["forgotten"] is True


# --- refusals ---------------------------------------------------------------------


def test_a_code_somebody_else_answered_is_an_answer_not_an_error(laptop, cloud, h, tmp_path):
    """The leaked-code signal. It must read as something to tell the human —
    not as a tool that broke, which a model retries."""
    stranger = machine(h, tmp_path, "stranger")
    started, _ = call(laptop, "pair_start")
    call(cloud, "pair_accept", code=started["code"])

    late, error = call(stranger, "pair_accept", code=started["code"])
    assert error is False
    assert late["paired"] is False and late["state"] == "already_redeemed"
    assert "leaked" in late["next"] and "Do not retry" in late["next"]
    assert stranger._pair_store.names() == [], "the loser keeps nothing"


def test_a_refused_code_says_what_it_is_instead(cloud):
    invite = Invite(url="https://hub.example", workspace="w", key=generate_key()).encode()
    refused, error = call(cloud, "pair_accept", code=invite)
    assert error is False and refused["paired"] is False
    assert "room invite" in refused["reason"] and "join" in refused["reason"]


def test_a_pairing_refusal_is_its_own_error_not_a_broken_tool(laptop):
    """`unknown_tool` sends a model looking for a tool that does not exist, and
    `hub_error` sends it to the hub. A pairing refusal is neither."""
    unknown, error = call(laptop, "roster", room="pair:nobody")
    assert error is True
    assert (unknown["error"], unknown["state"]) == ("pairing", "unknown")
    assert "pair status" in unknown["message"]

    heal, _ = call(laptop, "pair_heal", name="nobody")
    assert heal["error"] == "pairing"
    short, _ = call(laptop, "pair_start", ttl=5)
    assert short["error"] == "pairing" and "--ttl" in short["message"]
    bad, _ = call(laptop, "roster", room="pair:Not A Name")
    assert bad["error"] == "pairing" and "not a usable pair name" in bad["message"]


def test_join_room_refuses_a_code_and_leaves_it_unspent(laptop, cloud):
    """join_room promises an in-memory room for this session; redeeming writes
    a pair secret to disk. So it refuses — and refusing must not spend the
    code, or the human has to go back for another one."""
    started, _ = call(laptop, "pair_start")

    refused, error = call(cloud, "join_room", invite=started["code"])
    assert error is False and refused["joined"] is False
    assert "pairing code" in refused["error"]
    assert "pair_accept" in refused["next"]
    assert cloud._rooms == {}

    accepted, _ = call(cloud, "pair_accept", code=started["code"])
    assert accepted["paired"] is True


def test_starting_in_a_cloud_session_says_which_side_should(cloud):
    started, _ = call(cloud, "pair_start")
    assert "cloud container" in started["warning"]
    assert "pair_start on the durable machine" in started["warning"]


# --- the transcript ---------------------------------------------------------------


def test_no_result_carries_a_pair_secret_or_a_work_room_key(laptop, cloud, h):
    """The reason to pair over MCP at all. A transcript is a paper trail, and
    the only pairing string allowed into it is the code — once, from the tool
    that made it."""
    started, _ = call(laptop, "pair_start", name="review")
    pending = laptop._pair_store.pending("review")
    results = [call(laptop, "whoami")[0]]
    room = started["room"]
    results += [
        call(cloud, "pair_accept", code=started["code"])[0],
        call(laptop, "pair_status")[0],
        call(cloud, "pair_status")[0],
        call(laptop, "say", channel="build", message="hi", room=room)[0],
        call(cloud, "history", channel="build", room=room)[0],
        call(cloud, "whoami", room=room)[0],
        call(cloud, "rendezvous", want="a reviewer", room=room)[0],
        call(laptop, "pair_heal", name="review")[0],
        call(cloud, "pair_status")[0],
        call(laptop, "board_list", room=room)[0],
    ]
    record = laptop._pair_store.get("review")
    secrets = [record.secret, pending.secret, pending.private, record.transcript]
    for spec in (pairing.pair_room(record.pair_secret),
                 pairing.work_room(record.pair_secret, 0),
                 pairing.work_room(record.pair_secret, 1)):
        secrets += [spec.key, spec.write_key]
    results += [call(laptop, "pair_forget", name="review")[0]]
    h.advance(PAIR_RECHECK)
    results += [call(cloud, "roster", room=room)[0]]

    for payload in results:
        text = json.dumps(payload)
        assert started["code"] not in text, payload
        for value in secrets:
            assert value not in text, payload
    assert not any(value in json.dumps(started) for value in secrets)


# --- the schema ----------------------------------------------------------------------


def test_the_pairing_tools_take_no_room_and_declare_what_they_change():
    """Each picks its own rooms, so `room` on one would read as if it did
    something. And hosts act on the hints: a pair_status marked read-only
    would be called as freely as roster, and it completes pairings."""
    tools = {t["name"]: t for t in TOOLS}
    assert [t["name"] for t in TOOLS][-len(PAIR_TOOLS):] == list(PAIR_TOOLS)
    for name in PAIR_TOOLS:
        assert "room" not in tools[name]["inputSchema"]["properties"], name
        assert tools[name]["annotations"]["readOnlyHint"] is False, name
    destructive = {n for n in PAIR_TOOLS if tools[n]["annotations"]["destructiveHint"]}
    assert destructive == {"pair_accept", "pair_heal", "pair_forget"}
    assert tools["pair_accept"]["inputSchema"]["required"] == ["code"]


def test_the_room_parameter_documents_the_pair_handle():
    """An agent that cannot discover the handle will ask for the key instead."""
    roster = next(t for t in TOOLS if t["name"] == "roster")
    assert "pair:<name>" in roster["inputSchema"]["properties"]["room"]["description"]


# --- a hosted bridge ------------------------------------------------------------------


def test_a_hosted_bridge_neither_pairs_nor_resolves_a_pair_handle(h, tmp_path, monkeypatch):
    """A pair's secret lives on two machines; a hosted bridge is a third, and
    its operator holding a code could answer it in place of the peer. The
    front door already keeps the handle away from it — this is the bridge
    refusing on its own, and keeping nothing on its operator's disk."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SWITCHBOARD_PAIRS", raising=False)
    blob = Invite(url=h.url, workspace="hosted-room", token=h.token,
                  key=generate_key(), note="ChatGPT (test)").encode()
    bridge = mcp_server.hosted_bridge(blob, mcp_server.hosted_relay("Example Operator"))

    assert not set(PAIR_TOOLS) & {t["name"] for t in bridge.tools()}
    started, error = call(bridge, "pair_start")
    assert error is True and "hosted bridge" in started["detail"]
    routed, error = call(bridge, "roster", room="pair:review")
    assert error is True
    assert (routed["error"], routed["state"]) == ("pairing", "withheld")
    assert "hosted bridge" in routed["message"]
    plain, error = call(bridge, "roster")
    assert error is False and "pairings" not in plain

    assert bridge._pair_store is None, "a hosted bridge never opens a pair store"
    assert list(home.iterdir()) == []
    bridge.close()
