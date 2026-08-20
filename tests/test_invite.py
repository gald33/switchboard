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


# --- `--invite` on any command ----------------------------------------------
#
# `join` prints settings and proves the room; that is a step a *human* takes at
# a terminal. An agent handed a room has nowhere to put an export block — it
# has one command to run, in a room that is not its default, and no shell
# between it and the hub. So the invite has to be usable as an argument, not
# only as a thing you consume into an environment.


def test_a_single_command_runs_in_the_room_the_invite_names(cli_hub, capsys):
    """The whole claim: no exports, no `join`, no environment. One flag moves
    one command into somebody else's room and leaves everything else alone."""
    cli_hub.client("inviter", register=True).post("build", "already talking")
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key).encode()

    assert main(["--invite", blob, "history", "build", "--json"]) == 0
    heard = [m["body"] for m in json.loads(capsys.readouterr().out)]
    assert heard == ["already talking"]


def test_the_invite_beats_a_key_the_environment_already_has(cli_hub, capsys, monkeypatch):
    """The one tier where precedence is a safety property. Almost every agent
    has a key exported — `init` exports one — and an invite that lost to it
    would land the agent in the right workspace on the wrong key: registered,
    on a roster, reading nothing. Silence again, produced by the fix."""
    monkeypatch.setenv("SWITCHBOARD_KEY", generate_key())
    cli_hub.client("inviter", register=True).post("build", "readable only on the real key")
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key).encode()

    assert main(["--invite", blob, "history", "build", "--json"]) == 0
    heard = [m["body"] for m in json.loads(capsys.readouterr().out)]
    assert heard == ["readable only on the real key"]


def test_an_invite_that_omits_the_key_uses_the_one_you_hold(cli_hub, capsys, monkeypatch):
    """`invite --no-key` means "you already have this", not "unset this". If an
    omitted field cleared the environment instead, the peer it was minted for
    would join an encrypted room in the clear — which is the failure, not the
    fix."""
    monkeypatch.setenv("SWITCHBOARD_KEY", cli_hub.workspace_key)
    cli_hub.client("inviter", register=True).post("build", "sealed")
    blob = Invite(url=BASE_URL, workspace=WS, key=None).encode()

    assert main(["--invite", blob, "history", "build", "--json"]) == 0
    heard = [m["body"] for m in json.loads(capsys.readouterr().out)]
    assert heard == ["sealed"]


def test_the_invited_room_is_not_kept_for_the_next_command(cli_hub, capsys, monkeypatch):
    """A visit, not a move. `--invite` changes one invocation; the next one is
    back where it was, or a hook that borrowed a room once would quietly
    relocate everything that ran after it."""
    monkeypatch.setenv("SWITCHBOARD_URL", BASE_URL)
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", "w_home")
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key).encode()

    assert main(["--invite", blob, "say", "build", "over there", "-q"]) == 0
    capsys.readouterr()
    assert main(["history", "build", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_the_invited_agent_can_take_part_not_only_read(cli_hub, capsys):
    """Reading someone else's room is watching. Registering and speaking in it
    is joining, and it is what an agent is handed a room in order to do."""
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key).encode()

    assert main(["--invite", blob, "register", "--name", "newcomer", "-q"]) == 0
    assert main(["--invite", blob, "say", "build", "on it", "-q"]) == 0

    inviter = cli_hub.client("inviter")
    # The name reads back as itself, which is the part that matters: on a
    # mismatched key it would come back sealed, from a roster that still
    # listed one agent.
    assert "newcomer" in [a["name"] for a in inviter.agents()]
    assert "on it" in [m["body"] for m in inviter.history("build")]


def test_a_flag_that_contradicts_the_invite_is_refused_not_merged(cli_hub, capsys):
    """The four values are a room. Overriding one describes a room nobody is
    in — and arriving in one of those is the silent failure the whole feature
    exists to remove, so a half-invite must not be quietly assembled."""
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key).encode()

    assert main(["--invite", blob, "-w", "somewhere-else", "agents"]) == 1
    err = capsys.readouterr().err
    assert "--workspace" in err and "--invite" in err


def test_a_flag_that_agrees_with_the_invite_is_not_a_conflict(cli_hub, capsys):
    """A script that passes both is not confused about which room it means."""
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key).encode()
    assert main(["--invite", blob, "-w", WS, "--url", BASE_URL + "/", "agents"]) == 0


def test_a_mangled_invite_is_an_error_on_every_command_not_only_join(cli_hub, capsys):
    """It is parsed before anything is dialled, so the failure is a sentence
    rather than a traceback or, worse, a request to the default hub."""
    assert main(["--invite", "swb1_!!!not-base64", "agents"]) == 1
    assert "error:" in capsys.readouterr().err


def test_the_flag_also_works_after_the_subcommand(cli_hub, capsys):
    """Where people actually type it. Every other global flag reaches this
    position; one that did not would fail as `unrecognized arguments`."""
    cli_hub.client("inviter", register=True).post("build", "here")
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key).encode()

    assert main(["history", "build", "--invite", blob, "--json"]) == 0
    assert [m["body"] for m in json.loads(capsys.readouterr().out)] == ["here"]


def test_join_still_takes_its_invite_positionally(cli_hub, capsys):
    """The positional shares a dest with the global flag, which is exactly the
    argparse trap where an absent positional overwrites the parent's value.
    Both spellings have to keep working."""
    probe = _inviter_probe(cli_hub)
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key,
                  probe=probe).encode()

    assert main(["join", blob]) == 0
    assert "verified" in capsys.readouterr().err
    assert main(["--invite", blob, "join"]) == 0
    assert "verified" in capsys.readouterr().err


def test_a_probe_that_aged_out_is_not_reported_as_a_wrong_key(cli_hub, capsys):
    """These two failures are indistinguishable from one read and call for
    opposite responses. `join` used to answer "WRONG ROOM" to both, sending
    people off to re-key a room whose key was fine."""
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key,
                  probe=_inviter_probe(cli_hub)).encode()
    cli_hub.advance(86_401)          # past the blackboard's 24h ceiling

    assert main(["join", blob]) == 0
    err = capsys.readouterr().err
    assert "cannot verify" in err
    assert "nothing suggests your key is wrong" in err
    assert "WRONG ROOM" not in err
