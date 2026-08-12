"""`help` on both surfaces: the protocol, served without a hub.

An agent reaches for this at the moment coordination is already not working —
no skill installed, an empty roster it cannot explain, a hub that does not
answer. So the property worth pinning is not the wording but the
independence: `help` must not need a reachable hub, a registration, or any
network at all, or it is missing exactly when it is wanted.

The other half is that it serves the *same* text `init` installs. Two copies
of a convention is how the convention stops being one.
"""

from __future__ import annotations

from switchboard.cli import build_parser, main
from switchboard.guidance import skill_text
from switchboard.mcp_server import TOOLS, Bridge, handle_request


def _mcp_help(bridge: Bridge) -> dict:
    return handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "help", "arguments": {}},
    })["result"]


def _hubless_bridge() -> Bridge:
    """A bridge with no hub client at all.

    Not a mock: the attribute is absent, so any code path that reaches for the
    hub raises rather than quietly succeeding against a fixture. That is the
    assertion — `help` answering here is proof it touched nothing.
    """
    bridge = Bridge.__new__(Bridge)
    bridge.client = None
    bridge.timing = None
    bridge._registered = False
    return bridge


def test_help_is_offered_as_a_tool() -> None:
    tool = next(t for t in TOOLS if t["name"] == "help")
    assert tool["inputSchema"]["properties"] == {}
    assert not tool["inputSchema"].get("required")


def test_help_answers_with_no_hub_and_no_registration() -> None:
    result = _mcp_help(_hubless_bridge())
    assert not result.get("isError")
    assert result["content"][0]["text"] == skill_text()


def test_help_is_served_as_prose_not_an_envelope() -> None:
    # Every other tool answers with a JSON object carrying `unread_dms`, a
    # count that only means anything because the hub was asked for it. This
    # one asks nothing, so it returns the document itself rather than wrapping
    # it in a shape that implies a hub round-trip happened.
    text = _mcp_help(_hubless_bridge())["content"][0]["text"]
    assert text.lstrip().startswith("---")
    assert not text.lstrip().startswith("{")


def test_cli_help_prints_the_protocol(capsys, monkeypatch) -> None:
    # A URL nothing is listening on: reaching the network here fails the test
    # rather than hanging on someone's real hub.
    monkeypatch.setenv("SWITCHBOARD_URL", "http://127.0.0.1:1")
    assert main(["help"]) == 0
    assert capsys.readouterr().out.rstrip("\n") == skill_text().rstrip("\n")


def test_cli_help_json_carries_the_same_text(capsys) -> None:
    import json

    assert main(["--json", "help"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skill"] == "switchboard-coordinate"
    assert payload["text"] == skill_text()


def test_help_is_not_shadowed_by_the_parsers_own_help() -> None:
    # argparse owns `--help`; `help` is a subcommand, and the two are easy to
    # confuse into one never being reachable.
    args = build_parser().parse_args(["help"])
    assert args.command == "help"
