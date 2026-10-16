"""Pairing: one code carried by a human, and rooms neither machine ever sends.

The failure this exists for is the paper trail. An invite is the room, so the
chat, ticket or email that carried one holds the room for as long as it
lives. A pairing code is the part *expected* to leak, so these tests are
mostly about what a leaked code is worth: nothing after the pairing, a loud
refusal during it, and never a silent pair with the wrong machine.

Two machines are two stores. The hub is one, in-process, with a clock the
tests move — and every pairing call takes `now=` from that clock, because the
deadlines are compared against the hub's timestamps.
"""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from switchboard import pairing
from switchboard.crypto import WorkspaceCipher
from switchboard.pairing import (
    AlreadyRedeemed,
    PairingCode,
    PairingError,
    PairStore,
)
from switchboard.testing import hub as make_hub


class Machine:
    """One side: its own pair store, the shared hub, its own agent id."""

    def __init__(self, h, tmp_path, label: str, env: dict[str, str] | None = None) -> None:
        self.h = h
        self.label = label
        self.store = PairStore(tmp_path / label / "pairs", env=env if env is not None else {})
        self.base = h.client_config(workspace=f"team-{label}", agent_id=label)
        self.connect = h.client_class()

    def kw(self, **extra):
        return {"store": self.store, "connect": self.connect, "now": self.h.now,
                "agent_id": self.label, **extra}

    def start(self, **extra):
        return pairing.start(self.base, store=self.store, connect=self.connect,
                             now=self.h.now, **extra)

    def accept(self, code, **extra):
        return pairing.accept(code, self.base, **self.kw(**extra))

    def collect(self, name):
        return pairing.collect(name, self.base, **self.kw())

    def resolve(self, name):
        return pairing.resolve(name, self.base, **self.kw())

    def heal(self, name, **extra):
        return pairing.heal(name, self.base, **self.kw(**extra))

    def forget(self, name):
        return pairing.forget(name, self.base, **self.kw())

    def work(self, name):
        """A client in this machine's view of the pair's current work room."""
        resolved = self.resolve(name)
        return self.connect(resolved.config, agent_id=self.label)


@pytest.fixture
def h():
    with make_hub() as hub_:
        yield hub_


@pytest.fixture
def a(h, tmp_path):
    return Machine(h, tmp_path, "laptop")


@pytest.fixture
def b(h, tmp_path):
    return Machine(h, tmp_path, "cloud")


def paired(a: Machine, b: Machine, **start):
    started = a.start(**start)
    accepted = b.accept(started.code)
    collected = a.collect(started.name)
    assert collected.state == "paired", collected.message
    return started.name, accepted


def handshake_bodies(h, code: str) -> list[dict]:
    room = pairing.handshake_room(PairingCode.decode(code).secret)
    reader = h.client("reader", workspace=room.workspace, key=room.key, write_key="")
    return [m["body"] for m in reader.history(pairing.CHANNEL, limit=500)]


# --- the code ------------------------------------------------------------------


def test_a_code_round_trips():
    code = PairingCode(url="https://hub.example.com", secret=b"s" * 32,
                       initiator=b"e" * 32, expires_at=1_700_000_900,
                       commit=b"c" * 16, note="laptop", hello=True)
    assert PairingCode.decode(code.encode()) == code
    assert "=" not in code.encode()


def test_a_code_never_carries_the_hub_token_unless_asked(a):
    """The code is the part expected to land in a ticket. A hub token that is a
    real secret would land there with it, and outlive the code by years."""
    a.base.token = "a-real-perimeter-token"
    plain = PairingCode.decode(a.start().code)
    assert plain.token is None
    carried = PairingCode.decode(a.start(name="with-token", with_token=True).code)
    assert carried.token == "a-real-perimeter-token"


@pytest.mark.parametrize("blob, says", [
    ("swb1_abc", "room invite"),
    ("swbsecret1_abc", "long-lived secret"),
    ("swbp2_abc", "newer switchboard"),
    ("hello", "not a pairing code"),
    ("swbp1_!!!", "corrupt"),
])
def test_a_wrong_string_is_refused_by_what_it_is(blob, says):
    with pytest.raises(PairingError, match=says):
        PairingCode.decode(blob)


def test_an_invite_refuses_a_pairing_code_by_name():
    """`Invite.decode` ignores fields it does not know, so a code that parsed
    as an invite would be joined as an ordinary room by every older surface —
    with the handshake secret sitting in each of them."""
    from switchboard.invite import Invite, InviteError

    with pytest.raises(InviteError, match="pair accept"):
        Invite.decode("swbp1_" + "x" * 40)
    with pytest.raises(InviteError, match="long-lived secret"):
        Invite.decode("swbsecret1_" + "x" * 40)


# --- the derivations are frozen -------------------------------------------------

KAT_S = bytes(range(32))
KAT_R = bytes(range(16))


def _kat_pair():
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    a_ = X25519PrivateKey.from_private_bytes(bytes([7] * 32))
    b_ = X25519PrivateKey.from_private_bytes(bytes([9] * 32))
    pub_a, pub_b = pairing._x25519_public(a_), pairing._x25519_public(b_)
    th = pairing.transcript(pub_a, pub_b, pairing.commitment(KAT_R))
    from_a = pairing.derive_pair_secret(pairing._exchange(a_, pub_b), KAT_S, th)
    from_b = pairing.derive_pair_secret(pairing._exchange(b_, pub_a), KAT_S, th)
    return from_a, from_b, th


