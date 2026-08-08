"""Tests for the local adaptive-timing-forecast model.

The event being predicted is "when will this agent next come and look for
messages", so the unit of history is a declare/look pair: the agent says
what it is about to do, and later reads its inbox.
"""

from __future__ import annotations

from switchboard.timing import (
    CLASS_HALF_LIFE_SECONDS,
    DEFAULT_CLASSES,
    MAX_OBSERVATION_SECONDS,
    MIN_SAMPLES,
    TimingModel,
)


def cycle(model, execution_class, effort, at, look_at, agent="a", workspace="ws"):
    """One complete observation: declare at `at`, look at `look_at`."""
    forecast = model.declare(agent, workspace, execution_class, effort, now=at)
    model.note_look(agent, workspace, now=look_at)
    return forecast


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


def test_a_look_after_a_declaration_records_the_gap():
    model = TimingModel(":memory:")
    cycle(model, "coding", "medium", at=1_000_000.0, look_at=1_000_020.0)
    assert model._samples("a", "ws", "coding", "medium") == [20.0]


def test_posting_alone_never_records_an_observation():
    """say/dm declare but do not read, so they cannot close a window — an
    agent can talk without ever looking."""
    model = TimingModel(":memory:")
    model.declare("a", "ws", "coding", "medium", now=0.0)
    model.declare("a", "ws", "coding", "medium", now=10.0)
    model.declare("a", "ws", "coding", "medium", now=20.0)
    assert model._samples("a", "ws", "coding", "medium") == []
    # Only the look closes it, and it measures from the latest declaration.
    model.note_look("a", "ws", now=25.0)
    assert model._samples("a", "ws", "coding", "medium") == [5.0]


def test_a_look_with_no_outstanding_declaration_is_not_an_observation():
    model = TimingModel(":memory:")
    model.note_look("a", "ws", now=100.0)
    assert model._samples("a", "ws", None, None) == []


def test_redeclaring_supersedes_rather_than_scoring_the_old_forecast():
    """A revised estimate was never tested, so it must not count against
    calibration."""
    model = TimingModel(":memory:")
    model.declare("a", "ws", "coding", "high", now=0.0)
    model.declare("a", "ws", "research", "low", now=5.0)
    model.note_look("a", "ws", now=9.0)
    assert model._samples("a", "ws", "coding", "high") == []
    assert model._samples("a", "ws", "research", "low") == [4.0]


def test_declare_returns_nothing_without_a_classification():
    model = TimingModel(":memory:")
    assert model.declare("a", "ws", None, None, now=0.0) is None
    assert model._pending("a", "ws") is None


def test_specific_bucket_used_once_enough_samples():
    model = TimingModel(":memory:")
    t = 0.0
    for delta in range(1, MIN_SAMPLES + 1):
        cycle(model, "coding", "medium", at=t, look_at=t + delta)
        t += delta + 1
    f = model.forecast("a", "ws", "coding", "medium", now=t)
    assert f.source == "class+effort"
    assert f.samples == MIN_SAMPLES


def test_falls_back_to_coarser_bucket_when_sparse():
    model = TimingModel(":memory:")
    t = 0.0
    for delta in range(1, MIN_SAMPLES + 1):
        cycle(model, "research", "high", at=t, look_at=t + delta)
        t += delta + 1
    # A class/effort pair with zero samples of its own.
    f = model.forecast("a", "ws", "coding", "low", now=t)
    assert f.source == "agent-wide"
    assert f.samples == MIN_SAMPLES


def test_outlier_deltas_are_dropped():
    model = TimingModel(":memory:")
    cycle(model, "coding", "medium", at=0.0, look_at=MAX_OBSERVATION_SECONDS * 10)
    assert model._samples("a", "ws", "coding", "medium") == []


def test_agents_and_workspaces_are_isolated():
    model = TimingModel(":memory:")
    cycle(model, "coding", "medium", at=0.0, look_at=10.0, agent="a", workspace="ws1")
    assert model._samples("b", "ws1", "coding", "medium") == []
    assert model._samples("a", "ws2", "coding", "medium") == []


