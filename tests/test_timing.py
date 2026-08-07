"""Tests for the local adaptive-timing-forecast model."""

from __future__ import annotations

from switchboard.timing import MIN_SAMPLES, TimingModel


def test_bootstrap_forecast_before_any_history():
    model = TimingModel(":memory:")
    f = model.forecast("agent-a", "ws", "coding", "medium", now=1000.0)
    assert f.source == "bootstrap"
    assert f.samples == 0
    assert 0 < f.p50_seconds < f.p95_seconds


def test_bootstrap_scales_with_effort():
    model = TimingModel(":memory:")
    low = model.forecast("a", "ws", None, "low", now=0.0)
    high = model.forecast("a", "ws", None, "high", now=0.0)
    assert low.p50_seconds < high.p50_seconds
    assert low.p95_seconds < high.p95_seconds


def test_observe_and_classify_records_delta_between_sends():
    model = TimingModel(":memory:")
    t0 = 1_000_000.0
    # First classified send: nothing to observe yet, pending gets set.
    assert model.observe_and_classify("a", "ws", "coding", "medium", now=t0) is not None
    # Second send 20s later, same class: records a 20s observation for the
    # *first* send's classification and returns a forecast for this one.
    forecast = model.observe_and_classify("a", "ws", "coding", "medium", now=t0 + 20)
    assert forecast is not None
    samples = model._samples("a", "ws", "coding", "medium")
    assert samples == [20.0]


def test_specific_bucket_used_once_enough_samples():
    model = TimingModel(":memory:")
    t = 0.0
    for delta in range(1, MIN_SAMPLES + 2):
        model.observe_and_classify("a", "ws", "coding", "medium", now=t)
        t += delta
    f = model.forecast("a", "ws", "coding", "medium", now=t)
    assert f.source == "class+effort"
    assert f.samples == MIN_SAMPLES


def test_falls_back_to_coarser_bucket_when_sparse():
    model = TimingModel(":memory:")
    # Build up enough agent-wide history under a different class.
    t = 0.0
    for delta in range(1, MIN_SAMPLES + 2):
        model.observe_and_classify("a", "ws", "research", "high", now=t)
        t += delta
    # Ask about a class/effort pair with zero samples of its own.
    f = model.forecast("a", "ws", "coding", "low", now=t)
    assert f.source == "agent-wide"
    assert f.samples == MIN_SAMPLES


def test_outlier_deltas_are_dropped():
    model = TimingModel(":memory:")
    from switchboard.timing import MAX_OBSERVATION_SECONDS

    model.observe_and_classify("a", "ws", "coding", "medium", now=0.0)
    # A huge gap (e.g. an overnight restart) should not be recorded.
    model.observe_and_classify("a", "ws", "coding", "medium", now=MAX_OBSERVATION_SECONDS * 10)
    assert model._samples("a", "ws", "coding", "medium") == []


def test_unclassified_send_does_not_extend_pending_but_still_closes_it():
    model = TimingModel(":memory:")
    model.observe_and_classify("a", "ws", "coding", "medium", now=0.0)
    # An unclassified send 5s later: closes out the pending "coding/medium"
    # observation, but does not itself start a new pending window.
    result = model.observe_and_classify("a", "ws", None, None, now=5.0)
    assert result is None
    assert model._samples("a", "ws", "coding", "medium") == [5.0]
    assert model._pending("a", "ws") is None


def test_agents_and_workspaces_are_isolated():
    model = TimingModel(":memory:")
    model.observe_and_classify("a", "ws1", "coding", "medium", now=0.0)
    model.observe_and_classify("a", "ws1", "coding", "medium", now=10.0)
    assert model._samples("b", "ws1", "coding", "medium") == []
    assert model._samples("a", "ws2", "coding", "medium") == []


def test_forecast_as_message_meta_is_sparse():
    model = TimingModel(":memory:")
    f = model.forecast("a", "ws", "coding", "medium", now=1000.0)
    meta = f.as_message_meta()
    assert set(meta.keys()) == {"p50", "p95"}
    assert meta["p50"] < meta["p95"]
