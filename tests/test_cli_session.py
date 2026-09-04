"""`switchboard session ...`: the handoff, driven the way a script or a human would.

Everything the module tests prove is proved again here through `main()`,
because the CLI is the surface a hook, a parked loop and a person actually
touch: the exit codes a loop branches on, the sentences a person reads, the
resume allowlist that keeps an unattended receiver from running a stranger's
transcript, and the one file that comes out of `export` and goes into
`import` on a machine that has no hub at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from switchboard import claude_session as cs
from switchboard.cli import main
from switchboard.crypto import generate_key
from switchboard.testing import BASE_URL, hub

WS = "cli-session-ws"
SID = "5d2c1a0e-7f3b-4c9d-8e1a-6b2f4d9c0a11"
OTHER = "0c7e9f2a-3b4d-4e5f-9a6b-1c2d3e4f5a66"


def _record(sid=SID, **fields):
    base = {"type": "user", "sessionId": sid, "cwd": "/Users/gal/code/switchboard",
            "version": "2.1.260", "gitBranch": "main"}
    base.update(fields)
    return json.dumps(base)


def _session(cfg: Path, cwd: str, sid: str = SID) -> Path:
    project = cfg / "projects" / cs.project_key(cwd)
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / f"{sid}.jsonl"
    transcript.write_text(_record(sid) + "\n" + _record(sid, type="assistant") + "\n")
    return transcript


@pytest.fixture
def sender_cfg(tmp_path, monkeypatch):
    """The sender's Claude config dir, which the CLI reads from the environment."""
    cfg = tmp_path / "sender-claude"
    _session(cfg, "/Users/gal/code/switchboard")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    return cfg


@pytest.fixture
def room(monkeypatch):
    import switchboard.cli as cli_module

    key = generate_key()
    with hub(workspace=WS, key=key) as handle:
        monkeypatch.setattr(cli_module, "Client", handle.client_class())
        monkeypatch.setenv("SWITCHBOARD_KEY", key)
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        yield handle


def _run(capsys, *argv, agent="cli-agent"):
    code = main(["--url", BASE_URL, "-w", WS, "--agent-id", agent, "--json", *argv])
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


# --- no hub at all: export and import are just files ------------------------

def test_export_then_import_on_another_machine(sender_cfg, tmp_path, capsys, monkeypatch):
    path = tmp_path / "cap.json"
    assert main(["session", "export", "-o", str(path)]) == 0
    assert "exported" in capsys.readouterr().out
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    other = tmp_path / "other-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert main(["--json", "session", "import", str(path), "--cwd", "/workspace/switchboard"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result["transcript"]).parent == other / "projects" / "-workspace-switchboard"
    assert result["resume"].endswith(f"claude --resume {SID}")

    # And the human line for the same import, the second time round: nothing
    # changed, so nothing is written and nothing is backed up.
    assert main(["session", "import", str(path), "--cwd", "/workspace/switchboard"]) == 0
    out = capsys.readouterr().out
    assert "installed" in out and "resume with" in out and "kept the previous" not in out


def test_import_refuses_a_bad_file_as_a_sentence_not_a_traceback(tmp_path, capsys):
    (tmp_path / "junk").write_text("{not json")
    assert main(["session", "import", str(tmp_path / "junk")]) == 1
    assert "not a capsule" in capsys.readouterr().err
    assert main(["session", "import", str(tmp_path / "missing.json")]) == 1
    err = capsys.readouterr().err
    assert "error:" in err and "cannot reach hub" not in err


