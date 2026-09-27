"""The hosted bridge (`switchboard-mcp --http --hosted`) and what it discloses.

A hosted bridge serves many agents, each built from the invite in its URL, so
that someone with only a ChatGPT account can join a room. The price is that
whoever runs it can read every room sent to it — and these tests hold the two
promises that make that price honest: nothing is taken from the operator's
environment or written to its disk, and every roster the bridge reaches says
who can read the room.

Over a real socket, against a real hub, for the reason `test_mcp_http.py`
gives. The bridge's clients reach the hub in-process through the hub's own
`Client` subclass, which is the one thing swapped.
"""

from __future__ import annotations

import json

import pytest
from test_mcp import call, make_bridge
from test_mcp_http import rpc, send

from switchboard import mcp_server
from switchboard.cli import main as cli_main
from switchboard.client import relay_of
from switchboard.crypto import generate_key
from switchboard.invite import Invite
from switchboard.mcp_server import (
    HOSTED_WITHHELD,
    HostedBridges,
    hosted_relay,
    make_hosted_server,
)
from switchboard.testing import BASE_URL
from switchboard.testing import hub as make_hub

WS = "hosted-ws"
OPERATOR = "Example Operator <ops@example.com>"


@pytest.fixture
def hub(monkeypatch):
    with make_hub(workspace=WS, key=generate_key()) as handle:
        monkeypatch.setattr(mcp_server, "Client", handle.client_class())
        yield handle


def invite_for(hub, note: str = "ChatGPT (test)", **overrides) -> str:
    fields = dict(url=hub.url, workspace=hub.workspace, token=hub.token, key=hub.key,
                  note=note)
    fields.update(overrides)
    return Invite(**fields).encode()


@pytest.fixture
def hosted(hub):
    bridges = HostedBridges(hosted_relay(OPERATOR, "https://bridge.example"),
                            hubs=[hub.url])
    server = make_hosted_server(bridges, "127.0.0.1", 0)
    yield server, bridges
    server.server_close()
    bridges.close()


def tool(server, blob: str, name: str, **arguments):
    response = send(server, path=f"/mcp/{blob}", body=rpc(
        "tools/call", name=name, arguments=arguments))
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


# --- the invite is the agent ------------------------------------------------


def test_an_invite_url_is_a_working_agent_in_that_room(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub)
    payload, is_error = tool(server, blob, "claim", resource="docs/chatgpt.md")
    assert not is_error and payload["acquired"] is True
    # Held on the real hub, in the room the invite names: an agent on its own
    # machine, holding the same key, is shut out of it.
    theirs, _ = call(make_bridge(hub, "laptop"), "claim", resource="docs/chatgpt.md")
    assert theirs["acquired"] is False


def test_reconnecting_with_the_same_invite_is_the_same_agent(hub, hosted):
    server, bridges = hosted
    blob = invite_for(hub)
    first, _ = tool(server, blob, "whoami")
    again, _ = tool(server, blob, "whoami")
    assert first["agent_id"] == again["agent_id"]
    assert len(bridges) == 1


def test_two_invites_are_two_agents(hub, hosted):
    server, bridges = hosted
    one, _ = tool(server, invite_for(hub, note="alice"), "whoami")
    two, _ = tool(server, invite_for(hub, note="bob"), "whoami")
    assert one["agent_id"] != two["agent_id"]
    assert len(bridges) == 2


def test_the_agent_id_does_not_carry_the_invite(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub)
    payload, _ = tool(server, blob, "whoami")
    assert blob not in json.dumps(payload)
    assert hub.key not in json.dumps(payload)


def test_an_invite_that_leaves_its_key_out_is_refused(hub, hosted, monkeypatch):
    # The operator's own environment must never complete somebody's invite.
    monkeypatch.setenv("SWITCHBOARD_KEY", hub.key)
    server, _ = hosted
    blob = invite_for(hub, key=None, key_id="team")
    response = send(server, path=f"/mcp/{blob}", body=rpc("tools/list"))
    assert response.status_code == 400
    assert "switchboard invite" in response.json()["detail"]


