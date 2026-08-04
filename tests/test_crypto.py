"""End-to-end encryption.

The test that matters most is
:func:`test_the_hub_database_contains_no_plaintext`. Everything else here
checks a mechanism; that one checks the *claim* — that a hub operator reading
the raw database learns nothing. A round-trip test would pass just as happily
if the client encrypted on the way out and the hub stored the plaintext
alongside it.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest
from fastapi.testclient import TestClient

from switchboard.client import Client
from switchboard.config import ClientConfig, ServerConfig
from switchboard.crypto import (
    DecryptionError,
    WorkspaceCipher,
    generate_key,
    is_sealed,
)
from switchboard.server import create_app
from switchboard.store import Store

WS = "crypto-ws"
SECRET = "the orders migration is 0142 and the api key rotates friday"


@pytest.fixture
def key():
    return generate_key()


@pytest.fixture
def hub(tmp_path):
    """A real hub plus the path to its database, so we can read the bytes."""
    db = str(tmp_path / "e2e.db")
    store = Store(db)
    app = create_app(ServerConfig(db_path=db), store=store)
    with TestClient(app) as http:
        yield http, db, store
    store.close()


def bound(http, key, agent_id):
    """A Client whose transport is the in-process hub."""
    config = ClientConfig(url="http://testserver", workspace=WS, key=key)
    client = Client(config, agent_id=agent_id, key=key)
    client._http.close()
    client._http = http
    return client


def hub_bytes(db_path: str) -> bytes:
    """Everything a hub operator could read off disk.

    The store runs in WAL mode, so recent writes sit in `<db>-wal` and not in
    the main file at all. Reading only the main file made the no-plaintext
    assertion below pass without inspecting a single message — which is
    exactly how a security test comes to mean nothing.
    """
    import pathlib

    blob = b""
    for suffix in ("", "-wal", "-shm"):
        path = pathlib.Path(db_path + suffix)
        if path.exists():
            blob += path.read_bytes()
    return blob


# --- the cipher -------------------------------------------------------------


def test_seal_and_open_round_trip(key):
    c = WorkspaceCipher.from_key(key, WS)
    payload = {"files": ["a.py"], "n": 3, "ok": True, "nested": {"x": None}}
    sealed = c.seal(payload, "message.body")
    assert is_sealed(sealed)
    assert c.unseal(sealed, "message.body") == payload


def test_ciphertext_does_not_contain_the_plaintext(key):
    c = WorkspaceCipher.from_key(key, WS)
    sealed = c.seal(SECRET, "message.body")
    assert SECRET not in json.dumps(sealed)
    assert "0142" not in json.dumps(sealed)


def test_a_different_key_cannot_open_it(key):
    sealed = WorkspaceCipher.from_key(key, WS).seal(SECRET, "message.body")
    with pytest.raises(DecryptionError):
        WorkspaceCipher.from_key(generate_key(), WS).unseal(sealed, "message.body")


def test_tampering_is_detected(key):
    c = WorkspaceCipher.from_key(key, WS)
    sealed = c.seal(SECRET, "message.body")
    flipped = bytearray(sealed["c"].encode())
    flipped[5] = flipped[5] ^ 0x01 if flipped[5] != 0x01 else 0x02
    sealed["c"] = flipped.decode()
    with pytest.raises(DecryptionError):
        c.unseal(sealed, "message.body")


def test_a_sealed_value_cannot_be_moved_to_another_field(key):
    """AEAD context binding: a hub must not be able to relocate a ciphertext."""
    c = WorkspaceCipher.from_key(key, WS)
    sealed = c.seal(SECRET, "message.body")
    with pytest.raises(DecryptionError):
        c.unseal(sealed, "board.value")


def test_a_sealed_value_cannot_be_replayed_into_another_workspace(key):
    sealed = WorkspaceCipher.from_key(key, "tenant-a").seal(SECRET, "message.body")
    with pytest.raises(DecryptionError):
        WorkspaceCipher.from_key(key, "tenant-b").unseal(sealed, "message.body")


def test_plaintext_is_refused_rather_than_passed_through(key):
    """Refusing a downgrade is the point; silently accepting would look identical."""
    c = WorkspaceCipher.from_key(key, WS)
    with pytest.raises(DecryptionError, match="downgrade"):
        c.unseal({"not": "an envelope"}, "message.body")


def test_blinding_is_deterministic_across_clients(key):
    a = WorkspaceCipher.from_key(key, WS)
    b = WorkspaceCipher.from_key(key, WS)
    assert a.blind("build", "channel") == b.blind("build", "channel")


def test_blinding_hides_the_identifier(key):
    c = WorkspaceCipher.from_key(key, WS)
    blinded = c.blind("acme-billing-migration", "channel")
    assert "acme" not in blinded and "billing" not in blinded


def test_domains_keep_namespaces_apart(key):
    """A channel and a lease sharing a name must not blind to the same token."""
    c = WorkspaceCipher.from_key(key, WS)
    assert c.blind("build", "channel") != c.blind("build", "resource")
    assert c.blind("build", "channel") != c.blind("build", "board")


def test_different_keys_blind_differently(key):
    a = WorkspaceCipher.from_key(key, WS)
    b = WorkspaceCipher.from_key(generate_key(), WS)
    assert a.blind("build", "channel") != b.blind("build", "channel")


def test_agent_ids_are_blinded_once_at_the_client(key):
    """The privacy of a DM comes from the id, not from blinding the channel.

    Ids are blinded when the client is built, so an `@` channel is already in
    hub form. Blinding it a second time would address `blind(blind(id))`, which
    no inbox resolves to — a DM that disappears without error.
    """
    c = WorkspaceCipher.from_key(key, WS)
    hub_id = c.blind("local-feat-billing-laptop", "agent")
    assert "billing" not in hub_id and "laptop" not in hub_id
    assert c.blind_channel(f"@{hub_id}") == f"@{hub_id}"
    # A named channel is still blinded.
    assert c.blind_channel("build") != "build"


def test_a_short_key_is_rejected():
    with pytest.raises(Exception, match="at least 32 bytes"):
        WorkspaceCipher.from_key("dGlueQ", WS)


def test_generated_keys_are_distinct():
    assert len({generate_key() for _ in range(50)}) == 50


def test_hex_keys_are_accepted():
    # A real 32-byte key in hex. Note "ab" * 32 would NOT do: that is one byte
    # repeated, and the placeholder guard rejects it — correctly.
    import secrets as _secrets

    c = WorkspaceCipher.from_key("hex:" + _secrets.token_bytes(32).hex(), WS)
    assert c.unseal(c.seal("x", "message.body"), "message.body") == "x"


# --- through the hub --------------------------------------------------------


def test_agents_read_each_other_transparently(hub, key):
    http, _, _ = hub
    a1, a2 = bound(http, key, "a1"), bound(http, key, "a2")
    a1.register(name="alpha", branch="feat/billing", channels=["build"])
    a2.register(name="beta", channels=["build"])

    a2.post("build", {"secret": SECRET, "n": 42})
    got = a1.inbox()
    assert got[0]["body"] == {"secret": SECRET, "n": 42}


def test_direct_messages_work_encrypted(hub, key):
    http, _, _ = hub
    a1, a2 = bound(http, key, "a1"), bound(http, key, "a2")
    a1.register(name="alpha")
    a2.register(name="beta")
    # Address by the id the roster reports — already in hub (blinded) form.
    roster = {a["name"]: a["agent_id"] for a in a2.agents()}
    a2.send(roster["alpha"], "for your eyes only")
    assert [m["body"] for m in a1.inbox()] == ["for your eyes only"]


def test_leases_still_work_encrypted(hub, key):
    http, _, _ = hub
    a1, a2 = bound(http, key, "a1"), bound(http, key, "a2")
    a1.register(name="alpha")
    a2.register(name="beta")
    lease = a1.acquire("backend/alembic", note=SECRET)
    assert lease["note"] == SECRET
    from switchboard.client import LeaseHeld
    with pytest.raises(LeaseHeld):
        a2.acquire("backend/alembic")   # exclusion survives blinding
    a1.release("backend/alembic")
    assert a2.acquire("backend/alembic")["resource"]


def test_blackboard_round_trips_encrypted(hub, key):
    http, _, _ = hub
    a1, a2 = bound(http, key, "a1"), bound(http, key, "a2")
    a1.board_set("migration/plan", {"taken": ["0142"], "note": SECRET})
    assert a2.board_get("migration/plan") == {"taken": ["0142"], "note": SECRET}


def test_channel_history_decrypts(hub, key):
    http, _, _ = hub
    a1, a2 = bound(http, key, "a1"), bound(http, key, "a2")
    a1.register(name="alpha", channels=["build"])
    a2.post("build", SECRET)
    assert [m["body"] for m in a1.history("build")] == [SECRET]


def test_roster_decrypts_names_and_branches(hub, key):
    http, _, _ = hub
    a1 = bound(http, key, "a1")
    a1.register(name="alpha", branch="feat/acme-billing", task="migrating orders")
    entry = a1.agents()[0]
    assert entry["name"] == "alpha"
    assert entry["branch"] == "feat/acme-billing"
    assert entry["task"] == "migrating orders"


def test_an_agent_with_the_wrong_key_cannot_read(hub, key):
    http, _, _ = hub
    insider = bound(http, key, "a1")
    outsider = bound(http, generate_key(), "a2")
    insider.register(name="alpha", channels=["build"])
    insider.post("build", SECRET)
    # The outsider blinds "build" differently, so it does not even see the
    # channel — and could not open the body if it did.
    assert outsider.inbox(channels=["build"]) == []


# --- the claim --------------------------------------------------------------


def test_the_hub_database_contains_no_plaintext(hub, key):
    """A hub operator with the raw database file learns nothing.

    This is the property the whole feature exists for, so it is asserted
    against the bytes on disk rather than against any code path.
    """
    http, db_path, store = hub
    # Long, distinctive identifiers throughout. Short needles ("a1", "0142")
    # match base64url ciphertext by CHANCE — a 2-character needle has a ~1/4096
    # hit rate per position, which across a few KB of database is a coin flip.
    # An earlier version of this test used them and failed ~40% of the time,
    # which reads as a leak and is not one.
    a1 = bound(http, key, "agent-alpha-billing-laptop")
    a2 = bound(http, key, "agent-beta-orders-cloudbox")

    a1.register(name="alpha-billing-laptop", branch="feat/acme-billing-rework",
                task="migrating-orders-table", channels=["deployment-secrets"])
    a2.register(name="beta-orders-cloudbox", channels=["deployment-secrets"])
    a1.post("deployment-secrets", {"detail": SECRET})
    a1.acquire("backend/alembic/migration-0142-orders", note=SECRET)
    a1.board_set("migration-plan-acme", {"note": SECRET})
    a2.send(a1.agent_id, SECRET)

    raw = hub_bytes(db_path)

    forbidden = [
        SECRET,
        "migration-0142-orders", "alpha-billing-laptop", "beta-orders-cloudbox",
        "feat/acme-billing-rework", "migrating-orders-table",
        "deployment-secrets", "backend/alembic", "migration-plan-acme",
        "agent-alpha-billing-laptop", "agent-beta-orders-cloudbox",
    ]
    # Guard the guard: a short needle here would make this test flaky rather
    # than strict, and a flaky security test gets deleted, not fixed.
    too_short = [n for n in forbidden if len(n) < 8]
    assert not too_short, f"needles must be >=8 chars to avoid chance matches: {too_short}"

    leaked = [needle for needle in forbidden if needle.encode() in raw]
    assert not leaked, f"plaintext found in the hub's database: {leaked}"


def test_the_hub_sees_only_opaque_identifiers(hub, key):
    """What the hub *does* see, asserted so the metadata claim stays honest."""
    http, _, store = hub
    a1 = bound(http, key, "a1")
    a1.register(name="alpha", channels=["build"])
    a1.post("build", SECRET)

    channels = store.list_channels(workspace=WS)
    assert channels, "the hub still routes, so it still sees *a* channel"
    assert all(c["channel"] != "build" for c in channels)
    # It knows how many messages and when — that is the documented leakage.
    assert channels[0]["messages"] == 1


def test_encryption_is_off_unless_a_key_is_given(hub):
    """Self-hosted hubs that do not want this must be unaffected."""
    http, db_path, _ = hub
    plain = bound(http, None, "a1")
    assert plain.cipher is None
    plain.register(name="alpha", channels=["build"])
    plain.post("build", "readable")
    raw = hub_bytes(db_path)
    # The control for the test above: with no key the same read DOES find the
    # plaintext. If this ever fails, the no-plaintext assertion is vacuous.
    assert b"readable" in raw and b"build" in raw


def test_channel_names_stay_readable_for_key_holders(hub, key):
    """Blinding is one-way, so the label travels sealed alongside the body.

    Without this an agent reads every message labelled with 22 opaque
    characters and cannot tell which channel it came from — which would make
    the encrypted mode noticeably worse to use than the plaintext one.
    """
    http, _, store = hub
    a1, a2 = bound(http, key, "a1"), bound(http, key, "a2")
    a1.register(name="alpha", channels=["deployment-secrets"])
    a2.register(name="beta", channels=["deployment-secrets"])
    a2.post("deployment-secrets", "ready")

    message = a1.inbox()[0]
    assert message["channel"] == "deployment-secrets"   # readable to the holder
    assert message["body"] == "ready"

    # ...while the hub's own row still carries only the blinded token.
    stored = store.list_channels(workspace=WS)
    assert all(c["channel"] != "deployment-secrets" for c in stored)


def test_a_dict_body_is_not_mistaken_for_a_label_wrapper(hub, key):
    """The label check is structural, so ordinary dict bodies survive intact."""
    http, _, _ = hub
    a1, a2 = bound(http, key, "a1"), bound(http, key, "a2")
    a1.register(name="alpha", channels=["build"])
    tricky = {"b": "looks like a wrapper", "ch": "but is not one"}
    a2.post("build", tricky)
    assert a1.inbox()[0]["body"] == tricky


# --- length padding ---------------------------------------------------------


def test_padding_makes_similar_messages_indistinguishable(key):
    """AEAD preserves length exactly, so without padding the size IS the leak.

    Measured on real traffic before this landed: 18- and 19-character messages
    produced 143- and 144-byte rows. An operator could read message lengths to
    the byte.
    """
    c = WorkspaceCipher.from_key(key, WS)
    sizes = {len(json.dumps(c.seal("a" * n, "message.body"))) for n in range(1, 56)}
    assert len(sizes) == 1, f"length still leaks: {sorted(sizes)}"


def test_padding_still_buckets_larger_payloads(key):
    c = WorkspaceCipher.from_key(key, WS)
    small = len(json.dumps(c.seal("a" * 100, "message.body")))
    also_small = len(json.dumps(c.seal("a" * 120, "message.body")))
    large = len(json.dumps(c.seal("a" * 900, "message.body")))
    assert small == also_small
    assert large > small, "buckets must still grow, or big payloads cost nothing to hide"


def test_padding_is_exact_for_every_shape(key):
    """The filler must never bleed into the value."""
    c = WorkspaceCipher.from_key(key, WS)
    for value in ["", "x", "a" * 5000, {"a": [1, 2, 3]}, [], None, 0, False,
                  {"nested": {"deep": [None, True]}}]:
        assert c.unseal(c.seal(value, "message.body"), "message.body") == value


def test_padded_and_unpadded_clients_interoperate(key):
    """Padding is detected from the payload, not from the reader's setting."""
    padded = WorkspaceCipher.from_key(key, WS, pad=True)
    plain = WorkspaceCipher.from_key(key, WS, pad=False)
    assert plain.unseal(padded.seal("from padded", "message.body"),
                        "message.body") == "from padded"
    assert padded.unseal(plain.seal("from unpadded", "message.body"),
                         "message.body") == "from unpadded"