def test_both_sides_derive_one_pair_secret_without_sending_it():
    from_a, from_b, _ = _kat_pair()
    assert from_a == from_b


def test_the_v1_derivations_have_not_moved():
    """Known answers. Two machines that paired on one release must land in the
    same rooms after either upgrades, and a derivation that moves fails
    *silently* — both sides in different, equally unguessable, empty rooms.
    So an edit to any label, to the room-id scheme or to the cipher's subkeys
    fails here first. A change is a new protocol, never an edit."""
    secret, _, th = _kat_pair()
    assert pairing.default_name(KAT_S) == "pair-4cbs2t"
    assert pairing._b64e(pairing.commitment(KAT_R)) == "egLftq3xRzt1N379zhbwCg"
    assert pairing._b64e(secret) == "PXD8pY2D_cB5pKjGhZ-Pkco6445Sst94DS5l4LWCTQQ"
    assert pairing.sas(secret, th, KAT_R) == "6962-2786"
    expected = {
        "hs": ("ws_aspFKUFSi4zoM6XN6JQmuo", "gAP-qwlyhB1Vx71emlGVCEzBb-Cu8_fEqn-t4231MZA"),
        "pair": ("ws_l6FYM_WfOw7nsEciQMxB8q", "SF8Ywpq781EyZtFxbUBiAN3joITHZtrZl88x_cveIh4"),
        "w0": ("ws_38icJ3N6EmQbBQChtLI0e5", "PXEQAZHLfWcuUZhsiS1DHF39drM_Ew58RUM7kkuJ2Ao"),
        "w1": ("ws_M8ors_lB_E8SxY8SULIRpg", "0zH1wlBo8_TC5wZtStReHStS-nJYL7lWT3xmCug_6rw"),
    }
    got = {
        "hs": pairing.handshake_room(KAT_S), "pair": pairing.pair_room(secret),
        "w0": pairing.work_room(secret, 0), "w1": pairing.work_room(secret, 1),
    }
    assert {k: (v.workspace, v.key) for k, v in got.items()} == expected
    cipher = WorkspaceCipher.from_key(got["pair"].key, got["pair"].workspace)
    assert cipher.blind("coord/pair/side/initiator", "board") == "1gSa7H3ZTm4UQ7W9Ie6HOw"
    assert cipher.blind("pair", "channel") == "5-GY6NAHRK7NKFzRD71UVg"
    assert pairing._b64e(cipher._payload_key_for(5)) == \
        "n9RLsGgO7ePUaYZ5teNGXgWHZlmGMeSrc44CoypJCQg"


def test_every_room_is_write_protected_and_its_own():
    secret, _, _ = _kat_pair()
    specs = [pairing.handshake_room(KAT_S), pairing.pair_room(secret),
             *(pairing.work_room(secret, n) for n in range(4))]
    assert all(s.workspace.startswith("ws_") for s in specs)
    assert len({s.workspace for s in specs}) == len(specs)
    assert len({s.key for s in specs}) == len(specs)


# --- the handshake -----------------------------------------------------------------


def test_two_machines_pair_and_meet_in_one_work_room(a, b, h):
    started = a.start(note="laptop")
    assert started.verifiable
    accepted = b.accept(started.code)
    assert accepted.name == started.name, "both sides should arrive at one default name"
    assert accepted.state == "unconfirmed"

    collected = a.collect(started.name)
    assert collected.state == "paired"
    assert a.store.pending(started.name) is None, "the ephemeral key must be gone"

    mine, theirs = a.resolve(started.name), b.resolve(started.name)
    assert mine.state == theirs.state == "paired"
    assert mine.config.workspace == theirs.config.workspace
    assert mine.record.sas == theirs.record.sas and mine.record.sas
    with a.work(started.name) as laptop, b.work(started.name) as cloud:
        laptop.post("pair", "hello from the laptop")
        bodies = [m["body"] for m in cloud.history("pair")]
    assert "hello from the laptop" in bodies


def test_the_responder_works_before_it_is_confirmed(a, b):
    """Optimistic: the responder is in work room 0 the moment it accepts, and
    the initiator finds what it said when it arrives."""
    started = a.start()
    b.accept(started.code)
    early = b.resolve(started.name)
    assert early.state == "unconfirmed"
    assert any("unconfirmed" in w for w in early.warnings)
    with b.connect(early.config, agent_id="cloud") as cloud:
        cloud.post("pair", "already here")
    a.collect(started.name)
    with a.work(started.name) as laptop:
        assert "already here" in [m["body"] for m in laptop.history("pair")]


def test_a_leaked_code_after_pairing_opens_nothing(a, b, h):
    """Everything the handshake room holds, read by somebody holding the code:
    public keys, proofs and markers — never the pair secret or a room key."""
    started = a.start()
    b.accept(started.code)
    a.collect(started.name)
    secret = b.store.get(started.name)
    work = pairing.work_room(secret.pair_secret, 0)
    leaked = json.dumps(handshake_bodies(h, started.code))
    for value in (secret.secret, work.key, work.write_key,
                  pairing.pair_room(secret.pair_secret).key):
        assert value not in leaked
    tags = {body["t"] for body in handshake_bodies(h, started.code)}
    assert tags == {"swbp1/hello", "swbp1/answer", "swbp1/consumed"}


