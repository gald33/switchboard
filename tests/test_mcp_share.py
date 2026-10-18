"""Shared rooms through the MCP bridge: four tools, one handle, and one field.

A shared room travels sealed inside pair rooms, so on this surface — as with
pairing — the transcript is the thing to protect: no result may carry a
room's key, write key or token, and every other tool reaches the room by
name, as `room="shared:<name>"`, looked up on this machine on every call.

What else these tests hold is the bridge's half of "never silent": what a
paired machine did — offered a room, accepted one, left one — reaches the
agent as `share_notes` on whatever result comes next, once per bridge, from a
sweep that runs at most once a minute and that nothing it meets can turn into
a failed `say`.

Two bridges are two machines, as in `test_mcp_pair.py`: each has its own pair
store, with its share store beside it, and they share one in-process hub
whose clock the tests move.
"""

from __future__ import annotations

import json
import secrets

import httpx
import pytest

from switchboard import mcp_server, pairing, sharing
from switchboard.client import Identity
from switchboard.mcp_server import SHARE_SWEEP, TOOLS, Bridge, handle_request
from switchboard.pairing import PairStore
from switchboard.sharing import ShareStore
from switchboard.testing import hub as make_hub
from switchboard.timing import TimingModel, unwrap_forecast

SHARE_TOOLS = ("share_new", "share_accept", "share_leave", "share_status")
TOKEN = "tok-" + secrets.token_hex(16)


@pytest.fixture
def h(monkeypatch):
    # With a token, so a test can show the token is one of the things no
    # result carries — `share_new(with_token=True)` puts it in the offer.
    with make_hub(token=TOKEN) as handle:
        monkeypatch.setattr(mcp_server, "Client", handle.client_class())
        yield handle


def machine(h, tmp_path, label: str, *, kind: str = "local") -> Bridge:
    """A bridge on a machine of its own, built as `test_mcp_pair.machine` is.

    Only the pair store is given; the share store is left for the bridge to
    open, because where it opens it — beside the pair store — is part of what
    is under test.
    """
    bridge = Bridge.__new__(Bridge)
    bridge.config = h.client_config(workspace=f"team-{label}", agent_id=label)
    bridge.identity = Identity(agent_id=label, name=f"{label} agent", kind=kind,
                               branch="main", meta={})
    bridge.client = h.client(label, workspace=f"team-{label}", agent_id=label)
    bridge.timing = TimingModel(":memory:")
    bridge._registered = False
    bridge._rooms = {}
    bridge._pair_store = PairStore(tmp_path / label / "pairs", env={})
    bridge._clock = lambda: h.now
    return bridge


@pytest.fixture
def laptop(h, tmp_path):
    return machine(h, tmp_path, "laptop")


@pytest.fixture
def cloud(h, tmp_path):
    return machine(h, tmp_path, "cloud", kind="cloud")


@pytest.fixture
def desk(h, tmp_path):
    return machine(h, tmp_path, "desk")


def call(bridge: Bridge, tool: str, /, **arguments):
    response = handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result.get("isError", False)


def raw(bridge: Bridge, tool: str, /, **arguments):
    return handle_request(bridge, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })


def paired(a: Bridge, b: Bridge, name: str) -> str:
    """`a` and `b` paired under `name`, with nothing but tool calls."""
    started, error = call(a, "pair_start", name=name)
    assert not error, started
    accepted, _ = call(b, "pair_accept", code=started["code"])
    assert accepted["paired"], accepted
    status, _ = call(a, "pair_status", name=name)
    assert [row["state"] for row in status["pairs"]] == ["paired"], status
    return name


def texts(result: dict) -> list[str]:
    return [n["text"] for n in result.get("share_notes", [])]


def kinds(result: dict) -> list[str]:
    return [n["kind"] for n in result.get("share_notes", [])]


def stores(h, bridge: Bridge) -> dict:
    """The keyword arguments for calling the core as another process on the
    same machine — a terminal, a listener — with that machine's stores."""
    path = bridge._pair_store.path
    return {"store": ShareStore(path, env={}), "pair_store": PairStore(path, env={}),
            "connect": h.client_class(), "now": h.now}


def held(bridge: Bridge, name: str) -> dict:
    """A room as the share store keeps it — keys and all, for the tests that
    check they appear nowhere else."""
    return bridge._shares().load()["rooms"][name]


