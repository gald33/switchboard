"""The browser build, against a real hub over a real socket.

`switchboard_viewer/web/switchboard-room.js` assembles the same view that `snapshot()`
in `switchboard_viewer/viewer.py` assembles, so that one renderer can paint either. Two
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

WEB = Path(__file__).resolve().parents[1] / "extras" / "viewer" / "switchboard_viewer" / "web"
WORKSPACE = "w_browser-tests"
KEY = generate_key()

_spec = importlib.util.spec_from_file_location(
    "example_viewer_page",
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


@pytest.fixture(scope="module")
def long_room(hub):
    """A room with more history than one read can carry, in its own workspace
    so the shorter tests are not paging through it."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-tail", key=KEY)
    agent = Client(config, agent_id="chatty-agent", key=KEY)
    agent.register(name="chatty", kind="local", channels=["build"])
    agent.post("asides", "an aside nobody followed up on")
    for n in range(120):
        agent.post("build", f"line {n}")
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


def test_the_browser_shows_the_end_of_a_long_room_not_its_beginning(
    browser, page, long_room,
):
    """The bug this pins: the hub reads forward, so `since=0` is the opening
    `limit` messages of the room and stays that way however long the room
    runs. On anything busy the page then showed a window half an hour stale
    and never moved it — which reads as a board that has gone quiet. Both
    builders page to the tail, and to the same one.
    """
    tab, errors = open_page(browser, page, long_room)
    try:
        from_browser = tab.evaluate("""async () => {
            const mod = await import("./switchboard-room.js");
            const rooms = JSON.parse(localStorage.getItem("switchboard.rooms.v1"));
            return mod.snapshot(rooms[0]);
        }""")
    finally:
        tab.close()

    with Client(long_room, agent_id="viewer", key=KEY) as reader:
        from_python = viewer_app.snapshot(reader)

    assert errors == []
    shown = [m["body"] for m in from_browser["messages"] if m["channel"] == "build"]
    assert shown == [f"line {n}" for n in range(70, 120)]
    # And the quiet channel is not stepped over on the way there.
    assert "an aside nobody followed up on" in \
           [m["body"] for m in from_browser["messages"]]
    assert [m["body"] for m in from_browser["messages"]] == \
           [m["body"] for m in from_python["messages"]]


def test_the_empty_document_tells_an_agent_where_the_door_is(page):
    """What arrives without a browser, which is how an agent arrives.

    Every read this page makes happens in JavaScript, so a fetched document
    carries no room — and "the board is empty" is the wrong conclusion twice:
    nothing was read, and a viewer is not where an agent belongs anyway. So
    the empty document names the two ways in, and carries nothing that has to
    stay secret.
    """
    import urllib.request

    with urllib.request.urlopen(page, timeout=10) as response:
        html = response.read().decode()

    served = html[html.index("<noscript>"):html.index("</noscript>")]
    # Both ways in, in the order an agent should try them.
    assert served.index("join_room") < served.index("switchboard join")
    assert "switchboard-mcp" in served and "pip install agent-switchboard" in served
    assert "help" in served
    # Whole, never reassembled: each of the four fields fails silently alone.
    assert "swb1_" in served
    # And nothing that has to stay in the fragment. The block is static markup
    # precisely so that no invite can ever be reflected back out of it.
    assert KEY not in html
    assert "<noscript" in html and html.index("<noscript") < html.index("<header>")


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


def test_a_tab_can_be_closed_and_the_room_is_forgotten(browser, page, room):
    """The × on a tab is the same act as "forget" in the settings sheet: the
    room leaves storage, so a reload does not bring it back."""
    tab, errors = open_page(browser, page, room)
    try:
        # A second room, so there is a tab strip at all — one room needs no
        # switcher, and the page draws none.
        tab.click("#settings-open")
        tab.fill("#f-url", room.url)
        tab.fill("#f-workspace", room.workspace + "-other")
        tab.fill("#f-label", "other")
        tab.fill("#f-key", KEY)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.room').length === 2",
                              timeout=10_000)

        tab.click('.room[data-room$="-other"] .close')
        tab.wait_for_function("document.querySelectorAll('.room').length === 0",
                              timeout=10_000)
        stored = json.loads(tab.evaluate(
            "localStorage.getItem('switchboard.rooms.v1')"))
        assert [r["workspace"] for r in stored] == [WORKSPACE]

        tab.reload(wait_until="networkidle")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert tab.query_selector(".room") is None
        assert errors == []
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


def test_a_board_value_can_be_taken_out_of_the_page_as_a_file(browser, page, room):
    """The escape hatch for a reader with no checkout and no CLI.

    The page already holds the plaintext — it fetched the entry and opened it
    with the room key, which is why a sealed value is legible here at all — so
    handing it over as a file asks the hub for nothing new. What it buys is a
    session capsule collectable by a human with only a browser, which is
    exactly the case where no tooling is available to collect it.

    Pinned end to end rather than by reading the handler, because the part
    that breaks silently is the browser's: a blob URL that is revoked before
    the click lands downloads nothing, and nothing is also what a working
    button looks like from the outside.
    """
    tab, errors = open_page(browser, page, room)
    try:
        tab.click("#board-wide")               # the detail pane, where save lives
        tab.click('[data-board="handoff/lexer"]')
        with tab.expect_download() as download:
            tab.click('[data-save="handoff/lexer"]')
        saved = download.value
        # Named after the key, so a directory of these says what each one is,
        # and `.json` because a structured value is what a board carries.
        assert saved.suggested_filename == "handoff-lexer.json"
        assert json.loads(Path(saved.path()).read_text()) == {"next": "escapes"}
        assert errors == []
    finally:
        tab.close()


def test_nothing_offers_to_save_a_value_the_page_could_not_open(browser, page, room):
    """No key, no plaintext — and a button that would write out the envelope is
    worse than no button, because the file would look like the value.

    The trap this pins: a board entry's `sealed` flag reports whether its
    *key* stayed a blinded token, which needs a room key to establish, so it
    is false for every entry in a room the page cannot read. Offering to save
    has to ask whether the value came back, not whether it looks sealed.
    """
    tab, errors = open_page(browser, page, room, key="")
    try:
        tab.click("#board-wide")
        assert tab.query_selector("#board-detail") is not None
        assert tab.query_selector("[data-save]") is None
        assert errors == []
    finally:
        tab.close()


