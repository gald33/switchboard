"""Tests for `switchboard init` — the one-shot repo setup command.

Every writer it drives (`.mcp.json`, `.claude/settings.json`, `CLAUDE.md`,
the coordination skill, `.env`) has to merge into whatever is already there
and be safe to run twice, so that is most of what these tests check.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from switchboard.cli import (
    _CLAUDE_MD_MARKER,
    _CLAUDE_MD_SECTION,
    _CLAUDE_MD_SECTION_HISTORY,
    _HOOKS_DIR,
    _LOCAL_SETTINGS_REL,
    _SESSION_START_CMD_HISTORY,
    _STOP_CMD_HISTORY,
    _WAKE_REL,
    MANAGED_HUB_URL,
    _hook_env_prefix,
    _session_start_cmd,
    _session_start_script,
    _skill_history,
    _skill_source,
    _stop_cmd,
    _stop_script,
    _wake_script,
    main,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_init(monkeypatch, capsys, tmp_path, *extra_args):
    monkeypatch.chdir(tmp_path)
    code = main(["init", *extra_args])
    out = capsys.readouterr().out
    return code, out


# `--local` opts into the old self-hosted behaviour (dev token, .env, the
# machine-only warning). Bare `init` now defaults to the managed hub instead —
# see the "managed by default" tests below.


def test_fresh_repo_writes_everything(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path, "--local")
    assert code == 0

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    # the committed command resolves the binary rather than assuming it, so a
    # clone or a cloud session needs no install step first
    entry = mcp["mcpServers"]["switchboard"]
    assert entry["command"] == "sh"
    assert "switchboard-mcp" in entry["args"][1]
    assert mcp["mcpServers"]["switchboard"]["env"]["SWITCHBOARD_URL"] == "http://127.0.0.1:8787"
    assert "SWITCHBOARD_TOKEN" not in mcp["mcpServers"]["switchboard"]["env"]

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "switchboard" in settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "switchboard" in settings["hooks"]["Stop"][0]["hooks"][0]["command"]

    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert "## Coordinating with other agents" in claude_md

    skill = tmp_path / ".claude" / "skills" / "switchboard-coordinate" / "SKILL.md"
    assert skill.read_text() == _skill_source()

    env = (tmp_path / ".env").read_text()
    assert "SWITCHBOARD_TOKEN=" in env
    token_line = [ln for ln in env.splitlines() if ln.startswith("SWITCHBOARD_TOKEN=")][0]
    assert len(token_line.split("=", 1)[1]) > 20

    assert "Next" in out


def test_idempotent(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path, "--local")
    first_token = (tmp_path / ".env").read_text()

    code, out = run_init(monkeypatch, capsys, tmp_path, "--local")
    assert code == 0
    assert (tmp_path / ".env").read_text() == first_token

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionStart"]) == 1
    assert len(settings["hooks"]["Stop"]) == 1

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert list(mcp["mcpServers"].keys()) == ["switchboard"]

    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert claude_md.count("## Coordinating with other agents") == 1

    skill_path = tmp_path / ".claude" / "skills" / "switchboard-coordinate" / "SKILL.md"
    assert skill_path.read_text() == _skill_source()

    assert "already" in out


def test_merges_into_existing_mcp_json(monkeypatch, capsys, tmp_path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other-tool": {"command": "other-mcp"}}})
    )
    run_init(monkeypatch, capsys, tmp_path)
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert "other-tool" in mcp["mcpServers"]
    assert "switchboard" in mcp["mcpServers"]


def test_merges_into_existing_claude_settings(monkeypatch, capsys, tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    hook = {"hooks": [{"type": "command", "command": "echo hi"}]}
    existing = {"hooks": {"SessionStart": [hook]}}
    (claude_dir / "settings.json").write_text(json.dumps(existing))
    run_init(monkeypatch, capsys, tmp_path)
    settings = json.loads((claude_dir / "settings.json").read_text())
    commands = [h["command"] for h in settings["hooks"]["SessionStart"][0]["hooks"]] + [
        g["hooks"][0]["command"] for g in settings["hooks"]["SessionStart"][1:]
    ]
    assert any(c == "echo hi" for c in commands)
    assert any("switchboard" in c for c in commands)
    assert "Stop" in settings["hooks"]


def _project_with_hooks(tmp_path, url, workspace):
    """A repo with the hook scripts on disk, as `init` would leave it."""
    proj = tmp_path / "repo"
    (proj / _HOOKS_DIR).mkdir(parents=True)
    (proj / _HOOKS_DIR / "session-start.sh").write_text(_session_start_script(url, workspace))
    (proj / _HOOKS_DIR / "stop.sh").write_text(_stop_script(url, workspace))
    return proj


def _run_hook(command, project, fake_bin):
    """Run a registered hook command the way an agent runner does: from the
    project root, with nothing switchboard-related in the environment but a
    token. CLAUDE_PROJECT_DIR is deliberately absent so the shim's fallback
    is what gets exercised — that is the path a non-Claude runner takes."""
    return subprocess.run(
        ["bash", "-c", command],
        cwd=project,
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "SWITCHBOARD_TOKEN": "shh"},
        capture_output=True,
        text=True,
    )


# --- #32: hooks must not depend on ambient SWITCHBOARD_URL/WORKSPACE -------
#
# SessionStart/Stop hooks run as plain shell commands, not inside the
# switchboard-mcp subprocess `.mcp.json`'s `env` block reaches — a cloud
# session can have SWITCHBOARD_TOKEN ambient with no URL or workspace at
# all, and the hook would silently register against 127.0.0.1:8787/default
# instead, guarded by `|| true` into total silence. `init` knows both
# values at generation time and neither is secret, so they get baked in.


def test_hook_scripts_embed_the_resolved_url_and_workspace():
    # The bodies moved out of the agent's config into scripts switchboard
    # owns, but #32's guarantee is unchanged: the values are baked in, not
    # read from an environment the hook does not share.
    start = _session_start_script("https://hub.example.com", "acme/widgets")
    stop = _stop_script("https://hub.example.com", "acme/widgets")
    for script in (start, stop):
        assert (
            "export SWITCHBOARD_URL=https://hub.example.com; "
            "export SWITCHBOARD_WORKSPACE=acme/widgets;"
        ) in script


def test_hook_scripts_shell_quote_a_workspace_with_special_characters():
    # A workspace name is usually `org/repo`, but nothing stops it from
    # containing something shell-unsafe if a user overrides it by hand.
    start = _session_start_script("https://hub.example.com", "it's a workspace")
    assert "export SWITCHBOARD_WORKSPACE='it'\"'\"'s a workspace';" in start


def test_the_registered_command_is_identical_everywhere():
    # The whole point of the shim: one constant string, so recognizing our own
    # output in someone else's config is an exact match, not a heuristic.
    assert _session_start_cmd("https://a.example", "acme/one") == _session_start_cmd(
        "http://127.0.0.1:8787", "globex/two"
    )
    assert _stop_cmd("https://a.example", "acme/one") == _stop_cmd(
        "http://127.0.0.1:8787", "globex/two"
    )
    # and it carries no hub detail that would need updating later
    for cmd in (_session_start_cmd("https://a.example", "acme/one"),
                _stop_cmd("https://a.example", "acme/one")):
        assert "a.example" not in cmd and "acme/one" not in cmd


def test_init_bakes_the_resolved_hub_into_the_written_hooks(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    code = main(["-w", "acme/widgets", "--url", "https://hub.example.com", "init"])
    capsys.readouterr()
    assert code == 0
    start = (tmp_path / _HOOKS_DIR / "session-start.sh").read_text()
    stop = (tmp_path / _HOOKS_DIR / "stop.sh").read_text()
    assert "SWITCHBOARD_URL=https://hub.example.com" in start
    assert "SWITCHBOARD_WORKSPACE=acme/widgets" in stop
    # and the agent's config only points at them
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    start_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "hub.example.com" not in start_cmd
    assert f"{_HOOKS_DIR}/session-start.sh" in start_cmd


def test_session_start_hook_resolves_the_hub_with_only_the_token_ambient(tmp_path):
    """The actual reproduction from #32, via a fake `switchboard` on PATH
    instead of a live hub: run the generated hook through a real shell with
    an environment that has SWITCHBOARD_TOKEN and nothing else, and confirm
    the process it invokes actually saw the right URL and workspace."""
    record = tmp_path / "seen.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    stub = fake_bin / "switchboard"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "url=$SWITCHBOARD_URL workspace=$SWITCHBOARD_WORKSPACE '
        f'token=$SWITCHBOARD_TOKEN" >> {record}\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    proj = _project_with_hooks(tmp_path, "http://hub.internal", "acme/widgets")
    result = _run_hook(_session_start_cmd("", ""), proj, fake_bin)
    assert result.returncode == 0, result.stderr
    assert record.read_text() == "url=http://hub.internal workspace=acme/widgets token=shh\n"


def test_stop_hook_resolves_the_hub_for_every_nested_invocation(tmp_path):
    """The Stop hook chains three separate `switchboard` invocations — one
    of them launched by a nested `python -c ... subprocess.run(...)`, not
    directly by the shell. All three need to see the exported env, not just
    the first one the shell pipe touches directly."""
    record = tmp_path / "seen.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    stub = fake_bin / "switchboard"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "args=$* url=$SWITCHBOARD_URL workspace=$SWITCHBOARD_WORKSPACE" >> {record}\n'
        'if [[ "$*" == *whoami* ]]; then\n'
        '  echo \'{"agent_id": "me"}\'\n'
        'elif [[ "$*" == *claims* ]]; then\n'
        '  echo \'[{"resource": "scratch"}]\'\n'
        "fi\n"
    )
    stub.chmod(0o755)

    proj = _project_with_hooks(tmp_path, "http://hub.internal", "acme/widgets")
    result = _run_hook(_stop_cmd("", ""), proj, fake_bin)
    assert result.returncode == 0, result.stderr
    lines = record.read_text().splitlines()
    assert len(lines) == 3, lines  # whoami, claims, and the nested release
    assert all("url=http://hub.internal workspace=acme/widgets" in ln for ln in lines)
    assert any("release" in ln and "scratch" in ln for ln in lines)


# --- revision tracking: auto-upgrade untouched output, leave hand-edits ----
#
# `init` used to leave a hook or CLAUDE.md section alone the instant it saw
# *anything* already there, so a repo that ran `init` before #32 was fixed
# stayed on the buggy hooks forever, even after upgrading the package and
# rerunning `init`. Now existing content that matches the current revision
# or any past one we ever generated (tracked in *_HISTORY, extracted from
# git history rather than retyped) is recognized as untouched machine output
# and upgraded automatically. Anything else is presumed hand-edited and left
# alone unless --force is passed.


def test_hooks_auto_upgrade_from_a_known_past_revision(monkeypatch, capsys, tmp_path):
    url, workspace = "https://hub.example.com", "acme/widgets"
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    old_start = _SESSION_START_CMD_HISTORY[-1](url, workspace)
    old_stop = _STOP_CMD_HISTORY[-1](url, workspace)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": old_start}]}],
            "Stop": [{"hooks": [{"type": "command", "command": old_stop}]}],
        }
    }))

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["-w", workspace, "--url", url, "init"])
    out = capsys.readouterr().out
    assert code == 0
    assert "updated the SessionStart hook" in out
    assert "updated the Stop hook" in out

    settings = json.loads(settings_path.read_text())
    assert settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] == _session_start_cmd(
        url, workspace
    )
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == _stop_cmd(url, workspace)


def test_hooks_left_alone_if_hand_edited(monkeypatch, capsys, tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "switchboard register # custom"}]}
            ],
        }
    }))
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert "doesn't match a known switchboard revision" in out
    settings = json.loads(settings_path.read_text())
    assert (
        settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        == "switchboard register # custom"
    )


def test_hooks_overwritten_with_force(monkeypatch, capsys, tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "switchboard register # custom"}]}
            ],
        }
    }))
    code, out = run_init(monkeypatch, capsys, tmp_path, "--force")
    assert code == 0
    assert "updated the SessionStart hook" in out
    settings = json.loads(settings_path.read_text())
    new_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert new_cmd != "switchboard register # custom"


def test_claude_md_section_auto_upgrades_from_a_known_past_revision(monkeypatch, capsys, tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# My project\n\nSome existing notes.\n\n" + _CLAUDE_MD_SECTION_HISTORY[0]
    )
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert "updated CLAUDE.md's coordination section" in out
    text = (tmp_path / "CLAUDE.md").read_text()
    assert text == "# My project\n\nSome existing notes.\n\n" + _CLAUDE_MD_SECTION
    assert "Some existing notes." in text


def test_claude_md_section_left_alone_if_hand_edited(monkeypatch, capsys, tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        f"# My project\n\n{_CLAUDE_MD_MARKER}\n\nWe do it our own way here.\n"
    )
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert "doesn't match a known switchboard revision" in out
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "We do it our own way here." in text


def test_claude_md_section_overwritten_with_force(monkeypatch, capsys, tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        f"# My project\n\n{_CLAUDE_MD_MARKER}\n\nWe do it our own way here.\n"
    )
    code, out = run_init(monkeypatch, capsys, tmp_path, "--force")
    assert code == 0
    assert "updated CLAUDE.md's coordination section" in out
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "We do it our own way here." not in text
    assert text == "# My project\n\n" + _CLAUDE_MD_SECTION


def test_claude_md_section_already_up_to_date_is_left_alone(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path)
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert "left CLAUDE.md alone: coordination section is already up to date" in out


def test_appends_to_existing_claude_md(monkeypatch, capsys, tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My project\n\nSome existing notes.\n")
    run_init(monkeypatch, capsys, tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "Some existing notes." in text
    assert "## Coordinating with other agents" in text


def test_malformed_mcp_json_is_left_alone(monkeypatch, capsys, tmp_path):
    (tmp_path / ".mcp.json").write_text("{not valid json")
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert (tmp_path / ".mcp.json").read_text() == "{not valid json"
    assert "not valid JSON" in out


def test_skip_flags(monkeypatch, capsys, tmp_path):
    run_init(
        monkeypatch, capsys, tmp_path,
        "--local", "--skip-mcp", "--skip-hooks", "--skip-claude-md", "--skip-skill",
    )
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".claude" / "skills").exists()
    assert (tmp_path / ".env").exists()


def test_skill_file_left_alone_if_hand_edited(monkeypatch, capsys, tmp_path):
    skill_path = tmp_path / ".claude" / "skills" / "switchboard-coordinate" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# hand-customized\n")
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert skill_path.read_text() == "# hand-customized\n"
    assert "doesn't match a known switchboard revision" in out
    assert "--force" in out


def test_skill_file_overwritten_with_force(monkeypatch, capsys, tmp_path):
    skill_path = tmp_path / ".claude" / "skills" / "switchboard-coordinate" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# hand-customized\n")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--force")
    assert code == 0
    assert skill_path.read_text() == _skill_source()
    assert "updated the switchboard-coordinate skill" in out


def test_skill_file_already_up_to_date_is_left_alone_without_force(
    monkeypatch, capsys, tmp_path
):
    run_init(monkeypatch, capsys, tmp_path)
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert "left" in out and "already up to date" in out


# A repo initialised by an older switchboard has an older SKILL.md. Until the
# revision history below was backfilled it matched no known revision, so
# `init` read it as hand-edited and refused to touch it — a repo could never
# pick up a skill revision without `--force`, which is the opposite of the
# intent. One case per recorded revision, so a history entry that has drifted
# from what `init` can actually recognise fails here. Note what this cannot
# check: a revision nobody recorded is invisible to a test parametrised over
# the recordings. Comparing against git history would catch it, but CI clones
# shallowly, so recording a superseded revision stays a review-time habit.


@pytest.mark.parametrize("revision", range(len(_skill_history())))
def test_every_past_skill_revision_upgrades_in_place(
    monkeypatch, capsys, tmp_path, revision
):
    skill_path = tmp_path / ".claude" / "skills" / "switchboard-coordinate" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(_skill_history()[revision])
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert skill_path.read_text() == _skill_source()
    assert "updated the switchboard-coordinate skill" in out


def test_skill_history_excludes_the_current_revision():
    # `_revision_status` checks the current revision first, so a duplicate
    # here would be harmless — but it would also mean the newest entry was
    # never actually superseded, i.e. someone recorded history too early.
    history = _skill_history()
    assert _skill_source() not in history
    assert len(set(history)) == len(history)


def test_workspace_inferred_from_git_remote(monkeypatch, capsys, tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/widgets.git"],
        cwd=tmp_path,
        check=True,
    )
    run_init(monkeypatch, capsys, tmp_path)
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert mcp["mcpServers"]["switchboard"]["env"]["SWITCHBOARD_WORKSPACE"] == "acme/widgets"


def test_explicit_workspace_and_url_override_inference(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["-w", "custom/ws", "--url", "https://hub.example.com", "init"])
    capsys.readouterr()
    assert code == 0
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    env = mcp["mcpServers"]["switchboard"]["env"]
    assert env["SWITCHBOARD_WORKSPACE"] == "custom/ws"
    assert env["SWITCHBOARD_URL"] == "https://hub.example.com"
    # a remote hub isn't a local dev instance, so no token file is generated
    assert not (tmp_path / ".env").exists()


def test_local_hub_warns_it_is_machine_only(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path, "--local")
    assert code == 0
    assert "only reachable from this machine" in out
    assert "docs/deployment.md" in out


def test_remote_hub_has_no_local_only_or_managed_warning(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["--url", "https://hub.example.com", "init"])
    out = capsys.readouterr().out
    assert code == 0
    assert "only reachable from this machine" not in out
    assert "shared public hub" not in out


def test_json_output_includes_local_hub_note(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["--json", "init", "--local"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "only reachable from this machine" in payload["note"]


def test_json_output(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["--json", "init", "--local"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["workspace"]
    assert payload["url"] == "http://127.0.0.1:8787"
    assert len(payload["steps"]) >= 3


@pytest.mark.parametrize("existing_token", ["from-env", "from-dot-env"])
def test_reuses_existing_token(monkeypatch, capsys, tmp_path, existing_token):
    if existing_token == "from-env":
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SWITCHBOARD_TOKEN", "env-token-value")
        code = main(["init", "--local"])
        capsys.readouterr()
        assert code == 0
        mcp_env = (tmp_path / ".env")
        assert not mcp_env.exists()
    else:
        (tmp_path / ".env").write_text("SWITCHBOARD_TOKEN=preexisting-token\n")
        run_init(monkeypatch, capsys, tmp_path, "--local")
        assert (tmp_path / ".env").read_text() == "SWITCHBOARD_TOKEN=preexisting-token\n"


# --- managed by default -------------------------------------------------


def test_bare_init_defaults_to_the_managed_hub(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert mcp["mcpServers"]["switchboard"]["env"]["SWITCHBOARD_URL"] == MANAGED_HUB_URL
    # No self-hosted dev token: nothing here can issue one for a hub it doesn't run.
    assert not (tmp_path / ".env").exists()
    assert "sealed with a key" in out, "init encrypts by default"
    assert "--local" in out


def test_managed_default_json_output(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["--json", "init"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["url"] == MANAGED_HUB_URL
    assert "sealed with a key" in payload["note"], "init encrypts by default"


def test_local_flag_and_explicit_url_are_mutually_exclusive(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    code = main(["--url", "https://hub.example.com", "init", "--local"])
    err = capsys.readouterr().err
    assert code != 0
    assert "mutually exclusive" in err


def test_local_flag_points_at_localhost_without_url_flag(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path, "--local")
    assert code == 0
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert mcp["mcpServers"]["switchboard"]["env"]["SWITCHBOARD_URL"] == "http://127.0.0.1:8787"


# --- keeping the generated CLAUDE.md in sync with its own documentation -----
#
# `_CLAUDE_MD_SECTION` is what `init` actually writes into a repo;
# docs/claude-code.md's fenced example is what a human reads to "understand
# or hand-customize" it. Nothing keeps them in sync automatically, and they
# have drifted apart silently before (found while investigating an unrelated
# question) — assert equality directly so a change to one that forgets the
# other fails CI instead of shipping a stale generator or a misleading doc.


def _markdown_fence(doc_path: Path) -> str:
    text = doc_path.read_text()
    match = re.search(r"```markdown\n(.*?)\n```", text, re.S)
    assert match, f"no ```markdown fence found in {doc_path}"
    return match.group(1) + "\n"


def _body_after_intro(section: str) -> str:
    """Everything from the first bullet onward, skipping the marker line and
    intro paragraph — the one place Claude Code's and Codex's wording is
    legitimately allowed to differ ('Claude sessions' vs generic 'agents')."""
    lines = section.splitlines()
    first_bullet = next(i for i, line in enumerate(lines) if line.startswith("- **"))
    return "\n".join(lines[first_bullet:])


def test_claude_md_section_matches_its_own_documentation():
    documented = _markdown_fence(REPO_ROOT / "docs" / "claude-code.md")
    assert documented == _CLAUDE_MD_SECTION


def test_claude_and_codex_coordination_snippets_stay_in_sync():
    claude_doc = _markdown_fence(REPO_ROOT / "docs" / "claude-code.md")
    codex_doc = _markdown_fence(REPO_ROOT / "docs" / "codex-cli.md")
    assert _body_after_intro(claude_doc) == _body_after_intro(codex_doc)


# --- the coordination skill: one packaged file, several places link to it ---
#
# README.md, demo/README.md, docs/why-this-exists.md and both integration
# docs all link straight at the file in the repo instead of a doc page that
# paraphrases it — the same single-source-of-truth fix as the CLAUDE.md/Codex
# sync above, applied one level further so there is only ever one copy of
# this protocol to drift.


def test_skill_file_exists_at_the_path_docs_link_to():
    assert (
        REPO_ROOT / "src" / "switchboard" / "skill" / "switchboard-coordinate" / "SKILL.md"
    ).is_file()


def test_skill_source_reads_the_packaged_file():
    on_disk = (
        REPO_ROOT / "src" / "switchboard" / "skill" / "switchboard-coordinate" / "SKILL.md"
    ).read_text()
    assert _skill_source() == on_disk


def test_init_installs_the_wake_listener(monkeypatch, capsys, tmp_path):
    """The listener ships with `init` for the same reason the hooks do: an
    agent told to arm it by CLAUDE.md and the skill needs the file to be
    there, in every clone, without anyone copying it out of the docs."""
    run_init(monkeypatch, capsys, tmp_path)
    path = tmp_path / _WAKE_REL
    assert path.exists(), "init did not install the listener"
    assert os.access(path, os.X_OK)
    body = path.read_text()
    # The wrapper is the point: a background shell shares none of .mcp.json's
    # env, and a listener on the wrong hub or workspace waits quietly forever.
    # That is also all it is now — the listener itself is `switchboard listen`,
    # so what `init` installs is a way to arm one with a path and no knowledge
    # of which room this repo is in.
    assert "export SWITCHBOARD_URL=" in body
    assert "export SWITCHBOARD_WORKSPACE=" in body
    assert "sb listen" in body
    # Not `exec`: `sb` is a shell function, and exec needs a real command.
    assert "exec sb" not in body


def test_wake_script_reads_the_packaged_body():
    """One copy of the shell, for the same reason there is one copy of the
    skill: a literal in cli.py would drift from the file people read."""
    on_disk = (
        REPO_ROOT / "src" / "switchboard" / "scripts" / "wake-on-message.sh"
    ).read_text()
    assert on_disk in _wake_script("https://hub.example.com", "ws")


# --- workspace keys ---------------------------------------------------------
#
# The key is the one secret `init` handles, and the file it lands in is *not*
# the one that gets committed — that asymmetry is what these tests pin down.


def _local_settings(tmp_path):
    return json.loads((tmp_path / ".claude" / "settings.local.json").read_text())


def test_new_key_writes_key_and_opaque_workspace(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    assert code == 0
    key = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    assert key
    # printed once so it can be shared; nothing reads it back out later
    assert key in out
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    workspace = mcp["mcpServers"]["switchboard"]["env"]["SWITCHBOARD_WORKSPACE"]
    assert workspace.startswith("w_")
    # the workspace is the one thing the hub sees in the clear, so a fresh key
    # comes with a name that says nothing about the repo
    assert tmp_path.name not in workspace


def test_key_never_reaches_the_committed_files(monkeypatch, capsys, tmp_path):
    code, _ = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY", "-w", "w_shared")
    assert code == 0
    assert "TEAMKEY" not in (tmp_path / ".mcp.json").read_text()
    assert "TEAMKEY" not in (tmp_path / ".claude" / "settings.json").read_text()
    assert "TEAMKEY" not in (tmp_path / "CLAUDE.md").read_text()
    assert _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"] == "TEAMKEY"


def test_key_file_is_gitignored(monkeypatch, capsys, tmp_path):
    # Claude Code only auto-ignores this file when it writes it itself, so
    # `init` has to place the entry or we hand the user a secret to commit.
    code, _ = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    assert code == 0
    assert "**/.claude/settings.local.json" in (tmp_path / ".gitignore").read_text()


@pytest.mark.parametrize("existing", ["**/.claude/settings.local.json", ".claude/"])
def test_gitignore_not_duplicated_when_already_covered(
    monkeypatch, capsys, tmp_path, existing
):
    (tmp_path / ".gitignore").write_text(f"{existing}\n")
    run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    assert (tmp_path / ".gitignore").read_text() == f"{existing}\n"


def test_gitignore_append_keeps_existing_entries(monkeypatch, capsys, tmp_path):
    (tmp_path / ".gitignore").write_text("*.pyc")  # no trailing newline
    run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines == ["*.pyc", "**/.claude/settings.local.json"]


def test_same_key_again_is_a_noop(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    assert code == 0
    assert "already set" in out


def test_a_different_key_is_refused_without_force(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--key", "OTHERKEY")
    # Silently swapping would seal this agent away from everyone still on the
    # old key, with nothing anywhere reporting a problem.
    assert code == 1
    assert _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"] == "TEAMKEY"
    assert "--force" in out


def test_force_replaces_an_existing_key(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    code, _ = run_init(monkeypatch, capsys, tmp_path, "--key", "OTHERKEY", "--force")
    assert code == 0
    assert _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"] == "OTHERKEY"


def test_key_preserves_unrelated_local_settings(monkeypatch, capsys, tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"env": {"OTHER": "1"}, "permissions": {"allow": ["Bash"]}}))
    run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    data = _local_settings(tmp_path)
    assert data["env"] == {"OTHER": "1", "SWITCHBOARD_KEY": "TEAMKEY"}
    assert data["permissions"] == {"allow": ["Bash"]}


def test_malformed_local_settings_left_alone(monkeypatch, capsys, tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    assert code == 1
    assert path.read_text() == "{not json"
    assert "not valid JSON" in out


def test_new_key_and_key_are_mutually_exclusive(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_KEY", raising=False)
    code = main(["init", "--key", "K", "--new-key"])
    assert code == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_key_from_the_environment_is_adopted(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    monkeypatch.setenv("SWITCHBOARD_KEY", "FROMENV")
    assert main(["init"]) == 0
    capsys.readouterr()
    assert _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"] == "FROMENV"


def test_adopting_a_key_warns_when_the_workspace_was_only_inferred(
    monkeypatch, capsys, tmp_path
):
    # Right key, wrong room is the quiet failure: sealing succeeds, routing
    # doesn't, and neither agent sees an error — only silence.
    code, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    assert code == 0
    assert "workspace defaulted to" in out


def test_no_warning_when_the_workspace_is_explicit(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY", "-w", "w_shared")
    assert code == 0
    assert "workspace defaulted to" not in out


def test_the_plaintext_note_says_who_can_actually_read_you(monkeypatch, capsys, tmp_path):
    # Reachable only via --no-key now. It used to claim every other user of
    # the managed hub could read the workspace, which was true of a
    # shared-token deployment and is false of this one — a token is bound to
    # one workspace, so a stranger gets a 401. The operator is the real
    # exposure, and overstating the rest teaches people to distrust the wrong
    # thing.
    _, plain = run_init(monkeypatch, capsys, tmp_path, "--no-key")
    assert "not encrypted" in plain
    assert "other users cannot read yours" in plain
    assert "operator" in plain
    assert "can read and post" not in plain

    _, sealed = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY", "-w", "w_shared")
    assert "sealed with a key" in sealed
    # a key hides content, never metadata — saying otherwise would be the
    # kind of overclaim that gets someone to put real secrets on a public hub
    assert "metadata" in sealed or "timing, volume" in sealed


def test_json_output_includes_a_minted_key(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    monkeypatch.delenv("SWITCHBOARD_KEY", raising=False)
    code = main(["init", "--new-key", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["key"] == _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]


def test_no_key_means_no_local_settings_file(monkeypatch, capsys, tmp_path):
    # Now an opt-out rather than the default.
    run_init(monkeypatch, capsys, tmp_path, "--no-key")
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / ".gitignore").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["init", "-w", "w_shared", "--json"],          # after the subcommand
        ["-w", "w_shared", "--json", "init"],          # before it
        ["-w", "w_shared", "init", "--json"],          # split across both
    ],
)
def test_connection_flags_accepted_on_either_side_of_the_subcommand(
    monkeypatch, capsys, tmp_path, argv
):
    # init's own output tells you to run `switchboard init --key K -w ws`, so
    # the trailing form has to work rather than erroring on unrecognized args.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    monkeypatch.delenv("SWITCHBOARD_KEY", raising=False)
    code = main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["workspace"] == "w_shared"


# --- key/workspace agreement and the prompts that guard it -------------------
#
# A key seals what leaves the machine; the workspace decides who it is sealed
# *with*. When those two disagree nothing errors — both sides encrypt happily
# and simply never see each other. That silence is what these tests are for.


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _write_mcp(tmp_path, workspace, url=MANAGED_HUB_URL):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "switchboard": {
                "command": "switchboard-mcp",
                "env": {"SWITCHBOARD_URL": url, "SWITCHBOARD_WORKSPACE": workspace},
            }
        }
    }, indent=2) + "\n")


def _mcp_ws(tmp_path):
    data = json.loads((tmp_path / ".mcp.json").read_text())
    return data["mcpServers"]["switchboard"]["env"]["SWITCHBOARD_WORKSPACE"]


def test_new_key_pairs_with_the_already_registered_workspace(monkeypatch, capsys, tmp_path):
    # The regression: init used to skip an existing .mcp.json but still mint an
    # opaque workspace, then print `--key K -w w_new` while the repo itself
    # kept routing to the old name. The key went one way, the agents another.
    _write_mcp(tmp_path, "acme/billing")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    assert code == 0
    assert _mcp_ws(tmp_path) == "acme/billing"
    assert "acme/billing" in out
    # whatever it tells you to hand teammates has to match where the repo routes
    minted = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    assert f"--key {minted} -w acme/billing" in out
    assert not re.search(r"-w w_[A-Za-z0-9_-]{16}", out)


def test_force_repoints_mcp_json_at_the_new_workspace(monkeypatch, capsys, tmp_path):
    _write_mcp(tmp_path, "acme/billing")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key", "--force")
    assert code == 0
    assert _mcp_ws(tmp_path).startswith("w_")
    assert "repointed .mcp.json" in out
    minted = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    assert f"--key {minted} -w {_mcp_ws(tmp_path)}" in out


def test_explicit_workspace_conflicting_with_mcp_json_is_reported(monkeypatch, capsys, tmp_path):
    # Here there *is* an intent to honour, and it disagrees with the file.
    # Silently picking either one strands half the setup, so say so instead.
    _write_mcp(tmp_path, "acme/billing")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY", "-w", "w_other")
    assert code == 0
    assert _mcp_ws(tmp_path) == "acme/billing"
    assert "note:" in out and "w_other" in out and "--force" in out


def test_matching_workspace_is_not_a_conflict(monkeypatch, capsys, tmp_path):
    _write_mcp(tmp_path, "w_shared")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY", "-w", "w_shared")
    assert code == 0
    assert "note:" not in out
    assert _mcp_ws(tmp_path) == "w_shared"


def test_no_key_leaves_a_differing_workspace_alone(monkeypatch, capsys, tmp_path):
    # Without a key there is nothing to pair, so an already-configured repo is
    # just an already-configured repo — the old behaviour, unchanged.
    _write_mcp(tmp_path, "acme/billing")
    code, out = run_init(monkeypatch, capsys, tmp_path, "-w", "w_other")
    assert code == 0
    assert _mcp_ws(tmp_path) == "acme/billing"
    # the workspace is what must not move; on the managed hub the file is still
    # touched, to add the token an existing repo would otherwise never get
    assert "left the rest alone" in out or "left .mcp.json alone" in out


# --- prompting ---------------------------------------------------------------
#
# `init` asks before it does anything it cannot take back, but only when there
# is a human to answer. Every other caller — an agent's shell, CI, a piped
# docs command — has to reach the same defaults it always did, silently.


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def make_interactive(monkeypatch, answers):
    """Pretend a human is at the keyboard, with `answers` queued up."""
    err = _FakeTTY()
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    remaining = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a: next(remaining))
    return err


def test_no_tty_means_no_prompt(monkeypatch, capsys, tmp_path):
    # pytest's streams are not TTYs, which is the same shape as an agent's
    # shell or a pipe. If this ever prompts, it hangs instead of failing.
    _write_mcp(tmp_path, "acme/billing")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    assert code == 0
    assert "pick 1-" not in out and "overwrite it" not in out


@pytest.mark.parametrize("flag", ["--no-input", "-q", "--json"])
def test_flags_opt_out_of_prompting_even_on_a_tty(monkeypatch, capsys, tmp_path, flag):
    monkeypatch.chdir(tmp_path)
    _write_mcp(tmp_path, "acme/billing")
    err = make_interactive(monkeypatch, [])  # any prompt would StopIteration
    code = main(["init", "--new-key", flag])
    assert code == 0
    assert err.getvalue() == ""
    assert _mcp_ws(tmp_path) == "acme/billing"


def test_ci_env_suppresses_prompts(monkeypatch, capsys, tmp_path):
    # Some runners allocate a pseudo-TTY; without this guard the job hangs.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    _write_mcp(tmp_path, "acme/billing")
    err = make_interactive(monkeypatch, [])
    code = main(["init", "--new-key"])
    assert code == 0
    assert err.getvalue() == ""


def test_interactive_can_repoint_the_workspace(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_mcp(tmp_path, "acme/billing")
    err = make_interactive(monkeypatch, ["2"])  # switch to the freshly minted workspace
    code = main(["init", "--new-key"])
    assert code == 0
    assert "acme/billing" in err.getvalue()
    assert _mcp_ws(tmp_path).startswith("w_")


def test_interactive_defaults_to_keeping_the_registered_workspace(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_mcp(tmp_path, "acme/billing")
    make_interactive(monkeypatch, [""])  # bare enter takes the default
    code = main(["init", "--new-key"])
    assert code == 0
    assert _mcp_ws(tmp_path) == "acme/billing"


def _hand_edited_hooks(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "switchboard mine --x"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "switchboard mine --y"}]}],
        }
    }))
    return settings_path


def test_interactive_offers_to_overwrite_a_hand_edited_hook(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings_path = _hand_edited_hooks(tmp_path)
    err = make_interactive(monkeypatch, ["y", "y", "y", "y"])
    code = main(["init"])
    out = capsys.readouterr().out
    assert code == 0
    assert "hand-edited" in err.getvalue()
    assert "as you confirmed" in out
    assert "switchboard mine" not in settings_path.read_text()


def test_interactive_declining_keeps_the_hand_edited_hook(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings_path = _hand_edited_hooks(tmp_path)
    make_interactive(monkeypatch, ["n", "n", "n", "n"])
    code = main(["init"])
    out = capsys.readouterr().out
    assert code == 0
    assert "switchboard mine --x" in settings_path.read_text()
    assert "left the SessionStart hook alone" in out


def test_declining_is_the_default_answer(monkeypatch, capsys, tmp_path):
    # Someone's own edit is the thing you keep when they just hit enter.
    monkeypatch.chdir(tmp_path)
    settings_path = _hand_edited_hooks(tmp_path)
    make_interactive(monkeypatch, ["", "", "", ""])
    assert main(["init"]) == 0
    assert "switchboard mine --x" in settings_path.read_text()


def test_minted_key_is_reported_as_saved_not_as_lost(monkeypatch, capsys, tmp_path):
    # The key is sitting in settings.local.json in the clear, so "copy it now,
    # it is not shown again" would be false — and the way it goes wrong is
    # expensive: someone believes it is gone, re-mints, and silently cuts
    # themselves off from everyone holding the old key.
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    assert code == 0
    assert "not shown again" not in out
    assert _LOCAL_SETTINGS_REL in out
    assert "whoami --show-key" in out
    # the claim that survives is the one about the hub, which is true
    assert "hub never receives it" in out


def test_prompt_survives_a_closed_stdin(monkeypatch, capsys, tmp_path):
    # EOF mid-prompt takes the default rather than crashing the run.
    monkeypatch.chdir(tmp_path)
    settings_path = _hand_edited_hooks(tmp_path)

    def eof(*a):
        raise EOFError

    monkeypatch.setattr(sys, "stderr", _FakeTTY())
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", eof)
    assert main(["init"]) == 0
    assert "switchboard mine --x" in settings_path.read_text()


# --- getting the key back ----------------------------------------------------
#
# The key lives in settings.local.json in the clear, so it was never actually
# unrecoverable — only unprinted. Making that explicit removes the incentive
# to re-mint, which is the one move that silently strands your teammates.


def test_show_key_reads_it_back_from_the_repo(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path, "--new-key")
    saved = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    assert main(["whoami", "--show-key"]) == 0
    assert capsys.readouterr().out.strip() == saved


def test_show_key_works_with_nothing_exported(monkeypatch, capsys, tmp_path):
    # The whole point: a plain shell has no SWITCHBOARD_KEY, and a plain shell
    # is where you stand when a teammate asks you for it.
    run_init(monkeypatch, capsys, tmp_path, "--new-key")
    saved = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    monkeypatch.delenv("SWITCHBOARD_KEY", raising=False)
    assert main(["--json", "whoami", "--show-key"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["key"] == saved
    assert payload["workspace"] == _mcp_ws(tmp_path)


def test_show_key_without_a_key_is_a_clean_error(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    code = main(["whoami", "--show-key"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "no key here" in captured.err


def test_whoami_does_not_claim_encryption_it_will_not_perform(monkeypatch, capsys, tmp_path):
    # A key on disk is not a key in this shell's environment. Claude Code
    # injects it for the agents it spawns; a bare `switchboard say` here would
    # send in the clear, and whoami must not say otherwise.
    run_init(monkeypatch, capsys, tmp_path, "--new-key")
    monkeypatch.delenv("SWITCHBOARD_KEY", raising=False)
    assert main(["--json", "whoami"]) == 0
    assert json.loads(capsys.readouterr().out)["encrypted"] is False

    monkeypatch.setenv("SWITCHBOARD_KEY", _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"])
    assert main(["--json", "whoami"]) == 0
    assert json.loads(capsys.readouterr().out)["encrypted"] is True


def test_show_key_does_not_leak_into_plain_whoami(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path, "--new-key")
    saved = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    assert main(["whoami"]) == 0
    assert saved not in capsys.readouterr().out


# --- switchboard owns the hook bodies; the agent's config only points ------
#
# Inlining the bodies into `.claude/settings.json` meant every upgrade was a
# guess about whether a string in a file with several authors was still ours.
# The body now lives in a file with exactly one author, and what is left in
# the agent's config is a constant shim.


def test_existing_inline_hooks_migrate_to_the_shim(monkeypatch, capsys, tmp_path):
    # The upgrade path that matters: a repo initialized before the split must
    # be recognized as untouched machine output and moved over, not read as a
    # hand edit and left on a revision that no longer has a script behind it.
    url, workspace = "https://hub.example.com", "acme/widgets"
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    old_start = f"{_hook_env_prefix(url, workspace)}switchboard -q register -c build || true"
    settings_path.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": old_start}]}]}
    }))

    monkeypatch.chdir(tmp_path)
    code = main(["-w", workspace, "--url", url, "init"])
    out = capsys.readouterr().out
    assert code == 0
    assert "updated the SessionStart hook" in out

    settings = json.loads(settings_path.read_text())
    cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cmd == _session_start_cmd(url, workspace)
    # and the script it now points at exists, or the hook is a dead reference
    assert (tmp_path / _HOOKS_DIR / "session-start.sh").exists()


def test_migrated_hooks_still_reach_the_right_hub(monkeypatch, capsys, tmp_path):
    # End-to-end after migration: the #32 guarantee has to survive the move,
    # not just the string comparison.
    url, workspace = "http://hub.internal", "acme/widgets"
    record = tmp_path / "seen.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    stub = fake_bin / "switchboard"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "url=$SWITCHBOARD_URL workspace=$SWITCHBOARD_WORKSPACE" >> {record}\n'
        "exit 0\n"
    )
    stub.chmod(0o755)

    proj = tmp_path / "repo"
    proj.mkdir()
    monkeypatch.chdir(proj)
    assert main(["-w", workspace, "--url", url, "init"]) == 0
    capsys.readouterr()

    settings = json.loads((proj / ".claude" / "settings.json").read_text())
    cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert _run_hook(cmd, proj, fake_bin).returncode == 0
    assert record.read_text() == f"url={url} workspace={workspace}\n"


def test_hook_scripts_are_idempotent(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path)
    first = (tmp_path / _HOOKS_DIR / "session-start.sh").read_text()
    _, out = run_init(monkeypatch, capsys, tmp_path)
    assert (tmp_path / _HOOKS_DIR / "session-start.sh").read_text() == first
    assert "already up to date" in out


def test_a_hand_edited_hook_script_is_left_alone(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path)
    script = tmp_path / _HOOKS_DIR / "session-start.sh"
    script.write_text(script.read_text() + "\necho mine\n")
    _, out = run_init(monkeypatch, capsys, tmp_path)
    assert "echo mine" in script.read_text()
    assert "looks hand-edited" in out


def test_force_overwrites_a_hand_edited_hook_script(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path)
    script = tmp_path / _HOOKS_DIR / "session-start.sh"
    script.write_text("echo mine\n")
    _, out = run_init(monkeypatch, capsys, tmp_path, "--force")
    assert "echo mine" not in script.read_text()
    assert "updated" in out or "overwrote" in out


def test_skip_hooks_writes_no_scripts(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path, "--skip-hooks")
    assert not (tmp_path / ".switchboard").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_the_scripts_are_not_secret_and_belong_in_git(monkeypatch, capsys, tmp_path):
    # They carry a URL and a workspace name, both non-secret by design, and a
    # clone needs them for its hooks to work at all. Nothing may gitignore them.
    run_init(monkeypatch, capsys, tmp_path, "--new-key")
    ignored = (tmp_path / ".gitignore").read_text()
    assert ".switchboard" not in ignored
    script = (tmp_path / _HOOKS_DIR / "session-start.sh").read_text()
    assert "SWITCHBOARD_KEY" not in script
    assert "SWITCHBOARD_TOKEN" not in script


def test_warns_when_the_hook_scripts_would_not_be_committed(monkeypatch, capsys, tmp_path):
    # The cost of splitting the bodies out: the shim is committed, so a clone
    # without the scripts gets hooks pointing at nothing — and `|| true` makes
    # that quiet. Cheap to check, invisible when wrong, so check every run.
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".switchboard/\n")
    _, out = run_init(monkeypatch, capsys, tmp_path)
    assert "note:" in out
    assert ".switchboard/hooks/ is gitignored" in out


def test_no_warning_when_the_scripts_are_committable(monkeypatch, capsys, tmp_path):
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    _, out = run_init(monkeypatch, capsys, tmp_path)
    assert "is gitignored" not in out


def test_no_warning_outside_a_git_repo(monkeypatch, capsys, tmp_path):
    # `git check-ignore` exits 128 here; that is not a reason to warn.
    _, out = run_init(monkeypatch, capsys, tmp_path)
    assert "is gitignored" not in out


def test_hook_scripts_are_executable(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path)
    assert os.access(tmp_path / _HOOKS_DIR / "session-start.sh", os.X_OK)


# --- adopting a key without -w is two different acts ------------------------


def test_adopting_a_key_in_a_repo_with_a_remote_reads_as_correct(monkeypatch, capsys, tmp_path):
    # The multi-repo flow: one key, a different workspace per repo. A note
    # that reads as a warning here trains people to ignore notes.
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "https://github.com/acme/widgets.git"],
        check=True, capture_output=True,
    )
    _, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    assert "'acme/widgets'" in out and "from its git remote" in out
    assert "adding another of your own repos" in out
    # the teammate case is still called out, just not as the headline
    assert "came from someone else" in out


def test_adopting_a_key_with_no_remote_warns_harder(monkeypatch, capsys, tmp_path):
    # Nothing else will ever derive this name, so silence would be wrong.
    _, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY")
    assert "no git remote" in out
    assert "no other agent will arrive at on its own" in out


def test_an_explicit_workspace_gets_no_note(monkeypatch, capsys, tmp_path):
    _, out = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY", "-w", "acme/shared")
    assert "note:" not in out


# --- --skip-mcp still reads where the repo routes ---------------------------


def test_skip_mcp_still_pairs_the_key_with_the_registered_workspace(
    monkeypatch, capsys, tmp_path
):
    # --skip-mcp means "do not write the file", not "pretend it says nothing".
    # The key/workspace pairing fails silently, so it has to be checked even
    # when we are not touching .mcp.json.
    _write_mcp(tmp_path, "acme/billing")
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-mcp")
    assert "acme/billing" in out
    assert _mcp_ws(tmp_path) == "acme/billing"  # untouched, as asked
    minted = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    assert f"--key {minted} -w acme/billing" in out


def test_skip_mcp_never_repoints_even_with_force(monkeypatch, capsys, tmp_path):
    # --force must not silently change the workspace we pair against while
    # leaving the file that actually routes untouched: that is the original
    # misroute bug wearing a different hat.
    _write_mcp(tmp_path, "acme/billing")
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-mcp", "--force")
    assert _mcp_ws(tmp_path) == "acme/billing"
    minted = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    assert f"--key {minted} -w acme/billing" in out


# --- making sure the workspace is actually reachable -------------------------
#
# The gap #52 and #53 both left open. `init --new-key` mints a workspace that
# is brand new by construction, so on a hub that scopes tokens to workspaces
# nothing is bound to it — every call 403s while `init` reports success and
# tells you to restart your editor. The managed hub runs exactly that mode, so
# it was the default path, not an edge case.


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def fake_hub(monkeypatch, *, self_issued=False, reachable=True, register=None,
             advertises=True):
    """Stand in for a hub.

    Nothing registers anything any more — a room identifier is derived from its
    token rather than claimed — so the only question `init` asks is whether the
    workspace is reachable.
    """
    import httpx

    probed = []

    def get(url, **kwargs):
        if url.endswith("/health"):
            return _Resp(200, {"ok": True, "version": "0.4.6", "auth": True})
        if url.endswith("/agents"):
            return _Resp(200 if reachable else 403, {"detail": "no access"})
        raise AssertionError(f"unexpected GET {url}")

    def post(url, **kwargs):
        assert url.endswith("/keys/register")
        if register is not None:
            register.append(kwargs["json"]["workspace"])
        return _Resp(200, {"workspace": kwargs["json"]["workspace"]})

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    return probed


def test_an_unreachable_hub_is_reported_but_never_fatal(monkeypatch, capsys, tmp_path):
    # conftest already refuses outbound HTTP, which is exactly this case.
    monkeypatch.setenv("SWITCHBOARD_TOKEN", "whatever")
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    assert code == 0, "init writes correct files with or without a reachable hub"
    assert (tmp_path / ".mcp.json").exists()


def test_skip_token_touches_the_network_at_all(monkeypatch, capsys, tmp_path):
    import httpx

    def explode(*a, **k):
        raise AssertionError("--skip-token must not contact the hub")

    monkeypatch.setattr(httpx, "get", explode)
    monkeypatch.setattr(httpx, "post", explode)
    code, _ = run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-token")
    assert code == 0


def test_local_hub_keeps_its_own_token_flow(monkeypatch, capsys, tmp_path):
    import httpx

    def explode(*a, **k):
        raise AssertionError("--local must not register against a remote hub")

    monkeypatch.setattr(httpx, "get", explode)
    monkeypatch.setattr(httpx, "post", explode)
    code, out = run_init(monkeypatch, capsys, tmp_path, "--local")
    assert code == 0
    assert "SWITCHBOARD_TOKEN=" in (tmp_path / ".env").read_text()


def test_init_explains_which_half_travels_with_the_repo(monkeypatch, capsys, tmp_path):
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    assert code == 0
    assert "Two halves" in out
    # the two moves people actually need next
    # the secrets are written per repo, so a second one needs the key even
    # on the same machine — and -w is not mentioned, since naming a flag only
    # to say "do not pass it" is noise in a six-line summary
    assert "switchboard init --key <key>" in out
    assert "-w" not in out.split("Two halves")[1]
    assert "whoami --env" in out


def test_the_explainer_stays_short(monkeypatch, capsys, tmp_path):
    # It earns its place by being read. The first version ran to ~25 lines,
    # which is long enough that skimming past it is the likely outcome — the
    # same as not printing it at all.
    #
    # The budget grew by exactly one destination, the cloud environment. Three
    # lines rather than one because its two fields are filled in two separate
    # boxes, and collapsing them into a single shell block gives the reader
    # something that cannot be pasted anywhere as-is.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    explainer = out[out.index("Two halves"):]
    assert len(explainer.strip().splitlines()) <= 9, explainer
    assert max(len(ln) for ln in explainer.splitlines()) <= 100, "must not wrap in a terminal"


def test_the_explainer_names_the_cloud_environment_init_cannot_reach(
    monkeypatch, capsys, tmp_path
):
    # The one destination `init` can do nothing about: a cloud environment is
    # configured outside every repo, so nothing written into a checkout gets
    # there. An environment missing the *package* is the quietest way to end
    # up alone — no tools, no hooks, and nothing that resembles a failure.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    explainer = out[out.index("Two halves"):]
    assert "a cloud env" in explainer


def test_the_cloud_steps_are_split_the_way_the_settings_are(monkeypatch, capsys, tmp_path):
    # A setup script and a list of environment variables are two different
    # fields, filled in separately. Emitting `pip install` and the secrets as
    # one block of shell produces something that cannot be pasted into either
    # box as-is, and the reader has to take it apart before using it.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    lines = out.splitlines()
    script = [ln for ln in lines if "setup script" in ln][0]
    env = [ln for ln in lines if "env vars" in ln][0]

    assert "pip install" in script
    # A container image is where a system `cryptography` lives, and pip cannot
    # replace a package it did not install — so the plain command exits 1, the
    # setup script fails, and the environment comes up with no switchboard and
    # nothing saying why. Reported from a real cloud environment.
    assert "--ignore-installed cryptography" in script
    # Not [all]: FastAPI and uvloop are for a machine running a hub, and every
    # extra wheel is another chance to collide with what the image ships.
    assert "[all]" not in script
    assert "SWITCHBOARD_" not in script, "the secrets do not belong in the setup script"
    assert "SWITCHBOARD_KEY" in env and "SWITCHBOARD_TOKEN" in env
    assert "pip install" not in env, "the install does not belong in the variables box"
    # Named, not printed: a key in scrollback or a screen share is what
    # `whoami --show-key` makes you ask for on purpose.
    assert "whoami --env" in env


def test_the_explainer_does_not_claim_a_token_it_did_not_store(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-token")
    assert code == 0
    assert "key + token" not in out


def test_a_local_hub_gets_no_environment_explainer(monkeypatch, capsys, tmp_path):
    # --local is one machine talking to itself; there is no second environment
    # to bring in, and the .env token flow already covers what it needs.
    code, out = run_init(monkeypatch, capsys, tmp_path, "--local")
    assert code == 0
    assert "Two halves" not in out


def test_the_explainer_is_quiet_under_json_and_q(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-token", "-q")
    assert code == 0
    assert out.strip() == ""


# --- handing the settings to another environment -----------------------------


def test_env_prints_only_the_secrets(monkeypatch, capsys, tmp_path):
    # The URL and workspace live in the committed .mcp.json. Setting them in
    # the environment too pins that machine to values it should be following,
    # so it keeps the old ones when the repo moves — the same silent
    # divergence as a mismatched key, arrived at by being helpful.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    run_init(monkeypatch, capsys, tmp_path, "--new-key")
    settings = _local_settings(tmp_path)["env"]
    monkeypatch.setenv("SWITCHBOARD_TOKEN", "T")

    assert main(["whoami", "--env"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    got = dict(ln.split("=", 1) for ln in lines)
    assert set(got) == {"SWITCHBOARD_KEY", "SWITCHBOARD_TOKEN"}
    assert got["SWITCHBOARD_KEY"] == settings["SWITCHBOARD_KEY"]
    assert all(re.fullmatch(r"SWITCHBOARD_[A-Z]+=\S+", ln) for ln in lines), lines


def test_no_repo_adds_what_nothing_else_would_supply(monkeypatch, capsys, tmp_path):
    # The documented exception: an environment with no checkout has no
    # .mcp.json to read the routing from, so it has to be told.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    run_init(monkeypatch, capsys, tmp_path, "--new-key")

    monkeypatch.setenv("SWITCHBOARD_TOKEN", "T")
    assert main(["whoami", "--env", "--no-repo"]) == 0
    got = dict(ln.split("=", 1) for ln in capsys.readouterr().out.strip().splitlines())
    assert set(got) == {
        "SWITCHBOARD_URL", "SWITCHBOARD_WORKSPACE", "SWITCHBOARD_KEY", "SWITCHBOARD_TOKEN",
    }
    assert got["SWITCHBOARD_WORKSPACE"] == _mcp_ws(tmp_path)


def test_env_omits_what_this_machine_does_not_know(monkeypatch, capsys, tmp_path):
    # A blank would silently override a correct value already set there.
    run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-token")
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    assert main(["whoami", "--env"]) == 0
    assert "SWITCHBOARD_TOKEN=" not in capsys.readouterr().out


def test_env_does_not_prompt_without_a_terminal(monkeypatch, capsys, tmp_path):
    # A pipe must get the block and nothing else — no prompt to hang on.
    run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-token")
    assert main(["whoami", "--env"]) == 0
    assert "clipboard" not in capsys.readouterr().out


def test_the_clipboard_offer_copies_when_accepted(monkeypatch, capsys, tmp_path):
    import switchboard.cli as cli

    copied = []
    monkeypatch.setattr(cli, "_copy_to_clipboard", lambda text: copied.append(text) or "fake")
    run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-token")
    make_interactive(monkeypatch, ["y"])
    assert main(["whoami", "--env"]) == 0
    assert copied and copied[0].startswith("SWITCHBOARD_KEY=")


def test_declining_the_clipboard_copies_nothing(monkeypatch, capsys, tmp_path):
    import switchboard.cli as cli

    copied = []
    monkeypatch.setattr(cli, "_copy_to_clipboard", lambda text: copied.append(text) or "fake")
    run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-token")
    make_interactive(monkeypatch, ["n"])
    assert main(["whoami", "--env"]) == 0
    assert copied == []


def test_no_clipboard_tool_is_not_an_error(monkeypatch, capsys, tmp_path):
    import switchboard.cli as cli

    monkeypatch.setattr(cli, "_copy_to_clipboard", lambda text: None)
    run_init(monkeypatch, capsys, tmp_path, "--new-key", "--skip-token")
    err = make_interactive(monkeypatch, ["y"])
    assert main(["whoami", "--env"]) == 0, "no clipboard over SSH is normal, not a failure"
    assert "no clipboard tool found" in err.getvalue()


def test_the_explainer_names_the_package_install(monkeypatch, capsys, tmp_path):
    # Secrets alone do not make a new environment work: without the package,
    # switchboard-mcp is not on PATH, the MCP server never starts, and the
    # session has no switchboard tools at all — with the secrets set correctly
    # the whole time. Two steps because it is genuinely two.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    machine_line = [ln for ln in out.splitlines() if "another machine" in ln][0]
    assert "pip install" in machine_line
    # [crypto] whenever a key is in play: cryptography is an optional extra,
    # and without it the MCP server raises CryptoError instead of connecting.
    assert "agent-switchboard[crypto]" in machine_line
    assert machine_line.index("pip install") < machine_line.index("whoami --env")


# --- what init says above the explainer --------------------------------------


def test_no_placeholder_token_survives(monkeypatch, capsys, tmp_path):
    # `export SWITCHBOARD_TOKEN=<token>` told you to fill in a value from a
    # place it never named. Where a token actually comes from is the useful
    # thing to say.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    assert "<token>" not in out


def test_the_sealed_note_no_longer_gives_setup_instructions(monkeypatch, capsys, tmp_path):
    # It used to say `init --key <key> -w <workspace>` and to set
    # SWITCHBOARD_WORKSPACE in a cloud environment — both of which pin a
    # machine to a workspace it should be reading from the committed
    # .mcp.json, and both contradicted the explainer below it.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    note = out.split("Note: ")[1].split("Next")[0]
    assert "sealed with a key" in note
    assert "-w " not in note
    assert "SWITCHBOARD_WORKSPACE" not in note


def test_the_teammate_line_still_pins_the_workspace(monkeypatch, capsys, tmp_path):
    # The one case where -w is right: a teammate must land in *your* room, so
    # theirs has to match. That is the opposite of your own second repo.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    _, out = run_init(monkeypatch, capsys, tmp_path, "--new-key")
    line = [ln for ln in out.splitlines() if "Give teammates" in ln][0]
    assert f"-w {_mcp_ws(tmp_path)}" in line


# --- encryption is the default -----------------------------------------------
#
# Plaintext-unless-you-knew-to-ask made a workspace's privacy depend on having
# read the right doc first. It also made `cryptography` feel optional, which is
# how the bare package came to raise CryptoError at startup for anyone who did
# take the advice and minted a key.


def test_a_plain_init_encrypts(monkeypatch, capsys, tmp_path):
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]
    assert "sealed with a key" in out


def test_a_minted_default_keeps_the_derived_workspace(monkeypatch, capsys, tmp_path):
    # --new-key swaps in an opaque name; minting by default must not. A derived
    # org/repo is what makes a laptop, a clone and CI agree for free, and
    # trading that away silently is not a privacy win anyone asked for here.
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "git@github.com:acme/api.git")
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    run_init(monkeypatch, capsys, tmp_path)
    assert _mcp_ws(tmp_path) == "acme/api"

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init")
    _git(other, "remote", "add", "origin", "git@github.com:acme/api.git")
    monkeypatch.chdir(other)
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    main(["init", "--new-key"])
    capsys.readouterr()
    assert _mcp_ws(other).startswith("w_")


def test_rerunning_init_does_not_mint_a_second_key(monkeypatch, capsys, tmp_path):
    # The failure this would cause is the one _init_key exists to refuse:
    # a repo whose agents hold two different keys and never meet.
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    run_init(monkeypatch, capsys, tmp_path)
    first = _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"]

    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"] == first
    assert "already set" in out


def test_an_ambient_key_is_adopted_not_replaced(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("SWITCHBOARD_KEY", "AMBIENT-KEY")
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    run_init(monkeypatch, capsys, tmp_path)
    assert _local_settings(tmp_path)["env"]["SWITCHBOARD_KEY"] == "AMBIENT-KEY"


def test_no_key_opts_out(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path, "--no-key")
    assert code == 0
    assert not (tmp_path / ".claude" / "settings.local.json").exists()
    assert "not encrypted" in out


def test_no_key_conflicts_are_refused(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--no-key", "--new-key"]) == 1
    assert main(["init", "--no-key", "--key", "K"]) == 1


def test_cryptography_is_not_optional():
    # It was an extra, so `pip install agent-switchboard` produced an install
    # that raises CryptoError the moment a key is present — which, now that
    # init mints one by default, is always.
    #
    # Reads the built distribution's metadata rather than pyproject: it is what
    # a user actually installs, and it works on 3.10, which has no tomllib.
    from importlib.metadata import requires

    reqs = requires("agent-switchboard") or []
    base = [r for r in reqs if "extra ==" not in r]
    assert any(r.startswith("cryptography") for r in base), base
    # the old extra must still resolve for anything pinning it
    from importlib.metadata import metadata

    assert "crypto" in (metadata("agent-switchboard").get_all("Provides-Extra") or [])


# --- setup that needs no prior install ---------------------------------------
#
# The step people forget is the package, and it fails in the worst way: the
# binary is simply not on PATH, so the MCP server never starts and the session
# has no switchboard tools at all, with the secrets set correctly throughout.


def test_the_committed_config_resolves_the_binary(monkeypatch, capsys, tmp_path):
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    run_init(monkeypatch, capsys, tmp_path, "--new-key")

    entry = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["switchboard"]
    script = entry["args"][1]
    # installed binary first: an environment that pinned a version meant it
    assert script.index("command -v switchboard-mcp") < script.index("uvx")
    assert "agent-switchboard[crypto]" in script
    # never mutates the environment from a file someone merely checked out:
    # pip may be *suggested* in the failure message, never executed
    assert "pip install" not in script.split("echo ")[0]
    assert "pip install" in script, "the failure should say what to do"


def test_the_hooks_resolve_it_too(monkeypatch, capsys, tmp_path):
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    run_init(monkeypatch, capsys, tmp_path, "--new-key")

    for name in ("session-start", "stop"):
        body = (tmp_path / ".switchboard" / "hooks" / f"{name}.sh").read_text()
        assert "command -v switchboard" in body, name
        # every call goes through the function; a bare invocation would bypass
        # the resolution on a machine without the binary
        assert "\nswitchboard " not in body, name


def test_no_bootstrap_writes_the_plain_command(monkeypatch, capsys, tmp_path):
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    run_init(monkeypatch, capsys, tmp_path, "--new-key", "--no-bootstrap")

    entry = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["switchboard"]
    assert entry["command"] == "switchboard-mcp"
    assert "args" not in entry
    body = (tmp_path / ".switchboard" / "hooks" / "stop.sh").read_text()
    assert "uvx" not in body


def test_a_repo_from_an_earlier_init_upgrades_rather_than_looking_edited(
    monkeypatch, capsys, tmp_path
):
    # The v1 scripts called `switchboard` directly. They must be recognized as
    # our own output and replaced, not read as a hand edit and left forever.
    from switchboard.cli import _SESSION_START_BODY_V1, _hook_script_v1

    hooks = tmp_path / ".switchboard" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "session-start.sh").write_text(
        _hook_script_v1(_SESSION_START_BODY_V1, MANAGED_HUB_URL, "acme/api")
    )
    fake_hub(monkeypatch, self_issued=True, reachable=False, register=[])
    _, out = run_init(monkeypatch, capsys, tmp_path, "-w", "acme/api", "--new-key")
    assert "latest revision" in out
    assert "command -v switchboard" in (hooks / "session-start.sh").read_text()


# --- the hub token is reachability, not a secret ------------------------------


def test_the_managed_hub_token_ships_with_the_url(monkeypatch, capsys, tmp_path):
    """Published on purpose: every client uses the same value, nothing issues
    it, and bundling it is what makes it transparent — nobody types it. It buys
    a 401 from a string compare instead of a database query for untargeted
    scanning. It is not a boundary and must not be described as one."""
    from switchboard.cli import MANAGED_HUB_TOKEN

    fake_hub(monkeypatch, reachable=True)
    run_init(monkeypatch, capsys, tmp_path)
    env = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["switchboard"]["env"]
    assert env["SWITCHBOARD_TOKEN"] == MANAGED_HUB_TOKEN


def test_no_token_is_written_for_any_other_hub(monkeypatch, capsys, tmp_path):
    # The committed file must never carry a secret. A hub that wants a real
    # perimeter uses a secret token, and that one lives in the environment.
    fake_hub(monkeypatch, reachable=True)
    run_init(monkeypatch, capsys, tmp_path, "--url", "https://hub.example.com")
    env = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["switchboard"]["env"]
    assert "SWITCHBOARD_TOKEN" not in env


def test_a_local_hub_keeps_its_own_token_out_of_the_committed_file(
    monkeypatch, capsys, tmp_path
):
    run_init(monkeypatch, capsys, tmp_path, "--local")
    env = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["switchboard"]["env"]
    assert "SWITCHBOARD_TOKEN" not in env, "a generated dev token is a secret"


def test_an_existing_repo_gets_the_token_it_never_had(monkeypatch, capsys, tmp_path):
    """Leaving an entry alone must not mean leaving it broken.

    A repo set up before the hub required a token has none, and the hub now
    refuses it — so `init` adds the published constant in place while leaving
    everything the user may have customised exactly as it was.
    """
    from switchboard.cli import MANAGED_HUB_TOKEN

    _write_mcp(tmp_path, "w_existing")
    fake_hub(monkeypatch, reachable=True)
    _, out = run_init(monkeypatch, capsys, tmp_path)

    env = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["switchboard"]["env"]
    assert env["SWITCHBOARD_TOKEN"] == MANAGED_HUB_TOKEN
    assert env["SWITCHBOARD_WORKSPACE"] == "w_existing", "the workspace must not move"
    assert "added the hub's public token" in out


def test_a_token_already_there_is_not_overwritten(monkeypatch, capsys, tmp_path):
    # Someone may have set a real one by hand; replacing it would lock them out.
    import json as _json

    _write_mcp(tmp_path, "w_existing")
    path = tmp_path / ".mcp.json"
    data = _json.loads(path.read_text())
    data["mcpServers"]["switchboard"]["env"]["SWITCHBOARD_TOKEN"] = "mine"
    path.write_text(_json.dumps(data))

    fake_hub(monkeypatch, reachable=True)
    run_init(monkeypatch, capsys, tmp_path)
    env = _json.loads(path.read_text())["mcpServers"]["switchboard"]["env"]
    assert env["SWITCHBOARD_TOKEN"] == "mine"


def test_the_next_step_does_not_point_at_a_command_that_cannot_help(
    monkeypatch, capsys, tmp_path
):
    # Registration is gone; telling someone to run `init` to obtain a token
    # sends them to a command that will never produce one.
    fake_hub(monkeypatch, reachable=True)
    _, out = run_init(monkeypatch, capsys, tmp_path, "--url", "https://hub.example.com")
    assert "registers one against a hub" not in out


# --- a worktree that would land in a different room -------------------------
#
# A worktree is a separate checkout that deliberately shares the main
# checkout's room: `_git_common_dir` follows the pointer so both derive one
# remote. That holds while both sides *derive*, and stops the moment one of
# them pins a workspace the derivation no longer produces — a legacy value, a
# `-w` passed once. The two checkouts then sit one directory apart in rooms
# that cannot see each other, and nothing said so.


def _fake_worktree(tmp_path, pinned: str | None):
    """A main checkout with a pinned .mcp.json, plus a linked worktree of it.

    Built by hand rather than with `git worktree add` so the test needs no git
    binary: what the code reads is the `.git` file's `gitdir:` pointer and the
    common dir it names, which is exactly what git writes.
    """
    main_co = tmp_path / "main"
    (main_co / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main_co / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/acme/widgets.git\n'
    )
    if pinned is not None:
        (main_co / ".mcp.json").write_text(json.dumps({"mcpServers": {"switchboard": {
            "env": {"SWITCHBOARD_WORKSPACE": pinned}}}}))
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main_co / '.git' / 'worktrees' / 'wt'}\n")
    (main_co / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
    return main_co, wt


def test_a_worktree_is_warned_when_its_room_would_differ(monkeypatch, capsys, tmp_path):
    _, wt = _fake_worktree(tmp_path, pinned="w_LegacyPinnedValue")
    code, out = run_init(monkeypatch, capsys, wt, "--local")
    assert code == 0
    assert "WARNING" in out
    assert "w_LegacyPinnedValue" in out
    # The remedy, not just the diagnosis — an agent told only that something is
    # wrong does the same thing as one told nothing.
    assert "-w w_LegacyPinnedValue" in out


def test_no_warning_when_the_worktree_agrees(monkeypatch, capsys, tmp_path):
    """The derived case, which is the normal one: both sides compute
    `acme/widgets` from the shared remote and belong in one room."""
    _, wt = _fake_worktree(tmp_path, pinned="acme/widgets")
    _, out = run_init(monkeypatch, capsys, wt, "--local")
    assert "WARNING" not in out


def test_no_warning_when_the_main_checkout_pins_nothing(monkeypatch, capsys, tmp_path):
    _, wt = _fake_worktree(tmp_path, pinned=None)
    _, out = run_init(monkeypatch, capsys, wt, "--local")
    assert "WARNING" not in out


def test_a_plain_checkout_has_no_sibling_to_disagree_with(monkeypatch, capsys, tmp_path):
    """From the main checkout there is nothing to compare against, and
    comparing a file with itself would warn on every ordinary repo."""
    from switchboard.config import main_checkout

    main_co, _ = _fake_worktree(tmp_path, pinned="w_LegacyPinnedValue")
    assert main_checkout(main_co) is None
    _, out = run_init(monkeypatch, capsys, main_co, "--local")
    assert "WARNING" not in out
