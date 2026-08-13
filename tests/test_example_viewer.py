"""`examples/viewer.py`: what a human is shown, and what it promises not to do.

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
import threading
import urllib.request
from pathlib import Path

import pytest

from switchboard.client import Client
from switchboard.config import ClientConfig
from switchboard.crypto import generate_key
from switchboard.testing import hub

#: Loaded from `examples/` rather than imported from the package, because that
#: is where it lives and how a reader of the repo would run it.
_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "viewer.py"
_spec = importlib.util.spec_from_file_location("example_viewer", _EXAMPLE)
viewer_app = importlib.util.module_from_spec(_spec)
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

        assert any("different workspace key" in n for n in view["notes"])
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
