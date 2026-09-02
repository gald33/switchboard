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
    assert config.workspace == rooms.workspace_for("w_team")
    assert (config.key, config.url) == ("K", "https://h.example")


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
    assert ClientConfig.from_env(tmp_path).workspace == first == rooms.workspace_for("w_ops")


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


# --- the wire identifier is derived, not assigned ----------------------------


def test_the_workspace_is_derived_from_the_token(tmp_path, monkeypatch):
    # Definitional rather than asserted: two parties holding the same token
    # compute the same id without being told, which is why nothing needs to be
    # registered anywhere.
    write(tmp_path, [{"name": "team", "key_id": "default", "workspace_token": "shared-token"}])
    monkeypatch.setenv("SWITCHBOARD_KEY", "K")
    config = ClientConfig.from_env(tmp_path)
    assert config.workspace == rooms.workspace_for("shared-token")
    assert config.workspace != "shared-token", "the token itself must not be the wire value"


def test_two_repos_with_one_token_land_in_one_room(tmp_path):
    # N repos -> 1 room, the case that motivated multi-room in the first place.
    a, b = tmp_path / "api", tmp_path / "web"
    for d in (a, b):
        write(d, [{"name": "team", "key_id": "default", "workspace_token": "same"}])
    assert rooms.load(a)[0].workspace == rooms.load(b)[0].workspace


def test_a_readable_token_does_not_become_a_readable_room(tmp_path):
    # A token someone typed by hand must not turn into a guessable name the
    # hub can read, or squat.
    assert "acme" not in rooms.workspace_for("acme/api")
    assert rooms.workspace_for("acme/api").startswith("w_")


def test_the_derivation_is_stable_and_collision_free_enough(tmp_path):
    assert rooms.workspace_for("x") == rooms.workspace_for("x")
    assert len({rooms.workspace_for(str(n)) for n in range(500)}) == 500


# --- a room the repo did not choose ------------------------------------------
#
# Resolution can fail two ways — a file too malformed to declare anything, and
# a file that declares several rooms this environment can open — and both fall
# through to the derived `default-<tag>` workspace. That is not one of the
# rooms. Peers who *did* resolve the file are elsewhere, every command exits 0,
# and an empty roster reads exactly like being first to arrive. It is the
# failure the rooms file exists to remove, produced by the rooms file.


def _two_openable(tmp_path, monkeypatch):
    write(tmp_path, [
        {"name": "main", "key_id": "default", "workspace_token": "tok-main"},
        {"name": "guest", "key_id": "guest", "workspace_token": "tok-guest"},
    ])
    monkeypatch.setenv("SWITCHBOARD_KEY", "k" * 43)
    monkeypatch.setenv("SWITCHBOARD_KEY_GUEST", "g" * 43)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    monkeypatch.delenv("SWITCHBOARD_ROOM", raising=False)


def test_an_unresolved_rooms_file_is_reported_rather_than_silently_defaulted(
    tmp_path, monkeypatch,
):
    from switchboard.config import rooms_warning

    _two_openable(tmp_path, monkeypatch)
    config = ClientConfig.from_env(tmp_path)

    # The fallback still happens — raising here would break `--help`, which is
    # why it was swallowed in the first place. What changes is that it says so.
    assert config.workspace.startswith("default-")
    assert config.room_problem is not None
    note = rooms_warning(config)
    assert note is not None
    assert "guest, main" in note
    assert config.workspace in note          # names the room it actually used


def test_a_malformed_rooms_file_is_reported_the_same_way(tmp_path, monkeypatch):
    """The other route to the same place. `switchboard rooms` already said so,
    and that is not enough: nobody runs `rooms` while everything exits 0."""
    from switchboard.config import rooms_warning

    (tmp_path / rooms.ROOMS_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rooms.ROOMS_FILE).write_text("{not json")
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)

    config = ClientConfig.from_env(tmp_path)
    assert config.workspace.startswith("default-")
    assert "not valid JSON" in (rooms_warning(config) or "")


def test_resolving_cleanly_says_nothing(tmp_path, monkeypatch):
    from switchboard.config import rooms_warning

    _two_openable(tmp_path, monkeypatch)
    monkeypatch.setenv("SWITCHBOARD_ROOM", "guest")
    config = ClientConfig.from_env(tmp_path)

    assert config.workspace == rooms.workspace_for("tok-guest")
    assert config.room_problem is None
    assert rooms_warning(config) is None


def test_an_exported_workspace_settles_it_and_silences_the_warning(
    tmp_path, monkeypatch,
):
    """The ambiguity is only a problem because of what it falls back to. Told
    the workspace outright, there is nothing left to warn about, and doing so
    anyway would be nagging somebody who already decided."""
    from switchboard.config import rooms_warning

    _two_openable(tmp_path, monkeypatch)
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", "w_chosen")
    config = ClientConfig.from_env(tmp_path)

    assert (config.workspace, config.room_problem) == ("w_chosen", None)
    assert rooms_warning(config) is None


def test_a_repo_with_no_rooms_file_is_not_warned_about(tmp_path, monkeypatch):
    """Most repos. The derived default is the correct answer there, not a
    fallback from anything, and a warning would fire on every command."""
    from switchboard.config import rooms_warning

    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    config = ClientConfig.from_env(tmp_path)

    assert config.room_problem is None
    assert rooms_warning(config) is None


# --- the lobby derivation is frozen -----------------------------------------


def test_the_lobby_derivation_never_changes():
    """A golden value, pinned against the version shipped in 1.5.1.

    The lobby is the one room whose purpose is joining agents that do NOT ship
    together — different repos, machines and installed versions. Changing the
    derivation would sort them into rooms by library version, and do it
    silently: a lobby is unguessable by design, so an agent in the wrong one
    sees every call succeed and an empty roster, which is indistinguishable
    from a quiet room. This test is the loud failure that absence would deny
    anyone, so treat a diff here as a bug in the change, never in the test.
    """
    from switchboard.rooms import lobby_token

    # A literal, never a value recomputed from the code under test — that
    # would agree with any change it made, which is the one thing this must
    # not do.
    assert lobby_token("k" * 43) == "lobby-vAYwuUCdRhdRu6nqyk-0HbNFSxEjVLtn"


def test_the_domain_separator_carries_no_version():
    """A version here would sort agents into lobbies by library version, which
    is the failure the lobby exists to remove — so there must be nothing to
    bump. Adding something to bump is the bug this catches."""
    from switchboard.rooms import LOBBY_INFO

    assert LOBBY_INFO == b"switchboard-lobby"
    assert not any(c.isdigit() for c in LOBBY_INFO.decode())


def test_two_installed_versions_derive_the_same_lobby():
    """States the actual guarantee rather than a constant: the same key gives
    the same room, whatever the code around it does."""
    from switchboard.rooms import lobby, lobby_token

    key = "abc" * 14 + "d"
    assert lobby(key).workspace_token == lobby_token(key)
    assert lobby_token(key) == lobby_token(key)
