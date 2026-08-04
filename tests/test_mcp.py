"""Tests for the MCP stdio bridge.

Because the bridge implements the protocol directly rather than through an
SDK, the protocol shape itself is worth asserting: a client that gets a
malformed initialize result will simply refuse to load the server.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from switchboard import mcp_server
from switchboard.client import Client, Identity
from switchboard.config import ClientConfig, ServerConfig
from switchboard.mcp_server import TOOLS, Bridge, handle_request, serve_stdio
from switchboard.server import create_app
from switchboard.store import Store

WS = "mcp-ws"


@pytest.fixture
def hub(tmp_path):
    """A real server, reached through TestClient's transport."""
    store = Store(str(tmp_path / "mcp.db"))
    app = create_app(ServerConfig(db_path=str(tmp_path / "mcp.db")), store=store)
    with TestClient(app) as http:
        yield http
    store.close()


class _BoundClient(Client):
    """A Client whose transport is the in-process TestClient."""

    def __init__(self, http: TestClient, agent_id: str) -> None:
        config = ClientConfig(url="http://testserver", workspace=WS, agent_id=agent_id)
        super().__init__(config, agent_id=agent_id)
        self._http.close()
        self._http = http


def make_bridge(hub: TestClient, agent_id: str = "mcp-agent") -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge.config = ClientConfig(url="http://testserver", workspace=WS, agent_id=agent_id)
    bridge.identity = Identity(
        agent_id=agent_id, name="mcp agent", kind="local", branch="feat/x", meta={}
    )
    bridge.client = _BoundClient(hub, agent_id)
    bridge._registered = False
    return bridge


def call(bridge: Bridge, name: str, **arguments):
    response = handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    return payload, result.get("isError", False)


# --- protocol ---------------------------------------------------------------


def test_initialize_echoes_a_supported_protocol_version(hub):
    bridge = make_bridge(hub)
    response = handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
    })
    result = response["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "switchboard"
    assert "tools" in result["capabilities"]


def test_initialize_falls_back_for_an_unknown_version(hub):
    bridge = make_bridge(hub)
    response = handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "1999-01-01"},
    })
    assert response["result"]["protocolVersion"] == mcp_server.LATEST_PROTOCOL


