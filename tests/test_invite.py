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


# --- a key the invite names but does not carry -------------------------------
#
# `invite --no-key` means "you already hold this". That is only unambiguous for
# somebody who keeps one key, in the unnamed `SWITCHBOARD_KEY`. Anyone holding
# several — which is the entire point of named keys — has to guess which was
# meant, and a guess here is the original failure: right workspace, wrong key,
# quiet room. So the invite names the key it left out.


def test_a_named_key_is_looked_up_rather_than_guessed(cli_hub, capsys, monkeypatch):
    """Two keys in the environment, and the invite says which one. Without the
    id the unnamed one wins, and it is the wrong one."""
    monkeypatch.setenv("SWITCHBOARD_KEY", generate_key())          # the decoy
    monkeypatch.setenv("SWITCHBOARD_KEY_OPS", cli_hub.workspace_key)
    cli_hub.client("inviter", register=True).post("build", "readable under ops")
    blob = Invite(url=BASE_URL, workspace=WS, key=None, key_id="ops").encode()

    assert main(["--invite", blob, "history", "build", "--json"]) == 0
    assert [m["body"] for m in json.loads(capsys.readouterr().out)] == \
        ["readable under ops"]


def test_a_named_key_this_environment_lacks_is_refused_not_substituted(
    cli_hub, capsys, monkeypatch
):
    """The refusal is the feature. Falling back to whichever key happens to be
    exported would join the room and read nothing — and look fine doing it."""
    monkeypatch.setenv("SWITCHBOARD_KEY", generate_key())
    blob = Invite(url=BASE_URL, workspace=WS, key=None, key_id="ops").encode()

    assert main(["--invite", blob, "agents"]) == 1
    err = capsys.readouterr().err
    assert "needs key 'ops'" in err
    assert "SWITCHBOARD_KEY_OPS" in err


def test_an_unnamed_key_still_means_the_one_you_have(cli_hub, monkeypatch, capsys):
    """`key_id` is additive: an invite without one behaves exactly as before,
    or every invite minted by an older client would start refusing."""
    monkeypatch.setenv("SWITCHBOARD_KEY", cli_hub.workspace_key)
    cli_hub.client("inviter", register=True).post("build", "sealed")
    blob = Invite(url=BASE_URL, workspace=WS, key=None).encode()

    assert main(["--invite", blob, "history", "build", "--json"]) == 0
    assert [m["body"] for m in json.loads(capsys.readouterr().out)] == ["sealed"]


def test_a_carried_key_beats_the_named_one(cli_hub, monkeypatch):
    """An invite that carries a key is not asking a question. The id is a label
    on it, not a second lookup that could disagree."""
    monkeypatch.setenv("SWITCHBOARD_KEY_OPS", generate_key())
    blob = Invite(url=BASE_URL, workspace=WS, key=cli_hub.workspace_key,
                  key_id="ops")
    assert blob.resolve_key() == cli_hub.workspace_key


def test_the_key_id_survives_the_round_trip_and_shows_in_the_redaction():
    blob = Invite(url="https://h", workspace="w_a", key=None, key_id="ops")
    assert Invite.decode(blob.encode()).key_id == "ops"
    assert "id ops" in blob.redacted()


def test_an_older_invite_without_a_key_id_still_decodes():
    """`key_id` is additive within v1 on purpose — a version bump would make
    every invite already in someone's clipboard refuse to open."""
    import base64 as b64

    raw = b'{"v":1,"u":"https://h","w":"w_a","t":"tok","k":"","n":"","p":""}'
    blob = PREFIX + b64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert Invite.decode(blob).key_id == ""


# --- keygen as a way of handing over a room ----------------------------------


