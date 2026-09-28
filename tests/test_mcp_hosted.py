"""The hosted bridge (`switchboard-mcp --http --hosted`) and what it discloses.

A hosted bridge serves many agents, each built from the invite in its URL, so
that someone with only a ChatGPT account can join a room. The price is that
the bridge, an encryption service for apps that cannot encrypt on their own,
holds the key of every room sent to it — and these tests hold the two
promises that make that price honest: nothing is taken from the operator's
environment or written to its disk (bar the sealed sign-ins of
`test_bridge_links.py`), and every roster the bridge reaches says who runs it.

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


def _call(server, blob: str, name: str, session: str | None, arguments: dict) -> dict:
    """Join the room `blob` names through the front door, then call `name` in
    it — by session when there is one, by the returned handle otherwise."""
    headers = {"Mcp-Session-Id": session} if session else {}
    joined = send(server, path="/mcp", headers=headers, body=rpc(
        "tools/call", name="join_room", arguments={"invite": blob}))
    assert joined.status_code == 200, joined.text
    handle = json.loads(joined.json()["result"]["content"][0]["text"])["room"]
    response = send(server, path="/mcp", headers=headers, body=rpc(
        "tools/call", name=name, arguments={**arguments, "room": handle}))
    assert response.status_code == 200, response.text
    return response.json()["result"]


def tool(server, blob: str, name: str, session: str | None = None, **arguments):
    result = _call(server, blob, name, session, arguments)
    return json.loads(result["content"][0]["text"]), result["isError"]


# --- the invite is the agent ------------------------------------------------


def test_an_invite_is_a_working_agent_in_that_room(hub, hosted):
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
    server, bridges = hosted
    joined, is_error, _ = front_call(server, "join_room",
                                     invite=invite_for(hub, key=None, key_id="team"))
    assert is_error and "switchboard invite" in joined["error"]
    assert len(bridges) == 0


def test_an_invite_for_another_hub_is_refused(hub, hosted):
    # Or an invite would be a way to make the server fetch anything it can
    # reach — a cloud metadata endpoint, a private address — on request.
    server, bridges = hosted
    joined, is_error, _ = front_call(server, "join_room",
                                     invite=invite_for(hub, url="http://169.254.169.254/latest"))
    assert is_error and "serves rooms on" in joined["error"]
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


def test_the_front_door_is_the_only_door(hub, hosted):
    # No app pinned to a room by its URL: the invite is always a parameter.
    server, _ = hosted
    assert send(server, path=f"/mcp/{invite_for(hub)}", body=rpc("ping")).status_code == 404
    assert send(server, path="/mcp/some-token", body=rpc("ping")).status_code == 404
    assert send(server, path="/", body=rpc("ping")).status_code == 404


def test_machine_local_tools_are_withheld(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub)
    tools = {t["name"]: t for t in send(server, path="/mcp", body=rpc(
        "tools/list")).json()["result"]["tools"]}
    assert tools and not {"session_handoff", "session_import", "session_resume"} & set(tools)
    # join_room is there, but it is the front door's: the invite and nothing else.
    assert list(tools["join_room"]["inputSchema"]["properties"]) == ["invite"]
    payload, is_error = tool(server, blob, "session_handoff")
    assert is_error and "hosted bridge" in payload["detail"]


# --- disclosure -------------------------------------------------------------


def blocks(server, blob: str, name: str, **arguments) -> list[str]:
    """Every text block of a tool result, not just the payload."""
    return [c["text"] for c in _call(server, blob, name, None, arguments)["content"]]


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
    assert OPERATOR in texts[-1] and "trusted with what passes through it" in texts[-1]


def test_a_bridge_of_your_own_adds_nothing(hub):
    bridge = make_bridge(hub, "laptop")
    response = mcp_server.handle_request(bridge, rpc("tools/call", name="roster", arguments={}))
    assert len(response["result"]["content"]) == 1
    init = mcp_server.handle_request(bridge, rpc("initialize"))["result"]
    assert "Privacy:" not in init["instructions"]


def test_the_name_carries_the_disclosure_whatever_the_invite_says(hub, hosted):
    # The name is the one roster field every reader shows — old clients, the
    # web viewer — so the disclosure lives there too, and the note in the
    # invite can add to it but not replace it.
    server, _ = hosted
    me, _ = tool(server, invite_for(hub, note="just a normal agent"), "whoami")
    assert me["name"].startswith("just a normal agent")
    assert f"hosted encryption bridge run by {OPERATOR}" in me["name"]
    roster, _ = call(make_bridge(hub, "laptop"), "roster")
    (entry,) = [a for a in roster["agents"] if a["agent_id"] == me["agent_id"]]
    assert f"hosted encryption bridge run by {OPERATOR}" in entry["name"]


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


def test_the_app_names_the_agent_when_the_invite_does_not(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub, note="")
    session = connect(server, {"name": "openai-mcp", "version": "1.0.0"})
    me, _ = tool(server, blob, "whoami", session)
    assert me["name"] == f"ChatGPT (via hosted encryption bridge run by {OPERATOR})"


def test_an_agent_already_on_the_roster_is_renamed_there(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub, note="")
    first, _ = tool(server, blob, "whoami")           # registers, unnamed
    assert first["name"].startswith("hosted agent (")
    session = connect(server, {"name": "openai-mcp"})
    tool(server, blob, "whoami", session)             # rejoined by the app: re-announces
    roster, _ = call(make_bridge(hub, "laptop"), "roster")
    (entry,) = [a for a in roster["agents"] if a["agent_id"] == first["agent_id"]]
    assert entry["name"].startswith("ChatGPT (via hosted encryption bridge")


def test_an_invite_note_outranks_the_app(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub, note="Dana's ChatGPT")
    me, _ = tool(server, blob, "whoami", connect(server, {"name": "openai-mcp"}))
    assert me["name"].startswith("Dana's ChatGPT (via hosted encryption bridge")


def test_with_neither_the_disclosure_still_stands(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub, note="")
    me, _ = tool(server, blob, "whoami", connect(server))
    assert me["name"] == f"hosted agent (via hosted encryption bridge run by {OPERATOR})"


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


# --- the front door: one URL, the invite as a parameter ----------------------


def front_call(server, name: str, session: str | None = None,
               user_agent: str | None = None, **arguments):
    headers = {}
    if session:
        headers["Mcp-Session-Id"] = session
    if user_agent:
        headers["User-Agent"] = user_agent
    response = send(server, path="/mcp", headers=headers, body=rpc(
        "tools/call", name=name, arguments=arguments))
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    texts = [c["text"] for c in result["content"]]
    return json.loads(texts[0]), result["isError"], texts


def connect(server, client_info=None) -> str:
    params = {"protocolVersion": "2025-06-18", "capabilities": {}}
    if client_info is not None:
        params["clientInfo"] = client_info
    response = send(server, path="/mcp", body=rpc("initialize", **params))
    assert response.status_code == 200
    return response.headers["Mcp-Session-Id"]


def test_the_front_door_asks_for_an_invite_and_says_who_can_read(hosted):
    server, _ = hosted
    response = send(server, path="/mcp", body=rpc("initialize"))
    instructions = response.json()["result"]["instructions"]
    assert "join_room" in instructions and "swb1_" in instructions
    assert OPERATOR in instructions.split("Privacy:")[1]
    assert response.headers["Mcp-Session-Id"]


def test_the_front_door_lists_join_room_with_an_invite_parameter(hosted):
    server, _ = hosted
    tools = {t["name"]: t for t in send(server, path="/mcp", body=rpc(
        "tools/list")).json()["result"]["tools"]}
    assert tools["join_room"]["inputSchema"]["required"] == ["invite"]
    assert "room" in tools["roster"]["inputSchema"]["properties"]
    assert "room" not in tools["roster"]["inputSchema"]["required"]
    assert not {"session_handoff", "session_import", "session_resume"} & set(tools)


def test_join_once_and_the_session_stays_in_that_room(hub, hosted):
    server, _ = hosted
    session = connect(server, {"name": "openai-mcp"})
    joined, is_error, texts = front_call(server, "join_room", session,
                                         invite=invite_for(hub, note=""))
    assert not is_error and joined["joined"] and joined["room"].startswith("room_")
    assert joined["you_appear_as"].startswith("ChatGPT (via hosted encryption bridge")
    assert OPERATOR in texts[-1]
    # No room named from here on: the session remembers it.
    claim, is_error, texts = front_call(server, "claim", session, resource="docs/x")
    assert not is_error and claim["acquired"] is True
    assert OPERATOR in texts[-1]
    theirs, _ = call(make_bridge(hub, "laptop"), "claim", resource="docs/x")
    assert theirs["acquired"] is False


def test_without_a_session_the_handle_names_the_room(hub, hosted):
    server, _ = hosted
    joined, _, _ = front_call(server, "join_room", invite=invite_for(hub, note=""),
                              user_agent="openai-mcp/1.0")
    assert f"room='{joined['room']}'" in joined["next"]
    # Named from the User-Agent, since there is no session to remember the app by.
    assert joined["you_appear_as"].startswith("ChatGPT (via hosted encryption bridge")
    lost, is_error, _ = front_call(server, "roster")
    assert is_error and lost["error"] == "no_room"
    roster, is_error, _ = front_call(server, "roster", room=joined["room"])
    assert not is_error and roster["count"] == 1


def test_sessions_do_not_see_each_others_rooms(hub, hosted):
    server, _ = hosted
    one, two = connect(server), connect(server)
    front_call(server, "join_room", one, invite=invite_for(hub, note="one"))
    lost, is_error, _ = front_call(server, "whoami", two)
    assert is_error and lost["error"] == "no_room"


def test_the_same_invite_is_the_same_agent_and_handle(hub, hosted):
    server, _ = hosted
    blob = invite_for(hub)
    first, _, _ = front_call(server, "join_room", connect(server), invite=blob)
    again, _, _ = front_call(server, "join_room", connect(server), invite=blob)
    assert first["room"] == again["room"]
    one, _, _ = front_call(server, "whoami", room=first["room"])
    other, _ = tool(server, blob, "whoami", connect(server))
    assert one["agent_id"] == other["agent_id"]


def test_a_bad_invite_is_a_tool_error_not_an_http_one(hub, hosted):
    server, _ = hosted
    for invite in ("swb1_notbase64!!", "hello",
                   invite_for(hub, url="http://169.254.169.254/latest")):
        joined, is_error, _ = front_call(server, "join_room", invite=invite)
        assert is_error and joined["joined"] is False and joined["error"]
    missing, is_error, _ = front_call(server, "join_room")
    assert is_error and "invite" in missing["error"]


def test_an_unknown_handle_says_join_again(hosted):
    server, _ = hosted
    gone, is_error, _ = front_call(server, "roster", room="room_nope")
    assert is_error and gone["error"] == "room_expired"


def test_an_unknown_session_is_sent_to_initialize_again(hosted):
    server, _ = hosted
    response = send(server, path="/mcp", headers={"Mcp-Session-Id": "nope"},
                    body=rpc("tools/list"))
    assert response.status_code == 404


def test_a_session_can_be_ended(hub, hosted):
    server, _ = hosted
    session = connect(server)
    assert send(server, method="DELETE", path="/mcp",
                headers={"Mcp-Session-Id": session}).status_code == 204
    response = send(server, path="/mcp", headers={"Mcp-Session-Id": session},
                    body=rpc("tools/list"))
    assert response.status_code == 404


def test_the_invite_is_not_written_to_the_log_from_the_front_door(hub, hosted, capsys):
    server, _ = hosted
    blob = invite_for(hub)
    front_call(server, "join_room", connect(server), invite=blob)
    assert blob not in capsys.readouterr().err


def test_idle_sessions_and_handles_are_let_go(hub):
    clock = _Clock()
    bridges = HostedBridges(hosted_relay(OPERATOR), hubs=[hub.url], clock=clock)
    front = mcp_server.HostedFront(bridges, idle_seconds=60, clock=clock)
    sid = front._new_session(None)
    handle = front._handle_for(invite_for(hub))
    clock.now = 120
    front._new_session(None)                          # pruning runs on the way in
    front._handle_for(invite_for(hub, note="other"))
    assert sid not in front._sessions
    assert front._blob_for(handle) is None
    bridges.close()


# --- what a plugin submission needs -----------------------------------------


def test_every_tool_declares_all_three_hints(hosted):
    server, _ = hosted
    tools = send(server, path="/mcp", body=rpc("tools/list")).json()["result"]["tools"]
    for t in tools:
        assert set(t["annotations"]) >= {"readOnlyHint", "destructiveHint", "openWorldHint"}, \
            t["name"]
        assert t["annotations"]["openWorldHint"] is False, t["name"]
        # A read-only tool cannot also be destructive.
        assert not (t["annotations"]["readOnlyHint"] and t["annotations"]["destructiveHint"])


@pytest.mark.parametrize("name, destructive", [
    ("say", True), ("dm", True), ("whisper", True),       # a sent message can't be unsent
    ("board_set", True), ("board_delete", True), ("leave", True),
    ("claim", False), ("release", False), ("checkin", False), ("inbox", False),
    ("join_room", False),
])
def test_destructive_means_cannot_be_taken_back(hosted, name, destructive):
    server, _ = hosted
    tools = {t["name"]: t for t in send(server, path="/mcp", body=rpc(
        "tools/list")).json()["result"]["tools"]}
    assert tools[name]["annotations"]["destructiveHint"] is destructive


def test_the_openai_challenge_is_served_as_the_bare_token(hub):
    bridges = HostedBridges(hosted_relay(OPERATOR), hubs=[hub.url])
    server = make_hosted_server(bridges, "127.0.0.1", 0, challenge="tok_abc123")
    try:
        response = send(server, method="GET", path="/.well-known/openai-apps-challenge")
        assert response.status_code == 200
        assert response.text == "tok_abc123"
        assert response.headers["Content-Type"].startswith("text/plain")
    finally:
        server.server_close()
        bridges.close()


def test_no_challenge_configured_is_not_found(hosted):
    server, _ = hosted
    response = send(server, method="GET", path="/.well-known/openai-apps-challenge")
    assert response.status_code == 404


def test_the_challenge_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_OPENAI_CHALLENGE", " tok_env \n")
    args = mcp_server._parse_args(["--http", "--hosted", "--operator", "x"])
    assert args.openai_challenge == "tok_env"


def test_a_hosted_send_does_not_advise_running_a_listener(hub, hosted):
    # The host only calls tools, so the answer's place is named instead.
    server, _ = hosted
    sent, _ = tool(server, invite_for(hub), "dm", to="someone", message="hi")
    assert "switchboard listen" not in sent["listener"]["next"]
    assert "waits in your inbox" in sent["listener"]["next"]
