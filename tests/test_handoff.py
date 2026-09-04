"""A session crosses the hub as an opaque payload and arrives installed.

Two agents on one hub, each with its own Claude config dir. The sender
publishes; the receiver collects. The properties that matter are the ones
the design promises: the hub never sees the transcript in a keyed room, the
receiver installs only what a known sender announced, the board entry is
gone the moment it is claimed, a capsule nobody collected expires on its
own, and every step is a function call with no model in the loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard import claude_session as cs
from switchboard import handoff
from switchboard.crypto import generate_key
from switchboard.testing import hub as make_hub

WS = "handoff-ws"
SID = "3c1f7c2e-4b6a-4e0e-9a3f-2f9d8a1b5c7d"
CODEWORD = "TANGERINE-4471"


def _record(**fields):
    base = {"type": "user", "sessionId": SID, "cwd": "/Users/gal/code/switchboard",
            "version": "2.1.260", "gitBranch": "feat/handoff"}
    base.update(fields)
    return json.dumps(base)


def _source_session(root: Path) -> tuple[Path, bytes]:
    cfg = root / "sender-claude"
    project = cfg / "projects" / cs.project_key("/Users/gal/code/switchboard")
    project.mkdir(parents=True)
    transcript = project / f"{SID}.jsonl"
    transcript.write_text("\n".join([
        _record(type="user", message={"role": "user", "content": f"codeword {CODEWORD}"}),
        _record(type="assistant", message={"role": "assistant", "content": []}),
    ]) + "\n")
    (project / SID / "subagents").mkdir(parents=True)
    (project / SID / "subagents" / "agent-1.jsonl").write_text(_record() + "\n")
    return cfg, transcript.read_bytes()


@pytest.fixture
def room(tmp_path):
    key = generate_key()
    with make_hub(workspace=WS, key=key) as h:
        sender = h.client("sender", register=True)
        receiver = h.client("receiver", register=True)
        yield h, sender, receiver


# --- the round trip ----------------------------------------------------------

def test_handoff_arrives_installed_and_leaves_nothing_on_the_hub(room, tmp_path):
    h, sender, receiver = room
    src_cfg, transcript_bytes = _source_session(tmp_path)

    sent = handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    assert sent["key"] == f"sessions/{SID}"
    assert sent["to"] == receiver.agent_id
    assert sent["encrypted"] is True
    assert sent["files"] == 2

    # On the hub: a sealed value under a blinded key, and a sealed pointer of
    # the default type. Nothing that reads as a transcript, a codeword, a
    # session id — or even the word "session".
    for entry in h.board():
        assert CODEWORD not in entry.value and SID not in entry.value
        assert SID not in entry.key and "session" not in entry.key
    [message] = h.messages(f"@{receiver.agent_id}")
    assert CODEWORD not in message.body and SID not in message.body
    assert message.type == "note"

    dest_cfg = tmp_path / "receiver-claude"
    got = handoff.receive(receiver, config_dir=str(dest_cfg), cwd="/workspace/switchboard")
    assert got["listening_as"] == receiver.agent_id
    assert got["pointers"] == 1 and got["missing"] == [] and got["other"] == []
    [installed] = got["installed"]
    assert installed["session_id"] == SID
    assert installed["verified"] is True
    assert installed["deleted_from_board"] is True
    assert installed["acknowledged"] is True
    assert installed["from"]["who"] == "sender"
    landed = dest_cfg / "projects" / "-workspace-switchboard"
    assert (landed / f"{SID}.jsonl").read_bytes() == transcript_bytes
    assert (landed / SID / "subagents" / "agent-1.jsonl").exists()
    assert got["resume"] == [f"cd /workspace/switchboard && CLAUDE_CONFIG_DIR={dest_cfg} "
                             f"claude --resume {SID}"]

    # Collected means gone from the board, gone from the inbox, and the sender
    # has a receipt saying who took it and how to resume.
    assert h.board() == []
    assert receiver.inbox(peek=True) == []
    [receipt] = sender.inbox()
    assert receipt["body"]["t"] == handoff.RECEIPT_TYPE
    assert receipt["body"]["session_id"] == SID and receipt["body"]["by"] == "receiver"
    again = handoff.receive(receiver, config_dir=str(dest_cfg), cwd="/workspace/switchboard")
    assert again["installed"] == [] and again["pointers"] == 0


def test_a_checkpoint_needs_no_recipient_and_is_collected_by_id(room, tmp_path):
    h, sender, receiver = room
    src_cfg, transcript_bytes = _source_session(tmp_path)
    published = handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    assert published["to"] is None and "seq" not in published
    assert receiver.inbox(peek=True) == []

    got = handoff.receive(receiver, session_id=SID, config_dir=str(tmp_path / "r"), cwd="/w")
    [installed] = got["installed"]
    assert Path(installed["transcript"]).read_bytes() == transcript_bytes
    assert installed["verified"] is None, "nobody vouched; the collector trusted the room"
    assert h.board() == []


def test_the_capsule_expires_on_its_own(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    sent = handoff.handoff(sender, to=receiver.agent_id, session_id=SID,
                           config_dir=str(src_cfg), ttl=30)
    assert sent["expires_in"] == 30
    h.advance(31)
    got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["installed"] == [] and got["pointers"] == 0, "the pointer expired with it"
    got = handoff.receive(receiver, session_id=SID, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["installed"] == [] and "expired" in got["missing"][0]["reason"]
    assert h.board() == []


def test_the_default_ttl_is_short_and_the_ceiling_is_the_hubs(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    sent = handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    assert sent["expires_in"] == handoff.DEFAULT_TTL == 600
    capsule = cs.package(SID, config_dir=src_cfg)
    huge = handoff.publish(sender, capsule, ttl=10**9)
    assert huge["expires_in"] == 7 * 86400
    with pytest.raises(handoff.HandoffError):
        handoff.publish(sender, capsule, ttl=0)


def test_leases_are_listed_for_the_receiver_and_kept_unless_asked(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    sender.acquire("src/store.py")
    sender.acquire("migrations/")
    sent = handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    # Under a key the hub lists resources as tokens, so that is what travels:
    # two of them, still held, named in the pointer for the receiver to take.
    assert len(sent["held_leases"]) == 2 and sent["released_leases"] == []
    assert len(h.leases()) == 2
    [pointer] = handoff.take_delivery(receiver)["pointers"]
    assert pointer["held_leases"] == sent["held_leases"]

    dropped = handoff.handoff(sender, to=receiver.agent_id, session_id=SID,
                              config_dir=str(src_cfg), release_leases=True)
    assert len(dropped["released_leases"]) == 2 and dropped["held_leases"] == []
    assert h.leases() == []


def test_a_lease_listed_by_the_hub_can_be_released_by_what_it_listed(room):
    """The Stop hook's loop: `claims --holder me` then `release <resource>`.

    Under a key the listing reports blinded tokens, and releasing by one used
    to blind it again and match nothing — reported as "no lease" while the
    lease stood. Pinned here because a handoff hands leases on the same way.
    """
    h, sender, receiver = room
    sender.acquire("src/store.py")
    [listed] = sender.leases(holder=sender.agent_id)
    assert listed["resource"] != "src/store.py", "the hub lists the token, not the name"
    assert sender.release(listed["resource"]) is True
    assert h.leases() == []
    # And a readable name still blinds, so both spellings release the same lease.
    sender.acquire("src/store.py")
    assert sender.release("src/store.py") is True


def test_a_newer_handoff_replaces_the_older_one(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    first = handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    transcript = Path(src_cfg / "projects" / "-Users-gal-code-switchboard" / f"{SID}.jsonl")
    with transcript.open("a") as fh:
        fh.write(_record(type="assistant") + "\n")
    second = handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    assert second["revision"] == first["revision"] + 1
    # Two pointers in the inbox, one session: the newest pointer wins, it
    # matches what is on the board, and the receiver installs it once.
    got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["pointers"] == 1 and len(got["installed"]) == 1
    assert Path(got["installed"][0]["transcript"]).read_bytes() == transcript.read_bytes()


def test_a_stale_pointer_does_not_install_a_replaced_capsule(room, tmp_path):
    """The sender re-published after pointing; the old pointer's hash no longer
    matches what is on the board, so it is not acted on."""
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    [stale] = handoff.take_delivery(receiver)["pointers"]
    transcript = Path(src_cfg / "projects" / "-Users-gal-code-switchboard" / f"{SID}.jsonl")
    with transcript.open("a") as fh:
        fh.write(_record(type="assistant") + "\n")
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    got = handoff.receive_one(receiver, SID, pointer=stale,
                              config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["installed"] is False and "sha256 differs" in got["reason"]
    assert h.board() != [], "not claimed, not deleted"


def test_two_receivers_and_exactly_one_installs(room, tmp_path):
    h, sender, receiver = room
    second = h.client("second", register=True)
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    envelope = handoff.fetch(receiver, SID)
    # Both have read the capsule; the delete is the claim and only one wins.
    assert handoff.consume(receiver, SID) is True
    assert handoff.consume(second, SID) is False
    assert envelope is not None
    got = handoff.receive(second, session_id=SID, config_dir=str(tmp_path / "s"), cwd="/w")
    assert got["installed"] == [] and "collected" in got["missing"][0]["reason"]


# --- refusals ------------------------------------------------------------------

def test_an_unencrypted_room_is_refused_unless_allowed(tmp_path):
    with make_hub(workspace=WS) as h:
        sender = h.client("sender", register=True)
        src_cfg, _ = _source_session(tmp_path)
        with pytest.raises(handoff.HandoffError, match="not encrypted"):
            handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
        assert h.board() == []
        sent = handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg),
                               allow_plaintext=True)
        assert sent["encrypted"] is False


def test_a_pointer_nobody_vouches_for_is_not_installed(room, tmp_path):
    """The hub binds an agent id to nothing, so an impostor can post as the
    sender; what it cannot do is sign as the sender."""
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    mallory = h.client("mallory", agent_id=sender.agent_id)
    mallory.send(receiver.agent_id, {
        "t": handoff.POINTER_TYPE, "session_id": SID, "key": handoff.key_for(SID),
        "sha256": handoff.fetch(receiver, SID)["sha256"],
    })
    got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["installed"] == []
    [skipped] = got["missing"]
    assert skipped["signature"] != "verified" and "vouches" in skipped["reason"]
    assert h.board() != [], "left on the board for a human to look at"
    # Collecting it anyway is an explicit choice, and says so in the result.
    forced = handoff.receive(receiver, session_id=SID, config_dir=str(tmp_path / "r"), cwd="/w")
    assert forced["installed"][0]["verified"] is None


def test_without_a_session_id_the_environment_decides(room, tmp_path, monkeypatch):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    monkeypatch.delenv(cs.SESSION_ID_VAR, raising=False)
    with pytest.raises(handoff.HandoffError, match="no session id"):
        handoff.handoff(sender, config_dir=str(src_cfg))
    monkeypatch.setenv(cs.SESSION_ID_VAR, SID)
    assert handoff.handoff(sender, config_dir=str(src_cfg))["session_id"] == SID
    monkeypatch.setenv(cs.SESSION_ID_VAR, "11111111-2222-3333-4444-555555555555")
    with pytest.raises(handoff.HandoffError, match="not found"):
        handoff.handoff(sender, config_dir=str(src_cfg))


def test_a_foreign_or_forged_board_entry_is_refused_and_kept(room, tmp_path):
    h, sender, receiver = room
    sender.board_set(handoff.key_for(SID), {"t": "something-else"})
    with pytest.raises(handoff.HandoffError, match="not a session capsule"):
        handoff.receive_one(receiver, SID, config_dir=str(tmp_path / "r"), cwd="/w")

    src_cfg, _ = _source_session(tmp_path)
    capsule = cs.package(SID, config_dir=src_cfg)
    capsule["source_harness"] = {"name": "codex", "version": "1"}
    with pytest.raises(handoff.HandoffError, match="codex"):
        handoff.publish(sender, capsule)

    capsule = cs.package(SID, config_dir=src_cfg)
    capsule["files"][0]["data"] = "AAAA"
    handoff.publish(sender, capsule)
    with pytest.raises(handoff.HandoffError, match="corrupt"):
        handoff.receive_one(receiver, SID, config_dir=str(tmp_path / "r"), cwd="/w")
    # Refused after the claim is put back: the entry is there for a human to
    # look at, with what was left of its TTL.
    assert handoff.fetch(receiver, SID) is not None


def test_other_messages_in_the_inbox_are_handed_back_not_lost(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    sender.send(receiver.agent_id, "unrelated: the build is green")
    handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert len(got["installed"]) == 1
    assert [m["body"] for m in got["other"]] == ["unrelated: the build is green"]


# --- the shapes a real day produces --------------------------------------------

def test_a_pointer_the_model_already_read_is_still_found(room, tmp_path):
    """Every MCP result says unread_dms; the model calls inbox; the cursor
    moves past the pointer. Collecting afterwards must still work."""
    h, sender, receiver = room
    src_cfg, transcript_bytes = _source_session(tmp_path)
    handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    drained = receiver.inbox()
    assert len(drained) == 1 and receiver.inbox() == []
    got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["pointers"] == 1 and got["installed"][0]["verified"] is True
    assert Path(got["installed"][0]["transcript"]).read_bytes() == transcript_bytes
    # Collected; the pointer history still remembers is noted as stale, and
    # nothing is reported as missing.
    again = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert again["installed"] == [] and again["missing"] == []
    assert again["stale"] == [SID] and again["pointers"] == 0


def test_the_round_trip_comes_home_to_one_copy(room, tmp_path):
    """A -> B -> A with default arguments ends with one transcript under A's
    original key, resumed from A's original directory."""
    h, alice, bob = room
    a_cfg, _ = _source_session(tmp_path)
    a_home = a_cfg / "projects" / "-Users-gal-code-switchboard" / f"{SID}.jsonl"
    b_cfg = tmp_path / "bob-claude"

    handoff.handoff(alice, to=bob.agent_id, session_id=SID, config_dir=str(a_cfg))
    [on_b] = handoff.receive(bob, config_dir=str(b_cfg), cwd="/workspace/switchboard")["installed"]
    with Path(on_b["transcript"]).open("a") as fh:
        fh.write(_record(type="assistant", cwd="/workspace/switchboard") + "\n")

    handoff.handoff(bob, to=alice.agent_id, session_id=SID, config_dir=str(b_cfg))
    [back] = handoff.receive(alice, config_dir=str(a_cfg))["installed"]
    assert Path(back["transcript"]) == a_home
    assert back["resume_cwd"] == "/Users/gal/code/switchboard"
    assert len(back["backed_up"]) == 1
    assert cs.find_transcripts(a_cfg, SID) == [a_home]
    assert a_home.read_bytes() == Path(on_b["transcript"]).read_bytes()