def test_observation_records_what_was_predicted_for_it():
    """Calibration has to stay answerable after the fact, so the forecast
    issued when a window opened is stored with its outcome."""
    model = TimingModel(":memory:")
    forecast = cycle(model, "coding", "medium", at=0.0, look_at=30.0)
    row = model._connection().execute(
        "SELECT delta_seconds, predicted_p50, predicted_p95, predicted_from FROM observations"
    ).fetchone()
    assert row[0] == 30.0
    assert row[1] == forecast.p50_seconds
    assert row[2] == forecast.p95_seconds
    assert row[3] == "bootstrap"


def test_calibration_reports_hit_rates():
    model = TimingModel(":memory:")
    # Bootstrap medium is (120s, 900s). Two looks land under p50, two over.
    t = 0.0
    for delta in (10.0, 600.0, 10.0, 600.0):
        cycle(model, "coding", "medium", at=t, look_at=t + delta)
        t += delta + 1
    report = model.calibration("a", "ws")
    assert report["samples"] == 4
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
        cycle(model, "refactoring", "low", at=t, look_at=t + 5)
        t += 10
    for _ in range(2):
        cycle(model, "triage", "low", at=t, look_at=t + 5)
        t += 10

    ranked = model.top_classes("a", "ws", now=t)
    assert ranked[0] == "refactoring"
    assert ranked[1] == "triage"
    assert len(ranked) == 6
    assert set(DEFAULT_CLASSES) & set(ranked[2:])


def test_top_classes_decays_stale_categories_below_recent_ones():
    model = TimingModel(":memory:")
    t = 0.0
    for _ in range(4):
        cycle(model, "ancient", "low", at=t, look_at=t + 5)
        t += 10
    t += CLASS_HALF_LIFE_SECONDS * 6
    for _ in range(3):
        cycle(model, "current", "low", at=t, look_at=t + 5)
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
    assert cycle(model, "yak-shaving", "low", at=0.0, look_at=12.0) is not None
    assert "yak-shaving" in model.top_classes("a", "ws", now=12.0)


def test_forecast_as_message_meta_is_sparse():
    model = TimingModel(":memory:")
    f = model.forecast("a", "ws", "coding", "medium", now=1000.0)
    meta = f.as_message_meta()
    assert set(meta.keys()) == {"p50", "p95"}
    assert meta["p50"] < meta["p95"]


# --- small-sample calibration ------------------------------------------------


def test_p95_is_not_just_the_sample_maximum():
    """With the old i/(n-1) plotting position, q=0.95 landed exactly on the
    largest of five draws — which sits near the true p83, making the
    conservative checkpoint optimistic precisely where it is leaned on."""
    model = TimingModel(":memory:")
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert model._quantile(values, 0.95) > values[-2]
    # p50 of five sorted values is still the middle one.
    assert model._quantile(values, 0.50) == 3.0


def test_sparse_buckets_are_pulled_toward_the_wide_prior():
    """A handful of fast samples must not immediately produce a confident,
    narrow p95 — the tail you have not sampled always reads as too short."""
    model = TimingModel(":memory:")
    t = 0.0
    for _ in range(MIN_SAMPLES):
        cycle(model, "coding", "high", at=t, look_at=t + 1.0)
        t += 10
    sparse = model.forecast("a", "ws", "coding", "high", now=t)
    prior_p95 = 3600.0  # bootstrap 'high'
    assert 1.0 < sparse.p95_seconds < prior_p95, "should sit between sample and prior"

    # With plenty of consistent evidence it converges on the observations.
    for _ in range(300):
        cycle(model, "coding", "high", at=t, look_at=t + 1.0)
        t += 10
    dense = model.forecast("a", "ws", "coding", "high", now=t)
    assert dense.p95_seconds < sparse.p95_seconds


# --- restart safety ----------------------------------------------------------


def test_a_declaration_from_a_previous_run_is_discarded_not_scored():
    """Declare, die, restart, look: the elapsed time measures downtime, not
    behaviour, and a quick restart looks plausible enough to slip past the
    outlier ceiling."""
    import switchboard.timing as timing_module

    model = TimingModel(":memory:")
    model.declare("a", "ws", "coding", "medium", now=0.0)

    original = timing_module._RUNTIME_ID
    try:
        timing_module._RUNTIME_ID = "a-different-process"
        model.note_look("a", "ws", now=600.0)
    finally:
        timing_module._RUNTIME_ID = original

    assert model._samples("a", "ws", "coding", "medium") == []
    assert model._pending("a", "ws") is None
