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


def test_unconfigured_clients_do_not_all_share_one_workspace():
    # `default` put every unconfigured user on a shared hub in one room.
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
