"""`switchboard_viewer/viewer.py`: what a human is shown, and what it promises not to do.

The example is here to prove the SDK is enough to build a real application on,
so these tests are as much about the package as about the page. They import it
the way anyone else would — by path, from `examples/`, with no help from the
package — and everything it touches is public API. If a change to `switchboard`
breaks this file, it has broken somebody's application, which is exactly what
we want to hear about here rather than in an issue.

Two properties carry most of the weight. The page must not disturb the room it
is displaying — a viewer that drained an inbox would make an agent miss a
message, and the human watching would never know. And it must be honest under
encryption: name what it can name, mark what it cannot as sealed, and keep
going when a channel belongs to someone holding another key.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from switchboard.client import Client
from switchboard.config import ClientConfig
from switchboard.crypto import generate_key
from switchboard.testing import hub

#: Loaded by path rather than imported, because this suite runs against the
#: repo without `switchboard-viewer` installed. Installing it — and proving it
#: needs nothing private to work — is the `viewer-addon` job's business.
_EXAMPLE = (Path(__file__).resolve().parents[1]
            / "extras" / "viewer" / "switchboard_viewer" / "viewer.py")
_spec = importlib.util.spec_from_file_location("example_viewer", _EXAMPLE)
viewer_app = importlib.util.module_from_spec(_spec)
# Registered before executing, which anything loading a module by path has to
# do: `@dataclass` resolves its own module out of `sys.modules`, and a module
# that is not there yet fails at class-definition time.
sys.modules[_spec.name] = viewer_app
_spec.loader.exec_module(viewer_app)

make_server = viewer_app.make_server
snapshot = viewer_app.snapshot


@pytest.fixture
def h():
    with hub() as handle:
        yield handle


def viewer(h, **kwargs):
    """A client for the viewer to read through — never registered, as in life."""
    return h.client("viewer", **kwargs)


# --- what it shows ----------------------------------------------------------


def test_a_snapshot_carries_the_whole_room(h):
    alice = h.client("alice", register=True, branch="feat/x", kind="local")
    alice.acquire("db/migrations", note="adding 0142")
    alice.post("build", "rebasing onto main")
    alice.board_set("plan", {"next": "0143"})

    view = snapshot(viewer(h))

    assert view["hub"]["reachable"] is True
    assert [a["name"] for a in view["agents"]] == ["alice"]
    assert view["agents"][0]["branch"] == "feat/x"
    assert [le["resource"] for le in view["leases"]] == ["db/migrations"]
    assert view["leases"][0]["note"] == "adding 0142"
    assert [e["key"] for e in view["board"]] == ["plan"]
    assert [m["body"] for m in view["messages"]] == ["rebasing onto main"]
    assert view["messages"][0]["channel"] == "build"
    assert view["notes"] == []


def test_names_come_from_the_roster_not_the_id(h):
    """An agent id is not a name, and under encryption it is not even readable."""
    h.client("my-repo:feat/x", agent_id="a1", register=True).post("build", "hi")
    view = snapshot(viewer(h))
    assert view["messages"][0]["from"] == {"id": "a1", "name": "my-repo:feat/x"}


def test_messages_from_every_channel_arrive_in_hub_order(h):
    a = h.client("a", register=True)
    b = h.client("b", register=True)
    a.post("build", "one")
    b.post("release", "two")
    a.post("build", "three")

    view = snapshot(viewer(h))

    assert [m["body"] for m in view["messages"]] == ["one", "two", "three"]
    assert {c["name"] for c in view["channels"]} == {"build", "release"}


def test_direct_messages_are_marked_as_such(h):
    a = h.client("a", register=True)
    h.client("bob", register=True)
    a.send("bob", "just for you")

    view = snapshot(viewer(h))

    dm = next(m for m in view["messages"] if m["body"] == "just for you")
    assert dm["dm"] is True
    assert dm["channel"] == "@bob"


def test_a_forecast_rides_alongside_its_message_instead_of_inside_the_body(h):
    a = h.client("a", register=True)
    a.post("build", {"text": "looking again soon", "timing_forecast": {"p50": 30, "p95": 90}})

    view = snapshot(viewer(h))

    assert view["messages"][0]["body"] == "looking again soon"
    assert view["messages"][0]["forecast"]["p50"] == 30


def test_an_expired_message_is_not_shown(h):
    h.client("a", register=True).post("build", "gone by then", ttl=60)
    h.advance(61)
    assert snapshot(viewer(h))["messages"] == []


def test_the_channel_cap_is_reported_rather_than_silently_applied(h, monkeypatch):
    monkeypatch.setattr(viewer_app, "MAX_CHANNELS", 2)
    a = h.client("a", register=True)
    for i in range(4):
        a.post(f"chan-{i}", "x")

    view = snapshot(viewer(h))

    assert len(view["channels"]) == 2
    assert any("of 4 channels" in n for n in view["notes"])


# --- what it does not disturb -----------------------------------------------


def test_looking_does_not_advance_an_agents_cursor(h):
    """The whole point of reading through `/channels` rather than `/inbox`."""
    a = h.client("a", register=True, channels=["build"])
    h.client("b", register=True).post("build", "for a")

    snapshot(viewer(h))

    assert [m["body"] for m in a.inbox()] == ["for a"]


def test_the_viewer_does_not_appear_in_the_roster_it_is_showing(h):
    h.client("a", register=True)
    view = snapshot(viewer(h))
    assert [a["id"] for a in view["agents"]] == ["a"]


# --- honesty under encryption -----------------------------------------------


def test_channel_names_survive_a_hub_that_never_saw_them():
    """Blinding is one-way, so the name comes back out of the sealed body."""
    key = generate_key()
    with hub(key=key) as h:
        h.client("a", register=True).post("deploys", "shipping")

        view = snapshot(viewer(h))

        assert [c["channel"] for c in h.store.list_channels(workspace=h.workspace)] != ["deploys"]
        assert [c["name"] for c in view["channels"]] == ["deploys"]
        assert view["channels"][0]["named"] is True
        assert view["messages"][0]["channel"] == "deploys"


def test_a_blinded_resource_is_marked_sealed_rather_than_shown_as_a_name():
    key = generate_key()
    with hub(key=key) as h:
        h.client("a", register=True).acquire("db/migrations", note="adding 0142")

        view = snapshot(viewer(h))

        assert view["hub"]["encrypted"] is True
        assert view["leases"][0]["sealed"] is True
        assert view["leases"][0]["resource"] != "db/migrations"
        # The note is sealed content rather than a blinded identifier, so it
        # is the part a human can actually read.
        assert view["leases"][0]["note"] == "adding 0142"


def test_a_channel_under_another_key_is_shown_sealed_not_dropped():
    """Someone is talking in this room on another key. Losing their traffic
    silently is what the roster warning exists to prevent, so the page shows
    the messages exist and cannot be opened — and reading them does not take
    the readable half down with it."""
    key = generate_key()
    with hub(key=key) as h:
        h.client("mine", register=True).post("build", "readable")
        h.client("theirs", key=generate_key(), register=True).post("build", "not")

        view = snapshot(viewer(h))

        assert [(m["body"], m["sealed_body"]) for m in view["messages"]] == [
            ("readable", False), (None, True),
        ]
        assert sum(1 for c in view["channels"] if c["unreadable"]) == 1
        assert any("different key" in n for n in view["notes"])


def test_a_peer_on_another_key_is_named_in_the_notes():
    key = generate_key()
    with hub(key=key) as h:
        h.client("mine", register=True)
        h.client("theirs", key=generate_key(), register=True)

        view = snapshot(viewer(h))

        assert any("different key" in n for n in view["notes"])
        assert sum(1 for a in view["agents"] if a["unreadable"]) == 1


# --- degrading -------------------------------------------------------------


def test_an_unreachable_hub_is_a_note_on_the_page_not_a_traceback():
    """A hub goes away mid-poll. The page has to survive that: the human is
    watching *because* something might be wrong."""
    # Port 9 is discard, and nothing is listening on it here — a refused
    # connection on loopback, not a network round trip.
    config = ClientConfig(url="http://127.0.0.1:9", url_source="explicit",
                          workspace="test-workspace")
    with Client(config, agent_id="viewer") as dead:
        view = snapshot(dead)

    assert view["hub"]["reachable"] is False
    assert any("cannot reach the hub" in n for n in view["notes"])
    # Said once, not once per section.
    assert len(view["notes"]) == 1
    assert view["messages"] == []


# --- the server -------------------------------------------------------------


def test_the_page_and_its_state_are_served(h):
    h.client("a", register=True).post("build", "hello human")
    server = make_server(viewer(h), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as page:
            html = page.read().decode()
        with urllib.request.urlopen(base + "/api/state") as state:
            payload = json.loads(state.read())
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(base + "/nope")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "<title>switchboard</title>" in html
    assert payload["messages"][0]["body"] == "hello human"
    assert missing.value.code == 404


def test_a_viewer_with_no_key_says_so_instead_of_printing_envelopes():
    """The tier gap `whoami` warns about, met from the reading side: the room
    is encrypted, this process was started without the key, and nothing raises
    — the bodies just arrive sealed. Distinguishing that from an empty message
    is what `switchboard.looks_sealed` was made public for."""
    key = generate_key()
    with hub(key=key) as h:
        h.client("a", register=True).post("build", "you cannot read this")

        view = snapshot(h.client("viewer", key=""))

        assert view["messages"][0]["body"] is None
        assert view["messages"][0]["sealed_body"] is True
        assert any("holds no key" in n for n in view["notes"])


def test_a_genuinely_empty_message_is_not_mistaken_for_a_sealed_one(h):
    """`None` is a legal body. In an unencrypted room it means empty, not
    "you are missing a key" — the two must not render the same."""
    h.client("a", register=True).post("build", None)

    view = snapshot(viewer(h))

    assert view["messages"][0]["body"] is None
    assert view["messages"][0]["sealed_body"] is False
    assert view["notes"] == []


# --- the SDK surface the example needed -------------------------------------
#
# Each of these exists because writing the application above hit a wall. They
# are pinned here, next to the app that needs them, so a later cleanup that
# un-exports one fails against the use case rather than against a list.


def test_a_reader_takes_the_identifiers_the_hub_gave_it():
    """`channels()` returns hub-form identifiers — blinded tokens in an
    encrypted room, which no application can turn back into names. Passing one
    to `history()` would blind it a second time and quietly match nothing, so
    reading a room you cannot name is its own method."""
    key = generate_key()
    with hub(key=key) as h:
        a = h.client("a", register=True)
        a.post("deploys", "shipping")
        a.post("build", "green")
        reader = h.client("reader")

        tokens = [c["channel"] for c in reader.channels()]
        messages = reader.read_channels(tokens)

        assert "deploys" not in tokens
        # Every channel, one request, in hub order.
        assert [m["body"] for m in messages] == ["shipping", "green"]
        # The plaintext name rides back inside the body it could not have been
        # derived from — and the identifier it arrived under is kept, so a
        # reader can still tell which channel answered.
        assert {m["channel"] for m in messages} == {"deploys", "build"}
        assert {m["hub_channel"] for m in messages} == set(tokens)


def test_a_reader_is_shown_the_newest_messages_not_the_first():
    """The hub reads forward — `since=N` answers with the messages *after* N —
    so a single read from zero is the opening `limit` messages of the room,
    for as long as the room lives. A viewer polling that re-renders the same
    stale window forever, which reads as a run that has stalled. Reading a
    room means reading the end of it."""
    with hub() as h:
        a = h.client("a", register=True)
        for i in range(120):
            a.post("build", f"line {i}")
        reader = h.client("reader")

        tail = reader.read_channels([c["channel"] for c in reader.channels()], limit=50)

        assert [m["body"] for m in tail] == [f"line {i}" for i in range(70, 120)]


def test_a_quiet_channel_is_not_stepped_over_on_the_way_to_the_tail():
    """One `since` covers every channel in the request, so paging to the tail
    of the busy one must not carry the cursor past the next message in a
    quiet one. That loss would be silent: a channel that simply renders
    empty."""
    with hub() as h:
        a = h.client("a", register=True)
        a.post("asides", "first aside")
        for i in range(120):
            a.post("build", f"line {i}")
        a.post("asides", "last aside")
        reader = h.client("reader")

        tail = reader.read_channels([c["channel"] for c in reader.channels()], limit=50)

        assert [m["body"] for m in tail if m["channel"] == "asides"] == \
               ["first aside", "last aside"]
        assert len([m for m in tail if m["channel"] == "build"]) == 50


def test_paging_to_the_tail_still_leaves_every_cursor_where_it_was():
    """Several requests instead of one is several chances to disturb the room,
    and every one of them is still a peek."""
    with hub() as h:
        subscriber = h.client("sub", register=True, channels=["build"])
        a = h.client("a", register=True)
        for i in range(120):
            a.post("build", f"line {i}")
        reader = h.client("reader")

        reader.read_channels([c["channel"] for c in reader.channels()], limit=10)

        assert len(subscriber.inbox(limit=200)) == 120


async def test_the_async_client_reaches_the_same_tail():
    """The two clients must not drift here either — an async dashboard shows
    the same window a sync one does."""
    with hub() as h:
        a = h.client("a", register=True)
        for i in range(120):
            a.post("build", f"line {i}")
        reader = h.async_client("reader")

        tokens = [c["channel"] for c in await reader.channels()]
        tail = await reader.read_channels(tokens, limit=50)

        assert [m["body"] for m in tail] == [f"line {i}" for i in range(70, 120)]


def test_reading_a_room_leaves_every_cursor_where_it_was():
    """The property the whole method exists for: an observer must not be able
    to make a participant's next `inbox` come back empty."""
    with hub() as h:
        subscriber = h.client("sub", register=True, channels=["build"])
        h.client("a", register=True).post("build", "for the subscriber")
        reader = h.client("reader")

        reader.read_channels([c["channel"] for c in reader.channels()])

        assert [m["body"] for m in subscriber.inbox()] == ["for the subscriber"]


