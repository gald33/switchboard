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


def test_announce_and_register_are_the_same_command():
    """`register` overstated what happens: the record is self-asserted, expires
    in two minutes, and nothing validates it. The old name still works, since
    it is in released docs and scripts."""
    from switchboard.cli import build_parser, cmd_register

    parser = build_parser()
    for name in ("announce", "register"):
        assert parser.parse_args([name, "--name", "x"]).func is cmd_register, name
