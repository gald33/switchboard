"""What a send says about the turn after it, on both surfaces.

An agent that sends a message is almost never launching a one-off. It sends
because it needs something back, and the answer arrives on the peer's schedule
— usually after this turn has ended, into an inbox no process is watching.
Nothing on the hub can report that: a message waiting unread and a message
being read are the same row from the hub's side.

So `say`, `dm` and `whisper` say it themselves, from the one fact that settles
it — a live `listener/<id>`. The tests here pin the two halves separately,
because they answer different questions and only one of them used to be
reported at all: whether the *recipient* is parked decides when they read it,
and whether the *sender* is decides whether their answer is read at all.
"""

from __future__ import annotations

import json

import pytest

from switchboard import mcp_server, rendezvous
from switchboard.cli import main
from switchboard.client import Identity
from switchboard.crypto import generate_key
from switchboard.mcp_server import Bridge, handle_request
from switchboard.testing import BASE_URL
from switchboard.testing import hub as make_hub
from switchboard.timing import TimingModel

WS = "conversation-ws"


def make_bridge(handle, agent_id: str = "sender") -> Bridge:
    """The same construction `test_mcp.py` uses. Copied rather than imported:
    `tests/` is not a package, and one helper is cheaper than making it one."""
    bridge = Bridge.__new__(Bridge)
    bridge.config = handle.client_config(agent_id=agent_id)
    bridge.identity = Identity(
        agent_id=agent_id, name="sender", kind="local", branch="feat/x", meta={}
    )
    bridge.client = handle.client(agent_id, agent_id=agent_id)
    bridge.timing = TimingModel(":memory:")
    bridge._registered = False
    bridge._rooms = {}
    return bridge


def call(bridge: Bridge, name: str, **arguments):
    response = handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result.get("isError", False)


@pytest.fixture(params=["plaintext", "encrypted"])
def cli_hub(monkeypatch, request):
    """Both kinds of room, because this reads the board by prefix.

    A prefix listing is the operation encryption is most likely to break
    silently: keys leave blinded, so a listing that works on a plaintext hub
    can return nothing at all on an encrypted one and report it as "nobody is
    parked" — the exact false negative this advice exists to avoid. The
    rendezvous suite learned that the expensive way; it is cheap to pin here.
    """
    import switchboard.cli as cli_module

    key = generate_key() if request.param == "encrypted" else None
    with make_hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        if key:
            monkeypatch.setenv("SWITCHBOARD_KEY", key)
        else:
            monkeypatch.delenv("SWITCHBOARD_KEY", raising=False)
        monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "sender")
        # Reset between tests: the actionable block is printed once per
        # process, which is once per command in real use but once per *suite*
        # in a test module that calls `main` in-process.
        monkeypatch.setattr(cli_module, "_LISTENER_ADVISED", False)
        yield handle


def _park(handle, agent_id: str) -> str:
    """What `listen` writes while it is parked, without the process.

    Keyed on the client's *wire* id rather than the local one, because that is
    what `listen` writes and what the roster prints. In an encrypted room the
    two differ, and keying this on the local id would have made every one of
    these tests pass against an implementation that can never find a real
    listener.
    """
    client = handle.client(agent_id, agent_id=agent_id)
    client.board_set(rendezvous.listener_key(client.agent_id), "parked", ttl=120)
    return client.agent_id


def _peer(handle, agent_id: str = "peer") -> str:
    """A registered peer, and the id an agent would actually address it by."""
    client = handle.client(agent_id, agent_id=agent_id)
    client.register(name=agent_id)
    return client.agent_id


def _dm(args, capsys) -> dict:
    assert main(["--url", BASE_URL, "-w", WS, "--json", "dm", *args]) == 0
    return json.loads(capsys.readouterr().out)


# --- the CLI ----------------------------------------------------------------


def test_a_dm_reports_both_ends_unparked(cli_hub, capsys):
    out = _dm([_peer(cli_hub), "are you there"], capsys)
    assert out["listener"] == {
        "you_parked": False,
        "peer_parked": False,
        "next": rendezvous.listener_advice(you_parked=False, peer_parked=False),
    }


def test_a_parked_recipient_is_reported_as_reachable_now(cli_hub, capsys):
    peer = _peer(cli_hub)
    _park(cli_hub, "peer")
    out = _dm([peer, "are you there"], capsys)
    assert out["listener"]["peer_parked"] is True
    # Still the half that matters most, and still false: their being parked
    # says nothing about whether their answer reaches anybody.
    assert out["listener"]["you_parked"] is False


