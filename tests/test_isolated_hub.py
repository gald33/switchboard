"""Saying so when an agent is configured to coordinate but is talking to itself.

A hub on 127.0.0.1 is right for local development and wrong for anything else,
and nothing used to tell the difference. A cloud session or CI runner that
picked up a loopback URL -- from a `.mcp.json` committed by somebody's laptop,
or from the old localhost default -- reached a hub that existed only inside its
own container. `health` passed, `register` succeeded, and `agents` returned a
roster of one, which is indistinguishable from being first to arrive. Every
signal was green and only the combination was wrong.

The predicate is deliberately cheap and local: no probe, and nothing about
whether peers happen to be online. Two agents on one repo can legitimately be
alone; an agent on a hub nobody else can reach cannot legitimately be anything
else.

What must stay silent matters as much as what warns. A local dev running
`switchboard serve` in one terminal and agents in two others is the intended
happy path, and a CI job running a self-contained hub inside its own container
is a real setup somebody chose -- so an explicit `--url` or `SWITCHBOARD_URL`
never warns, at any kind.
"""

from __future__ import annotations

import json

import pytest

from switchboard.cli import _make_config, build_parser, main
from switchboard.config import (
    MANAGED_HUB_URL,
    ClientConfig,
    is_loopback,
    isolation_warning,
)

LOOPBACK = "http://127.0.0.1:8787"

ENV_VARS = ("SWITCHBOARD_URL", "SWITCHBOARD_WORKSPACE", "SWITCHBOARD_TOKEN",
            "SWITCHBOARD_KEY", "CLAUDE_CODE_REMOTE", "CODESPACES", "GITHUB_ACTIONS")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def write_mcp_json(directory, url):
    (directory / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"switchboard": {"command": "switchboard-mcp",
                                       "env": {"SWITCHBOARD_URL": url}}}
    }))


def config_for(*argv) -> ClientConfig:
    return _make_config(build_parser().parse_args(list(argv)))


# --- the predicate ----------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8787",
    "http://localhost:8787",
    "http://127.0.0.1:9000",  # a different port is no more reachable
    "http://127.0.0.2:8787",  # the whole 127/8 block is loopback
    "https://localhost",  # no port at all
    "http://[::1]:8787",
])
def test_loopback_urls_are_recognised(url):
    assert is_loopback(url)


@pytest.mark.parametrize("url", [
    MANAGED_HUB_URL,
    "https://hub.example.com",
    "http://192.168.1.10:8787",  # a LAN address other machines really can reach
    "",
])
def test_reachable_urls_are_not_loopback(url):
    assert not is_loopback(url)


@pytest.mark.parametrize("kind", ["cloud", "ci"])
def test_warns_when_a_remote_agent_inherits_a_loopback_hub(kind):
    config = ClientConfig(url=LOOPBACK, url_source="mcp.json")
    note = isolation_warning(config, kind)
    assert note is not None
    # Names the consequence and the fix, not just the condition -- "no
    # SWITCHBOARD_URL set" is a fact the reader can already see.
    assert "only from inside this container" in note
    assert "SWITCHBOARD_URL" in note
    assert "docs/environments.md" in note
    assert ".mcp.json" in note


def test_a_local_agent_on_a_local_hub_is_the_happy_path():
    """The case that must stay silent: serve in one terminal, agents in others."""
    assert isolation_warning(ClientConfig(url=LOOPBACK, url_source="default"), "local") is None
    assert isolation_warning(ClientConfig(url=LOOPBACK, url_source="mcp.json"), "local") is None


@pytest.mark.parametrize("source", ["flag", "env"])
@pytest.mark.parametrize("kind", ["cloud", "ci", "local"])
def test_choosing_loopback_here_and_now_never_warns(source, kind):
    """`--url http://127.0.0.1:8787` is somebody deciding, in this environment.

    A CI job running a self-contained hub in its own container is exactly this,
    and it is legitimate -- warning there would be nagging rather than news.
    """
    assert isolation_warning(ClientConfig(url=LOOPBACK, url_source=source), kind) is None


