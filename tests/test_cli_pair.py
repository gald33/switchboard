"""`switchboard pair`, and `--room pair:<name>`, driven as two machines.

The protocol is pinned in `test_pairing.py`. What these pin is the surface,
where its failures are quieter: a code printed with a caveat glued to it, so
the caller pastes both; a leaked code reported as an ordinary error a script
retries; a command that ran in a work room the other side had already left,
or that the other side had ended, and said nothing; a listener parked
everywhere except the one room the answer was going to land in.

A machine, to the CLI, is its environment: its own pair store, known-rooms
book, local databases and agent id. So two machines are one hub and an
environment switched between `main()` calls. The hub runs on a clock the
tests move, and the CLI reads pairing deadlines through `cli._pairing_now`,
which these point at that clock — without it every code would look years
expired against the wall clock.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import switchboard.cli as cli
from switchboard import knownrooms, pairing, rooms
from switchboard.cli import build_parser, main
from switchboard.client import detect_identity
from switchboard.crypto import generate_key
from switchboard.invite import Invite
from switchboard.pairing import PairStore
from switchboard.testing import BASE_URL, hub

WS = "pair-cli-ws"


def _be(machine: str, tmp_path, monkeypatch) -> None:
    """Make this process `machine`: everything a machine keeps, pointed at its own."""
    home = tmp_path / machine
    monkeypatch.setenv("SWITCHBOARD_PAIRS", str(home / "pairs"))
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", machine)
    monkeypatch.setenv("SWITCHBOARD_KNOWN_ROOMS", str(home / "known-rooms.json"))
    monkeypatch.setenv("SWITCHBOARD_PEER_DB", str(home / "peers.db"))
    monkeypatch.setenv("SWITCHBOARD_TIMING_DB", str(home / "timing.db"))


@pytest.fixture
def h(monkeypatch, tmp_path):
    with hub(workspace=WS) as handle:
        monkeypatch.setattr(cli, "Client", handle.client_class())
        monkeypatch.setattr(cli, "_pairing_now", lambda: handle.now)
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        # Out of the checkout, so its .mcp.json and settings do not choose
        # the room for a test that is about choosing rooms.
        monkeypatch.chdir(tmp_path)
        yield handle


@pytest.fixture
def run(h, tmp_path, monkeypatch, capsys):
    """`run(machine, *argv)` -> (exit code, stdout, stderr), as that machine."""

    def run_(machine: str, *argv: str) -> tuple[int, str, str]:
        _be(machine, tmp_path, monkeypatch)
        capsys.readouterr()
        code = main(["--url", BASE_URL, *argv])
        out, err = capsys.readouterr()
        return code, out, err

    return run_


def store_of(tmp_path, machine: str) -> PairStore:
    """A machine's pair store read directly, as the oracle — never through the CLI."""
    return PairStore(tmp_path / machine / "pairs", env={})


def pair_up(run, name: str = "lab") -> str:
    """Laptop starts, cloud accepts, laptop collects. Returns the code."""
    code, out, _ = run("laptop", "pair", "start", "--name", name)
    assert code == 0
    blob = out.strip()
    assert run("cloud", "pair", "accept", blob)[0] == 0
    assert run("laptop", "pair", "status", name)[0] == 0
    return blob


def _wait_for(capsys, needle: str, *, timeout: float = 10.0) -> tuple[str, str]:
    """Read what a thread prints until `needle` shows up on stderr."""
    out, err = "", ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        captured = capsys.readouterr()
        out, err = out + captured.out, err + captured.err
        if needle in err:
            return out, err
        time.sleep(0.05)
    raise AssertionError(f"never saw {needle!r} on stderr; got:\n{err}")


def _in_thread(argv: list[str]) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def target() -> None:
        try:
            result["code"] = main(argv)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def _join(thread: threading.Thread) -> None:
    """Wait for the command, and for the listener's own room threads after it.

    A room's thread clears its heartbeat on the way out, which can run just
    after `main()` has returned — and, in a test, after the hub it was talking
    to has been torn down, where it fails as a thread nobody joined.
    """
    thread.join(timeout=20)
    for other in threading.enumerate():
        if other.name.startswith("listen:"):
            other.join(timeout=10)


# --- start and accept ----------------------------------------------------------


