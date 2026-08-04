"""Store-level tests: expiry, lease exclusivity, cursors, blackboard revisions."""

from __future__ import annotations

import threading

import pytest

from switchboard.store import LeaseConflict, NotLeaseHolder, Store, StoreError

WS = "test-ws"


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


# --- presence ---------------------------------------------------------------


def test_register_is_idempotent_and_updates_fields(store):
    first = store.register_agent(workspace=WS, agent_id="a1", name="A", ttl=60)
    second = store.register_agent(
        workspace=WS, agent_id="a1", name="A-renamed", branch="feat/x", ttl=60
    )
    assert first.id == second.id == "a1"
    assert second.name == "A-renamed"
    assert second.branch == "feat/x"
    assert len(store.list_agents(workspace=WS)) == 1


def test_agent_expires_without_heartbeat(store):
    store.register_agent(workspace=WS, agent_id="a1", name="A", ttl=10, now=1000.0)
    assert len(store.list_agents(workspace=WS, now=1005.0)) == 1
    assert len(store.list_agents(workspace=WS, now=1011.0)) == 0


def test_heartbeat_extends_presence(store):
    store.register_agent(workspace=WS, agent_id="a1", name="A", ttl=10, now=1000.0)
    agent, _ = store.heartbeat(workspace=WS, agent_id="a1", ttl=10, now=1008.0)
    assert agent is not None
    assert len(store.list_agents(workspace=WS, now=1015.0)) == 1


def test_heartbeat_on_unknown_agent_returns_none(store):
    agent, leases = store.heartbeat(workspace=WS, agent_id="ghost", ttl=10)
    assert agent is None and leases == []


def test_workspaces_are_isolated(store):
    store.register_agent(workspace="one", agent_id="a1", name="A", ttl=60)
    store.register_agent(workspace="two", agent_id="a1", name="A", ttl=60)
    store.acquire_lease(workspace="one", resource="r", holder="a1", ttl=60)
    # Same resource name in a different workspace is a different lease.
    store.acquire_lease(workspace="two", resource="r", holder="a1", ttl=60)
    assert len(store.list_leases(workspace="one")) == 1
    assert len(store.list_leases(workspace="two")) == 1


# --- leases -----------------------------------------------------------------


def test_lease_is_exclusive(store):
    store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=60)
    with pytest.raises(LeaseConflict) as exc:
        store.acquire_lease(workspace=WS, resource="r", holder="a2", ttl=60)
    assert exc.value.holder == "a1"


def test_reacquiring_your_own_lease_is_a_renewal(store):
    first = store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=60, now=1000.0)
    again = store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=60, now=1030.0)
    assert again.expires_at > first.expires_at
    assert again.fence == first.fence  # same lease, not a new one


def test_expired_lease_is_free_for_the_taking(store):
    store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=10, now=1000.0)
    lease = store.acquire_lease(workspace=WS, resource="r", holder="a2", ttl=10, now=1020.0)
    assert lease.holder == "a2"


def test_fence_increases_when_a_lease_changes_hands(store):
    first = store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=10, now=1000.0)
    second = store.acquire_lease(workspace=WS, resource="r", holder="a2", ttl=10, now=1020.0)
    assert second.fence > first.fence


def test_expired_lease_is_not_listed(store):
    store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=10, now=1000.0)
    assert store.list_leases(workspace=WS, now=1005.0)
    assert not store.list_leases(workspace=WS, now=1020.0)
    assert store.get_lease(workspace=WS, resource="r", now=1020.0) is None


def test_cannot_release_another_agents_lease(store):
    store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=60)
    with pytest.raises(NotLeaseHolder):
        store.release_lease(workspace=WS, resource="r", holder="a2")
    assert store.release_lease(workspace=WS, resource="r", holder="a2", force=True)


def test_release_of_absent_lease_is_false_not_an_error(store):
    assert store.release_lease(workspace=WS, resource="nope", holder="a1") is False


def test_renew_requires_holding_the_lease(store):
    store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=60)
    with pytest.raises(NotLeaseHolder):
        store.renew_lease(workspace=WS, resource="r", holder="a2", ttl=60)


def test_renew_of_expired_lease_fails(store):
    store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=10, now=1000.0)
    with pytest.raises(NotLeaseHolder):
        store.renew_lease(workspace=WS, resource="r", holder="a1", ttl=10, now=1020.0)


