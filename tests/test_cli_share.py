"""`switchboard share`, `--room shared:<name>`, and the listener, driven as machines.

The model is pinned in `test_sharing.py`. What these pin is the surface, where
its failures are quieter: a `share new` that says "offered" for a pair whose
room it never reached; a room key in a `--json` document, a status line or an
invite's caveat; a book entry for a room whose key has nothing to point back
at; a handle that keeps naming a room this machine left; a token handed to a
hub another machine chose; and a listener parked everywhere except the rooms
the agent works in — or woken by its own writes and by anyone holding a work
room's key, each saying "look" about nothing.

A machine, to the CLI, is its environment (`test_cli_pair.py`): its own pair
and share stores, known-rooms book, local databases and agent id, switched
between `main()` calls over one in-process hub whose clock the tests move.
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

import switchboard.cli as cli
from switchboard import knownrooms, pairing, sharing
from switchboard.cli import build_parser, main
from switchboard.client import detect_identity
from switchboard.invite import Invite
from switchboard.pairing import PairStore
from switchboard.sharing import ShareStore
from switchboard.testing import BASE_URL, hub

WS = "share-cli-ws"
DAY = 86400


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


def shares_of(tmp_path, machine: str) -> ShareStore:
    return ShareStore(tmp_path / machine / "pairs", env={})


def doc_of(tmp_path, machine: str) -> dict:
    return sharing._normal(shares_of(tmp_path, machine).load())


def as_machine(h, tmp_path, machine: str) -> dict:
    """What a direct `sharing` call takes to act as `machine` — for a peer acting
    while this process is parked as another machine."""
    return {"store": shares_of(tmp_path, machine), "pair_store": store_of(tmp_path, machine),
            "connect": h.client_class(), "now": h.now}


def pair_up(run, name: str = "lab", other: str = "cloud") -> None:
    """The laptop starts, `other` accepts, the laptop collects, `other` confirms.

    And `other` reads the hello the laptop left in the work room as it
    collected: unread, it is a pair-channel message that wakes any listener
    `other` parks there, whatever these tests are about.
    """
    code, out, _ = run("laptop", "pair", "start", "--name", name)
    assert code == 0
    assert run(other, "pair", "accept", out.strip())[0] == 0
    assert run("laptop", "pair", "status", name)[0] == 0
    assert run(other, "pair", "status", name)[0] == 0
    assert run(other, "--room", f"pair:{name}", "inbox", "-c", pairing.CHANNEL)[0] == 0


def shared_core(run) -> None:
    """The laptop shares `core` with the cloud over pair `lab`, and the cloud keeps it."""
    pair_up(run)
    assert run("laptop", "share", "new", "core", "--with", "lab")[0] == 0
    assert run("cloud", "share", "accept", "core")[0] == 0


def _wait_for(capsys, needle: str, *, timeout: float = 10.0) -> tuple[str, str]:
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
    thread.join(timeout=20)
    for other in threading.enumerate():
        if other.name.startswith("listen:"):
            other.join(timeout=10)


def _listening(capsys, tmp_path, monkeypatch, machine: str,
               *argv: str) -> tuple[threading.Thread, dict, str]:
    """Arm `listen` as `machine` on a thread; return once it says it parked."""
    _be(machine, tmp_path, monkeypatch)
    capsys.readouterr()
    thread, result = _in_thread(["--url", BASE_URL, *argv])
    out, _ = _wait_for(capsys, "listen: parked")
    return thread, result, out


def _posts(argv: list[str]):
    args = build_parser().parse_args(argv)
    identity = detect_identity(agent_id=args.agent_id)
    return cli._listen_posts(args, identity)


# --- new, accept, status, leave ------------------------------------------------------


def test_a_shared_room_carries_messages_between_the_machines_it_was_offered_to(run):
    """The whole point, end to end: `share new` on one machine, `share accept`
    on the other, and `--room shared:core` lands both in the same room —
    though no human carried it and neither printed its key."""
    pair_up(run)
    code, out, _ = run("laptop", "share", "new", "core", "--with", "lab")
    assert (code, out) == (0, "shared:core offered to lab\n")

    code, out, _ = run("cloud", "--json", "share", "status")
    assert code == 0
    [offer] = json.loads(out)["offers"]
    assert (offer["offer"], offer["held"]) == ("lab/core", None)
    code, out, _ = run("cloud", "share", "accept", "core")
    assert (code, out) == (0, "shared:core\n")

    assert run("laptop", "--room", "shared:core", "say", "general", "hi from the laptop")[0] == 0
    code, out, _ = run("cloud", "--room", "shared:core", "inbox", "-c", "general")
    assert code == 0 and "hi from the laptop" in out


def test_status_shows_who_has_each_room_from_the_live_records(run):
    """A list entry is "has it" only once that pair's own record carries the
    room back — what the peer says, not what this side hopes — and the note
    saying it accepted is shown every time, not once."""
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    code, out, _ = run("laptop", "share", "status")
    assert code == 0 and "shared:core" in out and "lab (offered)" in out

    run("cloud", "share", "accept", "core")
    for _ in range(2):
        code, out, _ = run("laptop", "share", "status")
        assert code == 0 and "lab (has it)" in out and "lab accepted core" in out
    _, out, _ = run("laptop", "--json", "share", "status", "core")
    [room] = json.loads(out)["rooms"]
    assert room["list"] == [{"pair": "lab", "has": True, "offered": True}]
    assert room["origin"] == "created"

    code, out, _ = run("cloud", "share", "status")
    assert code == 0 and "lab's core" in out


def test_status_of_a_name_exits_1_unless_it_is_held_or_offered_here(run):
    """A script asking about one room by name is asking whether it can use it
    or accept it; an empty document with exit 0 would answer yes."""
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    assert run("laptop", "share", "status", "core")[0] == 0
    assert run("cloud", "share", "status", "core")[0] == 0      # offered
    code, out, _ = run("cloud", "--json", "share", "status", "nope")
    assert code == 1 and json.loads(out)["rooms"] == []
    run("cloud", "share", "leave", "core")                       # refused
    assert run("cloud", "share", "status", "core")[0] == 1


def test_accept_under_another_name_when_the_name_is_taken_here(run):
    """Names are local. A room offered as `core` to a machine that already
    holds a `core` of its own is refused until it is given another name."""
    pair_up(run)
    run("cloud", "share", "new", "core", "--with", "lab")
    run("laptop", "share", "new", "core", "--with", "lab")
    code, out, _ = run("cloud", "--json", "share", "accept", "lab/core")
    assert code == 1 and json.loads(out)["state"] == "name_taken"
    code, out, _ = run("cloud", "share", "accept", "lab/core", "--name", "laptop-core")
    assert (code, out) == (0, "shared:laptop-core\n")


def test_leave_says_who_was_told_and_a_refused_offer_is_said_as_one(run):
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    run("laptop", "share", "new", "side", "--with", "lab")
    run("cloud", "share", "accept", "core")

    code, out, err = run("cloud", "share", "leave", "core", "--note", "done here")
    assert (code, out) == (0, "left shared:core; told lab\n")
    assert "nothing was revoked" in err
    code, out, _ = run("cloud", "share", "leave", "side")
    assert (code, out) == (0, "refused the offer of side; told lab\n")

    _, out, _ = run("laptop", "--json", "share", "status")
    view = json.loads(out)
    assert view["rooms"][0]["name"] == "core" and view["rooms"][0]["list"] == []
    assert any(n["kind"] == "left" and "done here" in n["text"] for n in view["notes"])


def test_new_from_honours_a_left_and_says_what_running_in_parallel_strands(run):
    """`--from` takes the old list as it stands after reading its pairs: a pair
    that left is not offered the new room, and is named. The old room runs on;
    a lease held there excludes nobody in the new one, and that is said."""
    pair_up(run, "lab", "cloud")
    pair_up(run, "ci", "runner")
    run("laptop", "share", "new", "core", "--with", "lab", "--with", "ci")
    run("cloud", "share", "accept", "core")
    run("runner", "share", "accept", "core")
    run("runner", "share", "leave", "core")
    assert run("laptop", "--room", "shared:core", "claim", "db/migrations")[0] == 0

    code, out, err = run("laptop", "share", "new", "core2", "--from", "core")
    assert (code, out) == (0, "shared:core2 offered to lab\n")
    assert "left off core2's list, though on core's: ci" in err
    assert "runs on in parallel" in err and "1 lease(s)" in err
    # The leave is applied now, so core's own list no longer has ci to drop.
    _, out, _ = run("laptop", "--json", "share", "status", "core")
    assert [e["pair"] for e in json.loads(out)["rooms"][0]["list"]] == ["lab"]
    _, out, _ = run("laptop", "--json", "share", "new", "core3", "--from", "core")
    result = json.loads(out)
    assert (result["offered"], result["from"]) == (["lab"], "core")
    assert result["dropped"] == [] and len(result["left_behind"]) == 1


def test_new_names_a_pair_it_could_not_reach_and_the_offer_lands_later(
        run, tmp_path, monkeypatch):
    """"Offered to" is the claim, so it lists only the pairs whose room took
    the offer. One that could not be reached is named, with why, and the
    next touch that reaches it writes the offer."""
    pair_up(run, "lab", "cloud")
    pair_up(run, "ci", "runner")
    down = pairing.pair_room(store_of(tmp_path, "laptop").get("ci").pair_secret).workspace
    bound = cli.Client

    class CiPairRoomDown(bound):
        def board_list(self, **kwargs):
            if kwargs.get("prefix") == sharing.PREFIX and self.config.workspace == down:
                raise httpx.ConnectError("the ci pair room is unreachable")
            return super().board_list(**kwargs)

    monkeypatch.setattr(cli, "Client", CiPairRoomDown)
    code, out, err = run("laptop", "share", "new", "core", "--with", "lab", "--with", "ci")
    assert (code, out) == (0, "shared:core offered to lab\n")
    assert "could not reach ci" in err and "next touch" in err
    _, out, _ = run("laptop", "--json", "share", "new", "core2", "--with", "ci")
    result = json.loads(out)
    assert result["offered"] == [] and [u["pair"] for u in result["unreached"]] == ["ci"]

    monkeypatch.setattr(cli, "Client", bound)
    assert run("laptop", "share", "status")[0] == 0
    _, out, _ = run("runner", "--json", "share", "status")
    assert sorted(o["offer"] for o in json.loads(out)["offers"]) == ["ci/core", "ci/core2"]


# --- json, refusals, and what never leaves -------------------------------------------


def test_every_share_command_prints_one_json_document(run):
    def doc(machine, *argv):
        code, out, _ = run(machine, "--json", *argv)
        assert code == 0, (argv, out)
        return json.loads(out)

    pair_up(run)
    made = doc("laptop", "share", "new", "core", "--with", "lab", "--note", "the core work")
    assert (made["name"], made["room"], made["offered"]) == ("core", "shared:core", ["lab"])
    assert made["workspace"].startswith("ws_")
    status = doc("cloud", "share", "status")
    assert status["offers"][0]["note"] == "the core work"
    kept = doc("cloud", "share", "accept", "core")
    assert (kept["room"], kept["pair"], kept["offered_back"]) == ("shared:core", "lab", True)
    left = doc("cloud", "share", "leave", "core")
    assert (left["kind"], left["told"]) == ("room", ["lab"])


def test_a_refusal_under_json_is_one_document_carrying_its_state(run, tmp_path, monkeypatch,
                                                                capsys):
    """The pair group's shape: the sentence on stderr, `{error, state}` on
    stdout, exit 1 — so a script tells "nobody named" from "that pair is not
    on this machine" from "the name is taken" without parsing English."""
    pair_up(run)

    def refused(machine, *argv) -> str:
        code, out, err = run(machine, "--json", *argv)
        assert code == 1 and err.startswith("error: "), (argv, out, err)
        return json.loads(out)["state"]

    assert refused("laptop", "share", "new", "core") == "invalid"
    assert refused("laptop", "share", "new", "Not A Name", "--with", "lab") == "invalid"
    assert refused("laptop", "share", "new", "core", "--with", "nosuch") == "unknown"
    assert refused("laptop", "share", "new", "core", "--note", "x" * 201,
                   "--with", "lab") == "invalid"
    run("laptop", "share", "new", "core", "--with", "lab")
    assert refused("laptop", "share", "new", "core", "--with", "lab") == "name_taken"
    assert refused("cloud", "share", "accept", "nosuch") == "unknown"
    assert refused("cloud", "share", "leave", "nosuch") == "unknown"

    _be("laptop", tmp_path, monkeypatch)
    monkeypatch.setenv("SWITCHBOARD_PAIRS", "")
    capsys.readouterr()
    assert main(["--url", BASE_URL, "--json", "share", "status"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "disabled"


def test_a_share_store_that_cannot_be_used_is_a_refusal_not_the_hub(run, tmp_path):
    """The share store holds the only copy of a created room's keys, so it is
    never written over — and its failure is a refusal saying where to look,
    with `state: store`, on a share command and under `--room shared:` alike."""
    shared_core(run)
    lock = tmp_path / "laptop" / "pairs" / sharing.LOCK_FILE
    if lock.exists():
        lock.unlink()
    lock.mkdir()   # every read takes the lock first; a directory cannot be opened as one
    code, out, err = run("laptop", "--json", "share", "status")
    assert code == 1 and json.loads(out)["state"] == "store"
    assert "cannot reach hub" not in err
    code, out, err = run("laptop", "--room", "shared:core", "say", "general", "hi")
    assert code == 1 and out == "" and "the share store could not be used" in err


def test_no_result_carries_a_room_key_write_key_or_token(run, tmp_path):
    """A room's keys travel sealed through pair rooms so that no conversation
    holds them, and nothing revokes them. Every result reports the room
    without them — the one way out is `--room shared:<name> invite`."""
    pair_up(run)
    outputs = []

    def look(machine, *argv):
        code, out, err = run(machine, *argv)
        assert code == 0, (argv, err)
        outputs.append(out + err)

    look("laptop", "--token", "a-hub-token", "--json", "share", "new", "core", "--with", "lab",
         "--with-token")
    look("laptop", "share", "new", "side", "--with", "lab")
    look("cloud", "--json", "share", "status")
    look("cloud", "share", "status")
    look("cloud", "--json", "share", "accept", "core")
    look("cloud", "share", "accept", "side")
    look("laptop", "--json", "share", "status")
    look("laptop", "share", "status")
    look("cloud", "--json", "--room", "shared:core", "whoami")
    look("cloud", "--room", "shared:core", "say", "general", "hi")
    look("cloud", "--json", "share", "leave", "side")
    room = doc_of(tmp_path, "laptop")["rooms"]["core"]
    assert room["token"] == "a-hub-token"   # the offer did carry it
    for secret in (room["key"], room["write_key"], room["token"]):
        assert not any(secret in text for text in outputs)


def test_whoami_reports_the_shared_room_and_will_not_print_its_key(run, tmp_path):
    shared_core(run)
    room = doc_of(tmp_path, "cloud")["rooms"]["core"]
    code, out, _ = run("cloud", "--json", "--room", "shared:core", "whoami")
    assert code == 0
    payload = json.loads(out)
    assert payload["workspace"] == room["workspace"] and payload["encrypted"] is True
    assert (payload["shared"]["room"], payload["shared"]["pairs"]) == ("shared:core", ["lab"])

    for flag in ("--show-key", "--env"):
        code, out, err = run("cloud", "--room", "shared:core", "whoami", flag)
        assert code == 1 and out == ""
        assert "--room shared:core invite" in err and "nothing revokes" in err
        assert room["key"] not in err


def test_an_invite_is_the_room_itself_and_says_nothing_revokes_it(run, tmp_path):
    """The one deliberate way a shared room's keys leave. The usual caveat —
    the key opens a lobby and every room sealed with it — misdescribes a key
    minted for this room alone; what is true is that no heal or leave takes
    it back."""
    shared_core(run)
    code, out, err = run("cloud", "--json", "--room", "shared:core", "invite")
    assert code == 0
    payload = json.loads(out)
    assert "carries_key" not in payload and payload["shared"]["room"] == "shared:core"
    assert "nothing revokes" in err and "share new NAME --from core" in err
    room = doc_of(tmp_path, "cloud")["rooms"]["core"]
    assert Invite.decode(payload["invite"]).workspace == room["workspace"]


def test_an_invite_of_a_room_on_another_hub_carries_no_token_from_here(
        run, h, tmp_path, monkeypatch):
    """`invite` falls back to `--token`, then the environment's, then the
    checkout's token — right for this environment's rooms, and for a shared
    room on another machine's hub a credential handed to a host nobody here
    chose. A shared room's invite carries the token the room resolved with."""
    shared_core(run)
    store = shares_of(tmp_path, "cloud")
    data = store.load()
    data["rooms"]["core"].update(url="https://elsewhere.example", refreshed_at=h.now)
    store.save(data)
    code, out, _ = run("cloud", "--token", "this-hubs-token", "--json",
                       "--room", "shared:core", "invite")
    assert code == 0
    blob = Invite.decode(json.loads(out)["invite"])
    assert (blob.url, blob.token) == ("https://elsewhere.example", None)


# --- the handle -------------------------------------------------------------------------


def test_the_handle_is_resolved_from_the_store_on_every_use(run, tmp_path):
    """A terminal can leave the room between two commands; the handle must then
    refuse, saying so, rather than keep naming a room this machine left. An
    offer not yet accepted is refused as one, with the way to keep it."""
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    run("cloud", "share", "status")
    code, out, err = run("cloud", "--json", "--room", "shared:core", "say", "general", "hi")
    assert code == 1 and json.loads(out)["state"] == "offered" and "share accept core" in err

    run("cloud", "share", "accept", "core")
    assert run("cloud", "--room", "shared:core", "say", "general", "hi")[0] == 0
    run("cloud", "share", "leave", "core")
    code, out, err = run("cloud", "--json", "--room", "shared:core", "say", "general", "hi")
    assert code == 1 and json.loads(out)["state"] == "left"
    code, out, _ = run("cloud", "--json", "--room", "shared:nosuch", "inbox")
    assert code == 1 and json.loads(out)["state"] == "unknown"
    code, out, err = run("cloud", "--json", "--room", "shared:nosuch", "listen",
                         "--until", "+1")
    assert code == 1 and out == "" and "no shared room called 'nosuch'" in err


def test_the_handle_reads_and_writes_the_rooms_records_once_they_are_a_day_old(
        run, h, tmp_path, monkeypatch):
    """A machine that only ever works in a shared room — no share command, no
    listener on its pairs — must still keep its records alive and read its
    peers'. So `--room shared:` touches the room's pairs when its records were
    last refreshed over a day ago, and is a local read otherwise."""
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    run("cloud", "share", "accept", "core")
    real, calls = sharing.touch, []

    def counted(*args, **kwargs):
        calls.append(kwargs.get("rooms"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sharing, "touch", counted)
    _, _, err = run("laptop", "--room", "shared:core", "say", "general", "same day")
    assert calls == [] and "accepted" not in err

    h.advance(2 * DAY)
    code, _, err = run("laptop", "--room", "shared:core", "say", "general", "two days on")
    assert code == 0 and calls == [["core"]]
    assert "shared: lab accepted core" in err
    assert doc_of(tmp_path, "laptop")["rooms"]["core"]["refreshed_at"] == h.now
    run("laptop", "--room", "shared:core", "say", "general", "and again")
    assert calls == [["core"]]


def test_a_shared_room_on_another_hub_gets_no_token_from_this_environment(
        run, h, tmp_path, monkeypatch):
    """An offer's URL is one another machine chose. With no token in the offer,
    the room gets this environment's token only when it is on this
    environment's hub — never the book's `token or config.token`, which would
    hand it to whatever host the offer named."""
    shared_core(run)
    store = shares_of(tmp_path, "cloud")
    data = store.load()
    data["rooms"]["core"]["refreshed_at"] = h.now
    store.save(data)
    _be("cloud", tmp_path, monkeypatch)

    def config():
        return cli._make_config(build_parser().parse_args(
            ["--url", BASE_URL, "--token", "this-hubs-token", "--room", "shared:core",
             "inbox"]))

    assert config().token == "this-hubs-token"
    data["rooms"]["core"]["url"] = "https://elsewhere.example"
    store.save(data)
    moved = config()
    assert (moved.url, moved.token, moved.token_source) == ("https://elsewhere.example",
                                                            None, "none")


def test_a_shared_room_is_never_written_into_the_book(run, tmp_path):
    """Its key arrived sealed in a pair room and was never in a conversation,
    so there is no way it was acquired for an entry to point back at."""
    shared_core(run)
    run("cloud", "--room", "shared:core", "say", "general", "hi")
    run("cloud", "--room", "shared:core", "invite")
    workspace = doc_of(tmp_path, "cloud")["rooms"]["core"]["workspace"]
    book = knownrooms.Book(str(tmp_path / "cloud" / "known-rooms.json"))
    assert all(room.workspace != workspace for room in book.rooms())


def test_a_book_label_cannot_pose_as_a_shared_room(run):
    """`--room shared:x` never reads the book, so a book room labelled
    `shared:x` could not be reached by its label, and a shared room called x
    would answer to it instead."""
    with pytest.raises(SystemExit, match="reserved"):
        run("laptop", "join", "swb1_whatever", "--save", "shared:x")
    with pytest.raises(SystemExit, match="reserved"):
        run("laptop", "rooms", "--label", "w_abc=shared:x")


def test_an_mcp_tool_name_typed_as_a_command_says_which_one_to_use(capsys):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["share_new", "core"])
    assert exit_info.value.code == 2
    assert "share new <name> --with <pair>" in capsys.readouterr().err


# --- where a listener parks ------------------------------------------------------------


def test_listen_parks_in_shared_rooms_used_here_lately_and_names_the_rest(
        run, h, tmp_path, monkeypatch, capsys):
    """A shared room is never in the book, so without this an agent working in
    one all week, parked the way the skill says, is woken by nothing said
    there. One idle for a day is left out — and said, with the flag that adds
    it back, because a room skipped in silence looks exactly like a quiet one."""
    shared_core(run)
    _be("cloud", tmp_path, monkeypatch)
    posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby", "--no-pairs"])
    workspace = doc_of(tmp_path, "cloud")["rooms"]["core"]["workspace"]
    assert [(p.role, p.workspace) for p in posts][1:] == [("shared:core", workspace)]

    h.advance(2 * DAY)
    capsys.readouterr()
    posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby", "--no-pairs"])
    assert [p.role for p in posts] == ["room"]
    err = capsys.readouterr().err
    assert "not parking in shared:core: not used on this machine in the last day" in err
    assert "add --in shared:core" in err

    posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby", "--no-pairs",
                    "--in", "shared:core"])
    assert [p.role for p in posts] == ["room", "shared:core"]
    assert "not parking in shared:core" not in capsys.readouterr().err
    posts = _posts(["--url", BASE_URL, "listen", "--only", "shared:core"])
    assert [p.role for p in posts] == ["shared:core"]