def test_an_invite_for_another_hub_is_refused(hub, hosted):
    # Or an invite would be a way to make the server fetch anything it can
    # reach — a cloud metadata endpoint, a private address — on request.
    server, bridges = hosted
    blob = invite_for(hub, url="http://169.254.169.254/latest")
    response = send(server, path=f"/mcp/{blob}", body=rpc("tools/list"))
    assert response.status_code == 400
    assert "serves rooms on" in response.json()["detail"]
    assert len(bridges) == 0


def test_the_managed_hub_is_the_default_and_only_default():
    from switchboard.config import MANAGED_HUB_URL

    bridges = HostedBridges(hosted_relay(OPERATOR))
    assert bridges.hubs == {MANAGED_HUB_URL.rstrip("/").lower()}


def test_hubs_come_from_flags_or_the_environment(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_BRIDGE_HUBS", "https://a.example, https://b.example")
    args = mcp_server._parse_args(["--http", "--hosted", "--operator", "x"])
    assert args.hubs == ["https://a.example", "https://b.example"]
    monkeypatch.delenv("SWITCHBOARD_BRIDGE_HUBS")
    args = mcp_server._parse_args(["--http", "--hosted", "--operator", "x",
                                   "--hub", "https://c.example"])
    assert args.hubs == ["https://c.example"]


def test_a_corrupt_invite_is_a_bad_request(hosted):
    server, _ = hosted
    response = send(server, path="/mcp/swb1_notbase64!!", body=rpc("tools/list"))
    assert response.status_code == 400


def test_anything_but_an_invite_is_not_found(hosted):
    server, _ = hosted
    assert send(server, path="/mcp", body=rpc("ping")).status_code == 404
    assert send(server, path="/mcp/some-token", body=rpc("ping")).status_code == 404


def test_the_invite_is_not_written_to_the_log(hub, hosted, capsys):
    server, _ = hosted
    blob = invite_for(hub)
    send(server, path=f"/mcp/{blob}", body=rpc("ping"))
    assert blob not in capsys.readouterr().err


def test_machine_local_tools_are_withheld(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub)
    tools = send(server, path=f"/mcp/{blob}",
                 body=rpc("tools/list")).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names and not names & set(HOSTED_WITHHELD)
    payload, is_error = tool(server, blob, "session_handoff")
    assert is_error and "hosted bridge" in payload["detail"]


# --- disclosure -------------------------------------------------------------


def blocks(server, blob: str, name: str, **arguments) -> list[str]:
    """Every text block of a tool result, not just the payload."""
    response = send(server, path=f"/mcp/{blob}", body=rpc(
        "tools/call", name=name, arguments=arguments))
    return [c["text"] for c in response.json()["result"]["content"]]


def test_the_hosted_agent_is_told_who_can_read_its_room(hub, hosted):
    server, _ = hosted
    payload, _ = tool(server, invite_for(hub), "whoami")
    assert payload["kind"] == "hosted"
    assert payload["relay"]["operator"] == OPERATOR


@pytest.mark.parametrize("name, arguments", [
    ("roster", {}),
    ("claim", {"resource": "x"}),
    ("help", {}),                        # a plain-text result, not JSON
    ("session_handoff", {}),             # an error result
    ("no_such_tool", {}),                # and an unknown tool
])
def test_every_result_carries_the_notice(hub, hosted, name, arguments):
    # Not a field the model has to ask for: it cannot use this room through
    # the bridge without being handed the notice alongside the answer.
    server, _ = hosted
    texts = blocks(server, invite_for(hub), name, **arguments)
    assert len(texts) == 2
    assert OPERATOR in texts[-1] and "can read this room" in texts[-1]


def test_the_connection_itself_says_so(hub, hosted):
    server, _ = hosted
    result = send(server, path=f"/mcp/{invite_for(hub)}",
                  body=rpc("initialize")).json()["result"]
    assert OPERATOR in result["instructions"].split("IMPORTANT:")[1]


def test_a_bridge_of_your_own_adds_nothing(hub):
    bridge = make_bridge(hub, "laptop")
    response = mcp_server.handle_request(bridge, rpc("tools/call", name="roster", arguments={}))
    assert len(response["result"]["content"]) == 1
    init = mcp_server.handle_request(bridge, rpc("initialize"))["result"]
    assert "IMPORTANT:" not in init["instructions"]


def test_the_name_carries_the_disclosure_whatever_the_invite_says(hub, hosted):
    # The name is the one roster field every reader shows — old clients, the
    # web viewer — so the disclosure lives there too, and the note in the
    # invite can add to it but not replace it.
    server, _ = hosted
    me, _ = tool(server, invite_for(hub, note="just a normal agent"), "whoami")
    assert me["name"].startswith("just a normal agent")
    assert f"{OPERATOR} can read this room" in me["name"]
    roster, _ = call(make_bridge(hub, "laptop"), "roster")
    (entry,) = [a for a in roster["agents"] if a["agent_id"] == me["agent_id"]]
    assert f"{OPERATOR} can read this room" in entry["name"]


def test_every_other_agent_is_told_on_its_roster(hub, hosted):
    server, _ = hosted
    hosted_self, _ = tool(server, invite_for(hub), "whoami")
    roster, _ = call(make_bridge(hub, "laptop"), "roster")
    assert roster["relayed_agents"] == [hosted_self["agent_id"]]
    assert OPERATOR in roster["RELAY_NOTICE"]
    (entry,) = [a for a in roster["agents"] if a["agent_id"] == hosted_self["agent_id"]]
    assert entry["relay"]["operator"] == OPERATOR
    assert entry["relay"]["about"] == "https://bridge.example/.well-known/switchboard-bridge"


def test_a_room_with_no_hosted_agent_says_nothing(hub):
    roster, _ = call(make_bridge(hub, "laptop"), "roster")
    assert "RELAY_NOTICE" not in roster


def test_the_cli_roster_says_it_too(hub, hosted, monkeypatch, capsys):
    import switchboard.cli as cli_module

    server, _ = hosted
    tool(server, invite_for(hub), "whoami")
    monkeypatch.setattr(cli_module, "Client", hub.client_class())
    monkeypatch.setenv("SWITCHBOARD_KEY", hub.key)
    assert cli_main(["--url", BASE_URL, "-w", WS, "agents"]) == 0
    out = capsys.readouterr()
    assert "(relayed)" in out.out
    assert OPERATOR in out.err


def test_relay_of_ignores_anything_but_a_hosted_declaration():
    assert relay_of(None) is None
    assert relay_of({"relay": "yes"}) is None
    assert relay_of({"relay": {"hosted": False}}) is None
    assert relay_of({"relay": {"hosted": True, "operator": "x"}}) == {
        "hosted": True, "operator": "x"}


# --- what it says about itself ---------------------------------------------


def test_the_bridge_publishes_what_it_was_built_from(hosted, monkeypatch):
    server, _ = hosted
    info = send(server, method="GET", path="/.well-known/switchboard-bridge").json()
    assert info["hosted"] is True
    assert info["operator"] == OPERATOR
    assert info["source"] == mcp_server.BRIDGE_SOURCE


def test_build_info_names_the_commit_and_how_to_verify_the_image(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("SWITCHBOARD_BUILD_IMAGE",
                       "ghcr.io/gald33/switchboard-bridge@sha256:ffff")
    info = mcp_server.build_info()
    assert info["tree"].endswith("/tree/abc123")
    assert info["verify"] == ("gh attestation verify "
                              "oci://ghcr.io/gald33/switchboard-bridge@sha256:ffff "
                              "--repo gald33/switchboard")


def test_a_bridge_run_from_a_checkout_does_not_claim_a_commit(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_BUILD_COMMIT", raising=False)
    monkeypatch.delenv("SWITCHBOARD_BUILD_IMAGE", raising=False)
    info = mcp_server.build_info()
    assert info["commit"] is None and info["verify"] is None


# --- holding nothing for long -----------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_idle_agents_are_let_go(hub):
    clock = _Clock()
    bridges = HostedBridges(hosted_relay(OPERATOR), hubs=[hub.url], idle_seconds=60,
                            clock=clock)
    first, _ = bridges.get(invite_for(hub, note="a"))
    closed = []
    first.close = lambda: closed.append(True)
    clock.now = 120
    bridges.get(invite_for(hub, note="b"))
    assert len(bridges) == 1
    assert closed == [True]
    bridges.close()


def test_the_least_recent_agent_goes_past_the_cap(hub):
    bridges = HostedBridges(hosted_relay(OPERATOR), hubs=[hub.url], max_bridges=2,
                            clock=_Clock())
    a = invite_for(hub, note="a")
    bridges.get(a)
    bridges.get(invite_for(hub, note="b"))
    bridges.get(invite_for(hub, note="c"))
    assert len(bridges) == 2
    # `a` was the least recent, so asking for it again builds it afresh.
    bridges.get(a)
    assert len(bridges) == 2
    bridges.close()


# --- the command line -------------------------------------------------------


def test_hosted_needs_an_operator(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_BRIDGE_OPERATOR", raising=False)
    with pytest.raises(SystemExit):
        mcp_server._parse_args(["--http", "--hosted"])


def test_hosted_needs_http():
    with pytest.raises(SystemExit):
        mcp_server._parse_args(["--hosted", "--operator", "x"])


def test_hosted_takes_no_token():
    with pytest.raises(SystemExit):
        mcp_server._parse_args(["--http", "--hosted", "--operator", "x", "--token", "t"])


def test_hosted_reads_its_operator_from_the_environment(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_BRIDGE_OPERATOR", "ops")
    monkeypatch.delenv("SWITCHBOARD_MCP_TOKEN", raising=False)
    args = mcp_server._parse_args(["--http", "--hosted"])
    assert args.hosted and args.operator == "ops"


def test_nothing_is_written_to_the_operators_disk(hub, hosted, tmp_path, monkeypatch):
    # Every local store a bridge on your own machine keeps under ~/.switchboard
    # — timing history, the peer-key log, the unopened-message stash. On a
    # hosted bridge each would be a record of somebody else's room.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SWITCHBOARD_STASH_DB", raising=False)
    server, _ = hosted
    blob = invite_for(hub, note="disk")
    tool(server, blob, "say", channel="general", message="hi",
         execution_class="chat", effort="low")
    tool(server, blob, "inbox")
    tool(server, blob, "roster")
    assert list(tmp_path.iterdir()) == []


# --- naming without anybody passing --note ----------------------------------


def _connect(server, blob: str, client_info=None) -> None:
    params = {"protocolVersion": "2025-06-18", "capabilities": {}}
    if client_info is not None:
        params["clientInfo"] = client_info
    assert send(server, path=f"/mcp/{blob}",
                body=rpc("initialize", **params)).status_code == 200


def test_the_app_names_the_agent_when_the_invite_does_not(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub, note="")
    _connect(server, blob, {"name": "openai-mcp", "version": "1.0.0"})
    me, _ = tool(server, blob, "whoami")
    assert me["name"] == f"ChatGPT (via hosted bridge; {OPERATOR} can read this room)"


def test_an_agent_already_on_the_roster_is_renamed_there(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub, note="")
    first, _ = tool(server, blob, "whoami")           # registers, unnamed
    assert first["name"].startswith("hosted agent (")
    _connect(server, blob, {"name": "openai-mcp"})
    tool(server, blob, "whoami")                      # re-announces
    roster, _ = call(make_bridge(hub, "laptop"), "roster")
    (entry,) = [a for a in roster["agents"] if a["agent_id"] == first["agent_id"]]
    assert entry["name"].startswith("ChatGPT (via hosted bridge;")


def test_an_invite_note_outranks_the_app(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub, note="Dana's ChatGPT")
    _connect(server, blob, {"name": "openai-mcp"})
    me, _ = tool(server, blob, "whoami")
    assert me["name"].startswith("Dana's ChatGPT (via hosted bridge;")


def test_with_neither_the_disclosure_still_stands(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub, note="")
    _connect(server, blob)
    me, _ = tool(server, blob, "whoami")
    assert me["name"] == f"hosted agent (via hosted bridge; {OPERATOR} can read this room)"


@pytest.mark.parametrize("info, label", [
    ({"name": "openai-mcp"}, "ChatGPT"),
    ({"name": "ChatGPT Connector"}, "ChatGPT"),
    ({"name": "x", "title": "Some Client"}, "Some Client"),
    ({"name": "a\nb\tc"}, "a b c"),
    ({"name": "y" * 100}, "y" * 40),
    ({"name": "   "}, None),
    ({"version": "1"}, None),
    ("not a dict", None),
])
def test_client_labels_are_short_and_single_line(info, label):
    assert mcp_server._client_label(info) == label