def room_bodies(h, bridge: Bridge, name: str, channel: str) -> list:
    """What a shared room holds, read straight off the hub, not through the
    handle under test."""
    room = held(bridge, name)
    reader = h.client(f"reader-{name}", workspace=room["workspace"], key=room["key"],
                      write_key="")
    return [unwrap_forecast(m["body"])[0] for m in reader.history(channel)]


def outage(h, *, method: str = "board_list"):
    """The hub's client with share-record reads (or writes) failing, as a pair
    room the hub cannot serve would — side records and every other room fine."""

    class Down(h.client_class()):
        def board_list(self, *args, **kwargs):
            if method == "board_list" and kwargs.get("prefix") == sharing.PREFIX:
                raise httpx.ConnectError("no route to the pair room")
            return super().board_list(*args, **kwargs)

        def board_set(self, key, *args, **kwargs):
            if method == "board_set" and key.startswith(sharing.PREFIX):
                raise httpx.ConnectError("no route to the pair room")
            return super().board_set(key, *args, **kwargs)

    return Down


# --- the round trip ------------------------------------------------------------------


def test_a_room_is_shared_accepted_and_worked_in_through_tool_calls_alone(laptop, cloud, h):
    paired(laptop, cloud, "lc")
    made, error = call(laptop, "share_new", name="core", pairs=["lc"], note="the core team")
    assert error is False, made
    assert (made["room"], made["offered"], made["unreached"]) == ("shared:core", ["lc"], [])
    assert "notes" not in made, "notes ride in one place on this surface: share_notes"

    told, _ = call(cloud, "roster")
    [offer] = [n for n in told["share_notes"] if n["kind"] == "offered"]
    assert (offer["pair"], offer["room"]) == ("lc", "core")
    assert "the core team" in offer["text"]

    kept, error = call(cloud, "share_accept", offer="core")
    assert error is False, kept
    assert (kept["name"], kept["room"], kept["offered_back"]) == ("core", "shared:core", True)

    h.advance(SHARE_SWEEP)
    heard, _ = call(laptop, "roster")
    assert "lc accepted core" in texts(heard)

    said, error = call(laptop, "say", channel="general", message="hello, core",
                       room="shared:core")
    assert error is False, said
    assert said["shared"] == {"name": "core", "workspace": held(laptop, "core")["workspace"],
                              "pairs": ["lc"], "warnings": []}
    there, _ = call(cloud, "history", channel="general", room="shared:core")
    assert [m["body"] for m in there["messages"]] == ["hello, core"]
    assert room_bodies(h, cloud, "core", "general") == ["hello, core"]


def test_the_handle_reaches_the_shared_room_and_leaves_the_default_alone(laptop, cloud, h):
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    call(cloud, "share_accept", offer="core")

    call(cloud, "say", channel="build", message="for the room", room="shared:core")
    call(cloud, "say", channel="build", message="for my own room")

    here, _ = call(laptop, "history", channel="build", room="shared:core")
    assert [m["body"] for m in here["messages"]] == ["for the room"]
    mine, _ = call(cloud, "history", channel="build")
    assert [m["body"] for m in mine["messages"]] == ["for my own room"]
    assert room_bodies(h, laptop, "core", "build") == ["for the room"]
    assert set(cloud._share_clients) == {held(cloud, "core")["workspace"]}, \
        "one client per room, keyed by the room's workspace"


# --- share_notes ------------------------------------------------------------------------


def test_a_note_is_said_once_on_whatever_result_comes_next(laptop, cloud, h):
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])

    first, _ = call(cloud, "whoami")
    assert kinds(first) == ["offered"]
    again, _ = call(cloud, "whoami")
    assert "share_notes" not in again
    h.advance(SHARE_SWEEP)
    later, _ = call(cloud, "whoami")
    assert "share_notes" not in later, "an offer still waiting is not news twice"


def test_share_status_lists_every_note_and_later_results_do_not_repeat_them(laptop, cloud):
    """No stored "seen" flag: share_status says every note of the week, every
    time, and what it said is not said again on the next ordinary result."""
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"], note="the core team")

    status, error = call(cloud, "share_status")
    assert error is False, status
    assert [o["offer"] for o in status["offers"]] == ["lc/core"]
    assert any("lc offers room core" in n["text"] for n in status["notes"])
    assert "share_notes" not in status, "share_status carries them as 'notes'"
    after, _ = call(cloud, "roster")
    assert "share_notes" not in after
    again, _ = call(cloud, "share_status")
    assert [n["id"] for n in again["notes"]] == [n["id"] for n in status["notes"]]


