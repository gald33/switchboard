"""Tests for the local adaptive-timing-forecast model.

The event being predicted is "when will this agent next come and look for
messages", so the unit of history is a declare/look pair: the agent says
what it is about to do, and later reads its inbox.
"""

from __future__ import annotations

import pytest

from switchboard.timing import (
    CLASS_HALF_LIFE_SECONDS,
    DEFAULT_CLASSES,
    LOOK,
    MAX_OBSERVATION_SECONDS,
    MAX_OBSERVATIONS_PER_BUCKET,
    MIN_RECALIBRATION_SAMPLES,
    MIN_SAMPLES,
    RECALIBRATION_BOUNDS,
    SPEAK,
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
    assert model._deltas("a", "ws", "coding", "medium") == [20.0]


def test_posting_alone_never_records_an_observation():
    """say/dm declare but do not read, so they cannot close a window — an
    agent can talk without ever looking."""
    model = TimingModel(":memory:")
    model.declare("a", "ws", "coding", "medium", now=0.0)
    model.declare("a", "ws", "coding", "medium", now=10.0)
    model.declare("a", "ws", "coding", "medium", now=20.0)
    assert model._deltas("a", "ws", "coding", "medium") == []
    # Only the look closes it, and it measures from the latest declaration.
    model.note_look("a", "ws", now=25.0)
    assert model._deltas("a", "ws", "coding", "medium") == [5.0]


def test_a_look_with_no_outstanding_declaration_is_not_an_observation():
    model = TimingModel(":memory:")
    model.note_look("a", "ws", now=100.0)
    assert model._deltas("a", "ws", None, None) == []


def test_redeclaring_supersedes_rather_than_scoring_the_old_forecast():
    """A revised estimate was never tested, so it must not count against
    calibration."""
    model = TimingModel(":memory:")
    model.declare("a", "ws", "coding", "high", now=0.0)
    model.declare("a", "ws", "research", "low", now=5.0)
    model.note_look("a", "ws", now=9.0)
    assert model._deltas("a", "ws", "coding", "high") == []
    assert model._deltas("a", "ws", "research", "low") == [4.0]


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
    assert model._deltas("a", "ws", "coding", "medium") == []


def test_agents_and_workspaces_are_isolated():
    model = TimingModel(":memory:")
    cycle(model, "coding", "medium", at=0.0, look_at=10.0, agent="a", workspace="ws1")
    assert model._deltas("b", "ws1", "coding", "medium") == []
    assert model._deltas("a", "ws2", "coding", "medium") == []


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
        "dropped_as_outliers": 0, "discarded_from_other_runs": 0,
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


def test_a_declaration_from_a_previous_run_is_discarded_not_scored(tmp_path):
    """Declare, die, restart, look: the elapsed time measures downtime, not
    behaviour, and a quick restart looks plausible enough to slip past the
    outlier ceiling.

    A restart is two model instances over one store, which is what this
    builds — the process that declared is gone by the time anything looks.
    """
    db = str(tmp_path / "timing.db")

    # The dead run is the named one and the survivor takes the default, so
    # this exercises the case that actually ships: a fresh process, with no
    # runtime configured, refusing to score a window it did not open.
    died = TimingModel(db, runtime_id="the-process-that-died")
    died.declare("a", "ws", "coding", "medium", now=0.0)
    died.close()

    restarted = TimingModel(db)
    restarted.note_look("a", "ws", now=600.0)

    assert restarted._deltas("a", "ws", "coding", "medium") == []
    assert restarted._pending("a", "ws") is None


def test_an_explicit_runtime_id_spans_processes(tmp_path):
    """The CLI's case: every command is its own process, so the run has to
    be named from outside or no observation would ever be scored.

    The inverse of the test above, and the reason the runtime is injectable
    rather than always this process: here the declaring process really is
    gone, but the *run* it belonged to is not.
    """
    db = str(tmp_path / "timing.db")

    declared = TimingModel(db, runtime_id="run-1")
    declared.declare("a", "ws", "coding", "medium", now=0.0)
    declared.close()

    looked = TimingModel(db, runtime_id="run-1")
    looked.note_look("a", "ws", now=300.0)

    assert looked._deltas("a", "ws", "coding", "medium") == [300.0]


def test_a_different_named_run_still_does_not_score(tmp_path):
    """Naming the run must not become a way to score anything against
    anything: two concurrent scripts each own their own windows."""
    db = str(tmp_path / "timing.db")

    TimingModel(db, runtime_id="run-1").declare("a", "ws", "coding", "medium", now=0.0)
    other = TimingModel(db, runtime_id="run-2")
    other.note_look("a", "ws", now=300.0)

    assert other._deltas("a", "ws", "coding", "medium") == []


def test_a_window_another_run_closed_is_counted_not_silently_dropped(tmp_path):
    """Discarding is right; discarding invisibly is not.

    A caller whose runtime id changes between declaring and looking throws
    away every window it opens, and the only symptom was a sample count that
    never moved — indistinguishable from an agent that simply had not worked
    yet. This is what makes the two tellable apart.
    """
    db = str(tmp_path / "timing.db")
    for run in range(3):
        TimingModel(db, runtime_id=f"declare-{run}").declare(
            "a", "ws", "coding", "medium", now=float(run * 100))
        TimingModel(db, runtime_id=f"look-{run}").note_look(
            "a", "ws", now=float(run * 100 + 30))

    report = TimingModel(db).calibration("a", "ws")
    assert report["samples"] == 0
    assert report["discarded_from_other_runs"] == 3
    # Not folded into the outlier count: these were never observations, so
    # they carry none of the truncation bias `dropped` exists to expose.
    assert report["dropped_as_outliers"] == 0


def test_a_healthy_run_reports_no_discards(tmp_path):
    """The counter has to stay at zero when nothing is wrong, or it is just
    noise on every report."""
    db = str(tmp_path / "timing.db")
    model = TimingModel(db, runtime_id="steady")
    for run in range(3):
        cycle(model, "coding", "medium", at=float(run * 100), look_at=float(run * 100 + 30))

    report = model.calibration("a", "ws")
    assert report["samples"] == 3
    assert report["discarded_from_other_runs"] == 0


# --- truncation and retention ------------------------------------------------


def test_dropped_outliers_are_counted_not_silently_discarded():
    """Every censored observation is a gap longer than anything the
    estimator was allowed to see, so it biases p95 low. A silent drop hides
    that; a counted one lets you notice p95 is not trustworthy here."""
    model = TimingModel(":memory:")
    cycle(model, "waiting", "high", at=0.0, look_at=MAX_OBSERVATION_SECONDS * 3)
    assert model._deltas("a", "ws", "waiting", "high") == []
    report = model.calibration("a", "ws")
    assert report["dropped_as_outliers"] == 1


def test_the_ceiling_no_longer_truncates_ordinary_slow_work():
    """A multi-hour gap is real behaviour for a slow agent, not a restart —
    restarts are caught by runtime identity instead."""
    model = TimingModel(":memory:")
    eight_hours = 8 * 3600.0
    assert eight_hours <= MAX_OBSERVATION_SECONDS
    cycle(model, "waiting", "high", at=0.0, look_at=eight_hours)
    assert model._deltas("a", "ws", "waiting", "high") == [eight_hours]


def test_buckets_are_a_moving_window_not_an_archive():
    model = TimingModel(":memory:")
    t = 0.0
    for _ in range(MAX_OBSERVATIONS_PER_BUCKET + 40):
        cycle(model, "coding", "low", at=t, look_at=t + 3.0)
        t += 10
    assert len(model._deltas("a", "ws", "coding", "low")) == MAX_OBSERVATIONS_PER_BUCKET


def test_old_samples_age_out_so_a_faster_agent_is_tracked():
    """An agent that got quicker must not be held back by history from
    when it was slow."""
    model = TimingModel(":memory:")
    t = 0.0
    for _ in range(MAX_OBSERVATIONS_PER_BUCKET):
        cycle(model, "coding", "low", at=t, look_at=t + 300.0)
        t += 400
    slow = model.forecast("a", "ws", "coding", "low", now=t)

    for _ in range(MAX_OBSERVATIONS_PER_BUCKET):
        cycle(model, "coding", "low", at=t, look_at=t + 2.0)
        t += 10
    fast = model.forecast("a", "ws", "coding", "low", now=t)

    assert fast.p50_seconds < slow.p50_seconds / 10
    assert model._deltas("a", "ws", "coding", "low") == [2.0] * MAX_OBSERVATIONS_PER_BUCKET


def test_retention_is_per_bucket_not_global():
    model = TimingModel(":memory:")
    t = 0.0
    for _ in range(MAX_OBSERVATIONS_PER_BUCKET + 20):
        cycle(model, "coding", "low", at=t, look_at=t + 3.0)
        t += 10
    cycle(model, "research", "high", at=t, look_at=t + 50.0)
    assert model._deltas("a", "ws", "research", "high") == [50.0]


# --- self-correction ---------------------------------------------------------


def seed_ratios(model, n, delta, raw_p50, raw_p95, agent="a", workspace="ws"):
    """Insert observations whose outcomes stand in a known relation to the
    raw estimate that produced them."""
    conn = model._connection()
    for i in range(n):
        conn.execute(
            "INSERT INTO observations (agent_id, workspace, execution_class, effort,"
            " delta_seconds, observed_at, predicted_p50, predicted_p95, predicted_from,"
            " raw_p50, raw_p95) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (agent, workspace, "coding", "medium", delta, float(i),
             raw_p50, raw_p95, "class+effort", raw_p50, raw_p95),
        )
    conn.commit()