def test_start_prints_the_code_alone_and_its_caveats_elsewhere(run):
    """The code is the artifact: a caller piping stdout somewhere must get the
    code and nothing that could be pasted along with it. What the code is
    worth goes to stderr — and the credential caveat survives `-q`, because
    the caller most likely to pass `-q` is an agent that will paste it."""
    code, out, err = run("laptop", "pair", "start", "--name", "lab")
    assert code == 0
    assert out.count("\n") == 1 and out.startswith(pairing.CODE_PREFIX)
    assert "credential" in err and "pair accept" in err
    assert out.strip() not in err

    code, out, err = run("laptop", "-q", "pair", "start", "--name", "quiet")
    assert code == 0 and out.startswith(pairing.CODE_PREFIX)
    assert "credential" in err
    assert "pair accept <the code>" not in err


def test_start_in_a_cloud_session_says_to_start_on_the_machine_that_stays(
        run, monkeypatch):
    """The initiator keeps the handshake until it collects. Started in a
    container that is thrown away first, the pairing goes with it — so the
    code is still made, and the warning says which side should have made it."""
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "1")
    code, out, err = run("cloud", "-q", "pair", "start")
    assert code == 0 and out.startswith(pairing.CODE_PREFIX)
    assert "cloud session" in err and "machine that stays" in err


def test_accept_prints_the_room_handle_and_says_it_is_unconfirmed(run):
    """The handle is the one string both sides use; the pair secret is never
    printed. Until the other side collects, the responder is alone in work
    room 0, and a responder told nothing would start talking to nobody."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    code, out, err = run("cloud", "pair", "accept", out.strip())
    assert code == 0
    assert out == "pair:lab\n"
    assert "unconfirmed" in err and "pair wait lab" in err


def test_status_collects_and_both_sides_agree_on_the_sas(run):
    """`status` is one of the touches a turn-based initiator collects on. After
    it, both sides are paired and show the same short authentication string —
    the one check against a code that was raced."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    run("cloud", "pair", "accept", out.strip())

    code, out, _ = run("laptop", "--json", "pair", "status")
    assert code == 0
    [laptop] = json.loads(out)["pairs"]
    assert (laptop["name"], laptop["role"], laptop["state"]) == ("lab", "initiator", "paired")

    code, out, _ = run("cloud", "--json", "pair", "status", "lab")
    assert code == 0
    [cloud] = json.loads(out)["pairs"]
    assert (cloud["role"], cloud["state"]) == ("responder", "paired")
    assert cloud["sas"] == laptop["sas"]
    assert len(laptop["sas"]) == 9 and laptop["sas"][4] == "-"


def test_status_of_a_pair_this_machine_does_not_have_fails(run):
    """A script asking about one pair by name is asking whether it is usable;
    a row saying "unknown" with exit 0 would answer yes."""
    code, out, _ = run("laptop", "--json", "pair", "status", "nope")
    assert code == 1
    [row] = json.loads(out)["pairs"]
    assert row["state"] == "unknown"


# --- the room handle -------------------------------------------------------------


def test_a_paired_room_carries_messages_between_the_two_machines(run):
    """The whole point, end to end: `--room pair:lab` on each side lands in the
    same work room, though neither side ever sent the other its key."""
    pair_up(run)
    assert run("laptop", "--room", "pair:lab", "say", "pair", "hi from the laptop")[0] == 0

    code, out, _ = run("cloud", "--room", "pair:lab", "inbox", "-c", "pair")
    assert code == 0 and "hi from the laptop" in out
    code, out, _ = run("cloud", "--room", "pair:lab", "history", "pair")
    assert code == 0 and "hi from the laptop" in out

    assert run("cloud", "--room", "pair:lab", "say", "pair", "and back")[0] == 0
    code, out, _ = run("laptop", "--room", "pair:lab", "inbox", "-c", "pair")
    assert code == 0 and "and back" in out


def test_the_room_handle_works_after_the_subcommand_too(run):
    """`say --room pair:lab …` is how an agent told "use --room pair:lab" types
    it. Before `--room` was a global after the subcommand too, `pair:lab`
    would have been taken for the channel and posted to, in the wrong room."""
    args = build_parser().parse_args(["say", "general", "hi", "--room", "pair:lab"])
    assert (args.room, args.channel, args.message) == ("pair:lab", "general", ["hi"])

    pair_up(run)
    assert run("cloud", "say", "--room", "pair:lab", "pair", "flag after")[0] == 0
    code, out, _ = run("laptop", "history", "pair", "--room", "pair:lab")
    assert code == 0 and "flag after" in out


