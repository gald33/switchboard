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
    _SESSION_START_CMD_HISTORY,
    _STOP_CMD_HISTORY,
    MANAGED_HUB_URL,
    _session_start_cmd,
    _skill_source,
    _stop_cmd,
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
    assert mcp["mcpServers"]["switchboard"]["command"] == "switchboard-mcp"
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


# --- #32: hooks must not depend on ambient SWITCHBOARD_URL/WORKSPACE -------
#
# SessionStart/Stop hooks run as plain shell commands, not inside the
# switchboard-mcp subprocess `.mcp.json`'s `env` block reaches — a cloud
# session can have SWITCHBOARD_TOKEN ambient with no URL or workspace at
# all, and the hook would silently register against 127.0.0.1:8787/default
# instead, guarded by `|| true` into total silence. `init` knows both
# values at generation time and neither is secret, so they get baked in.


def test_hook_commands_embed_the_resolved_url_and_workspace():
    start = _session_start_cmd("https://hub.example.com", "acme/widgets")
    stop = _stop_cmd("https://hub.example.com", "acme/widgets")
    for cmd in (start, stop):
        assert cmd.startswith(
            "export SWITCHBOARD_URL=https://hub.example.com; "
            "export SWITCHBOARD_WORKSPACE=acme/widgets; "
        )


def test_hook_commands_shell_quote_a_workspace_with_special_characters():
    # A workspace name is usually `org/repo`, but nothing stops it from
    # containing something shell-unsafe if a user overrides it by hand.
    start = _session_start_cmd("https://hub.example.com", "it's a workspace")
    assert "export SWITCHBOARD_WORKSPACE='it'\"'\"'s a workspace'; " in start


def test_init_bakes_the_resolved_hub_into_the_written_hooks(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["-w", "acme/widgets", "--url", "https://hub.example.com", "init"])
    capsys.readouterr()
    assert code == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    start_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    stop_cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "SWITCHBOARD_URL=https://hub.example.com" in start_cmd
    assert "SWITCHBOARD_WORKSPACE=acme/widgets" in stop_cmd


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

    cmd = _session_start_cmd("http://hub.internal", "acme/widgets")
    result = subprocess.run(
        ["bash", "-c", cmd],
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "SWITCHBOARD_TOKEN": "shh"},
        capture_output=True,
        text=True,
    )
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

    cmd = _stop_cmd("http://hub.internal", "acme/widgets")
    result = subprocess.run(
        ["bash", "-c", cmd],
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "SWITCHBOARD_TOKEN": "shh"},
        capture_output=True,
        text=True,
    )
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
    assert "shared public hub" in out
    assert "one token everyone uses" in out
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
    assert "shared public hub" in payload["note"]


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


def test_managed_hub_note_reflects_that_a_key_makes_it_private(
    monkeypatch, capsys, tmp_path
):
    _, public = run_init(monkeypatch, capsys, tmp_path)
    assert "every other" in public and "can read and post" in public
    _, sealed = run_init(monkeypatch, capsys, tmp_path, "--key", "TEAMKEY", "-w", "w_shared")
    assert "sealed with a key" in sealed
    assert "can read and post" not in sealed
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
    run_init(monkeypatch, capsys, tmp_path)
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
    assert "left .mcp.json alone" in out


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
    err = make_interactive(monkeypatch, ["2", ""])  # switch workspace, then ack the key
    code = main(["init", "--new-key"])
    assert code == 0
    assert "acme/billing" in err.getvalue()
    assert _mcp_ws(tmp_path).startswith("w_")


def test_interactive_defaults_to_keeping_the_registered_workspace(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_mcp(tmp_path, "acme/billing")
    make_interactive(monkeypatch, ["", ""])  # bare enter takes the default
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


def test_interactive_pauses_after_printing_a_minted_key(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    err = make_interactive(monkeypatch, [""])
    code = main(["init", "--new-key"])
    assert code == 0
    assert "saved it" in err.getvalue()


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
