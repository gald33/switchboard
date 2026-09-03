"""Tests for `switchboard listen`, driven as the separate process it must be.

Everything else in this suite reaches the hub in-process. This one cannot, and
not because of how the listener is written: the whole mechanism is *a process
that exits*, so a faithful test needs a real process, a real socket and a real
hub. The fixture is therefore heavier than the rest of the suite — one
subprocess hub per module — and the deadlines are seconds rather than minutes
so it stays quick.

Both surfaces are exercised: the command, which is the implementation, and the
`init`-installed shim, which is what a repo's agents actually type.

What is being pinned here is not the shell. It is the four properties that are
silent when they break: a wake carries the message, the peek leaves it unread
for the session that was woken, a bounded park comes back on time, and a
listener that cannot understand its own arguments refuses to park rather than
parking forever.
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

from switchboard.cli import _wake_script
from switchboard.client import Client, ClientConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
HUB_TOKEN = "listener-test-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_hub(tmp_path_factory):
    """A hub on a real socket, because a shell script cannot dial an ASGI app."""
    pytest.importorskip("uvicorn")
    port = _free_port()
    db = tmp_path_factory.mktemp("hub") / "hub.db"
    # Explicit rather than inherited: whatever token happens to be exported on
    # the machine running the suite must not decide whether these pass.
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
            # A wake in one room leaves the other room's long-poll in flight
            # at the hub for up to 25 seconds — the hub does not notice a
            # client that vanished mid-wait — and graceful shutdown waits for
            # it. Ordinary in production (a deploy stops the container
            # regardless); here it is only the fixture's clock.
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def listener(live_hub, tmp_path, request):
    """The installed script, plus a `switchboard` on PATH that is *this* source.

    Without the shim the script would find whatever CLI happens to be installed
    on the developer's machine, which is how a test can pass against code that
    is not the code under test.
    """
    workspace = f"wake-{request.node.name[:40]}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "switchboard"
    shim.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH={REPO_ROOT / "src"} exec {sys.executable} -m switchboard.cli "$@"\n'
    )
    shim.chmod(0o755)

    script = tmp_path / "wake-on-message.sh"
    script.write_text(_wake_script(live_hub, workspace, bootstrap=False))
    script.chmod(0o755)

    base = ["switchboard", "--url", live_hub, "-w", workspace, "listen"]

    # Pinned rather than derived. An id is normally repo + branch + session,
    # and under pytest there is no session-id variable to hash — so each
    # subprocess would fall back to a per-process random and the listener
    # would park under an identity nothing else in the test can address.
    agent_id = f"listener-{abs(hash(workspace)) % 10**8}"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SWITCHBOARD_TOKEN": HUB_TOKEN,
        "SWITCHBOARD_AGENT_ID": agent_id,
    }
    # The script bakes in url and workspace; an inherited key would put the
    # listener in a different room than the peers these tests post from.
    env.pop("SWITCHBOARD_KEY", None)

    def run(*args, timeout=60):
        return subprocess.run(
            [*base, *args], capture_output=True, text=True,
            timeout=timeout, env=env, cwd=tmp_path,
        )

    def start(*args):
        return subprocess.Popen(
            [*base, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=tmp_path,
        )

    def via_shim(*args, timeout=60):
        """What a repo's own agents run: the file `init` installed."""
        return subprocess.run(
            ["sh", str(script), *args], capture_output=True, text=True,
            timeout=timeout, env=env, cwd=tmp_path,
        )

    run.via_shim = via_shim

    def client(agent_id):
        return Client(
            ClientConfig(url=live_hub, workspace=workspace, token=HUB_TOKEN),
            agent_id=agent_id,
        )

    run.agent_id = agent_id
    run.env = env
    run.start = start
    run.client = client
    run.workspace = workspace
    return run