def test_a_paired_work_room_is_never_written_into_the_book(run, tmp_path):
    """A heal retires the work room, and the book keys rooms on their
    workspace — so a recorded work room is a pointer to a room the pair may
    have left because its key got out."""
    pair_up(run)
    run("laptop", "--room", "pair:lab", "say", "pair", "hi")
    run("laptop", "--room", "pair:lab", "invite")
    book = knownrooms.Book(str(tmp_path / "laptop" / "known-rooms.json"))
    record = store_of(tmp_path, "laptop").get("lab")
    work = pairing.work_room(record.pair_secret, 0).workspace
    assert all(room.workspace != work for room in book.rooms())


def test_a_book_label_cannot_pose_as_a_pair_handle(run):
    """`--room pair:x` never reads the book, so a book room labelled `pair:x`
    could not be reached by its label, and a pair called x would answer to
    it instead."""
    with pytest.raises(SystemExit, match="reserved"):
        run("laptop", "join", "swb1_whatever", "--save", "pair:x")
    with pytest.raises(SystemExit, match="reserved"):
        run("laptop", "rooms", "--label", "w_abc=pair:x")


# --- the code is not an invite, and leaks are loud ---------------------------------


def test_a_redeemed_code_exits_with_the_conflict_code_and_says_it_leaked(run):
    """The one refusal that means somebody else holds the code. Exit 1 is what
    a flaky network gets and a script retries; this must not look like that."""
    blob = pair_up(run)
    code, out, err = run("stranger", "pair", "accept", blob)
    assert code == 2
    assert out == ""
    assert "leaked" in err and err.startswith("error: ")


def test_a_second_answer_before_collection_is_refused_as_a_leak(run):
    """Detection, not exclusion: anyone holding the code can answer while it
    is redeemable. The second one is told the code leaked, not "try again"."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    assert run("cloud", "pair", "accept", out.strip())[0] == 0
    code, _, err = run("stranger", "pair", "accept", out.strip())
    assert code == 2 and "leaked" in err


def test_a_pairing_code_is_refused_by_name_where_an_invite_goes(run, capsys):
    """Joined as an invite, a pairing code would be a handshake room entered in
    the open and written into the book. Refused — and the refusal says what
    the string is and where it goes, rather than "not an invite"."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    blob = out.strip()

    code, _, err = run("laptop", "--invite", blob, "agents")
    assert code == 1 and "pairing code" in err and "pair accept" in err
    code, _, err = run("laptop", "join", blob)
    assert code == 1 and "pairing code" in err and "pair accept" in err

    invite_blob = Invite(url=BASE_URL, workspace=WS).encode()
    code, _, err = run("cloud", "pair", "accept", invite_blob)
    assert code == 1 and "room invite, not a pairing code" in err


# --- heal and forget -----------------------------------------------------------------


def test_heal_moves_both_sides_and_says_the_other_is_behind_until_it_follows(run):
    """A heal is a room rotation. Reported as clean, it is how two agents end up
    in different rooms each thinking the other went quiet — so the healing
    side is told the other is behind, the other side is told it moved, and
    after it follows the two are in one room again."""
    pair_up(run)
    code, out, err = run("laptop", "pair", "heal", "lab", "--reason", "invite leaked")
    assert code == 0
    assert out == "pair:lab → work room 1\n"
    assert "still in work room 0" in err

    code, _, err = run("cloud", "--room", "pair:lab", "say", "pair", "on the new one")
    assert code == 0
    assert "moved from work room 0 to 1" in err and "invite leaked" in err

    code, out, _ = run("laptop", "--room", "pair:lab", "history", "pair")
    assert code == 0 and "on the new one" in out
    _, out, _ = run("laptop", "--json", "pair", "status", "lab")
    [row] = json.loads(out)["pairs"]
    assert (row["generation"], row["peer_generation"]) == (1, 1)
    assert not any("not moved" in w for w in row["warnings"])


def test_forget_makes_the_other_sides_next_command_fail(run):
    """A peer left working in a pair the other side ended is talking into a
    room nobody will read again. Its next command refuses, with the way out."""
    pair_up(run)
    code, out, _ = run("laptop", "pair", "forget", "lab")
    assert code == 0 and out == "forgot pair:lab; the other side was told\n"

    code, _, err = run("cloud", "--room", "pair:lab", "say", "pair", "anyone?")
    assert code == 1
    assert "the other side ended pair 'lab'" in err and "pair forget lab" in err
    code, out, _ = run("cloud", "--json", "pair", "status", "lab")
    assert code == 1
    assert json.loads(out)["pairs"][0]["state"] == "ended_by_peer"


