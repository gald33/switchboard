"""`examples/web/switchboard-open.js`: the browser reader, against the real cipher.

A second implementation of a wire format is a second chance to be subtly
wrong, and this one cannot be checked by reading it: HKDF info strings, an
AAD, a padding frame and a key epoch either agree byte for byte or the room
looks unopenable, and "unopenable" is also what a wrong key looks like. So
these tests seal with `WorkspaceCipher` — the implementation every agent
already uses — and open in a browser, which is the only place the answer
counts.

Skipped, not failed, where no browser is installed: a contributor without
Playwright should still get a green suite, and CI has one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from switchboard.crypto import WorkspaceCipher, generate_key

READER = Path(__file__).resolve().parents[1] / "examples" / "web" / "switchboard-open.js"

WORKSPACE = "w_reader-tests"


#: How to run the reader. Node is the one that runs everywhere, including CI,
#: and a browser is the one that runs where the code will actually live. Both
#: when both exist: the risk this catches is a mistake in `switchboard-open.js`,
#: which either engine finds, and the browser additionally proves the file
#: really is loadable as a module in the place it ships to.
ENGINES = ("node", "browser")


def _node_opener(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("no node on PATH")
    harness = tmp_path_factory.mktemp("reader") / "open.mjs"
    harness.write_text(
        "import { RoomKey } from " + json.dumps(READER.as_uri()) + ";\n"
        "const input = JSON.parse(process.argv[2]);\n"
        "try {\n"
        "  const room = RoomKey.from(input.key, input.workspace);\n"
        "  const value = await room.open(input.envelope, input.context);\n"
        "  process.stdout.write(JSON.stringify({ok: true, value}));\n"
        "} catch (e) {\n"
        "  process.stdout.write(JSON.stringify({ok: false, error: String(e?.message ?? e)}));\n"
        "}\n"
    )

    def run(envelope, context, key, workspace=WORKSPACE):
        payload = json.dumps({"envelope": envelope, "context": context,
                              "key": key, "workspace": workspace})
        out = subprocess.run([node, str(harness), payload], capture_output=True,
                             text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    return run


def _browser_opener():
    api = pytest.importorskip("playwright.sync_api")
    manager = api.sync_playwright().start()
    launched = None
    for kwargs in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try:
            launched = manager.chromium.launch(**kwargs)
            break
        except Exception:
            continue
    if launched is None:
        manager.stop()
        pytest.skip("no browser binary available")
    page = launched.new_page()
    # A module needs an origin to be imported from, and `about:blank` has
    # none. Nothing is fetched over the network: the route is fulfilled here.
    page.route("https://reader.test/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><title>reader</title>"))
    page.goto("https://reader.test/")
    source = READER.read_text()

    def run(envelope, context, key, workspace=WORKSPACE):
        return page.evaluate(
            """async ({source, envelope, context, key, workspace}) => {
                const url = URL.createObjectURL(
                    new Blob([source], {type: "text/javascript"}));
                const mod = await import(url);
                try {
                    const room = mod.RoomKey.from(key, workspace);
                    return {ok: true, value: await room.open(envelope, context)};
                } catch (e) {
                    return {ok: false, error: String((e && e.message) || e)};
                }
            }""",
            {"source": source, "envelope": envelope, "context": context,
             "key": key, "workspace": workspace},
        )

    run.close = lambda: (launched.close(), manager.stop())
    return run


@pytest.fixture(scope="module", params=ENGINES)
def opener(request, tmp_path_factory):
    """`open(envelope, context, key, workspace)`, run by a real WebCrypto."""
    if request.param == "node":
        yield _node_opener(tmp_path_factory)
        return
    run = _browser_opener()
    yield run
    run.close()


def test_the_browser_opens_what_the_cipher_sealed(opener):
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE)
    sealed = cipher.seal({"text": "parser.py is mine", "n": 3}, "message.body")

    result = opener(sealed, "message.body", key)

    assert result["ok"], result.get("error")
    assert result["value"] == {"text": "parser.py is mine", "n": 3}


def test_a_text_field_arrives_serialized_and_still_opens(opener):
    """Names and lease notes travel as a string carrying an envelope, because
    the wire schema types them as strings."""
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE)
    sealed = cipher.seal_text("rewriting the lexer", "lease.note")

    assert isinstance(sealed, str)
    result = opener(sealed, "lease.note", key)

    assert result["ok"], result.get("error")
    assert result["value"] == "rewriting the lexer"


def test_a_rotated_epoch_opens_under_the_key_the_message_names(opener):
    """The subkey follows the writer, not the reader's clock — otherwise
    history stops opening every fifteen minutes."""
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE, epoch_period=900)
    sealed = cipher.seal("written under a rotated key", "message.body")

    assert sealed.get("e"), "expected this fixture to exercise a non-zero epoch"
    result = opener(sealed, "message.body", key)

    assert result["ok"], result.get("error")
    assert result["value"] == "written under a rotated key"


def test_epoch_zero_still_opens(opener):
    """Rotation off writes bytes a pre-epoch reader understands, and the
    browser has to be one of those readers."""
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE, epoch_period=0)
    sealed = cipher.seal("written with rotation off", "message.body")

    assert "e" not in sealed
    result = opener(sealed, "message.body", key)

    assert result["ok"], result.get("error")
    assert result["value"] == "written with rotation off"


def test_an_unpadded_writer_still_opens(opener):
    """Padding is detected from the payload rather than agreed in advance."""
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE, pad=False)
    sealed = cipher.seal("no padding here", "message.body")

    result = opener(sealed, "message.body", key)

    assert result["ok"], result.get("error")
    assert result["value"] == "no padding here"


def test_a_long_body_survives_the_padding_frame(opener):
    """The 4-byte length is big-endian, which is invisible until a payload is
    longer than 255 bytes."""
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE)
    body = "x" * 5000
    sealed = cipher.seal(body, "message.body")

    result = opener(sealed, "message.body", key)

    assert result["ok"], result.get("error")
    assert result["value"] == body


def test_the_wrong_context_does_not_open(opener):
    """The guarantee the AAD exists for: a hub cannot move a sealed value from
    one field to another and have it still open."""
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE)
    sealed = cipher.seal("a lease note", "lease.note")

    result = opener(sealed, "message.body", key)

    assert not result["ok"]


def test_the_wrong_key_does_not_open(opener):
    cipher = WorkspaceCipher.from_key(generate_key(), WORKSPACE)
    sealed = cipher.seal("theirs", "message.body")

    result = opener(sealed, "message.body", generate_key())

    assert not result["ok"]


def test_the_wrong_workspace_does_not_open(opener):
    """The workspace is bound into both the subkey and the AAD, so the same
    key in a different room is a different reader."""
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE)
    sealed = cipher.seal("ours", "message.body")

    result = opener(sealed, "message.body", key, workspace="w_somewhere-else")

    assert not result["ok"]


def test_plaintext_is_refused_rather_than_passed_through(opener):
    """Same rule as the Python reader: accepting an unsealed value would let a
    hub strip the encryption and be believed."""
    result = opener({"text": "not an envelope"}, "message.body", generate_key())

    assert not result["ok"]
    assert "encrypted" in result["error"]


@pytest.mark.parametrize("form", ["base64url", "hex"])
def test_a_key_is_accepted_in_the_shapes_people_paste(opener, form):
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE)
    sealed = cipher.seal("pasted", "message.body")
    import base64

    typed = key
    if form == "hex":
        typed = "hex:" + base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).hex()

    result = opener(sealed, "message.body", typed)

    assert result["ok"], result.get("error")
    assert result["value"] == "pasted"


def test_every_field_the_viewer_reads_round_trips(opener):
    """The contexts are not interchangeable and the viewer touches all of
    them, so a typo in one would show up as a single blank column."""
    key = generate_key()
    cipher = WorkspaceCipher.from_key(key, WORKSPACE)
    cases = {
        "agent.name": "parser:feat/lexer",
        "agent.branch": "feat/lexer",
        "agent.task": "wiring the tokenizer",
        "lease.note": "adding 0142",
    }
    for context, value in cases.items():
        result = opener(cipher.seal_text(value, context), context, key)
        assert result["ok"], f"{context}: {result.get('error')}"
        assert result["value"] == value

    board = cipher.seal({"taken": ["0141"], "next": "0142"}, "board.value")
    assert opener(board, "board.value", key)["value"] == json.loads(
        '{"taken": ["0141"], "next": "0142"}')
