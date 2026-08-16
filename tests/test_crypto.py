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

from switchboard.crypto import (
    DecryptionError,
    WorkspaceCipher,
    generate_key,
    is_sealed,
)
from switchboard.testing import hub as make_hub

WS = "crypto-ws"
SECRET = "the orders migration is 0142 and the api key rotates friday"


@pytest.fixture
def key():
    return generate_key()


@pytest.fixture
def hub(tmp_path, key):
    """A real hub on a real file, so the no-plaintext test can read the bytes.

    Every client it hands out holds `key`, which is what makes them able to
    read each other while the hub cannot read any of them.
    """
    with make_hub(workspace=WS, key=key, db=str(tmp_path / "e2e.db")) as handle:
        yield handle


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


def test_a_dm_to_a_local_alias_reaches_that_agents_channel(key):
    """#90. The two forms are indistinguishable at the call site — a roster
    entry and the name an agent calls itself both go in as `dm(<something>)` —
    and passing both through unchanged meant one was delivered and the other
    discarded with no signal at all.

    A local alias has to be blinded, because the recipient's own inbox is
    keyed on the blinded form.
    """
    c = WorkspaceCipher.from_key(key, WS)
    assert c.blind_channel("@thinker") == "@" + c.blind("thinker", "agent")


def test_a_hub_form_id_is_never_blinded_twice(key):
    """The other half, and the reason this cannot simply blind everything:
    `blind(blind(id))` addresses a channel no inbox resolves to."""
    c = WorkspaceCipher.from_key(key, WS)
    hub_id = c.blind("thinker", "agent")
    assert c.blind_channel(f"@{hub_id}") == f"@{hub_id}"
    # Both spellings of the same agent land in one place, which is the
    # property that makes the two interchangeable at the call site.
    assert c.blind_channel("@thinker") == c.blind_channel(f"@{hub_id}")


def test_the_two_forms_are_told_apart_by_shape_not_by_asking_the_hub(key):
    """Resolving through the roster would have been the other option, and it
    fails the common case: turn-based agents are absent from `roster` between
    turns, so a DM to one would error even though blinding delivers it
    correctly — `blind` is deterministic, so the message waits in exactly the
    channel that agent resolves to when it comes back."""
    c = WorkspaceCipher.from_key(key, WS)
    never_registered = c.blind_channel("@has-never-announced")
    assert never_registered == "@" + c.blind("has-never-announced", "agent")


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
    a1, a2 = hub.client("a1"), hub.client("a2")
    a1.register(name="alpha", branch="feat/billing", channels=["build"])
    a2.register(name="beta", channels=["build"])

    a2.post("build", {"secret": SECRET, "n": 42})
    got = a1.inbox()
    assert got[0]["body"] == {"secret": SECRET, "n": 42}


def test_direct_messages_work_encrypted(hub, key):
    a1, a2 = hub.client("a1"), hub.client("a2")
    a1.register(name="alpha")
    a2.register(name="beta")
    # Address by the id the roster reports — already in hub (blinded) form.
    roster = {a["name"]: a["agent_id"] for a in a2.agents()}
    a2.send(roster["alpha"], "for your eyes only")
    assert [m["body"] for m in a1.inbox()] == ["for your eyes only"]


def test_leases_still_work_encrypted(hub, key):
    a1, a2 = hub.client("a1"), hub.client("a2")
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
    a1, a2 = hub.client("a1"), hub.client("a2")
    a1.board_set("migration/plan", {"taken": ["0142"], "note": SECRET})
    assert a2.board_get("migration/plan") == {"taken": ["0142"], "note": SECRET}


def test_channel_history_decrypts(hub, key):
    a1, a2 = hub.client("a1"), hub.client("a2")
    a1.register(name="alpha", channels=["build"])
    a2.post("build", SECRET)
    assert [m["body"] for m in a1.history("build")] == [SECRET]


def test_roster_decrypts_names_and_branches(hub, key):
    a1 = hub.client("a1")
    a1.register(name="alpha", branch="feat/acme-billing", task="migrating orders")
    entry = a1.agents()[0]
    assert entry["name"] == "alpha"
    assert entry["branch"] == "feat/acme-billing"
    assert entry["task"] == "migrating orders"


