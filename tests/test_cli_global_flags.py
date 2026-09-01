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


# --- unknown verbs ----------------------------------------------------------
#
# Two different agents, in two different sessions, typed `switchboard send`.
# Nothing in the docs or the skill ever suggested it — it is simply the word
# you reach for when you want to put a message somewhere and the verbs on
# offer are `say`, `dm` and `whisper`. argparse's reply was the full list of
# commands, which is a search rather than an answer, and it arrives at the
# moment the agent was trying to tell somebody something.


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("send", "say <channel>"),      # the observed one, twice
        ("send", "dm <agent>"),         # and it names both, because both are plausible
        ("post", "say <channel>"),
        ("tell", "dm <agent>"),
        ("roster", "agents"),           # the MCP tool name for the same thing
        ("board_set", "board set"),     # ...and one that is only spelled differently
    ],
)
def test_a_verb_we_do_not_have_says_which_one_to_use(typed, expected, capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args([typed, "hello"])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert f"no `{typed}` command" in err
    assert expected in err


def test_a_near_miss_falls_back_to_the_closest_command(capsys):
    """Nothing to look up for a typo, so the suggestion is computed."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["agens"])
    assert "Did you mean agents?" in capsys.readouterr().err


def test_something_unrecognisable_still_gets_argparse_s_own_answer(capsys):
    """No guess is better than a wrong guess: an argument that resembles
    nothing falls through to the full list, which is at least complete."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["zzzzzzzz"])
    assert "invalid choice" in capsys.readouterr().err
