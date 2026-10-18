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

import httpx
import pytest

import switchboard.cli as cli
from switchboard import knownrooms, pairing, rooms
from switchboard.cli import build_parser, main
from switchboard.client import SwitchboardError, detect_identity
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
    # `GITHUB_ACTIONS` outranks it when identity guesses the kind, so on a CI
    # runner the session would be `ci` and this would test nothing.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
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


def test_pair_help_says_a_code_is_live_until_collected_not_until_redeemed(capsys):
    """`pair --help` said a code found in a paper trail was worth nothing once
    redeemed. Between `pair accept` and the initiator collecting, one junk
    answer from anyone holding it voids both sides — so a reader told
    otherwise leaves it in a chat through the very window it can do harm."""
    with pytest.raises(SystemExit):
        main(["pair", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "after the code is redeemed" not in text
    assert ("live credential until the machine that made it has collected the "
            "answer, or until it expires unanswered") in text


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
    # Said once: `pairing.heal` words what a heal strands, and a surface
    # adding its own copy is how two sentences end up disagreeing.
    assert err.count("still in work room 0") == 1

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
    work = by_role[("pair:lab", False)]
    # The pair channel and this agent's own DMs: the listener puts its id on
    # the work room's roster, so the other side can address it there.
    assert work.channels == ["pair", f"@{work.agent_id}"]
    # A handshake room is read for the answer alone, under a throwaway id.
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
    assert posts[0].role == "pair:lab"
    assert posts[0].channels == ["pair", f"@{posts[0].agent_id}"]
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
    # On the wire the note is an ordinary one; only its sealed tag says what
    # it is, and that is what the listener must have recognised.
    assert wake["messages"][0]["type"] == "note"
    assert pairing.message_tag(wake["messages"][0]) == pairing.TAG_MOVED
    assert (wake["pair"]["generation"], wake["pair"]["moved"]) == (0, True)
    assert "--room pair:lab" in wake["pair"]["next"]


# --- the work room's listener: who can wake it, and what retires it -----------------


def _listening(capsys, tmp_path, monkeypatch, machine: str,
               *argv: str) -> tuple[threading.Thread, dict, str]:
    """Arm `listen` as `machine` on a thread; return once it says it parked."""
    _be(machine, tmp_path, monkeypatch)
    thread, result = _in_thread(["--url", BASE_URL, *argv])
    out, _ = _wait_for(capsys, "listen: parked")
    return thread, result, out


def _work_room(h, tmp_path, machine: str = "cloud"):
    """`machine`'s client in the pair's current work room, as its next command sees it."""
    resolved = pairing.resolve("lab", h.client_config(), store=store_of(tmp_path, machine),
                               connect=h.client_class(), agent_id=machine, now=h.now)
    return h.client_class()(resolved.config, agent_id=machine)


def _until(check, *, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("never happened")


def _other_on_roster(peer) -> str:
    """The id the other side's listener put on the work room's roster."""
    return _until(lambda: next((a["agent_id"] for a in peer.agents()
                                if a["agent_id"] != peer.agent_id), None))


def _parked_state(peer, other: str) -> dict:
    """`_listener_state` for a DM to `other`, once its heartbeat is up."""
    def look():
        state = cli._listener_state(peer, peer=other)
        return state if state.get("peer_parked") else None
    return _until(look)


def test_a_listener_in_a_work_room_wakes_on_a_direct_message(
        run, h, tmp_path, monkeypatch, capsys):
    """The listener puts this agent on the work room's roster every pass, so
    the other side can read the id there and `dm` it — and is then told a
    listener is parked. Watching the pair channel alone made that a lie: the
    hub drops an agent's own channel whenever channels are named, so the DM
    sat unread while the listener ran to its deadline and exited 2. And the
    wake must name a view that shows the DM: `inbox -c pair` does not."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "laptop",
                                     "listen", "--only", "pair:lab", "--until", "+8")
    with _work_room(h, tmp_path) as peer:
        laptop = _other_on_roster(peer)
        assert "peer_watching" not in _parked_state(peer, laptop)
        peer.send(laptop, "ping")
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + capsys.readouterr().out)
    assert wake["messages"][0]["body"] == "ping"
    assert wake["pair"]["next"] == "switchboard --room pair:lab inbox"


def test_dm_advice_says_when_a_parked_listener_is_not_reading_dms(
        run, h, tmp_path, monkeypatch, capsys):
    """`listen -c general` names its channels, so the hub never hands it a DM.
    A sender used to be told "their listener is parked, so an answer can
    arrive within seconds" all the same; the heartbeat says what it watches,
    and the advice now reads it. The pair channel, polled under `-c` only for
    the pair's own notes, is not advertised as a way to reach the agent."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    thread, result, _ = _listening(
        capsys, tmp_path, monkeypatch, "laptop", "--room", "pair:lab", "listen",
        "-c", "general", "--no-lobby", "--no-pairs", "--until", "+3")
    with _work_room(h, tmp_path) as peer:
        state = _parked_state(peer, _other_on_roster(peer))
    _join(thread)
    assert result.get("code") == cli.EXIT_DEADLINE, result
    assert state["peer_watching"] == ["general"]
    assert "watches only general" in state["next"] and "next turn" in state["next"]
    assert "within seconds" not in state["next"]


def test_a_heal_run_here_wakes_this_machines_own_listener(
        run, h, tmp_path, monkeypatch, capsys):
    """The session's `pair heal` and its background listener share one agent
    id, and a listener never wakes on its own agent's messages — so a heal run
    here left this machine's listener in the retired room while the peer
    followed to the new one, though the heal said a parked listener wakes."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "laptop",
                                     "listen", "--only", "pair:lab", "--until", "+8")
    healed = pairing.heal("lab", h.client_config(), store=store_of(tmp_path, "laptop"),
                          connect=h.client_class(), agent_id="laptop", now=h.now)
    assert healed.to_generation == 1
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + capsys.readouterr().out)
    assert wake["pair"]["moved"] is True and wake["pair"]["generation"] == 0


def test_a_listener_follows_a_heal_here_whose_note_never_landed(
        run, h, tmp_path, monkeypatch, capsys):
    """A heal saves its new generation here before it posts the moved note,
    and the note is best effort: a hub that flickered at that moment used to
    leave this machine's listener parked in the retired room for its whole
    window. The local store says the pair moved whether or not the note
    arrived, so the listener reads it each pass and wakes on it."""
    monkeypatch.setattr(cli, "_PARK_POLL", 0.3)
    pair_up(run)
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "laptop",
                                     "listen", "--only", "pair:lab", "--until", "+8")
    store = store_of(tmp_path, "laptop")
    record = store.get("lab")
    record.generation = 1   # what `pairing.heal` saves, without the note it posts
    store.save(record)
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    captured = capsys.readouterr()
    wake = json.loads(out + captured.out)
    assert wake["messages"] == []
    assert (wake["pair"]["moved"], wake["pair"]["to_generation"]) == (True, 1)
    assert "pair:lab: the pair was healed" in captured.err


def test_a_do_not_disturb_listener_still_wakes_on_a_heal(
        run, h, tmp_path, monkeypatch, capsys):
    """Do-not-disturb chooses which conversation wakes the agent; a heal is not
    conversation. Filtered out with everything else, the moved note left a
    `--type urgent` listener parked in the retired room while the peer's
    urgent message landed in the new one, which it could never see."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    thread, result, out = _listening(
        capsys, tmp_path, monkeypatch, "laptop", "--room", "pair:lab", "listen",
        "--type", "urgent", "--no-lobby", "--no-pairs", "--until", "+8")
    pairing.heal("lab", h.client_config(), store=store_of(tmp_path, "cloud"),
                 connect=h.client_class(), agent_id="cloud", now=h.now)
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + capsys.readouterr().out)
    assert pairing.message_tag(wake["messages"][0]) == pairing.TAG_MOVED
    assert (wake["pair"]["moved"], wake["pair"]["to_generation"]) == (True, 1)
    assert any("healed" in line for line in wake["pair"]["warnings"])


