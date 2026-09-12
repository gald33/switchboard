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
    LEGACY_WHISPER_CONTEXT,
    LEGACY_WHISPER_LABEL,
    LEGACY_WHISPER_MARKER,
    WHISPER_CONTEXT,
    DecryptionError,
    WorkspaceCipher,
    _seal_to_peer,
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


def test_whispering_to_a_peer_never_seen_reads_the_roster_rather_than_refusing(key):
    """First contact is the one moment the key is guaranteed to be missing.

    This used to raise, and its own error told the caller to "call agents()
    ... before whispering" — a remedy the method can perform in one request.
    Observed 2026-09-07: the documented path for meeting a stranger needed a
    key that only exists after meeting them, and four sealed messages were
    accepted by the hub and openable by nobody.
    """
    with make_hub(workspace=WS, key=key) as h:
        alice, bob = h.client("alice"), h.client("bob")
        alice.register(name="alice")
        bob.register(name="bob")
        assert alice._peer_exchange_keys == {}, "precondition: alice knows nobody"
        alice.whisper(bob.agent_id, "hello")            # no agents() call
        # And bob opens it without one either — the receive side learns the
        # sender's key the same way. Neither end read the roster explicitly.
        assert bob._peer_exchange_keys == {}, "precondition: bob knows nobody"
        assert bob.inbox()[0]["body"] == "hello"


def test_whispering_to_a_peer_who_is_not_there_still_raises(key):
    """A downgrade is the surface's call to announce, never the library's.

    The roster is read first, so this fires only when the peer genuinely is
    not on it — never announced, or presence expired.
    """
    with make_hub(workspace=WS, key=key) as h:
        alice = h.client("alice")
        alice.register(name="alice")
        with pytest.raises(UnknownPeerExchangeKey) as caught:
            alice.whisper("nobody-was-ever-here", "hello")
        assert "roster was read" in str(caught.value)


def test_the_roster_is_read_once_per_client_not_once_per_miss(key):
    """A loop over unknown peers costs one roster read, not one each."""
    with make_hub(workspace=WS, key=key) as h:
        alice = h.client("alice")
        alice.register(name="alice")
        calls = {"n": 0}
        inner = alice.agents

        def counted(*a, **kw):
            calls["n"] += 1
            return inner(*a, **kw)

        alice.agents = counted                          # type: ignore[method-assign]
        for peer in ("ghost-one", "ghost-two", "ghost-three"):
            with pytest.raises(UnknownPeerExchangeKey):
                alice.whisper(peer, "hello")
        assert calls["n"] == 1, f"read the roster {calls['n']} times for 3 misses"


def test_a_wrong_peer_exchange_key_fails_to_open_rather_than_decoding_wrong():
    a = SigningIdentity.generate()
    b = SigningIdentity.generate()
    mallory = SigningIdentity.generate()

    envelope = seal_to_peer(
        "the launch code is 0142", my_identity=a,
        peer_exchange_key=b.exchange_key, context=WHISPER_CONTEXT,
    )
    with pytest.raises(DecryptionError):
        # bob opens with the wrong sender key — a's, not mallory's — and it
        # must fail loudly rather than hand back something else.
        unseal_from_peer(
            envelope, my_identity=b, peer_exchange_key=mallory.exchange_key,
            context=WHISPER_CONTEXT,
        )