def test_the_conversation_follows_the_newest_message_unless_you_scrolled_up(
    browser, page, room, hub,
):
    """The two halves of a chat pane that repaints itself.

    Pinned to the bottom, new traffic has to arrive in view — otherwise a
    watcher has to scroll after every refresh. Scrolled up, the same repaint
    must not move them, because being yanked away mid-sentence is worse than a
    view that never followed at all. The pill is how the second case stays
    survivable: it says traffic arrived without moving anything.
    """
    tab, errors = open_page(browser, page, room)
    talker = Client(room, agent_id="chatty", key=KEY)
    try:
        # Enough to overflow the pane, so there is a bottom to be away from.
        for n in range(30):
            talker.post("build", f"filler line {n}")
        tab.wait_for_function(
            "document.querySelectorAll('.msg').length > 30", timeout=15_000)
        tab.wait_for_timeout(500)

        assert tab.evaluate("""() => {
            const p = document.getElementById('messages');
            return p.scrollHeight - p.scrollTop - p.clientHeight < 40;
        }"""), "a pane that is not scrollable proves nothing about following"
        assert tab.query_selector("#jump").is_visible() is False

        # Still pinned: the newest message arrives in view.
        talker.post("build", "arrived while pinned")
        tab.wait_for_function(
            "document.getElementById('messages').innerText.includes('arrived while pinned')",
            timeout=15_000)
        tab.wait_for_timeout(500)
        assert tab.evaluate("""() => {
            const p = document.getElementById('messages');
            return p.scrollHeight - p.scrollTop - p.clientHeight < 40;
        }""")

        # Now read something older, and stay there.
        tab.evaluate("document.getElementById('messages').scrollTop = 0")
        talker.post("build", "arrived while reading history")
        tab.wait_for_function(
            "document.getElementById('messages').innerText"
            ".includes('arrived while reading history')", timeout=15_000)
        tab.wait_for_timeout(500)
        assert tab.evaluate("document.getElementById('messages').scrollTop") == 0
        jump = tab.query_selector("#jump")
        assert jump.is_visible() is True
        assert "new message" in jump.inner_text()

        # And the pill puts you back.
        jump.click()
        tab.wait_for_function("""() => {
            const p = document.getElementById('messages');
            return p.scrollHeight - p.scrollTop - p.clientHeight < 40;
        }""", timeout=10_000)
        assert tab.query_selector("#jump").is_visible() is False
        assert errors == []
    finally:
        talker.close()
        tab.close()


def test_one_pasted_invite_fills_the_whole_sheet(browser, page, room):
    """The reason invites exist, in the place a person actually arrives.

    Four fields typed by hand are four chances to differ from the peer who
    sent them, and every one of them fails silently — a room you are alone in
    looks exactly like a quiet one. So the paste has to do all four, and the
    room it produces has to actually read.
    """
    from switchboard.invite import Invite

    blob = Invite(url=room.url, workspace=room.workspace, key=KEY,
                  note="parser room").encode()
    tab = browser.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    tab.goto(page, wait_until="networkidle")
    try:
        tab.fill("#f-invite", blob)
        assert tab.input_value("#f-url") == room.url
        assert tab.input_value("#f-workspace") == room.workspace
        assert tab.input_value("#f-key") == KEY
        assert tab.input_value("#f-label") == "parser room"
        assert tab.query_selector("#invite-ok").is_visible() is True

        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert "parser.py is mine for ~20 minutes" in tab.inner_text("#messages")
        assert errors == []
    finally:
        tab.close()


def test_a_bad_invite_says_so_while_it_is_still_on_screen(browser, page):
    """Reported as it is typed rather than on submit: after the sheet closes,
    the string you would compare against is gone."""
    tab = browser.new_page()
    tab.goto(page, wait_until="networkidle")
    try:
        tab.fill("#f-invite", "swb1_this-is-not-base64-json")
        warn = tab.query_selector("#invite-warn")
        assert warn.is_visible() is True
        assert "corrupt or truncated" in warn.inner_text()
        # And nothing was half-applied from it.
        assert tab.input_value("#f-workspace") == ""
    finally:
        tab.close()


def test_an_invite_whose_proof_this_key_cannot_open_says_WRONG_ROOM(
    browser, page, room, hub,
):
    """The forty-minute failure, caught on the first refresh.

    Same hub, same workspace, different key: both parties appear on one
    roster and can exchange nothing. A roster cannot tell you that; opening a
    value the inviter sealed can, and the invite carries the board key of one.
    """
    from switchboard.invite import PROBE_SENTINEL, Invite

    inviter = Client(room, agent_id="inviter", key=KEY)
    inviter.board_set("join/probe/aaaa", PROBE_SENTINEL)
    inviter.close()

    stranger = generate_key()
    blob = Invite(url=room.url, workspace=room.workspace, key=stranger,
                  probe="join/probe/aaaa").encode()
    tab = browser.new_page()
    tab.goto(page, wait_until="networkidle")
    try:
        tab.fill("#f-invite", blob)
        tab.click("#settings-save")
        tab.wait_for_function(
            "document.querySelector('#notes').innerText.length > 0", timeout=10_000)
        assert "WRONG ROOM" in tab.inner_text("#notes")
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


# --- an invite that arrives as a link ----------------------------------------
#
# The reader this is for was never going to run `switchboard join`: they have a
# browser, a link somebody sent them, and no checkout. The whole design rests on
# the invite riding in the *fragment* — never sent to a server — so these tests
# check that property as much as the behaviour.


def test_a_link_opens_the_sheet_already_filled_in(browser, page, room):
    """One URL, four fields, and the room reads. Same claim as the paste, for
    the reader who has nothing to paste it into."""
    from switchboard.invite import Invite

    blob = Invite(url=room.url, workspace=room.workspace, key=KEY,
                  note="parser room").encode()
    tab = browser.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    tab.goto(page + "#" + blob, wait_until="networkidle")
    try:
        assert tab.evaluate("document.getElementById('settings').open") is True
        assert tab.input_value("#f-url") == room.url
        assert tab.input_value("#f-workspace") == room.workspace
        assert tab.input_value("#f-key") == KEY
        assert tab.query_selector("#invite-ok").is_visible() is True

        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert "parser.py is mine for ~20 minutes" in tab.inner_text("#messages")
        assert errors == []
    finally:
        tab.close()


