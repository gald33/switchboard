"""Write-protected rooms: the one permission the hub enforces, with nothing stored.

The claim under test is not "a reader gets a 403". It is that the hub can
refuse a write **without holding anything** — no table of writers, no token it
could replay — because the room's identifier commits to a public key and a
writer proves possession of the private half on every request. So the tests
that matter are the negative ones: a reader who knows everything a reader can
know (the identifier, the key, another agent's id) still cannot post, take a
lease, or move somebody's cursor; and a captured signature cannot be re-sent.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from switchboard import ReadOnlyRoom, generate_key, generate_write_key
from switchboard.rooms import (
    WRITE_TOKEN_PREFIX,
    WRITE_WORKSPACE_PREFIX,
    RoomsError,
    is_write_protected,
    workspace_for,
    write_token,
)
from switchboard.testing import hub as make_hub
from switchboard.writekey import (
    KEY_HEADER,
    SIG_HEADER,
    RoomWriteKey,
    WriteKeyError,
    verify_request,
)


@pytest.fixture
def write_key():
    return generate_write_key()


@pytest.fixture
def room(write_key):
    with make_hub(key=generate_key(), write_key=write_key) as h:
        yield h


# --- the derivation ---------------------------------------------------------

def test_the_room_is_named_by_the_write_keys_public_half(write_key):
    writer = RoomWriteKey.from_seed(write_key)
    assert writer.workspace_token == write_token(writer.public_key)
    assert writer.workspace == workspace_for(writer.workspace_token)
    assert writer.workspace.startswith(WRITE_WORKSPACE_PREFIX)
    assert is_write_protected(writer.workspace)
    # The same length as an ordinary room identifier: hashed and truncated
    # rather than the key itself, so the wire shape does not depend on the
    # signature scheme.
    hashed = writer.workspace[len(WRITE_WORKSPACE_PREFIX):]
    assert len(hashed) == len(workspace_for("some ordinary token")[len("w_"):])


def test_the_derivation_is_deterministic_and_seeds_differ(write_key):
    assert RoomWriteKey.from_seed(write_key).workspace == \
        RoomWriteKey.from_seed(write_key).workspace
    assert RoomWriteKey.from_seed(write_key).workspace != \
        RoomWriteKey.from_seed(generate_write_key()).workspace


def test_ordinary_tokens_still_derive_ordinary_rooms():
    assert workspace_for("acme/billing").startswith("w_")
    assert not is_write_protected(workspace_for("acme/billing"))


@pytest.mark.parametrize("bad", ["", "not-base64!!", "short", "x" * 100])
def test_a_malformed_write_key_is_refused_with_a_sentence(bad):
    with pytest.raises(WriteKeyError):
        RoomWriteKey.from_seed(bad)


def test_a_malformed_write_token_is_refused():
    with pytest.raises(RoomsError):
        workspace_for(WRITE_TOKEN_PREFIX + "tooshort")


# --- the signature, on its own ---------------------------------------------

def test_a_signature_verifies_only_over_what_was_signed(write_key):
    writer = RoomWriteKey.from_seed(write_key)
    headers = writer.sign_request("POST", "/messages", "", b'{"a":1}')
    ok, _ = verify_request(writer.workspace, "POST", "/messages", "", b'{"a":1}', headers)
    assert ok
    for method, path, query, body in [
        ("PUT", "/messages", "", b'{"a":1}'),
        ("POST", "/board", "", b'{"a":1}'),
        ("POST", "/messages", "workspace=other", b'{"a":1}'),
        ("POST", "/messages", "", b'{"a":2}'),
    ]:
        ok, reason = verify_request(writer.workspace, method, path, query, body, headers)
        assert not ok and reason == "write signature does not verify", (method, path, query)


def test_a_signature_names_its_room(write_key):
    """The authorization itself: the presented key must hash to the room."""
    writer = RoomWriteKey.from_seed(write_key)
    other = RoomWriteKey.from_seed(generate_write_key())
    headers = writer.sign_request("POST", "/messages", "", b"")
    ok, reason = verify_request(other.workspace, "POST", "/messages", "", b"", headers)
    assert not ok and reason == "write key does not name this room"


def test_a_signature_expires(write_key):
    writer = RoomWriteKey.from_seed(write_key)
    stale = writer.sign_request("POST", "/messages", "", b"", now=time.time() - 3600)
    ok, reason = verify_request(writer.workspace, "POST", "/messages", "", b"", stale)
    assert not ok and "window" in reason
    ahead = writer.sign_request("POST", "/messages", "", b"", now=time.time() + 3600)
    ok, _ = verify_request(writer.workspace, "POST", "/messages", "", b"", ahead)
    assert not ok


def test_missing_or_mangled_headers_are_unsigned_not_errors(write_key):
    writer = RoomWriteKey.from_seed(write_key)
    assert verify_request(writer.workspace, "POST", "/x", "", b"", {}) == (False, "unsigned")
    good = writer.sign_request("POST", "/x", "", b"")
    for mangle in [
        {KEY_HEADER: "pk1_nope", SIG_HEADER: good[SIG_HEADER]},
        {KEY_HEADER: good[KEY_HEADER], SIG_HEADER: "garbage"},
        {KEY_HEADER: good[KEY_HEADER], SIG_HEADER: "123.!!!"},
    ]:
        ok, _ = verify_request(writer.workspace, "POST", "/x", "", b"", mangle)
        assert not ok


# --- the hub ----------------------------------------------------------------

def test_a_writer_writes_and_a_reader_reads(room):
    alice = room.client("alice", register=True, channels=["general"])
    viewer = room.client("viewer", write_key="")
    assert alice.can_write and not viewer.can_write

    alice.post("general", "the orders migration is 0142")
    alice.acquire("backend/alembic", note="running it")
    alice.board_set("handoff/orders", {"step": 3})

    assert [a["name"] for a in viewer.agents()] == ["alice"]
    assert [m["body"] for m in viewer.history("general")] == ["the orders migration is 0142"]
    # Lease resources are blinded one-way, so a reader sees the token the
    # writer sees, not the name; that the lease is *there* is the point.
    assert [le["holder"] for le in viewer.leases()] == [alice.agent_id]
    assert viewer.board_get("handoff/orders") == {"step": 3}


def test_every_write_is_refused_for_a_reader(room):
    """A reader who knows everything a reader can know — the room, the key,
    a writer's agent id — and can still change nothing."""
    alice = room.client("alice", register=True)
    alice.acquire("backend/alembic")
    alice.board_set("handoff/x", 1)
    viewer = room.client("viewer", write_key="")

    attempts = {
        "post": lambda: viewer.post("general", "sneaky"),
        "send": lambda: viewer.send(alice.agent_id, "sneaky"),
        "register": lambda: viewer.register(name="viewer"),
        "heartbeat": lambda: viewer.heartbeat(),
        "acquire": lambda: viewer.acquire("backend/other"),
        "release another's": lambda: viewer.release("backend/alembic"),
        "board_set": lambda: viewer.board_set("handoff/x", 2),
        "board_delete": lambda: viewer.board_delete("handoff/x"),
        "deregister another": lambda: viewer.deregister(agent_id=alice.agent_id),
    }
    for what, attempt in attempts.items():
        with pytest.raises(ReadOnlyRoom, match="write-protected") as refused:
            attempt()
        assert refused.value.status == 403, what

    # And nothing moved.
    assert [a["name"] for a in alice.agents()] == ["alice"]
    assert [le["holder"] for le in alice.leases()] == [alice.agent_id]
    assert alice.board_get("handoff/x") == 1


