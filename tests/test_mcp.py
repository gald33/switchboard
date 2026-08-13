"""Tests for the MCP stdio bridge.

Because the bridge implements the protocol directly rather than through an
SDK, the protocol shape itself is worth asserting: a client that gets a
malformed initialize result will simply refuse to load the server.
"""

from __future__ import annotations

import io
import json

import pytest

from switchboard import mcp_server
from switchboard.client import Identity
from switchboard.crypto import WorkspaceCipher
from switchboard.mcp_server import TOOLS, Bridge, handle_request, serve_stdio
from switchboard.testing import Hub
from switchboard.testing import hub as make_hub
from switchboard.timing import TimingModel

WS = "mcp-ws"


def _t(offset: float = 0.0) -> float:
    """A fixed epoch base, so timing tests never depend on the wall clock."""
    return 1_000_000.0 + offset


@pytest.fixture
def hub():
    """A real server, reached in-process. See `switchboard.testing`."""
    with make_hub(workspace=WS) as handle:
        yield handle


def make_bridge(hub: Hub, agent_id: str = "mcp-agent") -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge.config = hub.client_config(agent_id=agent_id)
    bridge.identity = Identity(
        agent_id=agent_id, name="mcp agent", kind="local", branch="feat/x", meta={}
    )
    bridge.client = hub.client(agent_id, agent_id=agent_id)
    bridge.timing = TimingModel(":memory:")
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
    # Same tools in the same order; only the execution_class hint text is
    # personalised per agent, so identity with TOOLS is not asserted.
    assert [t["name"] for t in tools] == [t["name"] for t in TOOLS]
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
    assert hub.http.get("/agents", params={"workspace": WS}).json()["count"] == 1


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


def test_inbox_messages_expose_seq_for_client_side_dedup(hub):
    """#24: agents need a stable identifier to defend against acting on the
    same message twice, independent of body content."""
    a1 = make_bridge(hub, "a1")
    a2 = make_bridge(hub, "a2")
    call(a1, "whoami")
    a1.client.register(name="a1", channels=["build"])
    call(a2, "say", channel="build", message="first")
    call(a2, "say", channel="build", message="second")
    payload, _ = call(a1, "inbox")
    seqs = [m["seq"] for m in payload["messages"]]
    assert len(seqs) == 2
    assert len(set(seqs)) == 2
    assert seqs == sorted(seqs)


def test_dm_reaches_only_the_addressee(hub):
    a1, a2, a3 = (make_bridge(hub, n) for n in ("a1", "a2", "a3"))
    for b in (a1, a2, a3):
        call(b, "whoami")
    call(a2, "dm", to="a1", message="just you")
    assert call(a1, "inbox")[0]["count"] == 1
    assert call(a3, "inbox")[0]["count"] == 0