def test_an_agent_with_the_wrong_key_cannot_read(hub, key):
    insider = hub.client("a1")
    outsider = hub.client("a2", key=generate_key())
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
    db_path = hub.config.db_path
    # Long, distinctive identifiers throughout. Short needles ("a1", "0142")
    # match base64url ciphertext by CHANCE — a 2-character needle has a ~1/4096
    # hit rate per position, which across a few KB of database is a coin flip.
    # An earlier version of this test used them and failed ~40% of the time,
    # which reads as a leak and is not one.
    a1 = hub.client("agent-alpha-billing-laptop")
    a2 = hub.client("agent-beta-orders-cloudbox")

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
    a1 = hub.client("a1")
    a1.register(name="alpha", channels=["build"])
    a1.post("build", SECRET)

    channels = hub.store.list_channels(workspace=WS, now=hub.now)
    assert channels, "the hub still routes, so it still sees *a* channel"
    assert all(c["channel"] != "build" for c in channels)
    # It knows how many messages and when — that is the documented leakage.
    assert channels[0]["messages"] == 1


def test_encryption_is_off_unless_a_key_is_given(hub):
    """Self-hosted hubs that do not want this must be unaffected."""
    db_path = hub.config.db_path
    plain = hub.client("a1", key="")
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
    a1, a2 = hub.client("a1"), hub.client("a2")
    a1.register(name="alpha", channels=["deployment-secrets"])
    a2.register(name="beta", channels=["deployment-secrets"])
    a2.post("deployment-secrets", "ready")

    message = a1.inbox()[0]
    assert message["channel"] == "deployment-secrets"   # readable to the holder
    assert message["body"] == "ready"

    # ...while the hub's own row still carries only the blinded token.
    stored = hub.store.list_channels(workspace=WS, now=hub.now)
    assert all(c["channel"] != "deployment-secrets" for c in stored)


def test_a_dict_body_is_not_mistaken_for_a_label_wrapper(hub, key):
    """The label check is structural, so ordinary dict bodies survive intact."""
    a1, a2 = hub.client("a1"), hub.client("a2")
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


# --- key mismatch must be loud ----------------------------------------------


def test_a_mismatched_key_is_visible_in_the_roster(hub, key):
    """The scenario that was previously silent in every direction.

    An agent on the wrong key blinds every channel differently, so its inbox is
    simply empty and its leases land on different rows. Nothing raises. The
    roster is the one view that survives a key change — it is keyed by the
    plaintext workspace — so failing to open a peer's name is what makes the
    partition visible.
    """
    current, stale = hub.client("a1"), hub.client("a2", key=generate_key())
    current.register(name="alpha", channels=["build"])
    stale.register(name="beta", channels=["build"])

    flagged = current.key_mismatches(current.agents())
    assert [a["agent_id"] for a in flagged] == [stale.agent_id]
    # ...and symmetrically, so whichever agent looks first finds out.
    assert len(stale.key_mismatches(stale.agents())) == 1


def test_an_unencrypted_agent_in_an_encrypted_workspace_is_flagged(hub, key):
    """The case a published key fingerprint could not catch.

    An agent running with no key publishes no fingerprint, so a scheme based on
    comparing published fingerprints missed it entirely. Detecting by "can we
    open this peer's fields" catches it, because its name arrives as plaintext
    where an envelope was expected.
    """
    encrypted, plain = hub.client("a1"), hub.client("a2", key="")
    encrypted.register(name="alpha")
    plain.register(name="beta")

    flagged = encrypted.key_mismatches(encrypted.agents())
    assert [a["agent_id"] for a in flagged] == [plain.agent_id]


def test_the_unencrypted_side_is_told_too(hub, key):
    """Seen from the side more likely to BE the misconfigured one."""
    encrypted, plain = hub.client("a1"), hub.client("a2", key="")
    encrypted.register(name="alpha")
    plain.register(name="beta")

    flagged = plain.key_mismatches(plain.agents())
    assert [a["agent_id"] for a in flagged] == [encrypted.agent_id]


def test_matching_keys_raise_no_false_alarm(hub, key):
    a1, a2 = hub.client("a1"), hub.client("a2")
    a1.register(name="alpha")
    a2.register(name="beta")
    assert a1.key_mismatches(a1.agents()) == []


def test_an_all_plaintext_workspace_raises_no_alarm(hub):
    """Nobody encrypts; nothing to warn about."""
    a1, a2 = hub.client("a1", key=""), hub.client("a2", key="")
    a1.register(name="alpha")
    a2.register(name="beta")
    assert a1.key_mismatches(a1.agents()) == []


def test_the_roster_still_lists_peers_it_cannot_read(hub, key):
    """Raising here would block the diagnostic AND break correctly-keyed peers."""
    a1, a2, a3 = (hub.client("a1"), hub.client("a2"),
                  hub.client("a3", key=generate_key()))
    a1.register(name="alpha")
    a2.register(name="beta")
    a3.register(name="gamma")

    roster = a1.agents()
    assert len(roster) == 3, "the whole roster must survive one unreadable entry"
    readable = {a["name"] for a in roster if not a.get("unreadable")}
    assert readable == {"alpha", "beta"}