def test_padding_is_on_by_default(key):
    assert WorkspaceCipher.from_key(key, WS).pad is True


def test_a_truncated_padded_payload_is_rejected(key):
    """A declared length beyond the payload must raise, not slice silently."""
    from switchboard.crypto import DecryptionError, _pad, _unpad

    framed = _pad(b'"hello"')
    corrupt = framed[:3] + b"\xff\xff" + framed[5:]
    with pytest.raises(DecryptionError):
        _unpad(corrupt)


def test_blinding_has_no_time_component(key):
    """Tokens must be stable forever — rotation was measured and rejected.

    A prototype that derived tokens per epoch produced a SILENT double-hold:
    the same logical resource became two rows across a boundary and `acquire`
    succeeded for both agents, because exclusion is enforced by the resource
    being a primary key. Rotating agent ids separately broke self-renewal, so
    an agent's own leases were orphaned and expired mid-work.

    If someone reintroduces rotation for these domains, this fails first.
    See docs/encryption.md, "rotating the blinding".
    """
    c = WorkspaceCipher.from_key(key, WS)
    for domain in ("resource", "agent", "channel", "board"):
        first = c.blind("backend/alembic", domain)
        # A fresh cipher from the same key, as a later process would build.
        again = WorkspaceCipher.from_key(key, WS).blind("backend/alembic", domain)
        assert first == again, f"{domain} tokens are not stable across clients"