def test_workspace_cipher_refuses_a_whisper_envelope_with_a_clear_error(key):
    a, b = SigningIdentity.generate(), SigningIdentity.generate()
    envelope = seal_to_peer(
        "hi", my_identity=a, peer_exchange_key=b.exchange_key, context=WHISPER_CONTEXT,
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


# --- what the wire said before 2.1.0 -----------------------------------------


def _sealed_by_a_2_0_1_sender(value, *, my_identity, peer_exchange_key):
    """Exactly the bytes a release from 0.11.0 to 2.0.1 put on the wire: the
    `ask` marker, the `ask` HKDF label, the `ask.body` context."""
    return _seal_to_peer(
        value, my_identity=my_identity, peer_exchange_key=peer_exchange_key,
        context=LEGACY_WHISPER_CONTEXT, pad=True,
        marker=LEGACY_WHISPER_MARKER, label=LEGACY_WHISPER_LABEL,
    )


def test_the_wire_says_whisper():
    """2.1.0 renamed the wire to match the tool. What is written is checked
    here rather than only round-tripped: a round trip passes just as well
    if both ends still said `ask`."""
    a, b = SigningIdentity.generate(), SigningIdentity.generate()
    envelope = seal_to_peer(
        "hi", my_identity=a, peer_exchange_key=b.exchange_key, context=WHISPER_CONTEXT,
    )
    assert envelope["m"] == "whisper"
    assert WHISPER_CONTEXT == "whisper.body"


def test_a_whisper_a_2_0_1_sender_sealed_still_opens():
    """Readers upgrade before senders, and the reader keeps reading: the
    marker says which names an envelope was sealed under, and the old names
    are accepted on the way in. A wrong sender key still fails on such an
    envelope, so the acceptance is of names, not of anything weaker."""
    a, b, mallory = (SigningIdentity.generate() for _ in range(3))
    legacy = _sealed_by_a_2_0_1_sender(
        "sealed by 2.0.1", my_identity=a, peer_exchange_key=b.exchange_key,
    )
    assert legacy["m"] == "ask"
    assert unseal_from_peer(
        legacy, my_identity=b, peer_exchange_key=a.exchange_key,
        context=WHISPER_CONTEXT,
    ) == "sealed by 2.0.1"
    with pytest.raises(DecryptionError):
        unseal_from_peer(
            legacy, my_identity=b, peer_exchange_key=mallory.exchange_key,
            context=WHISPER_CONTEXT,
        )


def test_a_2_0_1_reader_cannot_open_a_2_1_0_whisper():
    """The direction that does *not* hold, pinned so docs/upgrading.md is
    describing a measured fact: an old reader tries the old names and fails
    the AEAD rather than reading anything."""
    from switchboard.crypto import _derive_whisper_key, _unseal_bytes, _whisper_aad

    a, b = SigningIdentity.generate(), SigningIdentity.generate()
    new = seal_to_peer(
        "sealed by 2.1.0", my_identity=a, peer_exchange_key=b.exchange_key,
        context=WHISPER_CONTEXT,
    )
    old_key = _derive_whisper_key(b, a.exchange_key, LEGACY_WHISPER_LABEL)
    with pytest.raises(DecryptionError):
        _unseal_bytes(old_key, new,
                      _whisper_aad(LEGACY_WHISPER_CONTEXT, LEGACY_WHISPER_LABEL),
                      LEGACY_WHISPER_CONTEXT)


def test_a_0_11_0_typed_message_still_opens_and_the_alias_is_gone(key):
    """0.11.0 called this `ask`. The `type` such a peer puts on the wire
    still opens — a rename is not a reason to make an already-published
    release unreadable in the direction that matters — and the method that
    answered to the old name is gone, as 2.1.0 says it is."""
    with make_hub(workspace=WS, key=key) as h:
        alice, bob = h.client("alice"), h.client("bob")
        alice.register(name="alice")
        bob.register(name="bob")
        alice.agents()
        bob.agents()

        assert not hasattr(alice, "ask")
        alice.whisper(bob.agent_id, "sealed under the old type", type="ask")
        [got] = bob.inbox()
        assert got["type"] == "ask"
        assert got["body"] == "sealed under the old type"
        assert not got.get("unreadable")


# --- the CLI's fresh-process problem -----------------------------------------


def test_the_cli_reads_the_roster_before_draining_so_a_whisper_opens(key):
    """A fresh CLI process must open a whisper on the first look.

    **This is island game `g5`, reproduced.** Both traders were sealed their
    private capacities and tastes; neither could open a single one; no resend
    came; they played eight episodes blind to their own preferences. Opening a
    whisper needs the sender's `exchange_key`, and only a roster call puts it
    in `_peer_exchange_keys` -- a cache that is per process. The MCP server
    never hits this because it holds one long-lived client. Every CLI command
    is a new process, so the cache is always empty.

    The empty cache below is the whole of what a fresh process differs by: a
    signing daemon gives the CLI the same identity and therefore the same
    keys, and nothing carries the peer cache across.
    """
    with make_hub(workspace=WS, key=key) as h:
        mgr, trader = h.client("manager"), h.client("trader")
        mgr.register(name="manager")
        trader.register(name="trader")
        mgr.agents()          # the sender needs the recipient's key to seal
        mgr.whisper(trader.agent_id, "your capacities: salt 1.5894 per labour")

        trader._peer_exchange_keys.clear()          # a new process starts here
        trader._refreshed_for_read = False

        # `--peek`, so asserting on this does not also destroy the message --
        # precisely the trap `#172` warns about.
        #
        # This used to come back `unreadable` — what both g5 traders saw, every
        # time — and the CLI worked around it with `_learn_senders`. The client
        # now reads the roster itself before draining, so the workaround is no
        # longer what stands between a fresh process and the message.
        [blind] = trader.inbox(peek=True)
        assert not blind.get("unreadable"), "a fresh process must open it unaided"
        assert blind["body"] == "your capacities: salt 1.5894 per labour"

        # And `_learn_senders` is still safe to call: the CLI does, and calling
        # it twice must not cost a second roster read or change the answer.
        from switchboard import cli

        cli._learn_senders(trader)
        [got] = trader.inbox()
        assert not got.get("unreadable")
        assert got["body"] == "your capacities: salt 1.5894 per labour"


def test_learning_senders_never_raises_when_the_roster_cannot_be_read(key):
    """A roster that fails is a worse inbox, not a failed one.

    `inbox` is how an agent finds out why a move was refused. It must still
    drain when the roster call fails, and `#172`'s note still fires on
    anything that stays sealed.
    """
    from switchboard import cli

    class Broken:
        encrypted = True

        def agents(self):
            raise RuntimeError("hub unreachable")

    cli._learn_senders(Broken())      # must not raise


# --- the suite must not borrow another process's signer ----------------------


def test_the_signing_socket_is_private_to_this_pytest_process():
    """`socket_path` is derived from the agent id alone, so two suites on one
    machine would otherwise compute the same path — and `Client.__init__`
    attaches to whatever socket is already there, inheriting a foreign
    identity. The whisper tests above are what breaks when that happens, and
    they break intermittently, in whichever one races.

    Guarded here rather than left to the fixture, because the failure it
    prevents is invisible: nothing errors, a whisper simply comes back
    `unreadable` on a machine that happens to be busy.
    """
    import os

    from switchboard.signing import socket_path

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    assert runtime, "conftest must give this process its own runtime dir"
    assert "swb-" in runtime, f"not an isolated dir: {runtime}"
    assert str(socket_path("alice")).startswith(runtime)


def test_that_socket_path_still_fits_in_a_unix_socket():
    """The isolation above lengthens the path, and a unix socket address is
    capped near 104 bytes. Overshoot and `SigningServer.start()` fails with
    `OSError`, returns False, and every test silently exercises the
    no-socket path instead — which would disable the guard above rather than
    fail it."""
    from switchboard.signing import socket_path

    # A realistic worst case: agent ids are derived and long.
    longest = socket_path("a" * 96)
    assert len(str(longest)) < 104, f"{len(str(longest))} bytes: {longest}"


def test_the_cli_downgrades_loudly_rather_than_refusing(key, capsys, monkeypatch):
    """An unreachable peer and an unsealable one are different answers.

    The library refuses rather than quietly sending something weaker, and it
    is right to: a caller that reached for `whisper` wanted the peer-only
    property. But refusing was what made first contact impossible, so the
    surface with somebody to tell downgrades and says so — on stderr, in the
    JSON as `sealed_to_peer`, and in the success line itself.

    `--strict` is the way back to a refusal, for a secret that must not be
    readable by the rest of the room even once.
    """
    from switchboard import cli
    from switchboard.cli import build_parser

    with make_hub(workspace=WS, key=key) as h:
        alice = h.client("alice")
        alice.register(name="alice")
        monkeypatch.setattr(cli, "_make_client", lambda args: _NoClose(alice))

        parser = build_parser()
        args = parser.parse_args(["-q", "whisper", "never-announced", "hello?"])
        assert cli.cmd_whisper(args) == cli.EXIT_OK
        err = capsys.readouterr().err
        assert "room-sealed instead" in err, "the downgrade must be said out loud"
        assert "--strict" in err, "and it must name the way to refuse instead"

        strict = parser.parse_args(
            ["-q", "whisper", "--strict", "never-announced", "hello?"])
        with pytest.raises(SystemExit):
            cli.cmd_whisper(strict)


class _NoClose:
    """`cmd_whisper` uses the client as a context manager; the fixture owns it."""

    def __init__(self, client):
        self._client = client

    def __enter__(self):
        return self._client

    def __exit__(self, *exc):
        return False