def test_nothing_extra_is_published_to_detect_a_mismatch(hub, key):
    """Detection must cost the hub no additional information.

    An earlier version published a key fingerprint in the agent's meta. It was
    strictly worse — it missed unencrypted agents, it was a self-asserted claim
    rather than a demonstration of key possession, and it told the hub which
    agents share a key and when a key changed.
    """
    a1 = hub.client("a1")
    a1.register(name="alpha", meta={"host": "laptop"})
    stored = hub.store.list_agents(workspace=WS, now=hub.now)[0]
    assert "key_fp" not in stored.meta


# --- custom_scope: agent-initiated side channels -----------------------------
#
# A `custom_scope` is a complete, atomic override of workspace + key + blinded
# identity for exactly one call — see mcp_server.py's `_CUSTOM_SCOPE` schema.
# It must never require prior registration under that scope (it is meant to
# work the first time two agents who privately agreed on a pair use it), and
# it must never leak into or be affected by the caller's default scope.


def side_scope(ws_suffix: str = "") -> dict[str, str]:
    return {"workspace": f"w_side_{ws_suffix}_{generate_key()[:8]}", "key": generate_key()}


def test_custom_scope_round_trips_and_is_invisible_on_the_default_scope(hub, key):
    a1, a2 = hub.client("a1"), hub.client("a2")
    scope = side_scope()

    a1.post("plan", "private plan", custom_scope=scope)

    assert [m["body"] for m in a2.inbox(channels=["plan"], custom_scope=scope)] == ["private plan"]
    # Neither agent's default-scope inbox saw anything — different workspace
    # entirely, not merely a different key within the same one.
    assert a1.inbox(channels=["plan"]) == []
    assert a2.inbox(channels=["plan"]) == []


def test_custom_scope_without_a_key_is_plaintext_but_still_workspace_isolated(hub, key):
    a1, a2 = hub.client("a1"), hub.client("a2")
    scope = {"workspace": f"w_plain_{generate_key()[:8]}"}  # no key: unencrypted

    a1.post("plan", "not encrypted here", custom_scope=scope)
    got = a2.inbox(channels=["plan"], custom_scope=scope)
    assert [m["body"] for m in got] == ["not encrypted here"]


def test_custom_scope_requires_a_workspace(hub, key):
    a1 = hub.client("a1")
    with pytest.raises(TypeError):
        a1.post("plan", "x", custom_scope={"key": generate_key()})


def test_custom_scope_leases_exclude_independently_of_the_default_scope(hub, key):
    a1, a2 = hub.client("a1"), hub.client("a2")
    scope = side_scope()

    a1.acquire("shared/plan", custom_scope=scope)
    # The same resource string on the default scope is untouched — held under
    # the side scope's blinding, free under the default scope's.
    assert a1.acquire("shared/plan")["resource"]

    from switchboard.client import LeaseHeld
    with pytest.raises(LeaseHeld):
        a2.acquire("shared/plan", custom_scope=scope)

    assert a1.release("shared/plan", custom_scope=scope) is True
    assert a2.acquire("shared/plan", custom_scope=scope)["resource"]


def test_custom_scope_dm_after_discovering_a_peer_through_say(hub, key):
    """DM addressing under a custom scope: no roster exists for one, so the
    recipient's blinded id is learned from a message's `from` field instead —
    the same bootstrap `dm()`'s own docs already rely on for the default
    scope, where the roster happens to be the usual source of that id."""
    a1, a2 = hub.client("a1"), hub.client("a2")
    scope = side_scope()

    a2.post("hello", "a2 here", custom_scope=scope)
    [msg] = a1.inbox(channels=["hello"], custom_scope=scope)
    a1.send(msg["from"], "just you", custom_scope=scope)

    assert [m["body"] for m in a2.inbox(custom_scope=scope)] == ["just you"]


def test_custom_scope_does_not_change_the_agents_default_identity(hub, key):
    a1 = hub.client("a1")
    default_id = a1.agent_id
    a1.post("plan", "x", custom_scope=side_scope())
    assert a1.agent_id == default_id


# --- agent signing keys ------------------------------------------------------
#
# The workspace key proves "someone in this room wrote this". It cannot prove
# *which member*, because every member holds it — so `agent_id` is a claim
# rather than a fact. A per-agent signing key is the other half, and it is
# published sealed so peers can attribute while the hub still cannot.


def test_a_peer_reads_the_public_key_and_the_hub_never_does(hub, key):
    db = hub.config.db_path
    alice = hub.client("alice")
    bob = hub.client("bob")
    alice.register(name="alice")
    bob.register(name="bob")

    roster = {a["name"]: a for a in bob.agents()}
    assert roster["alice"]["pubkey"] == alice.public_key
    assert roster["alice"]["pubkey"] != roster["bob"]["pubkey"], "one key per agent"

    # and it is nowhere on disk in the clear — same standard as message bodies
    assert alice.public_key.encode() not in hub_bytes(db)


