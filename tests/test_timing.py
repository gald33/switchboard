"""Tests for the local adaptive-timing-forecast model."""

from __future__ import annotations

from switchboard.timing import (
    CLASS_HALF_LIFE_SECONDS,
    DEFAULT_CLASSES,
    MIN_SAMPLES,
    TimingModel,
)


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


def test_observation_records_what_was_predicted_for_it():
    """Gap 2: calibration has to stay answerable after the fact, so the
    forecast issued when a window opened is stored with its outcome."""
    model = TimingModel(":memory:")
    forecast = model.observe_and_classify("a", "ws", "coding", "medium", now=0.0)
    model.observe_and_classify("a", "ws", "coding", "medium", now=30.0)
    row = model._connection().execute(
        "SELECT delta_seconds, predicted_p50, predicted_p95, predicted_from FROM observations"
    ).fetchone()
    assert row[0] == 30.0
    assert row[1] == forecast.p50_seconds
    assert row[2] == forecast.p95_seconds
    assert row[3] == "bootstrap"


def test_calibration_reports_hit_rates():
    model = TimingModel(":memory:")
    # Bootstrap medium is (120s, 900s). Alternate outcomes either side of p50.
    t = 0.0
    for delta in (10.0, 600.0, 10.0, 600.0):
        model.observe_and_classify("a", "ws", "coding", "medium", now=t)
        t += delta
    model.observe_and_classify("a", "ws", "coding", "medium", now=t)
    report = model.calibration("a", "ws")
    assert report["samples"] == 4
    # Two of four landed under p50; all four under p95.
    assert report["p50_hit_rate"] == 0.5
    assert report["p95_hit_rate"] == 1.0


def test_calibration_is_empty_without_history():
    model = TimingModel(":memory:")
    assert model.calibration("a", "ws") == {
        "samples": 0, "p50_hit_rate": None, "p95_hit_rate": None,
    }


def test_top_classes_ranks_by_use_and_pads_with_defaults():
    model = TimingModel(":memory:")
    t = 0.0
    for _ in range(3):
        model.observe_and_classify("a", "ws", "refactoring", "low", now=t)
        t += 10
    model.observe_and_classify("a", "ws", "triage", "low", now=t)
    t += 10
    model.observe_and_classify("a", "ws", "triage", "low", now=t)

    ranked = model.top_classes("a", "ws", now=t)
    # Used classes lead, most-used first; defaults fill the rest of the slate.
    assert ranked[0] == "refactoring"
    assert ranked[1] == "triage"
    assert len(ranked) == 6
    assert set(DEFAULT_CLASSES) & set(ranked[2:])


def test_top_classes_decays_stale_categories_below_recent_ones():
    model = TimingModel(":memory:")
    t = 0.0
    # An old burst of one class...
    for _ in range(4):
        model.observe_and_classify("a", "ws", "ancient", "low", now=t)
        t += 10
    # ...then a long silence, then fewer but recent uses of another.
    t += CLASS_HALF_LIFE_SECONDS * 6
    for _ in range(3):
        model.observe_and_classify("a", "ws", "current", "low", now=t)
        t += 10

    ranked = model.top_classes("a", "ws", now=t)
    assert ranked.index("current") < ranked.index("ancient")


def test_top_classes_on_a_cold_start_offers_defaults():
    model = TimingModel(":memory:")
    assert model.top_classes("a", "ws", now=0.0) == list(DEFAULT_CLASSES)


def test_custom_class_is_never_rejected():
    """The offer set is a shortlist, not an enum — a brand-new label works
    and then earns its way into the shortlist."""
    model = TimingModel(":memory:")
    f = model.observe_and_classify("a", "ws", "yak-shaving", "low", now=0.0)
    assert f is not None
    model.observe_and_classify("a", "ws", "yak-shaving", "low", now=12.0)
    assert "yak-shaving" in model.top_classes("a", "ws", now=12.0)


def test_forecast_as_message_meta_is_sparse():
    model = TimingModel(":memory:")
    f = model.forecast("a", "ws", "coding", "medium", now=1000.0)
    meta = f.as_message_meta()
    assert set(meta.keys()) == {"p50", "p95"}
    assert meta["p50"] < meta["p95"]
