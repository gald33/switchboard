"""Where a CLI command decides it is talking to, and as whom.

`switchboard init` writes the hub URL and workspace into `.mcp.json` and a
token into `.claude/settings.local.json`. Resolving those used to happen
inside `cmd_whoami` alone, so `init` would wire a repo up and every other
command still dialled the default localhost -- while `whoami`, the one place
you would look to check, reported the configuration nothing else used.
"""

from __future__ import annotations

import json

import pytest

from switchboard.cli import _make_config, build_parser
from switchboard.config import MANAGED_HUB_TOKEN, MANAGED_HUB_URL

ENV_VARS = ("SWITCHBOARD_URL", "SWITCHBOARD_WORKSPACE", "SWITCHBOARD_TOKEN",
            "SWITCHBOARD_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def write_mcp_json(directory, **env):
    (directory / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"switchboard": {"command": "switchboard-mcp", "env": env}}}
    ))


def write_local_settings(directory, **env):
    path = directory / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env}))


def config_for(*argv):
    return _make_config(build_parser().parse_args(list(argv)))


def test_repo_config_supplies_hub_and_workspace(clean_env, monkeypatch, tmp_path):
    """The plain case that was broken: `init` ran, and a bare command in that
    repo should reach the hub it wired up rather than localhost."""
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, SWITCHBOARD_URL="https://hub.example",
                   SWITCHBOARD_WORKSPACE="org/repo",
                   SWITCHBOARD_TOKEN="repo-token")
    config = config_for("agents")
    assert config.url == "https://hub.example"
    assert config.workspace == "org/repo"
    assert config.token == "repo-token"


def test_the_environment_beats_the_checkout(clean_env, monkeypatch, tmp_path):
    """An exported value is a deliberate override of whatever the repo says."""
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, SWITCHBOARD_URL="https://hub.example",
                   SWITCHBOARD_WORKSPACE="org/repo")
    monkeypatch.setenv("SWITCHBOARD_URL", "https://elsewhere.example")
    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", "other/ws")
    config = config_for("agents")
    assert config.url == "https://elsewhere.example"
    assert config.workspace == "other/ws"


def test_flags_beat_everything(clean_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, SWITCHBOARD_URL="https://hub.example",
                   SWITCHBOARD_WORKSPACE="org/repo")
    monkeypatch.setenv("SWITCHBOARD_URL", "https://elsewhere.example")
    config = config_for("--url", "https://flag.example", "-w", "flag/ws", "agents")
    assert config.url == "https://flag.example"
    assert config.workspace == "flag/ws"


def test_a_machine_token_beats_the_one_in_the_checkout(clean_env, monkeypatch, tmp_path):
    """`.mcp.json` is committed and carries whatever default the repo ships;
    `settings.local.json` is this machine's and is gitignored. The personal
    one has to win, or a shared default would silently shadow it."""
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, SWITCHBOARD_TOKEN="repo-token")
    write_local_settings(tmp_path, SWITCHBOARD_TOKEN="my-token")
    assert config_for("agents").token == "my-token"


def test_the_workspace_key_is_not_taken_from_the_repo(clean_env, monkeypatch, tmp_path):
    """Deliberate, and the reason this resolution is not simply symmetric.

    Claude Code injects settings.local.json into agents it spawns, but a
    plain shell has nothing exported and would send in the clear. Reading
    the key here would have `whoami` report a sealed channel that this
    invocation does not seal -- an overclaim that gets someone to trust an
    unsealed channel.
    """
    monkeypatch.chdir(tmp_path)
    write_local_settings(tmp_path, SWITCHBOARD_KEY="K" * 43)
    assert config_for("agents").key is None


def test_no_repo_config_falls_back_to_the_managed_hub(clean_env, monkeypatch, tmp_path):
    """An empty directory dials the same hub `init` writes.

    This used to be localhost, which is what made the whole class of failure
    possible: `init` pointed a repo at the managed hub while any path that
    missed `.mcp.json` -- notably the MCP bridge, which never reads it --
    quietly dialled a hub inside its own container instead.
    """
    monkeypatch.chdir(tmp_path)
    config = config_for("agents")
    assert config.url == MANAGED_HUB_URL
    assert config.url_source == "default"
    # Published, not secret: without it the new default would 401 for anyone
    # who never ran `init`, which is a worse out-of-the-box story than the
    # localhost default it replaced.
    assert config.effective_token() == MANAGED_HUB_TOKEN
    # per-machine suffix, so two unconfigured machines do not collide
    assert config.workspace.startswith("default")
    assert config.token is None


