"""The browser build, against a real hub over a real socket.

`examples/web/switchboard-room.js` assembles the same view that `snapshot()`
in `examples/viewer.py` assembles, so that one renderer can paint either. Two
builders of one shape drift the moment nobody compares them, and the drift is
invisible: a field quietly missing renders as an empty column, not an error.
So this compares them — same hub, same room, same instant — rather than
asserting the JS produces something that merely looks plausible.

Everything here needs a real port, because a browser cannot reach an in-process
ASGI transport, and a real cross-origin request, because CORS is exactly the
part that is easy to get wrong and impossible to test any other way.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

from switchboard.client import Client
from switchboard.config import ClientConfig, ServerConfig
from switchboard.crypto import generate_key
from switchboard.store import Store

WEB = Path(__file__).resolve().parents[1] / "examples" / "web"
WORKSPACE = "w_browser-tests"
KEY = generate_key()

_spec = importlib.util.spec_from_file_location(
    "example_viewer_page", Path(__file__).resolve().parents[1] / "examples" / "viewer.py")
viewer_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = viewer_app
_spec.loader.exec_module(viewer_app)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    """A hub on a real port, with this page's origin allowed."""
    uvicorn = pytest.importorskip("uvicorn")
    from switchboard.server import create_app

    port, page_port = _free_port(), _free_port()
    store = Store(str(tmp_path_factory.mktemp("hub") / "hub.db"))
    app = create_app(
        ServerConfig(db_path=store.path, cors_origins=(f"http://127.0.0.1:{page_port}",)),
        store=store,
    )
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

    yield {"url": f"http://127.0.0.1:{port}", "page_port": page_port, "store": store}

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def page(hub):
    """The static build, served from an origin that is not the hub's."""
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB), **kwargs)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", hub["page_port"]), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{hub['page_port']}/"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    api = pytest.importorskip("playwright.sync_api")
    manager = api.sync_playwright().start()
    for kwargs in ({}, {"executable_path": "/opt/pw-browsers/chromium"}):
        try:
            launched = manager.chromium.launch(**kwargs)
            break
        except Exception:
            launched = None
    if launched is None:
        manager.stop()
        pytest.skip("no browser binary available")
    yield launched
    launched.close()
    manager.stop()