def test_keygen_can_hand_over_the_room_it_mints(cli_hub, capsys, monkeypatch):
    """A side channel is a minted room, so minting one should produce the same
    artifact as being handed one — not a pair of values each side has to set
    correctly and separately, which is the shape that fails quietly."""
    monkeypatch.setenv("SWITCHBOARD_URL", BASE_URL)
    monkeypatch.setenv("SWITCHBOARD_TOKEN", "tok")

    assert main(["keygen", "--as-invite", "--json", "--no-input"]) == 0
    minted = json.loads(capsys.readouterr().out)
    room = Invite.decode(minted["invite"])
    assert room.url == BASE_URL
    assert room.workspace == minted["workspace"] != WS
    assert room.key == minted["key"]
    assert minted["verifiable"] is True

    # And the far side can actually get in, which is the only claim that counts.
    assert main(["--invite", minted["invite"], "say", "build", "first words", "-q"]) == 0
    capsys.readouterr()
    assert main(["--invite", minted["invite"], "history", "build", "--json"]) == 0
    assert [m["body"] for m in json.loads(capsys.readouterr().out)] == ["first words"]


def test_the_minted_room_can_be_verified_like_any_other(cli_hub, capsys, monkeypatch):
    """The probe is what makes "did we both land here" answerable. A minted
    room that could not be verified would be the one room where the check that
    matters most — two people trying to start a private conversation — is
    unavailable."""
    monkeypatch.setenv("SWITCHBOARD_URL", BASE_URL)
    assert main(["keygen", "--as-invite", "--json", "--no-input"]) == 0
    blob = json.loads(capsys.readouterr().out)["invite"]

    assert main(["join", blob]) == 0
    assert "verified" in capsys.readouterr().err


