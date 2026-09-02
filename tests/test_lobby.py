"""The room every holder of one key already shares.

`init` gives each repo its own room, which is right for isolation and leaves
agents in different repos with nowhere to meet. The workaround is to agree on a
workspace name out of band and export it in every environment — a coordination
problem solved by coordination, and one that fails silently: an agent alone in
a room it chose by typo looks exactly like an agent in a quiet one.

A key already means "these agents are mine". So it can name the meeting place,
and then there is nothing to agree on and nothing to mistype.
"""

from __future__ import annotations

import argparse

import pytest

from switchboard import rooms
from switchboard.cli import _apply_lobby, build_parser

KEY = "0" * 43
OTHER = "1" * 43


def _args(**overrides):
    args = argparse.Namespace(lobby=True, invite=None, key=None)
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def test_one_key_is_one_lobby():
    """The property the whole idea rests on: nobody is told the room."""
    assert rooms.lobby(KEY).workspace == rooms.lobby(KEY).workspace


def test_another_key_is_another_lobby():
    assert rooms.lobby(KEY).workspace != rooms.lobby(OTHER).workspace


def test_the_lobby_is_not_a_well_known_name():
    """The reason it is derived rather than constant. A fixed token would hand
    every switchboard user on earth the same room identifier — contents still
    sealed, but the hub would hold one room with everybody's metadata in it,
    and what protects a room here is that its id is unguessable."""
    token = rooms.lobby_token(KEY)
    assert KEY not in token, "the token must not carry the key it came from"
    for guessable in ("lobby", "switchboard-lobby", rooms.LOBBY_INFO.decode()):
        assert rooms.workspace_for(guessable) != rooms.lobby(KEY).workspace


def test_the_flag_moves_the_room():
    config = _apply_lobby(_make_config_stub(workspace="w_repo", key=KEY), _args())
    assert config.workspace == rooms.lobby(KEY).workspace


def test_the_flag_outranks_a_workspace_from_the_environment():
    """Naming a room is the whole point of the flag, so an ambient workspace
    must not quietly win — the same reason `--invite` outranks it."""
    config = _apply_lobby(_make_config_stub(workspace="w_ambient", key=KEY), _args())
    assert config.workspace != "w_ambient"


def test_without_a_key_it_refuses_rather_than_guessing(tmp_path, monkeypatch):
    """There is no lobby to compute without one, and joining some other room
    instead would look exactly like a quiet lobby.

    From an empty directory on purpose: a checkout `init` has touched has a key
    in `.claude/settings.local.json`, and the flag will use it — reading it is
    the difference between working and refusing here, and the invocation does
    then really seal with it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_KEY", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        _apply_lobby(_make_config_stub(workspace="w_repo", key=None), _args())
    assert "derived from" in str(exit_info.value)


def test_a_lobby_and_an_invite_name_different_rooms():
    with pytest.raises(SystemExit) as exit_info:
        _apply_lobby(_make_config_stub(workspace="w", key=KEY),
                     _args(invite="swb1_something"))
    assert "pass one" in str(exit_info.value)


def test_the_flag_exists_on_every_command():
    """It is global for the same reason `--invite` is: `agents`, `say` and
    `listen` all need to be able to happen in the lobby."""
    args = build_parser().parse_args(["--lobby", "agents"])
    assert args.lobby is True


def _make_config_stub(*, workspace: str, key: str | None):
    from switchboard.config import ClientConfig
    return ClientConfig(url="https://hub.example.com", workspace=workspace, key=key)


def test_a_checkouts_own_key_is_enough(tmp_path, monkeypatch):
    """The one place this file reads a repo secret. Everywhere else that would
    make a command *claim* sealing it would not do; here the key is what the
    room is computed from, so reading it is the difference between joining the
    lobby and refusing to."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_KEY", raising=False)
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text('{"env": {"SWITCHBOARD_KEY": "%s"}}' % KEY)

    config = _apply_lobby(_make_config_stub(workspace="w_repo", key=None), _args())

    assert config.workspace == rooms.lobby(KEY).workspace
    assert config.key == KEY, "and it seals with what it computed the room from"


def test_the_hub_learns_nothing_from_a_rendezvous():
    """Pins the privacy claim so it cannot regress quietly.

    A rendezvous note names a topic, a role and a free-text want, and it is
    written to a shared blackboard — the obvious place for that to leak. The
    hub must hold it as a blinded key over a sealed value, learning that
    *something* was written in a room whose id it cannot connect to a key.
    """
    import json

    from switchboard import rendezvous
    from switchboard.crypto import generate_key
    from switchboard.testing import hub as make_hub

    key = generate_key()
    with make_hub(workspace="ws-probe", key=key) as handle:
        client = handle.client("alice", agent_id="alice")
        client.register(name="alice", kind="local", branch="feat/secret", meta={})
        note = rendezvous.Intent("alice", "design-review", "SECRET-WANT-STRING",
                                 0.0, 9e9, 0.0, rendezvous.OFFERING)
        client.board_set("coord/rendezvous/design-review/alice", note.as_json())

        with handle.app.state.store._tx() as conn:
            rows = conn.execute("SELECT key, value, updated_by FROM board").fetchall()
        assert len(rows) == 1, "the probe must not pass by finding nothing"
        stored = json.dumps([[str(x) for x in r] for r in rows])

    for secret in ("SECRET-WANT-STRING", "coord/rendezvous", "design-review",
                   "offer", "alice"):
        assert secret not in stored, f"hub can read {secret!r}"