@pytest.fixture(scope="module")
def room(hub):
    """A room with something in every panel, written by an ordinary client."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
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


def open_page(browser, page_url, room_config, *, key=KEY):
    """Load the static build and enter a room through the settings sheet —
    the path a person actually takes, rather than seeding storage behind it."""
    tab = browser.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    tab.goto(page_url, wait_until="networkidle")
    tab.fill("#f-url", room_config.url)
    tab.fill("#f-workspace", room_config.workspace)
    tab.fill("#f-label", "parser")
    if key:
        tab.fill("#f-key", key)
    tab.click("#settings-save")
    tab.wait_for_function("document.querySelectorAll('.msg').length > 0", timeout=10_000)
    return tab, errors


def test_the_browser_builds_the_same_view_the_python_viewer_does(browser, page, room):
    """The parity that lets one renderer serve both."""
    tab, errors = open_page(browser, page, room)
    try:
        from_browser = tab.evaluate("""async () => {
            const mod = await import("./switchboard-room.js");
            const rooms = JSON.parse(localStorage.getItem("switchboard.rooms.v1"));
            return mod.snapshot(rooms[0]);
        }""")
    finally:
        tab.close()

    with Client(room, agent_id="viewer", key=KEY) as reader:
        from_python = viewer_app.snapshot(reader)

    assert errors == []
    assert [m["body"] for m in from_browser["messages"]] == \
           [m["body"] for m in from_python["messages"]]
    assert [m["channel"] for m in from_browser["messages"]] == \
           [m["channel"] for m in from_python["messages"]]
    assert [a["name"] for a in from_browser["agents"]] == \
           [a["name"] for a in from_python["agents"]]
    assert [a["task"] for a in from_browser["agents"]] == \
           [a["task"] for a in from_python["agents"]]
    assert [le["note"] for le in from_browser["leases"]] == \
           [le["note"] for le in from_python["leases"]]
    assert [e["value"] for e in from_browser["board"]] == \
           [e["value"] for e in from_python["board"]]
    assert [c["name"] for c in from_browser["channels"]] == \
           [c["name"] for c in from_python["channels"]]
    # And the same keys, top level and per message, so the renderer cannot be
    # reading a field one of the two builders does not set.
    assert set(from_browser) == set(from_python)
    assert set(from_browser["messages"][0]) == set(from_python["messages"][0])
    assert set(from_browser["agents"][0]) == set(from_python["agents"][0])
    assert set(from_browser["leases"][0]) == set(from_python["leases"][0])
    assert set(from_browser["board"][0]) == set(from_python["board"][0])
    assert set(from_browser["channels"][0]) == set(from_python["channels"][0])


def test_the_page_decrypts_in_the_browser_and_shows_it(browser, page, room):
    tab, errors = open_page(browser, page, room)
    try:
        assert errors == []
        assert "parser.py is mine for ~20 minutes" in tab.inner_text("#messages")
        assert "parser:feat/lexer" in tab.inner_text("#agents")
        assert "rewriting the lexer" in tab.inner_text("#leases")
        assert "escapes" in tab.inner_text("#board")
        # A board key travels sealed beside its value, so it comes back
        # readable — the same way a channel name does.
        assert "handoff/lexer" in tab.inner_text("#board")
        # A lease resource has no such carrier. The hub only ever saw a blinded
        # token, and the page says so rather than dressing it up as a name.
        assert "🔒" in tab.inner_text("#leases")
    finally:
        tab.close()


def test_a_room_entered_once_is_remembered(browser, page, room):
    tab, _ = open_page(browser, page, room)
    try:
        stored = json.loads(tab.evaluate(
            "localStorage.getItem('switchboard.rooms.v1')"))
        assert [r["workspace"] for r in stored] == [WORKSPACE]
        # Reloading picks it up without asking again.
        tab.reload(wait_until="networkidle")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert tab.query_selector("#settings").is_visible() is False
    finally:
        tab.close()


def test_the_key_never_leaves_the_browser(browser, page, room):
    """The property the whole design rests on: the page decrypts locally, so
    the key must appear in no request it makes."""
    tab = browser.new_page()
    sent: list[str] = []
    tab.on("request", lambda r: sent.append(
        f"{r.url} {r.headers} {r.post_data or ''}"))
    tab.goto(page, wait_until="networkidle")
    tab.fill("#f-url", room.url)
    tab.fill("#f-workspace", room.workspace)
    tab.fill("#f-key", KEY)
    tab.click("#settings-save")
    tab.wait_for_function("document.querySelectorAll('.msg').length > 0", timeout=10_000)
    tab.close()

    assert sent, "expected the page to have made requests at all"
    assert not any(KEY in one for one in sent)


def test_without_the_key_the_room_reads_as_sealed(browser, page, room):
    """A workspace id and a token get you the shape of the room and nothing
    else — which is what the hub itself sees, and is worth showing honestly."""
    tab, errors = open_page(browser, page, room, key="")
    try:
        assert errors == []
        assert "sealed" in tab.inner_text("#messages")
        assert "no key is set here" in tab.inner_text("#notes")
    finally:
        tab.close()


def test_the_page_opens_on_the_managed_hub(browser, page):
    """A reader arriving at the published page has one hub they have not had
    to set up, and typing a URL from memory is the first place to lose them."""
    from switchboard.config import MANAGED_HUB_URL

    tab = browser.new_page()
    tab.goto(page, wait_until="networkidle")
    try:
        # No rooms stored, so the sheet opens itself with the hub already in.
        tab.wait_for_function("document.querySelector('#settings').open", timeout=10_000)
        assert tab.input_value("#f-url") == MANAGED_HUB_URL
    finally:
        tab.close()


def test_the_managed_hub_token_is_filled_in_and_a_private_hub_gets_none(browser, page):
    """The published token is prefilled because nobody types it; a hub someone
    else runs must never inherit it."""
    from switchboard.config import MANAGED_HUB_TOKEN, MANAGED_HUB_URL

    tab = browser.new_page()
    # Nothing here should reach a network, managed hub included: this is about
    # what gets stored, and the reads that follow are not the subject.
    tab.route("**/*", lambda route: route.abort()
              if "127.0.0.1" not in route.request.url else route.continue_())
    tab.goto(page, wait_until="networkidle")
    try:
        tab.fill("#f-url", MANAGED_HUB_URL)
        tab.fill("#f-workspace", "w_somebody")
        tab.click("#settings-save")
        tab.click("#settings-open")  # saving closes the sheet
        tab.fill("#f-url", "https://hub.example.invalid")
        tab.fill("#f-workspace", "w_theirs")
        tab.click("#settings-save")
        stored = json.loads(tab.evaluate("localStorage.getItem('switchboard.rooms.v1')"))
    finally:
        tab.close()

    by_url = {r["url"]: r.get("token", "") for r in stored}
    assert by_url[MANAGED_HUB_URL] == MANAGED_HUB_TOKEN
    assert by_url["https://hub.example.invalid"] == ""


def test_a_hub_that_does_not_allow_this_origin_says_which_problem_that_is(
    browser, page, room, hub,
):
    """A browser refuses a cross-origin read without telling the page why, so
    the page must not guess — and must name CORS, because that is the cause a
    reader cannot otherwise discover."""
    tab = browser.new_page()
    tab.goto(page, wait_until="networkidle")
    tab.fill("#f-url", "http://127.0.0.1:9")  # nothing is listening
    tab.fill("#f-workspace", WORKSPACE)
    tab.click("#settings-save")
    tab.wait_for_function(
        "document.querySelector('#notes').innerText.length > 0", timeout=10_000)
    note = tab.inner_text("#notes")
    tab.close()

    assert "cannot reach the hub" in note
    assert "SWITCHBOARD_CORS_ORIGINS" in note