@pytest.mark.parametrize("kind", ["cloud", "ci", "local"])
def test_a_reachable_hub_never_warns(kind):
    for source in ("default", "mcp.json", "rooms", "env", "flag"):
        config = ClientConfig(url="https://hub.example.com", url_source=source)
        assert isolation_warning(config, kind) is None


def test_a_set_token_is_mentioned_but_not_required():
    """Corroborating evidence, not a condition: an unconfigured cloud agent is
    just as alone as one that clearly meant to coordinate."""
    with_token = isolation_warning(
        ClientConfig(url=LOOPBACK, url_source="rooms", token="t"), "cloud")
    without = isolation_warning(ClientConfig(url=LOOPBACK, url_source="rooms"), "cloud")
    assert without is not None
    assert "SWITCHBOARD_TOKEN" not in without
    assert "SWITCHBOARD_TOKEN is set here" in with_token


# --- what the resolver remembers about where the URL came from --------------


def test_an_explicit_url_is_marked_as_chosen(clean_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, MANAGED_HUB_URL)
    config = config_for("agents", "--url", LOOPBACK)
    assert (config.url, config.url_source) == (LOOPBACK, "flag")


def test_the_environment_is_marked_as_chosen(clean_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWITCHBOARD_URL", LOOPBACK)
    config = config_for("agents")
    assert (config.url, config.url_source) == (LOOPBACK, "env")


def test_a_committed_file_is_not_a_choice_this_environment_made(
    clean_env, monkeypatch, tmp_path
):
    """The trap: `init --local` on a laptop, cloned into a container."""
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, LOOPBACK)
    config = config_for("agents")
    assert (config.url, config.url_source) == (LOOPBACK, "mcp.json")
    assert isolation_warning(config, "cloud") is not None


def test_the_source_survives_being_overridden(clean_env, monkeypatch, tmp_path):
    """The structural bug this replaces: `from_env` substituted the default
    before `.mcp.json` and `--url` had been applied at all, so a flag set there
    would have called an explicitly chosen hub "defaulted"."""
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, LOOPBACK)
    assert config_for("agents").url_source == "mcp.json"
    assert config_for("agents", "--url", LOOPBACK).url_source == "flag"
    monkeypatch.setenv("SWITCHBOARD_URL", LOOPBACK)
    assert config_for("agents").url_source == "env"


def test_the_public_token_is_never_sent_to_another_hub(clean_env, monkeypatch, tmp_path):
    """A config that starts out managed and is repointed must not carry the
    managed hub's token with it."""
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, "https://hub.example.com")
    assert config_for("agents").effective_token() is None


# --- the CLI surfaces -------------------------------------------------------


