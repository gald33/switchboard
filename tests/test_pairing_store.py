"""Pairing with a store that is not a directory.

A server that pairs on its users' behalf — one pairing per user who asks —
cannot keep pairs in `~/.switchboard/pairs`: its containers are replaced on
every deploy, and its users' secrets belong in its own encrypted table. So
every pairing call takes `store=`, and anything implementing
`pairing.Store` works there. These tests pair, talk, heal, expire and forget
through an in-memory store that has the protocol's members and nothing else,
the way such a server would write one, and hold the core to the contract it
documents: the lock never nested, never held across a hub call.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import time
from dataclasses import replace

import pytest

from switchboard import pairing
from switchboard.pairing import PairingError, PairRecord, PairStore, PendingHandshake
from switchboard.testing import hub as make_hub


class MemoryStore:
    """`pairing.Store` over three dicts, standing in for one owner's rows.

    Values are kept as JSON text, so nothing the core holds is shared with
    what the store holds — as with a table, a record changes only when saved.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.records: dict[str, str] = {}
        self.pendings: dict[str, str] = {}
        self.verdicts: dict[str, str] = {}
        self.held = False
        self.locks = 0
        self.broken: list[str] = []

    def require(self) -> None:
        if not self.enabled:
            raise PairingError("pairing is off for this owner", state="disabled")

    @contextlib.contextmanager
    def lock(self):
        if self.held:
            self.broken.append("lock() taken while already held")
            raise AssertionError("lock() nested")
        self.held, self.locks = True, self.locks + 1
        try:
            yield
        finally:
            self.held = False

    def get(self, name: str) -> PairRecord | None:
        data = self.records.get(name)
        return None if data is None else PairRecord.from_json(json.loads(data),
                                                              origin=f"row {name}")

    def save(self, record: PairRecord) -> None:
        self.records[record.name] = json.dumps(record.to_json())

    def delete(self, name: str) -> bool:
        return self.records.pop(name, None) is not None

    def names(self) -> list[str]:
        return list(self.records)

    def pending(self, name: str) -> PendingHandshake | None:
        data = self.pendings.get(name)
        return None if data is None else PendingHandshake.from_json(
            json.loads(data), origin=f"pending row {name}")

    def save_pending(self, pending: PendingHandshake) -> None:
        self.pendings[pending.name] = json.dumps(pending.to_json())

    def delete_pending(self, name: str) -> bool:
        return self.pendings.pop(name, None) is not None

    def pending_names(self) -> list[str]:
        return list(self.pendings)

    def verdict(self, name: str, *, now: float | None = None):
        data = self.verdicts.get(name)
        if data is None:
            return None
        verdict = json.loads(data)
        if (time.time() if now is None else now) > verdict["until"]:
            self.clear_verdict(name)
            return None
        return verdict

    def save_verdict(self, name: str, state: str, message: str, *, now: float) -> None:
        self.verdicts[name] = json.dumps({"name": name, "state": state, "message": message,
                                          "at": now, "until": now + pairing.VERDICT_KEEP})

    def clear_verdict(self, name: str) -> bool:
        return self.verdicts.pop(name, None) is not None

    def verdict_names(self) -> list[str]:
        return list(self.verdicts)

    def taken(self, name: str) -> str | None:
        if name in self.records:
            return f"this owner already has a pair called {name!r}"
        if name in self.pendings:
            return f"this owner is already waiting on a pairing called {name!r}"
        return None


class Side:
    """One owner: a memory store, the shared hub, a client that checks the lock."""

    def __init__(self, h, label: str) -> None:
        self.h = h
        self.label = label
        self.store = MemoryStore()
        self.base = h.client_config(workspace=f"team-{label}", agent_id=label)
        store = self.store

        class Guarded(h.client_class()):
            def _call(self, method, path, **kwargs):
                if store.held:
                    store.broken.append(f"{method} {path} with the store lock held")
                return super()._call(method, path, **kwargs)

        self.connect = Guarded

    def kw(self, **extra):
        return {"store": self.store, "connect": self.connect, "now": self.h.now,
                "agent_id": self.label, **extra}


@pytest.fixture
def h():
    with make_hub() as hub_:
        yield hub_


@pytest.fixture
def server(h):
    side = Side(h, "server")
    yield side
    assert not side.store.broken, side.store.broken


@pytest.fixture
def dev(h):
    side = Side(h, "dev")
    yield side
    assert not side.store.broken, side.store.broken


def pair(server: Side, dev: Side) -> str:
    started = pairing.start(server.base, store=server.store, connect=server.connect,
                            now=server.h.now)
    pairing.accept(started.code, dev.base, **dev.kw())
    assert pairing.collect(started.name, server.base, **server.kw()).state == "paired"
    return started.name


@pytest.mark.parametrize("make", [lambda tmp: PairStore(tmp / "pairs", env={}),
                                  lambda tmp: MemoryStore()], ids=["directory", "memory"])
def test_a_store_has_every_member_the_core_uses_with_its_signature(make, tmp_path):
    store: pairing.Store = make(tmp_path)
    members = [n for n in vars(pairing.Store) if not n.startswith("_")]
    assert {"get", "save", "lock", "require", "enabled", "taken"} <= set(members)
    for member in members:
        assert hasattr(store, member), member
        wanted = vars(pairing.Store)[member]
        if isinstance(wanted, property):
            continue
        shape = [(p.name, p.kind) for p in inspect.signature(wanted).parameters.values()]
        given = [(p.name, p.kind) for p in
                 inspect.signature(getattr(type(store), member)).parameters.values()]
        assert given == shape, member


