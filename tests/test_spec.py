"""What a repo declares about how its agents work together.

The mechanism is Switchboard's; every value in it belongs to the repo. These
tests are mostly about the two properties that make a discovered spec safe to
trust: an inferred value cannot quietly overwrite a decided one, and staleness
is answered by comparison rather than by feel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard.cli import main
from switchboard.spec import (
    Field,
    Refresh,
    Spec,
    SpecError,
    cadence,
)


def test_an_absent_spec_reads_as_empty_rather_than_failing(tmp_path):
    spec = Spec.load(tmp_path)
    assert spec.fields == {} and spec.history == []
    assert spec.is_stale(tmp_path) is None


def test_a_round_trip_keeps_provenance_and_evidence(tmp_path):
    spec = Spec()
    spec.apply({"roles": Field({"impl": "text"}, "asserted", "decided in #12")})
    spec.save(tmp_path)

    again = Spec.load(tmp_path)
    assert again.fields["roles"].provenance == "asserted"
    assert again.fields["roles"].evidence == "decided in #12"
    assert again.fields["roles"].value == {"impl": "text"}


def test_inference_does_not_overwrite_a_decision(tmp_path):
    """The whole reason provenance is recorded.

    A confidently wrong spec is worse than an absent one: every later agent
    reads it and none of them doubt it.
    """
    spec = Spec()
    spec.apply({"claim_prefix": Field("item:", "asserted")})

    changed = spec.apply({"claim_prefix": Field("guessed:", "inferred")})
    assert changed == []
    assert spec.fields["claim_prefix"].value == "item:"

    forced = spec.apply({"claim_prefix": Field("guessed:", "inferred")}, force=True)
    assert forced == ["claim_prefix"]
    assert spec.fields["claim_prefix"].value == "guessed:"


def test_an_unchanged_value_is_not_reported_as_a_change(tmp_path):
    spec = Spec()
    spec.apply({"a": Field(1, "inferred")})
    assert spec.apply({"a": Field(1, "inferred")}) == []


def test_staleness_follows_the_content_of_declared_inputs(tmp_path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("arc 1")
    spec = Spec(inputs=["roadmap.md"])
    spec.record(Refresh(at=100.0, outcome="updated",
                        fingerprint=spec.fingerprint(tmp_path)))
    assert spec.is_stale(tmp_path) is False

    roadmap.write_text("arc 1\narc 2")
    assert spec.is_stale(tmp_path) is True


def test_touching_a_file_without_changing_it_is_not_staleness(tmp_path):
    """Content, not mtime. A checkout, a rebase and a branch switch all move
    mtimes without changing a byte, and refreshing on each would cost a turn
    to learn nothing."""
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("arc 1")
    spec = Spec(inputs=["roadmap.md"])
    spec.record(Refresh(at=100.0, outcome="updated",
                        fingerprint=spec.fingerprint(tmp_path)))

    roadmap.write_text("arc 1")  # rewritten identically
    assert spec.is_stale(tmp_path) is False


def test_a_deleted_input_reads_as_a_change_not_as_agreement(tmp_path):
    roadmap = tmp_path / "roadmap.md"
    roadmap.write_text("arc 1")
    spec = Spec(inputs=["roadmap.md"])
    spec.record(Refresh(at=100.0, outcome="updated",
                        fingerprint=spec.fingerprint(tmp_path)))

    roadmap.unlink()
    assert spec.is_stale(tmp_path) is True


def test_undecidable_staleness_says_so_instead_of_guessing(tmp_path):
    """None is an answer. A confident False with nothing to compare against
    would stop anyone ever refreshing again."""
    spec = Spec()
    spec.record(Refresh(at=100.0, outcome="updated"))
    assert spec.is_stale(tmp_path) is None


def test_the_outcome_vocabulary_is_closed(tmp_path):
    spec = Spec()
    with pytest.raises(SpecError, match="not one of"):
        spec.record(Refresh(at=1.0, outcome="probably-fine"))


def test_cadence_measures_the_repo_not_the_looking(tmp_path):
    """Runs that found nothing say how often somebody looked, which is a fact
    about the agents. The question is how often the repo moves."""
    history = [
        Refresh(at=100.0, outcome="updated"),
        Refresh(at=150.0, outcome="unchanged"),
        Refresh(at=175.0, outcome="unchanged"),
        Refresh(at=200.0, outcome="updated"),
    ]
    assert cadence(history) == 100.0
    assert cadence(history[:1]) is None


def test_cadence_ignores_records_that_never_had_a_timestamp(tmp_path):
    """A record loaded without an `at` defaults to 0.0, which is not a time."""
    assert cadence([
        Refresh(at=0.0, outcome="updated"),
        Refresh(at=200.0, outcome="updated"),
    ]) is None


def test_history_is_capped_but_keeps_the_recent_end(tmp_path):
    spec = Spec()
    for i in range(30):
        spec.record(Refresh(at=float(i), outcome="unchanged"))
    assert len(spec.history) == 20
    assert spec.history[-1].at == 29.0


def test_a_corrupt_spec_is_an_error_not_a_silent_empty(tmp_path):
    path = tmp_path / ".switchboard"
    path.mkdir()
    (path / "spec.json").write_text("{not json")
    with pytest.raises(SpecError, match="not valid JSON"):
        Spec.load(tmp_path)


def test_an_unknown_provenance_is_refused_on_load(tmp_path):
    path = tmp_path / ".switchboard"
    path.mkdir()
    (path / "spec.json").write_text(json.dumps(
        {"fields": {"a": {"value": 1, "provenance": "vibes"}}}
    ))
    with pytest.raises(SpecError, match="provenance"):
        Spec.load(tmp_path)


# --- the CLI surface --------------------------------------------------------


def _run(args: list[str], tmp_path: Path) -> int:
    return main(["refresh", *args, "--dir", str(tmp_path)])


def test_set_then_status_round_trips_through_the_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        json.dumps({"roles": {"implementer": "state your assumption"}})
    ))
    assert _run(["set", "-", "--note", "first pass"], tmp_path) == 0
    capsys.readouterr()

    assert main(["--json", "refresh", "status", "--dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fields"]["roles"]["value"] == {"implementer": "state your assumption"}
    assert payload["history"][-1]["outcome"] == "updated"
    assert payload["stale"] is None, "nothing declared as an input yet"


def test_a_second_identical_set_records_unchanged(tmp_path, capsys, monkeypatch):
    import io

    body = json.dumps({"claim_prefix": "roadmap:"})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    _run(["set", "-"], tmp_path)
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    _run(["set", "-"], tmp_path)
    out = capsys.readouterr().out
    assert "outcome unchanged" in out, (
        "'I looked and nothing moved' must not read as 'I could not tell'"
    )


def test_help_refuses_a_role_the_repo_does_not_declare(tmp_path, capsys, monkeypatch):
    import io

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps({"roles": {"implementer": "state your assumption"}})
    ))
    main(["refresh", "set", "-"])
    capsys.readouterr()

    assert main(["help", "--role", "implementer"]) == 0
    assert "state your assumption" in capsys.readouterr().out

    assert main(["help", "--role", "orchestrator"]) == 1
    err = capsys.readouterr().err
    assert "declares no role" in err and "implementer" in err
