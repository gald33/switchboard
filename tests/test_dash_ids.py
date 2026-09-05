"""An id the hub minted, that the CLI would not accept.

Agent ids are blinded to base64url, whose alphabet includes `-`, so about one
id in sixty-six begins with one. `switchboard dm -yLAoQ63KcM86gn3wjgD3w hi`
then died on `unrecognized arguments` — and that id came from `switchboard
agents`, which the skill tells every agent to copy from. The tool refused a
value it generated and told you to use.

It surfaced as a 1-in-66 CI failure rather than as a bug report, which is the
part worth dwelling on: a peer you cannot address is indistinguishable from a
peer who is not there, and the documented response to a peer who is not there
is to stop waiting and do something else. Nobody would have filed this.

`--` always worked. These are about not having to know that.
"""

from __future__ import annotations

import string

import pytest

from switchboard.cli import _escape_dash_leading_positionals as escape
from switchboard.cli import build_parser, main
from switchboard.crypto import WorkspaceCipher, generate_key
from switchboard.testing import BASE_URL, hub

WS = "dash-ws"


@pytest.fixture
def parser():
    return build_parser()


#: base64url, which is what the premise of this whole file rests on.
ALPHABET = set(string.ascii_letters + string.digits + "-_")


def test_a_blinded_id_is_base64url_which_is_why_one_can_begin_with_a_dash():
    """The premise, measured rather than asserted. If blinding ever stopped
    using base64url this whole file could go — and it should not quietly stay
    as decoration that tests nothing.

    Read across the whole of each id rather than its first character alone.
    The earlier version drew 400 ids and asserted that at least one *began*
    with `-`: the same coin, flipped 400 times, so about one run in five
    hundred saw no dash and went red. It cost a release PR a cycle doing
    exactly that, on a diff that changed only version strings.

    The same sample read whole is thousands of draws instead of hundreds, and
    the alphabet check does not depend on luck at all — it fails the moment
    blinding emits a character base64url does not have, which is the change
    that would actually make this file pointless.
    """
    ids = [WorkspaceCipher.from_key(generate_key(), "w").blind(f"a{i}", "agent")
           for i in range(200)]
    stray = set("".join(ids)) - ALPHABET
    assert not stray, f"blinded ids left the base64url alphabet: {sorted(stray)}"
    assert "-" in set("".join(ids)), (
        "no '-' anywhere in 200 blinded ids — is the alphabet still base64url?")


@pytest.mark.parametrize("argv, expected", [
    (["dm", "-yLAoQ63KcM86gn3wjgD3w", "hi"],
     ["dm", "--", "-yLAoQ63KcM86gn3wjgD3w", "hi"]),
    (["board", "get", "-abc"], ["board", "get", "--", "-abc"]),
    (["claim", "-abc"], ["claim", "--", "-abc"]),
    # Global flags before the subcommand, which is where the first attempt at
    # this stopped walking and silently did nothing.
    (["--json", "claim", "-abc"], ["--json", "claim", "--", "-abc"]),
    (["--url", "http://x", "-w", "ws", "dm", "-abc", "hi"],
     ["--url", "http://x", "-w", "ws", "dm", "--", "-abc", "hi"]),
    # A workspace whose name spells a subcommand. The value of a flag is not a
    # subcommand, and reading it as one would escape the wrong token.
    (["-w", "claim", "dm", "-abc", "hi"], ["-w", "claim", "dm", "--", "-abc", "hi"]),
])
def test_a_dash_leading_positional_is_escaped(parser, argv, expected):
    assert escape(parser, argv) == expected


@pytest.mark.parametrize("argv", [
    # A real flag, and a value that merely looks like one. `-m` takes it, and
    # argparse already reads a token with a space as a value rather than a
    # flag, so there is nothing here to repair.
    ["claim", "x", "-m", "-a dashed note"],
    # Already joined by hand.
    ["board", "list", "--prefix=-weird"],
    # `-` on its own is the stdin sentinel, not a positional to protect.
    ["say", "chan", "-"],
    ["dm", "bob", "-"],
    ["board", "set", "k", "-"],
    # Nothing to do.
    ["dm", "bob", "hi"], ["agents"], ["dm", "-h"], ["--help"], ["--version"], [],
])
def test_everything_else_is_left_exactly_alone(parser, argv):
    """The risk of this fix is not that it fails to fire — it is that it fires
    where it should not and eats a flag."""
    assert escape(parser, argv) == argv