def test_two_agents_in_one_process_are_two_identities(hub, key):
    # A Client is an agent. Sharing a signing key between two would make the
    # scheme claim something it cannot back.
    assert hub.client("a").public_key != hub.client("b").public_key


def test_the_private_half_never_reaches_a_repr(key):
    from switchboard.signing import SigningIdentity

    identity = SigningIdentity.generate()
    # a traceback or a crash report must not carry it
    assert "private" not in repr(identity).lower()
    assert identity.public_key[:12] in repr(identity)


# --- signing: telling members of one room apart ------------------------------


def test_a_peer_verifies_a_message_it_can_attribute(hub, key):
    alice, bob = hub.client("alice"), hub.client("bob")
    alice.register(name="alice")
    bob.register(name="bob")
    bob.agents()  # learn alice's key

    alice.post("build", "rebasing onto main")
    got = bob.inbox(channels=["build"], include_own=True)
    assert got and got[0]["body"] == "rebasing onto main"
    assert got[0]["signature"]["status"] == "verified"


def test_an_impersonator_is_caught(hub, key):
    """The attack the whole scheme exists for: everyone in a room holds the
    same workspace key, so `mallory` can post *as* alice and the hub cannot
    tell. A signature can."""
    alice, bob = hub.client("alice"), hub.client("bob")
    mallory = hub.client("mallory")
    alice.register(name="alice")
    bob.register(name="bob")
    mallory.register(name="mallory")
    bob.agents()

    # mallory signs with her own key while claiming to be alice
    mallory.agent_id = alice.agent_id
    mallory.post("build", "ship it, no review needed")

    got = bob.inbox(channels=["build"], include_own=True)
    assert got[0]["signature"]["status"] == "mismatch", got[0]["signature"]


def test_an_unknown_sender_is_not_called_a_forgery(hub, key):
    # No key for a sender usually means a roster we have not read. Reporting
    # that as a bad signature would train people to ignore the warning.
    alice, bob = hub.client("alice"), hub.client("bob")
    alice.register(name="alice")
    bob.register(name="bob")
    alice.post("build", "hello")
    got = bob.inbox(channels=["build"], include_own=True)
    assert got[0]["signature"]["status"] == "unknown"


def test_a_restart_does_not_turn_history_into_forgeries(hub, key):
    # Identity is per process by design, so a restarted agent publishes a new
    # key. Its earlier messages are still legitimately signed by the old one.
    alice, bob = hub.client("alice"), hub.client("bob")
    alice.register(name="alice")
    bob.register(name="bob")
    bob.agents()
    alice.post("build", "before the restart")

    restarted = hub.client("alice")
    restarted.register(name="alice")
    bob.agents()
    restarted.post("build", "after the restart")

    statuses = [m["signature"]["status"]
                for m in bob.inbox(channels=["build"], include_own=True)]
    assert statuses == ["verified", "verified"], statuses


def test_the_signature_is_inside_the_ciphertext(hub, key):
    # A signature the transport can strip proves nothing. It travels sealed,
    # so removing it breaks the AEAD tag rather than silently downgrading.
    db = hub.config.db_path
    alice = hub.client("alice")
    alice.register(name="alice")
    alice.post("build", "sealed and signed")

    raw = hub_bytes(db)
    assert b"sealed and signed" not in raw
    assert alice.public_key.encode() not in raw


def test_a_gap_in_one_sender_is_visible(hub, key):
    """Signatures give authenticity; only the counter gives any grip on a hub
    that selectively withholds one agent's messages."""
    alice, bob = hub.client("alice"), hub.client("bob")
    alice.register(name="alice")
    bob.register(name="bob")
    bob.agents()

    alice.post("build", "one")
    alice.post("build", "two")
    alice.post("build", "three")
    read = bob.inbox(channels=["build"], include_own=True)
    assert [m["signature"]["seq"] for m in read] == [1, 2, 3]
    assert all("missing" not in m["signature"] for m in read)

    # a reader that saw only the first now meets the third
    carol = hub.client("carol")
    carol.note_peer_keys([{"agent_id": alice.agent_id, "pubkey": alice.public_key}])
    first, _, third = ({**m} for m in read)
    carol._verify_message(first, {"by": alice.agent_id, "n": 1,
                                  "sig": _resign(alice, first, 1)})
    carol._verify_message(third, {"by": alice.agent_id, "n": 3,
                                  "sig": _resign(alice, third, 3)})
    assert third["signature"]["missing"] == 1


def _resign(client, item, seq):
    from switchboard import signing

    return client.signing.sign(signing.message_payload(
        sender=client.agent_id, channel=item["channel"], seq=seq, body=item["body"],
    ))