def test_keygen_emits_a_key_and_an_opaque_workspace():
    """The workspace name is the one thing a hub sees in the clear."""
    from switchboard.cli import build_parser, cmd_keygen

    args = build_parser().parse_args(["--json", "keygen"])

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cmd_keygen(args) == 0
    payload = json.loads(out.getvalue())
    assert len(payload["key"]) >= 40
    assert payload["workspace"].startswith("w_")
    # It must not be derived from anything guessable, and must differ per call.
    second = io.StringIO()
    with contextlib.redirect_stdout(second):
        cmd_keygen(args)
    assert json.loads(second.getvalue())["workspace"] != payload["workspace"]


# --- properties a "salt or pepper" would be reaching for ---------------------


def test_encryption_is_already_non_deterministic(key):
    """Identical plaintexts must not produce identical ciphertexts.

    A fresh random nonce per seal is what provides this. If anyone ever
    "optimises" to a fixed or derived nonce, AES-GCM nonce reuse leaks the XOR
    of the plaintexts and permits forgery — catastrophic, and invisible from
    the outside. Nothing else in the suite would notice, so this test is the
    guard.
    """
    c = WorkspaceCipher.from_key(key, WS)
    first = c.seal("identical plaintext", "message.body")
    second = c.seal("identical plaintext", "message.body")
    assert first["c"] != second["c"]
    assert first["n"] != second["n"]


