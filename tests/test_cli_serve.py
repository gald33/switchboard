"""Tests for `switchboard serve`'s --keys-file wiring.

Only the paths that return before `uvicorn.run` actually binds a port are
exercised directly through `main([...])`; the happy path (a valid keys file
successfully starting the server) is covered by patching `uvicorn.run` so the
test doesn't block forever on a real server.
"""

from __future__ import annotations

import json

from switchboard.cli import main


def test_token_and_keys_file_together_is_rejected(capsys):
    # --token is a global flag and must precede the subcommand; --keys-file is
    # serve-specific and follows it.
    code = main(["--token", "tok", "serve", "--keys-file", "/nonexistent.json"])
    assert code != 0
    assert "mutually exclusive" in capsys.readouterr().err


def test_missing_keys_file_is_a_clean_error(tmp_path, capsys):
    missing = tmp_path / "nope.json"
    code = main(["serve", "--keys-file", str(missing)])
    assert code != 0
    assert "error" in capsys.readouterr().err.lower()


def test_malformed_keys_file_is_a_clean_error(tmp_path, capsys):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"tok": {"label": "no workspaces"}}))
    code = main(["serve", "--keys-file", str(path)])
    assert code != 0
    assert "workspaces" in capsys.readouterr().err


def test_valid_keys_file_starts_the_server_with_a_static_resolver(tmp_path, capsys, monkeypatch):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"tok-acme": {"workspaces": ["acme/app"], "label": "acme"}}))

    captured = {}

    def fake_run(app, **kwargs):
        # cmd_serve builds the app (and therefore the resolver) before calling
        # uvicorn.run — capture it here instead of actually binding a port.
        captured["app"] = app

    monkeypatch.setattr("uvicorn.run", fake_run)
    code = main(["serve", "--keys-file", str(path), "--db", str(tmp_path / "s.db")])
    assert code == 0
    assert "app" in captured
    assert "loaded 1 scoped key(s)" in capsys.readouterr().err


# --- --self-issued-keys: the three server auth modes are mutually exclusive -


def test_token_and_self_issued_keys_together_is_rejected(capsys):
    code = main(["--token", "tok", "serve", "--self-issued-keys"])
    assert code != 0
    assert "mutually exclusive" in capsys.readouterr().err


def test_keys_file_and_self_issued_keys_together_is_rejected(tmp_path, capsys):
    code = main(["serve", "--keys-file", "/nonexistent.json", "--self-issued-keys"])
    assert code != 0
    assert "mutually exclusive" in capsys.readouterr().err


def test_self_issued_keys_starts_the_server_with_that_resolver(tmp_path, capsys, monkeypatch):
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app

    monkeypatch.setattr("uvicorn.run", fake_run)
    code = main(["serve", "--self-issued-keys", "--db", str(tmp_path / "s.db")])
    assert code == 0
    assert "app" in captured
    assert "self-issued tokens enabled" in capsys.readouterr().err
    assert isinstance(captured["app"].state.config, object)  # sanity: app actually built


# --- naming: "key" is the secret that is never sent, "token" is the one that is


def test_the_old_flag_spellings_still_work():
    """`--keys-file` and `--self-issued-keys` are in released docs, in people's
    compose files and in CI config. The rename is for readers; nobody's
    deployment should break for it."""
    from switchboard.cli import build_parser

    parser = build_parser()
    old = parser.parse_args(["serve", "--self-issued-keys"])
    new = parser.parse_args(["serve", "--self-issued-tokens"])
    assert old.self_issued_keys is True and new.self_issued_keys is True

    old = parser.parse_args(["serve", "--keys-file", "/tmp/k.json"])
    new = parser.parse_args(["serve", "--tokens-file", "/tmp/k.json"])
    assert old.keys_file == new.keys_file == "/tmp/k.json"


def test_register_key_still_resolves_to_the_same_command():
    from switchboard.cli import build_parser, cmd_register_key

    parser = build_parser()
    for name in ("register-token", "register-key"):
        args = parser.parse_args([name, "--workspace", "w_x"])
        assert args.func is cmd_register_key, name


def test_the_two_secrets_are_described_as_opposites():
    # The whole point of the rename: one is sent, one never is. If a help
    # string ever stops saying which, this is the failure worth catching.
    from switchboard.cli import build_parser

    sub = build_parser()._subparsers._group_actions[0].choices
    def flat(parser):
        # argparse hard-wraps help, so match on unwrapped text
        return " ".join(parser.format_help().split())

    assert "never reaches the hub" in flat(sub["keygen"])
    assert "sent to the hub on every request" in flat(sub["register-token"])