# --- export ----------------------------------------------------------------------------


def test_export_for_the_peer_loads_from_the_environment_in_a_fresh_container(
        run, tmp_path, monkeypatch):
    """A cloud container does not keep its home directory. The machine that
    stays exports the other side's half into the container's secrets, and a
    fresh container with an empty store is that side of the pair."""
    pair_up(run)
    code, out, err = run("laptop", "pair", "export", "lab", "--peer")
    assert code == 0
    variable, _, value = out.strip().partition("=")
    assert variable == "SWITCHBOARD_PAIR_LAB" and pairing.looks_like_secret(value)
    assert "long-lived secret" in err and "never in a conversation" in err

    monkeypatch.setenv(variable, value)
    assert not (tmp_path / "container").exists()
    code, _, _ = run("container", "--room", "pair:lab", "say", "pair", "fresh container")
    assert code == 0
    _, out, _ = run("container", "--json", "pair", "status", "lab")
    [row] = json.loads(out)["pairs"]
    assert (row["role"], row["state"]) == ("responder", "paired")
    monkeypatch.delenv(variable)

    code, out, _ = run("laptop", "--room", "pair:lab", "inbox", "-c", "pair")
    assert code == 0 and "fresh container" in out


def test_export_without_peer_points_at_the_safer_one(run):
    pair_up(run)
    code, out, err = run("laptop", "pair", "export", "lab")
    assert code == 0 and out.startswith("SWITCHBOARD_PAIR_LAB=")
    assert "--peer" in err


# --- whoami and invite ----------------------------------------------------------------


def test_whoami_reports_the_work_room_and_will_not_print_its_key(run, tmp_path):
    """`whoami` under a pair handle must describe the room every other command
    in the invocation uses — and a work room's key, derived and never meant
    to travel, leaves only as an invite, with the caveat attached."""
    pair_up(run)
    record = store_of(tmp_path, "laptop").get("lab")
    code, out, _ = run("laptop", "--json", "--room", "pair:lab", "whoami")
    assert code == 0
    payload = json.loads(out)
    assert payload["workspace"] == pairing.work_room(record.pair_secret, 0).workspace
    assert payload["encrypted"] is True
    assert payload["pair"]["generation"] == 0 and payload["pair"]["state"] == "paired"

    for flag in ("--show-key", "--env"):
        code, out, err = run("laptop", "--room", "pair:lab", "whoami", flag)
        assert code == 1 and out == ""
        assert "--room pair:lab invite" in err and "pair heal lab" in err


def test_an_invite_into_a_work_room_says_a_heal_is_how_to_take_it_back(run, tmp_path):
    """The usual note says a key opens the lobby and every room sealed with it,
    which is false here and misleading in the direction that matters: this
    key opens one generation, and the remedy is a heal."""
    pair_up(run)
    code, out, err = run("laptop", "--json", "--room", "pair:lab", "invite")
    assert code == 0
    payload = json.loads(out)
    assert payload["pair"]["generation"] == 0 and "carries_key" not in payload
    assert "does not follow a heal" in err and "pair heal lab" in err
    record = store_of(tmp_path, "laptop").get("lab")
    assert Invite.decode(payload["invite"]).workspace == \
        pairing.work_room(record.pair_secret, 0).workspace


# --- wait ------------------------------------------------------------------------------


def test_wait_returns_at_once_when_the_answer_is_already_there(run):
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    run("cloud", "pair", "accept", out.strip())
    code, out, _ = run("laptop", "pair", "wait", "lab", "--until", "10")
    assert code == 0 and out.startswith("pair:lab paired — SAS ")


def test_wait_exits_2_at_its_deadline_and_1_once_the_code_is_dead(run, h):
    """2 means "nothing yet", as it does for `listen`; 1 means it can never
    pair. A script waiting on a code that expired must not wait forever."""
    run("laptop", "pair", "start", "--name", "lab", "--ttl", "60")
    code, out, _ = run("laptop", "pair", "wait", "lab", "--until", "0.5")
    assert code == 2 and "still pending" in out

    h.advance(60 + pairing.COLLECT_WINDOW + 60)
    code, out, err = run("laptop", "--json", "pair", "wait", "lab", "--until", "5")
    assert code == 1
    assert json.loads(out)["state"] == "expired"