def test_heartbeat_renews_held_leases_preserving_their_duration(store):
    store.register_agent(workspace=WS, agent_id="a1", name="A", ttl=60, now=1000.0)
    store.acquire_lease(workspace=WS, resource="short", holder="a1", ttl=30, now=1000.0)
    store.acquire_lease(workspace=WS, resource="long", holder="a1", ttl=300, now=1000.0)
    _, renewed = store.heartbeat(workspace=WS, agent_id="a1", ttl=60, now=1010.0)
    by_resource = {le.resource: le for le in renewed}
    assert by_resource["short"].expires_at == pytest.approx(1040.0)
    assert by_resource["long"].expires_at == pytest.approx(1310.0)


def test_heartbeat_does_not_renew_other_agents_leases(store):
    store.register_agent(workspace=WS, agent_id="a1", name="A", ttl=60, now=1000.0)
    store.acquire_lease(workspace=WS, resource="r", holder="a2", ttl=30, now=1000.0)
    _, renewed = store.heartbeat(workspace=WS, agent_id="a1", ttl=60, now=1010.0)
    assert renewed == []
    still_held = store.get_lease(workspace=WS, resource="r", now=1010.0)
    assert still_held is not None
    assert still_held.expires_at == pytest.approx(1030.0)


def test_deregistering_releases_leases(store):
    store.register_agent(workspace=WS, agent_id="a1", name="A", ttl=60)
    store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=60)
    store.deregister_agent(workspace=WS, agent_id="a1")
    assert store.get_lease(workspace=WS, resource="r") is None


def test_concurrent_acquire_yields_exactly_one_winner(store):
    """The property the whole lease concept rests on."""
    winners: list[str] = []
    barrier = threading.Barrier(12)

    def attempt(n: int) -> None:
        barrier.wait()
        try:
            store.acquire_lease(workspace=WS, resource="hot", holder=f"a{n}", ttl=60)
            winners.append(f"a{n}")
        except LeaseConflict:
            pass

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


# --- messages ---------------------------------------------------------------


def test_cursor_delivers_each_message_once(store):
    store.post(workspace=WS, channel="c", sender="a2", body="one", ttl=60)
    store.post(workspace=WS, channel="c", sender="a2", body="two", ttl=60)
    first = store.read(workspace=WS, channels=["c"], agent_id="a1")
    assert [m.body for m in first] == ["one", "two"]
    assert store.read(workspace=WS, channels=["c"], agent_id="a1") == []
    store.post(workspace=WS, channel="c", sender="a2", body="three", ttl=60)
    assert [m.body for m in store.read(workspace=WS, channels=["c"], agent_id="a1")] == ["three"]


def test_peek_does_not_advance_the_cursor(store):
    store.post(workspace=WS, channel="c", sender="a2", body="x", ttl=60)
    store.read(workspace=WS, channels=["c"], agent_id="a1", commit_cursor=False)
    assert [m.body for m in store.read(workspace=WS, channels=["c"], agent_id="a1")] == ["x"]


def test_sender_does_not_receive_its_own_message(store):
    store.post(workspace=WS, channel="c", sender="a1", body="mine", ttl=60)
    assert store.read(workspace=WS, channels=["c"], agent_id="a1") == []
    assert len(store.read(workspace=WS, channels=["c"], agent_id="a1", include_own=True)) == 0


def test_own_message_does_not_block_later_reads(store):
    """The cursor must skip past filtered messages, not stall on them."""
    store.post(workspace=WS, channel="c", sender="a1", body="mine", ttl=60)
    store.post(workspace=WS, channel="c", sender="a2", body="theirs", ttl=60)
    assert [m.body for m in store.read(workspace=WS, channels=["c"], agent_id="a1")] == ["theirs"]
    assert store.read(workspace=WS, channels=["c"], agent_id="a1") == []


def test_limit_does_not_skip_messages(store):
    for i in range(5):
        store.post(workspace=WS, channel="c", sender="a2", body=i, ttl=60)
    first = store.read(workspace=WS, channels=["c"], agent_id="a1", limit=2)
    second = store.read(workspace=WS, channels=["c"], agent_id="a1", limit=2)
    third = store.read(workspace=WS, channels=["c"], agent_id="a1", limit=2)
    assert [m.body for m in first + second + third] == [0, 1, 2, 3, 4]


def test_expired_messages_are_not_delivered(store):
    store.post(workspace=WS, channel="c", sender="a2", body="stale", ttl=10, now=1000.0)
    assert store.read(workspace=WS, channels=["c"], agent_id="a1", now=1020.0) == []


def test_direct_messages_are_just_a_channel(store):
    """A DM to a1 is a post on channel '@a1' — no separate concept."""
    store.post(workspace=WS, channel="@a1", sender="a2", body="psst", ttl=60)
    assert [m.body for m in store.read(workspace=WS, channels=["@a1"], agent_id="a1")] == ["psst"]
    # Exclusivity is a property of which channels an agent reads, and the
    # server only ever resolves '@<id>' for the agent whose id it is.
    assert store.read(workspace=WS, channels=["@a3"], agent_id="a3") == []