def test_initialized_notification_gets_no_response(hub):
    bridge = make_bridge(hub)
    assert handle_request(
        bridge, {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) is None


def test_unknown_method_returns_jsonrpc_error(hub):
    bridge = make_bridge(hub)
    response = handle_request(bridge, {"jsonrpc": "2.0", "id": 7, "method": "nope"})
    assert response["error"]["code"] == mcp_server.JSONRPC_METHOD_NOT_FOUND
    assert response["id"] == 7


def test_tools_list_shape_is_valid(hub):
    bridge = make_bridge(hub)
    tools = handle_request(
        bridge, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )["result"]["tools"]
    assert tools == TOOLS
    for tool in tools:
        assert tool["name"] and tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        for required in schema["required"]:
            assert required in schema["properties"], tool["name"]


def test_every_tool_has_a_handler(hub):
    bridge = make_bridge(hub)
    for tool in TOOLS:
        assert callable(getattr(bridge, tool["name"], None)), tool["name"]


def test_serve_stdio_round_trip(hub):
    bridge = make_bridge(hub)
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    stdout = io.StringIO()
    serve_stdio(bridge, stdin=stdin, stdout=stdout)
    lines = [json.loads(le) for le in stdout.getvalue().splitlines()]
    # Two requests, one notification -> exactly two responses.
    assert [le["id"] for le in lines] == [1, 2]


def test_malformed_json_does_not_kill_the_server(hub):
    bridge = make_bridge(hub)
    stdin = io.StringIO(
        "{not json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    stdout = io.StringIO()
    serve_stdio(bridge, stdin=stdin, stdout=stdout)
    lines = [json.loads(le) for le in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == mcp_server.JSONRPC_PARSE_ERROR
    assert lines[1]["id"] == 2


# --- tools ------------------------------------------------------------------


def test_whoami_registers_lazily(hub):
    bridge = make_bridge(hub, "a1")
    payload, is_error = call(bridge, "whoami")
    assert not is_error
    assert payload["agent_id"] == "a1"
    assert hub.get("/agents", params={"workspace": WS}).json()["count"] == 1


def test_roster_shows_other_agents_and_what_they_hold(hub):
    a1 = make_bridge(hub, "a1")
    a2 = make_bridge(hub, "a2")
    call(a1, "whoami")
    call(a2, "whoami")
    call(a2, "claim", resource="db")
    payload, _ = call(a1, "roster")
    by_id = {a["agent_id"]: a for a in payload["agents"]}
    assert by_id["a2"]["holding"] == ["db"]
    assert by_id["a1"]["is_you"] is True


def test_claim_conflict_is_reported_as_data_not_an_exception(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "claim", resource="db")
    payload, is_error = call(a2, "claim", resource="db")
    assert is_error is False, "a taken lease is a normal outcome, not a tool failure"
    assert payload["acquired"] is False
    assert payload["held_by"] == "a1"
    assert "advice" in payload


def test_claim_release_reclaim(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    assert call(a1, "claim", resource="db")[0]["acquired"] is True
    assert call(a1, "release", resource="db")[0]["released"] is True
    assert call(a2, "claim", resource="db")[0]["acquired"] is True


def test_say_and_inbox(hub):
    a1 = make_bridge(hub, "a1")
    a2 = make_bridge(hub, "a2")
    call(a1, "whoami")
    a1.client.register(name="a1", channels=["build"])
    call(a2, "say", channel="build", message="rebasing now")
    payload, _ = call(a1, "inbox")
    assert payload["count"] == 1
    assert payload["messages"][0]["body"] == "rebasing now"
    assert payload["messages"][0]["from"] == "a2"


def test_dm_reaches_only_the_addressee(hub):
    a1, a2, a3 = (make_bridge(hub, n) for n in ("a1", "a2", "a3"))
    for b in (a1, a2, a3):
        call(b, "whoami")
    call(a2, "dm", to="a1", message="just you")
    assert call(a1, "inbox")[0]["count"] == 1
    assert call(a3, "inbox")[0]["count"] == 0


def test_checkin_renews_leases_and_returns_messages(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    call(a1, "claim", resource="db")
    call(a2, "dm", to="a1", message="ping")
    payload, _ = call(a1, "checkin", task="migrating")
    assert [h["resource"] for h in payload["holding"]] == ["db"]
    assert payload["new_messages"] == 1
    assert payload["messages"][0]["body"] == "ping"


def test_checkin_recovers_from_expired_presence(hub):
    """An agent whose presence lapsed must re-register rather than error out."""
    a1 = make_bridge(hub, "a1")
    call(a1, "whoami")
    hub.delete("/agents/a1", params={"workspace": WS})
    payload, is_error = call(a1, "checkin")
    assert not is_error
    assert hub.get("/agents", params={"workspace": WS}).json()["count"] == 1


def test_board_tools_round_trip(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "board_set", key="plan/x", value={"steps": [1, 2]})
    payload, _ = call(a2, "board_get", key="plan/x")
    assert payload["found"] is True
    assert payload["value"] == {"steps": [1, 2]}
    assert payload["updated_by"] == "a1"
    listing, _ = call(a2, "board_list", prefix="plan/")
    assert [e["key"] for e in listing["entries"]] == ["plan/x"]


def test_board_get_missing_key_is_not_an_error(hub):
    payload, is_error = call(make_bridge(hub), "board_get", key="absent")
    assert is_error is False
    assert payload["found"] is False and payload["value"] is None


def test_history_does_not_consume_the_inbox(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    a1.client.register(name="a1", channels=["build"])
    call(a2, "say", channel="build", message="x")
    assert call(a1, "history", channel="build")[0]["messages"][0]["body"] == "x"
    assert call(a1, "inbox")[0]["count"] == 1


def test_unknown_tool_is_an_error_result_not_a_crash(hub):
    payload, is_error = call(make_bridge(hub), "no_such_tool")
    assert is_error is True
    assert payload["error"] == "unknown_tool"


def test_bad_arguments_are_reported_cleanly(hub):
    payload, is_error = call(make_bridge(hub), "claim", wrong_arg=1)
    assert is_error is True
    assert payload["error"] == "bad_arguments"