def test_a_client_says_whether_it_encrypts():
    """An encrypted room and a plaintext one look identical in a response, so
    an application cannot infer this — and it decides whether an identifier it
    displays is a name or a blinded token."""
    key = generate_key()
    with hub(key=key) as sealed, hub() as plain:
        assert sealed.client("a").encrypted is True
        assert plain.client("a").encrypted is False


async def test_the_async_client_reads_a_room_the_same_way():
    """The two clients must not drift: an async application reads a room it is
    not part of for the same reasons and meets the same wall in the same
    place."""
    key = generate_key()
    with hub(key=key) as h:
        h.client("a", register=True).post("deploys", "shipping")
        reader = h.async_client("reader")

        tokens = [c["channel"] for c in await reader.channels()]

        assert [m["body"] for m in await reader.read_channels(tokens)] == ["shipping"]


def test_standing_in_a_set_up_repo_is_the_whole_configuration_step(tmp_path, monkeypatch):
    """The wall a human hits rather than a program: `init` wrote the hub, the
    room and the key into the checkout, and the viewer — a plain SDK client —
    could see none of it. Four exports to look at your own agents is four
    chances to point a decrypting page at the wrong room, which is exactly
    what happened while writing the docs for this."""
    from switchboard.config import ClientConfig

    for name in ("SWITCHBOARD_URL", "SWITCHBOARD_WORKSPACE", "SWITCHBOARD_KEY",
                 "SWITCHBOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"switchboard": {"command": "switchboard-mcp", "env": {
            "SWITCHBOARD_URL": "http://127.0.0.1:8787",
            "SWITCHBOARD_WORKSPACE": "w_theirs",
        }}}
    }))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"SWITCHBOARD_KEY": generate_key()}}))
    (tmp_path / ".env").write_text("SWITCHBOARD_TOKEN=dev-token\n")
    monkeypatch.chdir(tmp_path)

    config = ClientConfig.from_repo(include_secrets=True)

    assert (config.url, config.workspace) == ("http://127.0.0.1:8787", "w_theirs")
    assert config.token == "dev-token"
    assert Client(config).encrypted is True