def test_a_channel_listener_in_a_work_room_wakes_on_a_heal_and_nothing_else_there(
        run, h, tmp_path, monkeypatch, capsys):
    """`-c general` replaced the pair channel, so the moved note — posted on
    it — was never even polled. The pair channel is polled again under `-c`,
    for the pair's notes alone: ordinary chatter there is still not this
    listener's to wake on, because it asked for `general`."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    thread, result, out = _listening(
        capsys, tmp_path, monkeypatch, "laptop", "--room", "pair:lab", "listen",
        "-c", "general", "--no-lobby", "--no-pairs", "--until", "+8")
    with _work_room(h, tmp_path) as peer:
        peer.post(pairing.CHANNEL, "chatter on the pair channel")
    time.sleep(1.0)
    assert thread.is_alive(), "an ordinary pair-channel message woke a -c general listener"
    pairing.heal("lab", h.client_config(), store=store_of(tmp_path, "cloud"),
                 connect=h.client_class(), agent_id="cloud", now=h.now)
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + capsys.readouterr().out)
    assert [pairing.message_tag(m) for m in wake["messages"]] == [pairing.TAG_MOVED]
    assert wake["pair"]["moved"] is True


def test_a_forged_moved_note_does_not_break_do_not_disturb(
        run, h, tmp_path, monkeypatch, capsys):
    """A heal happens because the work room's key got out, and whoever holds
    it can post a note shaped like a moved one. A note that gets past the
    filter only because of what it claims is checked against the pair room
    first: one that is not true is not a wake."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    thread, result, out = _listening(
        capsys, tmp_path, monkeypatch, "laptop", "--room", "pair:lab", "listen",
        "--type", "urgent", "--no-lobby", "--no-pairs", "--until", "+3")
    with _work_room(h, tmp_path) as holder_of_the_key:
        holder_of_the_key.post(pairing.CHANNEL, {"t": pairing.TAG_MOVED, "note": "move!"})
    _join(thread)
    assert result.get("code") == cli.EXIT_DEADLINE, result
    assert (out + capsys.readouterr().out) == ""


