"""Long-poll wake-on-write.

The property under test is not "messages arrive" — test_api covers that. It is
that they arrive *without the waiter having polled for them*, and that they
still arrive when the notifier is blind, which is the case whenever a hub runs
more than one worker.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from switchboard.config import ServerConfig
from switchboard.notify import Notifier
from switchboard.server import create_app
from switchboard.store import Store

WS = "notify-ws"


@pytest.fixture
def hub(tmp_path):
    """An app plus a read counter, driven over ASGI so long polls really block."""
    store = Store(str(tmp_path / "n.db"))
    counter = {"reads": 0}
    original = store.read

    def counting_read(**kwargs):
        counter["reads"] += 1
        return original(**kwargs)

    store.read = counting_read
    app = create_app(ServerConfig(db_path=str(tmp_path / "n.db")), store=store)
    yield app, counter, store
    store.close()


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --- the notifier in isolation ---------------------------------------------


async def test_notify_wakes_a_registered_waiter():
    notifier = Notifier()
    waiter = notifier.register("w", ["c"])
    notifier.notify("w", "c")
    await asyncio.wait_for(waiter, timeout=1)
    assert waiter.done()


async def test_notify_on_an_unwatched_channel_is_a_noop():
    notifier = Notifier()
    notifier.notify("w", "nobody-here")  # must not raise


async def test_one_waiter_on_many_channels_wakes_once():
    notifier = Notifier()
    waiter = notifier.register("w", ["a", "b", "c"])
    notifier.notify("w", "b")
    await asyncio.wait_for(waiter, timeout=1)
    # A second notify on a sibling channel must not explode on the done future.
    notifier.notify("w", "c")


async def test_unregister_releases_everything():
    notifier = Notifier()
    waiter = notifier.register("w", ["a", "b"])
    assert notifier.waiting == 1
    notifier.unregister("w", ["a", "b"], waiter)
    assert notifier.waiting == 0
    # Empty buckets are dropped, not left to accumulate on a long-lived hub.
    assert not notifier._waiters


async def test_workspaces_do_not_wake_each_other():
    notifier = Notifier()
    waiter = notifier.register("one", ["shared"])
    notifier.notify("two", "shared")
    assert not waiter.done()


# --- end to end -------------------------------------------------------------


async def test_message_wakes_a_long_poll_promptly(hub):
    app, counter, _ = hub
    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            await c.post("/agents/register", json={
                "workspace": WS, "agent_id": "a1", "name": "a1", "channels": ["build"]})

            async def poll():
                started = time.monotonic()
                r = await c.get("/inbox", params={
                    "workspace": WS, "agent_id": "a1", "wait": 20}, timeout=60)
                return time.monotonic() - started, r.json()

            task = asyncio.create_task(poll())
            await asyncio.sleep(0.3)
            await c.post("/messages", json={
                "workspace": WS, "channel": "build", "agent_id": "a2", "body": "hi"})
            elapsed, body = await task

    assert [m["body"] for m in body["messages"]] == ["hi"]
    # Woken by the write, not found by polling: the 5s floor has not elapsed.
    assert elapsed < 2.0, f"took {elapsed:.2f}s — wake-on-write did not fire"


async def test_an_idle_long_poll_barely_touches_the_database(hub):
    """The regression that would quietly restore per-waiter polling."""
    app, counter, _ = hub
    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            await c.post("/agents/register", json={
                "workspace": WS, "agent_id": "a1", "name": "a1", "channels": ["build"]})
            counter["reads"] = 0
            await c.get("/inbox", params={
                "workspace": WS, "agent_id": "a1", "wait": 3}, timeout=30)

    # One initial drain, plus at most one floor re-check inside 3s. The old
    # 0.25s poll made this ~13.
    assert counter["reads"] <= 3, f"{counter['reads']} reads for one idle 3s poll"


async def test_delivery_survives_a_blind_notifier(hub):
    """Simulates a second worker: the write happens where our notifier can't see it.

    This is the case that makes the slow floor a correctness requirement rather
    than an optimization. If someone ever deletes the floor because "the
    notifier handles it", this test is what fails.
    """
    app, _, store = hub
    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            await c.post("/agents/register", json={
                "workspace": WS, "agent_id": "a1", "name": "a1", "channels": ["build"]})

            async def poll():
                r = await c.get("/inbox", params={
                    "workspace": WS, "agent_id": "a1", "wait": 20}, timeout=60)
                return r.json()

            task = asyncio.create_task(poll())
            await asyncio.sleep(0.3)
            # Straight to the store — no HTTP, so notify() is never called,
            # exactly as if another process had written the row.
            await asyncio.to_thread(
                store.post, workspace=WS, channel="build", sender="a2",
                body="from another worker", ttl=60,
            )
            body = await asyncio.wait_for(task, timeout=30)

    assert [m["body"] for m in body["messages"]] == ["from another worker"]


async def test_no_lost_wakeup_when_the_write_races_the_first_read(hub):
    """A message posted in the gap between registering and draining.

    Registering interest before the first read is what makes this safe; if the
    order is ever flipped, this is the test that catches it.
    """
    app, _, store = hub
    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            await c.post("/agents/register", json={
                "workspace": WS, "agent_id": "a1", "name": "a1", "channels": ["build"]})

            # Post first, then poll: the message is already there and must be
            # returned immediately rather than waited on.
            await c.post("/messages", json={
                "workspace": WS, "channel": "build", "agent_id": "a2", "body": "early"})
            started = time.monotonic()
            r = await c.get("/inbox", params={
                "workspace": WS, "agent_id": "a1", "wait": 20}, timeout=60)
            elapsed = time.monotonic() - started

    assert [m["body"] for m in r.json()["messages"]] == ["early"]
    assert elapsed < 1.0


async def test_a_wakeup_for_someone_else_does_not_end_the_wait(hub):
    """Our own message wakes the channel but is filtered out of our inbox.

    The waiter must re-arm and keep waiting, not return empty as though the
    deadline had passed.
    """
    app, _, _ = hub
    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            for agent in ("a1", "a2"):
                await c.post("/agents/register", json={
                    "workspace": WS, "agent_id": agent, "name": agent,
                    "channels": ["build"]})

            async def poll():
                r = await c.get("/inbox", params={
                    "workspace": WS, "agent_id": "a1", "wait": 20}, timeout=60)
                return r.json()

            task = asyncio.create_task(poll())
            await asyncio.sleep(0.3)
            # a1's own message: wakes the channel, filtered from a1's inbox.
            await c.post("/messages", json={
                "workspace": WS, "channel": "build", "agent_id": "a1", "body": "mine"})
            await asyncio.sleep(0.5)
            assert not task.done(), "returned empty on a wakeup that had nothing for us"
            # A real message must still get through afterwards.
            await c.post("/messages", json={
                "workspace": WS, "channel": "build", "agent_id": "a2", "body": "theirs"})
            body = await asyncio.wait_for(task, timeout=10)

    assert [m["body"] for m in body["messages"]] == ["theirs"]


async def test_waiters_are_released_after_every_poll(hub):
    """A leak here is invisible until a long-lived hub runs out of memory."""
    app, _, _ = hub
    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            await c.post("/agents/register", json={
                "workspace": WS, "agent_id": "a1", "name": "a1", "channels": ["build"]})
            for _ in range(3):
                await c.get("/inbox", params={
                    "workspace": WS, "agent_id": "a1", "wait": 1}, timeout=30)
            assert app.state.notifier.waiting == 0
            assert not app.state.notifier._waiters


async def test_a_long_poll_keeps_the_waiter_on_the_roster(hub):
    """Waiting is not absence.

    An agent that sat in a wait loop for a peer used to drop off the roster
    while waiting for them — and the convention tells the peer not to wait on
    an agent that is not listed, so both sides followed it into mutual
    invisibility. Reproduced in this project's own cross-session dogfooding.
    """
    app, _, store = hub
    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            await c.post("/agents/register", json={
                "workspace": WS, "agent_id": "waiter", "name": "waiter",
                "channels": ["build"]})
            before = store.get_agent(workspace=WS, agent_id="waiter").last_seen_at

            await c.get("/inbox", params={
                "workspace": WS, "agent_id": "waiter", "wait": 1}, timeout=30)

            after = store.get_agent(workspace=WS, agent_id="waiter").last_seen_at
    assert after > before, "the wait itself must count as a sign of life"


async def test_a_long_poll_does_not_renew_what_the_waiter_holds(hub):
    """Presence, not leases. Renewing a claim is asserting you are working it."""
    app, _, store = hub
    async with await _client(app) as c:
        async with app.router.lifespan_context(app):
            await c.post("/agents/register", json={
                "workspace": WS, "agent_id": "waiter", "name": "waiter",
                "channels": ["build"]})
            await c.post("/leases/acquire", json={
                "workspace": WS, "agent_id": "waiter", "resource": "db/migrations",
                "ttl": 900})
            held = store.list_leases(workspace=WS)[0].expires_at

            await c.get("/inbox", params={
                "workspace": WS, "agent_id": "waiter", "wait": 1}, timeout=30)

            still = store.list_leases(workspace=WS)[0].expires_at
    assert still == held, "a wait must not extend a claim"