def test_another_session_on_the_machine_still_hears_every_note(laptop, cloud, h):
    """S12. The share store belongs to the OS user, so two sessions on one
    machine read one store. If the first to look marked notes seen, a second
    session working in the room would never hear that a peer left it."""
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    call(cloud, "share_accept", offer="core")
    h.advance(SHARE_SWEEP)
    call(laptop, "roster")                            # session one hears the accept
    call(cloud, "share_leave", name="core", note="moving on")

    call(laptop, "share_status")                      # and then reads them all
    second = machine(h, laptop._pair_store.path.parent.parent, "laptop")
    told, _ = call(second, "roster")
    assert "lc left core: moving on" in texts(told)
    assert "lc accepted core" in texts(told)


def test_a_note_found_by_a_call_that_fails_rides_on_its_error(laptop, cloud):
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])

    failed, error = call(cloud, "renew", resource="db/migrations")
    assert error is True and failed["error"] == "hub_error"
    assert kinds(failed) == ["offered"]
    again, _ = call(cloud, "whoami")
    assert "share_notes" not in again


def test_a_note_found_by_a_call_that_crashed_waits_for_the_next_response(laptop, cloud):
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])

    def crashes(**_):
        cloud._touch()
        raise RuntimeError("a bug in a tool")

    cloud.roster = crashes
    with pytest.raises(RuntimeError):
        raw(cloud, "roster")
    del cloud.roster

    later, _ = call(cloud, "whoami")
    assert kinds(later) == ["offered"]


# --- the sweep ------------------------------------------------------------------------------