def test_no_correction_until_there_is_enough_evidence():
    """Below the threshold the measured error is mostly noise, and
    'correcting' for it would add more error than it removes."""
    model = TimingModel(":memory:")
    seed_ratios(model, MIN_RECALIBRATION_SAMPLES - 1, delta=200.0,
                raw_p50=100.0, raw_p95=100.0)
    assert model._correction("a", "ws") == (1.0, 1.0)


def test_a_systematically_short_estimator_is_scaled_up():
    model = TimingModel(":memory:")
    # Every outcome came in at twice the predicted p50 and p95.
    seed_ratios(model, MIN_RECALIBRATION_SAMPLES + 20, delta=200.0,
                raw_p50=100.0, raw_p95=100.0)
    m50, m95 = model._correction("a", "ws")
    assert m50 == pytest.approx(2.0, rel=0.05)
    assert m95 == pytest.approx(2.0, rel=0.05)

    forecast = model.forecast("a", "ws", "coding", "medium", now=0.0)
    assert forecast.p95_seconds == pytest.approx(
        forecast.raw_p95_seconds * m95, rel=1e-6)


def test_a_well_calibrated_estimator_is_left_alone():
    model = TimingModel(":memory:")
    seed_ratios(model, 200, delta=100.0, raw_p50=100.0, raw_p95=100.0)
    m50, m95 = model._correction("a", "ws")
    assert m50 == pytest.approx(1.0, rel=0.05)
    assert m95 == pytest.approx(1.0, rel=0.05)


