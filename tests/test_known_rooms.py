"""The book of known rooms: references, a sweep, and parking where you suspect.

What is pinned here, in order of how badly it fails when it breaks: a key
never lands in the file as a value, only as the way it was acquired; a room
recorded by one command is found by another (`join` writes it, `rendezvous`
and `find` sweep it, `--room` runs there); and the sweep is read-only. The
listener half — parking in a recently joined room unasked — lives in
``test_wake_listener.py``, because it needs a real process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard import knownrooms, rendezvous
from switchboard.cli import main
from switchboard.crypto import generate_key
from switchboard.invite import Invite
from switchboard.testing import BASE_URL, hub

WS = "known-ws"
OTHER = "known-other"


# --- the file ----------------------------------------------------------------

def test_a_key_from_the_environment_is_kept_as_the_variables_name(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_KEY_OPS", "value-that-must-never-be-written-down")
    book = knownrooms.Book()
    book.remember(knownrooms.KnownRoom(
        label="ops", url="https://hub.example", workspace="w_ops",
        key=knownrooms.env_reference("SWITCHBOARD_KEY_OPS"), learned="init",
    ))
    text = Path(book.path).read_text()
    assert "never-be-written-down" not in text
    assert "SWITCHBOARD_KEY_OPS" in text
    # Resolved the way it was acquired — and not at all once that way is gone.
    assert knownrooms.Book().by_label("ops").config().key == \
        "value-that-must-never-be-written-down"
    monkeypatch.delenv("SWITCHBOARD_KEY_OPS")
    assert knownrooms.Book().by_label("ops").config() is None, \
        "an encrypted room whose key is gone must not open in the clear"


def test_a_key_that_arrived_as_an_invite_is_kept_as_the_invite():
    blob = Invite(url="https://hub.example", workspace="w_inv", token="tok",
                  key="k" * 43, note="island: meet here").encode()
    book = knownrooms.Book()
    book.remember(knownrooms.KnownRoom(
        label="island", url="https://hub.example", workspace="w_inv",
        key=knownrooms.invite_reference(blob), token=knownrooms.invite_reference(blob),
        learned="join",
    ))
    config = knownrooms.Book().by_label("island").config()
    assert config.key == "k" * 43 and config.token == "tok"
    assert blob in Path(book.path).read_text()


def test_there_is_no_way_to_store_a_bare_value():
    assert not hasattr(knownrooms, "value_reference")
    assert knownrooms.resolve_secret({"from": "value", "value": "x"}, "SWITCHBOARD_KEY") is None


def test_remember_keeps_the_first_label_and_learned_and_refreshes_last_used():
    book = knownrooms.Book()
    first = knownrooms.KnownRoom(label="one", url="https://h", workspace="w", learned="join")
    book.remember(first, now=1000.0)
    book.remember(knownrooms.KnownRoom(label="two", url="https://h", workspace="w",
                                       learned="invite"), now=2000.0)
    (room,) = knownrooms.Book().rooms()
    assert (room.label, room.learned, room.first_used, room.last_used) == \
        ("one", "join", 1000.0, 2000.0)


def test_recent_is_rooms_you_were_put_in_lately():
    book = knownrooms.Book()
    book.remember(knownrooms.KnownRoom(label="joined", url="https://h", workspace="a",
                                       learned="join"), now=5000.0)
    book.remember(knownrooms.KnownRoom(label="old", url="https://h", workspace="b",
                                       learned="join"), now=100.0)
    book.remember(knownrooms.KnownRoom(label="repo", url="https://h", workspace="c",
                                       learned="init"), now=5000.0)
    assert [r.label for r in knownrooms.Book().recent(now=5100.0)] == ["joined"]


def test_a_corrupt_file_reads_as_empty(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    monkeypatch.setenv("SWITCHBOARD_KNOWN_ROOMS", str(path))
    assert knownrooms.Book().rooms() == []


def test_an_empty_setting_disables_the_book(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_KNOWN_ROOMS", "")
    book = knownrooms.Book()
    assert not book.enabled
    book.remember(knownrooms.KnownRoom(label="x", url="https://h", workspace="w"))
    assert book.rooms() == []


# --- the commands --------------------------------------------------------------

@pytest.fixture
def cli_hub(monkeypatch):
    """Encrypted, like the rendezvous suite, and with a second room on the
    same hub under its own key — the situation the book exists for."""
    import switchboard.cli as cli_module

    key = generate_key()
    with hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.setenv("SWITCHBOARD_KEY", key)
        handle.workspace_key = key
        handle.other_key = generate_key()
        handle.other_invite = Invite(
            url=BASE_URL, workspace=OTHER, token=handle.token, key=handle.other_key,
            note="other-room: where the peer is",
        ).encode()
        yield handle


def _peer_in_other(cli_hub, name="peer"):
    return cli_hub.client(name, workspace=OTHER, key=cli_hub.other_key,
                          register=True, branch="feat/peer-work")


def test_join_records_the_room_and_rendezvous_sweeps_it(cli_hub, capsys):
    peer = _peer_in_other(cli_hub)
    assert main(["join", cli_hub.other_invite, "--no-verify"]) == 0
    capsys.readouterr()
    (room,) = knownrooms.Book().rooms()
    assert (room.label, room.learned, room.workspace) == ("other-room", "join", OTHER)
    assert room.key["from"] == "invite"

    assert main(["--url", BASE_URL, "-w", WS, "--json", "rendezvous", "t",
                 "--want", "x", "--wait", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    (report,) = out["elsewhere"]
    assert report["label"] == "other-room" and report["problem"] is None
    assert [a["name"] for a in report["roster"]] == ["peer"]
    assert report["reachable"] == []
    # The sweep is read-only: no note of ours, no presence of ours, over there.
    assert not [e for e in peer.board_list(prefix=rendezvous.PREFIX)]
    assert [a["name"] for a in peer.agents()] == ["peer"]
    # And it remembered who was seen there, by name.
    assert "peer" in knownrooms.Book().by_label("other-room").peers


def test_here_keeps_rendezvous_to_this_room(cli_hub, capsys):
    _peer_in_other(cli_hub)
    assert main(["join", cli_hub.other_invite, "--no-verify"]) == 0
    capsys.readouterr()
    assert main(["--url", BASE_URL, "-w", WS, "--json", "rendezvous", "t",
                 "--want", "x", "--wait", "0", "--here"]) == 0
    assert json.loads(capsys.readouterr().out)["elsewhere"] == []


def test_find_says_which_room_and_whether_parked(cli_hub, capsys):
    peer = _peer_in_other(cli_hub)
    assert main(["join", cli_hub.other_invite, "--no-verify"]) == 0
    capsys.readouterr()

    assert main(["--json", "find", "feat/peer"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["found"]
    (report,) = [r for r in out["rooms"] if r["roster"]]
    assert report["label"] == "other-room" and report["reachable"] == []

    peer.board_set(rendezvous.listener_key(peer.agent_id), {"pass": 1}, ttl=60)
    assert main(["--json", "find", "peer"]) == 0
    out = json.loads(capsys.readouterr().out)
    (report,) = [r for r in out["rooms"] if r["roster"]]
    assert report["reachable"] == [peer.agent_id]

    assert main(["--json", "find", "nobody-by-that-name"]) != 0


def test_room_flag_runs_a_command_in_a_known_room(cli_hub, capsys):
    _peer_in_other(cli_hub)
    assert main(["join", cli_hub.other_invite, "--no-verify"]) == 0
    capsys.readouterr()
    assert main(["--room", "other-room", "--json", "agents"]) == 0
    assert [a["name"] for a in json.loads(capsys.readouterr().out)] == ["peer"]
    # Naming two rooms at once is refused the way `--lobby --invite` is.
    with pytest.raises(SystemExit):
        main(["--room", "other-room", "--lobby", "agents"])


def test_an_invite_flag_records_the_room_too(cli_hub, capsys):
    _peer_in_other(cli_hub)
    assert main(["--invite", cli_hub.other_invite, "--json", "agents"]) == 0
    capsys.readouterr()
    (room,) = knownrooms.Book().rooms()
    assert (room.label, room.learned) == ("other-room", "invite")


def test_rooms_known_lists_and_forgets(cli_hub, capsys):
    assert main(["join", cli_hub.other_invite, "--no-verify"]) == 0
    capsys.readouterr()
    assert main(["rooms", "--known"]) == 0
    text = capsys.readouterr().out
    assert "other-room" in text and OTHER in text
    assert cli_hub.other_key not in text
    assert main(["rooms", "--label", f"{OTHER}=peer-room"]) == 0
    assert main(["rooms", "--forget", "peer-room"]) == 0
    capsys.readouterr()
    assert main(["--json", "rooms", "--known"]) == 0
    assert json.loads(capsys.readouterr().out)["rooms"] == []
    assert main(["rooms", "--forget", "peer-room"]) != 0


def test_a_room_whose_key_went_away_is_reported_not_skipped(cli_hub, capsys, monkeypatch):
    _peer_in_other(cli_hub)
    named = Invite(url=BASE_URL, workspace=OTHER, token=cli_hub.token,
                   key_id="ops", note="named-key: key by id").encode()
    monkeypatch.setenv("SWITCHBOARD_KEY_OPS", cli_hub.other_key)
    assert main(["join", named, "--no-verify"]) == 0
    capsys.readouterr()
    assert knownrooms.Book().by_label("named-key").key == \
        {"from": "env", "var": "SWITCHBOARD_KEY_OPS"}
    monkeypatch.delenv("SWITCHBOARD_KEY_OPS")
    assert main(["--json", "find", "peer"]) != 0
    out = json.loads(capsys.readouterr().out)
    assert "no longer holds its key" in out["rooms"][0]["problem"]


def test_init_records_a_reference_to_the_checkout_not_the_key(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--local", "--skip-hooks", "--skip-claude-md", "--skip-skill",
                 "--no-input"]) == 0
    capsys.readouterr()
    (room,) = knownrooms.Book().rooms()
    assert room.learned == "init" and room.key == knownrooms.repo_reference(tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    key = settings["env"]["SWITCHBOARD_KEY"]
    assert key not in Path(knownrooms.Book().path).read_text()
    assert room.config().key == key


def test_any_command_in_an_inited_checkout_records_the_repo_room(cli_hub, capsys,
                                                                  tmp_path, monkeypatch):
    """A repo set up before the book existed is listed the first time any
    command runs in it — as a reference to the checkout, never the key."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"switchboard": {
        "command": "switchboard-mcp",
        "env": {"SWITCHBOARD_URL": BASE_URL, "SWITCHBOARD_WORKSPACE": WS},
    }}}))
    assert main(["--json", "agents"]) == 0
    capsys.readouterr()
    (room,) = knownrooms.Book().rooms()
    assert (room.label, room.learned, room.workspace) == (tmp_path.name, "init", WS)
    # The fixture exports the key, so that is how it was acquired.
    assert room.key == {"from": "env", "var": "SWITCHBOARD_KEY"}
    assert cli_hub.workspace_key not in Path(knownrooms.Book().path).read_text()