def test_a_message_wakes_it_and_rides_out_on_stdout(listener):
    """The wake carries the event, so the session comes back holding it."""
    target = listener.agent_id
    peer = listener.client("peer")
    peer.post(f"@{target}", {"event": "push", "sha": "deadbee"})

    result = listener("--until", "+30", timeout=60)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["messages"][0]["body"] == {"event": "push", "sha": "deadbee"}


def test_the_woken_session_can_still_take_delivery(listener):
    """The listener peeks. It shares a read cursor with the session it serves,
    so draining here would consume the message it woke that session to read."""
    target = listener.agent_id
    listener.client("peer").post(f"@{target}", "still mine")

    assert listener("--until", "+30", timeout=60).returncode == 0

    # The session's own drain, as the woken agent would do it.
    still_waiting = listener.client(target).inbox()
    assert [m["body"] for m in still_waiting] == ["still mine"]


def test_a_bounded_park_comes_back_on_time(listener):
    """A park with no end is a promise to be reachable that nothing keeps."""
    started = time.monotonic()
    result = listener("--until", "+3", timeout=60)
    elapsed = time.monotonic() - started

    assert result.returncode == 2, result.stderr
    assert "with nothing to report" in result.stderr
    # The long-poll is capped at 25s server-side, so a listener that did not
    # clamp its last wait against the deadline would overshoot to ~25s here.
    # That margin is the whole of a short do-not-disturb.
    assert elapsed < 20, f"overshot its own deadline by {elapsed - 3:.0f}s"


def test_an_argument_it_cannot_understand_refuses_to_park(listener):
    """Regression: this used to print a complaint and then park with no
    deadline at all — `eval "$(...)" || exit` cannot fail, because eval of an
    empty string succeeds. Silently unbounded is the one outcome --until
    exists to prevent, so the failure has to be loud and immediate."""
    for bad in ("nonsense", "forecast:p90"):
        result = listener("--until", bad, timeout=30)
        assert result.returncode == 1, f"{bad!r} did not refuse: {result.stderr}"
        assert "--until" in result.stderr


def test_the_heartbeat_says_what_it_is_doing_and_when_it_stops(listener):
    """A peer reading the board learns both halves of the question it has:
    will this agent notice me, and if not now, when."""
    target = listener.agent_id
    proc = listener.start("--until", "+15")
    try:
        board = listener.client("observer")
        deadline = time.time() + 20
        entry = None
        while time.time() < deadline and entry is None:
            entry = board.board_entry(f"listener/{target}")
            time.sleep(0.25)
        assert entry is not None, "the listener never published a heartbeat"
        value = entry["value"]
        assert value["waiting_on"] == "inbox"
        assert value["until_source"] == "+15"
        assert "until" in value
        # The one key whose expiry is not housekeeping says so itself, because
        # the viewer renders every board key the same way and cannot know.
        assert "nobody is listening" in value["means"]
    finally:
        proc.wait(timeout=60)

    # And it cleans up behind itself, so a deliberate stop is visible at once
    # rather than at the end of the TTL.
    assert listener.client("observer").board_entry(f"listener/{target}") is None


def test_a_parked_listener_is_on_the_roster(listener):
    """Found the hard way: the listener wrote its heartbeat but never announced
    itself, so `agents` came back empty and `dm` warned the sender that their
    message would be "read by nobody" — while the listener was parked and
    working. A peer went looking for a corpse. Presence is what answers the
    question the sender actually asked."""
    proc = listener.start("--until", "+15")
    try:
        observer = listener.client("observer")
        deadline = time.time() + 20
        parked = None
        while time.time() < deadline and parked is None:
            parked = next(
                (a for a in observer.agents() if a["agent_id"] == listener.agent_id),
                None,
            )
            time.sleep(0.25)
        assert parked is not None, "a parked listener was invisible on the roster"
        # Not the task field — that belongs to the agent, and a listener writing
        # it every pass fought whatever the agent had published. `back_in` is
        # the fact a peer wants anyway, and the roster renders it as "away 12m".
        assert parked.get("back_in"), "a parked listener should say when it is back"
    finally:
        proc.wait(timeout=60)