def test_listen_only_a_shared_room_not_held_says_why_in_a_sentence(run, capsys):
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    run("cloud", "share", "status")
    with pytest.raises(SystemExit, match="none of the rooms named could be opened"):
        run("cloud", "listen", "--only", "shared:core", "--until", "+1")
    assert "not parking in shared:core" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="none of the rooms named could be opened"):
        run("cloud", "listen", "--only", "shared:nosuch", "--until", "+1")


def test_a_pair_carrying_shared_rooms_counts_as_active(run, h, tmp_path, monkeypatch, capsys):
    """A pair that is only a conduit for shared rooms is not "used" in
    pairing's sense, and goes idle within a day — but its work room is where
    a peer's share wake lands. On a held room's list, it is parked in."""
    shared_core(run)
    h.advance(2 * DAY)
    for machine in ("laptop", "cloud"):
        _be(machine, tmp_path, monkeypatch)
        capsys.readouterr()
        posts = _posts(["--url", BASE_URL, "-w", WS, "listen", "--no-lobby"])
        assert ("pair:lab", "lab") in [(p.role, p.pair) for p in posts]
        assert "not parking in pair:lab" not in capsys.readouterr().err


def test_listen_under_a_shared_handle_takes_the_lobby_from_its_own_key(
        run, tmp_path, monkeypatch):
    """A lobby derived from the shared room's key is not this environment's
    lobby; under `--room shared:<name>` the lobby comes from its own key."""
    from switchboard import rooms
    from switchboard.crypto import generate_key

    shared_core(run)
    own_key = generate_key()
    _be("cloud", tmp_path, monkeypatch)
    monkeypatch.setenv("SWITCHBOARD_KEY", own_key)
    posts = _posts(["--url", BASE_URL, "--room", "shared:core", "listen", "--no-pairs"])
    assert posts[0].role == "shared:core"
    [lobby] = [p for p in posts if p.role == "lobby"]
    room = doc_of(tmp_path, "cloud")["rooms"]["core"]
    assert lobby.workspace == rooms.lobby(own_key).workspace
    assert lobby.workspace != rooms.lobby(room["key"]).workspace