def test_a_forged_ended_note_does_not_tell_a_plain_listener_to_forget_the_pair(
        run, h, tmp_path, monkeypatch, capsys):
    """Only a filtered listener used to check a pairing note. A plain one took
    the tag at its word: anyone holding the leaked work-room key posted an
    ended note, and the session was woken with "the other side ended the
    pair: `switchboard pair forget lab`" — on stderr even under `-q` — and a
    session that obeyed destroyed a healthy pair on both sides. Checked
    against the pair room, a note that is not true is not a wake at all."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    thread, result, out = _listening(
        capsys, tmp_path, monkeypatch, "laptop", "--room", "pair:lab", "listen",
        "--no-lobby", "--no-pairs", "--until", "+3")
    with _work_room(h, tmp_path) as holder_of_the_key:
        holder_of_the_key.post(pairing.CHANNEL, {"t": pairing.TAG_ENDED, "note": "over"})
        holder_of_the_key.post(pairing.CHANNEL, {"t": pairing.TAG_MOVED, "note": "move!"})
    _join(thread)
    captured = capsys.readouterr()
    assert result.get("code") == cli.EXIT_DEADLINE, result
    assert (out + captured.out) == ""
    assert "pair forget" not in captured.err and "retired" not in captured.err
    code, out, _ = run("laptop", "--json", "pair", "status", "lab")
    assert code == 0 and json.loads(out)["pairs"][0]["state"] == "paired"


def test_a_forget_run_here_is_not_reported_as_the_other_sides_doing(
        run, h, tmp_path, monkeypatch, capsys):
    """`pair forget` deletes the record here, then posts an ended note that
    wakes this machine's own listener. The wake said "the other side ended
    the pair: `switchboard pair forget lab`" — false, and followed, it fails
    with "no pair called 'lab'". Checked, it says the pair is gone from this
    machine, and that nothing more arrives in that room."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "laptop",
                                     "listen", "--only", "pair:lab", "--until", "+8")
    pairing.forget("lab", h.client_config(), store=store_of(tmp_path, "laptop"),
                   connect=h.client_class(), agent_id="laptop", now=h.now)
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    captured = capsys.readouterr()
    wake = json.loads(out + captured.out)
    assert (wake["pair"]["ended"], wake["pair"]["state"]) == (True, "gone")
    assert "no longer on this machine" in wake["pair"]["next"]
    assert "the other side ended" not in wake["pair"]["next"] + captured.err


