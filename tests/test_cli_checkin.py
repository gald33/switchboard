"""`switchboard checkin` registers on demand instead of failing.

Presence lasts `DEFAULT_AGENT_TTL` (120s), so a CLI agent meets the same 404
two ways: it never announced at all, or it went quiet for longer than the TTL.
The MCP bridge recovers from both silently (`Bridge._touch`), so the skill --
written against the bridge -- tells agents to `checkin` on a timer without
ever mentioning registration. On the CLI that advice used to fail outright,
which a real agent hit while coordinating: "checkin failed first with 'unknown
or expired agent' because I hadn't announced yet".

These bind the CLI to an in-process hub (`switchboard.testing`), so the 404
being recovered from is the real one the server raises rather than a mock's
idea of it.
"""

from __future__ import annotations

import json

import pytest

from switchboard.cli import main
from switchboard.client import SwitchboardError
from switchboard.testing import BASE_URL, hub

WS = "checkin-cli-ws"


@pytest.fixture
def cli_bound_to_a_real_hub(monkeypatch):
    import switchboard.cli as cli_module

    with hub(workspace=WS) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        yield handle


def _prefix(agent_id: str) -> list[str]:
    return ["--url", BASE_URL, "-w", WS, "--agent-id", agent_id, "--json"]


def _roster(capsys) -> list[dict]:
    code = main(["--url", BASE_URL, "-w", WS, "--json", "agents"])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_checkin_registers_when_the_agent_never_announced(
    cli_bound_to_a_real_hub, capsys, monkeypatch
):
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    code = main([*_prefix("cold-start"), "checkin"])
    assert code == 0
    capsys.readouterr()
    assert any(a["agent_id"] == "cold-start" for a in _roster(capsys))


def test_checkin_recovers_after_presence_expires(
    cli_bound_to_a_real_hub, capsys, monkeypatch
):
    """The recurring case, and the one documentation alone cannot fix: the
    agent *did* announce, then spent longer than its TTL doing real work."""
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    code = main([*_prefix("goes-quiet"), "announce", "--ttl", "1"])
    assert code == 0
    capsys.readouterr()

    cli_bound_to_a_real_hub.advance(2)  # outlive the TTL just announced
    assert not any(a["agent_id"] == "goes-quiet" for a in _roster(capsys))

    code = main([*_prefix("goes-quiet"), "checkin"])
    assert code == 0
    capsys.readouterr()
    assert any(a["agent_id"] == "goes-quiet" for a in _roster(capsys))


def test_checkin_still_reports_errors_that_are_not_missing_presence(
    cli_bound_to_a_real_hub, capsys, monkeypatch
):
    """Re-registering is the answer to a 404 specifically. Anything else --
    a dead hub, a rejected token -- must still surface, or the one command
    agents are told to call on a timer becomes the one that hides outages.
    """
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    import switchboard.cli as cli_module

    def _boom(self, **kwargs):
        raise SwitchboardError("hub exploded", status=500)

    monkeypatch.setattr(cli_module.Client, "heartbeat", _boom, raising=False)
    # `main` reports hub errors rather than raising them, so the evidence the
    # 500 was not swallowed is a failing exit code and the message on stderr.
    assert main([*_prefix("unlucky"), "checkin"]) != 0
    assert "hub exploded" in capsys.readouterr().err


def test_checkin_does_not_register_when_presence_is_healthy(
    cli_bound_to_a_real_hub, capsys, monkeypatch
):
    """The recovery is a fallback, not a second registration on every call --
    a re-register resets task and channels, so doing it unconditionally would
    quietly clear state the agent set with `announce`."""
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    code = main([*_prefix("healthy"), "announce", "--task", "keep me"])
    assert code == 0
    capsys.readouterr()

    code = main([*_prefix("healthy"), "checkin"])
    assert code == 0
    capsys.readouterr()

    held = [a for a in _roster(capsys) if a["agent_id"] == "healthy"]
    assert held and held[0]["task"] == "keep me"