# --- what wakes a listener ----------------------------------------------------------------


def test_a_peers_offer_wakes_a_listener_in_the_pairs_work_room_with_the_note(
        run, h, tmp_path, monkeypatch, capsys):
    """The wake note says only "look". The listener reads the pair, finds the
    offer, and comes back holding the note — and pointed at `share status`,
    not at an inbox that shows the bare tag."""
    pair_up(run)
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "cloud",
                                     "listen", "--only", "pair:lab", "--until", "+8")
    sharing.share_new("core", h.client_config(agent_id="laptop"), pairs=["lab"],
                      **as_machine(h, tmp_path, "laptop"))
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    captured = capsys.readouterr()
    wake = json.loads(out + captured.out)
    assert [pairing.message_tag(m) for m in wake["messages"]] == [pairing.TAG_SHARED]
    [note] = wake["shared"]["notes"]
    assert note["kind"] == "offered" and "share accept lab/core" in note["text"]
    assert "share status" in wake["pair"]["next"]
    assert "listen: shared: lab offers room core" in captured.err


def test_neither_its_own_share_nor_a_forged_wake_wakes_a_listener(
        run, h, tmp_path, monkeypatch, capsys):
    """Every write this machine makes posts a wake its own listener does not
    skip, and anyone holding the work room's key can post one. A wake whose
    read finds nothing new is not a wake: both leave the listener parked."""
    pair_up(run)
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "laptop",
                                     "listen", "--only", "pair:lab", "--until", "+4")
    sharing.share_new("core", h.client_config(agent_id="laptop"), pairs=["lab"],
                      **as_machine(h, tmp_path, "laptop"))
    resolved = pairing.resolve("lab", h.client_config(), store=store_of(tmp_path, "cloud"),
                               connect=h.client_class(), agent_id="cloud", now=h.now)
    with h.client_class()(resolved.config, agent_id="holder") as holder_of_the_key:
        holder_of_the_key.post(pairing.CHANNEL, {"t": pairing.TAG_SHARED})
    _join(thread)
    assert result.get("code") == cli.EXIT_DEADLINE, result
    assert (out + capsys.readouterr().out) == ""