def test_a_note_that_cannot_be_checked_wakes_as_a_message_not_as_a_heal(
        run, h, tmp_path, monkeypatch, capsys):
    """With the pair room unreadable a moved note still wakes — a true heal
    slept through strands the agent until its deadline — but on a forger's
    say-so the wake used to announce "the pair was healed; this one is
    retired". It is a message, with a warning that it could not be checked
    and how to find out — in the wake, and on stderr."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    down = threading.Event()
    bound = cli.Client

    class PairRoomDown(bound):
        def board_list(self, **kwargs):
            if down.is_set() and kwargs.get("prefix") == pairing.SIDE_PREFIX:
                raise httpx.ConnectError("the pair room is unreachable")
            return super().board_list(**kwargs)

    monkeypatch.setattr(cli, "Client", PairRoomDown)
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "laptop",
                                     "listen", "--only", "pair:lab", "--until", "+8")
    down.set()
    with _work_room(h, tmp_path) as holder_of_the_key:
        holder_of_the_key.post(pairing.CHANNEL, {"t": pairing.TAG_MOVED, "note": "move!"})
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    captured = capsys.readouterr()
    wake = json.loads(out + captured.out)
    assert "moved" not in wake["pair"] and "ended" not in wake["pair"]
    assert wake["pair"]["next"] == "switchboard --room pair:lab inbox -c pair"
    assert any("could not be checked" in w for w in wake["pair"]["warnings"])
    assert "could not be checked" in captured.err and "retired" not in captured.err


# --- pairings begun, or settled, while a listener is parked --------------------------


def test_a_listener_armed_before_pair_start_still_collects_the_answer(
        run, h, tmp_path, monkeypatch, capsys):
    """Sessions keep a listener armed, so `pair start` usually runs with one
    already parked — and the listener chose its rooms once, at arming. The
    handshake room went unwatched while `pair start` said a parked listener
    collects the answer; nothing did, and the responder sat unconfirmed."""
    monkeypatch.setattr(cli, "_RESCAN_EVERY", 0.2)
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "laptop", "-w", WS,
                                     "listen", "--no-lobby", "--until", "+10")
    started = pairing.start(h.client_config(), store=store_of(tmp_path, "laptop"),
                            connect=h.client_class(), name="lab", now=h.now)
    more, _ = _wait_for(capsys, "[pair:lab handshake], begun after this listener was armed")
    pairing.accept(started.code, h.client_config(), store=store_of(tmp_path, "cloud"),
                   connect=h.client_class(), agent_id="cloud", now=h.now)
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + more + capsys.readouterr().out)
    assert wake["role"] == "pair:lab"
    assert (wake["pair"]["state"], wake["pair"]["handshake"]) == ("paired", True)


def test_a_handshake_settled_while_another_room_wins_is_still_reported(
        run, h, tmp_path, monkeypatch, capsys):
    """A handshake is collected inside its own room's thread, and collecting
    deletes the pending handshake. When another room's message won the wake,
    the outcome — "aborted, the code leaked" as much as "paired" — went with
    the process, and `pair status` afterwards said there were no pairs."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    blob = out.strip()
    collecting = threading.Event()
    real = cli._settle_handshake

    def slow(*args, **kwargs):
        collecting.set()
        time.sleep(1.0)   # a collect is several hub round trips
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "_settle_handshake", slow)
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "laptop", "-w", WS,
                                     "listen", "--no-lobby", "--until", "+10")
    pairing.accept(blob, h.client_config(), store=store_of(tmp_path, "cloud"),
                   connect=h.client_class(), agent_id="cloud", now=h.now)
    assert collecting.wait(5)
    h.client("teammate").send("laptop", "a question, meanwhile")
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    captured = capsys.readouterr()
    wake = json.loads(out + captured.out)
    assert wake["role"] == "room" and wake["messages"][0]["body"] == "a question, meanwhile"
    [settled] = wake["pairings"]
    assert (settled["name"], settled["state"]) == ("lab", "paired") and settled["sas"]
    assert "pair:lab paired — SAS" in captured.err


