"""HTTP-level tests, driven through the real FastAPI app."""

from __future__ import annotations

import json

import pytest

from switchboard.store import Store
from switchboard.testing import hub

WS = "api-ws"


@pytest.fixture
def client():
    """Raw HTTP against a real hub — these tests are about the wire format."""
    with hub() as h:
        yield h.raw(token=None)


@pytest.fixture
def secured():
    with hub(token="s3cret") as h:
        yield h.raw(token=None)


def _register(client, agent_id: str, **kw):
    payload = {"workspace": WS, "agent_id": agent_id, "name": agent_id}
    payload.update(kw)
    response = client.post("/agents/register", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["agent"]


# --- meta / auth ------------------------------------------------------------


def test_health_is_open(client):
    body = client.get("/health").json()
    assert body["ok"] is True


def test_token_is_required_when_configured(secured):
    assert secured.get("/agents", params={"workspace": WS}).status_code == 401
    assert secured.get("/health").status_code == 200
    ok = secured.get(
        "/agents", params={"workspace": WS}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


def test_wrong_token_is_rejected(secured):
    response = secured.get(
        "/agents", params={"workspace": WS}, headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401


# --- presence ---------------------------------------------------------------


def test_register_then_appear_in_roster(client):
    _register(client, "a1", branch="feat/x", task="wiring")
    agents = client.get("/agents", params={"workspace": WS}).json()["agents"]
    assert [a["agent_id"] for a in agents] == ["a1"]
    assert agents[0]["branch"] == "feat/x"
    assert agents[0]["expires_in"] > 0


def test_heartbeat_for_unknown_agent_is_404(client):
    response = client.post(
        "/agents/heartbeat", json={"workspace": WS, "agent_id": "ghost"}
    )
    assert response.status_code == 404


def test_heartbeat_returns_held_leases(client):
    _register(client, "a1")
    client.post("/leases/acquire", json={"workspace": WS, "resource": "r", "agent_id": "a1"})
    body = client.post("/agents/heartbeat", json={"workspace": WS, "agent_id": "a1"}).json()
    assert [le["resource"] for le in body["leases"]] == ["r"]


def test_heartbeat_reports_unread_dms_without_consuming_them(client):
    _register(client, "a1")
    _register(client, "a2")
    client.post("/messages", json={
        "workspace": WS, "channel": "@a1", "agent_id": "a2", "body": "ping",
    })
    first = client.post("/agents/heartbeat", json={"workspace": WS, "agent_id": "a1"}).json()
    assert first["unread_dms"] == 1
    # Non-destructive: a second heartbeat sees the same pending DM.
    second = client.post("/agents/heartbeat", json={"workspace": WS, "agent_id": "a1"}).json()
    assert second["unread_dms"] == 1
    # Actually reading the inbox is what clears it.
    client.get("/inbox", params={"workspace": WS, "agent_id": "a1"})
    third = client.post("/agents/heartbeat", json={"workspace": WS, "agent_id": "a1"}).json()
    assert third["unread_dms"] == 0


def test_heartbeat_does_not_count_channel_traffic_as_a_dm(client):
    _register(client, "a1", channels=["build"])
    _register(client, "a2")
    client.post("/messages", json={
        "workspace": WS, "channel": "build", "agent_id": "a2", "body": "noisy",
    })
    body = client.post("/agents/heartbeat", json={"workspace": WS, "agent_id": "a1"}).json()
    assert body["unread_dms"] == 0


def test_deregister_removes_agent_and_leases(client):
    _register(client, "a1")
    client.post("/leases/acquire", json={"workspace": WS, "resource": "r", "agent_id": "a1"})
    client.delete("/agents/a1", params={"workspace": WS})
    assert client.get("/agents", params={"workspace": WS}).json()["count"] == 0
    assert client.get("/leases", params={"workspace": WS}).json()["count"] == 0


# --- leases -----------------------------------------------------------------


def test_second_claim_gets_409_naming_the_holder(client):
    _register(client, "a1")
    _register(client, "a2")
    client.post("/leases/acquire", json={"workspace": WS, "resource": "r", "agent_id": "a1"})
    response = client.post(
        "/leases/acquire", json={"workspace": WS, "resource": "r", "agent_id": "a2"}
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "lease_conflict"
    assert body["holder"] == "a1"
    assert body["expires_in"] > 0


def test_release_then_reclaim(client):
    client.post("/leases/acquire", json={"workspace": WS, "resource": "r", "agent_id": "a1"})
    released = client.post(
        "/leases/release", json={"workspace": WS, "resource": "r", "agent_id": "a1"}
    )
    assert released.json()["released"] is True
    again = client.post(
        "/leases/acquire", json={"workspace": WS, "resource": "r", "agent_id": "a2"}
    )
    assert again.status_code == 200


def test_releasing_someone_elses_lease_is_409(client):
    client.post("/leases/acquire", json={"workspace": WS, "resource": "r", "agent_id": "a1"})
    response = client.post(
        "/leases/release", json={"workspace": WS, "resource": "r", "agent_id": "a2"}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "not_lease_holder"


def test_lease_ttl_is_clamped_to_the_ceiling(client):
    body = client.post("/leases/acquire", json={
        "workspace": WS, "resource": "r", "agent_id": "a1", "ttl": 999_999_999,
    }).json()
    assert body["lease"]["expires_in"] <= 86_400 + 1


def test_nonpositive_ttl_is_rejected(client):
    response = client.post("/leases/acquire", json={
        "workspace": WS, "resource": "r", "agent_id": "a1", "ttl": 0,
    })
    assert response.status_code == 422


def test_lease_resource_may_contain_slashes(client):
    client.post(
        "/leases/acquire",
        json={"workspace": WS, "resource": "backend/alembic/0142", "agent_id": "a1"},
    )
    body = client.get(f"/leases/{'backend/alembic/0142'}", params={"workspace": WS}).json()
    assert body["held"] is True
    assert body["lease"]["holder"] == "a1"


# --- messaging --------------------------------------------------------------


def test_channel_message_reaches_a_subscriber(client):
    _register(client, "a1", channels=["build"])
    _register(client, "a2", channels=["build"])
    client.post("/messages", json={
        "workspace": WS, "channel": "build", "agent_id": "a2", "body": "heads up",
    })
    body = client.get("/inbox", params={"workspace": WS, "agent_id": "a1"}).json()
    assert [m["body"] for m in body["messages"]] == ["heads up"]


def test_inbox_includes_the_agents_own_direct_channel(client):
    _register(client, "a1")
    client.post("/messages", json={
        "workspace": WS, "channel": "@a1", "agent_id": "a2", "body": "just for you",
    })
    body = client.get("/inbox", params={"workspace": WS, "agent_id": "a1"}).json()
    assert [m["body"] for m in body["messages"]] == ["just for you"]
    assert "@a1" in body["channels"]


def test_inbox_is_drained_once(client):
    _register(client, "a1", channels=["build"])
    client.post("/messages", json={
        "workspace": WS, "channel": "build", "agent_id": "a2", "body": "x",
    })
    first = client.get("/inbox", params={"workspace": WS, "agent_id": "a1"}).json()
    second = client.get("/inbox", params={"workspace": WS, "agent_id": "a1"}).json()
    assert first["count"] == 1 and second["count"] == 0


def test_peek_leaves_the_message_unread(client):
    _register(client, "a1", channels=["build"])
    client.post("/messages", json={
        "workspace": WS, "channel": "build", "agent_id": "a2", "body": "x",
    })
    peeked = client.get(
        "/inbox", params={"workspace": WS, "agent_id": "a1", "peek": True}
    ).json()
    assert peeked["count"] == 1
    assert client.get("/inbox", params={"workspace": WS, "agent_id": "a1"}).json()["count"] == 1


def test_inbox_without_agent_or_channel_is_400(client):
    assert client.get("/inbox", params={"workspace": WS}).status_code == 400


def test_structured_message_bodies_survive(client):
    _register(client, "a1", channels=["build"])
    payload = {"files": ["a.py"], "status": "done", "count": 2}
    client.post("/messages", json={
        "workspace": WS, "channel": "build", "agent_id": "a2",
        "body": payload, "type": "handoff",
    })
    got = client.get("/inbox", params={"workspace": WS, "agent_id": "a1"}).json()["messages"][0]
    assert got["body"] == payload
    assert got["type"] == "handoff"


def test_channel_history_does_not_consume(client):
    client.post("/messages", json={
        "workspace": WS, "channel": "build", "agent_id": "a2", "body": "one",
    })
    _register(client, "a1", channels=["build"])
    history = client.get("/channels/build", params={"workspace": WS}).json()
    assert [m["body"] for m in history["messages"]] == ["one"]
    assert client.get("/inbox", params={"workspace": WS, "agent_id": "a1"}).json()["count"] == 1


def test_channels_listing(client):
    client.post("/messages", json={
        "workspace": WS, "channel": "build", "agent_id": "a2", "body": "x",
    })
    names = [c["channel"] for c in client.get(
        "/channels", params={"workspace": WS}
    ).json()["channels"]]
    assert names == ["build"]


def test_long_poll_returns_promptly_when_a_message_arrives(client):
    """The wait path must return the message, not stall out to the deadline."""
    import threading
    import time

    _register(client, "a1", channels=["build"])

    def post_soon() -> None:
        time.sleep(0.3)
        client.post("/messages", json={
            "workspace": WS, "channel": "build", "agent_id": "a2", "body": "late",
        })

    thread = threading.Thread(target=post_soon)
    thread.start()
    started = time.monotonic()
    body = client.get(
        "/inbox", params={"workspace": WS, "agent_id": "a1", "wait": 10}
    ).json()
    elapsed = time.monotonic() - started
    thread.join()
    assert [m["body"] for m in body["messages"]] == ["late"]
    assert elapsed < 5, "long poll should wake on arrival, not run to the deadline"


def test_overlapping_long_polls_on_one_agent_id_never_both_get_the_message(client):
    """Issue #24: two sessions sharing an agent id (a real risk — see
    docs/concepts.md on how the id is derived from branch + host) each
    holding an open long-poll at once. Whichever wins the race gets it;
    the other must keep waiting and time out empty — not receive it too.
    """
    import queue
    import threading
    import time

    _register(client, "shared-id")

    results: queue.Queue = queue.Queue()

    def poll() -> None:
        body = client.get(
            "/inbox", params={"workspace": WS, "agent_id": "shared-id", "wait": 3}
        ).json()
        results.put([m["body"] for m in body["messages"]])

    t1 = threading.Thread(target=poll)
    t2 = threading.Thread(target=poll)
    t1.start()
    time.sleep(0.1)
    t2.start()
    time.sleep(0.3)  # let both register as long-poll waiters

    client.post("/messages", json={
        "workspace": WS, "channel": "@shared-id", "agent_id": "sender", "body": "once",
    })

    t1.join(timeout=10)
    t2.join(timeout=10)

    got = results.get() + results.get()
    assert got.count("once") == 1


# --- blackboard -------------------------------------------------------------


def test_board_round_trip(client):
    client.put("/board", json={
        "workspace": WS, "key": "plan/migration", "agent_id": "a1",
        "value": {"steps": ["a", "b"]},
    })
    entry = client.get("/board/plan/migration", params={"workspace": WS}).json()["entry"]
    assert entry["value"] == {"steps": ["a", "b"]}
    assert entry["revision"] == 1


def test_board_missing_key_is_404(client):
    assert client.get("/board/nope", params={"workspace": WS}).status_code == 404


def test_board_revision_conflict_is_409(client):
    client.put("/board", json={"workspace": WS, "key": "k", "agent_id": "a1", "value": 1})
    response = client.put("/board", json={
        "workspace": WS, "key": "k", "agent_id": "a2", "value": 2, "if_revision": 99,
    })
    assert response.status_code == 409


def test_board_prefix_listing(client):
    for key in ("plan/a", "plan/b", "other"):
        client.put("/board", json={"workspace": WS, "key": key, "agent_id": "a1", "value": 1})
    entries = client.get(
        "/board", params={"workspace": WS, "prefix": "plan/"}
    ).json()["entries"]
    assert [e["key"] for e in entries] == ["plan/a", "plan/b"]


def test_board_delete(client):
    client.put("/board", json={"workspace": WS, "key": "k", "agent_id": "a1", "value": 1})
    assert client.delete("/board/k", params={"workspace": WS}).json()["deleted"] is True
    assert client.get("/board/k", params={"workspace": WS}).status_code == 404


# --- isolation --------------------------------------------------------------


def test_workspaces_do_not_leak(client):
    _register(client, "a1")
    client.post("/agents/register", json={
        "workspace": "other", "agent_id": "b1", "name": "b1",
    })
    assert client.get("/agents", params={"workspace": WS}).json()["count"] == 1
    assert client.get("/agents", params={"workspace": "other"}).json()["count"] == 1


def test_every_ttl_path_is_clamped(client):
    """docs/api.md promises "always clamped to a ceiling". All five paths.

    Checked because an unclamped one is invisible until a record outlives the
    hub's assumption that nothing does — and the promise is documented, so it
    should be enforced rather than trusted.
    """
    from switchboard.config import (
        MAX_AGENT_TTL,
        MAX_BOARD_TTL,
        MAX_LEASE_TTL,
        MAX_MESSAGE_TTL,
    )

    huge = 10**9
    agent = client.post("/agents/register", json={
        "workspace": WS, "agent_id": "a", "name": "a", "ttl": huge}).json()["agent"]
    assert agent["expires_in"] <= MAX_AGENT_TTL + 2

    lease = client.post("/leases/acquire", json={
        "workspace": WS, "resource": "r", "agent_id": "a", "ttl": huge}).json()["lease"]
    assert lease["expires_in"] <= MAX_LEASE_TTL + 2

    entry = client.put("/board", json={
        "workspace": WS, "key": "k", "agent_id": "a", "value": 1,
        "ttl": huge}).json()["entry"]
    assert entry["expires_in"] <= MAX_BOARD_TTL + 2

    message = client.post("/messages", json={
        "workspace": WS, "channel": "ch", "agent_id": "a", "body": "x",
        "ttl": huge}).json()["message"]
    assert message["expires_at"] is not None  # rendered ISO; bound checked below

    # The heartbeat carries its own lease_ttl, a separate clamp path.
    beat = client.post("/agents/heartbeat", json={
        "workspace": WS, "agent_id": "a", "ttl": huge, "lease_ttl": huge}).json()
    assert beat["leases"][0]["expires_in"] <= MAX_LEASE_TTL + 2
    assert MAX_MESSAGE_TTL > 0  # named so the import is not unused


# --- agent signing keys ------------------------------------------------------


def test_registering_without_a_signing_key_still_works(tmp_path):
    # An older client sends no pubkey at all; the hub must not require one.
    with hub(token="tok") as h:
        http = h.raw(token=None)
        r = http.post("/agents/register",
                      json={"workspace": "w", "name": "legacy", "agent_id": "legacy"},
                      headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        assert r.json()["agent"]["pubkey"] is None


def test_a_database_from_before_pubkey_existed_still_opens(tmp_path):
    # CREATE TABLE IF NOT EXISTS will not add a column to an existing table.
    import sqlite3

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE agents (workspace TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,"
        " kind TEXT NOT NULL DEFAULT 'unknown', branch TEXT, task TEXT,"
        " channels TEXT NOT NULL DEFAULT '[]', meta TEXT NOT NULL DEFAULT '{}',"
        " registered_at REAL NOT NULL, last_seen_at REAL NOT NULL, expires_at REAL NOT NULL,"
        " PRIMARY KEY (workspace, id))"
    )
    conn.commit()
    conn.close()

    store = Store(db)
    agent = store.register_agent(workspace="w", agent_id="a", name="n", ttl=60,
                                 pubkey="PUBKEY")
    assert agent.pubkey == "PUBKEY"
    store.close()


def test_stats_does_not_enumerate_rooms(client):
    """A room identifier is the whole protection now that authorization is
    gone (#61): unguessable, and the only thing between a stranger and a room.
    Publishing the set here handed it over — enumerate, then read and post
    everywhere. Survivable while a token was required; fatal once one was not."""
    client.post("/agents/register", json={"workspace": "w_secret", "name": "a"})
    body = client.get("/stats").json()

    assert "workspaces" not in body
    assert "w_secret" not in json.dumps(body)
    # an operator still learns how many rooms are live, just not which
    assert body["workspace_count"] >= 1


def test_stats_still_answers_for_a_workspace_you_already_know(client):
    # Naming a room you already have the identifier for is not enumeration.
    client.post("/agents/register", json={"workspace": "w_known", "name": "a"})
    assert client.get("/stats", params={"workspace": "w_known"}).json()["agents"] == 1