def test_the_responder_waits_for_confirmation_and_wakes_on_it(
        run, h, tmp_path, monkeypatch, capsys):
    """The responder's wait parks in work room 0, where the initiator's
    collection says hello — so it ends the moment the other side collects,
    holding the SAS, rather than at the next poll."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    blob = out.strip()
    run("cloud", "pair", "accept", blob)
    _be("cloud", tmp_path, monkeypatch)
    thread, result = _in_thread(["--url", BASE_URL, "--json", "pair", "wait", "lab",
                                 "--until", "15"])
    time.sleep(0.5)
    collected = pairing.collect("lab", h.client_config(), store=store_of(tmp_path, "laptop"),
                                connect=h.client_class(), agent_id="laptop", now=h.now)
    assert collected.state == "paired"
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "paired" and report["sas"] == collected.record.sas


# --- json --------------------------------------------------------------------------------


def test_every_pair_command_prints_one_json_document(run):
    """`--json` stdout is parsed by something. Prose ahead of the document —
    the way `join --json` prints its env block first — breaks it."""
    def doc(machine, *argv):
        code, out, _ = run(machine, "--json", *argv)
        assert code == 0, (argv, out)
        return json.loads(out)

    started = doc("laptop", "pair", "start", "--name", "lab")
    assert started["state"] == "pending" and started["room"] == "pair:lab"
    assert pairing.looks_like_code(started["code"]) and started["verifiable"] is True
    assert "redeemable until" in started["describes"]
    accepted = doc("cloud", "pair", "accept", started["code"])
    assert (accepted["room"], accepted["state"], accepted["generation"]) == \
        ("pair:lab", "unconfirmed", 0)
    assert doc("laptop", "pair", "wait", "lab", "--until", "5")["state"] == "paired"
    assert doc("laptop", "pair", "status")["pairs"][0]["state"] == "paired"
    healed = doc("laptop", "pair", "heal", "lab")
    assert (healed["from"], healed["to"]) == (0, 1)
    exported = doc("laptop", "pair", "export", "lab", "--peer")
    assert (exported["variable"], exported["role"]) == ("SWITCHBOARD_PAIR_LAB", "responder")
    forgotten = doc("laptop", "pair", "forget", "lab")
    assert forgotten["forgotten"] is True and forgotten["peer_told"] is True


def test_no_result_carries_the_pair_secret(run, tmp_path):
    """`export` is the one way out. Everything else reports the pair without
    it — the secret in a status line would be in every transcript."""
    pair_up(run)
    secret = store_of(tmp_path, "laptop").get("lab").secret
    for argv in (["pair", "status"], ["pair", "wait", "lab"], ["pair", "heal", "lab"],
                 ["--room", "pair:lab", "whoami"]):
        _, out, err = run("laptop", "--json", *argv)
        assert secret not in out and secret not in err


# --- listen --------------------------------------------------------------------------------


def _posts(argv: list[str]):
    args = build_parser().parse_args(argv)
    identity = detect_identity(agent_id=args.agent_id)
    return cli._listen_posts(args, identity)


def test_listen_parks_in_pending_handshakes_and_paired_work_rooms_by_default(
        run, tmp_path, monkeypatch):
    """A turn-based initiator printed a code and ended its turn; the listener
    is the process still running. Parking where the answer will land is what
    collects it within seconds — and `--no-pairs` is the way out of it."""
    pair_up(run, "lab")
    run("laptop", "pair", "start", "--name", "next")
    _be("laptop", tmp_path, monkeypatch)

    posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby"])
    by_role = {(p.role, p.handshake): p for p in posts}
    assert ("pair:lab", False) in by_role and ("pair:next", True) in by_role
    assert by_role[("pair:lab", False)].channels == ["pair"]
    assert by_role[("pair:next", True)].channels == ["pair"]

    posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby", "--no-pairs"])
    assert [p.role for p in posts] == ["room"]

    posts = _posts(["--url", BASE_URL, "listen", "--only", "pair:lab"])
    assert [(p.role, p.pair, p.generation) for p in posts] == [("pair:lab", "lab", 0)]


def test_listen_under_a_pair_handle_takes_the_lobby_from_its_own_key(
        run, tmp_path, monkeypatch):
    """A lobby derived from the work room's key is a room nobody else will ever
    be in. Under `--room pair:<name>` the lobby is this environment's."""
    pair_up(run)
    own_key = generate_key()
    _be("laptop", tmp_path, monkeypatch)
    monkeypatch.setenv("SWITCHBOARD_KEY", own_key)
    posts = _posts(["--url", BASE_URL, "--room", "pair:lab", "listen", "--no-pairs"])
    assert posts[0].role == "pair:lab" and posts[0].channels == ["pair"]
    [lobby] = [p for p in posts if p.role == "lobby"]
    work_key = store_of(tmp_path, "laptop").get("lab")
    assert lobby.workspace == rooms.lobby(own_key).workspace
    assert lobby.workspace != rooms.lobby(
        pairing.work_room(work_key.pair_secret, 0).key).workspace