# --- key epochs --------------------------------------------------------------
#
# One thing rotates: the payload key. The room identifier cannot, because both
# hub and client must know it — and blinding must not, because the hub
# *compares* blinded values and a rotating blind key would stop a channel
# matching itself across a boundary.


def test_reading_follows_the_message_not_the_clock(key):
    # The property everything else rests on. A message written seconds before a
    # boundary stays readable past it, and a reader joining later opens history,
    # both because the epoch comes from the envelope.
    writer = WorkspaceCipher.from_key(key, WS, epoch_period=900)
    reader = WorkspaceCipher.from_key(key, WS, epoch_period=900)
    sealed = writer.seal({"m": "hello"}, "message.body")
    assert reader.unseal(sealed, "message.body") == {"m": "hello"}


def test_a_reader_with_rotation_off_still_opens_rotated_content(key):
    # What makes a read-first rollout safe: understanding epochs does not
    # require writing them.
    rotating = WorkspaceCipher.from_key(key, WS, epoch_period=900)
    plain = WorkspaceCipher.from_key(key, WS, epoch_period=0)
    assert plain.unseal(rotating.seal("x", "message.body"), "message.body") == "x"


def test_epoch_zero_writes_the_bytes_it_always_did(key):
    # A client with rotation off must produce envelopes an older reader opens,
    # so the field is omitted rather than written as 0.
    plain = WorkspaceCipher.from_key(key, WS, epoch_period=0)
    assert "e" not in plain.seal("x", "message.body")


def test_different_epochs_use_different_keys(key):
    cipher = WorkspaceCipher.from_key(key, WS, epoch_period=900)
    assert cipher._payload_key_for(0) != cipher._payload_key_for(1)
    assert cipher._payload_key_for(1) != cipher._payload_key_for(2)
    # and epoch 0 is the original derivation, so old content is untouched
    assert cipher._payload_key_for(0) == cipher._payload_key


def test_a_derived_key_is_cached_not_recomputed(key):
    cipher = WorkspaceCipher.from_key(key, WS, epoch_period=900)
    first = cipher._payload_key_for(7)
    assert cipher._subkeys[7] is first
    assert cipher._payload_key_for(7) is first


def test_blinding_does_not_rotate(key):
    # The hub compares blinded values; rotating this would break routing.
    a = WorkspaceCipher.from_key(key, WS, epoch_period=900)
    b = WorkspaceCipher.from_key(key, WS, epoch_period=1)
    assert a.blind("build", "channel") == b.blind("build", "channel")


def test_a_nonsense_epoch_is_refused(key):
    cipher = WorkspaceCipher.from_key(key, WS, epoch_period=900)
    sealed = cipher.seal("x", "message.body")
    for bad in ("later", -1, 1.5, True):
        with pytest.raises(DecryptionError):
            cipher.unseal({**sealed, "e": bad}, "message.body")


def test_rotation_is_on_by_default(monkeypatch, key):
    monkeypatch.delenv("SWITCHBOARD_KEY_EPOCH_PERIOD", raising=False)
    assert WorkspaceCipher.from_key(key, WS).current_epoch() > 0


def test_rotation_can_be_switched_off(monkeypatch, key):
    # The escape hatch for a fleet that still has pre-epoch readers: writing
    # epoch 0 produces bytes they can open.
    monkeypatch.setenv("SWITCHBOARD_KEY_EPOCH_PERIOD", "0")
    cipher = WorkspaceCipher.from_key(key, WS)
    assert cipher.current_epoch() == 0
    assert "e" not in cipher.seal("x", "message.body")


def test_the_period_can_be_switched_on_by_environment(monkeypatch, key):
    monkeypatch.setenv("SWITCHBOARD_KEY_EPOCH_PERIOD", "900")
    cipher = WorkspaceCipher.from_key(key, WS)
    assert cipher.current_epoch(now=1_800_000) == 2000
    assert cipher.current_epoch(now=1_800_899) == 2000
    assert cipher.current_epoch(now=1_800_900) == 2001


def test_a_broken_period_setting_falls_back_to_the_default(monkeypatch, key):
    # Not to 0: a typo in an environment variable should not silently switch
    # rotation off for that agent while its peers keep rotating.
    monkeypatch.setenv("SWITCHBOARD_KEY_EPOCH_PERIOD", "every-so-often")
    assert WorkspaceCipher.from_key(key, WS).current_epoch() > 0