# --- several rooms at once ---------------------------------------------------


def test_the_switcher_lists_every_room_and_says_where_people_are(h):
    """The question a room switcher exists to answer is "is anyone in there",
    so the rooms you are *not* looking at still report their roster — one
    request each, rather than a full snapshot each."""
    quiet = h.client("quiet-reader", workspace="w_quiet")
    busy = h.client("busy-reader", workspace="w_busy")
    h.client("someone", workspace="w_busy", register=True)

    server = make_server([
        viewer_app.Room(label="quiet repo", client=quiet, source="mcp.json"),
        viewer_app.Room(label="busy repo", client=busy, source="rooms"),
    ], host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/api/state") as r:
            first = json.loads(r.read())
        second_id = first["rooms"][1]["id"]
        with urllib.request.urlopen(
            base + "/api/state?room=" + urllib.parse.quote(second_id)
        ) as r:
            second = json.loads(r.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert [r["label"] for r in first["rooms"]] == ["quiet repo", "busy repo"]
    # The first room is the one shown until asked otherwise...
    assert [r["selected"] for r in first["rooms"]] == [True, False]
    assert first["hub"]["workspace"] == "w_quiet"
    # ...and the others still say whether anyone is in them.
    assert [r["awake"] for r in first["rooms"]] == [0, 1]
    # Asking for one by id switches which room is read in full.
    assert second["hub"]["workspace"] == "w_busy"
    assert [r["selected"] for r in second["rooms"]] == [False, True]


def test_a_room_that_cannot_be_reached_does_not_take_the_others_down(h):
    """One dead hub among several is the normal state of a laptop with three
    checkouts on it — a viewer that fails whole is useless there."""
    from switchboard.client import Client
    from switchboard.config import ClientConfig

    dead = Client(ClientConfig(url="http://127.0.0.1:9", url_source="explicit",
                               workspace="w_gone"), agent_id="reader")
    live = h.client("live-reader")

    server = make_server([
        viewer_app.Room(label="live", client=live),
        viewer_app.Room(label="gone", client=dead),
    ], host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/api/state"
        ) as r:
            payload = json.loads(r.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        dead.close()

    assert payload["hub"]["reachable"] is True
    assert payload["rooms"][1]["error"] is not None
    assert payload["rooms"][1]["awake"] is None


def test_a_checkout_that_declares_several_rooms_shows_all_of_them(tmp_path, monkeypatch):
    """`select()` refuses ambiguity because *acting* in two rooms by accident
    is worse than being asked which. Reading is the opposite: showing someone
    every room they hold a key for is the point."""
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    (tmp_path / ".switchboard").mkdir()
    (tmp_path / ".switchboard" / "rooms.json").write_text(json.dumps({"rooms": [
        {"name": "parser", "key_id": "default", "workspace_token": "tok-parser"},
        {"name": "ops", "key_id": "ops", "workspace_token": "tok-ops"},
        {"name": "locked", "key_id": "nobody", "workspace_token": "tok-locked"},
    ]}))
    monkeypatch.setenv("SWITCHBOARD_KEY", generate_key())
    monkeypatch.setenv("SWITCHBOARD_KEY_OPS", generate_key())

    rooms = viewer_app.discover([str(tmp_path)], [])
    try:
        # The room whose key this machine does not hold is not shown, because
        # it could not be read — that is `joinable`, not a filter of our own.
        assert [r.label for r in rooms] == ["parser", "ops"]
        assert len({r.client.workspace for r in rooms}) == 2
        assert all(r.client.encrypted for r in rooms)
    finally:
        for room in rooms:
            room.client.close()


def test_one_room_reached_from_two_clones_is_shown_once(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    for name in ("clone-a", "clone-b"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"switchboard": {"command": "switchboard-mcp", "env": {
                "SWITCHBOARD_URL": "https://hub.example.com",
                "SWITCHBOARD_WORKSPACE": "w_same",
            }}}
        }))

    rooms = viewer_app.discover([str(tmp_path / "clone-a"), str(tmp_path / "clone-b")], [])
    try:
        assert [r.label for r in rooms] == ["clone-a"]
    finally:
        for room in rooms:
            room.client.close()


def test_scanning_finds_the_checkouts_that_have_been_set_up(tmp_path, monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    for name, workspace in (("alpha", "w_alpha"), ("nested/beta", "w_beta")):
        directory = tmp_path / name
        directory.mkdir(parents=True)
        (directory / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"switchboard": {"command": "switchboard-mcp", "env": {
                "SWITCHBOARD_URL": "https://hub.example.com",
                "SWITCHBOARD_WORKSPACE": workspace,
            }}}
        }))
    (tmp_path / "not-a-checkout").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / ".mcp.json").write_text("{}")

    rooms = viewer_app.discover([], [str(tmp_path)])
    try:
        assert sorted(r.label for r in rooms) == ["alpha", "beta"]
    finally:
        for room in rooms:
            room.client.close()