def test_the_initiators_own_hello_is_not_a_wake(run, tmp_path, monkeypatch, capsys):
    """The hello was in the handshake room before the listener was. Waking on
    it would return the session at once, every time it parked, for nothing —
    which is how a listener teaches its session to stop arming it."""
    run("laptop", "pair", "start", "--name", "lab")
    _be("laptop", tmp_path, monkeypatch)
    capsys.readouterr()
    code = main(["--url", BASE_URL, "listen", "--only", "pair:lab", "--until", "+1"])
    assert code == cli.EXIT_DEADLINE
    assert capsys.readouterr().out == ""


def test_a_parked_listener_collects_the_answer_the_moment_it_lands(
        run, h, tmp_path, monkeypatch, capsys):
    """The wake carries the outcome, collected in-process: the session comes
    back paired and holding the SAS, instead of having to ask. (`--only` keeps
    this to the one room; a pairing still waiting on its answer is parked in
    its handshake room, whether named or by default.)"""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    blob = out.strip()
    _be("laptop", tmp_path, monkeypatch)
    thread, result = _in_thread(["--url", BASE_URL, "listen", "--only", "pair:lab",
                                 "--until", "+8"])
    out, err = _wait_for(capsys, "listen: parked")
    assert "[pair:lab handshake]" in err

    responder = store_of(tmp_path, "cloud")
    pairing.accept(blob, h.client_config(), store=responder, connect=h.client_class(),
                   agent_id="cloud", now=h.now)
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + capsys.readouterr().out)
    assert wake["role"] == "pair:lab"
    assert (wake["pair"]["state"], wake["pair"]["handshake"]) == ("paired", True)

    assert store_of(tmp_path, "laptop").pending("lab") is None
    resolved = pairing.resolve("lab", h.client_config(), store=responder,
                               connect=h.client_class(), agent_id="cloud", now=h.now)
    assert resolved.state == "paired" and resolved.record.sas == wake["pair"]["sas"]


def test_a_listener_in_a_work_room_wakes_on_the_pair_channel(
        run, h, tmp_path, monkeypatch, capsys):
    """Neither side knows the other's agent id, so the pair channel is where
    the other side speaks — and the wake says how to take delivery, because
    a listener only peeks."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    _be("laptop", tmp_path, monkeypatch)
    thread, result = _in_thread(["--url", BASE_URL, "listen", "--only", "pair:lab",
                                 "--until", "+8"])
    out, _ = _wait_for(capsys, "listen: parked")

    resolved = pairing.resolve("lab", h.client_config(), store=store_of(tmp_path, "cloud"),
                               connect=h.client_class(), agent_id="cloud", now=h.now)
    with h.client_class()(resolved.config, agent_id="cloud") as peer:
        peer.post(pairing.CHANNEL, "ready when you are")
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + capsys.readouterr().out)
    assert wake["messages"][0]["body"] == "ready when you are"
    assert wake["pair"]["next"] == "switchboard --room pair:lab inbox -c pair"


def test_a_listener_in_a_retired_work_room_wakes_on_the_heal(
        run, h, tmp_path, monkeypatch, capsys):
    """A heal by the other side leaves a moved note in the old room. A listener
    parked there must wake and say the room is retired — otherwise it waits,
    in a room whose key got out, for a peer who has already left."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    _be("laptop", tmp_path, monkeypatch)
    thread, result = _in_thread(["--url", BASE_URL, "listen", "--only", "pair:lab",
                                 "--until", "+8"])
    out, _ = _wait_for(capsys, "listen: parked")

    healed = pairing.heal("lab", h.client_config(), store=store_of(tmp_path, "cloud"),
                          connect=h.client_class(), agent_id="cloud", now=h.now)
    assert healed.to_generation == 1
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + capsys.readouterr().out)
    assert wake["messages"][0]["type"] == "pair.moved"
    assert (wake["pair"]["generation"], wake["pair"]["moved"]) == (0, True)
    assert "--room pair:lab" in wake["pair"]["next"]
