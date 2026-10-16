"""The pairing docs, held to the code and the surfaces they describe.

`docs/pairing.md` is mostly lifetimes and commands, and both rot quietly. A
lifetime that changes in `pairing.py` and not on the page is a reader who
trusts a code for fifteen minutes that is live for an hour. A command the page
shows that the parser refuses is a reader who assumes their install is
broken — or an agent that retries a flag that never existed. Neither is caught
by reading the doc, because a doc is not run; `test_invite.py` does the same
for every `switchboard <verb>` in the README, and this is that, for pairing.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from switchboard import invite, pairing
from switchboard.config import MAX_MESSAGE_TTL
from switchboard.testing import hub

ROOT = Path(__file__).resolve().parents[1]
PAIRING_DOC = ROOT / "docs" / "pairing.md"


def _fenced_lines(text: str) -> list[str]:
    """The lines inside code fences: the ones a reader copies and runs.

    Prose names commands without their arguments ("run `pair accept` in the
    cloud session"), which is right for prose and would read as a refused
    command here, so only fenced examples are held to the parser.
    """
    lines, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append(line)
    return lines


# --- what an older client does with a code ----------------------------------


def test_a_release_before_pairing_refuses_a_code_rather_than_joining_it(tmp_path):
    """docs/upgrading.md and docs/pairing.md both promise that a client from
    before pairing, handed a code, refuses it — "not a switchboard invite" —
    instead of joining the handshake room as an ordinary room.

    That promise is nothing but the prefix. Every release up to 2.5.0 opened
    `Invite.decode` by refusing whatever did not start with `swb1_`, and
    ignored fields it did not know in whatever did. So a pairing string that
    started with the invite prefix would be joined in the open by every older
    surface — `join`, the viewer, a hosted bridge — with the handshake secret
    sitting in each of them, and nothing would say so.
    """
    with hub() as h:
        started = pairing.start(
            h.client_config(workspace="team-laptop"),
            store=pairing.PairStore(tmp_path / "pairs", env={}),
            connect=h.client_class(), now=h.now,
        )
    for blob in (started.code, pairing.SECRET_PREFIX + "anything"):
        assert not blob.startswith(invite.PREFIX), blob[:12]
        # And this release says what it is, rather than the generic refusal.
        with pytest.raises(invite.InviteError, match="pair"):
            invite.Invite.decode(blob)


# --- the numbers on the page are the numbers in the code ---------------------

#: (what pairing.py has, what it must be, words docs/pairing.md uses for it).
#: Each pair of halves is checked separately, so a failure says which moved.
LIFETIMES = [
    ("DEFAULT_TTL", pairing.DEFAULT_TTL, 15 * 60,
     "redeemable for 15 minutes by default"),
    ("MIN_TTL", pairing.MIN_TTL, 60, "60 s to 12 h"),
    ("MAX_TTL", pairing.MAX_TTL, 12 * 3600, "60 s to 12 h"),
    ("COLLECT_WINDOW", pairing.COLLECT_WINDOW, 12 * 3600,
     "the code's expiry plus 12 hours"),
    ("the longest answer", pairing.MAX_TTL + pairing.COLLECT_WINDOW, MAX_MESSAGE_TTL,
     "so at most 24 hours"),
    ("the hello's margin", pairing.LOCAL_SLACK + pairing.SKEW, 17 * 60,
     "the code's lifetime plus 17 minutes"),
    ("SIDE_TTL", pairing.SIDE_TTL, 7 * 86400, "7 days, the board ceiling"),
    ("SIDE_REFRESH", pairing.SIDE_REFRESH, 86400, "rewritten at most once a day"),
    ("STALE_AFTER", pairing.STALE_AFTER, 6 * 86400,
     "has not seen the other for six days"),
    ("LAPSE_JUMP", pairing.LAPSE_JUMP, 2 ** 20, "2²⁰ generations ahead"),
]


@pytest.mark.parametrize("what, actual, expected, phrase", LIFETIMES,
                         ids=[row[0] for row in LIFETIMES])
def test_the_lifetimes_the_pairing_doc_states_are_the_codes(what, actual, expected,
                                                           phrase):
    assert actual == expected, (
        f"{what} is {actual} in pairing.py; docs/pairing.md says {phrase!r}. "
        "Change both, or neither.")
    page = " ".join(PAIRING_DOC.read_text().split())
    assert phrase in page, f"docs/pairing.md no longer says {phrase!r} for {what}"


# --- the commands on the page are commands the surfaces take ------------------


def _documented_invocations(text: str) -> list[tuple[str, list[str]]]:
    """Fenced `switchboard …` lines that pair, or reach a pair's room."""
    found = []
    for line in _fenced_lines(text):
        match = re.match(r"\s*(?:\$\s+)?switchboard\s+(.*)", line)
        if not match:
            continue
        try:
            tokens = shlex.split(match.group(1), comments=True)
        except ValueError:
            continue
        if tokens[:1] == ["pair"] or any(t.startswith(pairing.HANDLE_PREFIX)
                                        for t in tokens):
            found.append((line.strip(), tokens))
    return found


@pytest.mark.parametrize("doc", ["docs/pairing.md", "README.md"])
def test_every_pairing_command_the_docs_show_is_one_the_cli_takes(doc):
    """Parsed the way `main()` parses, flags and all — not only the verb.

    A walkthrough is where a reader copies from, and a flag on the page that
    the parser refuses fails on the reader's machine, as "unrecognized
    arguments", on the step that was meant to be the easy one.
    """
    from switchboard.cli import _escape_dash_leading_positionals, build_parser

    shown = _documented_invocations((ROOT / doc).read_text())
    assert shown, f"{doc} is listed here for showing pairing commands, and shows none"
    parser = build_parser()
    refused = []
    for line, tokens in shown:
        try:
            args, extras = parser.parse_known_args(
                _escape_dash_leading_positionals(parser, tokens))
        except SystemExit:
            refused.append(line)
            continue
        # `main()` folds stray words into a free-text message and nothing else.
        message = getattr(args, "message", None)
        if extras and not (isinstance(message, list)
                           and all(not e.startswith("-") for e in extras)):
            refused.append(line)
    assert not refused, f"{doc} shows commands the CLI refuses: {refused}"


def _documented_tool_calls(text: str) -> list[tuple[str, list[str], str]]:
    """Fenced MCP calls (`tool  arg="…" → result`) that pair, or reach a pair."""
    found = []
    for line in _fenced_lines(text):
        match = re.match(r"(?P<tool>[a-z][a-z_]*)\s+(?P<rest>.*)", line)
        if not match:
            continue
        call = match.group("rest").split("→")[0]
        if match.group("tool").startswith("pair_") or 'room="pair:' in call:
            found.append((match.group("tool"), re.findall(r"(\w+)=", call), line.strip()))
    return found


@pytest.mark.parametrize("doc", ["docs/pairing.md", "docs/claude-code.md"])
def test_every_pairing_tool_call_the_docs_show_is_one_the_bridge_takes(doc):
    """The MCP half of the same rule: the tool exists, and every argument the
    example passes is one its schema declares — `room` included, which the
    bridge adds to every room-scoped tool rather than to each by hand."""
    from switchboard.mcp_server import TOOLS

    schemas = {tool["name"]: tool["inputSchema"].get("properties", {}) for tool in TOOLS}
    shown = _documented_tool_calls((ROOT / doc).read_text())
    assert shown, f"{doc} is listed here for showing pairing tool calls, and shows none"
    wrong = []
    for tool, arguments, line in shown:
        if tool not in schemas:
            wrong.append(f"{line} (no tool {tool!r})")
            continue
        unknown = [a for a in arguments if a not in schemas[tool]]
        if unknown:
            wrong.append(f"{line} ({tool} takes no {unknown})")
    assert not wrong, f"{doc} shows tool calls the bridge refuses: {wrong}"
