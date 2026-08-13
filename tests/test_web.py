"""The viewer: what a human is shown, and what it promises not to do.

Two properties carry most of the weight here. The page must not disturb the
room it is displaying — a viewer that drained an inbox would make an agent
miss a message, and the human watching would never know. And it must be
honest under encryption: name what it can name, mark what it cannot as sealed,
and keep going when a channel belongs to someone holding another key.
"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from switchboard.client import Client
from switchboard.config import ClientConfig
from switchboard.crypto import generate_key
from switchboard.testing import hub
from switchboard.web import make_server, snapshot


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
    monkeypatch.setattr("switchboard.web.MAX_CHANNELS", 2)
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


def test_a_channel_under_another_key_is_reported_not_fatal():
    key = generate_key()
    with hub(key=key) as h:
        h.client("mine", register=True).post("build", "readable")
        h.client("theirs", key=generate_key(), register=True).post("build", "not")

        view = snapshot(viewer(h))

        assert [m["body"] for m in view["messages"]] == ["readable"]
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


# --- the command ------------------------------------------------------------


def test_the_command_serves_the_room_the_rest_of_the_cli_would_talk_to(h, monkeypatch):
    """`switchboard web` must resolve its hub exactly like `switchboard say`
    does, or it shows a different room than the one the agents are in."""
    import switchboard.cli as cli

    monkeypatch.setattr(cli, "Client", h.client_class())
    monkeypatch.setenv("SWITCHBOARD_URL", h.url)
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", h.workspace)
    served = {}

    def fake_serve(hub, **kwargs):
        served["workspace"] = hub.workspace
        served.update(kwargs)
        kwargs["announce"]("http://127.0.0.1:8799")

    monkeypatch.setattr(cli.web, "serve", fake_serve)

    assert cli.main(["web", "--port", "9100", "--limit", "7"]) == 0

    assert served["workspace"] == h.workspace
    assert (served["host"], served["port"], served["limit"]) == ("127.0.0.1", 9100, 7)


def test_binding_off_loopback_says_what_that_publishes(h, monkeypatch, capsys):
    import switchboard.cli as cli

    monkeypatch.setattr(cli, "Client", h.client_class())
    monkeypatch.setenv("SWITCHBOARD_URL", h.url)
    monkeypatch.setattr(cli.web, "serve", lambda hub, **kwargs: None)

    cli.main(["web", "--host", "0.0.0.0"])
    loud = capsys.readouterr().err
    cli.main(["web", "--host", "127.0.0.1"])
    quiet = capsys.readouterr().err

    assert "no authentication" in loud
    assert "no authentication" not in quiet


def test_a_port_already_in_use_is_not_reported_as_an_unreachable_hub(h, monkeypatch, capsys):
    """Both surface as OSError, and `main` blames the hub for all of them."""
    import switchboard.cli as cli

    monkeypatch.setattr(cli, "Client", h.client_class())
    monkeypatch.setenv("SWITCHBOARD_URL", h.url)

    def refuse(hub, **kwargs):
        raise OSError("[Errno 98] Address already in use")

    monkeypatch.setattr(cli.web, "serve", refuse)

    assert cli.main(["web", "--port", "9100"]) == 1
    assert "cannot serve on 127.0.0.1:9100" in capsys.readouterr().err


def test_a_viewer_with_no_key_says_so_instead_of_printing_envelopes():
    """The tier gap `whoami` warns about, met from the reading side: the room
    is encrypted, this process was started without the key, and nothing raises
    — the bodies just arrive sealed."""
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