def test_a_quiet_listener_still_says_a_pair_was_voided(run, h, tmp_path):
    """`-q` drops success chatter. "Voided: the code leaked" is not chatter —
    and the resolution that says so has already deleted the record, so a line
    dropped here is a verdict nothing on this machine can report again."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    assert run("cloud", "pair", "accept", out.strip())[0] == 0
    [(_, handshake)] = pairing.pending_rooms(store_of(tmp_path, "laptop"),
                                             h.client_config(), now=h.now)
    with h.client_class()(handshake, agent_id="stranger") as stranger:
        stranger.post(pairing.CHANNEL, {"t": pairing.TAG_ABORTED, "why": "a second holder"})
    code, out, err = run("cloud", "-q", "-w", WS, "listen", "--no-lobby", "--until", "+1")
    assert code == cli.EXIT_DEADLINE and out == ""
    assert "not parking in pair:lab" in err and "voided" in err and "leaked" in err


# --- which pairs a plain listener parks in ---------------------------------------------


def test_a_fresh_container_parks_in_a_pair_exported_days_ago(run, h, tmp_path, monkeypatch):
    """The documented cloud setup: the export sits in the environment's secrets
    for good, and every new container's first act is `listen`. The export
    used to carry the exporting machine's last use, so once it was a day old
    every container skipped the pair in silence."""
    pair_up(run)
    _, out, _ = run("laptop", "pair", "export", "lab", "--peer")
    variable, _, value = out.strip().partition("=")
    h.advance(3 * 86400)
    monkeypatch.setenv(variable, value)
    _be("container", tmp_path, monkeypatch)
    posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby"])
    assert [(p.role, p.pair) for p in posts if p.pair] == [("pair:lab", "lab")]


def test_listen_names_each_pair_it_leaves_out(run, h, tmp_path, monkeypatch, capsys):
    """A pair idle here for a day is not parked in, and its work room looks
    exactly like a quiet one — while the other side, whose use this machine
    cannot see, may be talking there. So the skip is a line, with the flag
    that adds the pair back."""
    pair_up(run)
    h.advance(2 * 86400)
    _be("laptop", tmp_path, monkeypatch)
    capsys.readouterr()
    posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby"])
    assert not any(p.pair for p in posts)
    err = capsys.readouterr().err
    assert "not parking in pair:lab: not used on this machine since" in err
    assert "add --in pair:lab" in err

    posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby", "--in", "pair:lab"])
    assert [p.pair for p in posts if p.pair] == ["lab"]
    assert "not parking in pair:lab" not in capsys.readouterr().err


def test_listen_only_with_an_empty_pair_name_refuses_in_a_sentence(run, capsys):
    """`pair:` names the empty pair, which is falsy, and the check for "any pair
    named" used truthiness — so the pair base was never built and the command
    died on an assert, with a traceback, instead of the refusal."""
    with pytest.raises(SystemExit, match="none of the rooms named could be opened"):
        run("laptop", "listen", "--only", "pair:", "--until", "+1")
    assert "not a usable pair name" in capsys.readouterr().err


# --- what a code holder can write into the handshake room ----------------------------


def test_a_malformed_answer_aborts_the_pairing_without_hiding_the_others(run, h, tmp_path):
    """Anyone holding the code can write an answer, and one that is not a key
    used to crash `pair status` with a traceback — taking every other pair's
    row with it. It is what it looks like: tampering, so the pairing aborts."""
    pair_up(run, "other")
    run("laptop", "pair", "start", "--name", "lab")
    [(_, handshake)] = pairing.pending_rooms(store_of(tmp_path, "laptop"),
                                             h.client_config(), now=h.now)
    with h.client_class()(handshake, agent_id="stranger") as stranger:
        stranger.post(pairing.CHANNEL, {"t": pairing.TAG_ANSWER, "e": "A", "c": "AAAA"})
    code, out, err = run("laptop", "--json", "pair", "status")
    assert code == 0 and "Traceback" not in err
    rows = {row["name"]: row for row in json.loads(out)["pairs"]}
    assert rows["lab"]["state"] == "aborted" and rows["other"]["state"] == "paired"
    assert store_of(tmp_path, "laptop").pending("lab") is None


# --- pair wait through a hub that is not answering -----------------------------------------


def test_wait_rides_out_hub_errors_and_exits_2_still_pending(run, monkeypatch):
    """Exit 1 is "it can never pair", and a script acts on it by throwing the
    pairing away. One timeout used to produce it, with no document under
    `--json`; a hub error says nothing about the pairing, so the wait goes on
    to its deadline and reports pending — and that it never reached the hub."""
    run("laptop", "pair", "start", "--name", "lab")

    def down(*args, **kwargs):
        raise httpx.ReadTimeout("the hub is restarting")

    monkeypatch.setattr(pairing, "collect", down)
    code, out, _ = run("laptop", "--json", "pair", "wait", "lab", "--until", "1")
    assert code == cli.EXIT_DEADLINE
    report = json.loads(out)
    assert (report["state"], report["checked"]) == ("pending", False)
    assert any("could not check for an answer" in w for w in report["warnings"])
    assert any("no check reached the hub" in w for w in report["warnings"])


def test_wait_survives_one_hub_error_and_still_pairs(run, monkeypatch):
    """The other half: a hub that answers again is waited for, not given up on."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    run("cloud", "pair", "accept", out.strip())
    real, calls = pairing.collect, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("one slow moment")
        return real(*args, **kwargs)

    monkeypatch.setattr(pairing, "collect", flaky)
    code, out, _ = run("laptop", "--json", "pair", "wait", "lab", "--until", "10")
    assert code == 0 and json.loads(out)["state"] == "paired"


