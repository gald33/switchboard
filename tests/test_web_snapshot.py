"""The browser build's view assembly, run under node against a real hub.

`switchboard_viewer/web/switchboard-room.js` and `snapshot()` in `switchboard_viewer/viewer.py`
build the same shape from the same room, so one renderer can paint either.
`tests/test_web_page.py` already compares them — but only inside a browser,
and the browser tests skip wherever playwright is absent, **which includes
CI**. So the parity assertion existed and had never run there: a change to
this file could land behind a green CI having been checked by nobody.

That is not hypothetical. The board-key envelope in #119 edited exactly this
file and was verified only because playwright happened to be installed by
hand on the machine that wrote it.

Nothing in `switchboard-room.js` touches the DOM — no `document`, no
`window`, no `localStorage`; the page hands it a plain config object, and
`fetch` and `crypto.subtle` are both globals in node 18+. So the half that
drifts silently can be checked without a browser at all, in the same node
that already checks the cipher in `test_web_reader.py`.

What still needs a real browser, and stays in `test_web_page.py`: rendering,
the settings sheet, localStorage, and the cross-origin request a browser
refuses before sending.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from switchboard.client import Client
from switchboard.config import ClientConfig, ServerConfig
from switchboard.crypto import generate_key
from switchboard.store import Store

WEB = Path(__file__).resolve().parents[1] / "extras" / "viewer" / "switchboard_viewer" / "web"
ROOM_JS = WEB / "switchboard-room.js"
WORKSPACE = "w_node-snapshot"
KEY = generate_key()

_spec = importlib.util.spec_from_file_location(
    "example_viewer_snapshot",
    Path(__file__).resolve().parents[1] / "extras" / "viewer" / "switchboard_viewer" / "viewer.py")
viewer_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = viewer_app
_spec.loader.exec_module(viewer_app)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    """A hub on a real port: node cannot reach an in-process ASGI transport."""
    uvicorn = pytest.importorskip("uvicorn")
    from switchboard.server import create_app

    port = _free_port()
    store = Store(str(tmp_path_factory.mktemp("hub") / "hub.db"))
    app = create_app(ServerConfig(db_path=store.path), store=store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.05)
    else:  # pragma: no cover - only on a very slow machine
        pytest.fail("hub did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def room(hub):
    """A room with something in every panel, written by an ordinary client."""
    config = ClientConfig(url=hub, url_source="explicit",
                          workspace=WORKSPACE, key=KEY)
    agent = Client(config, agent_id="parser-agent", key=KEY)
    agent.register(name="parser:feat/lexer", kind="local", branch="feat/lexer",
                   task="wiring the tokenizer", channels=["build"])
    agent.post("build", "parser.py is mine for ~20 minutes")
    agent.post("build", {"suite": "pytest -q", "failed": 2})
    agent.acquire("src/parser.py", note="rewriting the lexer")
    agent.board_set("handoff/lexer", {"next": "escapes"})
    yield config
    agent.close()


@pytest.fixture(scope="module")
def in_node(tmp_path_factory):
    """Run `snapshot()` from the shipped module and hand back what it built."""
    node = shutil.which("node")
    if not node:
        pytest.skip("no node on PATH")
    harness = tmp_path_factory.mktemp("snapshot") / "snap.mjs"
    harness.write_text(
        "import { snapshot } from " + json.dumps(ROOM_JS.as_uri()) + ";\n"
        "const config = JSON.parse(process.argv[2]);\n"
        "process.stdout.write(JSON.stringify(await snapshot(config)));\n"
    )

    def run(config: dict) -> dict:
        out = subprocess.run([node, str(harness), json.dumps(config)],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    return run


def _config(room, *, key: str | None = KEY) -> dict:
    return {"url": room.url, "workspace": room.workspace, "token": "", "key": key or ""}


def test_node_and_python_build_the_same_view(in_node, room):
    """The parity that lets one renderer serve both, asserted where CI can see it."""
    from_node = in_node(_config(room))
    with Client(room, agent_id="viewer", key=KEY) as reader:
        from_python = viewer_app.snapshot(reader)

    assert [m["body"] for m in from_node["messages"]] == \
           [m["body"] for m in from_python["messages"]]
    assert [m["channel"] for m in from_node["messages"]] == \
           [m["channel"] for m in from_python["messages"]]
    assert [a["name"] for a in from_node["agents"]] == \
           [a["name"] for a in from_python["agents"]]
    assert [le["note"] for le in from_node["leases"]] == \
           [le["note"] for le in from_python["leases"]]
    assert [e["value"] for e in from_node["board"]] == \
           [e["value"] for e in from_python["board"]]
    assert [c["name"] for c in from_node["channels"]] == \
           [c["name"] for c in from_python["channels"]]
    # And the same field names, so the renderer cannot be reading something
    # only one of the two builders sets — the drift that renders as an empty
    # column rather than an error.
    assert set(from_node) == set(from_python)
    for panel in ("messages", "agents", "leases", "board", "channels"):
        assert set(from_node[panel][0]) == set(from_python[panel][0]), panel


def test_a_board_key_comes_back_readable_in_node_too(in_node, room):
    """The specific regression this file exists for.

    #119 made board keys travel sealed beside their value so a listing returns
    `handoff/lexer` rather than a blinded token. That change was to
    `switchboard-room.js`, and every test of it needed a browser.
    """
    from_node = in_node(_config(room))
    assert [e["key"] for e in from_node["board"]] == ["handoff/lexer"]


def test_a_channel_name_comes_back_readable_in_node_too(in_node, room):
    """The older half of the same mechanism, for the same reason."""
    from_node = in_node(_config(room))
    assert {m["channel"] for m in from_node["messages"]} == {"build"}


def test_without_the_key_the_room_reads_as_sealed_not_empty(in_node, room):
    """A viewer with no key must not render an encrypted room as a quiet one —
    the same distinction the Python client draws with `unreadable`."""
    from_node = in_node(_config(room, key=None))

    assert from_node["hub"]["encrypted"] is False
    assert [m["body"] for m in from_node["messages"]] == [None, None]
    assert all(m["sealed_body"] for m in from_node["messages"]), (
        "a body nobody could open must say so, not read as an empty one"
    )
