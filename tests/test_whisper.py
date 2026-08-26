"""`whisper`: sealed to one peer, unreadable by anyone else who holds this
workspace's key.

The property this whole feature exists for is tested directly in
:func:`test_a_third_member_with_the_workspace_key_cannot_read_a_whisper` — a
round trip alone would pass just as happily if `whisper` quietly degraded into an
ordinary workspace-encrypted `send`.
"""

from __future__ import annotations

import pytest

from switchboard.client import UnknownPeerExchangeKey
from switchboard.crypto import (
    DecryptionError,
    WorkspaceCipher,
    seal_to_peer,
    unseal_from_peer,
)
from switchboard.signing import SigningIdentity
from switchboard.testing import hub as make_hub

WS = "whisper-ws"


@pytest.fixture
def key():
    from switchboard.crypto import generate_key

    return generate_key()


# --- the round trip, in both kinds of workspace ------------------------------


def test_whisper_round_trips_in_an_encrypted_workspace(key):
    with make_hub(workspace=WS, key=key) as h:
        alice, bob = h.client("alice"), h.client("bob")
        alice.register(name="alice")
        bob.register(name="bob")
        # Each side needs the other's exchange key before either can `whisper`:
        # alice to seal to it, bob to auto-open what arrives under it.
        alice.agents()
        bob.agents()

        alice.whisper(bob.agent_id, "the orders migration is 0142")
        [got] = bob.inbox()
        assert got["type"] == "whisper"
        assert got["body"] == "the orders migration is 0142"
        assert not got.get("unreadable")


def test_whisper_round_trips_in_a_plaintext_workspace():
    """`whisper` must not depend on `WorkspaceCipher` being configured — the
    pairwise seal is real confidentiality on its own."""
    with make_hub(workspace=WS) as h:  # no key: this hub is plaintext
        alice, bob = h.client("alice"), h.client("bob")
        assert not alice.encrypted and not bob.encrypted
        alice.register(name="alice")
        bob.register(name="bob")
        alice.agents()
        bob.agents()

        alice.whisper(bob.agent_id, {"secret": "0142"})
        [got] = bob.inbox()
        assert got["body"] == {"secret": "0142"}


# --- the property this feature exists for ------------------------------------


def test_a_third_member_with_the_workspace_key_cannot_read_a_whisper(key):
    """Alice and Bob share this workspace's key with Carol — Carol can see
    that a message was sent, its type, its size bucket, everything the outer
    transport carries. She must not be able to recover a single byte of its
    content, even though nothing stops her reading the channel directly."""
    with make_hub(workspace=WS, key=key) as h:
        alice, bob, carol = h.client("alice"), h.client("bob"), h.client("carol")
        alice.register(name="alice")
        bob.register(name="bob")
        carol.register(name="carol")
        alice.agents()
        bob.agents()
        # Carol reads the roster too — the worst case for this property: she
        # now holds both alice's and bob's exchange keys, and still must not
        # be able to derive the pair key that belongs to the two of them.
        carol.agents()

        alice.whisper(bob.agent_id, "do not tell carol")

        # Carol was never subscribed to bob's DM channel, but nothing about
        # the workspace key stops her reading it directly — that is the
        # whole reason this property has to hold at the content layer.
        got = carol.inbox(channels=[f"@{bob.agent_id}"], peek=True)
        assert len(got) == 1
        assert got[0]["type"] == "whisper"
        assert got[0].get("unreadable") is True
        assert got[0]["body"] != "do not tell carol"


# --- failure modes, each with its own clear error ----------------------------


def test_whispering_to_a_peer_whose_exchange_key_is_unknown_raises(key):
    with make_hub(workspace=WS, key=key) as h:
        alice, bob = h.client("alice"), h.client("bob")
        alice.register(name="alice")
        bob.register(name="bob")
        # alice never called agents(), so she has not learned bob's key.
        with pytest.raises(UnknownPeerExchangeKey):
            alice.whisper(bob.agent_id, "hello")


def test_a_wrong_peer_exchange_key_fails_to_open_rather_than_decoding_wrong():
    a = SigningIdentity.generate()
    b = SigningIdentity.generate()
    mallory = SigningIdentity.generate()

    envelope = seal_to_peer(
        "the launch code is 0142", my_identity=a,
        peer_exchange_key=b.exchange_key, context="ask.body",
    )
    with pytest.raises(DecryptionError):
        # bob opens with the wrong sender key — a's, not mallory's — and it
        # must fail loudly rather than hand back something else.
        unseal_from_peer(
            envelope, my_identity=b, peer_exchange_key=mallory.exchange_key,
            context="ask.body",
        )


def test_workspace_cipher_refuses_a_whisper_envelope_with_a_clear_error(key):
    a, b = SigningIdentity.generate(), SigningIdentity.generate()
    envelope = seal_to_peer(
        "hi", my_identity=a, peer_exchange_key=b.exchange_key, context="ask.body",
    )
    cipher = WorkspaceCipher.from_key(key, WS)
    with pytest.raises(DecryptionError, match="whisper"):
        cipher.unseal(envelope, "message.body")


def test_unseal_from_peer_refuses_an_ordinary_workspace_envelope(key):
    cipher = WorkspaceCipher.from_key(key, WS)
    envelope = cipher.seal("hi", "message.body")
    a, b = SigningIdentity.generate(), SigningIdentity.generate()
    with pytest.raises(DecryptionError, match="workspace"):
        unseal_from_peer(
            envelope, my_identity=a, peer_exchange_key=b.exchange_key,
            context="message.body",
        )


# --- through the socket a real MCP-server-backed agent uses ------------------


def test_whisper_through_a_socket_backed_signing_identity(key):
    """Every MCP-server-backed agent signs and seals through
    `RemoteSigningIdentity`, over the unix socket `SigningServer` listens on
    — not through an in-process `SigningIdentity` directly. The "exchange" op
    added to that protocol is exercised here, not just unit-tested in
    isolation."""
    from switchboard.signing import SigningServer

    with make_hub(workspace=WS, key=key) as h:
        session = h.client("alice")
        server = SigningServer(session.signing, "alice")
        if not server.start():
            pytest.skip("no unix sockets here")
        try:
            # A second process for the same agent — a CLI command or a hook —
            # signs and seals through the socket rather than its own keypair.
            hook = h.client("alice")
            assert hook.signing.exchange_key == session.signing.exchange_key

            bob = h.client("bob")
            session.register(name="alice")
            bob.register(name="bob")
            bob.agents()
            # `hook` is its own Client instance — a separate process, in
            # reality — so it needs its own roster read to learn bob's
            # exchange key, exactly as `whisper`'s docs say any caller must.
            hook.agents()

            hook.whisper(bob.agent_id, "released the lease")
            [got] = bob.inbox()
            assert got["body"] == "released the lease"
        finally:
            server.close()


def test_the_ask_alias_still_sends_and_a_0_11_0_typed_message_still_opens(key):
    """0.11.0 called this `ask`. Both halves of that name go on working: the
    method a caller wrote against then, and the `type` such a peer puts on
    the wire — a rename made for readability is not a reason to make an
    already-published release unreadable."""
    with make_hub(workspace=WS, key=key) as h:
        alice, bob = h.client("alice"), h.client("bob")
        alice.register(name="alice")
        bob.register(name="bob")
        alice.agents()
        bob.agents()

        # The old method name, sending under the old wire type a 0.11.0
        # client would have used.
        alice.ask(bob.agent_id, "sealed by the old name", type="ask")
        [got] = bob.inbox()
        assert got["type"] == "ask"
        assert got["body"] == "sealed by the old name"
        assert not got.get("unreadable")
