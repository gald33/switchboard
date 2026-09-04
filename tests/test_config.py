"""Client configuration, and the workspace name nobody chose.

The workspace is the one value a hub always sees in the clear, and the one
that decides who an agent coordinates *with*. What it defaults to therefore
matters twice: for collisions on a shared hub, and for what the name gives
away.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from switchboard.cli import _default_workspace
from switchboard.config import ClientConfig, default_workspace, machine_suffix


def test_machine_suffix_is_short_stable_and_opaque():
    first = machine_suffix()
    assert first == machine_suffix(), "must be stable, or agents drift apart between calls"
    assert re.fullmatch(r"[0-9a-f]{8}", first)


def test_machine_suffix_does_not_leak_the_hostname():
    import socket

    host = socket.gethostname()
    assert host not in machine_suffix()
    # the whole point: unique to this machine without naming it to the hub
    assert host.split(".")[0].lower() not in machine_suffix().lower()


def test_unconfigured_clients_do_not_all_share_one_workspace(tmp_path, monkeypatch):
    # `default` put every unconfigured user on a shared hub in one room.
    # Outside a repo with a remote there is nothing shared to derive from, so
    # the name stays machine-scoped and opaque rather than becoming a
    # directory name every other user also has.
    monkeypatch.chdir(tmp_path)
    assert default_workspace() != "default"
    assert default_workspace().startswith("default-")


def test_same_machine_still_coordinates_without_configuration():
    # The property that made `default` useful: two terminals on one laptop
    # find each other with no setup. Only cross-machine matching is dropped.
    assert ClientConfig.from_env().workspace == ClientConfig.from_env().workspace


def test_env_still_wins(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", "acme/app")
    assert ClientConfig.from_env().workspace == "acme/app"


def test_empty_env_falls_back_rather_than_using_an_empty_workspace(monkeypatch):
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", "")
    assert ClientConfig.from_env().workspace == default_workspace()


def _git(tmp_path, *args):
    subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)


def test_init_prefers_the_git_remote(tmp_path):
    # The good case, and the reason cloud + CI + laptop agree for free: every
    # clone derives the same name.
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    assert _default_workspace(tmp_path) == "acme/widgets"


def test_init_disambiguates_a_repo_with_no_remote(tmp_path):
    project = tmp_path / "backend"
    project.mkdir()
    _git(project, "init", "-q")
    workspace = _default_workspace(project)
    # readable, because the user chose the directory name — but not a name
    # every other `backend` in the world also lands on
    assert workspace.startswith("backend-")
    assert workspace != "backend"
    assert workspace.endswith(machine_suffix(str(project.resolve())))


def test_no_remote_workspace_is_stable_across_calls(tmp_path):
    project = tmp_path / "api"
    project.mkdir()
    assert _default_workspace(project) == _default_workspace(project)


def test_paths_to_the_same_directory_agree(tmp_path):
    # Two agents started with different-looking cwds in one checkout are the
    # case this used to get right by accident; keep it right on purpose.
    project = tmp_path / "api"
    (project / "sub").mkdir(parents=True)
    assert _default_workspace(project) == _default_workspace(project / "sub" / "..")


def test_unrelated_projects_with_the_same_name_do_not_collide(tmp_path):
    # Two scratch checkouts both called `api` are not the same project, and on
    # a shared hub they would otherwise claim each other's leases. A repo with
    # no remote has no cross-machine clone to agree with, so folding the path
    # in costs nothing that was ever available.
    a = tmp_path / "one" / "api"
    b = tmp_path / "two" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert _default_workspace(a) != _default_workspace(b)
    assert _default_workspace(a).startswith("api-")
    assert _default_workspace(b).startswith("api-")


def test_a_remote_still_overrides_all_of_that(tmp_path):
    # Local path must never leak into the name when a remote exists, or a
    # laptop and a cloud checkout of the same repo would stop agreeing.
    for parent in ("one", "two"):
        d = tmp_path / parent / "api"
        d.mkdir(parents=True)
        _git(d, "init", "-q")
        _git(d, "remote", "add", "origin", "https://github.com/acme/api.git")
    assert _default_workspace(tmp_path / "one" / "api") == "acme/api"
    assert _default_workspace(tmp_path / "two" / "api") == "acme/api"


# --- the client's fallback, as distinct from init's derivation ---------------
#
# `init` picks a name to *write down*, so a readable `org/repo` is right: you
# saw it printed, you chose to commit it, and every clone reads the file rather
# than deriving anything. The client's fallback fires when nothing was written,
# and has to satisfy two constraints at once — identical in every clone, or
# cross-machine agents never meet; and unguessable, or a name nobody chose is a
# room a stranger can walk into knowing only where you work.
#
# Hashing the remote with the repo's root commit gives both. The root commit is
# already committed and in every clone, and cannot be obtained without read
# access to the repo.


def _commit(directory, content="x"):
    (directory / "f.txt").write_text(content)
    _git(directory, "add", "-A")
    _git(directory, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")


def _repo(directory, remote="https://github.com/acme/api.git", content="x"):
    # `content` distinguishes histories. Two commits with the same tree, author
    # and message made in the same second hash identically, so a test that
    # means "different history" has to actually make one.
    directory.mkdir(parents=True, exist_ok=True)
    _git(directory, "init")
    _git(directory, "remote", "add", "origin", remote)
    _commit(directory, content)
    return directory


def test_the_client_derives_an_opaque_name_from_the_repo(tmp_path, monkeypatch):
    project = _repo(tmp_path / "payments", "git@github.com:acme/payments.git")
    monkeypatch.chdir(project)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)

    workspace = ClientConfig.from_env().workspace
    assert workspace.startswith("w_")
    # The repo is what it is derived *from*, never what it says.
    assert "acme" not in workspace and "payments" not in workspace
    # init, asked about the same directory, answers differently on purpose:
    # it is choosing a name to print and commit, not one nothing announced.
    assert _default_workspace(project) == "acme/payments"


def test_the_name_cannot_be_derived_from_the_repo_name_alone(tmp_path, monkeypatch):
    """Two repos with the same remote and different histories do not collide,
    which is the same property that stops a stranger computing yours."""
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    one = _repo(tmp_path / "one" / "api", content="one")
    two = _repo(tmp_path / "two" / "api", content="two")

    monkeypatch.chdir(one)
    first = ClientConfig.from_env().workspace
    monkeypatch.chdir(two)
    assert ClientConfig.from_env().workspace != first


def test_clones_of_one_repo_land_in_the_same_room(tmp_path, monkeypatch):
    """The property a machine-scoped hash could not give: a laptop and a cloud
    session, in separate checkouts of one repo, agree with no configuration."""
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    origin = _repo(tmp_path / "origin")
    laptop, container = tmp_path / "a", tmp_path / "b"
    for clone in (laptop, container):
        _git(tmp_path, "clone", "-q", str(origin), str(clone))
        _git(clone, "remote", "set-url", "origin", "https://github.com/acme/api.git")

    monkeypatch.chdir(laptop)
    first = ClientConfig.from_env().workspace
    monkeypatch.chdir(container)
    assert ClientConfig.from_env().workspace == first
    assert first.startswith("w_")


def test_a_repo_with_no_commits_falls_back_rather_than_going_unsalted(
    tmp_path, monkeypatch
):
    """Nothing to salt with, so no cross-machine matching is on offer -- and a
    guessable name is not worth having instead."""
    project = tmp_path / "fresh"
    project.mkdir()
    _git(project, "init")
    _git(project, "remote", "add", "origin", "https://github.com/acme/fresh.git")
    monkeypatch.chdir(project)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)

    workspace = ClientConfig.from_env().workspace
    assert workspace.startswith("default-")
    assert "acme" not in workspace


def test_worktrees_of_one_repo_share_a_room(tmp_path, monkeypatch):
    """A worktree's `.git` is a file pointing at a gitdir that names the common
    dir. Following it means agents on one repo meet, a branch each."""
    project = _repo(tmp_path / "proj", "git@github.com:acme/proj.git")
    worktree = tmp_path / "wt"
    _git(project, "worktree", "add", str(worktree))
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)

    monkeypatch.chdir(project)
    main_room = ClientConfig.from_env().workspace
    assert (worktree / ".git").is_file()
    monkeypatch.chdir(worktree)
    assert ClientConfig.from_env().workspace == main_room


def test_unrelated_directories_with_the_same_name_do_not_collide(tmp_path, monkeypatch):
    # The promise machine_suffix's docstring makes, which the client did not
    # keep: `extra` was never passed, so every directory called `api` on a
    # machine landed in one room.
    a, b = tmp_path / "one" / "api", tmp_path / "two" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)

    monkeypatch.chdir(a)
    first = ClientConfig.from_env().workspace
    monkeypatch.chdir(b)
    second = ClientConfig.from_env().workspace
    assert first != second
    assert first.startswith("default-") and second.startswith("default-")


def test_subdirectories_of_one_checkout_still_agree(tmp_path, monkeypatch):
    # The property worth protecting: two terminals in one project meet with no
    # configuration. Keying on the working directory would have broken it.
    project = tmp_path / "proj"
    (project / "src" / "deep").mkdir(parents=True)
    _git(project, "init")
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)

    monkeypatch.chdir(project)
    at_root = ClientConfig.from_env().workspace
    monkeypatch.chdir(project / "src" / "deep")
    assert ClientConfig.from_env().workspace == at_root


def test_a_worktree_gets_its_own_room(tmp_path, monkeypatch):
    # `.git` is a file in a worktree, not a directory. It still marks a root,
    # which is what we want: a worktree is a separate checkout.
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    _git(project, "init")
    monkeypatch.chdir(project)
    main_ws = ClientConfig.from_env().workspace

    worktree = tmp_path / "wt"
    (project / "f.txt").write_text("x")
    _git(project, "add", "-A")
    _git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    _git(project, "worktree", "add", str(worktree))
    assert (worktree / ".git").is_file()
    monkeypatch.chdir(worktree)
    assert ClientConfig.from_env().workspace != main_ws


def test_outside_any_checkout_it_still_resolves(tmp_path, monkeypatch):
    # The "no repo at all" flow: no .git anywhere up the tree.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    assert ClientConfig.from_env().workspace.startswith("default-")


# --- agent identity: unique per session, stable across a resume --------------


def test_concurrent_sessions_do_not_share_an_agent_id(monkeypatch, tmp_path):
    # `kind-branch-host` was identical for two editor tabs in one worktree, so
    # they shared a read cursor and could release each other's leases — by
    # construction, not by impersonation.
    from switchboard.client import detect_identity

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-one")
    first = detect_identity().agent_id
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-two")
    second = detect_identity().agent_id
    assert first != second


def test_a_resumed_session_keeps_its_agent_id(monkeypatch, tmp_path):
    # The property the old derivation existed to provide: reclaim your own
    # leases instead of waiting out their TTL. Resuming keeps the session id.
    from switchboard.client import detect_identity

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-one")
    assert detect_identity().agent_id == detect_identity().agent_id


def test_the_session_id_is_never_sent_as_itself(monkeypatch, tmp_path):
    # agent_id is blinded when encryption is on, but reaches the operator in
    # the clear otherwise, and a host session id is not ours to hand over.
    from switchboard.client import detect_identity

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "super-secret-session-value")
    assert "super-secret-session-value" not in detect_identity().agent_id


def test_without_any_session_id_it_still_differs_per_process(monkeypatch, tmp_path):
    from switchboard.client import session_suffix

    for var in ("SWITCHBOARD_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_HOST_SESSION_ID", "TERM_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    # stable within this process, and seeded per process, so a second process
    # gets its own — the trade is losing resume, never a duplicate identity
    assert session_suffix() == session_suffix()
    assert len(session_suffix()) == 8


def test_a_long_hostname_cannot_truncate_the_suffix(monkeypatch, tmp_path):
    # Truncating to 96 chars must never eat the part that makes it unique.
    from switchboard import client as client_module
    from switchboard.client import detect_identity, session_suffix

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(client_module.socket, "gethostname", lambda: "h" * 200)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
    agent_id = detect_identity().agent_id
    assert len(agent_id) <= 96
    assert agent_id.endswith(session_suffix())


def test_an_explicit_agent_id_still_wins(monkeypatch, tmp_path):
    from switchboard.client import detect_identity

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "chosen")
    assert detect_identity().agent_id == "chosen"


def test_identity_reports_where_its_id_came_from(monkeypatch, tmp_path):
    """A pin that worked and a pin that was dropped must be tellable apart.

    Under a workspace key the id is blinded before anyone sees it, so both
    render as an opaque token. A peer agent read that as "the override was
    ignored", concluded a working feature was broken, and nearly shipped a
    remediation for a bug that did not exist.
    """
    from switchboard.client import detect_identity

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_AGENT_ID", raising=False)
    assert detect_identity().id_source == "derived"

    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "chosen")
    assert detect_identity().id_source == "SWITCHBOARD_AGENT_ID"

    assert detect_identity(agent_id="explicit").id_source == "argument"


def test_a_pinned_id_is_identical_from_any_directory(monkeypatch, tmp_path):
    """The whole point of pinning: cwd stops being part of who you are."""
    from switchboard.client import detect_identity

    project = tmp_path / "p"
    project.mkdir()
    _git(project, "init")
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "pinned-one")

    monkeypatch.chdir(project)
    inside = detect_identity()
    monkeypatch.chdir(elsewhere)
    outside = detect_identity()

    assert inside.agent_id == outside.agent_id == "pinned-one"


def test_one_session_keeps_one_id_wherever_it_runs(monkeypatch, tmp_path):
    """The defect this derivation was carrying, pinned down as its inverse.

    An id built from the branch and the directory changed whenever either
    did — so one session became two identities, with a separate roster row,
    inbox and lease set, while believing it was one agent. Both inputs are
    gone from the id now. What legitimately still differs outside a checkout
    is the *workspace*, which `rootless_warning` says.
    """
    from switchboard.client import detect_identity

    project = tmp_path / "p"
    project.mkdir()
    _git(project, "init")
    _git(project, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "root")
    _git(project, "checkout", "-q", "-b", "feature/one")
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    for var in ("SWITCHBOARD_AGENT_ID", "SWITCHBOARD_AGENT_NAME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "one-session")

    monkeypatch.chdir(project)
    inside = detect_identity()
    monkeypatch.chdir(elsewhere)
    outside = detect_identity()

    assert inside.agent_id == outside.agent_id, (
        "one session, one identity — the directory does not decide who you are"
    )
    assert inside.in_repo and not outside.in_repo


def test_the_rootless_warning_fires_only_when_it_can_bite(monkeypatch, tmp_path):
    from switchboard.client import detect_identity, rootless_warning

    project = tmp_path / "p"
    project.mkdir()
    _git(project, "init")
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.delenv("SWITCHBOARD_AGENT_ID", raising=False)

    monkeypatch.chdir(project)
    assert rootless_warning(detect_identity()) is None, "inside a checkout"

    monkeypatch.chdir(elsewhere)
    note = rootless_warning(detect_identity())
    # The room, not the id: the id no longer depends on the directory, so
    # advising a pinned id here would leave the real problem in place.
    assert note is not None and "SWITCHBOARD_WORKSPACE" in note
    assert "SWITCHBOARD_AGENT_ID" not in note

    # Pinned id: nothing about *this* is fixed, but a pinned id means the
    # caller is driving identity by hand and has said so.
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "pinned-one")
    assert rootless_warning(detect_identity()) is None


def test_the_agent_id_survives_a_branch_switch(monkeypatch, tmp_path):
    # The id is what the signer socket is keyed on, what the roster holds a
    # pubkey against, what owns a lease and a read cursor. Checking out a
    # branch is the most ordinary thing an agent does; it must not mint a
    # second identity and orphan all four.
    from switchboard.client import detect_identity

    project = tmp_path / "p"
    project.mkdir()
    _git(project, "init")
    _git(project, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "root")
    _git(project, "checkout", "-q", "-b", "feature/one")
    monkeypatch.chdir(project)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "one-session")
    monkeypatch.delenv("SWITCHBOARD_AGENT_ID", raising=False)
    monkeypatch.delenv("SWITCHBOARD_AGENT_NAME", raising=False)

    before = detect_identity()
    _git(project, "checkout", "-q", "-b", "feature/two")
    after = detect_identity()

    assert before.agent_id == after.agent_id
    # The branch still travels — on the field the roster registers and
    # `dm` resolves against, which says where the agent is now.
    assert (before.branch, after.branch) == ("feature/one", "feature/two")
    assert after.name.endswith(":feature/two")


# --- what a checkout says, and what only its owner may read -----------------
#
# `from_repo` is the tier the CLI has always had and nothing else could reach.
# It moved into the package when `switchboard_viewer/viewer.py` needed the same answer:
# a program standing in a set-up repo should land in the same room as the
# agents that live there, without being told anything twice.


def set_up_repo(directory, *, url="http://127.0.0.1:8787", workspace="w_room",
                key=None, dotenv_token=None, settings_token=None):
    """A repo as `switchboard init` leaves it."""
    (directory / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"switchboard": {"command": "switchboard-mcp", "env": {
            "SWITCHBOARD_URL": url, "SWITCHBOARD_WORKSPACE": workspace,
        }}}
    }))
    if key or settings_token:
        env = {}
        if key:
            env["SWITCHBOARD_KEY"] = key
        if settings_token:
            env["SWITCHBOARD_TOKEN"] = settings_token
        (directory / ".claude").mkdir(exist_ok=True)
        (directory / ".claude" / "settings.local.json").write_text(json.dumps({"env": env}))
    if dotenv_token:
        (directory / ".env").write_text(f"# written by init\nSWITCHBOARD_TOKEN={dotenv_token}\n")
    return directory


@pytest.fixture
def bare_env(monkeypatch):
    for name in ("SWITCHBOARD_URL", "SWITCHBOARD_WORKSPACE", "SWITCHBOARD_KEY",
                 "SWITCHBOARD_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_a_checkout_supplies_the_hub_and_room_it_committed(bare_env, tmp_path):
    set_up_repo(tmp_path)
    config = ClientConfig.from_repo(tmp_path)
    assert (config.url, config.workspace) == ("http://127.0.0.1:8787", "w_room")
    # And says the checkout chose it, which is what `isolation_warning` reads.
    assert config.url_source == "mcp.json"


def test_the_environment_still_wins_over_the_checkout(bare_env, tmp_path, monkeypatch):
    set_up_repo(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_URL", "https://hub.example.com")
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", "elsewhere")
    config = ClientConfig.from_repo(tmp_path)
    assert (config.url, config.url_source) == ("https://hub.example.com", "env")
    assert config.workspace == "elsewhere"


def test_secrets_on_disk_are_left_alone_unless_asked_for(bare_env, tmp_path):
    """The default is the interesting half: a client that *sends* must not
    quietly pick up a key from a file, or an identical-looking shell elsewhere
    seals where this one would not."""
    set_up_repo(tmp_path, key="K" * 43, dotenv_token="dev-token")
    assert ClientConfig.from_repo(tmp_path).key is None
    assert ClientConfig.from_repo(tmp_path).token is None


def test_a_reader_may_open_what_its_owner_can_open(bare_env, tmp_path):
    set_up_repo(tmp_path, key="K" * 43, dotenv_token="dev-token")
    config = ClientConfig.from_repo(tmp_path, include_secrets=True)
    assert config.key == "K" * 43
    # `.env` is where `init --local` puts the dev hub's token, and until now
    # nothing on the client side read it — so a freshly initialised repo could
    # not authenticate against its own hub without the human hunting for it.
    assert config.token == "dev-token"


def test_a_personal_token_beats_the_one_the_checkout_ships_with(bare_env, tmp_path):
    set_up_repo(tmp_path, settings_token="mine", dotenv_token="the-repo-default")
    assert ClientConfig.from_repo(tmp_path, include_secrets=True).token == "mine"


def test_a_directory_with_nothing_in_it_is_not_an_error(bare_env, tmp_path):
    """Every field falls back to what `from_env` would have said."""
    assert ClientConfig.from_repo(tmp_path).url_source == "default"


def test_the_cli_and_an_sdk_reader_resolve_the_same_room(bare_env, tmp_path, monkeypatch):
    """The reason this moved out of the CLI: two implementations of one
    precedence order are two answers that can disagree."""
    from switchboard.cli import _make_config, build_parser

    set_up_repo(tmp_path, key="K" * 43)
    monkeypatch.chdir(tmp_path)
    from_cli = _make_config(build_parser().parse_args(["agents"]))
    from_sdk = ClientConfig.from_repo(include_secrets=True)

    assert (from_cli.url, from_cli.workspace) == (from_sdk.url, from_sdk.workspace)
    assert from_cli.url_source == from_sdk.url_source
    # Differing in exactly one place, on purpose.
    assert from_cli.key is None and from_sdk.key == "K" * 43


def test_a_checkout_names_its_rooms_and_a_bare_directory_names_none(bare_env, tmp_path):
    """`rooms_in` is the test of "has this been set up", so it must not invent
    a room for a directory that declares none — anything walking a tree of
    checkouts would otherwise find one everywhere."""
    from switchboard.config import rooms_in

    assert rooms_in(tmp_path) == []
    set_up_repo(tmp_path, workspace="w_room")
    (found,) = rooms_in(tmp_path)
    assert (found.label, found.source) == (tmp_path.resolve().name, "mcp.json")
    assert found.config.workspace == "w_room"


def test_a_rooms_file_names_each_room_it_holds_a_key_for(bare_env, tmp_path, monkeypatch):
    from switchboard.config import rooms_in

    (tmp_path / ".switchboard").mkdir()
    (tmp_path / ".switchboard" / "rooms.json").write_text(json.dumps({"rooms": [
        {"name": "parser", "key_id": "default", "workspace_token": "tok-parser"},
        {"name": "ops", "key_id": "ops", "workspace_token": "tok-ops"},
        {"name": "locked", "key_id": "nobody", "workspace_token": "tok-locked"},
    ]}))
    monkeypatch.setenv("SWITCHBOARD_KEY", "K" * 43)
    monkeypatch.setenv("SWITCHBOARD_KEY_OPS", "O" * 43)

    found = rooms_in(tmp_path)

    assert [r.label for r in found] == ["parser", "ops"]
    # Each carries its own key, which is the reason one client cannot serve
    # two rooms.
    assert [r.config.key for r in found] == ["K" * 43, "O" * 43]
    assert len({r.config.workspace for r in found}) == 2