def test_a_second_redeemer_is_refused_and_the_first_still_pairs(a, b, h, tmp_path):
    started = a.start()
    b.accept(started.code)
    thief = Machine(h, tmp_path, "thief")
    with pytest.raises(AlreadyRedeemed, match="already answered by a machine other"):
        thief.accept(started.code)
    assert thief.store.get(started.name) is None
    assert a.collect(started.name).state == "paired"


def test_a_redeemer_after_collection_is_told_the_code_was_used(a, b, h, tmp_path):
    """The paper-trail case: the code is read after the pairing finished. The
    reader is refused by what the handshake room says, not by a hub promise
    about deletion — and learns nothing it could use."""
    started = a.start()
    b.accept(started.code)
    a.collect(started.name)
    late = Machine(h, tmp_path, "late")
    with pytest.raises(AlreadyRedeemed, match="answered and collected"):
        late.accept(started.code)
    assert late.store.get(started.name) is None


def test_two_answers_void_the_pairing_on_both_sides(a, b, h):
    """The race the code cannot prevent: two machines answer before the
    initiator looks. Exclusion is impossible — any code holder can write — so
    it is detected instead, and neither side ends up paired with anyone."""
    started = a.start()
    b.accept(started.code)
    room = pairing.handshake_room(PairingCode.decode(started.code).secret)
    rival = h.client("rival", workspace=room.workspace, key=room.key,
                     write_key=room.write_key)
    rival.post(pairing.CHANNEL, {"t": "swbp1/answer", "e": pairing._b64e(b"r" * 32),
                                 "c": "x"}, type="pair.answer")

    voided = a.collect(started.name)
    assert voided.state == "aborted"
    assert a.store.get(started.name) is None and a.store.pending(started.name) is None
    with pytest.raises(PairingError) as exc:
        b.resolve(started.name)
    assert exc.value.state == "aborted"
    assert b.store.get(started.name) is None


def test_the_responder_who_lost_the_race_is_told_at_once(a, b, h, tmp_path):
    """The thief answered first and the initiator collected it. The real
    responder is refused before it sends anything — the code has been used."""
    started = a.start()
    thief = Machine(h, tmp_path, "thief")
    thief.accept(started.code)
    assert a.collect(started.name).state == "paired"
    with pytest.raises(AlreadyRedeemed):
        b.accept(started.code)


def test_an_answer_after_expiry_is_ignored_by_both_sides(a, b, h):
    """Judged on the hub's timestamp, which both sides read the same way — a
    responder whose clock runs slow cannot slip an answer in late."""
    started = a.start(ttl=300)
    h.advance(300 + pairing.SKEW + 30)
    late = Machine(b.h, b.store.path.parent.parent, "late")
    with pytest.raises(PairingError, match="too late") as exc:
        pairing.accept(started.code, late.base, store=late.store, connect=late.connect,
                       now=h.now - 400)
    assert exc.value.state == "expired"
    assert late.store.get(started.name) is None
    waiting = a.collect(started.name)
    assert waiting.state == "waiting"
    assert any("after the code expired" in w for w in waiting.warnings)


def test_a_clearly_expired_code_is_refused_before_any_hub_call(a, b, h):
    started = a.start(ttl=60)

    def no_hub(*args, **kwargs):
        raise AssertionError("an expired code must be refused locally")

    with pytest.raises(PairingError, match="expired"):
        pairing.accept(started.code, b.base, store=b.store, connect=no_hub,
                       now=h.now + 60 + pairing.LOCAL_SLACK + 1)


def test_an_uncollected_handshake_is_dropped_at_its_deadline(a, h):
    started = a.start()
    h.advance(pairing.DEFAULT_TTL + pairing.COLLECT_WINDOW + 1)
    expired = a.collect(started.name)
    assert expired.state == "expired"
    assert "pair forget" in expired.message
    assert a.store.pending(started.name) is None


def test_a_responder_on_the_wrong_hub_sends_nothing(a, b, tmp_path):
    """Same URL, different hub — a loopback or split-DNS address resolves on
    the reader's network. Without the hello the responder would answer into a
    room the initiator never reads, and believe itself paired."""
    started = a.start()
    with make_hub() as elsewhere:
        stranger = Machine(elsewhere, tmp_path, "stranger")
        with pytest.raises(PairingError, match="different hub") as exc:
            stranger.accept(started.code)
        assert exc.value.state == "wrong_hub"
        assert stranger.store.get(started.name) is None


def test_a_responder_that_cannot_keep_the_pair_spends_nothing(a, b, h, tmp_path):
    """The save is the commit point. A responder that sent its answer and then
    failed to save would leave the initiator paired with nobody."""
    started = a.start()
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the store directory should be")
    broken = Machine(h, tmp_path, "broken")
    broken.store = PairStore(blocked / "pairs", env={})
    with pytest.raises(OSError):
        broken.accept(started.code)
    assert [body["t"] for body in handshake_bodies(h, started.code)] == ["swbp1/hello"]
    assert b.accept(started.code).state == "unconfirmed"