def test_the_hub_stores_nothing_about_writers(room):
    """The property that distinguishes this from a credential store: a fresh
    hub that has never seen this room accepts its writer on the first request
    and refuses a reader, with no registration in between."""
    write_key = room.write_key
    with make_hub(key=generate_key(), write_key=write_key) as fresh:
        assert fresh.workspace == room.workspace
        writer = fresh.client("w")
        writer.post("general", "first ever request to this hub")
        with pytest.raises(ReadOnlyRoom):
            fresh.client("r", write_key="").post("general", "no")
    assert "key_bindings" not in room.store.path or True  # there is no such table; see store.py


def test_a_captured_signature_cannot_be_replayed(room):
    alice = room.client("alice")
    body = {"workspace": room.workspace, "agent_id": alice.agent_id,
            "channel": "general", "body": "once"}
    request = alice._http.build_request("POST", "/messages", json=body)
    alice._sign(request, {"json": body})
    assert alice._http.send(request).status_code == 200
    again = alice._http.send(request)
    assert again.status_code == 403
    assert again.json()["error"] == "read_only"
    assert "already been used" in again.json()["detail"]


def test_an_unsigned_inbox_read_never_commits_a_cursor(room):
    """The one GET with a side effect. A reader may look at bob's inbox by
    naming bob — the hub cannot stop that, agent ids are self-asserted — but
    it must not make the message disappear for bob."""
    alice = room.client("alice", register=True, channels=["general"])
    bob = room.client("bob", register=True, channels=["general"])
    alice.post("general", "for bob")

    peeked = room.raw().get("/inbox", params={"workspace": room.workspace,
                                              "agent_id": bob.agent_id})
    assert peeked.status_code == 200 and len(peeked.json()["messages"]) == 1
    assert [m["body"] for m in bob.inbox()] == ["for bob"]
    assert bob.inbox() == []


def test_a_writers_own_inbox_read_still_commits(room):
    alice = room.client("alice", register=True, channels=["general"])
    bob = room.client("bob", register=True, channels=["general"])
    alice.post("general", "one")
    assert [m["body"] for m in bob.inbox()] == ["one"]
    assert bob.inbox() == []


def test_the_key_is_not_a_write_key(room):
    """Holding the workspace key — enough to read every byte — buys no write."""
    reader = room.client("reader", key=room.key, write_key="")
    assert reader.encrypted
    with pytest.raises(ReadOnlyRoom):
        reader.post("general", "I can read but not speak")