# --- invites ----------------------------------------------------------------


def test_a_probe_the_key_can_open_says_nothing_at_all(h):
    """Silence is the good outcome. These notes are drawn as warnings, and a
    viewer that announced every success would be shouting the normal case."""
    from switchboard.invite import PROBE_SENTINEL

    key = generate_key()
    inviter = h.client("inviter", key=key, register=True)
    inviter.board_set("join/probe/abcd", PROBE_SENTINEL)

    view = snapshot(viewer(h, key=key), probe="join/probe/abcd")

    assert not any("proof-of-room" in n or "WRONG ROOM" in n for n in view["notes"])
    # Not nothing at all, though. Silence was right only while there was
    # nowhere to put a success; the page has a place for one beside the room
    # name now, and the notes strip is still only for warnings.
    assert view["hub"]["verified"] is True


def test_the_probe_is_machinery_and_is_kept_off_the_blackboard(h):
    """An invite's proof-of-room is an ordinary board entry — that is the
    elegant part of its design — but it is this viewer's own machinery, not
    state an agent published, and its value is a sentinel that means nothing
    to a reader. The room's own entries are untouched."""
    from switchboard.invite import PROBE_SENTINEL

    key = generate_key()
    inviter = h.client("inviter", key=key, register=True)
    inviter.board_set("join/probe/abcd", PROBE_SENTINEL)
    inviter.board_set("handoff/lexer", {"next": "escapes"})

    view = snapshot(viewer(h, key=key), probe="join/probe/abcd")

    assert [e["key"] for e in view["board"]] == ["handoff/lexer"]