def test_the_correction_is_bounded():
    """An apparent 100x error is far likelier to be a bug or a regime
    change than a calibration problem, and silently applying it would turn
    a small fault into a useless forecast."""
    model = TimingModel(":memory:")
    seed_ratios(model, 60, delta=100_000.0, raw_p50=1.0, raw_p95=1.0)
    low, high = RECALIBRATION_BOUNDS
    m50, m95 = model._correction("a", "ws")
    assert m50 == high and m95 == high

    other = TimingModel(":memory:")
    seed_ratios(other, 60, delta=0.001, raw_p50=1000.0, raw_p95=1000.0)
    assert other._correction("a", "ws") == (low, low)


def test_the_correction_does_not_compound_on_itself():
    """The failure this guards: measuring error against an
    already-corrected number and then correcting again multiplies every
    cycle. Ratios are taken against the raw estimate, so each pass is a
    fresh calculation with no memory to run away."""
    model = TimingModel(":memory:")
    t = 0.0
    for _ in range(400):
        model.declare("a", "ws", "coding", "medium", now=t)
        model.note_look("a", "ws", now=t + 60.0)
        t += 120.0
    first = model._correction("a", "ws")
    for _ in range(400):
        model.declare("a", "ws", "coding", "medium", now=t)
        model.note_look("a", "ws", now=t + 60.0)
        t += 120.0
    second = model._correction("a", "ws")
    # Stable, not drifting further from 1.0 with every cycle.
    assert abs(second[1] - first[1]) < 0.5
    assert 0.5 < second[1] < 2.0


