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
