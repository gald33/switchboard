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

from switchboard.cli import build_parser, main
from switchboard.testing import BASE_URL, hub

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
def cli_bound_to_a_real_hub(monkeypatch):
    """Route `switchboard.cli.Client` at an in-process hub, so `main()`
    exercises the real HTTP round trip without a subprocess."""
    import switchboard.cli as cli_module

    with hub(workspace=WS) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        yield handle


def test_board_set_get_delete_with_a_short_key_name(
    cli_bound_to_a_real_hub, capsys, monkeypatch
):
    """The exact repro from #16: a board key far under 32 bytes, no
    encryption configured, must round-trip cleanly through the CLI."""
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    prefix = ["--url", BASE_URL, "-w", WS, "--json"]

    code = main([*prefix, "board", "set", "my-board-key", "hello"])
    assert code == 0
    set_payload = json.loads(capsys.readouterr().out)
    assert set_payload["key"] == "my-board-key"

    code = main([*prefix, "board", "get", "my-board-key"])
    assert code == 0
    get_payload = json.loads(capsys.readouterr().out)
    assert get_payload["key"] == "my-board-key"
    assert get_payload["value"] == "hello"

    code = main(["--url", BASE_URL, "-w", WS, "-q", "board", "delete", "my-board-key"])
    assert code == 0


def test_board_key_does_not_leak_into_the_encryption_config(
    cli_bound_to_a_real_hub, capsys, monkeypatch
):
    """Before the fix, a bare board command with no --key still failed
    validation because the board entry name was read as the encryption key.
    """
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    code = main([
        "--url", BASE_URL, "-w", WS, "-q",
        "board", "set", "definitely-not-32-bytes", "x",
    ])
    assert code == 0