def test_a_lost_response_is_not_mistaken_for_a_leak(a, b, h):
    """The answer landed and only the reply was lost. Retrying with a fresh key
    would find 'somebody else's' answer — this side's own — and cry leak."""
    started = a.start()
    real = b.connect

    class Flaky(real):
        def post(self, channel, body, **kwargs):
            result = super().post(channel, body, **kwargs)
            if isinstance(body, dict) and body.get("t") == "swbp1/answer":
                raise httpx.ReadTimeout("response lost")
            return result

    b.connect = Flaky
    assert b.accept(started.code).state == "unconfirmed"
    b.connect = real
    assert a.collect(started.name).state == "paired"
    assert b.resolve(started.name).state == "paired"


def test_a_collect_interrupted_after_saving_is_finished_next_time(a, b, h):
    started = a.start()
    b.accept(started.code)
    real = a.connect

    class Down(real):
        def board_set(self, *args, **kwargs):
            raise httpx.ConnectError("hub went away")

    a.connect = Down
    first = a.collect(started.name)
    assert first.state == "paired"
    assert a.store.pending(started.name).collected, "kept, so the next touch can finish"
    a.connect = real
    assert a.collect(started.name).state == "paired"
    assert a.store.pending(started.name) is None
    assert b.resolve(started.name).state == "paired"


def test_the_responder_learns_it_was_collected_before_the_proof_lands(a, b, h):
    started = a.start()
    b.accept(started.code)
    real = a.connect

    class NoSide(real):
        def board_set(self, *args, **kwargs):
            raise httpx.ConnectError("pair room unreachable")

    a.connect = NoSide
    a.collect(started.name)
    # The consumed marker carries the same proof the side record would, so the
    # responder confirms from the handshake room alone — and a code holder,
    # who cannot compute that proof, cannot fake it.
    still = b.resolve(started.name)
    assert still.state == "paired"
    assert still.record.sas == a.store.get(started.name).sas


def test_an_answer_that_does_not_verify_pairs_nobody(a, b, h):
    started = a.start()
    room = pairing.handshake_room(PairingCode.decode(started.code).secret)
    forger = h.client("forger", workspace=room.workspace, key=room.key,
                      write_key=room.write_key)
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    key = pairing._x25519_public(X25519PrivateKey.generate())
    forger.post(pairing.CHANNEL, {"t": "swbp1/answer", "e": pairing._b64e(key),
                                  "c": pairing._b64e(b"0" * 32)}, type="pair.answer")
    done = a.collect(started.name)
    assert done.state == "aborted"
    assert a.store.get(started.name) is None


# --- the pair room ------------------------------------------------------------------


def test_a_heal_moves_both_sides_to_a_room_the_old_key_cannot_open(a, b, h):
    name, _ = paired(a, b)
    before = a.resolve(name).config
    with a.connect(before, agent_id="laptop") as old:
        old.acquire("db/migrations", ttl=600)
    healed = a.heal(name, reason="work key pasted into a log")
    assert (healed.from_generation, healed.to_generation) == (0, 1)
    assert len(healed.left_behind) == 1, "the lease held in the old room is reported"
    behind = a.resolve(name)
    assert any("not moved to work room 1" in w for w in behind.warnings)

    followed = b.resolve(name)
    assert followed.generation == 1 and followed.moved_from == 0
    assert any("pasted into a log" in w for w in followed.warnings)
    assert followed.config.workspace != before.workspace
    assert not any("not moved" in w for w in a.resolve(name).warnings)

    exposed = pairing.work_room(a.store.get(name).pair_secret, 0)
    with b.connect(before, agent_id="eavesdropper") as old:
        notes = [m["body"] for m in old.history("pair")]
    assert any(isinstance(n, dict) and n.get("t") == "swbp1/moved" for n in notes)
    assert exposed.key != followed.config.key


def test_heals_from_either_side_converge_on_the_higher_generation(a, b):
    """No compare-and-set and nothing to race: each side writes only its own
    record, and the current work room is the highest generation either wrote.
    A second heal from the other side moves on from the first, never back."""
    name, _ = paired(a, b)
    assert a.heal(name).to_generation == 1
    assert b.heal(name).to_generation == 2
    assert a.resolve(name).generation == b.resolve(name).generation == 2


def test_forgetting_tells_the_other_side(a, b):
    name, _ = paired(a, b)
    gone = a.forget(name)
    assert gone.peer_told
    assert a.store.get(name) is None
    with pytest.raises(PairingError) as exc:
        b.resolve(name)
    assert exc.value.state == "ended_by_peer"


def test_a_responder_never_collected_is_told_at_the_deadline(a, b, h):
    started = a.start()
    b.accept(started.code)
    h.advance(pairing.DEFAULT_TTL + pairing.COLLECT_WINDOW + pairing.SKEW + 1)
    with pytest.raises(PairingError) as exc:
        b.resolve(started.name)
    assert exc.value.state == "never_confirmed"
    assert "pair forget" in str(exc.value)