def test_a_link_fills_the_sheet_rather_than_joining_behind_the_reader(
    browser, page, room,
):
    """A link is *less* trustworthy than a paste — it arrives from somewhere,
    often forwarded. A page that read a URL and silently entered a room would
    leave its reader nowhere to notice they had opened the wrong one."""
    from switchboard.invite import Invite

    blob = Invite(url=room.url, workspace=room.workspace, key=KEY).encode()
    tab = browser.new_page()
    tab.goto(page + "#" + blob, wait_until="networkidle")
    try:
        assert tab.evaluate(
            "localStorage.getItem('switchboard.rooms.v1')") in (None, "[]")
        assert tab.query_selector_all(".msg") == []
    finally:
        tab.close()


def test_the_key_is_scrubbed_from_the_address_bar(browser, page, room):
    """What is left otherwise is a key in browser history, in the next
    screenshot, and in whatever "copy link" pastes."""
    from switchboard.invite import Invite

    blob = Invite(url=room.url, workspace=room.workspace, key=KEY).encode()
    tab = browser.new_page()
    tab.goto(page + "#" + blob, wait_until="networkidle")
    try:
        assert KEY not in tab.url
        assert "#" not in tab.url
        # ...and it is still usable: scrubbing happens after the sheet is fed.
        assert tab.input_value("#f-key") == KEY
    finally:
        tab.close()


def test_the_page_host_never_receives_the_invite(browser, page, room):
    """The reason this is a fragment and not a query string. Everything else
    about the shape is convenience; this is the part that is load bearing."""
    from switchboard.invite import Invite

    blob = Invite(url=room.url, workspace=room.workspace, key=KEY).encode()
    tab = browser.new_page()
    asked: list[str] = []
    tab.on("request", lambda r: asked.append(r.url))
    tab.goto(page + "#" + blob, wait_until="networkidle")
    try:
        assert asked, "no requests captured, so this proves nothing"
        assert not [u for u in asked if "swb1_" in u or KEY in u]
    finally:
        tab.close()


def test_a_mangled_link_says_so_instead_of_failing_silently(browser, page):
    """Truncation is what happens to URLs — a chat client wraps one, somebody
    copies to the line break. It has to be visible, not an empty sheet."""
    tab = browser.new_page()
    tab.goto(page + "#swb1_this-is-not-base64-json", wait_until="networkidle")
    try:
        warn = tab.query_selector("#invite-warn")
        assert warn.is_visible() is True
        assert "corrupt or truncated" in warn.inner_text()
        assert tab.input_value("#f-workspace") == ""
    finally:
        tab.close()


def test_a_fragment_that_is_not_an_invite_is_left_alone(browser, page):
    """Fragments have other uses, and a page that grabbed every one of them
    would break the moment anything else wanted to put something there."""
    tab = browser.new_page()
    tab.goto(page + "#anchor", wait_until="networkidle")
    try:
        assert tab.url.endswith("#anchor")
        assert tab.query_selector("#invite-warn").is_visible() is False
    finally:
        tab.close()


def test_a_link_naming_a_key_it_did_not_carry_says_which_one(browser, page, room):
    """A browser has no `SWITCHBOARD_KEY_<ID>` to resolve an id against, so it
    cannot find the key — but "no key" leaves its reader with a question they
    have no way to answer, and the id turns it into one they can."""
    from switchboard.invite import Invite

    blob = Invite(url=room.url, workspace=room.workspace, key=None,
                  key_id="ops").encode()
    tab = browser.new_page()
    tab.goto(page + "#" + blob, wait_until="networkidle")
    try:
        said = tab.inner_text("#invite-ok")
        assert "names key 'ops'" in said
        assert tab.input_value("#f-key") == ""
    finally:
        tab.close()


@pytest.fixture(scope="module")
def busy_room(hub):
    """A room with several channels, a DM among them, in its own workspace."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-channels", key=KEY)
    agent = Client(config, agent_id="parser-agent", key=KEY)
    agent.register(name="parser", kind="local", channels=["build"])
    agent.post("plan", "the oldest channel here")
    agent.post("review", "something in the middle")
    agent.post("@tests", "a word meant for one agent")
    agent.post("build", "the newest thing anybody said")
    yield config
    agent.close()


def test_the_page_says_which_channel_you_are_reading(browser, page, busy_room):
    """A chip is not a location.

    On a narrow window the row of chips scrolls, so the filled-in one can be
    off-screen entirely — and then nothing on the page says what you are
    looking at. The heading says it, and offers the way back out.
    """
    tab, errors = open_page(browser, page, busy_room)
    try:
        assert "all channels" in tab.inner_text("#convo-head")

        tab.click('.chip[data-c="review"]')
        tab.wait_for_function(
            "document.getElementById('convo-head').innerText.includes('review')",
            timeout=10_000)
        assert "something in the middle" in tab.inner_text("#messages")
        assert "the newest thing anybody said" not in tab.inner_text("#messages")

        # And back out again, from the heading rather than by hunting for the
        # chip that started it.
        tab.click("#convo-head .clear")
        tab.wait_for_function(
            "document.getElementById('convo-head').innerText.includes('all channels')",
            timeout=10_000)
        assert "the newest thing anybody said" in tab.inner_text("#messages")
        assert errors == []
    finally:
        tab.close()


def test_channels_are_ordered_by_what_moved_last_and_dms_sort_after(
    browser, page, busy_room,
):
    """Alphabetical order puts the busy channel wherever its name falls.

    Recency is what a reader is actually after, and `latest_at` was already
    being carried by both builders. A direct message is a different kind of
    thing from the room talking, so the two groups are visibly two.
    """
    tab, errors = open_page(browser, page, busy_room)
    try:
        chips = tab.eval_on_selector_all(
            "#chips .chip", "els => els.map(e => e.dataset.c)")
        assert chips[0] == ""                     # "all" stays pinned first
        assert chips[1] == "build"                # the newest thing said
        assert chips.index("review") < chips.index("plan")
        assert chips[-1] == "@tests"              # a DM sorts after the room
        assert tab.eval_on_selector_all(
            "#chips .chip.dm", "els => els.map(e => e.dataset.c)") == ["@tests"]
        assert errors == []
    finally:
        tab.close()


def test_the_roster_reaches_the_channels_it_names(browser, page, busy_room):
    """"watching build" names exactly what the chips filter, so it may as well
    reach it rather than sending the reader hunting for the matching chip."""
    tab, errors = open_page(browser, page, busy_room)
    try:
        tab.click("#agents .chanlink")
        tab.wait_for_function(
            "document.getElementById('convo-head').innerText.includes('build')",
            timeout=10_000)
        assert "the newest thing anybody said" in tab.inner_text("#messages")
        assert errors == []
    finally:
        tab.close()


def test_a_refresh_leaves_the_rows_it_did_not_change_alone(
    browser, page, room, hub,
):
    """The page used to be rebuilt with `innerHTML` every few seconds.

    That cancels a selection mid-drag, drops focus, and makes anything the
    reader adjusted — an expanded value — impossible to keep, because the node
    holding it is gone. Rows are keyed and reconciled now, so a poll that adds
    one message touches one node.
    """
    tab, errors = open_page(browser, page, room)
    talker = Client(room, agent_id="chatty", key=KEY)
    try:
        # Mark the nodes that exist now. A mark is a property rather than an
        # attribute, so only the actual node surviving can carry it.
        tab.evaluate("""() => {
            document.querySelectorAll('#messages .msg')
                    .forEach((n, i) => { n.__mark = i; });
            document.querySelectorAll('#agents > div')
                    .forEach((n, i) => { n.__mark = i; });
        }""")
        before = tab.evaluate("document.querySelectorAll('#messages .msg').length")

        talker.post("build", "one more, and only one more")
        tab.wait_for_function(
            "document.getElementById('messages').innerText"
            ".includes('one more, and only one more')", timeout=15_000)

        kept = tab.evaluate("""() => ({
            msgs: [...document.querySelectorAll('#messages .msg')]
                    .filter(n => n.__mark !== undefined).length,
            agents: [...document.querySelectorAll('#agents > div')]
                    .filter(n => n.__mark !== undefined).length,
        })""")
        assert kept["msgs"] == before      # every earlier message is the same node
        assert kept["agents"] > 0          # and so is the roster beside it
        assert errors == []
    finally:
        talker.close()
        tab.close()


def test_claims_lead_with_what_lapses_soonest(browser, page, hub):
    """Expiry is the one time-critical thing on that panel, and it read the
    same at four minutes and four seconds."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-claims", key=KEY)
    agent = Client(config, agent_id="holder", key=KEY)
    agent.register(name="holder", kind="local")
    agent.post("build", "so the page has something to wait for")
    agent.acquire("src/slow.py", note="the long hold", ttl=3000)
    agent.acquire("src/soon.py", note="about to lapse", ttl=30)
    tab, errors = open_page(browser, page, config)
    try:
        notes = tab.eval_on_selector_all(
            "#leases > div", "els => els.map(e => e.innerText)")
        assert "about to lapse" in notes[0]
        assert "the long hold" in notes[1]
        # And a countdown that is nearly out says so in more than digits.
        assert tab.query_selector("#leases time.urgent") is not None
        assert errors == []
    finally:
        agent.close()
        tab.close()


