"""A Claude Code session survives being packaged and installed elsewhere.

The property under test is the one the handoff feature rests on: the bytes
that reach the destination are the bytes that left, they land where
``claude --resume`` looks, and nothing about the receiving machine — its
working directory, an empty config dir, a different project key — changes
that. No hub is involved here; ``switchboard.claude_session`` is the
harness-specific half and it never talks to one.

The other half of the property is refusal: a capsule comes from another
agent, so its file names are an attack surface, and Claude Code itself will
refuse to resume an id that sits under two project keys. Both are checked as
behaviour, not as documentation.
"""

from __future__ import annotations

import base64
import json
import os
import zlib
from pathlib import Path

import pytest

from switchboard import claude_session as cs

SID = "089ad0fb-8aff-46dd-bcad-18d3a8a2e069"


def _record(**fields):
    base = {
        "type": "user",
        "sessionId": SID,
        "cwd": "/Users/gal/code/switchboard",
        "version": "2.1.260",
        "gitBranch": "main",
    }
    base.update(fields)
    return json.dumps(base)


def _make_session(cfg: Path, cwd: str, *, sidecar: bool = True) -> Path:
    """A plausible transcript plus a subagent sidecar, under cwd's project key."""
    project = cfg / "projects" / cs.project_key(cwd)
    project.mkdir(parents=True)
    transcript = project / f"{SID}.jsonl"
    lines = [
        _record(type="queue-operation", operation="enqueue"),
        _record(type="user", message={"role": "user", "content": "codeword TANGERINE-4471"}),
        "this line is not json and must be tolerated",
        _record(type="assistant", message={"role": "assistant", "content": []}),
        _record(type="assistant", cwd="/Users/gal/code/switchboard"),
    ]
    transcript.write_text("\n".join(lines) + "\n")
    if sidecar:
        sub = project / SID / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-abc.jsonl").write_text(_record(type="user") + "\n")
        (sub / "agent-abc.meta.json").write_text('{"agentType": "general-purpose"}')
    return transcript


@pytest.fixture
def source(tmp_path):
    cfg = tmp_path / "source-claude"
    transcript = _make_session(cfg, "/Users/gal/code/switchboard")
    return cfg, transcript


# --- discovery ---------------------------------------------------------------

def test_project_key_is_claude_codes_own_scheme():
    assert cs.project_key("/home/user/switchboard") == "-home-user-switchboard"
    assert cs.project_key("/Users/gal/code/switchboard") == "-Users-gal-code-switchboard"


def test_project_key_refuses_what_claude_would_hash(tmp_path):
    with pytest.raises(cs.CapsuleError):
        cs.project_key("/" + "x" * 250)


def test_config_dir_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv(cs.CONFIG_DIR_VAR, raising=False)
    assert cs.config_dir() == Path.home() / ".claude"
    monkeypatch.setenv(cs.CONFIG_DIR_VAR, str(tmp_path / "elsewhere"))
    assert cs.config_dir() == tmp_path / "elsewhere"
    assert cs.config_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_the_session_id_comes_from_claude_and_must_be_a_uuid(monkeypatch):
    monkeypatch.delenv(cs.SESSION_ID_VAR, raising=False)
    assert cs.current_session_id() is None
    monkeypatch.setenv(cs.SESSION_ID_VAR, SID.upper())
    assert cs.current_session_id() == SID
    monkeypatch.setenv(cs.SESSION_ID_VAR, "../../etc/passwd")
    assert cs.current_session_id() is None
    with pytest.raises(cs.CapsuleError):
        cs.valid_session_id("not-a-session")


def test_metadata_is_read_off_native_records_without_choking(source):
    _, transcript = source
    meta = cs.transcript_metadata(transcript)
    assert meta["cwd"] == "/Users/gal/code/switchboard"
    assert meta["version"] == "2.1.260"
    assert meta["git_branch"] == "main"
    assert meta["records"] == 5
    assert (meta["user_messages"], meta["assistant_messages"]) == (1, 2)


# --- package -----------------------------------------------------------------

def test_package_carries_every_file_compressed_and_hashed(source):
    cfg, transcript = source
    capsule = cs.package(SID, config_dir=cfg)
    assert capsule["capsule_version"] == cs.CAPSULE_VERSION
    assert capsule["source_harness"] == {"name": "claude-code", "version": "2.1.260"}
    assert capsule["session_id"] == SID
    assert capsule["project_key"] == "-Users-gal-code-switchboard"
    assert capsule["original_working_directory"] == "/Users/gal/code/switchboard"
    names = [f["relative_destination"] for f in capsule["files"]]
    assert names == [
        f"{SID}.jsonl",
        f"{SID}/subagents/agent-abc.jsonl",
        f"{SID}/subagents/agent-abc.meta.json",
    ]
    main = capsule["files"][0]
    assert main["encoding"] == "zlib+base64"
    assert zlib.decompress(base64.b64decode(main["data"])) == transcript.read_bytes()
    assert main["bytes"] == len(transcript.read_bytes())
    assert cs.capsule_size(capsule) == sum(f["bytes"] for f in capsule["files"])
    assert cs.summary(capsule)["files"] == 3


