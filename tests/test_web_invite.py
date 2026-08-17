"""`switchboard-room.js`'s invite reader, against the real encoder.

An invite exists because five facts that must match a peer's fail *silently*
when they don't — you are simply alone in a room that looks quiet. A second
decoder is a second chance to reintroduce exactly that: a page that reads four
of the five fields, or reads a v2 invite as if it were v1, joins somewhere
plausible and says nothing.

So these encode with `Invite` — the implementation the CLI already ships — and
decode in a real JavaScript engine, rather than asserting that the JS produces
something that looks about right.

Node rather than a browser: this is string handling with no WebCrypto in it,
`tests/test_web_page.py` covers the same function inside a browser through the
settings sheet, and node is the engine CI has on every runner.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from switchboard.invite import PREFIX, VERSION, Invite

ROOM_JS = (Path(__file__).resolve().parents[1] / "extras" / "viewer"
           / "switchboard_viewer" / "web" / "switchboard-room.js")


@pytest.fixture(scope="module")
def decode(tmp_path_factory):
    """`decode(blob)` → `{ok, value}` or `{ok: false, error}`, run by node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("no node on PATH")
    harness = tmp_path_factory.mktemp("invite") / "decode.mjs"
    harness.write_text(
        "import { decodeInvite } from " + json.dumps(ROOM_JS.as_uri()) + ";\n"
        "try {\n"
        "  process.stdout.write(JSON.stringify(\n"
        "    {ok: true, value: decodeInvite(process.argv[2])}));\n"
        "} catch (e) {\n"
        "  process.stdout.write(JSON.stringify(\n"
        "    {ok: false, error: String(e?.message ?? e)}));\n"
        "}\n"
    )

    def run(blob):
        out = subprocess.run([node, str(harness), blob], capture_output=True,
                             text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    return run


def test_everything_the_encoder_put_in_comes_back_out(decode):
    """The whole point: one string, five facts, no chance to mistype four."""
    blob = Invite(
        url="https://hub.example.com",
        workspace="w_QLpP1nCqRZ2s3t4u5v6w7",
        token="tok_abc123",
        key="9ikzU6p1vI4jCT9gn7gA9cpj-XkMxE7RK54ZRvNTl5E",
        note="parser room, ping me if the lexer breaks",
        probe="join/probe/deadbeef",
    ).encode()

    result = decode(blob)

    assert result["ok"], result.get("error")
    assert result["value"] == {
        "url": "https://hub.example.com",
        "workspace": "w_QLpP1nCqRZ2s3t4u5v6w7",
        "token": "tok_abc123",
        "key": "9ikzU6p1vI4jCT9gn7gA9cpj-XkMxE7RK54ZRvNTl5E",
        "note": "parser room, ping me if the lexer breaks",
        "probe": "join/probe/deadbeef",
    }


def test_an_invite_with_no_key_or_token_reads_as_having_none(decode):
    """A plaintext room on an open hub is a real shape, and "" is not the same
    answer as "absent" to a page deciding whether to try decrypting."""
    result = decode(Invite(url="http://127.0.0.1:8787", workspace="w_plain").encode())

    assert result["ok"], result.get("error")
    assert result["value"]["key"] is None
    assert result["value"]["token"] is None
    assert result["value"]["note"] == ""
    assert result["value"]["probe"] == ""


def test_padding_and_surrounding_whitespace_survive_a_paste(decode):
    """`encode` strips base64 padding so the string survives places that treat
    `=` specially, and a pasted line usually arrives with a newline on it."""
    blob = Invite(url="https://hub.example.com/", workspace="w_x").encode()
    assert not blob.endswith("=")

    result = decode(f"  {blob}\n")

    assert result["ok"], result.get("error")
    # And a trailing slash on the hub is dropped, or every request would carry
    # a double slash the moment a path is appended.
    assert result["value"]["url"] == "https://hub.example.com"


def test_something_that_is_not_an_invite_says_so_and_names_the_prefix(decode):
    result = decode("https://hub.example.com/w_x")

    assert result["ok"] is False
    assert PREFIX in result["error"]
    assert "switchboard invite" in result["error"]


def test_a_truncated_invite_fails_at_the_parse(decode):
    """The failure this whole mechanism exists to move earlier: a mangled
    invite must not half-apply and leave someone in a plausible wrong room."""
    blob = Invite(url="https://hub.example.com", workspace="w_x",
                  key="k" * 43).encode()

    result = decode(blob[:len(blob) // 2])

    assert result["ok"] is False
    assert "corrupt or truncated" in result["error"]


def test_a_future_version_is_refused_rather_than_half_read(decode):
    """An invite whose shape this page does not know might mean something
    different by every field. Refusing is the only safe reading."""
    payload = {"v": VERSION + 1, "u": "https://hub.example.com", "w": "w_x",
               "t": "", "k": "", "n": "", "p": ""}
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    result = decode(PREFIX + raw)

    assert result["ok"] is False
    assert f"version {VERSION + 1}" in result["error"]
    assert str(VERSION) in result["error"]


@pytest.mark.parametrize("missing", ["u", "w"])
def test_an_invite_missing_a_routing_field_is_refused(decode, missing):
    payload = {"v": VERSION, "u": "https://hub.example.com", "w": "w_x",
               "t": "", "k": "", "n": "", "p": ""}
    payload[missing] = ""
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    result = decode(PREFIX + raw)

    assert result["ok"] is False
    assert ("hub URL" if missing == "u" else "workspace") in result["error"]