def test_the_proof_of_room_is_said_when_it_passes_and_stays_off_the_board(
    browser, page, hub,
):
    """Silence was the right answer for a success only while there was nowhere
    to put one: these notes are drawn as warnings. There is a place now — and
    the probe entry itself is this viewer's machinery, not state an agent
    published, so it comes off the blackboard either way.
    """
    from switchboard.invite import PROBE_SENTINEL, Invite

    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-probe", key=KEY)
    inviter = Client(config, agent_id="inviter", key=KEY)
    inviter.register(name="inviter", kind="local")
    inviter.post("build", "so the page has something to wait for")
    inviter.board_set("join/probe/bbbb", PROBE_SENTINEL)
    inviter.board_set("handoff/lexer", {"next": "escapes"})
    inviter.close()

    blob = Invite(url=config.url, workspace=config.workspace, key=KEY,
                  probe="join/probe/bbbb").encode()
    tab = browser.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    try:
        tab.goto(page, wait_until="networkidle")
        tab.fill("#f-invite", blob)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert tab.query_selector("#verified").is_visible() is True
        assert "WRONG ROOM" not in tab.inner_text("#notes")
        # The room's own state is still there; only the machinery is not.
        assert "handoff/lexer" in tab.inner_text("#board")
        assert "join/probe" not in tab.inner_text("#board")
        assert PROBE_SENTINEL not in tab.inner_text("#board")
        assert errors == []
    finally:
        tab.close()


def test_searching_narrows_the_conversation_and_says_so(browser, page, busy_room):
    """A channel was the only way to narrow the room, which leaves "where was
    that message about the lexer" unanswerable on anything busy."""
    tab, errors = open_page(browser, page, busy_room)
    try:
        tab.fill("#q", "middle")
        tab.wait_for_function(
            "document.querySelectorAll('#messages .msg').length === 1", timeout=10_000)
        assert "something in the middle" in tab.inner_text("#messages")
        assert 'matching "middle"' in tab.inner_text("#convo-head")

        # A search that matches nothing says which of the three narrowed it
        # away, rather than "nothing said yet" — which would read as a quiet
        # room and is wrong twice.
        tab.fill("#q", "no message anywhere says this")
        tab.wait_for_function(
            "document.getElementById('messages').innerText.includes('matches')",
            timeout=10_000)
        assert "nothing here matches that" in tab.inner_text("#messages")

        # And the way out puts the whole room back.
        tab.click("#convo-clear")
        tab.wait_for_function(
            "document.querySelectorAll('#messages .msg').length === 4", timeout=10_000)
        assert tab.input_value("#q") == ""
        assert errors == []
    finally:
        tab.close()


def test_a_name_on_the_roster_narrows_to_what_they_said(browser, page, hub):
    """Reading one agent's side of a busy room meant reading past everybody
    else's, and the roster already knows who said what."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-speakers", key=KEY)
    one = Client(config, agent_id="parser-agent", key=KEY)
    one.register(name="parser", kind="local")
    two = Client(config, agent_id="tests-agent", key=KEY)
    two.register(name="tests", kind="local")
    one.post("build", "something the parser said")
    two.post("build", "something the tests said")
    tab, errors = open_page(browser, page, config)
    try:
        tab.click("#agents button.name:text-is('parser')")
        tab.wait_for_function(
            "document.querySelectorAll('#messages .msg').length === 1", timeout=10_000)
        assert "something the parser said" in tab.inner_text("#messages")
        assert "from parser" in tab.inner_text("#convo-head")

        # Clicking the same name again is the way back out: a filter you can
        # only enter is a trap.
        tab.click("#agents button.name:text-is('parser')")
        tab.wait_for_function(
            "document.querySelectorAll('#messages .msg').length === 2", timeout=10_000)
        assert errors == []
    finally:
        one.close()
        two.close()
        tab.close()


def test_the_scope_survives_a_reload_and_can_be_sent_to_somebody(
    browser, page, busy_room,
):
    """A filter that lives only in a module variable is forgotten on every
    reload and cannot be handed to anyone: "look at what the parser said about
    escapes" stays a sentence instead of becoming a link."""
    tab, errors = open_page(browser, page, busy_room)
    try:
        tab.click('.chip[data-c="review"]')
        tab.wait_for_function(
            "document.getElementById('convo-head').innerText.includes('review')",
            timeout=10_000)
        assert "#view=" in tab.url and "c=review" in tab.url

        tab.reload(wait_until="networkidle")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert "review" in tab.inner_text("#convo-head")
        assert "something in the middle" in tab.inner_text("#messages")
        assert "the newest thing anybody said" not in tab.inner_text("#messages")

        # And leaving the scope leaves the URL as it found it.
        tab.click("#convo-clear")
        tab.wait_for_function("!location.hash", timeout=10_000)
        assert errors == []
    finally:
        tab.close()