def test_the_pointer_never_outlives_a_message(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    sent = handoff.handoff(sender, to=receiver.agent_id, session_id=SID,
                           config_dir=str(src_cfg), ttl=3 * 86400)
    assert sent["expires_in"] == 3 * 86400
    [message] = h.messages(f"@{receiver.agent_id}")
    assert message.expires_at - h.now == 86400


# --- the hub really does not keep it ------------------------------------------------

def _rows(db: str, table: str = "board") -> int:
    """What is physically in the table, with no expiry filter in the way."""
    import sqlite3

    with sqlite3.connect(db) as raw:
        return raw.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608


def _on_disk(db: str) -> bytes:
    """Everything an operator could read off disk: main file, WAL and shm."""
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        path = Path(db + suffix)
        if path.exists():
            blob += path.read_bytes()
    return blob


def test_a_collected_capsule_is_deleted_from_the_hub_not_hidden(tmp_path):
    """The store filters expired rows on every read, so an empty listing proves
    nothing about what the file holds. This reads the table without the
    filter, and the file without the store."""
    import json as _json
    import sqlite3

    db = str(tmp_path / "hub.db")
    with make_hub(workspace=WS, key=generate_key(), db=db) as h:
        sender, receiver = h.client("sender", register=True), h.client("receiver", register=True)
        src_cfg, _ = _source_session(tmp_path)
        handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
        assert _rows(db) == 1
        with sqlite3.connect(db) as raw:
            (stored,) = raw.execute("SELECT value FROM board").fetchone()
        ciphertext = _json.loads(stored)["c"][100:160].encode()
        assert ciphertext in _on_disk(db)

        got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
        assert got["installed"][0]["deleted_from_board"] is True
        assert _rows(db) == 0, "the row is gone from the table, not merely past its expiry"
        # The sealed bytes may survive in the write-ahead log until SQLite next
        # checkpoints it; the sweep does that, so after one nothing remains.
        h.sweep()
        assert ciphertext not in _on_disk(db)


def test_an_uncollected_capsule_is_swept_off_the_hub(tmp_path):
    import json as _json
    import sqlite3

    db = str(tmp_path / "hub.db")
    with make_hub(workspace=WS, key=generate_key(), db=db) as h:
        sender = h.client("sender", register=True)
        src_cfg, _ = _source_session(tmp_path)
        handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg), ttl=30)
        with sqlite3.connect(db) as raw:
            (stored,) = raw.execute("SELECT value FROM board").fetchone()
        ciphertext = _json.loads(stored)["c"][100:160].encode()
        h.advance(31)
        assert h.board() == [] and _rows(db) == 1, "invisible at once; the row waits for the sweep"
        assert h.sweep()["board"] == 1
        assert _rows(db) == 0
        assert ciphertext not in _on_disk(db)