def test_say_without_timing_hints_has_no_forecast(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    a1.client.register(name="a1", channels=["build"])
    payload, _ = call(a2, "say", channel="build", message="rebasing now")
    assert "timing_forecast" not in payload
    inbox, _ = call(a1, "inbox")
    assert inbox["messages"][0]["body"] == "rebasing now"
    assert "timing_forecast" not in inbox["messages"][0]


def test_say_with_timing_hints_attaches_bootstrap_forecast(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    a1.client.register(name="a1", channels=["build"])
    payload, _ = call(
        a2, "say", channel="build", message="digging into the parser bug",
        execution_class="coding", effort="medium",
    )
    # The sender gets both the shared checkpoints and a local convenience
    # countdown — the countdown never crosses the wire.
    assert set(payload["timing_forecast"].keys()) == {
        "p50", "p95", "p50_in_seconds", "p95_in_seconds",
        "speak_p50", "speak_p95", "speak_p50_in_seconds", "speak_p95_in_seconds",
    }
    assert "now" in payload

    inbox, _ = call(a1, "inbox")
    msg = inbox["messages"][0]
    # The message body seen by the receiver is unwrapped back to plain text,
    # and its forecast is the sparse wire form only.
    assert msg["body"] == "digging into the parser bug"
    # The receiver gets the shareable checkpoints only: the two look ones it
    # always got, plus the speak pair — and none of the sender's relative
    # countdowns, which mean nothing once the message has travelled.
    assert set(msg["timing_forecast"].keys()) == {
        "p50", "p95", "speak_p50", "speak_p95",
    }
    assert msg["timing_forecast"] == {
        "p50": payload["timing_forecast"]["p50"],
        "p95": payload["timing_forecast"]["p95"],
        "speak_p50": payload["timing_forecast"]["speak_p50"],
        "speak_p95": payload["timing_forecast"]["speak_p95"],
    }
    assert "now" in inbox


def test_dm_forecast_learns_from_local_history(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    call(a2, "whoami")

    from switchboard.timing import MIN_SAMPLES

    # Seed enough local history for a2's own (coding, low) bucket that the
    # forecast should no longer be the wide bootstrap prior.
    t = 1_000_000.0
    for delta in range(1, MIN_SAMPLES + 1):
        a2.timing.declare("a2", a2.config.workspace, "coding", "low", now=t)
        a2.timing.note_look("a2", a2.config.workspace, now=t + delta)
        t += delta + 1

    payload, _ = call(
        a2, "dm", to="a1", message="quick fix incoming",
        execution_class="coding", effort="low",
    )
    f = a2.timing.forecast("a2", a2.config.workspace, "coding", "low", now=t)
    assert f.source == "class+effort"
    assert f.samples >= MIN_SAMPLES
    assert "timing_forecast" in payload


def test_reading_the_inbox_is_what_closes_the_window(hub):
    """The forecast predicts when the agent next comes *looking*, so an
    inbox read is the event that scores it."""
    a1 = make_bridge(hub, "a1")
    ws = a1.config.workspace
    call(a1, "say", channel="build", message="heads down", execution_class="coding",
         effort="medium")
    assert a1.timing._pending("a1", ws) is not None
    call(a1, "inbox")
    assert a1.timing._pending("a1", ws) is None
    assert len(a1.timing._deltas("a1", ws, "coding", "medium")) == 1


def test_posting_repeatedly_without_looking_records_nothing(hub):
    """An agent can talk without ever reading; none of that is evidence
    about when it next looks."""
    a1 = make_bridge(hub, "a1")
    ws = a1.config.workspace
    for _ in range(3):
        call(a1, "say", channel="build", message="still going",
             execution_class="coding", effort="medium")
    assert a1.timing._deltas("a1", ws, "coding", "medium") == []
    assert a1.timing._pending("a1", ws) is not None


def test_checkin_closes_the_window_and_can_declare_a_new_one(hub):
    """checkin long-polls the inbox, so it both scores the open forecast
    and can declare the next stretch."""
    a1 = make_bridge(hub, "a1")
    ws = a1.config.workspace
    call(a1, "say", channel="build", message="starting", execution_class="coding",
         effort="medium")
    for _ in range(2):
        payload, _ = call(a1, "checkin", execution_class="coding", effort="medium")
        assert "timing_forecast" in payload
    assert len(a1.timing._deltas("a1", ws, "coding", "medium")) == 2


def test_a_look_without_hints_closes_without_reopening(hub):
    a1 = make_bridge(hub, "a1")
    ws = a1.config.workspace
    call(a1, "say", channel="build", message="starting", execution_class="coding",
         effort="low")
    payload, _ = call(a1, "checkin")
    assert "timing_forecast" not in payload
    assert a1.timing._pending("a1", ws) is None
    assert len(a1.timing._deltas("a1", ws, "coding", "low")) == 1


def test_inbox_can_declare_the_next_stretch(hub):
    """An agent looping on inbox rather than checkin must still be able to
    forecast."""
    a1 = make_bridge(hub, "a1")
    payload, _ = call(a1, "inbox", execution_class="research", effort="high")
    assert set(payload["timing_forecast"]) == {
        "p50", "p95", "p50_in_seconds", "p95_in_seconds",
        "speak_p50", "speak_p95", "speak_p50_in_seconds", "speak_p95_in_seconds",
    }
    # One declaration opens a window per kind, and both must be outstanding.
    from switchboard.timing import LOOK, SPEAK
    for kind in (LOOK, SPEAK):
        assert a1.timing._pending("a1", a1.config.workspace, kind) is not None


def test_tools_list_offers_this_agents_own_top_classes(hub):
    a1 = make_bridge(hub, "a1")
    ws = a1.config.workspace
    for i in range(3):
        a1.timing.declare("a1", ws, "yak-shaving", "low", now=_t(i * 10))
        a1.timing.note_look("a1", ws, now=_t(i * 10 + 5))
    tools = {t["name"]: t for t in a1.tools()}
    description = tools["say"]["inputSchema"]["properties"]["execution_class"]["description"]
    assert "yak-shaving" in description
    # Still an open string — the shortlist must never become an enum.
    assert "enum" not in tools["say"]["inputSchema"]["properties"]["execution_class"]


def test_a_broken_timing_store_never_blocks_a_send(hub):
    """The whole feature is advisory; it must fail open."""
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    a1.client.register(name="a1", channels=["build"])

    class _Broken:
        def declare(self, *a, **k):
            raise RuntimeError("disk gone")

        def top_classes(self, *a, **k):
            raise RuntimeError("disk gone")

        def close(self):
            pass

    a2.timing = _Broken()
    payload, is_error = call(
        a2, "say", channel="build", message="still gets through",
        execution_class="coding", effort="medium",
    )
    assert not is_error
    assert payload["posted"] is True
    assert "timing_forecast" not in payload
    assert call(a1, "inbox")[0]["messages"][0]["body"] == "still gets through"
    # tools/list degrades to the static list rather than raising.
    assert [t["name"] for t in a2.tools()] == [t["name"] for t in TOOLS]


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
    hub.http.delete("/agents/a1", params={"workspace": WS})
    payload, is_error = call(a1, "checkin")
    assert not is_error
    assert hub.http.get("/agents", params={"workspace": WS}).json()["count"] == 1


# --- automatic presence + DM notice, on every op, not just checkin ----------


def test_ops_report_unread_dms_without_waiting_for_checkin(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    assert call(a1, "say", channel="build", message="starting")[0]["unread_dms"] == 0

    call(a2, "dm", to="a1", message="ping")

    # a1 never calls checkin or inbox — an unrelated op is what surfaces it.
    payload, _ = call(a1, "board_set", key="scratch/x", value=1)
    assert payload["unread_dms"] == 1


def test_the_unread_dm_signal_does_not_consume_the_message(hub):
    """A cheap notice, not a read — the message must still be there for inbox."""
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    call(a2, "dm", to="a1", message="ping")

    call(a1, "claim", resource="r")           # an unrelated op, sees the notice
    call(a1, "claim", resource="r2")           # calling it again changes nothing

    payload, _ = call(a1, "inbox")
    assert payload["count"] == 1
    assert payload["messages"][0]["body"] == "ping"


def test_unread_dms_ignores_channel_traffic(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    a1.client.register(name="a1", channels=["build"])
    call(a2, "say", channel="build", message="noisy but not a dm")
    payload, _ = call(a1, "claim", resource="r")
    assert payload["unread_dms"] == 0


def test_a_non_checkin_op_recovers_from_expired_presence(hub):
    """Every op relies on heartbeat now, so it needs checkin's same recovery."""
    a1 = make_bridge(hub, "a1")
    call(a1, "whoami")
    hub.http.delete("/agents/a1", params={"workspace": WS})
    payload, is_error = call(a1, "say", channel="build", message="still here")
    assert not is_error
    assert hub.http.get("/agents", params={"workspace": WS}).json()["count"] == 1


def test_claim_conflict_still_reports_unread_dms(hub):
    """The notice belongs on every branch of a tool's result, not just success."""
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "claim", resource="db")
    call(a2, "whoami")
    call(a1, "dm", to="a2", message="ping")
    payload, _ = call(a2, "claim", resource="db")  # a2 conflicts with a1's hold
    assert payload["acquired"] is False
    assert payload["unread_dms"] == 1


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


# --- keygen and custom_scope: agent-initiated side channels ------------------


def test_keygen_returns_a_key_and_an_opaque_workspace(hub):
    a1 = make_bridge(hub, "a1")
    payload, is_error = call(a1, "keygen")
    assert is_error is False
    assert len(payload["key"]) > 20
    assert payload["workspace"].startswith("w_")

    second, _ = call(a1, "keygen")
    assert second["key"] != payload["key"]
    assert second["workspace"] != payload["workspace"]


def test_keygen_needs_no_prior_registration(hub):
    """Purely local — no hub call, so it works before whoami/register ever run."""
    payload, is_error = call(make_bridge(hub, "fresh"), "keygen")
    assert is_error is False
    assert payload["key"]


def test_custom_scope_say_and_inbox_reach_only_peers_who_know_it(hub):
    a1, a2, outsider = (make_bridge(hub, n) for n in ("a1", "a2", "outsider"))
    pair, _ = call(a1, "keygen")
    scope = {"workspace": pair["workspace"], "key": pair["key"]}

    call(a1, "say", channel="plan", message="side conversation", custom_scope=scope)

    assert call(outsider, "inbox", channels=["plan"])[0]["count"] == 0
    payload, _ = call(a2, "inbox", channels=["plan"], custom_scope=scope)
    assert payload["count"] == 1
    assert payload["messages"][0]["body"] == "side conversation"


def test_custom_scope_claim_excludes_within_the_side_channel_only(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    pair, _ = call(a1, "keygen")
    scope = {"workspace": pair["workspace"], "key": pair["key"]}

    assert call(a1, "claim", resource="shared", custom_scope=scope)[0]["acquired"] is True
    # The same resource string on the default scope is untouched.
    assert call(a2, "claim", resource="shared")[0]["acquired"] is True
    # But within the side channel, a2 is excluded.
    payload, _ = call(a2, "claim", resource="shared", custom_scope=scope)
    assert payload["acquired"] is False

    assert call(a1, "release", resource="shared", custom_scope=scope)[0]["released"] is True
    assert call(a2, "claim", resource="shared", custom_scope=scope)[0]["acquired"] is True


def test_custom_scope_dm_after_discovering_a_peer_via_say(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    pair, _ = call(a1, "keygen")
    scope = {"workspace": pair["workspace"], "key": pair["key"]}

    call(a2, "say", channel="hello", message="a2 here", custom_scope=scope)
    inbox, _ = call(a1, "inbox", channels=["hello"], custom_scope=scope)
    a2_side_id = inbox["messages"][0]["from"]

    call(a1, "dm", to=a2_side_id, message="just you", custom_scope=scope)
    got, _ = call(a2, "inbox", custom_scope=scope)
    assert got["count"] == 1
    assert got["messages"][0]["body"] == "just you"


def test_custom_scope_missing_workspace_is_a_bad_argument(hub):
    payload, is_error = call(
        make_bridge(hub), "say", channel="x", message="y", custom_scope={"key": "z"}
    )
    assert is_error is True
    assert payload["error"] == "bad_arguments"


def test_custom_scope_tool_schemas_are_all_optional(hub):
    """custom_scope must never become required — every existing call, with no
    knowledge of it, has to keep working exactly as before."""
    covered = {"claim", "release", "say", "dm", "inbox"}
    for tool in TOOLS:
        if tool["name"] in covered:
            props = tool["inputSchema"]["properties"]
            assert "custom_scope" in props, tool["name"]
            assert "custom_scope" not in tool["inputSchema"]["required"], tool["name"]


# --- expired forecasts and self-calibration ----------------------------------


def test_a_forecast_whose_p95_has_passed_is_flagged_expired(hub):
    """Comparing two timestamps is arithmetic the model should not have to
    do, and the answer changes what the forecast is worth."""
    from datetime import datetime, timedelta, timezone

    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    a1.client.register(name="a1", channels=["build"])
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    a2.client.post("build", {
        "text": "long gone",
        "timing_forecast": {
            "p50": (stale - timedelta(minutes=5)).isoformat(),
            "p95": stale.isoformat(),
        },
    })
    msg = call(a1, "inbox")[0]["messages"][0]
    assert msg["body"] == "long gone"
    assert msg["timing_forecast"]["expired"] is True
    # The prediction itself is still visible; only annotated.
    assert "p50" in msg["timing_forecast"] and "p95" in msg["timing_forecast"]


def test_a_live_forecast_is_not_flagged(hub):
    a1, a2 = make_bridge(hub, "a1"), make_bridge(hub, "a2")
    call(a1, "whoami")
    a1.client.register(name="a1", channels=["build"])
    call(a2, "say", channel="build", message="working",
         execution_class="research", effort="high")
    msg = call(a1, "inbox")[0]["messages"][0]
    assert "expired" not in msg["timing_forecast"]


def test_whoami_reports_the_id_peers_address(hub):
    """The bridge had the same defect as the CLI (#90): it answered "how am I
    addressed?" with the process's own name, which is not what a peer sees
    once a workspace key blinds everything on the way out."""
    a1 = make_bridge(hub, "a1")
    a1.client.cipher = WorkspaceCipher.from_key("K" * 43, WS)
    a1.client.agent_id = a1.client.cipher.blind("a1", "agent")

    payload, _ = call(a1, "whoami")
    assert payload["local_agent_id"] == "a1"
    assert payload["agent_id"] == a1.client.agent_id != "a1"


def test_whoami_says_whether_this_workspace_is_sealed(hub):
    """The CLI's `whoami` has reported `encrypted` since it existed; the
    bridge did not, leaving the one surface whose caller cannot inspect its
    own environment unable to find out. An agent that believes it is sealed
    when it is not will say things here it would not say in the clear."""
    plain = make_bridge(hub, "plain")
    assert call(plain, "whoami")[0]["encrypted"] is False

    sealed = make_bridge(hub, "sealed")
    sealed.client.cipher = WorkspaceCipher.from_key("K" * 43, WS)
    assert call(sealed, "whoami")[0]["encrypted"] is True


def test_whoami_stays_quiet_until_there_is_enough_history(hub):
    a1 = make_bridge(hub, "a1")
    assert "forecast_calibration" not in call(a1, "whoami")[0]


def test_whoami_speaks_up_when_history_is_being_discarded(hub, tmp_path):
    """Silence below MIN_SAMPLES is right for an agent that simply has not
    worked yet, and wrong for one whose windows are all being thrown away —
    they look identical from here, and only the second is actionable.

    This is the state a per-call MCP server lands in: a fresh runtime id
    every time, so no declaration is ever closed by the run that opened it.
    """
    a1 = make_bridge(hub, "a1")
    ws = a1.config.workspace
    # A real file rather than the fixture's ":memory:", since the point is
    # several runs sharing one store.
    db = str(tmp_path / "timing.db")
    a1.timing = TimingModel(db, runtime_id="the-bridge")
    for run in range(3):
        TimingModel(db, runtime_id=f"declare-{run}").declare(
            "a1", ws, "coding", "medium", now=float(run * 100))
        TimingModel(db, runtime_id=f"look-{run}").note_look(
            "a1", ws, now=float(run * 100 + 30))

    report = call(a1, "whoami")[0]["forecast_calibration"]
    assert report["samples"] == 0
    assert report["discarded_from_other_runs"] == 3
    assert "SWITCHBOARD_RUNTIME_ID" in report["note"]


def test_whoami_surfaces_calibration_once_it_means_something(hub):
    """The data was otherwise dark: an agent could publish badly
    calibrated forecasts forever with no way to find out."""
    from switchboard.timing import MIN_SAMPLES

    a1 = make_bridge(hub, "a1")
    ws = a1.config.workspace
    t = 1_000_000.0
    for _ in range(MIN_SAMPLES + 2):
        a1.timing.declare("a1", ws, "coding", "medium", now=t)
        a1.timing.note_look("a1", ws, now=t + 30.0)
        t += 60
    report = call(a1, "whoami")[0]["forecast_calibration"]
    assert report["samples"] >= MIN_SAMPLES
    assert 0.0 <= report["p50_hit_rate"] <= 1.0


def test_badly_calibrated_forecasts_come_with_a_warning(hub):
    a1 = make_bridge(hub, "a1")
    ws = a1.config.workspace
    # Always look far later than any forecast predicts, so both hit rates
    # collapse and the agent is told its forecasts mislead.
    t = 1_000_000.0
    for _ in range(20):
        a1.timing.declare("a1", ws, "coding", "low", now=t)
        a1.timing.note_look("a1", ws, now=t + 3000.0)
        t += 4000
    report = call(a1, "whoami")[0]["forecast_calibration"]
    assert "note" in report


# --- what the model is told about signatures ---------------------------------
#
# Verification happens in the client; this is the boundary that decides what
# reaches the model. The rule is that only an actionable result crosses it.


def _project(message):
    from switchboard.mcp_server import Bridge

    return Bridge._msg(message)


def _msg(**over):
    base = {"seq": 1, "from": "alice", "channel": "build", "body": "hi",
            "created_at": "2026-08-09T00:00:00Z", "type": "note"}
    base.update(over)
    return base


def test_a_verified_message_says_nothing_about_it():
    # The overwhelmingly common path. A per-message "verified" line is noise,
    # and noise is how a warning stops being read.
    out = _project(_msg(signature={"status": "verified", "seq": 3, "key": "k" * 43}))
    assert "warning" not in out
    assert "signature" not in out and "key" not in out


def test_an_unknown_sender_says_nothing_either():
    # Usually just a roster we have not read. Reporting it would train the
    # model to skim past the case that matters.
    assert "warning" not in _project(_msg(signature={"status": "unknown", "seq": 1}))


def test_an_unsigned_message_says_nothing():
    assert "warning" not in _project(_msg(signature={"status": "unsigned"}))
    assert "warning" not in _project(_msg())


def test_a_mismatch_is_loud_and_names_what_to_do():
    out = _project(_msg(signature={"status": "mismatch", "seq": 2}))
    assert "warning" in out
    assert "alice" in out["warning"]
    assert "unattributed" in out["warning"]
    # the claim is "none of the keys I have seen verify this", not that some
    # registry vouched for a different one — and "registered" already means
    # binding a workspace credential to a hub
    assert "registered" not in out["warning"]


def test_a_gap_is_reported_because_it_is_otherwise_invisible():
    out = _project(_msg(signature={"status": "verified", "seq": 7, "missing": 2}))
    assert out["missing_before"] == 2
    assert "warning" not in out, "a gap is not an impersonation"


def test_the_raw_proof_never_reaches_the_model():
    # 43 characters of base64 per message with no decision attached to them.
    out = _project(_msg(signature={"status": "mismatch", "seq": 2, "key": "K" * 43}))
    assert "K" * 43 not in json.dumps(out)