def test_wait_does_not_wait_out_a_refused_token(run, monkeypatch):
    """A 401 does not get better: waiting through it would turn the one thing
    to fix into minutes of "still pending"."""
    run("laptop", "pair", "start", "--name", "lab")

    def refused(*args, **kwargs):
        raise SwitchboardError("unauthorized", status=401)

    monkeypatch.setattr(pairing, "collect", refused)
    started = time.time()
    code, _, _ = run("laptop", "pair", "wait", "lab", "--until", "30")
    assert code == 1 and time.time() - started < 10


_REFUSED = "invalid or missing bearer token"


def test_the_responders_wait_does_not_wait_out_a_refused_token(run, monkeypatch):
    """The responder's twin of the test above. Its first look is a resolve,
    and `resolve` fails soft on the pair room — so the hub's 401 came back as
    "unchecked", the wait ran its whole length, exited 2 "still unconfirmed"
    and said to check the URL: a script retrying on 2 never learned the token
    was wrong. (Refused at the pair room only, so it is that look that has to
    notice.)"""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    assert run("cloud", "pair", "accept", out.strip())[0] == 0

    class Refused(cli.Client):
        def board_list(self, **kwargs):
            raise SwitchboardError(_REFUSED, status=401)

    monkeypatch.setattr(cli, "Client", Refused)
    started = time.time()
    code, out, err = run("cloud", "pair", "wait", "lab", "--until", "20")
    assert code == 1 and time.time() - started < 10
    assert "still unconfirmed" not in out
    assert _REFUSED in err and "SWITCHBOARD_TOKEN" in err


def test_a_token_refused_mid_wait_ends_the_wait(run, monkeypatch):
    """A token rotated while `pair wait` is parked: the pair room read fine,
    and the refusal comes on the long poll — where it was swallowed like a
    timeout, and the wait ran to its deadline as "still unconfirmed"."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    assert run("cloud", "pair", "accept", out.strip())[0] == 0

    class RotatedMidWait(cli.Client):
        def inbox(self, *args, **kwargs):
            raise SwitchboardError(_REFUSED, status=401)

    monkeypatch.setattr(cli, "Client", RotatedMidWait)
    started = time.time()
    code, out, err = run("cloud", "pair", "wait", "lab", "--until", "20")
    assert code == 1 and time.time() - started < 10
    assert _REFUSED in err and "SWITCHBOARD_TOKEN" in err


def test_a_dropped_connection_mid_wait_is_waited_through_not_blamed_on_the_store(
        run, monkeypatch):
    """Only a refusal ends the wait early. A socket that fails on the long
    poll is an OSError, and re-raised with the refusals it would land in the
    handler `pair` keeps for the store's OSErrors — "the pair store could not
    be used" for a hub that is restarting. It is waited through, as pending."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    assert run("cloud", "pair", "accept", out.strip())[0] == 0

    class Dropped(cli.Client):
        def inbox(self, *args, **kwargs):
            raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(cli, "Client", Dropped)
    code, out, err = run("cloud", "--json", "pair", "wait", "lab", "--until", "1")
    assert code == cli.EXIT_DEADLINE
    assert json.loads(out)["state"] == "unconfirmed"
    assert "pair store" not in err