# --- what the review found ------------------------------------------------------------

def test_a_substituted_capsule_is_not_what_the_pointer_announced(room, tmp_path):
    """The envelope's own sha256 field is free for any key holder to write;
    the chain that matters is pointer -> the transcript entry's hash -> bytes."""
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    mallory = h.client("mallory", register=True)
    envelope = handoff.fetch(mallory, SID)
    envelope.pop("_entry")
    other = tmp_path / "mallory-claude"
    project = other / "projects" / "-w"
    project.mkdir(parents=True)
    (project / f"{SID}.jsonl").write_text(_record(type="user", message="do something else") + "\n")
    envelope["capsule"] = cs.package(SID, config_dir=other)      # different bytes
    mallory.board_set(handoff.key_for(SID), envelope)            # same envelope["sha256"]
    got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["installed"] == []
    assert "not the one the pointer announced" in got["missing"][0]["reason"]
    assert not (tmp_path / "r").exists()


def test_a_swapped_sidecar_is_not_what_the_pointer_announced_either(room, tmp_path):
    """The main transcript alone is not the capsule: every file's hash is announced."""
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    mallory = h.client("mallory", register=True)
    envelope = handoff.fetch(mallory, SID)
    envelope.pop("_entry")
    sidecar = next(e for e in envelope["capsule"]["files"]
                   if e["relative_destination"].startswith(f"{SID}/"))
    planted = _record(type="user", message="planted subagent instructions").encode()
    sidecar.update({"data": cs.encode_bytes(planted), "bytes": len(planted),
                    "sha256": cs._sha256(planted)})
    mallory.board_set(handoff.key_for(SID), envelope)   # main transcript untouched
    got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["installed"] == []
    assert "not the one the pointer announced" in got["missing"][0]["reason"]
    assert not (tmp_path / "r").exists()