def test_raw_and_issued_are_both_recorded():
    model = TimingModel(":memory:")
    seed_ratios(model, MIN_RECALIBRATION_SAMPLES + 5, delta=200.0,
                raw_p50=100.0, raw_p95=100.0)
    forecast = model.declare("a", "ws", "coding", "medium", now=10_000.0)
    model.note_look("a", "ws", now=10_050.0)
    row = model._connection().execute(
        "SELECT predicted_p95, raw_p95 FROM observations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == pytest.approx(forecast.p95_seconds)
    assert row[1] == pytest.approx(forecast.raw_p95_seconds)
    assert row[0] != row[1], "correction was applied, so they must differ"


# --- recency weighting and calibration breakdowns ----------------------------


def test_recent_observations_outweigh_old_ones():
    """The retention window is a cliff — a sample counts fully until it
    vanishes. Weighting makes an agent that changed speed converge as new
    behaviour accumulates, rather than waiting for old rows to fall off."""
    model = TimingModel(":memory:")
    now = 100 * 86400.0
    old = [(600.0, now - 60 * 86400.0)] * 40   # slow, two months ago
    new = [(10.0, now - 3600.0)] * 40          # fast, an hour ago
    weighted = model._weighted_quantile(old + new, 0.50, now)
    assert weighted < 100.0, "recent fast samples should dominate"

    # With everything equally recent, the median sits between the two modes.
    same_age = [(v, now) for v, _ in old + new]
    assert 10.0 < model._weighted_quantile(same_age, 0.50, now) < 600.0


def test_weighted_quantile_reduces_to_the_unweighted_one():
    """Equal weights must reproduce the (i-0.5)/n plotting position, so the
    small-sample correction is not quietly lost."""
    model = TimingModel(":memory:")
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    now = 0.0
    for q in (0.1, 0.5, 0.95):
        assert model._weighted_quantile([(v, now) for v in values], q, now) == \
            pytest.approx(model._quantile(values, q))


def test_weighted_quantile_handles_a_single_sample():
    model = TimingModel(":memory:")
    assert model._weighted_quantile([(42.0, 0.0)], 0.95, 0.0) == 42.0


def test_calibration_can_be_broken_down_by_dimension():
    """An aggregate rate says that an agent is miscalibrated, never where."""
    model = TimingModel(":memory:")
    t = 0.0
    # 'quick' work always resolves well inside the forecast; 'slow' never does.
    for _ in range(12):
        cycle(model, "quick", "low", at=t, look_at=t + 1.0)
        t += 100
    for _ in range(12):
        cycle(model, "slow", "low", at=t, look_at=t + 10_000.0)
        t += 20_000

    by_class = model.calibration_by("a", "ws", "execution_class")
    assert by_class["quick"]["p95_hit_rate"] > by_class["slow"]["p95_hit_rate"]
    assert by_class["quick"]["samples"] == 12

    # Same data, split by a different dimension, still totals the same.
    by_effort = model.calibration_by("a", "ws", "effort")
    assert by_effort["low"]["samples"] == 24


def test_calibration_breakdown_rejects_an_unknown_dimension():
    model = TimingModel(":memory:")
    with pytest.raises(ValueError):
        model.calibration_by("a", "ws", "hostname")


# --- looking versus speaking -------------------------------------------------
#
# Reading and replying are separated by a whole model turn. Predicting only
# the first is what made a ten-second rendezvous unwinnable by message-passing
# in dogfooding: the forecast said when the peer would *look*, both agents
# aimed at it, and the window closed in the gap before either acted.


def test_looking_and_speaking_are_learned_separately(tmp_path):
    """An agent that reads promptly and replies slowly must not end up with
    one averaged distribution that describes neither."""
    db = str(tmp_path / "timing.db")
    model = TimingModel(db, runtime_id="run")
    t = 0.0
    for _ in range(MIN_SAMPLES + 1):
        model.declare("a", "ws", "coding", "low", now=t)
        model.note_look("a", "ws", now=t + 2.0)     # looks in 2s
        model.note_speak("a", "ws", now=t + 40.0)   # speaks in 40s
        t += 100.0

    assert model._deltas("a", "ws", "coding", "low", LOOK) == [2.0] * (MIN_SAMPLES + 1)
    assert model._deltas("a", "ws", "coding", "low", SPEAK) == [40.0] * (MIN_SAMPLES + 1)

    looking = model.forecast("a", "ws", "coding", "low", now=t, kind=LOOK)
    speaking = model.forecast("a", "ws", "coding", "low", now=t, kind=SPEAK)
    assert looking.p50_seconds < 10 < speaking.p50_seconds
    assert looking.source == speaking.source == "class+effort"


def test_a_read_does_not_close_the_speak_window(tmp_path):
    """The two windows are independent, or looking would silently score as
    speaking and the gap this exists to measure would vanish."""
    db = str(tmp_path / "timing.db")
    model = TimingModel(db, runtime_id="run")
    model.declare("a", "ws", "coding", "low", now=0.0)
    model.note_look("a", "ws", now=5.0)

    assert model._pending("a", "ws", LOOK) is None      # closed
    assert model._pending("a", "ws", SPEAK) is not None  # still open
    assert model._deltas("a", "ws", "coding", "low", SPEAK) == []

    model.note_speak("a", "ws", now=60.0)
    assert model._deltas("a", "ws", "coding", "low", SPEAK) == [60.0]


def test_one_declaration_carries_both_estimates(tmp_path):
    """What a peer receives: the look pair it always got, and the speak pair
    attached to the same declaration."""
    model = TimingModel(str(tmp_path / "timing.db"), runtime_id="run")
    forecast = model.declare("a", "ws", "coding", "low", now=0.0)
    assert forecast.kind == LOOK
    assert forecast.speak is not None and forecast.speak.kind == SPEAK
    assert set(forecast.as_message_meta()) == {"p50", "p95", "speak_p50", "speak_p95"}


def test_history_written_before_speaking_was_predicted_still_reads_as_looks(tmp_path):
    """The `kind` column defaults to 'look' because that is what every
    pre-existing row actually timed — the default is a fact about old data,
    not a placeholder."""
    db = str(tmp_path / "timing.db")
    model = TimingModel(db, runtime_id="run")
    model._connection().execute(
        "INSERT INTO observations (agent_id, workspace, execution_class, effort,"
        " delta_seconds, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("a", "ws", "coding", "low", 12.0, 1.0),
    )
    model._connection().commit()
    assert model._deltas("a", "ws", "coding", "low", LOOK) == [12.0]
    assert model._deltas("a", "ws", "coding", "low", SPEAK) == []