def test_a_channel_listener_still_wakes_on_shared_room_news(
        run, h, tmp_path, monkeypatch, capsys):
    """Under `-c general` the pair channel is polled for protocol notes alone;
    a share wake there passes, because a peer offering a room is news the
    agent did not have to ask for by channel."""
    pair_up(run)
    thread, result, out = _listening(
        capsys, tmp_path, monkeypatch, "cloud", "--room", "pair:lab", "listen",
        "-c", "general", "--no-lobby", "--no-pairs", "--until", "+8")
    sharing.share_new("core", h.client_config(agent_id="laptop"), pairs=["lab"],
                      **as_machine(h, tmp_path, "laptop"))
    _join(thread)
    assert not thread.is_alive() and result.get("code") == 0, result
    wake = json.loads(out + capsys.readouterr().out)
    assert [m["channel"] for m in wake["messages"]] == [pairing.CHANNEL]
    assert wake["shared"]["notes"][0]["kind"] == "offered"


def test_news_that_arrived_while_nothing_was_parked_wakes_at_arming_once(run):
    """The one read as it arms: an offer written while no listener was parked
    wakes it at once, as a message already waiting would. Applied by that
    read, it is not news to the next arming."""
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    code, out, err = run("cloud", "listen", "--only", "pair:lab", "--until", "+5")
    assert code == 0
    wake = json.loads(out)
    assert (wake["messages"], wake["role"]) == ([], "shared")
    assert wake["shared"]["notes"][0]["kind"] == "offered"
    assert "listen: shared: lab offers room core" in err

    code, out, _ = run("cloud", "listen", "--only", "pair:lab", "--until", "+1")
    assert code == cli.EXIT_DEADLINE and out == ""


