"""Regression tests for #16.

`switchboard board get/set/delete`'s positional `key` (the board entry name)
used the implicit argparse dest `"key"`, colliding with the global `--key`
(workspace encryption key) on the same `Namespace` — a board entry name would
overwrite, or be overwritten by, the encryption key, and a board key under 32
bytes failed with a bogus "workspace key must be at least 32 bytes" error
even with no encryption configured at all.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from switchboard.cli import build_parser, main
from switchboard.client import Client
from switchboard.config import ServerConfig
from switchboard.server import create_app
from switchboard.store import Store

WS = "board-cli-ws"


def test_key_and_board_key_are_independent_in_parsed_args():
    """The fast, direct check: parsing must never let one clobber the other,
    in either direction."""
    parser = build_parser()
    encryption_key = "x" * 32

    args = parser.parse_args(["--key", encryption_key, "board", "get", "short-name"])
    assert args.key == encryption_key
    assert args.board_key == "short-name"

    # And the reverse order/combination — a board key that itself looks
    # nothing like an encryption key must not disturb --key either.
    args = parser.parse_args([
        "--key", encryption_key, "board", "set", "another-short-name", "value",
    ])
    assert args.key == encryption_key
    assert args.board_key == "another-short-name"


@pytest.fixture
def cli_bound_to_a_real_hub(tmp_path, monkeypatch):
    """Route `switchboard.cli.Client` through an in-process TestClient, the
    same swap `test_mcp.py` uses, so `main()` exercises the real HTTP round
    trip without a subprocess."""
    store = Store(str(tmp_path / "board.db"))
    app = create_app(ServerConfig(db_path=str(tmp_path / "board.db")), store=store)
    with TestClient(app) as http:

        class _Bound(Client):
            def __init__(self, config=None, *, agent_id=None, timeout=40.0, key=None):
                super().__init__(config, agent_id=agent_id, timeout=timeout, key=key)
                self._http.close()
                self._http = http

            def close(self) -> None:
                pass  # shared across calls in a test; the fixture closes it once

        import switchboard.cli as cli_module

        monkeypatch.setattr(cli_module, "Client", _Bound)
        yield
    store.close()


def test_board_set_get_delete_with_a_short_key_name(
    cli_bound_to_a_real_hub, capsys, monkeypatch
):
    """The exact repro from #16: a board key far under 32 bytes, no
    encryption configured, must round-trip cleanly through the CLI."""
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    prefix = ["--url", "http://testserver", "-w", WS, "--json"]

    code = main([*prefix, "board", "set", "my-board-key", "hello"])
    assert code == 0
    set_payload = json.loads(capsys.readouterr().out)
    assert set_payload["key"] == "my-board-key"

    code = main([*prefix, "board", "get", "my-board-key"])
    assert code == 0
    get_payload = json.loads(capsys.readouterr().out)
    assert get_payload["key"] == "my-board-key"
    assert get_payload["value"] == "hello"

    code = main(["--url", "http://testserver", "-w", WS, "-q", "board", "delete", "my-board-key"])
    assert code == 0


def test_board_key_does_not_leak_into_the_encryption_config(
    cli_bound_to_a_real_hub, capsys, monkeypatch
):
    """Before the fix, a bare board command with no --key still failed
    validation because the board entry name was read as the encryption key.
    """
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    code = main([
        "--url", "http://testserver", "-w", WS, "-q",
        "board", "set", "definitely-not-32-bytes", "x",
    ])
    assert code == 0
