"""Adaptive timing over the CLI.

The forecast machinery (`timing.py`) was reachable only through the MCP
bridge, so a CLI-driven agent published no forecasts and learned nothing.
These cover the parts of closing that gap that are easy to get wrong: the
wire shape a receiver has to understand, and the run identity that decides
whether an observation is recorded at all.
"""

from __future__ import annotations

import json

from switchboard.cli import (
    _body_with_forecast,
    _runtime_id,
    _split_forecast,
    build_parser,
)
from switchboard.timing import TimingModel


def _forecast(**kwargs):
    model = TimingModel(":memory:")
    return model.declare("a", "ws", kwargs.get("cls", "coding"), kwargs.get("effort", "low"))


# --- argument surface --------------------------------------------------------


def test_the_timing_flags_reach_every_command_that_can_use_them():
    """say/dm declare; inbox/checkin/watch both observe and declare. A
    command missing the pair is a silent hole — the agent passes the flag,
    argparse rejects it or ignores it, and no forecast is ever published."""
    parser = build_parser()
    for argv in (
        ["say", "general", "hello"],
        ["dm", "bob", "hello"],
        ["inbox"],
        ["checkin"],
        ["watch"],
    ):
        args = parser.parse_args([*argv, "--execution-class", "coding", "--effort", "high"])
        assert args.execution_class == "coding", argv
        assert args.effort == "high", argv


def test_effort_is_constrained_but_execution_class_is_free_form():
    """The effort scale is closed on purpose; the class taxonomy is meant to
    emerge from use, so the CLI must not police it."""
    parser = build_parser()
    args = parser.parse_args(["say", "c", "m", "--execution-class", "untangling-yaml"])
    assert args.execution_class == "untangling-yaml"

    try:
        parser.parse_args(["say", "c", "m", "--effort", "enormous"])
    except SystemExit:
        pass
    else:  # pragma: no cover
        raise AssertionError("an unknown effort level should be rejected")


# --- wire shape --------------------------------------------------------------


def test_the_body_shape_matches_what_the_mcp_bridge_reads_back():
    """A CLI agent and an MCP agent must be able to hold one conversation,
    so the envelope has to be the exact shape `Bridge._msg` unwraps."""
    from switchboard.mcp_server import Bridge

    forecast = _forecast()
    body = _body_with_forecast("taking the migration", forecast)
    assert set(body) == {"text", "timing_forecast"}

    unwrapped = Bridge._msg({
        "seq": 1, "from": "b", "channel": "@a", "body": body,
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    assert unwrapped["body"] == "taking the migration"
    assert unwrapped["timing_forecast"]["p50"] == forecast.as_message_meta()["p50"]


def test_a_message_without_a_forecast_is_left_as_a_bare_string():
    """No declaration means no envelope. Wrapping unconditionally would put
    every plain CLI message into a dict body that older clients render as
    JSON noise."""
    assert _body_with_forecast("plain", None) == "plain"
    assert _split_forecast("plain") == ("plain", None)


def test_an_ordinary_dict_body_is_not_mistaken_for_an_envelope():
    """Someone posting structured JSON that happens to have a `text` key
    must get it back untouched."""
    body = {"text": "hi", "severity": "high"}
    assert _split_forecast(body) == (body, None)


def test_round_trip_through_the_envelope_recovers_the_text():
    forecast = _forecast()
    text, carried = _split_forecast(_body_with_forecast("hello", forecast))
    assert text == "hello"
    assert set(carried) == {"p50", "p95"}


def test_only_the_two_timestamps_travel():
    """Sample counts, the fallback tier that produced the estimate and the
    self-correction multipliers are local diagnostics. Leaking them would
    publish the shape of an agent's history to everyone on the hub."""
    forecast = _forecast()
    body = _body_with_forecast("hello", forecast)
    assert set(body["timing_forecast"]) == {"p50", "p95"}
    assert "source" not in json.dumps(body)
    assert "samples" not in json.dumps(body)


# --- run identity ------------------------------------------------------------


def test_the_runtime_id_is_stable_across_invocations_of_one_agent():
    """The whole reason the CLI needed more than argument plumbing: each
    command is a new process, so a per-process runtime would discard every
    observation and the estimator would never leave its bootstrap priors."""
    assert _runtime_id("agent-a") == _runtime_id("agent-a")


def test_two_agents_on_one_machine_do_not_share_a_run(monkeypatch):
    monkeypatch.delenv("SWITCHBOARD_RUNTIME_ID", raising=False)
    assert _runtime_id("agent-a") != _runtime_id("agent-b")


def test_an_explicit_runtime_id_overrides_the_per_agent_default(monkeypatch):
    """So one script can scope a run deliberately, and two concurrent
    scripts driving the same agent id keep their windows apart."""
    monkeypatch.setenv("SWITCHBOARD_RUNTIME_ID", "script-7")
    assert _runtime_id("agent-a") == "script-7"
    assert _runtime_id("agent-b") == "script-7"


def test_a_declare_then_look_across_two_processes_is_recorded(tmp_path):
    """End to end over the store, standing in for `say --effort` followed by
    a separate `inbox`: the observation must survive the process boundary."""
    db = str(tmp_path / "timing.db")
    runtime = _runtime_id("agent-a")

    sender = TimingModel(db, runtime_id=runtime)
    sender.declare("agent-a", "ws", "coding", "low", now=100.0)
    sender.close()

    reader = TimingModel(db, runtime_id=runtime)
    reader.note_look("agent-a", "ws", now=130.0)

    assert reader._deltas("agent-a", "ws", "coding", "low") == [30.0]