def test_nonces_do_not_repeat(key):
    c = WorkspaceCipher.from_key(key, WS)
    nonces = {c.seal("x", "message.body")["n"] for _ in range(5000)}
    assert len(nonces) == 5000


def test_the_same_key_in_two_workspaces_blinds_differently(key):
    """Workspace separation without needing a per-workspace salt to distribute.

    The workspace is bound into the HKDF info, so even a key reused across two
    workspaces does not let a hub correlate a channel between them.
    """
    a = WorkspaceCipher.from_key(key, "tenant-a")
    b = WorkspaceCipher.from_key(key, "tenant-b")
    assert a.blind("build", "channel") != b.blind("build", "channel")
    assert a.blind("backend/alembic", "resource") != b.blind("backend/alembic", "resource")


def test_a_placeholder_key_is_refused():
    """No salt rescues a key with no entropy, so reject it outright."""
    from switchboard.crypto import CryptoError

    for placeholder in ("hex:" + "00" * 32, "hex:" + "11" * 32, "hex:" + "0f" * 32):
        with pytest.raises(CryptoError, match="placeholder"):
            WorkspaceCipher.from_key(placeholder, WS)
    # A genuine key with all-distinct-ish bytes is still fine.
    WorkspaceCipher.from_key(generate_key(), WS)