def test_do_not_disturb_is_not_broken_by_shared_room_news(
        run, h, tmp_path, monkeypatch, capsys):
    """Do-not-disturb wakes on the types senders declared, and a share wake is
    a plain note. News found as it arms is said on stderr and kept for `share
    status`; news arriving while parked waits for the next touch; and the
    listener stays parked to its deadline either way."""
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    _be("cloud", tmp_path, monkeypatch)
    capsys.readouterr()
    thread, result = _in_thread(["--url", BASE_URL, "--room", "pair:lab", "listen",
                                 "--type", "urgent", "--no-lobby", "--no-pairs",
                                 "--until", "+3"])
    out, armed = _wait_for(capsys, "listen: parked")
    assert "listen: shared: lab offers room core" in armed
    sharing.share_new("side", h.client_config(agent_id="laptop"), pairs=["lab"],
                      **as_machine(h, tmp_path, "laptop"))
    _join(thread)
    assert result.get("code") == cli.EXIT_DEADLINE, result
    assert (out + capsys.readouterr().out) == ""
    code, out, _ = run("cloud", "--json", "share", "status")
    assert sorted(o["name"] for o in json.loads(out)["offers"]) == ["core", "side"]


def test_a_listener_armed_before_accept_parks_in_the_room_once_it_is_used(
        run, h, tmp_path, monkeypatch, capsys):
    """Sessions keep a listener armed, so `share accept` usually runs with one
    already parked — which chose its rooms at arming. The room is picked up
    while parked, and a message there wakes it."""
    monkeypatch.setattr(cli, "_RESCAN_EVERY", 0.2)
    pair_up(run)
    run("laptop", "share", "new", "core", "--with", "lab")
    run("cloud", "share", "status")       # the offer is no longer news
    thread, result, out = _listening(capsys, tmp_path, monkeypatch, "cloud", "-w", WS,
                                     "listen", "--no-lobby", "--until", "+10")
    sharing.accept("core", h.client_config(agent_id="cloud"), **as_machine(h, tmp_path, "cloud"))
    more, _ = _wait_for(capsys, "[shared:core], begun after this listener was armed")
    room = sharing.resolve_room("core", h.client_config(), store=shares_of(tmp_path, "laptop"),
                                now=h.now)
    # A direct message: a listener in a room wakes on the agent's own channel
    # and its subscriptions there, as in any room, and it has subscribed to none.
    with h.client_class()(room.config, agent_id="cloud") as cloud:
        addressed_as = cloud.agent_id
    with h.client_class()(room.config, agent_id="laptop") as laptop:
        laptop.send(addressed_as, "in the shared room")
    thread.join(timeout=5)   # woken by the message, well before its deadline
    assert result.get("code") == 0, result
    _join(thread)
    wake = json.loads(out + more + capsys.readouterr().out)
    assert wake["role"] == "shared:core"
    assert wake["messages"][0]["body"] == "in the shared room"