def test_package_refuses_an_unknown_or_ambiguous_session(source, tmp_path):
    cfg, _ = source
    with pytest.raises(cs.CapsuleError, match="not found"):
        cs.package("11111111-2222-3333-4444-555555555555", config_dir=cfg)
    # A second copy under another key: resume itself would refuse to choose,
    # and so does export — unless told which working directory it means.
    _make_session(cfg, "/somewhere/else", sidecar=False)
    with pytest.raises(cs.CapsuleError, match="several project keys"):
        cs.package(SID, config_dir=cfg)
    capsule = cs.package(SID, config_dir=cfg, cwd="/somewhere/else")
    assert capsule["project_key"] == "-somewhere-else"
    assert len(capsule["files"]) == 1


# --- install -----------------------------------------------------------------

def test_install_under_the_destination_directory_is_byte_for_byte(source, tmp_path):
    cfg, transcript = source
    capsule = cs.package(SID, config_dir=cfg)
    dest = tmp_path / "dest-claude"
    result = cs.install(capsule, config_dir=dest, cwd="/workspace/switchboard")
    project = dest / "projects" / "-workspace-switchboard"
    assert Path(result["transcript"]) == project / f"{SID}.jsonl"
    assert (project / f"{SID}.jsonl").read_bytes() == transcript.read_bytes()
    assert (project / SID / "subagents" / "agent-abc.meta.json").read_text() == (
        '{"agentType": "general-purpose"}'
    )
    assert len(result["written"]) == 3 and not result["backed_up"]
    assert result["resume_cwd"] == "/workspace/switchboard"
    assert result["resume"].startswith("cd /workspace/switchboard && ")
    assert result["resume"].endswith(f"claude --resume {SID}")
    assert oct((project / f"{SID}.jsonl").stat().st_mode & 0o777) == "0o600"


def test_install_without_a_directory_keeps_the_original_key(source, tmp_path):
    cfg, _ = source
    capsule = cs.package(SID, config_dir=cfg)
    dest = tmp_path / "dest-claude"
    result = cs.install(capsule, config_dir=dest)
    assert Path(result["project_dir"]).name == "-Users-gal-code-switchboard"
    assert result["resume_cwd"] == "/Users/gal/code/switchboard"


def test_install_is_idempotent_and_backs_up_only_real_changes(source, tmp_path):
    cfg, transcript = source
    dest = tmp_path / "dest-claude"
    first = cs.install(cs.package(SID, config_dir=cfg), config_dir=dest, cwd="/w")
    again = cs.install(cs.package(SID, config_dir=cfg), config_dir=dest, cwd="/w")
    assert again["written"] == [] and again["unchanged"] == first["written"]
    # The session moved on at the source; the newer transcript replaces the
    # old one, and the old one is kept beside it rather than lost.
    with transcript.open("a") as fh:
        fh.write(_record(type="assistant") + "\n")
    third = cs.install(cs.package(SID, config_dir=cfg), config_dir=dest, cwd="/w")
    assert third["written"] == [first["written"][0]]
    assert len(third["backed_up"]) == 1
    assert third["backed_up"][0].startswith(first["written"][0] + ".bak-")


def test_install_refuses_a_second_project_key_unless_forced(source, tmp_path):
    cfg, _ = source
    capsule = cs.package(SID, config_dir=cfg)
    dest = tmp_path / "dest-claude"
    cs.install(capsule, config_dir=dest, cwd="/one")
    with pytest.raises(cs.CapsuleError, match="ambiguous"):
        cs.install(capsule, config_dir=dest, cwd="/two")
    forced = cs.install(capsule, config_dir=dest, cwd="/two", force=True)
    assert Path(forced["project_dir"]).name == "-two"