def test_a_key_swap_under_a_live_identity_is_noticed(hub, key):
    """The announcement upserts and nothing validates it, so anyone may
    announce as anyone and replace the published key. The hub cannot be asked
    to arbitrate — a first-writer-wins column there would be the registry that
    was deliberately removed — so the peer notices instead."""
    alice, bob = hub.client("alice"), hub.client("bob")
    mallory = hub.client("mallory")
    alice.register(name="alice")
    bob.register(name="bob")
    bob.agents()  # witness alice, alive, under her own key

    # mallory announces under alice's id, replacing the published key
    mallory.agent_id = alice.agent_id
    mallory.register(name="alice")

    roster = {a["name"]: a for a in bob.agents()}
    assert roster["alice"].get("key_changed_while_live") is True


def test_a_restart_is_not_reported_as_a_swap(hub, key):
    # The distinction that makes the signal worth reading: a restart goes quiet
    # first, so the previous entry is stale by the time the new key appears.
    alice, bob = hub.client("alice"), hub.client("bob")
    alice.register(name="alice")
    bob.register(name="bob")
    for entry in bob.agents():
        if entry["name"] == "alice":
            entry["stale"] = True
            bob.note_peer_keys([entry])

    restarted = hub.client("alice")
    restarted.register(name="alice")
    roster = {a["name"]: a for a in bob.agents()}
    assert "key_changed_while_live" not in roster["alice"]


# --- one identity across the processes that make up one agent ----------------
#
# The MCP server is long-lived and holds the keypair; the lifecycle hooks and
# every CLI command are separate processes. Without a bridge each generates its
# own, so an agent appears as a stream of one-message strangers and `release`
# from the Stop hook — an impersonation target — is signed by nobody in
# particular.


def test_a_second_process_signs_as_the_same_agent(hub, key):
    from switchboard.signing import SigningServer

    session = hub.client("alice")
    server = SigningServer(session.signing, "alice")
    if not server.start():
        pytest.skip("no unix sockets here")
    try:
        hook = hub.client("alice")
        assert hook.public_key == session.public_key
        # and a peer verifies what the hook wrote against the session's key
        bob = hub.client("bob")
        session.register(name="alice")
        bob.register(name="bob")
        bob.agents()
        hook.post("build", "released the lease")
        got = bob.inbox(channels=["build"], include_own=True)
        assert got[0]["signature"]["status"] == "verified"
    finally:
        server.close()


def test_a_different_agent_gets_its_own_identity(hub, key):
    from switchboard.signing import SigningServer

    session = hub.client("alice")
    server = SigningServer(session.signing, "alice")
    if not server.start():
        pytest.skip("no unix sockets here")
    try:
        assert hub.client("mallory").public_key != session.public_key
    finally:
        server.close()


def test_no_session_means_sign_as_yourself(hub, key):
    # A CLI command must not fail or hang because no MCP server is running.
    from switchboard.signing import SigningIdentity

    assert isinstance(hub.client("nobody-listening").signing, SigningIdentity)


def test_the_key_itself_never_crosses_the_socket(hub, key):
    from switchboard.signing import SigningServer, _ask, socket_path

    session = hub.client("alice")
    server = SigningServer(session.signing, "alice")
    if not server.start():
        pytest.skip("no unix sockets here")
    try:
        path = socket_path("alice")
        assert 0o777 & path.stat().st_mode == 0o600, "owner only"
        for op in ({"op": "pubkey"}, {"op": "sign", "payload": "aGk"}):
            reply = _ask(path, op)
            assert "private" not in json.dumps(reply).lower()
            assert set(reply) <= {"pubkey", "sig"}
    finally:
        server.close()


# --- a peer on another key must not take out a whole listing ----------------


def test_a_foreign_key_entry_does_not_break_the_board_listing(hub, key):
    """One unreadable entry used to abort `board_list` for everybody.

    Workspace routing is plaintext, so an agent on a different key writes into
    the same board we read. Raising on its value took the whole listing with
    it — including our own entries — and the coordination convention opens with
    `board_list prefix="coord/"`, so a single mismatched newcomer disabled
    handoff discovery for the agents whose key was right.
    """
    mine = hub.client("mine")
    theirs = hub.client("theirs", key=generate_key())

    mine.board_set("coord/reports/ours", {"plan": SECRET})
    theirs.board_set("coord/reports/theirs", {"plan": "not for us"})

    entries = mine.board_list()

    readable = [e for e in entries if not e.get("unreadable")]
    hidden = [e for e in entries if e.get("unreadable")]
    assert len(entries) == 2, "the listing must still return every row"
    assert len(readable) == 1 and len(hidden) == 1
    assert readable[0]["value"] == {"plan": SECRET}
    assert hidden[0]["value"] is None, "an unopenable value is dropped, not guessed"