def test_an_explicit_double_dash_is_never_second_guessed(parser):
    argv = ["dm", "--", "-abc", "-still-a-body"]
    assert escape(parser, argv) == argv


def test_a_dm_reaches_a_peer_whose_id_begins_with_a_dash(capsys, monkeypatch):
    """End to end, with the id forced rather than waited for. The bug is one
    run in sixty-six, so a test that used a random id would report success
    sixty-five times for every time it checked anything."""
    import switchboard.cli as cli_module

    key = generate_key()
    with hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        monkeypatch.setenv("SWITCHBOARD_KEY", key)

        # Find a peer name whose blinded id actually starts with '-', rather
        # than asserting about a hypothetical one. Deep enough that not
        # finding one is impossible rather than unlucky: at one id in
        # sixty-six, 500 candidates came up empty about one run in twenty-five
        # hundred, and this file exists because of a bug that hid at exactly
        # that rate.
        cipher = WorkspaceCipher.from_key(key, WS)
        name = next((n for n in (f"bob{i}" for i in range(5000))
                     if cipher.blind(n, "agent").startswith("-")), None)
        assert name, "no blinded id in 5000 began with '-'"
        bob = handle.client(name, agent_id=name)
        bob.register(name=name)
        assert bob.agent_id.startswith("-")

        monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "alice")
        assert main(["--url", BASE_URL, "-w", WS, "dm", bob.agent_id, "hello"]) == 0

    err = capsys.readouterr().err
    assert "unrecognized arguments" not in err
    assert "nobody on the roster" not in err, "escaped, but addressed to the wrong id"


@pytest.mark.parametrize("argv, expected", [
    # The bug this pins: an agent id is base64url and may begin with `-`, and
    # `--from` is how a parked receiver names the sender it will run for. The
    # id comes from `switchboard agents` — the tool refusing a value it
    # generated, again, one time in sixty-six.
    (["session", "receive", "--from", "-yLAoQ63KcM86gn3wjgD3w"],
     ["session", "receive", "--from=-yLAoQ63KcM86gn3wjgD3w"]),
    # A board key is blinded too, so `--prefix` had it as well.
    (["board", "list", "--prefix", "-weird"], ["board", "list", "--prefix=-weird"]),
    # Global flags before the subcommand are walked over the same way.
    (["--url", "http://x", "session", "receive", "--from", "-abc"],
     ["--url", "http://x", "session", "receive", "--from=-abc"]),
    # A short option reads `=` as part of the value, so it takes the
    # concatenated form instead — `-m=-x` would set the note to `=-x`.
    (["claim", "x", "-m", "-dashed"], ["claim", "x", "-m-dashed"]),
])
def test_a_dash_leading_option_value_is_joined_to_its_flag(parser, argv, expected):
    """The other half of the same bug. `--` cannot help here: it protects
    positionals, and this is an option's value."""
    assert escape(parser, argv) == expected


@pytest.mark.parametrize("argv", [
    ["session", "receive", "--from", "-yLAoQ63KcM86gn3wjgD3w"],
    ["board", "list", "--prefix", "-weird"],
    ["claim", "x", "-m", "-dashed"],
])
def test_the_joined_form_is_one_argparse_accepts(parser, argv):
    """The rewrite is only worth anything if argparse then takes it — and the
    two joined forms are not interchangeable, which is the trap. Parsed here
    rather than eyeballed, because `-m=-x` also parses and means the wrong
    thing.
    """
    dest = {"--from": "from_agents", "--prefix": "prefix", "-m": "note"}[argv[-2]]
    got = vars(parser.parse_args(escape(parser, argv)))[dest]
    # `--from` is repeatable, so it collects into a list.
    assert (got == [argv[-1]] if isinstance(got, list) else got == argv[-1])