@pytest.mark.parametrize("bad", [
    "../escape.jsonl",
    "/etc/passwd",
    f"{SID}/../other.jsonl",
    f"{SID}\\subagents\\x.jsonl",
    "someone-else.jsonl",
    "",
])
def test_install_refuses_paths_outside_the_session(source, tmp_path, bad):
    cfg, _ = source
    capsule = cs.package(SID, config_dir=cfg)
    capsule["files"][1]["relative_destination"] = bad
    with pytest.raises(cs.CapsuleError, match="unsafe|no relative_destination"):
        cs.install(capsule, config_dir=tmp_path / "dest", cwd="/w")
    assert not (tmp_path / "dest").exists()


def test_install_refuses_a_tampered_file(source, tmp_path):
    cfg, _ = source
    capsule = cs.package(SID, config_dir=cfg)
    forged = base64.b64encode(zlib.compress(b"not the transcript")).decode()
    capsule["files"][0]["data"] = forged
    with pytest.raises(cs.CapsuleError, match="sha256 mismatch"):
        cs.install(capsule, config_dir=tmp_path / "dest", cwd="/w")
    capsule = cs.package(SID, config_dir=cfg)
    capsule["files"][0]["encoding"] = "rot13"
    with pytest.raises(cs.CapsuleError, match="encoding"):
        cs.install(capsule, config_dir=tmp_path / "dest", cwd="/w")


def test_install_still_reads_the_prototypes_plain_base64(source, tmp_path):
    cfg, transcript = source
    capsule = cs.package(SID, config_dir=cfg)
    raw = transcript.read_bytes()
    capsule["files"] = [{
        "relative_destination": f"{SID}.jsonl",
        "bytes": len(raw),
        "sha256": capsule["files"][0]["sha256"],
        "encoding": "base64",
        "data": base64.b64encode(raw).decode(),
    }]
    result = cs.install(capsule, config_dir=tmp_path / "dest", cwd="/w")
    assert Path(result["transcript"]).read_bytes() == raw


def test_validate_rejects_foreign_capsules(source):
    cfg, _ = source
    capsule = cs.package(SID, config_dir=cfg)
    with pytest.raises(cs.CapsuleError, match="capsule_version"):
        cs.validate({**capsule, "capsule_version": 99})
    with pytest.raises(cs.CapsuleError, match="not a claude-code"):
        cs.validate({**capsule, "source_harness": {"name": "codex"}})
    with pytest.raises(cs.CapsuleError, match="no files"):
        cs.validate({**capsule, "files": []})
    with pytest.raises(cs.CapsuleError):
        cs.validate("a string")


# --- resume ------------------------------------------------------------------

def test_a_child_claude_must_not_inherit_the_parents_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_x")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = cs.child_env()
    assert not any(name in env for name in cs.CHILD_UNSET)
    assert env["PATH"] == "/usr/bin"


def test_resume_command_shapes(monkeypatch, tmp_path):
    monkeypatch.delenv(cs.CONFIG_DIR_VAR, raising=False)
    assert cs.resume_argv(SID) == ["claude", "--resume", SID]
    assert cs.resume_argv(SID, background=True) == ["claude", "--bg", "--resume", SID]
    assert cs.shell_resume_command(SID) == f"claude --resume {SID}"
    line = cs.shell_resume_command(SID, cwd="/w", config_dir=tmp_path / "c", background=True)
    assert line == f"cd /w && CLAUDE_CONFIG_DIR={tmp_path / 'c'} claude --bg --resume {SID}"
    # The default config dir is not worth spelling out.
    default = cs.shell_resume_command(SID, config_dir=Path.home() / ".claude")
    assert default == f"claude --resume {SID}"


def test_spawn_reports_a_missing_claude_as_data_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cs.shutil, "which", lambda name: None)
    result = cs.spawn_resume(SID, cwd=str(tmp_path), config_dir=tmp_path / "c")
    assert result["started"] is False
    assert "PATH" in result["reason"]
    assert result["command"].endswith(f"claude --bg --resume {SID}")


def test_spawn_runs_claude_with_a_clean_environment(monkeypatch, tmp_path):
    """A stand-in `claude` records what it was given; the real one is not needed."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "seen.txt"
    script = fake_bin / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {log}\n"
        "printf 'SID=%s CFG=%s CWD=%s\\n' "
        f"\"$CLAUDE_CODE_SESSION_ID\" \"$CLAUDE_CONFIG_DIR\" \"$PWD\" >> {log}\n"
        "echo backgrounded\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = cs.spawn_resume(SID, cwd=str(workdir), config_dir=tmp_path / "c")
    assert result["started"] is True
    assert result["output"] == "backgrounded"
    assert result["attach"] == f"claude attach {SID[:8]}"
    seen = log.read_text().splitlines()
    assert seen[:3] == ["--bg", "--resume", SID]
    assert seen[3] == f"SID= CFG={tmp_path / 'c'} CWD={workdir}"