def test_reading_multiple_channels_is_ordered_by_sequence(store):
    store.post(workspace=WS, channel="x", sender="a2", body=1, ttl=60)
    store.post(workspace=WS, channel="y", sender="a2", body=2, ttl=60)
    store.post(workspace=WS, channel="x", sender="a2", body=3, ttl=60)
    got = store.read(workspace=WS, channels=["x", "y"], agent_id="a1")
    assert [m.body for m in got] == [1, 2, 3]


def test_history_is_independent_of_cursor(store):
    store.post(workspace=WS, channel="c", sender="a2", body="a", ttl=60)
    store.read(workspace=WS, channels=["c"], agent_id="a1")
    assert [m.body for m in store.peek(workspace=WS, channel="c")] == ["a"]


def test_structured_bodies_round_trip(store):
    payload = {"files": ["a.py", "b.py"], "done": True, "n": 3}
    store.post(workspace=WS, channel="c", sender="a2", body=payload, ttl=60)
    got = store.read(workspace=WS, channels=["c"], agent_id="a1")
    assert got[0].body == payload


# --- blackboard -------------------------------------------------------------


def test_board_revision_increments_on_write(store):
    first = store.board_set(workspace=WS, key="k", value=1, updated_by="a1", ttl=60)
    second = store.board_set(workspace=WS, key="k", value=2, updated_by="a1", ttl=60)
    assert (first.revision, second.revision) == (1, 2)
    assert store.board_get(workspace=WS, key="k").value == 2


def test_board_optimistic_concurrency(store):
    store.board_set(workspace=WS, key="k", value=1, updated_by="a1", ttl=60)
    with pytest.raises(StoreError):
        store.board_set(workspace=WS, key="k", value=2, updated_by="a2", ttl=60, if_revision=99)
    store.board_set(workspace=WS, key="k", value=3, updated_by="a2", ttl=60, if_revision=1)
    assert store.board_get(workspace=WS, key="k").value == 3


def test_board_if_revision_zero_means_only_if_absent(store):
    store.board_set(workspace=WS, key="new", value=1, updated_by="a1", ttl=60, if_revision=0)
    with pytest.raises(StoreError):
        store.board_set(workspace=WS, key="new", value=2, updated_by="a2", ttl=60, if_revision=0)


def test_board_entries_expire(store):
    store.board_set(workspace=WS, key="k", value=1, updated_by="a1", ttl=10, now=1000.0)
    assert store.board_get(workspace=WS, key="k", now=1005.0) is not None
    assert store.board_get(workspace=WS, key="k", now=1020.0) is None


def test_board_prefix_filter_escapes_wildcards(store):
    store.board_set(workspace=WS, key="plan/a", value=1, updated_by="a1", ttl=60)
    store.board_set(workspace=WS, key="planXa", value=2, updated_by="a1", ttl=60)
    keys = [e.key for e in store.board_list(workspace=WS, prefix="plan/")]
    assert keys == ["plan/a"]
    # An underscore in a prefix must not act as a single-character wildcard.
    store.board_set(workspace=WS, key="a_b", value=1, updated_by="a1", ttl=60)
    store.board_set(workspace=WS, key="axb", value=2, updated_by="a1", ttl=60)
    assert [e.key for e in store.board_list(workspace=WS, prefix="a_")] == ["a_b"]


# --- maintenance ------------------------------------------------------------


def test_sweep_deletes_only_expired_rows(store):
    store.register_agent(workspace=WS, agent_id="live", name="L", ttl=1000, now=1000.0)
    store.register_agent(workspace=WS, agent_id="dead", name="D", ttl=10, now=1000.0)
    store.post(workspace=WS, channel="c", sender="x", body="old", ttl=10, now=1000.0)
    store.board_set(workspace=WS, key="k", value=1, updated_by="x", ttl=10, now=1000.0)
    counts = store.sweep(now=1100.0)
    assert counts["agents"] == 1
    assert counts["messages"] == 1
    assert counts["board"] == 1
    assert [a.id for a in store.list_agents(workspace=WS, now=1100.0)] == ["live"]


def test_sweep_is_not_required_for_correct_reads(store):
    """Expiry is enforced at read time; the sweeper only reclaims disk."""
    store.acquire_lease(workspace=WS, resource="r", holder="a1", ttl=10, now=1000.0)
    assert store.get_lease(workspace=WS, resource="r", now=1020.0) is None