def test_a_week_of_silence_moves_both_sides_somewhere_fresh(a, b, h):
    """The hub forgets everything within a week, including which work room is
    current. Resuming the last one could mean resuming a room a heal abandoned
    because its key leaked — so a lapsed side jumps somewhere certainly fresh,
    and the other side follows the higher generation."""
    name, _ = paired(a, b)
    a.resolve(name)
    b.resolve(name)
    h.advance(pairing.SIDE_TTL + 3600)
    jumped = a.resolve(name)
    assert jumped.generation >= pairing.LAPSE_JUMP
    assert any("moved to a fresh work room" in w for w in jumped.warnings)
    assert a.resolve(name).generation == jumped.generation, "a lapse happens once"
    followed = b.resolve(name)
    assert followed.generation == jumped.generation
    assert any("came back to a pair room" in w for w in followed.warnings)


def test_records_in_use_are_refreshed_before_the_hub_forgets_them(a, b, h):
    name, _ = paired(a, b)
    for _ in range(10):
        h.advance(pairing.SIDE_REFRESH + 60)
        a.resolve(name)
        b.resolve(name)
    assert a.resolve(name).generation == 0, "active pairs never lapse"


def test_a_record_served_under_the_wrong_slot_is_refused(a, b, h):
    name, _ = paired(a, b)
    record = a.store.get(name)
    room = pairing.pair_room(record.pair_secret)
    with h.client("hub-mischief", workspace=room.workspace, key=room.key,
                  write_key=room.write_key) as forger:
        value = forger.board_get("coord/pair/side/initiator")
        forger.board_set("coord/pair/side/responder", value)
    with pytest.raises(PairingError) as exc:
        a.resolve(name)
    assert exc.value.state == "tampered"


def test_an_unknown_healing_strategy_is_refused_not_guessed(a, b, h):
    name, _ = paired(a, b)
    record = a.store.get(name)
    room = pairing.pair_room(record.pair_secret)
    with h.client("future", workspace=room.workspace, key=room.key,
                  write_key=room.write_key) as newer:
        newer.board_set("coord/pair/side/responder",
                        {"t": "swbp1/side", "role": "responder", "g": 9,
                         "s": "ratchet", "at": h.now, "ended": False})
    with pytest.raises(PairingError, match="(?i)upgrade") as exc:
        a.resolve(name)
    assert exc.value.state == "upgrade"


def test_an_unreachable_pair_room_falls_back_and_says_so(a, b):
    name, _ = paired(a, b)

    class Down(a.connect):
        def board_list(self, *args, **kwargs):
            raise httpx.ConnectError("no route")

    a.connect = Down
    fallback = a.resolve(name)
    assert not fallback.checked
    assert fallback.generation == 0
    assert any("last this machine saw" in w for w in fallback.warnings)


def test_heal_refuses_to_write_blind(a, b):
    name, _ = paired(a, b)

    class Down(a.connect):
        def board_list(self, *args, **kwargs):
            raise httpx.ConnectError("no route")

    a.connect = Down
    with pytest.raises(PairingError, match="cannot heal"):
        a.heal(name)


# --- what each machine keeps ---------------------------------------------------------


def test_the_store_is_owner_only(a, b):
    name, _ = paired(a, b)
    directory = a.store.path
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    for path in directory.rglob("*.json"):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, path


def test_no_secret_reaches_a_repr_or_a_summary(a, b):
    started = a.start()
    pending = a.store.pending(started.name)
    assert pending.private not in repr(pending) and pending.secret not in repr(pending)
    assert started.code not in repr(started)
    b.accept(started.code)
    a.collect(started.name)
    record = a.store.get(started.name)
    resolution = a.resolve(started.name)
    for text in (repr(record), json.dumps(resolution.summary())):
        assert record.secret not in text
        assert resolution.config.key not in text


def test_a_chosen_name_travels_so_both_sides_share_it(a, b):
    started = a.start(name="review")
    assert b.accept(started.code).name == "review"
    assert a.collect("review").state == "paired"


def test_names_are_checked_before_anything_is_sent(a, b):
    with pytest.raises(PairingError, match="not a usable pair name"):
        a.start(name="Bad Name")
    started = a.start(name="review")
    with pytest.raises(PairingError, match="already waiting") as exc:
        a.start(name="review")
    assert exc.value.state == "name_taken"
    assert b.accept(started.code).name == "review"
    a.collect("review")
    again = a.start(name="other")
    with pytest.raises(PairingError, match="already has a pair"):
        b.accept(again.code, name="review")


def test_a_disabled_store_refuses_to_pair(h, tmp_path):
    off = Machine(h, tmp_path, "off")
    off.store = PairStore(env={pairing.ENV: ""})
    with pytest.raises(PairingError, match="disabled") as exc:
        off.start()
    assert exc.value.state == "disabled"


def test_a_pair_exported_to_another_environment_keeps_working(a, b, h, tmp_path):
    """The cloud container loses its home directory; its environment's secret
    store does not. The side that stays exports the other side's half, so the
    secret never passes through the cloud session's own transcript."""
    name, _ = paired(a, b)
    a.resolve(name)
    blob = pairing.export(a.store.get(name), for_peer=True)
    assert blob.startswith(pairing.SECRET_PREFIX)
    container = Machine(h, tmp_path, "fresh-container",
                        env={pairing.env_var_for(name): blob})
    there = container.resolve(name)
    assert there.record.role == pairing.RESPONDER and there.state == "paired"
    assert there.config.workspace == a.resolve(name).config.workspace

    a.heal(name)
    moved = container.resolve(name)
    assert moved.generation == 1
    assert any("pair export" in w and "--peer" in w for w in moved.warnings)


