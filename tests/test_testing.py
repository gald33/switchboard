"""The test harness is shipped, so it is tested.

`switchboard.testing` is public API — an agent author writing tests against
the SDK gets their hub from here. That makes its own bugs other people's
failing suites, and the worst kind: a harness that quietly does not do what it
claims (a clock that does not move, a client that reaches a different hub than
the one you are asserting against) turns a green suite into a lie.

So these tests are mostly about the harness's own promises rather than about
the hub: that the clock actually governs expiry, that two clients of one hub
really are two agents, that an encrypted hub really does hold ciphertext.
"""

from __future__ import annotations

import json

import pytest

from switchboard.client import LeaseHeld, SwitchboardError
from switchboard.crypto import generate_key
from switchboard.testing import EPOCH, Clock, hub


@pytest.fixture
def h():
    with hub() as handle:
        yield handle


# --- the clock --------------------------------------------------------------


def test_a_hub_starts_at_a_fixed_epoch_not_now():
    """Otherwise a printed timestamp differs on every run and every machine."""
    with hub() as h:
        assert h.now == EPOCH


def test_the_clock_only_moves_forward():
    clock = Clock()
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_a_lease_expires_because_the_clock_moved(h):
    """The reason this module exists: fifteen minutes of TTL, no waiting."""
    a = h.client("a")
    lease = a.acquire("db/migrations", ttl=900)
    assert lease["holder"] == "a"

    b = h.client("b")
    with pytest.raises(LeaseHeld):
        b.acquire("db/migrations")

    h.advance(901)
    assert b.acquire("db/migrations")["holder"] == "b"


def test_presence_goes_stale_on_hub_time(h):
    a = h.client("a", register=True)
    assert [x["agent_id"] for x in a.agents()] == ["a"]

    h.advance(60 * 60)
    assert a.agents() == []


def test_a_board_entry_expires(h):
    a = h.client("a")
    a.board_set("handoff/notes", {"next": "run the backfill"}, ttl=3600)
    assert a.board_get("handoff/notes")["next"] == "run the backfill"

    h.advance(3601)
    assert a.board_get("handoff/notes", default=None) is None


def test_sweeping_deletes_what_expiry_only_hides(h):
    """Reads filter on expiry, so `advance` alone is enough to make something
    invisible. `sweep` is for when the deletion itself is the subject."""
    h.client("a").board_set("k", 1, ttl=10)
    h.advance(11)

    assert h.board() == []  # gone as far as anyone can tell
    assert h.sweep()["board"] == 1  # but the row was still there


def test_two_hubs_do_not_share_a_clock():
    """Per-app injection rather than a patched module — otherwise advancing one
    hub in a test would silently expire another's state."""
    with hub() as one, hub() as two:
        one.advance(5000)
        assert two.now == EPOCH
        assert one.now != two.now


# --- clients ----------------------------------------------------------------


def test_each_client_is_a_separate_agent(h):
    a = h.client("a", register=True)
    b = h.client("b", register=True)
    assert sorted(x["agent_id"] for x in a.agents()) == ["a", "b"]
    assert a.agent_id != b.agent_id


def test_a_name_is_the_agent_id_so_assertions_are_readable(h):
    a = h.client("alice")
    assert a.acquire("r")["holder"] == "alice"


def test_clients_of_one_hub_can_talk_to_each_other(h):
    a = h.client("a", register=True)
    b = h.client("b", register=True)

    a.send("b", "the migration is yours")
    assert [m["body"] for m in b.inbox()] == ["the migration is yours"]


def test_closing_the_hub_does_not_leave_clients_open(h):
    a = h.client("a")
    h.close()
    with pytest.raises(RuntimeError, match="closed"):
        a.health()


def test_a_client_does_not_close_a_transport_it_was_given(h):
    """`Client.close()` on an injected transport must be a no-op — the CLI
    closes its client on every command, and a shared transport would then be
    dead for the rest of the test."""
    a = h.client("a")
    a.close()
    assert a.health()["ok"] is True


async def test_the_async_client_reaches_the_same_hub(h):
    sync = h.client("sync", register=True)
    async with h.async_client("async") as other:
        await other.register(name="async")
        roster = await other.agents()
    assert sorted(x["agent_id"] for x in roster) == ["async", "sync"]
    assert sorted(x["agent_id"] for x in sync.agents()) == ["async", "sync"]


# --- reading state ----------------------------------------------------------


def test_the_store_is_the_same_one_the_app_writes_to(h):
    """An in-memory database opened twice is two empty hubs, and every
    assertion against the wrong one passes vacuously."""
    h.client("a").acquire("shared/thing")
    assert [le.resource for le in h.leases()] == ["shared/thing"]


def test_reading_messages_as_an_oracle_does_not_move_a_cursor(h):
    h.client("b", register=True).post("build", "starting")

    assert [m.body for m in h.messages("build")] == ["starting"]
    # The observation did not consume it: a reader still finds it unread.
    reader = h.client("a", register=True, channels=["build"])
    assert [m["body"] for m in reader.inbox(channels=["build"])] == ["starting"]


def test_raw_http_exposes_the_wire_format(h):
    body = h.http.get("/health").json()
    assert body["ok"] is True and "version" in body


# --- perimeter and encryption ------------------------------------------------


def test_a_token_admits_clients_and_refuses_strangers():
    with hub(token="s3cret") as h:
        assert h.client("a").health()["ok"] is True
        assert h.raw(token=None).get("/agents").status_code == 401
        assert h.raw(token="wrong").get("/agents").status_code == 401


def test_an_encrypted_hub_holds_ciphertext_its_clients_can_read(tmp_path):
    key = generate_key()
    secret = "the api key rotates on friday"
    with hub(key=key, db=str(tmp_path / "sealed.db")) as h:
        a = h.client("a", register=True)
        b = h.client("b", register=True)
        a.post("plans", secret)

        assert [m["body"] for m in b.inbox(channels=["plans"])] == [secret]
        # ...and the hub itself is holding something it cannot read.
        stored = json.dumps([m.body for m in h.messages(h.client("x")._blind_channel("plans"))])
        assert secret not in stored


def test_a_client_with_the_wrong_key_cannot_read(tmp_path):
    with hub(key=generate_key()) as h:
        h.client("a", register=True).post("plans", "sealed")
        stranger = h.client("z", key=generate_key(), register=True)
        assert stranger.inbox(channels=["plans"]) == []


# --- config -----------------------------------------------------------------


def test_the_hub_url_is_never_loopback(h):
    """A loopback URL trips `isolation_warning`, which would print a spurious
    "you are talking to yourself" warning through every CLI test."""
    from switchboard.config import is_loopback

    assert not is_loopback(h.url)


def test_client_config_is_enough_to_build_your_own_client(h):
    config = h.client_config(agent_id="byo")
    assert config.url == h.url
    assert config.workspace == h.workspace


def test_an_unknown_agent_still_gets_a_real_error(h):
    """The harness must not swallow or reshape hub errors."""
    with pytest.raises(SwitchboardError):
        h.client("ghost").heartbeat()