@pytest.fixture
def isolated_cloud_repo(clean_env, monkeypatch, tmp_path):
    """A cloud agent in a repo whose committed hub URL is loopback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "1")
    write_mcp_json(tmp_path, LOOPBACK)
    return tmp_path


def test_whoami_warns_on_stderr_and_still_exits_zero(isolated_cloud_repo, capsys):
    assert main(["whoami"]) == 0
    captured = capsys.readouterr()
    assert "only from inside this container" in captured.err
    # The annotation goes on the line whose claim it qualifies.
    assert "this container only" in captured.out


def test_whoami_reports_the_hub_it_would_actually_dial(isolated_cloud_repo, capsys):
    """The payload used to read `from_env` alone, skipping `.mcp.json` -- so
    `whoami` printed localhost while `announce` talked to the committed hub."""
    assert main(["whoami", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hub"] == LOOPBACK


def test_json_output_carries_no_warning(isolated_cloud_repo, capsys):
    assert main(["whoami", "--json"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # parses: nothing leaked into the document
    assert "warning" not in captured.out
    assert "only from inside this container" in captured.err


def test_quiet_suppresses_it(isolated_cloud_repo, capsys):
    assert main(["whoami", "--quiet"]) == 0
    captured = capsys.readouterr()
    assert "only from inside this container" not in captured.err
    # Including the annotation, which points at a note `--quiet` just removed.
    assert "this container only" not in captured.out


def test_a_local_agent_sees_nothing(clean_env, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, LOOPBACK)
    assert main(["whoami"]) == 0
    captured = capsys.readouterr()
    assert "only from inside this container" not in captured.err
    assert "this container only" not in captured.out


def test_an_explicit_url_silences_it_at_any_kind(isolated_cloud_repo, capsys):
    assert main(["whoami", "--url", LOOPBACK]) == 0
    assert "only from inside this container" not in capsys.readouterr().err


# --- the surfaces that need a hub to answer ---------------------------------
#
# `register` and `health` warn only after the call succeeds, which is the
# point: the lie being corrected is a *successful* one. Bound to an in-process
# hub the same way test_cli_board.py does it, so `main()` makes the real round
# trip without a subprocess.


@pytest.fixture
def cli_bound_to_a_real_hub(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import switchboard.cli as cli_module
    from switchboard.client import Client as RealClient
    from switchboard.config import ServerConfig
    from switchboard.server import create_app
    from switchboard.store import Store

    store = Store(str(tmp_path / "hub.db"))
    app = create_app(ServerConfig(db_path=str(tmp_path / "hub.db")), store=store)
    with TestClient(app) as http:

        class _Bound(RealClient):
            def __init__(self, config=None, *, agent_id=None, timeout=40.0, key=None):
                super().__init__(config, agent_id=agent_id, timeout=timeout, key=key)
                self._http.close()
                self._http = http

            def close(self) -> None:
                pass

        monkeypatch.setattr(cli_module, "Client", _Bound)
        yield
    store.close()


def test_register_warns_and_names_the_hub_it_registered_against(
    isolated_cloud_repo, cli_bound_to_a_real_hub, capsys
):
    """`registered <id> (cloud) in <workspace>` read identically whether the
    hub was shared or a dead end. This is the moment the claim is made."""
    assert main(["register"]) == 0
    captured = capsys.readouterr()
    assert LOOPBACK in captured.out
    assert "only from inside this container" in captured.err


def test_health_warns_beside_its_ok(
    isolated_cloud_repo, cli_bound_to_a_real_hub, capsys
):
    """`{"ok": true}` about a hub nobody else can reach is the most misleading
    of the three outputs. This command has no `--json` branch -- stdout is
    always the hub's JSON -- so the warning has nowhere to go but stderr."""
    assert main(["health"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "only from inside this container" in captured.err


def test_register_stays_quiet_under_quiet(
    isolated_cloud_repo, cli_bound_to_a_real_hub, capsys
):
    assert main(["register", "--quiet"]) == 0
    assert "only from inside this container" not in capsys.readouterr().err


def test_a_local_agent_registering_locally_sees_nothing(
    clean_env, monkeypatch, tmp_path, cli_bound_to_a_real_hub, capsys
):
    monkeypatch.chdir(tmp_path)
    write_mcp_json(tmp_path, LOOPBACK)
    assert main(["register"]) == 0
    assert "only from inside this container" not in capsys.readouterr().err


# --- the bridge -------------------------------------------------------------


def test_mcp_whoami_carries_the_warning(isolated_cloud_repo):
    """An agent driving the bridge never runs the CLI, so a CLI-only warning
    would miss the audience this failure hits hardest."""
    from switchboard.config import ClientConfig as _Config
    from switchboard.mcp_server import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge.config = _Config(url=LOOPBACK, url_source="mcp.json")

    class _Identity:
        kind = "cloud"
        agent_id = "a"
        name = "a"
        branch = None

    bridge.identity = _Identity()
    bridge.client = type("_C", (), {"agent_id": "a"})()
    bridge._touch = lambda: 0
    bridge._calibration = lambda: None
    out = Bridge.whoami(bridge)
    assert "only from inside this container" in out["WARNING"]


def test_mcp_whoami_is_silent_when_the_hub_is_shared():
    from switchboard.config import ClientConfig as _Config
    from switchboard.mcp_server import Bridge

    bridge = Bridge.__new__(Bridge)
    bridge.config = _Config(url=MANAGED_HUB_URL, url_source="default")

    class _Identity:
        kind = "cloud"
        agent_id = "a"
        name = "a"
        branch = None

    bridge.identity = _Identity()
    bridge.client = type("_C", (), {"agent_id": "a"})()
    bridge._touch = lambda: 0
    bridge._calibration = lambda: None
    assert "WARNING" not in Bridge.whoami(bridge)
