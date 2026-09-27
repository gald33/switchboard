"""Tests for the MCP bridge's streamable HTTP transport (`switchboard-mcp --http`).

Over a real socket, because the transport is the thing under test: the
routing, the token checks and the status codes are what a host like ChatGPT
actually sees. The server handles requests on this thread, as it does in
production — see `make_http_server` for why it is not threaded — so each
request is sent from a helper thread while this one answers it.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest
from test_mcp import WS, call, make_bridge

from switchboard import mcp_server
from switchboard.mcp_server import TOOLS, make_http_server
from switchboard.testing import hub as make_hub

TOKEN = "s3cret-token"


@pytest.fixture
def hub():
    with make_hub(workspace=WS) as handle:
        yield handle


@pytest.fixture
def served(hub):
    """A factory for a bound server; closes whatever it made."""
    made = []

    def serve(token: str | None = TOKEN):
        server = make_http_server(make_bridge(hub), "127.0.0.1", 0, token)
        made.append(server)
        return server

    yield serve
    for server in made:
        server.server_close()


def send(server, method: str = "POST", path: str = "/mcp", body=None,
         headers: dict[str, str] | None = None) -> httpx.Response:
    """Make one request against `server`, answering it on this thread."""
    port = server.server_address[1]
    out: dict[str, httpx.Response] = {}

    def client() -> None:
        content = None if body is None else (
            body if isinstance(body, (bytes, str)) else json.dumps(body))
        out["response"] = httpx.request(
            method, f"http://127.0.0.1:{port}{path}", content=content,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream", **(headers or {})},
            timeout=10, trust_env=False,
        )

    thread = threading.Thread(target=client)
    thread.start()
    server.handle_request()
    thread.join(10)
    return out["response"]


def rpc(method: str, request_id: int | None = 1, **params):
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        message["id"] = request_id
    return message


# --- the token --------------------------------------------------------------


def test_the_token_in_the_path_is_the_credential(served):
    server = served()
    response = send(server, path=f"/mcp/{TOKEN}", body=rpc("initialize"))
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == "switchboard"


def test_a_bearer_token_is_accepted_at_the_bare_path(served):
    server = served()
    response = send(server, body=rpc("ping"),
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_no_token_is_refused(served):
    server = served()
    response = send(server, body=rpc("tools/list"))
    assert response.status_code == 401
    assert "result" not in response.json()


def test_a_wrong_bearer_token_is_refused(served):
    server = served()
    response = send(server, body=rpc("tools/list"),
                    headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_a_wrong_token_in_the_path_is_not_found(served):
    # Not 401: the endpoint should not confirm that a guessed shape was right.
    server = served()
    assert send(server, path="/mcp/nope", body=rpc("tools/list")).status_code == 404


def test_the_token_is_not_written_to_the_log(served, capsys):
    server = served()
    send(server, path=f"/mcp/{TOKEN}", body=rpc("ping"))
    assert TOKEN not in capsys.readouterr().err


# --- without a token --------------------------------------------------------


def test_an_open_endpoint_serves_local_callers(served):
    server = served(token=None)
    response = send(server, body=rpc("ping"), headers={"Origin": "http://localhost:6274"})
    assert response.status_code == 200


def test_an_open_endpoint_refuses_a_foreign_origin(served):
    # DNS rebinding: a web page reaching a loopback server through its own name.
    server = served(token=None)
    response = send(server, body=rpc("tools/list"), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_no_auth_is_refused_off_loopback():
    with pytest.raises(SystemExit):
        mcp_server._parse_args(["--http", "--no-auth", "--host", "0.0.0.0"])


def test_http_without_a_token_mints_one(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_MCP_TOKEN", raising=False)
    args = mcp_server._parse_args(["--http"])
    assert args.token and len(args.token) >= 24


def test_the_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_MCP_TOKEN", "from-env")
    assert mcp_server._parse_args(["--http"]).token == "from-env"


# --- the protocol over HTTP -------------------------------------------------


def test_tools_list_carries_read_only_annotations(served):
    server = served()
    tools = send(server, path=f"/mcp/{TOKEN}",
                 body=rpc("tools/list")).json()["result"]["tools"]
    assert [t["name"] for t in tools] == [t["name"] for t in TOOLS]
    hints = {t["name"]: t["annotations"]["readOnlyHint"] for t in tools}
    assert hints["roster"] is True
    assert hints["claim"] is False
    # Reading the inbox moves a cursor, so it is not read-only.
    assert hints["inbox"] is False


def test_a_tool_call_reaches_the_hub(hub, served):
    server = served()
    result = send(server, path=f"/mcp/{TOKEN}", body=rpc(
        "tools/call", name="claim", arguments={"resource": "docs/chatgpt.md"},
    )).json()["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["acquired"] is True
    # Held on the real hub: an agent on stdio is now shut out of it.
    payload, _ = call(make_bridge(hub, "stdio-agent"), "claim", resource="docs/chatgpt.md")
    assert payload["acquired"] is False
    assert payload["held_by"] == "mcp-agent"


def test_a_notification_is_accepted_with_no_body(served):
    server = served()
    response = send(server, path=f"/mcp/{TOKEN}",
                    body=rpc("notifications/initialized", request_id=None))
    assert response.status_code == 202
    assert response.content == b""


def test_a_batch_gets_a_batch_back(served):
    server = served()
    response = send(server, path=f"/mcp/{TOKEN}", body=[
        rpc("ping", request_id=1),
        rpc("notifications/initialized", request_id=None),
        rpc("tools/list", request_id=2),
    ])
    assert [r["id"] for r in response.json()] == [1, 2]


def test_malformed_json_is_a_parse_error(served):
    server = served()
    response = send(server, path=f"/mcp/{TOKEN}", body="{not json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == mcp_server.JSONRPC_PARSE_ERROR


def test_get_is_refused_since_there_is_no_stream(served):
    server = served()
    response = send(server, method="GET", path=f"/mcp/{TOKEN}")
    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"


def test_other_paths_are_not_found(served):
    server = served()
    assert send(server, path="/", body=rpc("ping")).status_code == 404