# --- what a peer's records can make the surface do ---------------------------------------


def _forge(h, tmp_path, machine: str, pair: str, value: dict, rid: str) -> None:
    """Write a record into `pair`'s room, in `machine`'s slot — the peer's own pen."""
    record = store_of(tmp_path, machine).get(pair)
    room = pairing.pair_room(record.pair_secret)
    with h.client("forger", workspace=room.workspace, key=room.key,
                  write_key=room.write_key) as client:
        client.board_set(f"{sharing.PREFIX}{record.role}/{rid}", value,
                         ttl=sharing.RECORD_TTL)


def _an_offer(name: str, *, note: str, at: float) -> tuple[dict, str]:
    from switchboard.crypto import generate_key
    from switchboard.writekey import RoomWriteKey, generate_write_key

    write_key = generate_write_key()
    workspace = RoomWriteKey.from_seed(write_key).workspace
    offer = {"url": BASE_URL, "workspace": workspace, "key": generate_key(),
             "write_key": write_key, "token": None}
    value = {"t": sharing.TAG_SHARE, "v": 1, "name": name, "offer": offer, "left": False,
             "note": note, "at": at}
    return value, sharing.room_id(BASE_URL, workspace)


def test_peer_text_no_terminal_can_print_never_reaches_one(h, run, tmp_path):
    """F6 / SEC-2: a lone surrogate in a peer's note made `share status`, its
    `--json`, and the listener's wake raise on a strict UTF-8 stdout — for the
    seven days a note is kept, on every run."""
    pair_up(run)
    value, rid = _an_offer("core", note="hi \ud800 there", at=h.now + 1)
    _forge(h, tmp_path, "laptop", "lab", value, rid)
    code, out, err = run("cloud", "listen", "--only", "pair:lab", "--until", "+3")
    assert code == 0, err
    [note] = json.loads(out)["shared"]["notes"]
    assert note["kind"] == "offered" and "\ud800" not in note["text"]
    code, out, err = run("cloud", "share", "status")
    assert code == 0, err
    assert "lab/core" in out
    code, out, err = run("cloud", "--json", "share", "status")
    assert code == 0, err
    assert json.loads(out)["offers"][0]["note"].startswith("hi")


