"""The evening of 2026-09-03, replayed as a test, so it cannot happen again.

Two agents on one hub, holding two different keys, so no lobby in common.
Agent A (the ai-lab session) parks in its own room and leaves a rendezvous
note. Agent B (the maintainer) was handed an invite to that room once, a
week ago, and has forgotten it. What went wrong that night: B sat parked in
its repo room and lobby, A sat parked in its room, both reachable, neither
reached, until a human carried the coordinates.

What the tool is now expected to do, in order, with nothing remembered by B:

1. ``find review`` names the room A is in and says A is reachable *now*.
2. ``--room <label> dm`` reaches A there — B never retypes the invite or the
   key; the book resolves both from the invite it was handed a week ago.
3. A's listener wakes with B's message.
4. B parks with ``--in <label>`` and is woken by A's reply, in that room.

Driven as real processes on a real socket, like ``test_wake_listener.py``,
because the whole mechanism is processes that exit.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from switchboard import knownrooms
from switchboard.client import Client, ClientConfig
from switchboard.crypto import generate_key
from switchboard.invite import Invite

REPO_ROOT = Path(__file__).resolve().parent.parent
HUB_TOKEN = "evening-token"
ISLAND = "island-evening"
REPO_B = "repo-b-evening"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_hub(tmp_path_factory):
    pytest.importorskip("uvicorn")
    port = _free_port()
    db = tmp_path_factory.mktemp("hub") / "hub.db"
    env = {**os.environ, "SWITCHBOARD_DB": str(db), "SWITCHBOARD_TOKEN": HUB_TOKEN}
    proc = subprocess.Popen(
        [sys.executable, "-m", "switchboard.cli", "serve", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail("the hub exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the hub never came up")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


class Agent:
    """One agent: its own key, its own book, its own id, its own shell."""

    def __init__(self, name: str, live_hub: str, tmp_path: Path, bin_dir: Path):
        self.name = name
        self.url = live_hub
        self.key = generate_key()
        self.book = tmp_path / f"{name}-known-rooms.json"
        self.cwd = tmp_path / name
        self.cwd.mkdir()
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "SWITCHBOARD_TOKEN": HUB_TOKEN,
            "SWITCHBOARD_AGENT_ID": name,
            "SWITCHBOARD_KNOWN_ROOMS": str(self.book),
        }
        self.env.pop("SWITCHBOARD_KEY", None)

    def run(self, *args, timeout=60):
        return subprocess.run(
            ["switchboard", "--url", self.url, *args], capture_output=True,
            text=True, timeout=timeout, env=self.env, cwd=self.cwd,
        )

    def start(self, *args):
        return subprocess.Popen(
            ["switchboard", "--url", self.url, *args], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=self.env, cwd=self.cwd,
        )

    def id_in(self, workspace: str, key: str) -> str:
        """This agent's blinded id in a room, as a peer's roster shows it."""
        return Client(ClientConfig(url=self.url, workspace=workspace, token=HUB_TOKEN,
                                   key=key), agent_id=self.name).agent_id


@pytest.fixture
def agents(live_hub, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "switchboard"
    shim.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH={REPO_ROOT / "src"} exec {sys.executable} -m switchboard.cli "$@"\n'
    )
    shim.chmod(0o755)
    return (Agent("agent-a", live_hub, tmp_path, bin_dir),
            Agent("agent-b", live_hub, tmp_path, bin_dir))


def _wait_for(predicate, what: str, seconds: float = 25.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    pytest.fail(f"timed out waiting for {what}")


def _wake(proc: subprocess.Popen, timeout: float = 60) -> dict:
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, f"listener did not wake: exit {proc.returncode}\n{err}"
    return json.loads(out)


def test_the_evening_replayed(agents, live_hub):
    a, b = agents
    island_invite = Invite(url=live_hub, workspace=ISLAND, token=HUB_TOKEN, key=a.key,
                           note="island-rehearsal: meet here").encode()

    # --- a week ago: B was handed A's invite once, and verified it -------------
    joined = b.run("join", island_invite, "--no-verify")
    assert joined.returncode == 0, joined.stderr
    (room,) = knownrooms.Book(str(b.book)).rooms()
    assert room.label == "island-rehearsal" and room.key["from"] == "invite"
    # A week passes. The book keeps the room; nothing else about B does.
    aged = knownrooms.Book(str(b.book))
    aged.rooms()
    aged._rooms[0].last_used -= 7 * 86400
    aged._save()
    assert a.key not in b.book.read_text().replace(island_invite, ""), \
        "the key is in the book only as the invite it arrived as"

    # --- tonight: A is in its room, has left a note, and is parked ------------
    noted = a.run("-w", ISLAND, "--key", a.key, "rendezvous", "review",
                  "--want", "need switchboard#208 reviewed", "--wait", "0", "--here")
    assert noted.returncode == 0, noted.stderr
    a_listener = a.start("-w", ISLAND, "--key", a.key, "listen", "--until", "+90",
                         "--no-lobby")
    a_id = a.id_in(ISLAND, a.key)
    observer = Client(ClientConfig(url=live_hub, workspace=ISLAND, token=HUB_TOKEN,
                                   key=a.key), agent_id="observer")
    _wait_for(lambda: any(e["key"] == f"listener/{a_id}"
                          for e in observer.board_list(prefix="listener/")),
              "A's listener heartbeat")

    # --- B, in its own repo room with its own key, looks for A ---------------
    # No lobby in common: B's key is not A's. This is the search that failed
    # for an hour on the night; now it is one command.
    found = b.run("-w", REPO_B, "--key", b.key, "--json", "find", "review")
    assert found.returncode == 0, found.stderr
    report = json.loads(found.stdout)
    assert report["found"]
    (hit,) = [r for r in report["rooms"] if r["roster"] or r["notes"]]
    assert hit["label"] == "island-rehearsal" and hit["workspace"] == ISLAND
    assert a_id in hit["reachable"], "find must say A can be woken right now"
    assert any(n["agent_id"] == a_id for n in hit["notes"])

    # --- B reaches A in that room by label: no invite, no key retyped ----------
    sent = b.run("--room", "island-rehearsal", "dm", a_id, "found you — reviewing now")
    assert sent.returncode == 0, sent.stderr
    wake = _wake(a_listener)
    assert wake["role"] == "room" and wake["room"] == ISLAND
    assert wake["messages"][0]["body"] == "found you — reviewing now"
    b_id_there = wake["messages"][0]["from"]
    assert b_id_there == b.id_in(ISLAND, a.key)

    # --- B parks for the reply, naming the room; A answers where it was asked --
    b_listener = b.start("-w", REPO_B, "--key", b.key, "listen", "--in", "island-rehearsal",
                         "--until", "+90", "--no-lobby")
    _wait_for(lambda: any(e["key"] == f"listener/{b_id_there}"
                          for e in observer.board_list(prefix="listener/")),
              "B's listener heartbeat in the island room")
    reply = a.run("-w", ISLAND, "--key", a.key, "dm", b_id_there, "thanks, holding for the go")
    assert reply.returncode == 0, reply.stderr
    wake = _wake(b_listener)
    assert wake["role"] == "island-rehearsal" and wake["room"] == ISLAND
    assert wake["messages"][0]["body"] == "thanks, holding for the go"
    observer.close()


def test_without_the_book_the_evening_repeats(agents, live_hub):
    """The control: the same two agents with B's book disabled cannot find each
    other, which is what happened. Kept so the fix stays a fix."""
    a, b = agents
    a.run("-w", ISLAND, "--key", a.key, "rendezvous", "review",
          "--want", "need a review", "--wait", "0", "--here")
    b.env["SWITCHBOARD_KNOWN_ROOMS"] = ""
    found = b.run("-w", REPO_B, "--key", b.key, "--json", "find", "review")
    assert found.returncode != 0
    swept = b.run("-w", REPO_B, "--key", b.key, "--json", "rendezvous", "review",
                  "--want", "the same review", "--wait", "0")
    assert swept.returncode == 0, swept.stderr
    out = json.loads(swept.stdout)
    assert out["elsewhere"] == [] and out["roster"] == [] and out["notes"] == []