def test_a_foreign_key_lease_does_not_break_the_claims_listing(hub, key):
    mine = hub.client("mine")
    theirs = hub.client("theirs", key=generate_key())

    mine.acquire("db/migrations", note=SECRET)
    theirs.acquire("db/migrations", note="also mine, on another key")

    leases = mine.leases()

    assert len(leases) == 2, (
        "two keys blind one resource name to two tokens, so both leases exist "
        "and neither excludes the other — which is exactly what the roster "
        "warning is for"
    )
    readable = [le for le in leases if not le.get("unreadable")]
    assert [le["note"] for le in readable] == [SECRET]


def test_our_own_entry_still_reads_back_exactly(hub, key):
    """Tolerating a neighbour must not soften our own reads."""
    mine = hub.client("mine")
    hub.client("theirs", key=generate_key()).board_set("theirs", {"x": 1})

    mine.board_set("coord/reports/ours", {"plan": SECRET})
    assert mine.board_get("coord/reports/ours") == {"plan": SECRET}


# --- prefix listings, which were dead in every encrypted room ---------------
#
# Every prefix test in this suite ran against a hub with no key, so all of
# them passed while the feature did not work at all for anyone using
# encryption. These run where it was broken.


def test_a_prefix_listing_finds_entries_in_an_encrypted_room(hub, key):
    """The regression. `prefix` was sent as a plaintext query parameter and
    matched with SQL `LIKE` against the *blinded* keys the hub stores, so it
    matched nothing and every prefixed listing came back empty.

    Empty is the worst answer it could have given: the coordination convention
    opens with `board_list prefix="coord/"`, and an empty result reads as an
    empty room rather than as a broken query. Agents correctly concluded that
    nobody was coordinating and started work twice.
    """
    mine = hub.client("mine")
    mine.board_set("coord/reports/auth", {"plan": SECRET})
    mine.board_set("scratch/notes", {"plan": "unrelated"})

    found = mine.board_list(prefix="coord/")

    assert [e["key"] for e in found] == ["coord/reports/auth"]
    assert found[0]["value"] == {"plan": SECRET}


def test_the_hub_still_cannot_do_the_matching_it_was_being_asked_to_do(hub, key):
    """Both halves of the fix, stated against the hub's own store.

    The hub holds blinded keys and could never have matched a plaintext
    prefix — so the work moved to the client, and the prefix stopped being
    sent at all. It is not merely useless to the hub; it was a plaintext
    detail of what an agent was looking for, given to a party that could do
    nothing with it.
    """
    hub.client("mine").board_set("coord/reports/auth", {"plan": SECRET})

    assert hub.board() != [], "the entry is there — the guard below is not vacuous"
    assert hub.board(prefix="coord/") == [], (
        "the hub cannot see through blinding — this is why it is not asked to"
    )
    assert len(hub.client("mine").board_list(prefix="coord/")) == 1


def test_a_listed_key_can_be_handed_straight_back_to_board_get(hub, key):
    """The same bug in its quieter form: listings returned blinded tokens, and
    feeding one back to `board_get` blinded it a second time and answered 404.
    Restoring the readable key from inside the ciphertext fixes both."""
    mine = hub.client("mine")
    mine.board_set("coord/reports/auth", {"plan": SECRET})

    entry = mine.board_list(prefix="coord/")[0]

    assert mine.board_get(entry["key"]) == {"plan": SECRET}
    assert entry["hub_key"] != entry["key"], "the routing token is still kept"


def test_a_foreign_key_entry_is_kept_rather_than_filtered_away(hub, key):
    """We cannot read a neighbour's key, so we cannot say it does not match.

    Dropping it would restore the silent wrong answer in miniature — a prefix
    listing that quietly omits rows it could not classify — and would also
    hide the key mismatch that the count in `board list` exists to report.
    """
    mine = hub.client("mine")
    mine.board_set("coord/reports/ours", {"plan": SECRET})
    mine.board_set("scratch/ours", {"plan": "unrelated"})
    hub.client("theirs", key=generate_key()).board_set("coord/reports/theirs", {"x": 1})

    found = mine.board_list(prefix="coord/")

    readable = [e for e in found if not e.get("unreadable")]
    hidden = [e for e in found if e.get("unreadable")]
    assert [e["key"] for e in readable] == ["coord/reports/ours"], (
        "our own non-matching entry is still filtered out"
    )
    assert len(hidden) == 1, "an entry we cannot classify is kept and marked"


def test_the_board_key_is_still_never_written_down_in_the_clear(hub, key, tmp_path):
    """The label travels inside the ciphertext, so this must stay true."""
    hub.client("mine").board_set("coord/reports/auth", {"plan": SECRET})

    blob = hub_bytes(str(tmp_path / "e2e.db"))
    assert b"coord/reports/auth" not in blob
    assert SECRET.encode() not in blob


