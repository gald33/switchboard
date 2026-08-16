"""One string instead of five facts, and a join that proves the room.

Joining takes a hub, a token, a workspace, a key and an identity, and every one
must match a peer's independently. What makes that expensive is not the count:
it is that getting any of them wrong fails silently — every call succeeds and
you are simply somewhere else, alone, with an inbox indistinguishable from a
quiet one. Two agents in this project's own dogfooding lost forty minutes to
exactly that, on the same hub and workspace, on two different keys, watching a
roster that listed them both.
"""

from __future__ import annotations

import json

import pytest

from switchboard.cli import main
from switchboard.crypto import generate_key
from switchboard.invite import PREFIX, PROBE_SENTINEL, Invite, InviteError
from switchboard.testing import BASE_URL, hub

WS = "invite-ws"


def test_an_invite_round_trips_every_fact():
    original = Invite(
        url="https://hub.example.com", workspace="w_abc",
        token="tok", key="k" * 43, note="the billing room",
    )
    assert Invite.decode(original.encode()) == original


def test_an_invite_survives_being_pasted():
    """No padding on the way out, restored on the way in — `=` is exactly the
    character that gets mangled by the places people paste things."""
    blob = Invite(url="https://h", workspace="w", token="t", key="k").encode()
    assert "=" not in blob
    assert Invite.decode(f"  {blob}\n") .workspace == "w"


def test_something_that_is_not_an_invite_says_so():
    with pytest.raises(InviteError, match="not a switchboard invite"):
        Invite.decode("https://hub.example.com")


def test_a_truncated_invite_fails_at_the_parse():
    """The whole point is to fail here rather than an hour later in an empty
    room, so a half-readable invite must never half-work."""
    blob = Invite(url="https://h", workspace="w").encode()
    with pytest.raises(InviteError):
        Invite.decode(blob[:-8])


def test_an_invite_from_a_future_version_is_refused_not_guessed():
    """Reading five of six fields and joining somewhere unexpected would be the
    silent failure this exists to remove, reintroduced by the fix itself."""
    import base64

    payload = base64.urlsafe_b64encode(
        json.dumps({"v": 99, "u": "https://h", "w": "w"}).encode()
    ).decode().rstrip("=")
    with pytest.raises(InviteError, match="version"):
        Invite.decode(PREFIX + payload)


def test_a_hub_url_is_required():
    import base64

    payload = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "w": "w"}).encode()
    ).decode().rstrip("=")
    with pytest.raises(InviteError, match="hub URL"):
        Invite.decode(PREFIX + payload)


def test_redaction_never_leaks_the_secrets_it_describes():
    """It is meant for logs and PR bodies, so this is the assertion."""
    described = Invite(
        url="https://h", workspace="w", token="SECRET-TOKEN", key="SECRET-KEY",
    ).redacted()
    assert "SECRET-TOKEN" not in described
    assert "SECRET-KEY" not in described
    assert "token=set" in described and "key=set" in described


def test_an_invite_can_deliberately_omit_a_secret():
    blob = Invite(url="https://h", workspace="w", token=None, key=None)
    again = Invite.decode(blob.encode())
    assert again.token is None and again.key is None
    assert "token=none" in again.redacted()


# --- through the CLI --------------------------------------------------------


@pytest.fixture
def cli_hub(monkeypatch):
    import switchboard.cli as cli_module

    key = generate_key()
    with hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        handle.workspace_key = key
        yield handle


def _inviter_probe(handle) -> str:
    """What `switchboard invite` leaves behind: a value sealed with the
    inviter's key, for a joiner to try to open."""
    probe = "coord/join-probe/fixture"
    handle.client("inviter").board_set(probe, PROBE_SENTINEL)
    return probe


def test_join_verifies_by_opening_what_the_inviter_left(cli_hub, capsys, monkeypatch):
    """A roster listing you both proves nothing — that is what the forty-minute
    failure looked like. Opening the OTHER side's sealed value is the check
    that means something; opening your own is self-consistent in any room."""
    blob = Invite(
        url=BASE_URL, workspace=WS, token=None, key=cli_hub.workspace_key,
        probe=_inviter_probe(cli_hub),
    ).encode()
    assert main(["join", blob]) == 0
    assert "verified" in capsys.readouterr().err


def test_join_with_the_wrong_key_fails_instead_of_looking_fine(
    cli_hub, capsys, monkeypatch
):
    """The exact failure this feature exists for: same hub, same workspace,
    different key. Everything the hub does still succeeds."""
    probe = _inviter_probe(cli_hub)  # sealed with the hub's real key
    blob = Invite(
        url=BASE_URL, workspace=WS, token=None, key=generate_key(), probe=probe,
    ).encode()
    assert main(["join", blob]) == 1
    assert "WRONG ROOM" in capsys.readouterr().err


def test_an_invite_with_no_proof_says_it_could_not_verify(cli_hub, capsys):
    """Silence here would be the bug: an unverified join that prints settings
    and nothing else reads exactly like a verified one."""
    blob = Invite(url=BASE_URL, workspace=WS, token=None, key=cli_hub.workspace_key)
    assert main(["join", blob.encode()]) == 0
    assert "cannot verify" in capsys.readouterr().err


def test_join_can_print_settings_without_touching_the_hub(cli_hub, capsys):
    blob = Invite(url=BASE_URL, workspace=WS, token="tok", key=None).encode()
    assert main(["join", blob, "--no-verify"]) == 0
    out = capsys.readouterr().out
    assert "export SWITCHBOARD_URL=" in out
    assert "export SWITCHBOARD_WORKSPACE=" in out


def test_a_bad_invite_is_an_error_exit_not_a_partial_join(cli_hub, capsys):
    assert main(["join", "swb1_!!!not-base64"]) == 1
    assert "error:" in capsys.readouterr().err