def test_a_shared_room_that_cannot_be_opened_costs_that_room_not_the_listener(
        h, run, tmp_path):
    """F4 / SEC-5, in depth: `sharing` now refuses an offer whose key no
    cipher takes, but a room kept before that check — or by another release —
    must cost that room alone. `Client()` raising stopped `listen` before it
    parked anywhere."""
    shared_core(run)
    store = shares_of(tmp_path, "cloud")
    doc = store.load()
    doc["rooms"]["core"]["key"] = "not-a-key"
    store.save(doc)
    code, _, err = run("cloud", "-w", WS, "listen", "--no-lobby", "--no-pairs",
                       "--until", "+1")
    assert code == cli.EXIT_DEADLINE, err
    assert "not parking in shared:core: it cannot be opened (CryptoError)" in err
    assert "share leave core" in err


def test_arming_reads_every_pair_so_an_offer_over_an_idle_one_wakes_at_once(
        h, run, tmp_path):
    """Conformance F3: the arming read covered only the pairs it parked in. A
    pair idle for days, carrying nothing yet, is neither — so an offer over
    it went unsaid to a CLI-only agent until something ran `share status`."""
    pair_up(run)
    h.advance(2 * DAY)
    assert run("laptop", "share", "new", "core", "--with", "lab")[0] == 0
    code, out, err = run("cloud", "-w", WS, "listen", "--no-lobby", "--until", "+3")
    assert "not parking in pair:lab" in err   # idle, so not parked in
    assert code == 0, err
    wake = json.loads(out)
    assert wake["role"] == "shared"
    assert [n["kind"] for n in wake["shared"]["notes"]] == ["offered"]
    # Reading it did not make the pair one this machine uses.
    assert pairing.active(store_of(tmp_path, "cloud"), now=h.now) == []