def test_the_installed_shim_is_the_same_listener(listener):
    """`init` still writes a file, because a repo's agents should be able to
    arm one with a path and no knowledge of which room they are in. It is a
    shim now — one implementation, reachable two ways."""
    target = listener.agent_id
    listener.client("peer").post(f"@{target}", "through the shim")

    result = listener.via_shim("--until", "+30", timeout=60)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["messages"][0]["body"] == "through the shim"


def test_another_room_is_one_flag_away(listener, live_hub, tmp_path):
    """The gap that prompted the command. The generated wrapper bakes in one
    hub and workspace, so cross-repo work meant hand-assembling a script; every
    other command takes `-w`, and now so does this one."""
    elsewhere = f"{listener.workspace}-elsewhere"
    peer = Client(
        ClientConfig(url=live_hub, workspace=elsewhere, token=HUB_TOKEN),
        agent_id=listener.agent_id,
    )
    peer_client = Client(
        ClientConfig(url=live_hub, workspace=elsewhere, token=HUB_TOKEN),
        agent_id="peer-elsewhere",
    )
    peer_client.post(f"@{listener.agent_id}", "in the other room")

    result = subprocess.run(
        ["switchboard", "--url", live_hub, "-w", elsewhere, "listen", "--until", "+30"],
        capture_output=True, text=True, timeout=60,
        env=listener.env, cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["messages"][0]["body"] == "in the other room"
    # And it did not stray into the repo's own room on the way.
    assert elsewhere in result.stderr
    peer.close()
    peer_client.close()


def test_parking_does_not_paint_over_what_the_agent_is_doing(listener):
    """Seen in a live room: the roster text flapped between the agent's task and
    the listener's every pass, two writers under one id. Presence is this
    process's business; the work is the agent's, and `back_in` already tells a
    peer when to expect it."""
    peer = listener.client("observer")
    listener.client(listener.agent_id).register(
        name="worker", task="migrating the auth module", ttl=120)

    assert listener("--until", "+3", timeout=30).returncode == 2

    row = next(a for a in peer.agents() if a["agent_id"] == listener.agent_id)
    assert row["task"] == "migrating the auth module"


def test_parking_does_not_unsubscribe_you(listener):
    """Reported from a live room as "a listener with no subscriptions wakes only
    on DMs" — and the listener was the cause. It sent an empty channel list on
    every pass, so it removed the subscriptions of the agent it was serving."""
    me = listener.client(listener.agent_id)
    me.register(name="worker", channels=["general"], ttl=120)

    assert listener("--until", "+3", timeout=30).returncode == 2

    row = next(a for a in listener.client("observer").agents()
               if a["agent_id"] == listener.agent_id)
    assert row["channels"] == ["general"]


def test_a_channel_message_wakes_a_listener_that_named_no_channels(listener):
    """The consequence of the above, from the outside: subscriptions the agent
    already had must still deliver, or the room is busy while it sleeps."""
    me = listener.client(listener.agent_id)
    me.register(name="worker", channels=["general"], ttl=120)
    listener.client("peer").post("general", "does this wake you")

    result = listener("--until", "+30", timeout=60)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["messages"][0]["body"] == "does this wake you"


def test_the_deadline_can_come_from_the_agents_own_forecast(listener):
    """The quantile is the posture; the timing model supplies the number."""
    result = listener("--until", "forecast:p50", "--effort", "low", timeout=60)
    assert result.returncode in (0, 2), result.stderr
    # Says which quantile, and whether it was measured or is the wide prior —
    # a deadline built on a bootstrap default should be a shorter one.
    assert "p50" in result.stderr
    assert "bootstrap" in result.stderr or "sample(s)" in result.stderr


# --- one process, two rooms ----------------------------------------------------
#
# Observed on 2026-09-03: a maintainer session parked one listener in its
# repo's room and another in the lobby, two processes for one question — "did
# anyone want me?" — and the answer to which room a wake came from was left to
# whichever process exited first. One listener parks in both, and the process
# is where the rule about which message wins lives, so it can change without
# every agent that arms a listener learning a new habit.


def _with_key(listener, key):
    """The listener's environment with a workspace key, which the fixture
    strips: the lobby is derived from the key, so these need one."""
    return {**listener.env, "SWITCHBOARD_KEY": key}


def test_it_parks_in_the_lobby_too(listener, live_hub, tmp_path):
    """The default is to be findable. A peer that holds the same key and is
    in some other repo has one place to look — the lobby — and a DM there
    wakes a listener armed from this repo's room."""
    from switchboard import rooms
    from switchboard.crypto import generate_key

    key = generate_key()
    lobby = rooms.lobby(key).workspace
    proc = subprocess.Popen(
        [*listener_base(live_hub, listener.workspace), "--until", "+40"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_with_key(listener, key), cwd=tmp_path,
    )
    try:
        # The listener's id in the lobby is its local id blinded under the
        # lobby's cipher — the same derivation a peer's `agents` shows.
        me_there = Client(
            ClientConfig(url=live_hub, workspace=lobby, token=HUB_TOKEN, key=key),
            agent_id=listener.agent_id,
        )
        observer = Client(
            ClientConfig(url=live_hub, workspace=lobby, token=HUB_TOKEN, key=key),
            agent_id="peer-in-the-lobby",
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if any(a["agent_id"] == me_there.agent_id for a in observer.agents()):
                break
            time.sleep(0.25)
        else:
            pytest.fail("the listener never showed up in the lobby")
        observer.send(me_there.agent_id, "found you from another repo")
        out, err = proc.communicate(timeout=60)
    finally:
        proc.kill()
        me_there.close()
        observer.close()

    assert proc.returncode == 0, err
    wake = json.loads(out)
    assert wake["messages"][0]["body"] == "found you from another repo"
    assert wake["room"] == lobby and wake["role"] == "lobby"
    # And the exit line named both rooms, so the woken session knows where it
    # was reachable and where the message came from.
    assert listener.workspace in err and lobby in err and "[lobby]" in err


def test_no_lobby_parks_in_one_room(listener, live_hub, tmp_path):
    """The opt-out, for a session that does not want to be found. A DM in the
    lobby then wakes nobody, and the deadline is what brings it back."""
    from switchboard import rooms
    from switchboard.crypto import generate_key

    key = generate_key()
    lobby = rooms.lobby(key).workspace
    me_there = Client(
        ClientConfig(url=live_hub, workspace=lobby, token=HUB_TOKEN, key=key),
        agent_id=listener.agent_id,
    )
    peer = Client(
        ClientConfig(url=live_hub, workspace=lobby, token=HUB_TOKEN, key=key),
        agent_id="peer-in-the-lobby",
    )
    peer.send(me_there.agent_id, "nobody should hear this")
    result = subprocess.run(
        [*listener_base(live_hub, listener.workspace), "--no-lobby", "--until", "+6"],
        capture_output=True, text=True, timeout=60,
        env=_with_key(listener, key), cwd=tmp_path,
    )
    me_there.close()
    peer.close()
    assert result.returncode == 2, result.stderr
    assert lobby not in result.stderr


def test_without_a_key_there_is_no_lobby_and_it_says_so(listener):
    """A lobby is derived from the key. No key, no lobby — and the listener
    says which one room it is in rather than silently parking in one."""
    result = listener("--until", "+4")
    assert result.returncode == 2, result.stderr
    assert "no lobby" in result.stderr
    assert listener.workspace in result.stderr


def listener_base(live_hub, workspace):
    return ["switchboard", "--url", live_hub, "-w", workspace, "listen"]
