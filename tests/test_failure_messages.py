"""What a failure says about what it tried.

Two errors in this CLI were a sentence that could not be acted on. A refused
connection escaped as forty frames of httpx traceback with the one useful fact
— which URL — nowhere in it (#88). And `invalid or missing bearer token` reads
identically whether nothing was sent, a stale export was, or the checkout's own
default was; a token is an opaque string, so even printing it would not
separate them.

Both were found the same way: someone typed a command, got a true sentence, and
drew the wrong conclusion from it. The lobby flag was blamed for a stale
`SWITCHBOARD_TOKEN` left over from a throwaway hub.
"""

from __future__ import annotations

from switchboard.cli import _tried, main
from switchboard.client import SwitchboardError
from switchboard.config import ClientConfig


def test_it_names_the_hub_and_where_the_url_came_from():
    line = _tried(ClientConfig(url="https://hub.example.com", url_source="mcp.json"))
    assert "https://hub.example.com" in line
    assert ".mcp.json" in line


def test_it_names_where_the_token_came_from():
    """The distinction that matters: a stale shell export is a different
    problem from a repo default, and both produce the same 401."""
    shell = _tried(ClientConfig(url="https://h", token="x", token_source="env"))
    checkout = _tried(ClientConfig(url="https://h", token="x",
                                   token_source="settings.local.json"))
    assert "in this shell" in shell
    assert "in this checkout" in checkout


def test_no_token_is_said_out_loud_rather_than_left_blank():
    assert "no token at all" in _tried(ClientConfig(url="https://h"))


def test_a_default_url_says_nobody_chose_it():
    """A loopback default inherited by a cloud session is one of the documented
    ways an agent ends up alone, so 'nobody chose this' has to be visible."""
    assert "built-in default" in _tried(ClientConfig(url="http://127.0.0.1:8787"))


def test_a_401_says_what_it_tried_and_what_to_do(monkeypatch, capsys):
    def refuse(args):
        raise SwitchboardError("invalid or missing bearer token", status=401)

    monkeypatch.setattr("switchboard.cli.cmd_agents", refuse)
    monkeypatch.setenv("SWITCHBOARD_TOKEN", "stale-from-a-throwaway-hub")

    code = main(["--url", "https://hub.example.com", "agents"])

    err = capsys.readouterr().err
    assert code == 1
    assert "invalid or missing bearer token" in err
    assert "https://hub.example.com" in err
    assert "SWITCHBOARD_TOKEN in this shell" in err
    assert "--invite" in err, "the flag that carries a token should be offered"


def test_another_error_is_left_alone(monkeypatch, capsys):
    """Only 401 gets the credential paragraph. A lease conflict explaining
    where your token came from would be noise."""
    def refuse(args):
        raise SwitchboardError("board revision conflict", status=409)

    monkeypatch.setattr("switchboard.cli.cmd_agents", refuse)
    main(["--url", "https://hub.example.com", "agents"])
    assert "token" not in capsys.readouterr().err


def test_a_refused_connection_names_the_url(capsys):
    """#88: this used to be a raw traceback. Port 9 is discard — nothing
    listens, and the refusal is immediate."""
    code = main(["--url", "http://127.0.0.1:9", "agents"])

    err = capsys.readouterr().err
    assert code == 1
    assert "http://127.0.0.1:9" in err
    assert "Traceback" not in err
    assert "SWITCHBOARD_URL" in err