def test_an_anchor_somebody_else_put_in_the_url_is_not_overwritten(
    browser, page, busy_room,
):
    """The fragment is shared ground — an invite arrives in it, and so does
    anything else that wants one. The page writes only where it already
    wrote."""
    tab, errors = open_page(browser, page, busy_room)
    try:
        tab.evaluate("history.replaceState(null, '', location.pathname + '#anchor')")
        tab.click('.chip[data-c="review"]')
        tab.wait_for_function(
            "document.getElementById('convo-head').innerText.includes('review')",
            timeout=10_000)
        assert tab.url.endswith("#anchor")
        assert errors == []
    finally:
        tab.close()


def test_widening_the_scope_does_not_pretend_old_messages_just_arrived(
    browser, page, busy_room,
):
    """Rows the reader filtered away come back as the same rows. Six of them
    lighting up because a search was cleared says exactly the wrong thing."""
    tab, errors = open_page(browser, page, busy_room)
    try:
        tab.fill("#q", "no message anywhere says this")
        tab.wait_for_function(
            "document.querySelectorAll('#messages .msg').length === 0", timeout=10_000)
        tab.fill("#q", "")
        tab.wait_for_function(
            "document.querySelectorAll('#messages .msg').length === 4", timeout=10_000)
        assert tab.eval_on_selector_all("#messages .msg.arrived", "e => e.length") == 0
        assert errors == []
    finally:
        tab.close()


def test_a_wall_of_channels_is_offered_rather_than_shown(browser, page, hub):
    """Sixty chips is not a switcher. The row keeps what a glance can use, and
    whatever you are reading stays in it however far down it sorted."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-many", key=KEY)
    agent = Client(config, agent_id="talker", key=KEY)
    agent.register(name="talker", kind="local")
    for n in range(20):
        agent.post(f"topic{n:02d}", f"a word in topic {n}")
    tab, errors = open_page(browser, page, config)
    try:
        shown = tab.eval_on_selector_all("#chips .chip:not(.more)", "e => e.length")
        assert shown == 13            # twelve channels, plus "all"
        assert "+8 more" in tab.inner_text(".chip.more")

        tab.click(".chip.more")
        tab.wait_for_function(
            "document.querySelectorAll('#chips .chip:not(.more)').length === 21",
            timeout=10_000)
        assert errors == []
    finally:
        agent.close()
        tab.close()


def test_the_blackboard_reads_as_the_tree_its_keys_already_are(browser, page, hub):
    """A key is a path, not a name with a slash in it, and a flat list throws
    away structure the room itself put there — while recency has to keep
    running through it, or "what just changed" is lost to tidiness."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-board", key=KEY)
    agent = Client(config, agent_id="writer", key=KEY)
    agent.register(name="writer", kind="local")
    agent.post("build", "so the page has something to wait for")
    agent.board_set("build/ci/unit", "green")
    agent.board_set("build/ci/lint", "green")
    agent.board_set("build", "a key that is also a branch")
    agent.board_set("alone/here", "a prefix shared with nothing")
    agent.board_set("handoff/lexer", {"next": "escapes"})
    agent.board_set("solo", "no prefix at all")
    tab, errors = open_page(browser, page, config)
    try:
        rows = tab.eval_on_selector_all(
            "#board > div",
            "els => els.map(e => e.className + '|' "
            "+ e.textContent.trim().split('\\n').map(t => t.trim()).join(' '))")
        # Newest first at every level, so the last two keys written lead — and
        # neither grows a branch, because a branch leading to one thing is a
        # longer name rather than a level.
        assert rows[0].startswith("d0|solo")
        assert rows[1].startswith("d0|handoff/lexer")
        assert rows[2].startswith("d0|alone/here")
        # Then the one real branch, carrying what is behind it.
        assert rows[3].startswith("d0 twig|") or rows[3].startswith("twig d0|")
        assert "build/" in rows[3] and "3" in rows[3]
        # A key that is also a branch is a child of itself, named in full
        # because that is the one place a tail segment is ambiguous.
        assert rows[4].startswith("d1|build ")
        assert "ci/" in rows[5]
        assert rows[6].startswith("d2|") and rows[7].startswith("d2|")
        assert {"lint", "unit"} == {r.split("|")[1].split(" ")[0] for r in rows[6:8]}
        assert errors == []
    finally:
        agent.close()
        tab.close()


