"""`Client.from_invite` and `verify`: joining a room somebody sent you.

Four values have to match a peer's — hub, workspace, token, key — and getting
any of them wrong fails *silently*. You connect, you register, you appear on a
roster beside people you cannot read, and the room looks quiet. An invite
removes three of those four chances to differ by carrying all four together;
the probe removes the fourth by making "am I in the right room" a question
with an answer.

So these tests are about the two claims that matter: everything arrives from
one string, and a wrong key is *caught* rather than tolerated.
"""

from __future__ import annotations

import pytest

from switchboard import Invite, InviteError, RoomCheck
from switchboard.crypto import generate_key
from switchboard.invite import PROBE_SENTINEL
from switchboard.testing import BASE_URL, hub

WS = "w_invited"


@pytest.fixture
def host():
    """A room with an inviter already in it, and a probe on the board."""
    key = generate_key()
    with hub(workspace=WS, key=key) as handle:
        inviter = handle.client("inviter", register=True)
        inviter.board_set("join/probe/abcd", PROBE_SENTINEL)
        inviter.post("build", "already talking in here")
        handle.workspace_key = key
        yield handle


@pytest.fixture
def host_plain():
    """The same shape with no key at all — the case the guard exists for."""
    with hub(workspace="w_plain") as handle:
        handle.client("inviter", register=True).board_set("plan", {"next": "0143"})
        yield handle


def invite_for(host, *, key=None, probe="join/probe/abcd", note=""):
    return Invite(url=BASE_URL, workspace=WS,
                  key=key if key is not None else host.workspace_key,
                  probe=probe, note=note).encode()


def joined(host, blob, **kwargs):
    """A client built from an invite, on this hub's transport."""
    return host.client_class().from_invite(blob, **kwargs)


# --- one string carries the room --------------------------------------------


def test_everything_needed_to_join_comes_from_the_invite(host):
    client = joined(host, invite_for(host))
    try:
        assert client.config.url == BASE_URL
        assert client.workspace == WS
        assert client.encrypted is True
        # And it can actually read the room, which is the only proof that the
        # four values were assembled correctly rather than merely stored.
        assert [m["body"] for m in client.history("build")] == ["already talking in here"]
    finally:
        client.close()


def test_the_invited_client_can_take_part_not_only_read(host):
    """Joining is not watching. An agent handed a room has to be able to work
    in it — register, claim, speak — or the invite was only half useful."""
    client = joined(host, invite_for(host), agent_id="newcomer")
    try:
        client.register(name="newcomer:feat/x", kind="local")
        client.acquire("src/parser.py", note="mine for ten minutes")
        client.post("build", "joined from an invite")

        names = [a["name"] for a in host.client("inviter").agents()]
        assert "newcomer:feat/x" in names
        heard = [m["body"] for m in host.client("inviter").history("build")]
        assert "joined from an invite" in heard
    finally:
        client.close()


def test_joining_leaves_this_process_its_own_identity(host):
    """The room is theirs; who you are is yours. An invite that reassigned
    identity would make two invited agents indistinguishable."""
    client = joined(host, invite_for(host), agent_id="mine")
    try:
        assert client.local_agent_id == "mine"
        # Blinded on the wire, because the room is encrypted.
        assert client.agent_id != "mine"
    finally:
        client.close()


def test_building_a_client_from_an_invite_touches_no_network(host):
    """Verification is a round trip and belongs to the caller, not a
    constructor — an async caller has no event loop inside `__init__`."""
    calls: list[str] = []
    cls = host.client_class()

    class Counting(cls):  # type: ignore[misc, valid-type]
        def _call(self, method, path, **kwargs):
            calls.append(path)
            return super()._call(method, path, **kwargs)

    client = Counting.from_invite(invite_for(host))
    try:
        assert calls == []
    finally:
        client.close()


def test_a_mangled_invite_refuses_before_a_client_exists(host):
    with pytest.raises(InviteError):
        joined(host, "swb1_not-really-an-invite")


# --- the probe: same room, or only the same hub ------------------------------


def test_the_right_key_opens_the_proof(host):
    client = joined(host, invite_for(host))
    try:
        check = client.verify()
        assert isinstance(check, RoomCheck)
        assert check.ok is True
        assert check.verdict == "verified"
    finally:
        client.close()