def test_plain_keygen_still_touches_no_hub(monkeypatch, capsys):
    """Unchanged, and worth a test: `keygen` is usable before anything is
    registered and with no hub reachable at all. Only `--as-invite` dials."""
    import switchboard.cli as cli_module

    def explode(*a, **k):  # pragma: no cover - the point is that it is not called
        raise AssertionError("keygen reached the hub")

    monkeypatch.setattr(cli_module, "Client", explode)
    assert main(["keygen", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["key"]


# --- where the key id comes from ---------------------------------------------


def _write_rooms(directory, entries):
    from switchboard import rooms

    path = directory / rooms.ROOMS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rooms": entries}))
    return path


def test_invite_names_the_key_its_room_declares(cli_hub, tmp_path, monkeypatch, capsys):
    """So `invite --no-key` is usable by somebody holding several keys: the
    string says which one, and they need not be told separately."""
    from switchboard import rooms

    token = "tok-for-the-ops-room"
    _write_rooms(tmp_path, [{"name": "ops", "key_id": "ops", "workspace_token": token}])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_URL", BASE_URL)
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", rooms.workspace_for(token))
    monkeypatch.setenv("SWITCHBOARD_KEY", cli_hub.workspace_key)

    assert main(["invite", "--no-key", "--json"]) == 0
    assert Invite.decode(json.loads(capsys.readouterr().out)["invite"]).key_id == "ops"


def test_a_key_id_is_never_named_for_a_room_it_does_not_open(
    cli_hub, tmp_path, monkeypatch, capsys
):
    """The declared room and the room this invocation is in can differ — `-w`,
    an environment variable, a second entry in the file. Naming the wrong key
    is worse than naming none: the receiver looks up a variable, finds a real
    key in it, and seals with it."""
    _write_rooms(tmp_path, [
        {"name": "ops", "key_id": "ops", "workspace_token": "tok-for-the-ops-room"},
    ])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_URL", BASE_URL)
    monkeypatch.setenv("SWITCHBOARD_KEY", cli_hub.workspace_key)

    assert main(["-w", "somewhere-else", "invite", "--no-key", "--json"]) == 0
    assert Invite.decode(json.loads(capsys.readouterr().out)["invite"]).key_id == ""


# --- an invite as a link ------------------------------------------------------


def test_a_link_carries_the_invite_in_the_fragment(cli_hub, capsys):
    """Not a query string, and the difference is the whole feature: a fragment
    is never sent to a server, so the page's host never sees the key — not in
    an access log, not in a Referer, not in a proxy in between."""
    assert main(["-w", WS, "invite", "--link",
                 "https://pages.example/switchboard/", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)

    head, _, fragment = out["link"].partition("#")
    assert head == "https://pages.example/switchboard/"
    assert "?" not in out["link"]
    assert fragment == out["invite"]
    assert Invite.decode(fragment).workspace == WS


def test_the_page_in_a_link_is_named_rather_than_defaulted(cli_hub, capsys):
    """A default here would be this tool choosing, on somebody's behalf, who
    gets to serve the page that will hold their key. The fragment keeps it
    from that host's server; nothing keeps it from the script it serves."""
    assert main(["invite", "--json"]) == 0
    assert "link" not in json.loads(capsys.readouterr().out)

    blob = Invite(url=BASE_URL, workspace=WS)
    with pytest.raises(TypeError):
        blob.link()          # type: ignore[call-arg]


def test_a_link_says_which_host_is_being_trusted(cli_hub, capsys):
    """The caveat names the host, because "link only to a page you trust" is
    advice and "pages.example serves the script that reads this key" is a
    fact about the string just printed."""
    assert main(["invite", "--link", "https://pages.example/switchboard/",
                 "--no-input"]) == 0
    err = capsys.readouterr().err
    assert "pages.example" in err
    assert "never receives it" in err


def test_an_existing_fragment_is_replaced_not_appended(cli_hub):
    """Two `#` in a URL is one URL with a fragment that starts with `#`, which
    no page will decode."""
    blob = Invite(url=BASE_URL, workspace=WS)
    assert blob.link("https://p/page#old").count("#") == 1


def test_a_minted_room_can_be_handed_over_as_a_link_too(cli_hub, capsys, monkeypatch):
    """`keygen --as-invite` produces a room; how it travels is a separate
    question, and a person with a browser is a perfectly ordinary answer."""
    monkeypatch.setenv("SWITCHBOARD_URL", BASE_URL)
    assert main(["keygen", "--as-invite", "--link", "https://pages.example/v/",
                 "--json", "--no-input"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["link"] == "https://pages.example/v/#" + out["invite"]
    assert Invite.decode(out["invite"]).key == out["key"]


# --- the docs, against the parser they describe -------------------------------


def _documented_switchboard_commands(text: str) -> set[str]:
    """Every `switchboard <verb>` a doc tells somebody to run.

    Global flags may precede the subcommand — `switchboard --invite <blob> say`
    is the whole point of several of these examples — so the verb is not simply
    the token after `switchboard`. Which flags swallow a following value comes
    from the parser rather than a list here, so a new global flag cannot make
    this quietly start reading its value as a command name.
    """
    import re
    import shlex

    from switchboard.cli import _GLOBAL_FLAGS

    takes_value = {
        flag for flags, options in _GLOBAL_FLAGS
        for flag in flags if options.get("action") != "store_true"
    }

    found: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"\s*(?:\$\s+)?switchboard\s+(.*)", line)
        if not match:
            continue
        try:
            tokens = shlex.split(match.group(1), comments=True)
        except ValueError:            # an unbalanced quote in prose
            continue
        skip = False
        for token in tokens:
            if skip:
                skip = False
                continue
            if token.startswith("-"):
                skip = token in takes_value and "=" not in token
                continue
            # Subcommand names are lowercase words. Anything else on this line
            # is output that happens to start with the program name, like the
            # `switchboard 0.9.0 -> http://...` banner `serve` prints.
            if re.fullmatch(r"[a-z][a-z-]*", token):
                found.add(token)
            break
    return found


@pytest.mark.parametrize("doc", ["README.md", "docs/quickstart.md", "docs/model.md"])
def test_every_command_these_docs_tell_you_to_run_exists(doc):
    """Drafting the quickstart's invite section, I wrote `switchboard roster`.
    There is no such command — `roster` is the MCP tool; the CLI verb is
    `agents` — and nothing would have caught it, because a doc is not run.

    A wrong command in a quickstart is worse than a missing one: the reader
    assumes they mistyped, or that their install is broken. Both are cheaper to
    find here.
    """
    import argparse
    from pathlib import Path

    from switchboard.cli import build_parser

    parser = build_parser()
    known: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            known |= set(action.choices)
    # `switchboard-viewer` and `switchboard-mcp` are separate console scripts,
    # not subcommands of this parser.
    ignore = {"viewer", "mcp"}

    text = (Path(__file__).resolve().parents[1] / doc).read_text()
    used = _documented_switchboard_commands(text) - ignore
    unknown = sorted(c for c in used if c not in known)
    assert not unknown, f"{doc} documents commands that do not exist: {unknown}"
