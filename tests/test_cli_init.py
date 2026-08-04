"""Tests for `switchboard init` — the one-shot repo setup command.

Every writer it drives (`.mcp.json`, `.claude/settings.json`, `CLAUDE.md`,
`.env`) has to merge into whatever is already there and be safe to run twice,
so that is most of what these tests check.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from switchboard.cli import main


def run_init(monkeypatch, capsys, tmp_path, *extra_args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["init", *extra_args])
    out = capsys.readouterr().out
    return code, out


def test_fresh_repo_writes_everything(monkeypatch, capsys, tmp_path):
    code, out = run_init(monkeypatch, capsys, tmp_path)
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

    env = (tmp_path / ".env").read_text()
    assert "SWITCHBOARD_TOKEN=" in env
    token_line = [ln for ln in env.splitlines() if ln.startswith("SWITCHBOARD_TOKEN=")][0]
    assert len(token_line.split("=", 1)[1]) > 20

    assert "Next" in out


def test_idempotent(monkeypatch, capsys, tmp_path):
    run_init(monkeypatch, capsys, tmp_path)
    first_token = (tmp_path / ".env").read_text()

    code, out = run_init(monkeypatch, capsys, tmp_path)
    assert code == 0
    assert (tmp_path / ".env").read_text() == first_token

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["SessionStart"]) == 1
    assert len(settings["hooks"]["Stop"]) == 1

    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert list(mcp["mcpServers"].keys()) == ["switchboard"]

    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert claude_md.count("## Coordinating with other agents") == 1

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
    run_init(monkeypatch, capsys, tmp_path, "--skip-mcp", "--skip-hooks", "--skip-claude-md")
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".env").exists()


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


def test_json_output(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
    monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    code = main(["--json", "init"])
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
        code = main(["init"])
        capsys.readouterr()
        assert code == 0
        mcp_env = (tmp_path / ".env")
        assert not mcp_env.exists()
    else:
        (tmp_path / ".env").write_text("SWITCHBOARD_TOKEN=preexisting-token\n")
        run_init(monkeypatch, capsys, tmp_path)
        assert (tmp_path / ".env").read_text() == "SWITCHBOARD_TOKEN=preexisting-token\n"
