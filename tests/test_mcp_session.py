"""The three session tools on the MCP bridge.

The bridge is what a running Claude Code actually calls, so the contract is
checked at its edge: registered like every other tool, routed to a room like
every other tool, carrying `unread_dms` where it touches the hub, answering
expected refusals as data, and never putting capsule bytes into a result —
the model's context is the one place a transcript must not land.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard import claude_session as cs
from switchboard.client import Identity
from switchboard.crypto import generate_key
from switchboard.mcp_server import TOOLS, Bridge, handle_request
from switchboard.testing import Hub
from switchboard.testing import hub as make_hub
from switchboard.timing import TimingModel

WS = "mcp-session-ws"
SID = "8a1b2c3d-4e5f-4a6b-9c8d-7e6f5a4b3c2d"


def make_bridge(hub: Hub, agent_id: str) -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge.config = hub.client_config(agent_id=agent_id)
    bridge.identity = Identity(
        agent_id=agent_id, name=agent_id, kind="local", branch="feat/x", meta={}
    )
    bridge.client = hub.client(agent_id, agent_id=agent_id)
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


def _session(cfg: Path, cwd: str) -> Path:
    project = cfg / "projects" / cs.project_key(cwd)
    project.mkdir(parents=True, exist_ok=True)
    record = {"type": "user", "sessionId": SID, "cwd": cwd, "version": "2.1.260",
              "message": {"content": "codeword PANGOLIN-9"}}
    transcript = project / f"{SID}.jsonl"
    transcript.write_text(json.dumps(record) + "\n")
    return transcript


@pytest.fixture
def room():
    with make_hub(workspace=WS, key=generate_key()) as handle:
        yield handle


def test_the_tools_are_registered_and_shaped_like_the_rest():
    names = [t["name"] for t in TOOLS]
    assert names.index("session_handoff") < names.index("join_room")
    for name in ("session_handoff", "session_import", "session_resume"):
        [tool] = [t for t in TOOLS if t["name"] == name]
        assert tool["inputSchema"]["additionalProperties"] is False
        assert "claude --resume" in tool["description"] or "resume" in tool["description"]
    props = {t["name"]: set(t["inputSchema"]["properties"]) for t in TOOLS}
    assert "room" in props["session_handoff"] and "room" in props["session_import"]
    assert "room" not in props["session_resume"], "resuming is local, not a room action"


def test_handoff_and_import_through_the_bridge(room, tmp_path, monkeypatch):
    sender_cfg = tmp_path / "sender-claude"
    _session(sender_cfg, "/Users/gal/code/switchboard")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(sender_cfg))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/Users/gal/code/switchboard")
    alice, bob = make_bridge(room, "alice"), make_bridge(room, "bob")
    call(bob, "whoami")  # on the roster, so alice can address it

    sent, is_error = call(alice, "session_handoff", to=bob.client.agent_id)
    assert not is_error and sent["handed_off"] is True and sent["published"] is True
    assert sent["key"] == f"sessions/{SID}" and "capsule" not in sent
    assert "unread_dms" in sent
    assert "PANGOLIN" not in json.dumps(sent)

    # Bob's side: a different machine, a different config dir, its own cwd.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob-claude"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "0d0d0d0d-1111-4222-8333-444444444444")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/workspace/switchboard")
    got, is_error = call(bob, "session_import")
    assert not is_error and got["pointers"] == 1
    [installed] = got["installed"]
    assert installed["verified"] is True
    assert Path(installed["transcript"]).parent.name == "-workspace-switchboard"
    assert installed["resume"].endswith(f"claude --resume {SID}")
    assert "PANGOLIN" not in json.dumps(got), "capsule bytes never reach the model"
    assert "unread_dms" in got
    assert room.board() == []
    # Alice learns it landed, on her next call, the way every DM is learned.
    after, _ = call(alice, "whoami")
    assert after["unread_dms"] == 1


def test_refusals_are_answers(room, tmp_path, monkeypatch):
    alice = make_bridge(room, "alice")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    payload, is_error = call(alice, "session_handoff")
    assert not is_error and payload["handed_off"] is False
    assert "CLAUDE_CODE_SESSION_ID" in payload["reason"] and "unread_dms" in payload

    payload, is_error = call(alice, "session_import", session_id=SID)
    assert not is_error and payload["installed"] == []
    assert "expired" in payload["missing"][0]["reason"]

    payload, is_error = call(alice, "session_resume", session_id=SID)
    assert not is_error and payload["started"] is False and "no transcript" in payload["reason"]
    assert "unread_dms" not in payload, "local, like keygen"


def test_a_plaintext_room_is_refused_as_data(tmp_path, monkeypatch):
    with make_hub(workspace=WS) as handle:
        alice = make_bridge(handle, "alice")
        _session(tmp_path / "c", "/w")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        payload, is_error = call(alice, "session_handoff")
        assert not is_error and payload["handed_off"] is False
        assert "allow_plaintext" in payload["reason"]
        payload, _ = call(alice, "session_handoff", allow_plaintext=True)
        assert payload["published"] is True and payload["encrypted"] is False


def test_a_corrupt_capsule_is_an_error_with_its_own_code(room, tmp_path, monkeypatch):
    from switchboard import handoff

    alice, bob = make_bridge(room, "alice"), make_bridge(room, "bob")
    _session(tmp_path / "c", "/w")
    capsule = cs.package(SID, config_dir=tmp_path / "c")
    capsule["files"][0]["data"] = "AAAA"
    handoff.publish(alice.client, capsule)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob"))
    payload, is_error = call(bob, "session_import", session_id=SID, cwd="/w")
    assert is_error and payload["error"] == "handoff" and "corrupt" in payload["detail"]


def test_a_disk_failure_is_not_reported_as_a_hub_outage(room, tmp_path, monkeypatch):
    from switchboard import handoff

    alice, bob = make_bridge(room, "alice"), make_bridge(room, "bob")
    _session(tmp_path / "c", "/w")
    handoff.publish(alice.client, cs.package(SID, config_dir=tmp_path / "c"))

    def no_disk(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "/nope")

    monkeypatch.setattr(cs, "install", no_disk)
    payload, is_error = call(bob, "session_import", session_id=SID, cwd="/w")
    assert is_error and payload["error"] == "handoff" and "Permission denied" in payload["detail"]
    assert handoff.fetch(bob.client, SID) is not None, "the claimed capsule went back"


OTHER = "w_invited-room"


def _invited(hub, monkeypatch):
    """An invite to a second room on this hub — the recipe tests/test_mcp.py uses."""
    from switchboard import mcp_server
    from switchboard.invite import PROBE_SENTINEL, Invite
    from switchboard.testing import BASE_URL

    room_key = generate_key()
    host = hub.client("host", workspace=OTHER, key=room_key, agent_id="host")
    host.register(name="host:main", kind="local")
    host.board_set("join/probe/abcd", PROBE_SENTINEL)
    monkeypatch.setattr(mcp_server, "Client", hub.client_class())
    return Invite(url=BASE_URL, workspace=OTHER, key=room_key, probe="join/probe/abcd").encode()


def test_the_session_tools_follow_the_room_parameter(room, tmp_path, monkeypatch):
    """A handoff in a joined room lands on that room's board, not the default one."""
    alice, bob = make_bridge(room, "alice"), make_bridge(room, "bob")
    invite = _invited(room, monkeypatch)
    for bridge in (alice, bob):
        joined, is_error = call(bridge, "join_room", invite=invite)
        assert not is_error and joined["joined"], joined
    bob_there = bob._rooms[OTHER].agent_id
    call(bob, "whoami", room=OTHER)

    _session(tmp_path / "c", "/w")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    before = len(room.board(workspace=OTHER))
    sent, is_error = call(alice, "session_handoff", to=bob_there, room=OTHER)
    assert not is_error and sent["published"] is True, sent
    assert room.board() == [] and len(room.board(workspace=OTHER)) == before + 1

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    got, is_error = call(bob, "session_import", cwd="/x", room=OTHER)
    assert not is_error and len(got["installed"]) == 1, got
    assert len(room.board(workspace=OTHER)) == before, "collected from that room's board"


def test_other_messages_come_back_in_inbox_shape(room, tmp_path, monkeypatch):
    alice, bob = make_bridge(room, "alice"), make_bridge(room, "bob")
    _session(tmp_path / "c", "/w")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    call(bob, "whoami")
    alice.client.send(bob.client.agent_id, "unrelated: the build is green")
    call(alice, "session_handoff", to=bob.client.agent_id)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    got, is_error = call(bob, "session_import", cwd="/x")
    assert not is_error and len(got["installed"]) == 1
    [other] = got["other"]
    assert other["body"] == "unrelated: the build is green"
    assert {"seq", "from", "channel", "at"} <= set(other)
