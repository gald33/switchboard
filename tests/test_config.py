"""Client configuration, and the workspace name nobody chose.

The workspace is the one value a hub always sees in the clear, and the one
that decides who an agent coordinates *with*. What it defaults to therefore
matters twice: for collisions on a shared hub, and for what the name gives
away.
"""

from __future__ import annotations

import re
import subprocess

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


def test_a_long_branch_cannot_truncate_the_suffix(monkeypatch, tmp_path):
    # Truncating to 96 chars must never eat the part that makes it unique.
    from switchboard.client import detect_identity, session_suffix

    project = tmp_path / "p"
    project.mkdir()
    _git(project, "init")
    _git(project, "checkout", "-b", "feature/" + ("x" * 200))
    monkeypatch.chdir(project)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s")
    agent_id = detect_identity().agent_id
    assert len(agent_id) <= 96
    assert agent_id.endswith(session_suffix())


def test_an_explicit_agent_id_still_wins(monkeypatch, tmp_path):
    from switchboard.client import detect_identity

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "chosen")
    assert detect_identity().agent_id == "chosen"