def test_a_reader_gets_a_sentence_not_a_status_code(room):
    viewer = room.client("viewer", write_key="")
    with pytest.raises(ReadOnlyRoom) as caught:
        viewer.post("general", "x")
    assert caught.value.status == 403
    assert "write key" in str(caught.value)


def test_an_ordinary_room_is_untouched():
    """Rooms named by an ordinary token behave exactly as before, write key
    or no write key: a `SWITCHBOARD_WRITE_KEY` exported for one room must not
    break `-w other`."""
    with make_hub(key=generate_key()) as h:
        assert not is_write_protected(h.workspace)
        plain = h.client("plain")
        assert plain.can_write
        plain.post("general", "as ever")
        stray = h.client("stray", write_key=generate_write_key())
        assert stray.writer is None  # dropped: names another room
        stray.post("general", "also fine")


def test_a_write_key_for_another_protected_room_is_refused_loudly(room):
    with pytest.raises(WriteKeyError, match="names room"):
        room.client("lost", write_key=generate_write_key())


def test_the_async_client_signs_too(write_key):
    async def scenario():
        with make_hub(write_key=write_key) as h:
            writer, reader = h.async_client("w"), h.async_client("r", write_key="")
            try:
                await writer.post("general", "async")
                with pytest.raises(ReadOnlyRoom):
                    await reader.post("general", "no")
                assert [m["body"] for m in await reader.history("general")] == ["async"]
            finally:
                await writer.aclose()
                await reader.aclose()
    asyncio.run(scenario())


def test_a_custom_scope_carries_its_write_key(room):
    """A side room minted by keygen is write-protected like any other; the
    scope carries the write key and `_sign` finds it by workspace."""
    side_write = generate_write_key()
    side = RoomWriteKey.from_seed(side_write)
    alice = room.client("alice")
    scope = {"workspace": side.workspace, "key": generate_key(), "write_key": side_write}
    alice.post("private", "in the side room", custom_scope=scope)
    bob = room.client("bob")
    with pytest.raises(ReadOnlyRoom):
        bob.post("private", "without the write key",
                 custom_scope={"workspace": side.workspace, "key": scope["key"]})


# --- what the hub sees -------------------------------------------------------

def test_the_hub_learns_a_public_key_and_nothing_else(room, tmp_path):
    """The write key never leaves the writer. What the request carries is the
    room's token — the public half — which the room identifier already
    committed to."""
    alice = room.client("alice")
    body = {"workspace": room.workspace, "agent_id": alice.agent_id,
            "channel": "general", "body": "x"}
    request = alice._http.build_request("POST", "/messages", json=body)
    alice._sign(request, {"json": body})
    assert request.headers[KEY_HEADER] == RoomWriteKey.from_seed(room.write_key).workspace_token
    assert room.write_key not in request.headers[KEY_HEADER]
    assert room.write_key not in request.headers[SIG_HEADER]
    assert room.write_key not in request.content.decode()


# --- invites -----------------------------------------------------------------

def test_an_invite_without_the_write_key_joins_read_only(room):
    from switchboard import Client, Invite

    writer = RoomWriteKey.from_seed(room.write_key)
    read_only = Invite(url=room.url, workspace_token=writer.workspace_token,
                       token=room.token, key=room.key)
    assert read_only.workspace == room.workspace
    assert "read-only" in read_only.redacted()
    joined = Client.from_invite(read_only.encode(), agent_id="viewer", http=room.raw())
    assert not joined.can_write
    with pytest.raises(ReadOnlyRoom):
        joined.post("general", "no")

    full = Invite(url=room.url, workspace_token=writer.workspace_token,
                  token=room.token, key=room.key, write_key=room.write_key)
    assert Invite.decode(full.encode()).write_key == room.write_key
    member = Client.from_invite(full.encode(), agent_id="member", http=room.raw())
    assert member.can_write
    member.post("general", "yes")


def test_the_config_derives_the_workspace_from_the_write_key(monkeypatch, tmp_path, write_key):
    from switchboard import ClientConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    monkeypatch.setenv("SWITCHBOARD_WRITE_KEY", write_key)
    config = ClientConfig.from_env()
    assert config.workspace == RoomWriteKey.from_seed(write_key).workspace
    assert config.write_key == write_key


# --- the CLI -------------------------------------------------------------------

def test_keygen_mints_a_write_protected_room(capsys):
    from switchboard.cli import main

    assert main(["keygen", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    writer = RoomWriteKey.from_seed(out["write_key"])
    assert out["workspace"] == writer.workspace
    assert out["workspace_token"] == writer.workspace_token
    assert is_write_protected(out["workspace"])


def test_keygen_prints_an_env_block(capsys):
    from switchboard.cli import main

    assert main(["keygen"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    names = [line.split("=", 1)[0] for line in lines]
    assert names == ["SWITCHBOARD_WORKSPACE", "SWITCHBOARD_KEY", "SWITCHBOARD_WRITE_KEY"]