def test_an_environment_and_a_store_naming_different_pairs_is_refused(a, b, h, tmp_path):
    name, _ = paired(a, b)
    other_a, other_b = Machine(h, tmp_path, "x"), Machine(h, tmp_path, "y")
    other, _ = paired(other_a, other_b, name=name)
    clash = Machine(h, tmp_path, "laptop", env={
        pairing.env_var_for(name): pairing.export(other_a.store.get(other))})
    with pytest.raises(PairingError, match="different pairs"):
        clash.store.get(name)


def test_status_reports_every_pair_without_one_failure_hiding_the_rest(a, b, h, tmp_path):
    name, _ = paired(a, b)
    waiting = a.start(name="waiting")
    c = Machine(h, tmp_path, "third")
    other, _ = paired(a, c, name="doomed")
    c.forget(other)
    rows = {row["name"]: row for row in pairing.status(a.base, **a.kw())}
    assert rows[name]["state"] == "paired"
    assert rows[waiting.name]["state"] == "pending"
    assert rows["doomed"]["state"] == "ended_by_peer"
    assert all("secret" not in json.dumps(row) for row in rows.values())


def test_the_hub_holds_nothing_it_could_read(a, b, h):
    name, _ = paired(a, b)
    record = a.store.get(name)
    with h.store._conn() as conn:
        rows = conn.execute(
            "SELECT workspace, key, value FROM board UNION ALL "
            "SELECT workspace, channel, body FROM messages").fetchall()
    dump = json.dumps([list(row) for row in rows], default=str)
    for word in ("swbp1/", "initiator", "responder", record.secret, "laptop"):
        assert word not in dump


def test_an_unsent_answer_is_never_resumed_against_another_code(a, b, h):
    """A retry reuses the saved answer — but only for the code it was made
    for. Reusing it for a second code under the same name would answer that
    code with the first code's key, and pair with nobody."""
    first = a.start()
    real = b.connect

    class Down(real):
        def post(self, *args, **kwargs):
            raise httpx.ConnectError("hub went away")

        def history(self, *args, **kwargs):
            if getattr(self, "_seen", False):
                raise httpx.ConnectError("hub went away")
            self._seen = True
            return super().history(*args, **kwargs)

    b.connect = Down
    with pytest.raises(PairingError) as exc:
        b.accept(first.code, name="shared")
    assert exc.value.state == "unsent"
    assert not b.store.get("shared").sent
    b.connect = real
    second = a.start(name="second")
    with pytest.raises(PairingError, match="already has a pair called 'shared'"):
        b.accept(second.code, name="shared")
    assert b.accept(first.code, name="shared").state == "unconfirmed"


def test_forgetting_while_the_hub_is_down_says_the_peer_was_not_told(a, b):
    name, _ = paired(a, b)

    class Down(a.connect):
        def board_set(self, *args, **kwargs):
            raise httpx.ConnectError("no route")

    a.connect = Down
    gone = a.forget(name)
    assert not gone.peer_told
    assert any("was not told" in w for w in gone.warnings)
    assert a.store.get(name) is None


def test_an_expired_handshake_leaves_no_private_key_where_a_listener_passes(a, h):
    """The pending handshake is the one private key this package writes down.
    Past its deadline it can collect nothing, so the first listener, or the next
    `pair start`, drops it — the docs' bound is a property, not a hope."""
    started = a.start()
    h.advance(pairing.DEFAULT_TTL + pairing.COLLECT_WINDOW + 1)
    assert pairing.pending_rooms(a.store, a.base, now=h.now) == []
    assert a.store.pending(started.name) is None
    other = a.start(name="other")
    h.advance(pairing.DEFAULT_TTL + pairing.COLLECT_WINDOW + 1)
    a.start(name="third")
    assert a.store.pending(other.name) is None


def test_a_responder_with_nothing_to_collect_is_not_reported_paired(a, b):
    started = a.start()
    b.accept(started.code)
    assert b.collect(started.name).state == "unconfirmed"


def test_a_heal_says_what_it_strands_on_every_surface(a, b):
    name, _ = paired(a, b)
    healed = a.heal(name)
    assert any("still in work room 0" in w for w in healed.warnings)


def test_a_disabled_store_says_so_instead_of_no_such_pair(h, tmp_path):
    off = Machine(h, tmp_path, "off")
    off.store = PairStore(env={pairing.ENV: ""})
    with pytest.raises(PairingError) as exc:
        off.resolve("anything")
    assert exc.value.state == "disabled"


# --- what the second review found -------------------------------------------------------


def _code_holder(h, code: str, label: str = "holder"):
    """A client in the handshake room, as anybody reading the code has."""
    room = pairing.handshake_room(PairingCode.decode(code).secret)
    return h.client(label, workspace=room.workspace, key=room.key,
                    write_key=room.write_key)


