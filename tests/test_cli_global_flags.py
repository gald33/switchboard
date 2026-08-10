"""Global flags work on either side of the subcommand.

`switchboard say general hi --json` is what everyone reaches for first, and
argparse's answer was "unrecognized arguments". It is not a cosmetic problem:
an agent coordinating over the CLI ran `announce --json`, the command failed,
so it never registered, and it spent the opening minutes of a two-agent
exercise invisible on `roster`. Nothing in the error said the flag was merely
in the wrong place.

`init` already accepted the trailing form through separate dests it reads by
hand; these cover the same courtesy for every other command, via suppressed
defaults instead, so no command has to read two dests.
"""

from __future__ import annotations

import pytest

from switchboard.cli import build_parser

# One representative per shape: a plain command, one taking positionals, an
# aliased one, and a nested subcommand -- that last is where the flag lands
# on a command that has subcommands of its own.
COMMANDS: list[list[str]] = [
    ["agents"],
    ["whoami"],
    ["announce"],
    ["register"],
    ["say", "general", "hello"],
    ["dm", "someone", "hello"],
    ["inbox"],
    ["checkin"],
    ["claim", "src/foo.py"],
    ["release", "src/foo.py"],
    ["history", "general"],
    ["timing"],
    ["board", "set", "k", "v"],
    ["board", "get", "k"],
    ["board", "list"],
]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: "-".join(c[:2]))
def test_json_is_accepted_after_the_subcommand(command):
    assert build_parser().parse_args([*command, "--json"]).json is True


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: "-".join(c[:2]))
def test_json_is_still_accepted_before_the_subcommand(command):
    """The half that already worked, and the half a naive fix breaks: a
    subparser option sharing a dest overwrites the parent's value with its
    own default whenever the flag is not repeated after the subcommand."""
    assert build_parser().parse_args(["--json", *command]).json is True


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: "-".join(c[:2]))
def test_connection_options_are_accepted_after_the_subcommand(command):
    args = build_parser().parse_args(
        [*command, "--url", "http://example", "-w", "ws", "--token", "tok"]
    )
    assert (args.url, args.workspace, args.token) == ("http://example", "ws", "tok")


def test_quiet_works_in_both_positions():
    assert build_parser().parse_args(["-q", "release", "r"]).quiet is True
    assert build_parser().parse_args(["release", "r", "-q"]).quiet is True


def test_a_trailing_flag_does_not_erase_a_leading_one():
    """Both positions at once must not cancel out -- the failure mode where
    the parent parses `-w early` and the subparser's default resets it."""
    args = build_parser().parse_args(["-w", "early", "say", "c", "m", "--json"])
    assert args.workspace == "early"
    assert args.json is True


def test_encryption_key_survives_in_both_positions():
    """`--key` is the one global that collides with a positional name: the
    board commands take a `key` argument, parked on `board_key` (#16). The
    encryption key must not be confused with it from either side."""
    key = "K" * 32
    leading = build_parser().parse_args(["--key", key, "board", "get", "entry"])
    trailing = build_parser().parse_args(["board", "get", "entry", "--key", key])
    for args in (leading, trailing):
        assert args.key == key
        assert args.board_key == "entry"


def test_init_keeps_its_own_handling():
    """`init` declares these itself with separate dests and reads both, so it
    is deliberately skipped. Re-declaring the same strings on it would be an
    argparse conflict, and this pins that it was not swept in."""
    parser = build_parser()
    assert parser.parse_args(["init", "-w", "foo"]).init_workspace == "foo"
    assert parser.parse_args(["-w", "foo", "init"]).workspace == "foo"


def test_the_help_text_does_not_advertise_the_duplicates(capsys):
    """Suppressed so `switchboard say --help` still reads as the say options,
    rather than repeating the global list under every command."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["say", "--help"])
    rendered = capsys.readouterr().out
    assert "--execution-class" in rendered  # a real say option is documented
    assert "--token" not in rendered  # the borrowed global is not