def test_two_owners_in_memory_pair_talk_heal_and_forget(server, dev, h):
    started = pairing.start(server.base, store=server.store, connect=server.connect,
                            now=h.now)
    assert server.store.pending_names() == [started.name]
    assert [n for n, _ in pairing.pending_rooms(server.store, server.base, now=h.now)] \
        == [started.name]

    accepted = pairing.accept(started.code, dev.base, **dev.kw())
    assert accepted.state == "unconfirmed"
    assert pairing.collect(started.name, server.base, **server.kw()).state == "paired"
    assert server.store.pending_names() == [], "the ephemeral key must be gone"
    name = started.name

    mine = pairing.resolve(name, server.base, **server.kw())
    theirs = pairing.resolve(name, dev.base, **dev.kw())
    assert mine.state == theirs.state == "paired"
    assert mine.config.workspace == theirs.config.workspace
    with server.connect(mine.config, agent_id="server") as a, \
            dev.connect(theirs.config, agent_id="dev") as b:
        a.post("pair", "hello from the server")
        assert "hello from the server" in [m["body"] for m in b.history("pair")]

    healed = pairing.heal(name, dev.base, **dev.kw(reason="key in a log"))
    assert healed.to_generation == 1
    followed = pairing.resolve(name, server.base, **server.kw())
    assert followed.generation == 1 and followed.moved_from == 0
    assert dev.store.get(name).generation == 1, "the heal was saved through the store"

    [row] = pairing.status(server.base, **server.kw())
    assert row["state"] == "paired"
    assert pairing.active(server.store, now=h.now) == [name]

    gone = pairing.forget(name, dev.base, **dev.kw())
    assert gone.peer_told and dev.store.names() == []
    with pytest.raises(PairingError) as exc:
        pairing.resolve(name, server.base, **server.kw())
    assert exc.value.state == "ended_by_peer"
    assert server.store.locks and dev.store.locks, "the lock was taken, so it was checked"


def test_a_second_redeemer_leaves_nothing_in_its_store(server, dev, h):
    started = pairing.start(server.base, store=server.store, connect=server.connect,
                            now=h.now)
    pairing.accept(started.code, dev.base, **dev.kw())
    thief = Side(h, "thief")
    with pytest.raises(pairing.AlreadyRedeemed):
        pairing.accept(started.code, thief.base, **thief.kw())
    assert thief.store.names() == [] and not thief.store.broken
    assert pairing.collect(started.name, server.base, **server.kw()).state == "paired"


def test_an_expired_code_leaves_a_verdict_until_its_keep_runs_out(server, h):
    started = pairing.start(server.base, store=server.store, connect=server.connect,
                            now=h.now)
    swept = pairing.start(server.base, store=server.store, connect=server.connect,
                          name="swept", now=h.now)
    h.advance(pairing.DEFAULT_TTL + pairing.COLLECT_WINDOW + 1)

    assert pairing.collect(started.name, server.base, **server.kw()).state == "expired"
    assert pairing.sweep_expired(server.store, now=h.now) == [swept.name]
    assert server.store.pending_names() == [], "no private key outlives its deadline"
    states = {row["name"]: row["state"]
              for row in pairing.status(server.base, **server.kw())}
    assert states == {started.name: "expired", swept.name: "expired"}

    h.advance(pairing.VERDICT_KEEP + 1)
    pairing.sweep_expired(server.store, now=h.now)
    assert server.store.verdict_names() == []
    assert pairing.status(server.base, **server.kw()) == []


def test_a_forgotten_pair_is_not_brought_back_by_a_stale_copy(server, dev):
    name = pair(server, dev)
    stale = server.store.get(name)
    server.store.delete(name)
    with pytest.raises(PairingError) as exc:
        pairing._keep(server.store, stale)
    assert exc.value.state == "ended"
    assert server.store.get(name) is None


def test_a_stale_copy_never_overwrites_another_pair_under_its_name(server, dev):
    """Forgotten and paired again meanwhile: the name now holds another pair,
    whose secret exists nowhere else."""
    name = pair(server, dev)
    stale = server.store.get(name)
    newer = replace(stale, secret=pairing._b64e(b"n" * 32))
    server.store.save(newer)
    with pytest.raises(PairingError) as exc:
        pairing._keep(server.store, stale)
    assert exc.value.state == "ended"
    assert server.store.get(name).secret == newer.secret


def test_a_store_that_is_off_refuses_rather_than_finds_nothing(h):
    off = Side(h, "off")
    off.store.enabled = False
    with pytest.raises(PairingError) as exc:
        pairing.start(off.base, store=off.store, connect=off.connect, now=h.now)
    assert exc.value.state == "disabled"
    with pytest.raises(PairingError) as exc:
        pairing.status(off.base, **off.kw())
    assert exc.value.state == "disabled"


def test_a_store_failure_costs_only_its_own_row(server, dev, h):
    name = pair(server, dev)
    pairing.start(server.base, store=server.store, connect=server.connect,
                  name="waiting", now=h.now)
    real_get, real_pending = server.store.get, server.store.pending

    def get(each):
        if each == name:
            raise OSError("connection to the secrets table lost")
        return real_get(each)

    def pending(each):
        if each == "waiting":
            raise OSError("connection to the secrets table lost")
        return real_pending(each)

    server.store.get, server.store.pending = get, pending
    assert pairing.active(server.store, now=h.now) == [name], \
        "listed, so the listener's resolve says why it cannot park there"
    assert pairing.pending_rooms(server.store, server.base, now=h.now) == []
    assert pairing.sweep_expired(server.store, now=h.now) == []
    rows = {row["name"]: row for row in pairing.status(server.base, **server.kw())}
    assert rows[name]["state"] == "error" and "secrets table" in rows[name]["error"]