def _thief_answer(code: str) -> dict:
    """A well-formed answer a code holder could write: its own key and proof."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    parsed = PairingCode.decode(code)
    mine = X25519PrivateKey.generate()
    public = pairing._x25519_public(mine)
    th = pairing.transcript(parsed.initiator, public, parsed.commit)
    secret = pairing.derive_pair_secret(
        pairing._exchange(mine, parsed.initiator), parsed.secret, th)
    return {"t": "swbp1/answer", "e": pairing._b64e(public),
            "c": pairing._b64e(pairing.confirmation(secret, pairing.RESPONDER, th))}


def test_a_flooded_handshake_room_aborts_instead_of_pairing_the_last_answer(a, b, h):
    """The hub's channel read is newest-first and capped. A code holder who
    pushes the real answer out of the window with filler, then answers, must
    not be the one paired — a full read is treated as the attack it is."""
    started = a.start()
    b.accept(started.code)
    holder = _code_holder(h, started.code)
    for _ in range(pairing._HISTORY):
        holder.post(pairing.CHANNEL, {"t": "swbp1/hello"})
    holder.post(pairing.CHANNEL, _thief_answer(started.code))
    done = a.collect(started.name)
    assert done.state == "aborted"
    assert "full of messages" in done.message
    with pytest.raises(PairingError) as exc:
        b.resolve(started.name)
    assert exc.value.state == "aborted"


def test_a_forged_aborted_marker_aborts_both_sides_alike(a, b, h):
    """One side voiding while the other pairs is the worst split there is: a
    pair alone in a quiet room. A marker the initiator never wrote aborts it
    too, so both sides reach the same verdict."""
    started = a.start()
    b.accept(started.code)
    _code_holder(h, started.code).post(pairing.CHANNEL, {"t": "swbp1/aborted"})
    with pytest.raises(PairingError) as exc:
        b.resolve(started.name)
    assert exc.value.state == "aborted"
    done = a.collect(started.name)
    assert done.state == "aborted"
    assert a.store.get(started.name) is None


def test_a_forged_consumed_marker_confirms_nothing(a, b, h):
    """The marker names a transcript anyone can compute — but its proof needs
    the pair secret. Without the proof it neither confirms the responder nor
    holds it past its deadline."""
    started = a.start()
    b.accept(started.code)
    record = b.store.get(started.name)
    _code_holder(h, started.code).post(pairing.CHANNEL, {
        "t": "swbp1/consumed", "th": record.transcript, "ci": "AAAA", "r": "AAAA"})
    assert b.resolve(started.name).state == "unconfirmed"
    h.advance(pairing.DEFAULT_TTL + pairing.COLLECT_WINDOW + pairing.SKEW + 1)
    with pytest.raises(PairingError) as exc:
        b.resolve(started.name)
    assert exc.value.state == "never_confirmed"


def test_a_rival_after_collection_does_not_void_the_responder(a, b, h, monkeypatch):
    """The initiator collected this answer; a second redeemer then answers.
    That redeemer is refused on its own side — the pair that was made stands."""
    started = a.start()
    original = pairing._read_handshake
    calls = {"n": 0}

    def collected_in_between(hub, expires_at):
        calls["n"] += 1
        if calls["n"] == 2:  # after this side posted, before it re-reads
            a.collect(started.name)
            _code_holder(h, started.code, "rival").post(
                pairing.CHANNEL, _thief_answer(started.code))
        return original(hub, expires_at)

    monkeypatch.setattr(pairing, "_read_handshake", collected_in_between)
    accepted = b.accept(started.code)
    assert accepted.state == "paired"
    monkeypatch.setattr(pairing, "_read_handshake", original)
    assert a.resolve(started.name).state == b.resolve(started.name).state == "paired"


def test_a_responder_voided_after_collection_tells_the_initiator(a, b, h):
    started = a.start()
    b.accept(started.code)

    class Quiet(a.connect):
        def post(self, channel, body, **kwargs):
            if isinstance(body, dict) and body.get("t") == "swbp1/consumed":
                raise httpx.ConnectError("lost")
            return super().post(channel, body, **kwargs)

        def board_set(self, *args, **kwargs):
            raise httpx.ConnectError("lost")

    a.connect = Quiet
    assert a.collect(started.name).state == "paired"
    a.connect = Machine(h, b.store.path.parent.parent, "x").connect
    _code_holder(h, started.code).post(pairing.CHANNEL, {"t": "swbp1/aborted"})
    with pytest.raises(PairingError):
        b.resolve(started.name)
    with pytest.raises(PairingError) as exc:
        a.resolve(started.name)
    assert exc.value.state == "ended_by_peer"


@pytest.mark.parametrize("key", ["a", "AAAA", pairing._b64e(b"x" * 31)])
def test_an_answer_that_is_not_a_key_aborts_rather_than_crashes(a, b, h, key):
    started = a.start()
    _code_holder(h, started.code).post(pairing.CHANNEL, {
        "t": "swbp1/answer", "e": key, "c": "x"})
    assert a.collect(started.name).state == "aborted"
    assert a.store.pending(started.name) is None
    assert pairing.status(a.base, **a.kw()) == []


def test_a_code_that_expired_unanswered_is_not_reported_pending(a, h):
    started = a.start(ttl=120)
    h.advance(120 + 3 * pairing.SKEW)
    done = a.collect(started.name)
    assert done.state == "expired"
    assert a.store.pending(started.name) is None


def test_an_answer_that_never_reached_the_hub_is_resent_on_next_use(a, b, h):
    started = a.start()

    class Down(b.connect):
        def post(self, *args, **kwargs):
            raise httpx.ConnectError("hub went away")

        def history(self, *args, **kwargs):
            if getattr(self, "_seen", False):
                raise httpx.ConnectError("hub went away")
            self._seen = True
            return super().history(*args, **kwargs)

    real = b.connect
    b.connect = Down
    with pytest.raises(PairingError) as exc:
        b.accept(started.code)
    assert exc.value.state == "unsent"
    b.connect = real
    assert b.resolve(started.name).state == "unconfirmed"
    assert b.store.get(started.name).sent
    assert a.collect(started.name).state == "paired"
    assert b.resolve(started.name).state == "paired"


def test_an_answer_that_missed_the_code_is_dropped_not_left_waiting(a, b, h):
    started = a.start()

    class Down(b.connect):
        def post(self, *args, **kwargs):
            raise httpx.ConnectError("hub went away")

        def history(self, *args, **kwargs):
            if getattr(self, "_seen", False):
                raise httpx.ConnectError("hub went away")
            self._seen = True
            return super().history(*args, **kwargs)

    real = b.connect
    b.connect = Down
    with pytest.raises(PairingError):
        b.accept(started.code)
    b.connect = real
    h.advance(pairing.DEFAULT_TTL + pairing.LOCAL_SLACK + 1)
    with pytest.raises(PairingError) as exc:
        b.resolve(started.name)
    assert exc.value.state == "unsent"
    assert b.store.get(started.name) is None


def test_an_active_side_with_an_idle_peer_never_lapses(a, b, h):
    """Only a gap in *this* side's reading can hide a heal. A side that keeps
    working while its peer sleeps for a fortnight stays in its room — and is
    told the peer is quiet, then absent, rather than nothing."""
    name, _ = paired(a, b)
    a.resolve(name)
    b.resolve(name)
    said = []
    for _ in range(14):
        h.advance(86400)
        resolution = a.resolve(name)
        assert resolution.generation == 0
        said += resolution.warnings
    assert any("no sign of the other side" in w for w in said)
    assert any("not in the pair room" in w for w in said)
    assert b.resolve(name).generation == 0


def test_lapses_from_one_stale_export_do_not_land_in_the_same_room(a, b, h, tmp_path):
    name, _ = paired(a, b)
    a.resolve(name)
    blob = pairing.export(a.store.get(name), for_peer=True)
    targets = set()
    for i in range(3):
        h.advance(pairing.SIDE_TTL + 3600)
        fresh = Machine(h, tmp_path, f"container-{i}", env={pairing.env_var_for(name): blob})
        targets.add(fresh.resolve(name).generation)
    assert len(targets) == 3, "each lapse picks a room no earlier lapse could have"


def test_a_side_that_ended_here_is_not_resurrected_by_a_racing_resolve(a, b, h):
    name, _ = paired(a, b)
    record = a.store.get(name)
    a.store.delete(name)
    with pytest.raises(PairingError) as exc:
        pairing._keep(a.store, record)
    assert exc.value.state == "ended"
    assert a.store.get(name) is None


def test_a_lower_generation_never_overwrites_a_higher_one_on_disk(a, b):
    name, _ = paired(a, b)
    stale = a.store.get(name)
    healed = a.heal(name)
    assert healed.to_generation == 1
    pairing._keep(a.store, stale)
    assert a.store.get(name).generation == 1


def test_this_sides_own_ended_record_finishes_the_forget(a, b, h):
    name, _ = paired(a, b)
    pairing._publish_side(a.store.get(name), a.base, connect=a.connect, now=h.now,
                          ended=True)
    with pytest.raises(PairingError) as exc:
        a.resolve(name)
    assert exc.value.state == "ended"
    assert a.store.get(name) is None


def test_an_unconfirmed_pair_is_not_exported_frozen(a, b):
    started = a.start()
    b.accept(started.code)
    with pytest.raises(PairingError) as exc:
        pairing.export(b.store.get(started.name))
    assert exc.value.state == "unconfirmed"


def test_a_heal_wakes_this_machines_own_listener_too(a, b, h):
    """The moved note goes out under a throwaway id: a listener never wakes on
    its own agent's messages, and this machine's own listener is parked in the
    room being left as surely as the other side's is."""
    name, _ = paired(a, b)
    old = a.resolve(name).config
    a.heal(name)
    with a.connect(old, agent_id="laptop") as mine:
        tags = [pairing.message_tag(m) for m in mine.inbox(channels=["pair"])]
    assert "swbp1/moved" in tags


def test_pairing_messages_are_ordinary_notes_on_the_wire(a, b, h):
    name, _ = paired(a, b)
    a.heal(name)
    with h.store._conn() as conn:
        types = {row[0] for row in conn.execute("SELECT DISTINCT type FROM messages")}
    assert types == {"note"}, "a `pair.*` type would tell the hub which rooms are handshakes"
