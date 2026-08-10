"""Measuring how busy the hub is, so a load target can come from numbers.

#72 turns on one measured quantity rather than an invented capacity. Nothing
here sheds traffic — it measures, so the target can be chosen from readings
rather than a guess, which is the order that design asks for.
"""

from __future__ import annotations

import threading
import time

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
    }
    # delays are recorded on the way out, so this counts *completed* requests
    # — the /stats call itself is still in flight while it builds this payload
    assert body["load"]["samples"] >= 1
    assert body["load"]["active"] == 1, "only this request is in flight"


def test_serving_a_request_does_not_leak_a_slot(hub):
    for _ in range(5):
        hub.get("/health")
    # the /stats request itself is the only one in flight
    assert hub.get("/stats").json()["load"]["active"] <= 1