def _accept_unsent(run, blob: str, monkeypatch) -> tuple[int, str, str]:
    """`pair accept` while the hub answers the first read and then goes: the
    answer's post fails, and so does the look that would say whether it
    landed. The answer is kept, unsent, to be retried on the next use."""
    real_post, real_read = pairing._post, pairing._read_handshake
    reads: list[int] = []

    def post(hub, body, **kwargs):
        if body.get("t") == pairing.TAG_ANSWER:
            raise httpx.ReadTimeout("the hub went away")
        return real_post(hub, body, **kwargs)

    def read(hub, expires_at):
        reads.append(1)
        if len(reads) > 1:
            raise httpx.ReadTimeout("the hub went away")
        return real_read(hub, expires_at)

    with monkeypatch.context() as patched:
        patched.setattr(pairing, "_post", post)
        patched.setattr(pairing, "_read_handshake", read)
        return run("cloud", "--json", "pair", "accept", blob)


def test_wait_on_a_leaked_code_exits_1_not_still_pending(run, tmp_path, monkeypatch):
    """`pair wait`'s 2 is "still pending at the deadline", which a script
    retries on — and a leaked code is refused with the conflict code, which is
    2 as well. So a responder whose answer never reached the hub, waiting
    after somebody else had answered the code, was told 2 and looped. It can
    never pair: 1, in the wait's own one-document shape — the same document
    the next wait gives from the verdict the refusal left behind."""
    _, out, _ = run("laptop", "pair", "start", "--name", "lab")
    blob = out.strip()
    code, out, _ = _accept_unsent(run, blob, monkeypatch)
    assert code == 1 and json.loads(out)["state"] == "unsent"
    assert store_of(tmp_path, "cloud").get("lab").sent is False
    assert run("thief", "pair", "accept", blob)[0] == 0
    assert run("laptop", "pair", "status", "lab")[0] == 0

    code, out, _ = run("cloud", "--json", "pair", "wait", "lab", "--until", "30")
    assert code == cli.EXIT_ERROR
    refused = json.loads(out)
    assert (refused["room"], refused["state"]) == ("pair:lab", "already_redeemed")
    assert "leaked" in refused["message"]

    code, out, _ = run("cloud", "--json", "pair", "wait", "lab", "--until", "30")
    assert code == cli.EXIT_ERROR and json.loads(out) == refused
    code, out, _ = run("cloud", "pair", "wait", "lab", "--until", "30")
    assert code == cli.EXIT_ERROR and out == "pair:lab already_redeemed\n"


# --- export, status and refusals carry what a script needs ----------------------------------


def test_export_json_carries_what_the_resolution_warned(run):
    """`export --json` dropped every resolution warning — not in the document,
    not on stderr — and those are the ones that matter for a value headed
    into a secret store: the other side is behind, the generation is stale."""
    pair_up(run)
    run("laptop", "pair", "heal", "lab")
    code, out, err = run("laptop", "--json", "pair", "export", "lab", "--peer")
    assert code == 0
    doc = json.loads(out)
    assert (doc["generation"], doc["checked"], doc["state"]) == (1, True, "paired")
    assert any("not moved to work room 1" in w for w in doc["warnings"])
    assert "not moved to work room 1" in err