def test_a_branch_folds_and_stays_folded_across_a_refresh(browser, page, hub):
    """Folding is only useful if it survives the three-second repaint — and it
    must not hide that something moved inside what was folded, which is why the
    branch carries its own count and its own newest."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-fold", key=KEY)
    agent = Client(config, agent_id="writer", key=KEY)
    agent.register(name="writer", kind="local")
    agent.post("build", "so the page has something to wait for")
    agent.board_set("deep/one", "1")
    agent.board_set("deep/two", "2")
    agent.board_set("shallow", "3")
    tab, errors = open_page(browser, page, config)
    try:
        assert tab.eval_on_selector_all("#board > div", "e => e.length") == 4
        branch = tab.query_selector('[data-twig="key/deep"]')
        assert "2" in branch.inner_text()      # what is behind it, before folding

        branch.click()
        tab.wait_for_function(
            "document.querySelectorAll('#board > div').length === 2", timeout=10_000)
        assert "deep/" in tab.inner_text("#board")
        assert "one" not in tab.inner_text("#board")

        # Through a repaint, and still folded.
        tab.wait_for_timeout(3500)
        assert tab.eval_on_selector_all("#board > div", "e => e.length") == 2
        assert tab.query_selector('[data-twig="key/deep"] .caret.shut') is not None

        tab.click('[data-twig="key/deep"]')
        tab.wait_for_function(
            "document.querySelectorAll('#board > div').length === 4", timeout=10_000)
        assert errors == []
    finally:
        agent.close()
        tab.close()


def test_a_panel_folded_away_stays_folded(browser, page, room):
    """Three panels of equal weight and no order means the claims — the one
    thing here that expires — sit below a roster that never shrinks."""
    tab, errors = open_page(browser, page, room)
    try:
        assert tab.inner_text("#n-agents") == "1"
        tab.click("#p-agents > summary")
        tab.wait_for_function(
            "localStorage.getItem('switchboard.panels.v1')"
            "?.includes('p-agents') === true", timeout=10_000)
        assert tab.evaluate("document.getElementById('p-agents').open") is False

        tab.reload(wait_until="networkidle")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert tab.evaluate("document.getElementById('p-agents').open") is False
        assert tab.evaluate("document.getElementById('p-leases').open") is True
        # And the count is readable with the panel shut, which is the point.
        assert tab.inner_text("#n-agents") == "1"
        assert errors == []
    finally:
        tab.close()


def test_the_page_answers_the_keyboard(browser, page, busy_room):
    """A page people leave open all day beside the work it describes should not
    need the mouse."""
    tab, errors = open_page(browser, page, busy_room)
    try:
        tab.press("body", "/")
        assert tab.evaluate("document.activeElement.id") == "q"
        tab.keyboard.type("middle")
        tab.wait_for_function(
            "document.querySelectorAll('#messages .msg').length === 1", timeout=10_000)

        # One step at a time, narrowest first.
        tab.keyboard.press("Escape")
        tab.wait_for_function(
            "document.querySelectorAll('#messages .msg').length === 4", timeout=10_000)
        assert tab.input_value("#q") == ""
        assert errors == []
    finally:
        tab.close()


def test_a_narrow_window_shows_one_pane_at_a_time(browser, page, room):
    """Stacked under the conversation, the roster and the claims were in
    practice unreachable on a phone: the talking is long and never stops
    arriving. A switcher makes each of them a place you can go, and gives the
    conversation the whole screen back."""
    tab = browser.new_page(viewport={"width": 430, "height": 880})
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    try:
        tab.goto(page, wait_until="networkidle")
        tab.fill("#f-url", room.url)
        tab.fill("#f-workspace", room.workspace)
        tab.fill("#f-key", KEY)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)

        assert tab.query_selector("#panes").is_visible() is True
        # The conversation first, and the counts say what is behind the rest
        # without going there.
        assert tab.query_selector(".convo").is_visible() is True
        assert tab.query_selector("#leases").is_visible() is False
        assert "1" in tab.inner_text('.pane[data-pane="leases"]')

        tab.click('.pane[data-pane="leases"]')
        tab.wait_for_function(
            "document.querySelector('main').dataset.pane === 'leases'", timeout=10_000)
        assert tab.query_selector(".convo").is_visible() is False
        assert "rewriting the lexer" in tab.inner_text("#leases")
        assert tab.query_selector("#agents").is_visible() is False

        # And the page still does not scroll sideways or as a whole: the pane
        # scrolls, the way it does on a wide window.
        assert tab.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth + 1")
        assert tab.evaluate(
            "document.documentElement.scrollHeight <= window.innerHeight + 2")
        assert errors == []
    finally:
        tab.close()


def test_a_panel_folded_on_a_wide_window_still_opens_on_a_narrow_one(
    browser, page, room,
):
    """Below the breakpoint the summary is gone, so a panel the reader folded
    away on a desktop would be a segment that shows nothing at all."""
    tab = browser.new_page(viewport={"width": 1280, "height": 860})
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    try:
        tab.goto(page, wait_until="networkidle")
        tab.fill("#f-url", room.url)
        tab.fill("#f-workspace", room.workspace)
        tab.fill("#f-key", KEY)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert tab.query_selector("#panes").is_visible() is False

        tab.click("#p-leases > summary")
        tab.wait_for_function(
            "document.getElementById('p-leases').open === false", timeout=10_000)

        tab.set_viewport_size({"width": 430, "height": 880})
        tab.click('.pane[data-pane="leases"]')
        tab.wait_for_function(
            "document.getElementById('p-leases').open === true", timeout=10_000)
        assert "rewriting the lexer" in tab.inner_text("#leases")

        # Back on a wide window all three are on screen at once again, so the
        # pane chosen down there is not a state to still be in.
        tab.set_viewport_size({"width": 1280, "height": 860})
        tab.wait_for_function(
            "document.querySelector('main').dataset.pane === 'convo'", timeout=10_000)
        for panel in ("#agents", "#leases", "#board"):
            assert tab.query_selector(panel).is_visible() is True
        assert tab.query_selector(".convo").is_visible() is True
        assert errors == []
    finally:
        tab.close()


def test_the_room_you_are_reading_can_be_handed_to_somebody(browser, page, room):
    """A viewer that can only be arrived at is half a tool: the person who has
    the room open is exactly the person asked to share it, and retyping four
    fields into a chat window is the silent-failure path invites exist to
    remove.

    It hands on only what this browser already holds, so the link grants what
    its sender had and nothing more.
    """
    from switchboard.invite import Invite

    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    tab = context.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    try:
        tab.goto(page, wait_until="networkidle")
        tab.fill("#f-url", room.url)
        tab.fill("#f-workspace", room.workspace)
        tab.fill("#f-label", "parser")
        tab.fill("#f-key", KEY)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)

        tab.click("#share-open")
        tab.wait_for_selector("#share[open]", timeout=10_000)
        link = tab.input_value("#share-url")

        # In the clipboard, and said to be — never the other way round. The
        # clipboard is a round trip through a permission check, so the sheet
        # says it is copying until it knows, and this waits for that answer
        # rather than for the sheet.
        tab.wait_for_function(
            "!document.getElementById('share-said').textContent.endsWith('…')",
            timeout=10_000)
        assert "Copied" in tab.inner_text("#share-said")
        assert tab.evaluate("navigator.clipboard.readText()") == link

        # The key rides in the fragment, which is the only reason a link may
        # carry one: it is never sent to this page's host.
        assert "#swb1_" in link
        assert KEY not in link.split("#")[0]
        # And what it carries is said before it is pasted anywhere.
        carries = tab.inner_text("#share-carries")
        assert room.workspace in carries and "with the key" in carries

        # The CLI reads what the page wrote.
        invite = Invite.decode(link.split("#", 1)[1])
        assert invite.url == room.url
        assert invite.workspace == room.workspace
        assert invite.key == KEY
        assert errors == []
    finally:
        tab.close()
        context.close()


def test_a_shared_link_opens_the_room_in_a_browser_that_never_saw_it(
    browser, page, room,
):
    """The whole loop, which is the only proof that matters: the link one
    reader copies is a room the next one can actually read."""
    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    tab = context.new_page()
    try:
        tab.goto(page, wait_until="networkidle")
        tab.fill("#f-url", room.url)
        tab.fill("#f-workspace", room.workspace)
        tab.fill("#f-key", KEY)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        tab.click("#share-open")
        tab.wait_for_selector("#share[open]", timeout=10_000)
        link = tab.input_value("#share-url")
    finally:
        tab.close()
        context.close()

    stranger = browser.new_context()
    other = stranger.new_page()
    errors: list[str] = []
    other.on("pageerror", lambda e: errors.append(str(e)))
    try:
        other.goto(link, wait_until="networkidle")
        # Filled and shown, never joined behind the reader — a link arrives
        # from somewhere, often forwarded, and is less trustworthy than a
        # paste.
        other.wait_for_selector("#settings[open]", timeout=10_000)
        assert other.input_value("#f-workspace") == room.workspace
        assert other.input_value("#f-key") == KEY
        assert "#" not in other.url          # and scrubbed from the address bar

        other.click("#settings-save")
        other.wait_for_function("document.querySelectorAll('.msg').length > 0",
                                timeout=10_000)
        assert "parser.py is mine for ~20 minutes" in other.inner_text("#messages")
        assert errors == []
    finally:
        other.close()
        stranger.close()


def test_a_browser_that_refuses_the_clipboard_is_told_the_truth(browser, page, room):
    """The one unacceptable outcome is saying "copied" when nothing was: the
    reader pastes an empty clipboard into a chat window and sends nobody
    anything. A refusal says so, and leaves the link on screen.

    The wait is the point as much as the message. The clipboard is a round trip
    through a permission check, so the sheet cannot know its own status when it
    opens — it says it is copying, and says what happened once it knows.
    """
    context = browser.new_context()
    # Slow, then refuse: the sheet must be honest at both ends of that.
    context.add_init_script("""
        Object.defineProperty(navigator, 'clipboard', {
          configurable: true,
          value: {writeText: () => new Promise((_, no) =>
            setTimeout(() => no(new Error('denied')), 400))},
        });
    """)
    tab = context.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    try:
        tab.goto(page, wait_until="networkidle")
        tab.fill("#f-url", room.url)
        tab.fill("#f-workspace", room.workspace)
        tab.fill("#f-key", KEY)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)

        tab.click("#share-open")
        tab.wait_for_selector("#share[open]", timeout=10_000)
        # Never blank, and never a claim it cannot make yet.
        assert tab.inner_text("#share-said").endswith("…")

        tab.wait_for_function(
            "!document.getElementById('share-said').textContent.endsWith('…')",
            timeout=10_000)
        said = tab.inner_text("#share-said")
        assert "Copied" not in said
        assert "clipboard" in said and "copy it from there" in said
        # And the link is still there to copy by hand.
        assert "#swb1_" in tab.input_value("#share-url")
        assert errors == []
    finally:
        tab.close()
        context.close()


def test_a_forecast_counts_down_to_the_moment_it_names(browser, page, hub):
    """`timing_forecast` carries instants, not durations: `p50` is *when* the
    sender will next look. Read as a number of seconds it was `Math.round(NaN)`
    — not less than 60, not less than 3600 — so every forecast on the page said
    `~NaNhNaNm`, through the one branch of that formatter nothing had reached.

    And a deadline behind us is "due", not "expired": a look that was owed two
    minutes ago has not expired, and it costs nobody a lock, so it does not
    take the amber that a lapsing claim does.
    """
    from datetime import datetime, timedelta, timezone

    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-forecast", key=KEY)
    agent = Client(config, agent_id="planner", key=KEY)
    agent.register(name="planner", kind="local")
    soon = datetime.now(tz=timezone.utc) + timedelta(minutes=90)
    later = datetime.now(tz=timezone.utc) + timedelta(minutes=95)
    gone = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    # Exactly the shape `wrap_forecast` puts on the wire.
    agent.post("build", {"text": "looking again after the lexer lands",
                         "timing_forecast": {"p50": soon.isoformat(),
                                             "p95": later.isoformat(),
                                             "speak_p50": later.isoformat()}})
    agent.post("build", {"text": "this one is overdue",
                         "timing_forecast": {"p50": gone.isoformat()}})
    tab, errors = open_page(browser, page, config)
    try:
        forecasts = tab.eval_on_selector_all(
            ".forecast", "els => els.map(e => e.innerText)")
        assert len(forecasts) == 2
        assert "NaN" not in " ".join(forecasts)
        assert "1h29m" in forecasts[0] or "1h30m" in forecasts[0]
        assert "next message ~1h3" in forecasts[0]
        # A deadline already behind us says so, in its own word.
        assert "due" in forecasts[1] and "expired" not in forecasts[1]
        # And is not dressed as a claim about to lapse.
        assert tab.eval_on_selector_all(".forecast time.urgent", "e => e.length") == 0
        assert errors == []
    finally:
        agent.close()
        tab.close()


def test_a_clock_says_when_and_not_only_how_long_ago(browser, page, room):
    """Every time on this page was relative and only relative — right for the
    last minute, useless past it. A room you came back to in the morning said
    "17h ago" where a person wanted the hour; one you came back to on Monday
    said "73h12m ago", because the formatter had no unit above hours.

    So: a real `datetime` on each of them, the whole moment on hover, an age
    that becomes a date once counting hours stops meaning anything, and a
    countdown that counts days.
    """
    tab, errors = open_page(browser, page, room)
    try:
        # Machine-readable and hoverable, on everything that shows a time.
        assert tab.eval_on_selector_all(
            "time", "els => els.every(e => e.getAttribute('datetime'))")
        assert tab.eval_on_selector_all(
            "time", "els => els.every(e => (e.getAttribute('title') || '').length > 8)")
        assert "ago" in tab.eval_on_selector("#messages time", "e => e.textContent")

        # The ticker is what decides between the two forms, so it is what this
        # drives: two ages and two deadlines, filled in by the page itself.
        tab.evaluate("""() => {
          const iso = (ms) => new Date(Date.now() + ms).toISOString();
          document.getElementById('board').insertAdjacentHTML('afterbegin', `
            <div id="probe">
              <time data-at="${iso(-3 * 86400e3)}"></time>
              <time data-at="${iso(-2 * 3600e3)}"></time>
              <time data-until="${iso(3 * 86400e3)}"></time>
              <time data-until="${iso(-60e3)}" data-lapsed="due"></time>
            </div>`);
        }""")
        tab.wait_for_function(
            "document.querySelector('#probe time').textContent !== ''", timeout=5_000)
        old, recent, far, past = tab.eval_on_selector_all(
            "#probe time", "els => els.map(e => e.textContent)")

        # Three days back is a date and a clock time, not a count of hours.
        assert "ago" not in old
        assert ":" in old                      # an hour of the day
        assert any(ch.isalpha() for ch in old)  # and the month it belongs to
        assert "72h" not in old
        # Two hours back is still the useful relative form.
        assert recent.endswith("ago") and recent.startswith("2h")
        # Three days out counts days rather than seventy-two hours.
        assert far.startswith("2d") or far.startswith("3d")
        # And a deadline behind us still says so in its own word.
        assert past == "due"
        assert errors == []
    finally:
        tab.close()


@pytest.fixture(scope="module")
def deep_board(hub):
    """A board with the shapes a real room grows: nested paths, a value with
    real structure in it, and a couple of scalars."""
    config = ClientConfig(url=hub["url"], url_source="explicit",
                          workspace="w_browser-deep", key=KEY)
    agent = Client(config, agent_id="writer", key=KEY)
    agent.register(name="writer", kind="local")
    agent.post("build", "so the page has something to wait for")
    agent.board_set("build/ci/unit/last-run", {
        "suite": "pytest -q", "failed": 2, "duration_seconds": 412.5,
        "failures": ["tests/test_lexer.py::test_escapes",
                     "tests/test_lexer.py::test_unicode_identifiers"]})
    agent.board_set("build/ci/lint", "green")
    agent.board_set("handoff/lexer/state", {"phase": "escapes", "owner": "parser"})
    agent.board_set("status", "the room is mid-rewrite")
    yield config
    agent.close()


def test_the_board_can_have_the_window_on_a_wide_one(browser, page, deep_board):
    """A key is a path and a value is whatever an agent wrote, neither of which
    has any reason to fit in a 340px column — where one CI result was a JSON
    blob forty lines tall with a thousand pixels of screen unused beside it.

    So it can have the window: the tree as an index down one side, the whole of
    one entry down the other.
    """
    tab = browser.new_context().new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    try:
        tab.set_viewport_size({"width": 1600, "height": 950})
        tab.goto(page, wait_until="networkidle")
        tab.fill("#f-url", deep_board.url)
        tab.fill("#f-workspace", deep_board.workspace)
        tab.fill("#f-key", KEY)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        assert tab.query_selector("#board-detail").is_visible() is False

        # A key in the sidebar is the way in: there is nowhere in a column to
        # put the entry it just chose.
        tab.click('[data-board="handoff/lexer/state"]')
        tab.wait_for_function(
            "document.querySelector('main').dataset.pane === 'board'", timeout=10_000)
        assert "handoff/lexer/state" in tab.inner_text("#board-detail .key")

        # The entry gets the larger half, and the tree is an index rather than a
        # second copy of every value.
        tree = tab.evaluate(
            "document.getElementById('board').getBoundingClientRect().width")
        detail = tab.evaluate(
            "document.getElementById('board-detail').getBoundingClientRect().width")
        assert detail > tree * 2
        assert tab.eval_on_selector_all(
            "#board pre", "els => els.every(e => e.offsetParent === null)")

        # And a structured value keeps its own line breaks instead of being
        # folded to fit, and is not clamped where there is room for it.
        tab.click('[data-board="build/ci/unit/last-run"]')
        tab.wait_for_function(
            "document.querySelector('#board-detail .key').innerText"
            ".includes('last-run')", timeout=10_000)
        lines = tab.eval_on_selector(
            "#board-detail pre",
            "e => Math.round(e.getBoundingClientRect().height / 18)")
        assert lines < 14                      # it is 41 in the column
        assert tab.eval_on_selector_all(
            "#board-detail .more", "els => els.every(e => e.offsetParent === null)")
        assert tab.eval_on_selector_all("#board > div.on", "e => e.length") == 1

        # Closing gives the conversation back.
        tab.click("#board-narrow")
        tab.wait_for_function(
            "document.querySelector('main').dataset.pane === 'convo'", timeout=10_000)
        assert tab.query_selector(".convo").is_visible() is True
        assert tab.query_selector("#board-detail").is_visible() is False
        assert errors == []
    finally:
        tab.close()


def test_a_narrow_window_has_no_room_for_the_wide_board_and_says_so_by_not_offering_it(
    browser, page, deep_board,
):
    """Two columns do not fit, and a key that opens nothing is a dead control —
    so on a phone the keys are text, and a wide view left open when the window
    shrinks is not a state to stay in."""
    tab = browser.new_context().new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda e: errors.append(str(e)))
    try:
        tab.set_viewport_size({"width": 1600, "height": 950})
        tab.goto(page, wait_until="networkidle")
        tab.fill("#f-url", deep_board.url)
        tab.fill("#f-workspace", deep_board.workspace)
        tab.fill("#f-key", KEY)
        tab.click("#settings-save")
        tab.wait_for_function("document.querySelectorAll('.msg').length > 0",
                              timeout=10_000)
        tab.click("#board-wide")
        tab.wait_for_function(
            "document.querySelector('main').dataset.pane === 'board'", timeout=10_000)

        tab.set_viewport_size({"width": 430, "height": 880})
        tab.wait_for_function(
            "document.querySelector('main').dataset.pane === 'convo'", timeout=10_000)
        assert tab.query_selector("#wide-host").is_visible() is False
        # The panes went back where they came from rather than being rebuilt.
        assert tab.evaluate(
            "document.getElementById('board-split').parentElement.id") == "p-board"

        tab.click('.pane[data-pane="board"]')
        tab.wait_for_timeout(300)
        assert tab.eval_on_selector_all("#board [data-board]", "e => e.length") == 0
        assert "the room is mid-rewrite" in tab.inner_text("#board")
        assert errors == []
    finally:
        tab.close()