def test_a_plaintext_room_still_lets_the_hub_do_the_filtering(tmp_path):
    """No key, no blinding, nothing to fix — and the cheaper path stays."""
    with make_hub(workspace=WS, db=str(tmp_path / "plain.db")) as handle:
        mine = handle.client("mine")
        mine.board_set("coord/reports/auth", {"plan": "readable"})
        mine.board_set("scratch/notes", {"plan": "unrelated"})

        assert [e["key"] for e in mine.board_list(prefix="coord/")] == [
            "coord/reports/auth"
        ]
        assert len(handle.board(prefix="coord/")) == 1, (
            "with nothing blinded the hub matches it directly, as it always did"
        )


# --- what the persisted witness log may and may not conclude -----------------


def test_a_key_change_between_processes_is_not_reported_as_a_swap(tmp_path, key):
    """The correction to a detector that shipped and immediately misfired.

    An agent with no long-lived signer mints a fresh keypair per process, so
    between processes a key change is the ordinary case rather than evidence of
    anything. Comparing against the persisted log flagged every CLI peer as
    impersonated the moment it was observed twice.
    """
    log = str(tmp_path / "peers.db")
    with make_hub(workspace=WS, key=key, peer_log=log) as h:
        alice = h.client("alice")
        alice.register(name="alice")
        h.client("bob-first-run").agents()          # one process witnesses alice

        # Alice's next command is a new process, so a new keypair - and nothing
        # else about her has changed.
        h.client("alice").register(name="alice")

        roster = {a["name"]: a for a in h.client("bob-second-run").agents()}
        assert "key_changed_while_live" not in roster["alice"]


def test_a_swap_within_one_process_is_still_noticed(hub, key):
    """The sound half, unchanged: witnessed alive under one key, now another."""
    alice, bob = hub.client("alice"), hub.client("bob")
    mallory = hub.client("mallory")
    alice.register(name="alice")
    bob.register(name="bob")
    bob.agents()                                    # bob witnesses alice, alive

    mallory.agent_id = alice.agent_id
    mallory.register(name="alice")

    roster = {a["name"]: a for a in bob.agents()}
    assert roster["alice"].get("key_changed_while_live") is True


def test_an_agent_never_reports_itself(tmp_path, key):
    """Observed in dogfooding hours after the persisted log shipped: every CLI
    agent flagged *itself*, because its own commands are separate processes and
    `note_peer_keys` did not skip self the way `key_mismatches` does."""
    log = str(tmp_path / "peers.db")
    with make_hub(workspace=WS, key=key, peer_log=log) as h:
        first = h.client("solo", agent_id="solo")
        first.register(name="solo")
        first.agents()

        second = h.client("solo", agent_id="solo")   # same agent, new process
        second.register(name="solo")
        # Keyed by name: under a workspace key the roster's agent_id is the
        # blinded form, not the string passed in.
        roster = {a["name"]: a for a in second.agents()}
        assert "key_changed_while_live" not in roster["solo"]


def test_persisted_keys_still_serve_signature_verification(tmp_path, key):
    """What the log is actually for, and why it is not simply deleted: a
    turn-based agent can verify a signature using a key an earlier process
    learned, which it would otherwise have no key for at all."""
    log = str(tmp_path / "peers.db")
    with make_hub(workspace=WS, key=key, peer_log=log) as h:
        alice = h.client("alice")
        alice.register(name="alice")
        h.client("bob-first-run").agents()           # learns alice's key, ends

        alice.post("build", "the migration is mine")

        later = h.client("bob-second-run")           # never saw a roster itself
        later.register(name="bob-second-run")
        got = later.inbox(channels=["build"])
        assert got and got[0]["signature"]["status"] == "verified", (
            "the key came from the log, not from this process's own witnessing"
        )


def test_the_witness_log_keeps_workspaces_apart(tmp_path, key):
    log = str(tmp_path / "peers.db")
    with make_hub(workspace="ws-one", key=key, peer_log=log) as h:
        alice = h.client("alice", workspace="ws-one")
        alice.register(name="alice")
        h.client("bob", workspace="ws-one").agents()

        mallory = h.client("mallory", workspace="ws-two")
        mallory.agent_id = alice.agent_id
        mallory.register(name="alice", workspace="ws-two")

        roster = {a["name"]: a for a in h.client("carol", workspace="ws-two").agents()}
        assert "key_changed_while_live" not in roster["alice"], (
            "same id in a different workspace is a different agent, not a swap"
        )


def test_a_broken_witness_log_costs_only_the_warning(tmp_path, key):
    """Fail soft: a read-only home must not take out the command."""
    from switchboard.peers import PeerKeyLog

    log = PeerKeyLog(str(tmp_path / "nope" / "deep" / "peers.db"))
    log._broken = True
    assert log.known_keys(WS, "alice") == set()
    assert log.state(WS, "alice") == (None, False)
    log.record(WS, "alice", "k", live=True)  # must not raise