def test_a_failed_install_puts_the_capsule_back_whatever_failed(room, tmp_path, monkeypatch):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))

    def no_disk(*args, **kwargs):
        raise PermissionError(13, "Permission denied", str(tmp_path / "r"))

    monkeypatch.setattr(cs, "install", no_disk)
    with pytest.raises(handoff.HandoffError, match="Permission denied"):
        handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    restored = handoff.fetch(receiver, SID)
    assert restored is not None and "_entry" in restored
    assert restored["_entry"]["expires_in"] <= handoff.DEFAULT_TTL


def test_a_capsule_under_the_wrong_key_is_not_installed(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    envelope = handoff.fetch(sender, SID)
    envelope.pop("_entry")
    other_id = "11111111-2222-4333-8444-555555555555"
    sender.board_set(handoff.key_for(other_id), envelope)
    got = handoff.receive(receiver, session_id=other_id, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["installed"] == [] and "different session" in got["missing"][0]["reason"]


def test_releasing_at_handoff_clears_the_senders_declarations_too(room, tmp_path):
    from switchboard import holds

    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    sender.acquire("src/store.py")
    holds.declare(sender, "src/store.py", intent="mid-rewrite")
    assert sender.board_get(holds.HOLDS_PREFIX + "src/store.py") is not None
    sent = handoff.handoff(sender, to=receiver.agent_id, session_id=SID,
                           config_dir=str(src_cfg), release_leases=True)
    assert len(sent["released_leases"]) == 1
    assert sender.board_get(holds.HOLDS_PREFIX + "src/store.py") is None


def test_an_unverified_pointer_for_a_gone_capsule_is_stale_not_missing(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    # A pointer from a signer whose key was never on the roster (a client
    # that never registered), read once by the model, for a capsule that is
    # gone by the time anyone collects.
    ghost = h.client("ghost")
    ghost.send(receiver.agent_id, {"t": handoff.POINTER_TYPE, "session_id": SID,
                                   "key": handoff.key_for(SID), "sha256": "0" * 64})
    receiver.inbox()
    handoff.consume(sender, SID)
    got = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")
    assert got["missing"] == [] and got["stale"] == [SID] and got["pointers"] == 0


def test_the_result_names_the_signer_the_hub_attested_to(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, to=receiver.agent_id, session_id=SID, config_dir=str(src_cfg))
    [installed] = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")["installed"]
    assert installed["sender"] == sender.agent_id and installed["verified"] is True
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    [by_id] = handoff.receive(receiver, session_id=SID, config_dir=str(tmp_path / "r"),
                              cwd="/w")["installed"]
    assert by_id["sender"] is None and by_id["verified"] is None


def test_two_receivers_through_receive_and_exactly_one_installs(room, tmp_path, monkeypatch):
    h, sender, receiver = room
    second = h.client("second", register=True)
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    winner = handoff.receive(receiver, session_id=SID, config_dir=str(tmp_path / "r"), cwd="/w")
    loser = handoff.receive(second, session_id=SID, config_dir=str(tmp_path / "s"), cwd="/w")
    assert winner["installed"][0]["deleted_from_board"] is True
    assert loser["installed"] == [] and "collected" in loser["missing"][0]["reason"]
    # And the race itself: both have read the capsule, one delete wins.
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    real_delete = second.board_delete
    monkeypatch.setattr(second, "board_delete", lambda key: real_delete(key) and False)
    lost = handoff.receive(second, session_id=SID, config_dir=str(tmp_path / "s"), cwd="/w")
    assert lost["installed"] == [] and "another receiver" in lost["missing"][0]["reason"]


def test_a_put_back_capsule_keeps_the_ttl_it_had_left(room, tmp_path, monkeypatch):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg), ttl=300)
    h.advance(100)
    monkeypatch.setattr(cs, "install", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(handoff.HandoffError):
        handoff.receive(receiver, session_id=SID, config_dir=str(tmp_path / "r"), cwd="/w")
    restored = handoff.fetch(receiver, SID)
    assert restored["_entry"]["expires_in"] == 200
    assert restored["_entry"]["revision"] == 1, "a deleted row comes back as a new one"


def test_the_unverified_override_installs_but_still_checks_the_bytes(room, tmp_path):
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)
    handoff.handoff(sender, session_id=SID, config_dir=str(src_cfg))
    ghost = h.client("ghost")   # never registered: its signature cannot be checked
    sha = handoff.fetch(receiver, SID)["sha256"]
    ghost.send(receiver.agent_id, {"t": handoff.POINTER_TYPE, "session_id": SID,
                                   "key": handoff.key_for(SID), "sha256": sha})
    refused = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w", keep=True)
    assert refused["installed"] == [] and refused["missing"][0]["signature"] == "unknown"
    [got] = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w",
                            unverified=True, keep=True)["installed"]
    assert got["verified"] is False and got["deleted_from_board"] is False
    assert h.board() != [], "--keep left it there"
    # Unverified is not unchecked: an announced hash that does not match still refuses.
    ghost.send(receiver.agent_id, {"t": handoff.POINTER_TYPE, "session_id": SID,
                                   "key": handoff.key_for(SID), "sha256": "0" * 64})
    bad = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w", unverified=True)
    assert bad["installed"] == []
    assert "not the one the pointer announced" in bad["missing"][0]["reason"]


def test_a_lean_capsule_announces_what_it_left_behind(room, tmp_path):
    """The count rides on the envelope, so a receiver reading the board can
    tell a stripped capsule from a session that never had subagents."""
    h, sender, receiver = room
    src_cfg, _ = _source_session(tmp_path)

    sent = handoff.handoff(sender, to=receiver.agent_id, session_id=SID,
                           config_dir=str(src_cfg), subagents=False)
    assert sent["omitted_subagent_files"] == 1   # this fixture has one sidecar
    assert sent["files"] == 1

    [got] = handoff.receive(receiver, config_dir=str(tmp_path / "r"), cwd="/w")["installed"]
    assert got["installed"] is True
    # The conversation is all there; only the subagents' own work stayed home.
    assert Path(got["transcript"]).is_file()
    assert not (Path(got["project_dir"]) / SID).exists()