def test_a_room_nobody_asked_us_to_verify_says_neither_yes_nor_no(h):
    """Without an invite there is nothing to check, and "not verified" would
    read as a failure rather than as an absence of any claim."""
    key = generate_key()
    h.client("inviter", key=key, register=True).board_set("plan", {"next": "0143"})

    assert snapshot(viewer(h, key=key))["hub"]["verified"] is None


def test_a_probe_this_key_cannot_open_is_the_forty_minute_failure_caught(h):
    """Same hub, same workspace, different key: both parties on one roster,
    able to exchange nothing. Only opening what the other side sealed can tell
    you that, which is exactly what the probe is."""
    from switchboard.invite import PROBE_SENTINEL

    inviter = h.client("inviter", key=generate_key(), register=True)
    inviter.board_set("join/probe/abcd", PROBE_SENTINEL)

    view = snapshot(viewer(h, key=generate_key()), probe="join/probe/abcd")

    assert any("WRONG ROOM" in n for n in view["notes"])


def test_a_probe_that_is_simply_gone_says_that_instead(h):
    """An expired probe is not a wrong key, and telling someone their key is
    wrong when it isn't sends them to re-key a room that was fine."""
    key = generate_key()
    h.client("inviter", key=key, register=True).board_set("plan", {"next": "0143"})

    view = snapshot(viewer(h, key=key), probe="join/probe/vanished")

    notes = " ".join(view["notes"])
    assert "not on the blackboard" in notes
    assert "WRONG ROOM" not in notes