def test_the_sweep_reads_the_pairs_at_most_once_a_minute(laptop, cloud, h, monkeypatch):
    paired(laptop, cloud, "lc")
    sweeps = []
    real = sharing.touch

    def counted(*args, **kwargs):
        if kwargs.get("pair_store") is laptop._pair_store:
            sweeps.append(kwargs.get("rooms"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sharing, "touch", counted)
    for _ in range(5):
        call(laptop, "roster")
    assert sweeps == [None], "one sweep, of every pair"
    h.advance(SHARE_SWEEP - 1)
    call(laptop, "roster")
    assert len(sweeps) == 1
    h.advance(1)
    call(laptop, "roster")
    assert len(sweeps) == 2


def test_a_pair_room_that_cannot_be_read_is_one_note_per_outage(laptop, cloud, h, monkeypatch):
    """S14. The sweep runs every minute; a note every minute for the length of
    an outage is noise, and noise hides the one note that matters."""
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    working = mcp_server.Client
    monkeypatch.setattr(mcp_server, "Client", outage(h))

    first, error = call(cloud, "roster")
    assert error is False, first
    assert kinds(first) == ["cannot_read"]
    assert "lc" in first["share_notes"][0]["text"]
    for _ in range(3):
        h.advance(SHARE_SWEEP)
        during, error = call(cloud, "roster")
        assert error is False and "share_notes" not in during

    monkeypatch.setattr(mcp_server, "Client", working)
    h.advance(SHARE_SWEEP)
    back, _ = call(cloud, "roster")
    assert kinds(back) == ["offered"], "the offer waited out the outage"

    monkeypatch.setattr(mcp_server, "Client", outage(h))
    h.advance(SHARE_SWEEP)
    again, _ = call(cloud, "roster")
    assert kinds(again) == ["cannot_read"], "a new outage is a new note"


def test_a_pair_whose_records_break_the_sweep_costs_only_that_pair(
        laptop, cloud, desk, h, monkeypatch):
    """Records come from other machines. One whose records trip a bug must not
    fail an unrelated `say`, nor keep the offers of every other pair from
    being read — and what it is told says which pair, never what the records
    held."""
    paired(cloud, laptop, "lc")
    paired(desk, laptop, "ld")
    call(cloud, "share_new", name="core", pairs=["lc"])
    real = sharing.touch
    broken = {"on": True}

    def buggy(*args, **kwargs):
        mine = kwargs.get("pair_store") is laptop._pair_store
        if broken["on"] and mine and kwargs.get("pairs") in (None, ["ld"]):
            raise KeyError("swbsecret1_quoted-from-a-record")
        return real(*args, **kwargs)

    monkeypatch.setattr(sharing, "touch", buggy)
    said, error = call(laptop, "say", channel="general", message="unrelated")
    assert error is False and said["posted"] is True, said
    by_kind = {n["kind"]: n for n in said["share_notes"]}
    assert by_kind["offered"]["pair"] == "lc", "the other pair's offer still arrived"
    assert by_kind["cannot_read"]["pair"] == "ld"
    assert "KeyError" in by_kind["cannot_read"]["text"]
    assert "swbsecret1_" not in json.dumps(said), "the text of a peer's records stays out"

    h.advance(SHARE_SWEEP)
    quiet, _ = call(laptop, "roster")
    assert "share_notes" not in quiet, "the same outage is said once"
    broken["on"] = False
    h.advance(SHARE_SWEEP)
    call(laptop, "roster")
    broken["on"] = True
    h.advance(SHARE_SWEEP)
    again, _ = call(laptop, "roster")
    assert kinds(again) == ["cannot_read"], "cleared by a good sweep, so said again"


def test_a_share_store_that_cannot_be_read_is_said_once_and_fails_nothing(laptop, cloud, h):
    paired(laptop, cloud, "lc")
    path = laptop._pair_store.path / sharing.STORE_FILE
    path.write_text("{ not json")

    first, error = call(laptop, "roster")
    assert error is False, first
    [note] = first["share_notes"]
    assert note["kind"] == "store" and "not valid JSON" in note["text"]
    h.advance(SHARE_SWEEP)
    again, error = call(laptop, "roster")
    assert error is False and "share_notes" not in again
    refused, error = call(laptop, "share_status")
    assert error is True and refused["state"] == "store"
    assert path.read_text() == "{ not json", "it may hold the only copy of a room's keys"


def test_a_machine_that_never_paired_writes_no_share_store(h, tmp_path):
    bridge = machine(h, tmp_path, "fresh")
    for _ in range(2):
        result, error = call(bridge, "roster")
        assert error is False and "share_notes" not in result
        h.advance(SHARE_SWEEP)
    assert not (tmp_path / "fresh").exists()


def test_an_offer_not_delivered_is_written_by_a_later_sweep(laptop, cloud, h, monkeypatch):
    paired(laptop, cloud, "lc")
    working = mcp_server.Client
    monkeypatch.setattr(mcp_server, "Client", outage(h, method="board_set"))

    made, error = call(laptop, "share_new", name="core", pairs=["lc"])
    assert error is False, made
    assert made["offered"] == [] and [u["pair"] for u in made["unreached"]] == ["lc"]
    assert "by itself" in made["next"]
    assert "cannot_read" in kinds(made)

    monkeypatch.setattr(mcp_server, "Client", working)
    h.advance(SHARE_SWEEP)
    call(laptop, "roster")
    told, _ = call(cloud, "roster")
    assert "offered" in kinds(told)


# --- the handle ------------------------------------------------------------------------------


def test_a_room_left_in_another_session_is_refused_not_kept(laptop, cloud, h):
    """S9. Leaving in a terminal while this bridge runs: the bridge looks the
    room up on every call, so its next call through the handle is refused —
    rather than sent on into a room the other machines will stop reading."""
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    call(cloud, "share_accept", offer="core")
    call(cloud, "say", channel="general", message="before", room="shared:core")
    assert cloud._share_clients

    sharing.leave("core", cloud.config, **stores(h, cloud))
    refused, error = call(cloud, "say", channel="general", message="after",
                          room="shared:core")
    assert error is True and (refused["error"], refused["state"]) == ("pairing", "left")
    assert cloud._share_clients == {}, "the left room's client is closed"
    assert room_bodies(h, laptop, "core", "general") == ["before"]


def test_a_name_that_now_means_another_room_is_said_and_followed(laptop, cloud, h):
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    call(cloud, "share_accept", offer="core")
    call(cloud, "say", channel="general", message="to the first room", room="shared:core")
    first = held(cloud, "core")["workspace"]

    elsewhere = stores(h, cloud)
    sharing.leave("core", cloud.config, **elsewhere)
    call(laptop, "share_new", name="core2", pairs=["lc"])
    sharing.accept("core2", cloud.config, name="core", **elsewhere)

    sent, error = call(cloud, "say", channel="general", message="to the second room",
                       room="shared:core")
    assert error is False, sent
    assert any("different room" in w for w in sent["shared"]["warnings"])
    assert set(cloud._share_clients) == {held(cloud, "core")["workspace"]} != {first}
    assert room_bodies(h, laptop, "core2", "general") == ["to the second room"]
    again, _ = call(cloud, "roster", room="shared:core")
    assert again["shared"]["warnings"] == [], "said once, on the call that found it"


def test_a_handle_for_an_offer_says_to_accept_it_first(laptop, cloud):
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    call(cloud, "roster")

    refused, error = call(cloud, "roster", room="shared:core")
    assert error is True and refused["state"] == "offered"
    assert "share_accept(offer=" in refused["next"]


def test_a_handle_that_names_nothing_is_a_refusal_with_its_state(laptop):
    unknown, error = call(laptop, "roster", room="shared:nobody")
    assert error is True and (unknown["error"], unknown["state"]) == ("pairing", "unknown")
    assert "share_status" in unknown["next"]
    bad, error = call(laptop, "roster", room="shared:Not A Name")
    assert error is True and bad["state"] == "invalid"


def test_a_room_not_refreshed_for_a_day_is_touched_when_used(laptop, cloud, h, monkeypatch):
    """S10. A machine that only ever works in the room — no listener, no share
    command — must still keep its records alive and read its peers'. And a
    refresh that cannot reach the pair is not retried on every call."""
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    call(cloud, "share_accept", offer="core")
    touches = []
    real = sharing.touch

    def counted(*args, **kwargs):
        if kwargs.get("pair_store") is laptop._pair_store:
            touches.append(kwargs.get("rooms"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sharing, "touch", counted)
    h.advance(sharing.REFRESH + 60)
    laptop._shares_swept_at = h.now                   # as if a sweep had just run
    call(laptop, "say", channel="general", message="still here", room="shared:core")
    assert touches == [["core"]]
    call(laptop, "say", channel="general", message="still here", room="shared:core")
    assert touches == [["core"]], "refreshed, so no longer due"

    monkeypatch.setattr(mcp_server, "Client", outage(h))
    h.advance(sharing.REFRESH + 60)
    laptop._shares_swept_at = h.now
    for _ in range(3):
        result, error = call(laptop, "roster", room="shared:core")
        assert error is False, result
    assert touches == [["core"], ["core"]], "failing, it waits for the next minute"


# --- the tools ----------------------------------------------------------------------------------


def test_leaving_tells_the_other_side_and_the_next_room_leaves_it_out(laptop, cloud, desk, h):
    paired(laptop, cloud, "lc")
    paired(laptop, desk, "ld")
    call(laptop, "share_new", name="core", pairs=["lc", "ld"])
    call(cloud, "share_accept", offer="core")
    call(desk, "share_accept", offer="core")
    call(cloud, "roster", room="shared:core")

    gone, error = call(cloud, "share_leave", name="core", note="moving on")
    assert error is False, gone
    assert (gone["kind"], gone["told"], gone["untold"]) == ("room", ["lc"], [])
    assert any("nothing was revoked" in w for w in gone["warnings"])
    assert cloud._share_clients == {}, "the room left is not kept open"

    h.advance(SHARE_SWEEP)
    heard, _ = call(laptop, "roster")
    assert "lc left core: moving on" in texts(heard)
    [room] = call(laptop, "share_status", name="core")[0]["rooms"]
    assert [entry["pair"] for entry in room["list"]] == ["ld"]

    made, _ = call(laptop, "share_new", name="core2", from_room="core")
    assert made["offered"] == ["ld"]
    assert all(o["name"] != "core2" for o in call(cloud, "share_status")[0]["offers"])


def test_a_withdrawn_offer_is_refused_and_says_why(laptop, cloud):
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    call(cloud, "roster")
    call(laptop, "share_leave", name="core")

    refused, error = call(cloud, "share_accept", offer="core")
    assert error is True and (refused["error"], refused["state"]) == ("pairing", "withdrawn")
    assert "lc left core" in texts(refused), "what the accept read rides on its refusal"


def test_a_sharing_refusal_says_what_to_call_on_this_surface(laptop, cloud):
    """The core's refusals name CLI flags — 'accept it with --name' — and an
    agent with only this surface has nowhere to put a flag."""
    paired(laptop, cloud, "lc")
    call(cloud, "share_new", name="core", pairs=["lc"])
    call(laptop, "share_new", name="core", pairs=["lc"])

    taken, error = call(cloud, "share_accept", offer="core")
    assert error is True and taken["state"] == "name_taken"
    assert "name=" in taken["next"]
    kept, error = call(cloud, "share_accept", offer="core", name="laptop-core")
    assert error is False and kept["name"] == "laptop-core"

    nobody, error = call(laptop, "share_new", name="other")
    assert error is True and nobody["state"] == "invalid"
    assert "pairs=" in nobody["next"]
    unknown, _ = call(laptop, "share_accept", offer="nothing")
    assert unknown["state"] == "unknown" and "share_status" in unknown["next"]


def test_one_pair_given_as_a_string_is_one_pair(laptop, cloud):
    paired(laptop, cloud, "lc")
    made, error = call(laptop, "share_new", name="core", pairs="lc")
    assert error is False and made["offered"] == ["lc"]


def test_a_room_on_a_sharing_tool_is_ignored_not_resolved(laptop, cloud):
    """An agent working in room='shared:core' passes it to everything, the
    share tools included. Resolving it first would make share_status — the
    call that says why a room is gone — fail on the room being gone."""
    paired(laptop, cloud, "lc")
    status, error = call(laptop, "share_status", room="shared:gone")
    assert error is False and "share store" in status["room_ignored"]
    assert status["next"].startswith("No shared rooms")


# --- the transcript --------------------------------------------------------------------------


def test_no_result_carries_a_room_key_write_key_or_token(laptop, cloud, h):
    """S6. The reason the room travels sealed through pair rooms at all: a
    transcript is a paper trail. The only deliberate way out of the store is
    `--room shared:<name> invite`, in a terminal."""
    paired(laptop, cloud, "lc")
    results = [call(laptop, "share_new", name="core", pairs=["lc"], with_token=True,
                    note="with the token")[0]]
    results += [
        call(cloud, "roster")[0],
        call(cloud, "share_status")[0],
        call(cloud, "share_accept", offer="core")[0],
        call(laptop, "share_status")[0],
        call(laptop, "say", channel="general", message="hi", room="shared:core")[0],
        call(cloud, "history", channel="general", room="shared:core")[0],
        call(cloud, "whoami", room="shared:core")[0],
        call(cloud, "board_set", key="plan", value="tests", room="shared:core")[0],
        call(laptop, "board_list", room="shared:core")[0],
        call(cloud, "rendezvous", want="a reviewer", room="shared:core")[0],
        call(cloud, "share_status", name="core")[0],
        call(cloud, "share_leave", name="core")[0],
    ]
    h.advance(SHARE_SWEEP)
    results += [call(laptop, "roster")[0], call(laptop, "share_status")[0]]

    room = held(laptop, "core")
    assert room["token"] == TOKEN, "the offer did carry the token"
    record = laptop._pair_store.get("lc")
    spec = pairing.pair_room(record.pair_secret)
    keys = [room["key"], room["write_key"], TOKEN, record.secret, spec.key, spec.write_key]
    for payload in results:
        text = json.dumps(payload)
        for value in keys:
            assert value not in text, payload


# --- the schema ----------------------------------------------------------------------------


def test_the_sharing_tools_take_no_room_and_declare_what_they_change():
    tools = {t["name"]: t for t in TOOLS}
    for name in SHARE_TOOLS:
        assert "room" not in tools[name]["inputSchema"]["properties"], name
        assert tools[name]["annotations"]["readOnlyHint"] is False, name
    destructive = {n for n in SHARE_TOOLS if tools[n]["annotations"]["destructiveHint"]}
    assert destructive == {"share_new", "share_accept", "share_leave"}
    assert tools["share_new"]["inputSchema"]["required"] == ["name"]
    assert tools["share_accept"]["inputSchema"]["required"] == ["offer"]
    assert tools["share_leave"]["inputSchema"]["required"] == ["name"]
    assert tools["share_status"]["inputSchema"]["required"] == []


def test_the_room_parameter_documents_the_shared_handle():
    roster = next(t for t in TOOLS if t["name"] == "roster")
    assert "shared:<name>" in roster["inputSchema"]["properties"]["room"]["description"]


# --- where it is kept ----------------------------------------------------------------------


def test_the_share_store_is_kept_beside_the_pair_store(laptop, cloud, h, tmp_path):
    """The two only work together, so a bridge handed a pair store keeps its
    shared rooms beside it — not wherever the environment points, which here
    is somewhere else entirely."""
    paired(laptop, cloud, "lc")
    call(laptop, "share_new", name="core", pairs=["lc"])
    assert laptop._shares().path == laptop._pair_store.path
    assert (laptop._pair_store.path / sharing.STORE_FILE).exists()
    assert not (tmp_path / "pairs" / sharing.STORE_FILE).exists()

    off = machine(h, tmp_path, "off")
    off._pair_store = PairStore("", env={})
    refused, error = call(off, "share_status")
    assert error is True and refused["state"] == "disabled"
    plain, error = call(off, "roster")
    assert error is False and "share_notes" not in plain
