"""A CLI-only agent is one identity across commands, not one per process.

Observed 2026-09-27 on the managed hub. An agent that ran only the CLI did
`announce`, then `listen`, then `inbox`, then `whisper` — four processes, four
keypairs. `listen` re-registered with a fresh exchange key, the peer sealed a
whisper to it, `listen` opened it and exited, and `inbox` (a third key) could
not open it and marked it read: gone. Its reply was signed by yet another key,
so the peer watched the agent's key change on every command. `listen` also
re-registered under the directory-derived name, overwriting `announce --name`.

The fix is a per-agent signer (`switchboard signer`, see `signing.serve`): a
small long-lived process that holds the key in memory, which every command
attaches to, starting it on first use. These drive the real sequence through
the real CLI against an in-process hub, with a real signer subprocess.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from switchboard import signing
from switchboard.cli import main
from switchboard.crypto import generate_key
from switchboard.testing import BASE_URL, hub

WS = "cli-signer-ws"
AGENT = "cli-only-agent"


@pytest.fixture
def room(monkeypatch):
    import switchboard.cli as cli_module

    key = generate_key()
    # conftest turns the signer off for the suite, because a test that spawned
    # one per invented agent id would leave processes behind. This one wants
    # it, and stops what it started.
    monkeypatch.setenv("SWITCHBOARD_SIGNER", "on")
    with hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        try:
            yield handle, key
        finally:
            signing.stop_signer(AGENT)


def _cli(key: str, *args: str) -> int:
    return main(["--url", BASE_URL, "-w", WS, "--key", key, "--agent-id", AGENT,
                 *args])


def _row(peer, agent_hub_id: str) -> dict:
    [row] = [a for a in peer.agents() if a["agent_id"] == agent_hub_id]
    return row


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_announce_listen_inbox_whisper_speak_as_one_agent(room, capsys):
    h, key = room
    peer = h.client("peer")
    peer.register(name="peer")

    # 1. announce, with a chosen name.
    assert _cli(key, "--json", "announce", "--name", "cli-agent") == 0
    announced = json.loads(capsys.readouterr().out)
    me = announced["agent_id"]
    first = _row(peer, me)
    assert first["name"] == "cli-agent"

    # 2. listen, parked in the background as a real agent would run it.
    result: dict = {}

    def park() -> None:
        result["code"] = _cli(key, "listen", "--until", "+60", "--no-lobby")

    thread = threading.Thread(target=park, daemon=True)
    thread.start()
    _wait_for(lambda: bool(h.board()))  # the listener's heartbeat is up

    # listen re-registered. It must not have changed the key or the name.
    parked = _row(peer, me)
    assert parked["exchange_key"] == first["exchange_key"], \
        "listen re-registered with a different exchange key"
    assert parked["pubkey"] == first["pubkey"]
    assert parked["name"] == "cli-agent", "listen overwrote announce --name"

    # 3. the peer reads the roster and whispers.
    peer.whisper(me, "the migration is 0142")

    # 4. listen wakes with it, opened, and exits.
    thread.join(timeout=30)
    assert result.get("code") == 0
    woke = json.loads(capsys.readouterr().out)
    [msg] = woke["messages"]
    assert msg["body"] == "the migration is 0142"

    # 5. inbox, a new process, opens it too.
    assert _cli(key, "--json", "inbox") == 0
    [got] = json.loads(capsys.readouterr().out)["messages"]
    assert not got.get("unreadable"), "inbox could not open what listen could"
    assert got["body"] == "the migration is 0142"

    # 6. the reply is sealed and signed by the same identity.
    assert _cli(key, "whisper", "--strict", "peer", "ack") == 0
    capsys.readouterr()
    [reply] = peer.inbox()
    assert reply["body"] == "ack"
    final = _row(peer, me)
    assert final["pubkey"] == first["pubkey"]
    assert final["exchange_key"] == first["exchange_key"]
    assert not final.get("key_changed_while_live"), \
        "the peer saw this agent's key change between commands"


def test_the_signer_holds_the_key_only_in_memory(room):
    """The invariant from signing.py, checked rather than trusted: starting a
    signer leaves a socket and an empty lock file, and nothing else."""
    signer = signing.ensure_signer(AGENT)
    assert signer is not None
    again = signing.ensure_signer(AGENT)
    assert again.public_key == signer.public_key, "a second command started a second signer"

    for path in signing.socket_path(AGENT).parent.iterdir():
        if path.is_file():
            assert path.read_bytes() == b"", f"{path.name} holds data"


def test_no_signer_when_turned_off(room, monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_SIGNER", "off")
    assert signing.ensure_signer("nobody-started-this") is None


def test_signer_status_and_stop_from_the_cli(room, capsys):
    h, key = room
    assert _cli(key, "signer", "--status") != 0, "nothing is serving yet"
    capsys.readouterr()

    assert _cli(key, "--json", "announce") == 0     # starts one
    capsys.readouterr()
    assert _cli(key, "--json", "signer", "--status") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["running"] and status["pubkey"]

    assert _cli(key, "signer", "--stop") == 0
    capsys.readouterr()
    assert signing.attach(AGENT) is None, "the key outlived --stop"
