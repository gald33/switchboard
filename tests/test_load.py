"""Measuring how busy the hub is, so a load target can come from numbers.

#72 turns on one measured quantity rather than an invented capacity. Nothing
here sheds traffic — it measures, so the target can be chosen from readings
rather than a guess, which is the order that design asks for.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from switchboard.config import ServerConfig
from switchboard.load import LoadMeter
from switchboard.server import create_app
from switchboard.store import Store


@pytest.fixture
def hub(tmp_path):
    db = str(tmp_path / "l.db")
    store = Store(db)
    with TestClient(create_app(ServerConfig(db_path=db), store=store)) as c:
        yield c
    store.close()


def test_a_parked_long_poll_is_not_load():
    """The distinction that decides whether the measurement is usable at all.

    `inbox?wait=` holds a connection up to 25s, and that is the normal state of
    an idle agent. Counting it would show inbox consuming the whole hub while
    doing nothing, and a scheduler reading that would throttle everything else
    in favour of idleness.
    """
    meter = LoadMeter()
    with meter.serving():
        assert meter.snapshot().active == 1
        with meter.parked():
            assert meter.snapshot().active == 0
            assert meter.snapshot().parked == 1
        assert meter.snapshot().active == 1
    assert meter.snapshot().active == 0


def test_concurrency_is_counted_across_threads():
    meter = LoadMeter()
    hold = threading.Event()
    seen = []

    def work():
        with meter.serving():
            seen.append(meter.snapshot().active)
            hold.wait(2)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    peak = meter.snapshot().active
    hold.set()
    for t in threads:
        t.join()
    assert peak == 4
    assert meter.snapshot().active == 0
    assert meter.snapshot().peak_active == 4


def test_delay_is_measured_on_the_way_out():
    # Recorded once with the time it actually took, rather than sampled while
    # still running — otherwise one slow request dominates the window.
    meter = LoadMeter()
    with meter.serving():
        time.sleep(0.05)
    snap = meter.snapshot()
    assert snap.samples == 1
    assert snap.delay_p95_ms >= 40


def test_an_idle_meter_reports_zero_not_an_error():
    snap = LoadMeter().snapshot()
    assert (snap.active, snap.parked, snap.samples) == (0, 0, 0)
    assert snap.delay_p50_ms == 0.0


def test_the_hub_reports_load_for_a_target_to_be_chosen_from(hub):
    hub.get("/health")  # something has to have finished first
    body = hub.get("/stats").json()
    assert set(body["load"]) == {
        "active", "parked", "peak_active", "delay_p50_ms", "delay_p95_ms", "samples",
        "admission",
    }
    assert body["load"]["admission"]["enabled"] is False, "inert until a target is set"
    # delays are recorded on the way out, so this counts *completed* requests
    # — the /stats call itself is still in flight while it builds this payload
    assert body["load"]["samples"] >= 1
    assert body["load"]["active"] == 1, "only this request is in flight"


def test_serving_a_request_does_not_leak_a_slot(hub):
    for _ in range(5):
        hub.get("/health")
    # the /stats request itself is the only one in flight
    assert hub.get("/stats").json()["load"]["active"] <= 1


# --- admission ---------------------------------------------------------------
#
# Built inert. The target is meant to come from measurement, so shipping it
# switched on would reintroduce exactly the invented constant #72 removes.


def _admission(target_ms=50.0):
    from switchboard.load import Admission

    return Admission(LoadMeter(), target_ms=target_ms)


def test_admission_is_off_unless_a_target_is_set():
    from switchboard.load import CLASS_WRITE

    off = _admission(target_ms=0.0)
    assert not off.enabled
    # nothing is bounded, whatever the load
    for _ in range(200):
        with off.admit(CLASS_WRITE):
            pass


def test_a_flood_of_writes_cannot_starve_admission():
    """The cheap attack this exists to stop: flood messages and no new room can
    be created. A reservation guarantees admission its share regardless."""
    from switchboard.load import CLASS_ADMIT, CLASS_WRITE, Rejected

    sched = _admission()
    held = []
    try:
        while True:
            ctx = sched.admit(CLASS_WRITE)
            ctx.__enter__()
            held.append(ctx)
            if len(held) > 400:
                break
    except Rejected:
        pass
    assert held, "writes should get in first"
    # admission still has its floor
    with sched.admit(CLASS_ADMIT):
        pass
    for ctx in held:
        ctx.__exit__(None, None, None)


def test_a_class_is_refused_once_it_exceeds_its_share():
    from switchboard.load import CLASS_WRITE, Rejected

    sched = _admission()
    held = []
    with pytest.raises(Rejected) as exc:
        for _ in range(600):
            ctx = sched.admit(CLASS_WRITE)
            ctx.__enter__()
            held.append(ctx)
    assert exc.value.work_class == CLASS_WRITE
    assert exc.value.retry_after > 0, "a refusal should say when to come back"
    for ctx in held:
        ctx.__exit__(None, None, None)


def test_the_limit_is_discovered_not_configured():
    # Concurrency rises while delay sits under target and falls when it does
    # not, so capacity tracks the machine rather than a constant going stale.
    from switchboard.load import CLASS_READ, Admission

    meter = LoadMeter()
    sched = Admission(meter, target_ms=1000.0)
    start = sched.snapshot()["limit"]
    for _ in range(20):
        with meter.serving(), sched.admit(CLASS_READ):
            pass
    assert sched.snapshot()["limit"] > start, "under target it should probe up"

    slow = LoadMeter()
    strict = Admission(slow, target_ms=0.0001)
    with slow.serving():
        time.sleep(0.02)
    before = strict.snapshot()["limit"]
    for _ in range(5):
        with slow.serving(), strict.admit(CLASS_READ):
            pass
    assert strict.snapshot()["limit"] < before, "over target it should back off"


def test_an_unfed_meter_does_not_ratchet_the_limit_upward():
    # No samples is no information, not "under target" — otherwise a scheduler
    # wired to a meter nobody feeds climbs to its maximum and calls that a
    # measurement.
    from switchboard.load import CLASS_READ, Admission

    sched = Admission(LoadMeter(), target_ms=50.0)
    start = sched.snapshot()["limit"]
    for _ in range(50):
        with sched.admit(CLASS_READ):
            pass
    assert sched.snapshot()["limit"] == start


def test_it_never_squeezes_down_to_serving_nobody():
    from switchboard.load import CLASS_READ, Admission

    meter = LoadMeter()
    sched = Admission(meter, target_ms=0.0001)
    with sched.admit(CLASS_READ):
        time.sleep(0.05)
    for _ in range(500):
        with sched.admit(CLASS_READ):
            pass
    assert sched.snapshot()["limit"] >= Admission.MIN_LIMIT


def test_a_refusal_says_which_class_and_when_to_return(tmp_path, monkeypatch):
    """Shedding is a scheduling decision, not an error, so the response has to
    carry enough to back off well rather than retry into the same wall.

    Note admission bounds *concurrent* work, not request rate: a single client
    issuing requests one after another never exceeds it, and should not — rate
    limiting is the edge's job (#72). So this drives the refusal directly
    rather than pretending a sequential loop is a flood.
    """
    from switchboard.load import Admission, Rejected

    @contextmanager
    def always_busy(self, work_class):
        raise Rejected(work_class, retry_after=2.0)
        yield  # pragma: no cover

    monkeypatch.setattr(Admission, "admit", always_busy)

    db = str(tmp_path / "shed.db")
    store = Store(db)
    with TestClient(create_app(ServerConfig(db_path=db, load_target_ms=50.0),
                               store=store)) as c:
        r = c.post("/messages", json={"workspace": "w", "channel": "x",
                                      "agent_id": "a", "body": "flood"})
        assert r.status_code == 429
        assert r.json() == {"error": "busy", "work_class": "write", "retry_after": 2.0}
        assert r.headers["Retry-After"] == "2"

        # and a new room draws on its own reservation, not the flooded one
        r = c.post("/agents/register", json={"workspace": "w", "name": "a"})
        assert r.json()["work_class"] == "admit"
    store.close()


def test_nothing_is_shed_without_a_target(tmp_path):
    db = str(tmp_path / "open.db")
    store = Store(db)
    with TestClient(create_app(ServerConfig(db_path=db), store=store)) as c:
        codes = {c.post("/messages", json={"workspace": "w", "channel": "x",
                                          "agent_id": "a", "body": "n"}).status_code
                 for _ in range(120)}
        assert 429 not in codes
    store.close()