def test_a_wrong_key_is_caught_by_the_probe_and_not_by_the_roster(host):
    """The forty-minute failure this exists to prevent. Both parties are on
    one roster and can exchange nothing; only the probe can say so."""
    client = joined(host, invite_for(host, key=generate_key()))
    try:
        # The roster looks fine — that is precisely the problem.
        assert len(client.agents()) == 1

        check = client.verify()
        assert check.ok is False
        assert check.verdict == "wrong_room"
        assert "your key does not match theirs" in check.detail
    finally:
        client.close()


def test_an_invite_with_no_probe_says_it_cannot_check_rather_than_passing(host):
    """Silence is not a pass. An unverifiable invite must not report the same
    thing as a verified one, or the check is decoration."""
    client = joined(host, invite_for(host, probe=""))
    try:
        check = client.verify()
        assert check.ok is False
        assert check.verdict == "no_probe"
    finally:
        client.close()


def test_an_expired_probe_is_not_reported_as_a_wrong_key(host):
    """The two failures are indistinguishable from `board_get` — a wrong key
    blinds the probe's name to a token nobody stored, so the hub 404s exactly
    as it does for one that aged out. They call for opposite responses, and
    answering "wrong key" to an expired probe sends somebody to re-key a room
    whose key was fine.
    """
    host.advance(86_401)          # past the board's 24h ceiling
    client = joined(host, invite_for(host))
    try:
        check = client.verify()
        assert check.ok is False
        assert check.verdict == "probe_gone"
        assert "nothing suggests your key is wrong" in check.detail
    finally:
        client.close()


def test_a_wrong_key_is_still_told_apart_from_an_expired_one(host):
    """The other side of that coin: the board is *there*, this key just
    cannot open it, and that is a key problem rather than an expiry."""
    client = joined(host, invite_for(host, key=generate_key()))
    try:
        assert client.verify().verdict == "wrong_room"
    finally:
        client.close()


def test_a_plaintext_room_never_blames_the_key(host_plain):
    """`hub_key` equals `key` for every entry when nothing is blinded, so the
    unreadable check would fire on every plaintext room without its guard."""
    client = host_plain.client_class().from_invite(
        Invite(url=BASE_URL, workspace="w_plain", probe="join/probe/none").encode())
    try:
        assert client.verify().verdict == "probe_gone"
    finally:
        client.close()


# --- async parity ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_async_client_joins_and_verifies_the_same_way(host):
    """Same classmethod, same verdicts. Two implementations of one security
    answer is two chances for the wording — or the logic — to drift, so the
    async client inherits `from_invite` and shares `_read_proof`."""
    import httpx

    from switchboard import AsyncClient

    class BoundAsync(AsyncClient):
        def __init__(self, config=None, *, agent_id=None, key=None, http=None):
            super().__init__(config, agent_id=agent_id, key=key,
                             http=http or httpx.AsyncClient(
                                 transport=httpx.ASGITransport(app=host.app),
                                 base_url=host.url, timeout=10))

    client = BoundAsync.from_invite(invite_for(host))
    try:
        check = await client.verify()
        assert (check.ok, check.verdict) == (True, "verified")
        assert [m["body"] for m in await client.history("build")] == \
               ["already talking in here"]
    finally:
        await client.aclose()

    wrong = BoundAsync.from_invite(invite_for(host, key=generate_key()))
    try:
        assert (await wrong.verify()).verdict == "wrong_room"
    finally:
        await wrong.aclose()


# --- a key the invite names but does not carry -------------------------------


def test_the_sdk_finds_a_named_key_in_the_environment(host, monkeypatch):
    """`--no-key` means "you already hold this". Which one, when you hold
    several, is what the id answers — and holding several is the entire point
    of named keys."""
    monkeypatch.setenv("SWITCHBOARD_KEY", generate_key())        # the decoy
    monkeypatch.setenv("SWITCHBOARD_KEY_OPS", host.workspace_key)
    blob = Invite(url=BASE_URL, workspace=WS, key=None, key_id="ops").encode()

    client = joined(host, blob)
    try:
        assert [m["body"] for m in client.history("build")] == ["already talking in here"]
    finally:
        client.close()


def test_a_named_key_this_process_lacks_refuses_before_a_client_exists(host, monkeypatch):
    """Refusing is the feature. A fallback to whatever is exported builds a
    client that connects, registers, and reads nothing."""
    monkeypatch.setenv("SWITCHBOARD_KEY", generate_key())
    monkeypatch.delenv("SWITCHBOARD_KEY_OPS", raising=False)
    blob = Invite(url=BASE_URL, workspace=WS, key=None, key_id="ops").encode()

    with pytest.raises(InviteError, match="SWITCHBOARD_KEY_OPS"):
        joined(host, blob)