def test_export_json_says_when_it_could_not_read_the_pair_room(run, monkeypatch):
    pair_up(run)
    bound = cli.Client

    class PairRoomDown(bound):
        def board_list(self, **kwargs):
            if kwargs.get("prefix") == pairing.SIDE_PREFIX:
                raise httpx.ConnectError("the pair room is unreachable")
            return super().board_list(**kwargs)

    monkeypatch.setattr(cli, "Client", PairRoomDown)
    code, out, err = run("laptop", "--json", "pair", "export", "lab", "--peer")
    assert code == 0
    doc = json.loads(out)
    assert doc["checked"] is False
    assert any("could not read the pair room" in w for w in doc["warnings"])
    assert "could not read the pair room" in err


def test_status_says_when_the_other_side_last_used_the_pair(run):
    """`paired` here says nothing about whether the other side has touched the
    pair in days, and its work room then looks exactly like a quiet one."""
    pair_up(run)
    run("cloud", "pair", "status", "lab")
    code, out, _ = run("laptop", "pair", "status", "lab")
    assert code == 0 and "peer: last active " in out
    _, out, _ = run("laptop", "--json", "pair", "status", "lab")
    [row] = json.loads(out)["pairs"]
    assert row["peer_present"] is True and row["peer_active_at"]


def test_a_refusal_under_json_is_one_document_carrying_its_state(run):
    """A script told only "error: <sentence>" on stderr can tell `expired` from
    `wrong_hub` from `ended_by_peer` only by parsing English. Under `--json`
    the state comes as a document — except where stdout is a listener's wake,
    which an error object must not be mistaken for."""
    code, out, err = run("laptop", "--json", "pair", "heal", "nosuch")
    assert code == 1 and err.startswith("error: ")
    doc = json.loads(out)
    assert doc["state"] == "unknown" and "no pair called 'nosuch'" in doc["error"]

    blob = pair_up(run)
    code, out, _ = run("stranger", "--json", "pair", "accept", blob)
    assert code == 2 and json.loads(out)["state"] == "already_redeemed"

    code, out, _ = run("laptop", "--json", "--room", "pair:nosuch", "whoami")
    assert code == 1 and json.loads(out)["state"] == "unknown"

    code, out, err = run("laptop", "--json", "--room", "pair:nosuch", "listen",
                         "--until", "+1")
    assert code == 1 and out == "" and "no pair called 'nosuch'" in err


def _unwritable(store_dir) -> None:
    """Make a pair store refuse every write, on the real filesystem.

    Every write takes the store's lock first, and a lock that is a directory
    cannot be opened — not by root either, which these tests may run as, and
    for whom a read-only mode refuses nothing.
    """
    lock = store_dir / ".lock"
    if lock.exists():
        lock.unlink()
    lock.mkdir()


def test_a_store_that_cannot_be_written_is_a_refusal_not_the_hub(run, tmp_path):
    """Every `--room pair:` command writes the store — `inbox` too — and its
    OSError reached `main()` as "cannot reach hub: [Errno 28] …", which sends
    somebody to debug a connection for a disk that is full; a `pair` command
    put it on stderr alone, so `--json` had an empty stdout. Either way it is
    a refusal now: the sentence says where to look, `--json` carries the
    `store` state, and nothing is sent."""
    pair_up(run)
    _unwritable(tmp_path / "cloud" / "pairs")
    code, out, err = run("cloud", "--room", "pair:lab", "say", "pair", "from-a-full-disk")
    assert code == 1 and out == ""
    assert "cannot reach hub" not in err
    assert err.startswith("error: the pair store could not be used")
    code, out, err = run("cloud", "--json", "--room", "pair:lab", "inbox")
    assert code == 1 and json.loads(out)["state"] == "store"
    assert "cannot reach hub" not in err
    code, out, _ = run("cloud", "--json", "pair", "heal", "lab")
    assert code == 1 and json.loads(out)["state"] == "store"
    code, out, _ = run("laptop", "--room", "pair:lab", "history", "pair")
    assert code == 0 and "from-a-full-disk" not in out

    _unwritable(tmp_path / "laptop" / "pairs")
    code, out, _ = run("laptop", "--json", "pair", "start", "--name", "next")
    doc = json.loads(out)
    assert code == 1 and doc["state"] == "store" and "SWITCHBOARD_PAIRS" in doc["error"]