# --- how whoami answers "how am I addressed?" (#90) --------------------------
#
# Everything leaving the machine is blinded when a workspace key is set, so a
# peer sees the blinded id -- while `whoami`, the one command you would run to
# find out how to be addressed, reported the local name. The two coincide with
# no key, which is why it survived: only encrypted workspaces were affected,
# and only there does it matter.


def test_whoami_reports_the_id_peers_actually_use(clean_env, monkeypatch, capsys, tmp_path):
    from switchboard.cli import main

    monkeypatch.chdir(tmp_path)
    code = main(["--url", "http://testserver", "-w", "ws", "--key", "K" * 43,
                 "--agent-id", "local-name", "--json", "whoami"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["local_agent_id"] == "local-name"
    # The addressable id is the blinded one, and it is what a peer would see.
    assert payload["agent_id"] != "local-name"
    from switchboard.crypto import WorkspaceCipher
    assert payload["agent_id"] == WorkspaceCipher.from_key("K" * 43, "ws").blind(
        "local-name", "agent")


def test_whoami_ids_coincide_without_a_key(clean_env, monkeypatch, capsys, tmp_path):
    """Nothing changes for the unencrypted case -- which is the whole reason
    this went unnoticed, and worth pinning so a future change cannot make the
    plain path suddenly report two different names."""
    from switchboard.cli import main

    monkeypatch.chdir(tmp_path)
    code = main(["--url", "http://testserver", "-w", "ws",
                 "--agent-id", "local-name", "--json", "whoami"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent_id"] == payload["local_agent_id"] == "local-name"


# --- a room the repo did not choose, on the way to the hub --------------------


@pytest.fixture
def cli_hub():
    """A hub for commands that actually dial one. The workspace does not
    matter here — the point is which one the CLI *chose*, not what is in it."""
    from switchboard.testing import hub as _hub

    with _hub() as handle:
        yield handle


def _ambiguous_repo(tmp_path, monkeypatch):
    from switchboard import rooms as rooms_mod

    path = tmp_path / rooms_mod.ROOMS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rooms": [
        {"name": "main", "key_id": "default", "workspace_token": "tok-main"},
        {"name": "guest", "key_id": "guest", "workspace_token": "tok-guest"},
    ]}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_KEY", "k" * 43)
    monkeypatch.setenv("SWITCHBOARD_KEY_GUEST", "g" * 43)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    monkeypatch.delenv("SWITCHBOARD_ROOM", raising=False)


def test_a_read_command_says_it_is_in_a_room_the_repo_did_not_choose(
    tmp_path, monkeypatch, capsys, cli_hub,
):
    """On a *read*, deliberately. `agents` and `inbox` are where this failure
    is most convincing — an empty answer from the wrong room is indexed by the
    reader as "nobody is here yet", which is the belief that costs the
    afternoon. Warning only on the publishing commands would leave it."""
    import switchboard.cli as cli_module

    _ambiguous_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "Client", cli_hub.client_class())
    monkeypatch.setattr(cli_module, "_ROOMS_WARNED", False)

    assert cli_module.main(["agents"]) == 0
    err = capsys.readouterr().err
    assert "rooms file" in err
    assert "guest, main" in err


def test_quiet_silences_it(tmp_path, monkeypatch, capsys, cli_hub):
    """It is a warning, not a failure — a hook running with `-q` has already
    said it does not want commentary on stdout or stderr."""
    import switchboard.cli as cli_module

    _ambiguous_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "Client", cli_hub.client_class())
    monkeypatch.setattr(cli_module, "_ROOMS_WARNED", False)

    assert cli_module.main(["-q", "agents"]) == 0
    assert "rooms file" not in capsys.readouterr().err


def test_it_is_said_once_rather_than_per_client(tmp_path, monkeypatch, capsys, cli_hub):
    import switchboard.cli as cli_module

    _ambiguous_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_module, "Client", cli_hub.client_class())
    monkeypatch.setattr(cli_module, "_ROOMS_WARNED", False)

    cli_module.main(["agents"])
    cli_module.main(["agents"])
    assert capsys.readouterr().err.count("rooms file") == 1


def test_help_still_works_with_a_rooms_file_too_broken_to_read(tmp_path, monkeypatch):
    """The property the silence was protecting, and the reason this is a
    warning rather than a raise. `from_env` runs for every command, including
    the ones with nothing to do with a hub."""
    import switchboard.cli as cli_module
    from switchboard import rooms as rooms_mod

    path = tmp_path / rooms_mod.ROOMS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        cli_module.main(["--help"])
    assert exit_info.value.code == 0