def test_the_senders_own_listener_is_the_half_that_is_reported(cli_hub, capsys):
    _park(cli_hub, "sender")
    out = _dm([_peer(cli_hub), "are you there"], capsys)
    assert out["listener"]["you_parked"] is True
    assert "wakes you" in out["listener"]["next"]


def test_an_unparked_sender_gets_the_command_and_not_a_hint(cli_hub, capsys):
    """The failure this exists for, in the words an agent has to act on."""
    peer = _peer(cli_hub)
    assert main(["--url", BASE_URL, "-w", WS, "dm", peer, "are you there"]) == 0
    printed = capsys.readouterr().out
    assert "sent #" in printed
    assert "nothing is parked for you" in printed
    assert "switchboard listen --until forecast:p50" in printed


def test_a_parked_sender_is_told_so_and_not_told_to_park(cli_hub, capsys):
    _park(cli_hub, "sender")
    assert main(["--url", BASE_URL, "-w", WS, "dm", _peer(cli_hub), "hello"]) == 0
    printed = capsys.readouterr().out
    assert "wakes you" in printed
    assert "switchboard listen" not in printed


def test_the_block_is_printed_once_however_many_messages_a_turn_sends(
    cli_hub, capsys
):
    """A paragraph repeated four times is a paragraph skimmed past on the
    first. The per-end lines still print every time; the command does not."""
    peer = _peer(cli_hub)
    for _ in range(3):
        assert main(["--url", BASE_URL, "-w", WS, "dm", peer, "hello"]) == 0
    printed = capsys.readouterr().out
    assert printed.count("switchboard listen --until forecast:p50") == 1
    assert printed.count("no listener is parked for them") == 3


def test_quiet_says_nothing_and_asks_the_hub_nothing(cli_hub, capsys, monkeypatch):
    """`--quiet` is for scripts, and a script does not have a next turn."""
    import switchboard.cli as cli_module

    def refuse(*a, **k):
        raise AssertionError("a quiet send must not read the board")

    peer = _peer(cli_hub)
    monkeypatch.setattr(cli_module, "_listener_state", refuse)
    assert main(["--url", BASE_URL, "-w", WS, "--quiet", "dm", peer, "hi"]) == 0
    assert capsys.readouterr().out == ""


def test_a_board_read_that_fails_costs_the_advice_and_not_the_send(
    cli_hub, capsys, monkeypatch
):
    """The message is already accepted by the time this runs. Nothing about an
    annotation on it may turn a delivered message into a failed command."""
    peer = _peer(cli_hub)
    client_class = cli_hub.client_class()

    class Broken(client_class):
        def board_list(self, *a, **k):
            raise RuntimeError("hub said no")

    import switchboard.cli as cli_module

    monkeypatch.setattr(cli_module, "Client", Broken)
    assert main(["--url", BASE_URL, "-w", WS, "dm", peer, "hello"]) == 0
    printed = capsys.readouterr().out
    assert "sent #" in printed
    assert "switchboard listen" not in printed


def test_a_parked_sender_posting_to_a_channel_is_not_told_about_a_them(
    cli_hub, capsys
):
    _park(cli_hub, "sender")
    assert main(["--url", BASE_URL, "-w", WS, "say", "build", "hi"]) == 0
    printed = capsys.readouterr().out
    assert "a listener is parked for you" in printed
    assert "their" not in printed


def test_a_channel_post_reports_only_the_senders_own_end(cli_hub, capsys):
    """There is no single recipient to be parked, and inventing one would be
    the same false confidence the roster gives an agent in the wrong room."""
    assert main(["--url", BASE_URL, "-w", WS, "--json", "say", "build", "hi"]) == 0
    listener = json.loads(capsys.readouterr().out)["listener"]
    assert listener["you_parked"] is False
    assert "peer_parked" not in listener


def test_a_whisper_carries_it_too(cli_hub, capsys):
    # A whisper is sealed to the recipient, so unlike `dm` it needs a peer that
    # has actually been on the roster with an exchange key.
    peer = _peer(cli_hub)
    _park(cli_hub, "sender")
    assert main(["--url", BASE_URL, "-w", WS, "--json", "whisper", peer, "shh"]) == 0
    assert json.loads(capsys.readouterr().out)["listener"]["you_parked"] is True


# --- the MCP bridge, saying the same thing ----------------------------------


@pytest.fixture
def bridge():
    with make_hub(workspace=WS) as handle:
        yield make_bridge(handle, agent_id="sender"), handle


