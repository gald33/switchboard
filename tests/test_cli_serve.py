"""Starting a hub, and telling its own clients how to find it.

A client with nothing configured dials the managed hub, so `serve` in one
terminal and an agent in another no longer meet by default. The agent reaches
something real and coordinates with nobody, which is precisely why it never
complains — `serve` is the one moment both halves are known at once: what is
being started, and what a client standing in this directory would dial instead.
"""

from __future__ import annotations

import json

import pytest

from switchboard.cli import _serve_client_note, _would_reach
from switchboard.config import MANAGED_HUB_URL

LOOPBACK = "http://127.0.0.1:8787"


@pytest.mark.parametrize("url,host,port", [
    ("http://127.0.0.1:8787", "127.0.0.1", 8787),
    ("http://localhost:8787", "127.0.0.1", 8787),  # both loopback
    ("http://127.0.0.1:8787", "0.0.0.0", 8787),  # bound everywhere
    ("http://hub.example.com:9000", "hub.example.com", 9000),
    ("http://HUB.example.com:9000", "hub.example.com", 9000),  # case
])
def test_a_client_that_would_reach_this_hub(url, host, port):
    assert _would_reach(url, host, port)


@pytest.mark.parametrize("url,host,port", [
    (MANAGED_HUB_URL, "127.0.0.1", 8787),
    ("http://127.0.0.1:9999", "127.0.0.1", 8787),  # wrong port
    ("http://192.168.1.10:8787", "127.0.0.1", 8787),  # not this machine
    ("https://hub.example.com", "127.0.0.1", 8787),  # implicit 443
])
def test_a_client_that_would_not(url, host, port):
    assert not _would_reach(url, host, port)


def test_the_note_names_what_clients_will_dial_and_how_to_fix_it():
    note = _serve_client_note(MANAGED_HUB_URL, "127.0.0.1", 8787)
    assert note is not None
    assert MANAGED_HUB_URL in note
    assert "export SWITCHBOARD_URL=http://127.0.0.1:8787" in note


def test_binding_everywhere_still_suggests_an_address_a_client_can_dial():
    """`0.0.0.0` is a binding instruction, not somewhere to connect to."""
    note = _serve_client_note(MANAGED_HUB_URL, "0.0.0.0", 8787)
    assert "export SWITCHBOARD_URL=http://127.0.0.1:8787" in note
    assert "0.0.0.0" not in note


def test_no_note_when_the_environment_already_points_here():
    assert _serve_client_note(LOOPBACK, "127.0.0.1", 8787) is None


def test_no_note_when_the_repo_committed_this_hub(tmp_path, monkeypatch):
    """`switchboard init --local` is the intended happy path, and it must not
    be nagged at every start."""
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"switchboard": {"command": "switchboard-mcp",
                                       "env": {"SWITCHBOARD_URL": LOOPBACK}}}
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)

    from switchboard.cli import _apply_repo_url
    from switchboard.config import ClientConfig

    config = ClientConfig.from_env()
    _apply_repo_url(config, tmp_path)
    assert config.url == LOOPBACK
    assert _serve_client_note(config.url, "127.0.0.1", 8787) is None


def test_announce_and_register_are_the_same_command():
    """`register` overstated what happens: the record is self-asserted, expires
    in two minutes, and nothing validates it. The old name still works, since
    it is in released docs and scripts."""
    from switchboard.cli import build_parser, cmd_register

    parser = build_parser()
    for name in ("announce", "register"):
        assert parser.parse_args([name, "--name", "x"]).func is cmd_register, name