def test_export_without_a_session_says_what_is_missing(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert main(["session", "export"]) == 1
    assert "CLAUDE_CODE_SESSION_ID" in capsys.readouterr().err


# --- through the hub -------------------------------------------------------------

def test_handoff_then_receive(room, sender_cfg, tmp_path, capsys, monkeypatch):
    h = room
    receiver = h.client("bob", register=True)
    code, sent, _ = _run(capsys, "session", "handoff", receiver.agent_id)
    assert code == 0
    assert sent["key"] == f"sessions/{SID}" and sent["to"] == receiver.agent_id
    assert sent["unread_dms"] == 0

    dest = tmp_path / "bob-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(dest))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    code, got, err = _run(capsys, "session", "receive", "--cwd", "/w", agent="bob")
    assert code == 0, (got, err)
    assert got["listening_as"] == receiver.agent_id
    [installed] = got["installed"]
    assert installed["verified"] is True and installed["deleted_from_board"] is True
    assert Path(installed["transcript"]).read_text().count("\n") == 2
    assert got["resumed"] == []
    assert h.board() == []


def test_handoff_resolves_a_name_the_way_dm_does(room, sender_cfg, capsys):
    h = room
    receiver = h.client("bob", register=True)
    code, sent, err = _run(capsys, "session", "handoff", "bob")
    assert code == 0 and sent["to"] == receiver.agent_id
    assert "matched the name" in err


def test_publish_is_a_checkpoint_collected_by_id(room, sender_cfg, tmp_path, capsys, monkeypatch):
    code, sent, _ = _run(capsys, "session", "publish", "--ttl", "45")
    assert code == 0 and sent["to"] is None and sent["expires_in"] == 45
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "puller"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    code, got, err = _run(capsys, "session", "receive", SID, "--cwd", "/w", agent="puller")
    assert code == 0, (got, err)
    assert got["installed"][0]["verified"] is None


def test_receive_exit_codes_tell_a_loop_what_happened(room, sender_cfg, tmp_path, capsys,
                                                      monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob-claude"))
    # Nothing pending, no wait: fine, exit 0.
    code, got, err = _run(capsys, "session", "receive", agent="bob")
    assert code == 0 and got["installed"] == [] and got["pointers"] == 0
    # Waited and nothing came: the deadline code, like `listen`.
    code, got, _ = _run(capsys, "session", "receive", "--wait", "0.1", agent="bob")
    assert code == 2
    # A capsule that is gone by the time it is asked for: an error a loop can log.
    code, got, _ = _run(capsys, "session", "receive", SID, agent="bob")
    assert code == 1 and "expired" in got["missing"][0]["reason"]


def test_resume_needs_an_allowlist_and_claude(room, sender_cfg, tmp_path, capsys, monkeypatch):
    h = room
    receiver = h.client("bob", register=True)
    _run(capsys, "session", "handoff", receiver.agent_id)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob-claude"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setattr(cs.shutil, "which", lambda name: None)

    code, got, err = _run(capsys, "session", "receive", "--cwd", "/w", "--resume", agent="bob")
    assert code == 0, (got, err)
    [run] = got["resumed"]
    assert run["started"] is False and "--from" in run["reason"], (
        "installed, but not started: nobody said this sender may run here"
    )

    # Named sender, but no claude on PATH: reported, with the command to run.
    from_id = got["installed"][0]["from"]["agent_id"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(sender_cfg))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    _run(capsys, "session", "handoff", receiver.agent_id)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob-claude"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    code, got, err = _run(capsys, "session", "receive", "--cwd", "/w", "--resume", "--bg",
                          "--from", from_id, "--force", agent="bob")
    assert got["installed"], (got, err)
    [run] = got["resumed"]
    assert run["started"] is False and "PATH" in run["reason"]
    assert run["command"].endswith(f"claude --bg --resume {SID}")


def test_resume_command_prints_the_line_a_human_runs(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    assert main(["session", "resume", SID, "--command", "--cwd", "/w"]) == 0
    line = capsys.readouterr().out.strip()
    assert line == f"cd /w && CLAUDE_CONFIG_DIR={tmp_path / 'c'} claude --resume {SID}"
    assert main(["session", "resume", "not-an-id", "--command"]) == 1
    assert "not a Claude Code session id" in capsys.readouterr().err
    # Not installed here: refused before claude is run, with the reason.
    assert main(["session", "resume", SID]) == 1
    assert "no transcript" in capsys.readouterr().err


def test_a_plaintext_room_is_refused_with_a_way_through(sender_cfg, capsys, monkeypatch):
    import switchboard.cli as cli_module

    with hub(workspace=WS) as h:
        monkeypatch.setattr(cli_module, "Client", h.client_class())
        monkeypatch.delenv("SWITCHBOARD_TOKEN", raising=False)
        code = main(["--url", BASE_URL, "-w", WS, "session", "publish"])
        assert code == 1
        assert "--allow-plaintext" in capsys.readouterr().err
        assert h.board() == []
        code = main(["--url", BASE_URL, "-w", WS, "session", "publish", "--allow-plaintext"])
        assert code == 0
        assert "warning" in capsys.readouterr().err


def test_a_typo_points_at_the_session_verbs(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["handoff", "bob"])
    assert stop.value.code == 2
    assert "session handoff" in capsys.readouterr().err


def test_an_unattended_wait_needs_an_address_that_lasts(room, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    with pytest.raises(SystemExit) as stop:
        main(["--url", BASE_URL, "-w", WS, "session", "receive", "--wait", "0.1"])
    assert "SWITCHBOARD_AGENT_ID" in str(stop.value)
    # Pinned, the same wait is allowed and simply times out.
    monkeypatch.setenv("SWITCHBOARD_AGENT_ID", "parked-receiver")
    assert main(["--url", BASE_URL, "-w", WS, "session", "receive", "--wait", "0.1"]) == 2
    assert "listening as" in capsys.readouterr().err


def test_resume_gates_on_the_verified_signer_not_the_board_value(room, sender_cfg, tmp_path,
                                                                   capsys, monkeypatch):
    h = room
    receiver = h.client("bob", register=True)
    monkeypatch.setattr(cs.shutil, "which", lambda name: None)
    # A checkpoint nobody signed for: --any-sender still does not start it.
    _run(capsys, "session", "publish")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob-claude"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    code, got, err = _run(capsys, "session", "receive", SID, "--cwd", "/w", "--resume",
                          "--any-sender", agent="bob")
    assert code == 0, (got, err)
    [run] = got["resumed"]
    assert run["started"] is False and "vouched" in run["reason"]

    # A signed handoff, allowed by the alias the sender pinned, not its hub form.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(sender_cfg))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    _run(capsys, "session", "handoff", receiver.agent_id)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "bob-claude"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    code, got, err = _run(capsys, "session", "receive", "--cwd", "/w", "--resume", "--bg",
                          "--from", "cli-agent", agent="bob")
    assert code == 0, (got, err)
    [run] = got["resumed"]
    assert "PATH" in run["reason"], "allowed by --from; only the missing binary stopped it"


def test_publish_reports_what_is_waiting_for_you(room, sender_cfg, tmp_path, capsys):
    h = room
    bob = h.client("bob", register=True)
    me = h.client("cli-agent")
    bob.send(me.agent_id, "read this first")
    code, sent, _ = _run(capsys, "session", "publish")
    assert code == 0 and sent["unread_dms"] == 1


def test_resume_command_honours_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    assert main(["--json", "session", "resume", SID, "--command", "--cwd", "/w"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == SID and payload["command"].startswith("cd /w && ")