def test_the_mcp_dm_result_carries_the_same_sentence_as_the_cli(bridge):
    b, handle = bridge
    peer = _park(handle, "peer")
    out, err = call(b, "dm", to=peer, message="are you there")
    assert not err
    assert out["listener"] == {
        "you_parked": False,
        "peer_parked": True,
        "next": rendezvous.listener_advice(you_parked=False, peer_parked=True),
    }
    # The same sentence the CLI puts under `--json`, from the one source both
    # surfaces read, so an agent switching surfaces is told the same thing.
    assert out["listener"]["next"].startswith("A listener is parked for them")


def test_the_mcp_say_result_reports_only_the_senders_end(bridge):
    b, _ = bridge
    out, err = call(b, "say", channel="build", message="hi")
    assert not err
    assert out["listener"]["you_parked"] is False
    assert "peer_parked" not in out["listener"]


def test_every_sending_tool_tells_an_agent_the_turn_is_not_over(bridge):
    """The description is the whole interface an agent has to a tool, so the
    advice has to be *in* it rather than only in the result it returns."""
    sending = {t["name"]: t["description"] for t in mcp_server.TOOLS}
    for name in ("say", "dm", "whisper"):
        assert "listener" in sending[name]
        assert "switchboard listen --until forecast:p50" in sending[name]


def test_a_broken_board_read_does_not_break_an_mcp_send(bridge, monkeypatch):
    b, _ = bridge
    monkeypatch.setattr(
        b.client, "board_list",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hub said no")),
    )
    out, err = call(b, "dm", to="peer", message="hello")
    assert not err
    assert out["sent"] is True
    assert out["listener"] == {}


# --- the advice itself ------------------------------------------------------


def test_the_unparked_sender_is_told_a_consequence_not_a_suggestion():
    text = rendezvous.listener_advice(you_parked=False)
    assert "no process is watching" in text
    assert "switchboard listen --until forecast:p50" in text


def test_the_recipients_half_is_omitted_when_there_is_no_recipient():
    """A channel post has no single recipient, so nothing may be claimed about
    one — including by a pronoun left over from the two-ended wording."""
    solo = rendezvous.listener_advice(you_parked=True)
    assert "them" not in solo and "their" not in solo
    assert solo == "A listener is parked for you, so an answer wakes you."


# --- a recipient on do-not-disturb -------------------------------------------
#
# "Parked" used to mean "answers in seconds", and a listener parked with
# `listen --type urgent` breaks that on purpose: it is reachable, but not for
# this. The heartbeat declares the terms, and the sender is told them rather
# than left to find out by waiting.


def _park_dnd(handle, agent_id: str = "peer") -> str:
    client = handle.client(agent_id, agent_id=agent_id)
    client.board_set(rendezvous.listener_key(client.agent_id), {
        "waiting_on": "inbox",
        "dnd": {"wakes_on_types": ["urgent"],
                "reads_everything_else_at": "2026-09-23T12:00:00+00:00",
                "dms_held": True, "skipped": 0},
    }, ttl=120)
    return client.agent_id


def test_an_ordinary_dm_to_a_busy_agent_says_when_it_will_be_read(cli_hub, capsys):
    peer = _peer(cli_hub)
    _park_dnd(cli_hub)
    out = _dm([peer, "whenever you get a chance"], capsys)
    listener = out["listener"]
    assert listener["peer_parked"] is True
    assert listener["peer_dnd"]["wakes_on_types"] == ["urgent"]
    assert "do-not-disturb until 2026-09-23T12:00:00+00:00" in listener["next"]
    assert "kept for them" in listener["next"]


def test_an_urgent_dm_to_a_busy_agent_is_told_it_wakes_them(cli_hub, capsys):
    peer = _peer(cli_hub)
    _park_dnd(cli_hub)
    out = _dm([peer, "prod is down", "--type", "urgent"], capsys)
    assert "which wakes them" in out["listener"]["next"]


def test_the_mcp_dm_result_reports_do_not_disturb_too(bridge):
    b, handle = bridge
    peer = _park_dnd(handle, "peer")
    out, err = call(b, "dm", to=peer, message="later is fine")
    assert not err
    assert out["listener"]["peer_dnd"]["wakes_on_types"] == ["urgent"]
    assert out["listener"]["next"] == rendezvous.listener_advice(
        you_parked=False, peer_parked=True, peer_dnd=out["listener"]["peer_dnd"],
        sent_type="note")
    urgent, _ = call(b, "dm", to=peer, message="now", type="urgent")
    assert "which wakes them" in urgent["listener"]["next"]
