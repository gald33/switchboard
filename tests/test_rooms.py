"""A repo declares rooms; an environment holds keys; the agent joins the
intersection.

The point is that a key and a workspace can no longer disagree — they are
chosen together as one record rather than being two values kept in step — and
that a room you cannot open fails *locally and loudly* instead of as an empty
inbox indistinguishable from a quiet one.
"""

from __future__ import annotations

import json

import pytest

from switchboard import rooms
from switchboard.config import ClientConfig


def write(directory, entries, private=False):
    path = directory / (rooms.ROOMS_LOCAL_FILE if private else rooms.ROOMS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rooms": entries}))


def test_a_repo_with_no_rooms_file_behaves_as_before(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert rooms.load(tmp_path) == []
    assert ClientConfig.from_env(tmp_path).workspace.startswith("default-")


def test_the_record_supplies_workspace_key_and_hub_together(tmp_path, monkeypatch):
    write(tmp_path, [{"name": "team", "key_id": "default",
                      "workspace_token": "w_team", "hub_url": "https://h.example"}])
    monkeypatch.setenv("SWITCHBOARD_KEY", "K")
    config = ClientConfig.from_env(tmp_path)
    assert (config.workspace, config.key, config.url) == ("w_team", "K", "https://h.example")


def test_the_environment_still_wins(tmp_path, monkeypatch):
    # How a cloud environment or a one-off command overrides a checkout.
    write(tmp_path, [{"name": "team", "key_id": "default", "workspace_token": "w_team"}])
    monkeypatch.setenv("SWITCHBOARD_KEY", "K")
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", "w_override")
    assert ClientConfig.from_env(tmp_path).workspace == "w_override"


def test_keys_are_referenced_by_id_never_by_position(tmp_path, monkeypatch):
    # A positional index into a list held elsewhere is the original bug in a
    # new place: reorder the file and every repo resolves somewhere else.
    entries = [{"name": "ops", "key_id": "ops", "workspace_token": "w_ops"},
               {"name": "team", "key_id": "default", "workspace_token": "w_team"}]
    monkeypatch.setenv("SWITCHBOARD_KEY_OPS", "K")
    write(tmp_path, entries)
    first = ClientConfig.from_env(tmp_path).workspace
    write(tmp_path, list(reversed(entries)))
    assert ClientConfig.from_env(tmp_path).workspace == first == "w_ops"


def test_a_missing_key_is_a_local_failure_that_names_it(tmp_path):
    write(tmp_path, [{"name": "ops", "key_id": "ops", "workspace_token": "w_ops"}])
    with pytest.raises(rooms.RoomsError) as exc:
        rooms.select(rooms.load(tmp_path), env={})
    assert "ops" in str(exc.value)
    assert "SWITCHBOARD_KEY_OPS" in str(exc.value), "say which variable to set"


def test_ambiguity_is_refused_rather_than_resolved(tmp_path):
    write(tmp_path, [{"name": "a", "key_id": "default", "workspace_token": "w_a"},
                     {"name": "b", "key_id": "other", "workspace_token": "w_b"}])
    env = {"SWITCHBOARD_KEY": "K", "SWITCHBOARD_KEY_OTHER": "K2"}
    with pytest.raises(rooms.RoomsError, match="more than one"):
        rooms.select(rooms.load(tmp_path), env=env)
    # naming one resolves it
    assert rooms.select(rooms.load(tmp_path), env=env, chosen="b").workspace_token == "w_b"


def test_the_private_overlay_wins_and_is_marked(tmp_path):
    write(tmp_path, [{"name": "team", "key_id": "default", "workspace_token": "w_public"}])
    write(tmp_path, [{"name": "team", "key_id": "default", "workspace_token": "w_private"}],
          private=True)
    loaded = rooms.load(tmp_path)
    assert [r.workspace_token for r in loaded] == ["w_private"]
    assert loaded[0].private is True


def test_env_var_names_are_predictable(tmp_path):
    assert rooms.env_var_for("default") == "SWITCHBOARD_KEY"
    assert rooms.env_var_for("ops") == "SWITCHBOARD_KEY_OPS"
    # a key id is free-form; the variable name cannot be
    assert rooms.env_var_for("team/infra") == "SWITCHBOARD_KEY_TEAM_INFRA"


def test_a_broken_rooms_file_does_not_break_every_command(tmp_path, monkeypatch):
    # from_env runs on the path of `--help`. A malformed file must surface in
    # `switchboard rooms`, not make the CLI unusable.
    (tmp_path / ".switchboard").mkdir()
    (tmp_path / rooms.ROOMS_FILE).write_text("{not json")
    monkeypatch.chdir(tmp_path)
    assert ClientConfig.from_env(tmp_path).workspace.startswith("default-")
    with pytest.raises(rooms.RoomsError):
        rooms.load(tmp_path)


def test_a_room_without_a_workspace_token_is_refused(tmp_path):
    write(tmp_path, [{"name": "team", "key_id": "default"}])
    with pytest.raises(rooms.RoomsError, match="workspace_token"):
        rooms.load(tmp_path)