def test_an_invite_is_the_whole_configuration(monkeypatch):
    """`--invite` outranks the checkout, because it names a room somebody else
    is already in — the case where reading the local repo shows the wrong one
    with no sign that it did."""
    from switchboard.invite import Invite

    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    blob = Invite(url="https://hub.example.com", workspace="w_theirs",
                  token="tok", key=generate_key(), note="parser room",
                  probe="join/probe/abcd").encode()

    served: dict[str, object] = {}
    monkeypatch.setattr(viewer_app, "_run",
                        lambda args, rooms: served.setdefault("rooms", rooms) and 0)

    assert viewer_app.main(["--invite", blob]) in (0, None)

    rooms = served["rooms"]
    assert len(rooms) == 1
    room = rooms[0]
    try:
        assert room.label == "parser room"
        assert room.source == "invite"
        assert room.probe == "join/probe/abcd"
        assert room.client.config.url == "https://hub.example.com"
        assert room.client.workspace == "w_theirs"
        assert room.client.config.token == "tok"
    finally:
        room.client.close()


def test_a_mangled_invite_stops_before_anything_is_served(capsys, monkeypatch):
    monkeypatch.setattr(viewer_app, "_run",
                        lambda args, rooms: pytest.fail("should not have served"))

    assert viewer_app.main(["--invite", "swb1_nonsense"]) == 1
    assert "corrupt or truncated" in capsys.readouterr().err
