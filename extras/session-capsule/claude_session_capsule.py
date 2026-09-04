#!/usr/bin/env python3
"""Prototype: export / import a Claude Code session as an opaque capsule.

Disposable experiment, deliberately Switchboard-neutral. Everything Claude Code
wrote for the session is carried verbatim: the main transcript
``<session-id>.jsonl`` and, when present, the whole ``<session-id>/`` sidecar
directory (subagent transcripts and metadata). Nothing inside those files is
parsed for any purpose other than reading two metadata fields (``cwd`` and
``version``) off native records.

    claude_session_capsule.py export  [--session-id ID] [--config-dir DIR] [--cwd DIR] \
                                      -o capsule.json
    claude_session_capsule.py inspect capsule.json
    claude_session_capsule.py import  capsule.json [--config-dir DIR] [--cwd DIR] [--force]

Observed facts this relies on (Claude Code 2.1.260, see README.md next to this file):

* Sessions live at ``<config-dir>/projects/<project-key>/<session-id>.jsonl``,
  where ``<config-dir>`` is ``$CLAUDE_CONFIG_DIR`` or ``~/.claude`` and
  ``<project-key>`` is the working directory with every character outside
  ``[A-Za-z0-9]`` replaced by ``-``.
* ``claude --resume <id>`` looks in the current directory's project key first,
  then scans every project directory. The scan refuses to pick when the same id
  exists under more than one project key ("No conversation found").
* The transcript records the original ``cwd`` on every line but resume does
  not require that directory to exist.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

CAPSULE_VERSION = "0.1"
HARNESS = "claude-code"


# ---------------------------------------------------------------- helpers ---

def config_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def project_key(cwd: str) -> str:
    """Mirror Claude Code's project-directory naming for a working directory.

    Claude Code additionally truncates keys longer than 200 characters and
    appends a hash; that branch is not reproduced here, so refuse instead.
    """
    key = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    if len(key) > 200:
        raise SystemExit(f"working directory too long for this prototype (>200 chars): {cwd}")
    return key


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_session_files(cfg: Path, session_id: str) -> list[Path]:
    projects = cfg / "projects"
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.glob(f"*/{session_id}.jsonl") if p.is_file())


def native_metadata(transcript: Path) -> dict:
    """Read cwd / version / gitBranch off native records without interpreting them."""
    meta: dict = {"cwd": None, "version": None, "git_branch": None, "records": 0,
                  "user_messages": 0, "assistant_messages": 0}
    with transcript.open("rb") as fh:
        for raw in fh:
            meta["records"] += 1
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("cwd"):
                meta["cwd"] = rec["cwd"]          # last one wins: where it was last driven
            if rec.get("version"):
                meta["version"] = rec["version"]
            if rec.get("gitBranch"):
                meta["git_branch"] = rec["gitBranch"]
            t = rec.get("type")
            if t == "user":
                meta["user_messages"] += 1
            elif t == "assistant":
                meta["assistant_messages"] += 1
    return meta


# ----------------------------------------------------------------- export ---

def cmd_export(args: argparse.Namespace) -> int:
    session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        raise SystemExit("no session id: pass --session-id or set CLAUDE_CODE_SESSION_ID")
    cfg = config_dir(args.config_dir)

    candidates = find_session_files(cfg, session_id)
    if args.cwd:
        key = project_key(os.path.abspath(args.cwd))
        preferred = cfg / "projects" / key / f"{session_id}.jsonl"
        candidates = [p for p in candidates if p == preferred] or candidates
    if not candidates:
        raise SystemExit(f"session {session_id} not found under {cfg / 'projects'}")
    if len(candidates) > 1:
        raise SystemExit("ambiguous: session exists under several project keys:\n  "
                         + "\n  ".join(map(str, candidates)) + "\npass --cwd to pick one")
    transcript = candidates[0]
    project_dir = transcript.parent
    sidecar = project_dir / session_id

    files = []

    def add(path: Path, rel: str) -> None:
        data = path.read_bytes()
        files.append({
            "relative_destination": rel,
            "bytes": len(data),
            "sha256": sha256_bytes(data),
            "base64": base64.b64encode(data).decode("ascii"),
        })

    add(transcript, f"{session_id}.jsonl")
    if sidecar.is_dir():
        for p in sorted(sidecar.rglob("*")):
            if p.is_file():
                add(p, f"{session_id}/{p.relative_to(sidecar).as_posix()}")

    meta = native_metadata(transcript)
    capsule = {
        "capsule_version": CAPSULE_VERSION,
        "source_harness": {"name": HARNESS, "version": meta["version"]},
        "session_id": session_id,
        "project_key": project_dir.name,
        "original_working_directory": meta["cwd"],
        "git_branch": meta["git_branch"],
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "exported_from_config_dir": str(cfg),
        "stats": {k: meta[k] for k in ("records", "user_messages", "assistant_messages")},
        "files": files,
    }
    out = Path(args.output)
    out.write_text(json.dumps(capsule, indent=None if args.compact else 1))
    total = sum(f["bytes"] for f in files)
    print(f"exported session {session_id}: {len(files)} file(s), {total} bytes payload -> {out}")
    for f in files:
        print(f"  {f['relative_destination']}  {f['bytes']}B  {f['sha256'][:12]}")
    return 0


# ---------------------------------------------------------------- inspect ---

def load_capsule(path: str) -> dict:
    cap = json.loads(Path(path).read_text())
    harness = cap.get("source_harness", {}).get("name")
    if cap.get("capsule_version") != CAPSULE_VERSION or harness != HARNESS:
        raise SystemExit("not a ClaudeSessionCapsule this prototype understands")
    for f in cap["files"]:
        rel = f["relative_destination"]
        if rel.startswith("/") or ".." in rel.split("/"):
            raise SystemExit(f"unsafe relative_destination in capsule: {rel}")
    return cap


def cmd_inspect(args: argparse.Namespace) -> int:
    cap = load_capsule(args.capsule)
    for k in ("capsule_version", "source_harness", "session_id", "project_key",
              "original_working_directory", "git_branch", "exported_at", "stats"):
        print(f"{k}: {cap.get(k)}")
    for f in cap["files"]:
        ok = sha256_bytes(base64.b64decode(f["base64"])) == f["sha256"]
        print(f"  {f['relative_destination']}  {f['bytes']}B  sha256 {'ok' if ok else 'MISMATCH'}")
    return 0


# ----------------------------------------------------------------- import ---

def cmd_import(args: argparse.Namespace) -> int:
    cap = load_capsule(args.capsule)
    session_id = cap["session_id"]
    cfg = config_dir(args.config_dir)

    if args.cwd:
        dest_key = project_key(os.path.abspath(args.cwd))
        resume_cwd = os.path.abspath(args.cwd)
    else:
        dest_key = cap["project_key"]
        resume_cwd = cap.get("original_working_directory")
    dest_dir = cfg / "projects" / dest_key

    # Duplicates under other project keys make Claude Code's cross-project scan
    # refuse to resume, so surface them before writing anything.
    others = [p for p in find_session_files(cfg, session_id) if p.parent != dest_dir]
    if others and not args.force:
        raise SystemExit("session already exists under other project key(s); "
                         "resume by id would be ambiguous:\n  "
                         + "\n  ".join(map(str, others))
                         + "\nre-run with --force to import anyway")

    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for f in cap["files"]:
        target = dest_dir / f["relative_destination"]
        data = base64.b64decode(f["base64"])
        if sha256_bytes(data) != f["sha256"]:
            raise SystemExit(f"sha256 mismatch for {f['relative_destination']}; capsule corrupt")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            backup = target.with_name(target.name + f".bak-{stamp}")
            target.replace(backup)
            print(f"  backed up existing {target.name} -> {backup.name}")
        target.write_bytes(data)
        os.chmod(target, 0o600)
        print(f"  wrote {target}")

    print()
    print(f"imported session {session_id} into {dest_dir}")
    print("resume with:")
    explicit_cfg = args.config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    prefix = f"CLAUDE_CONFIG_DIR={cfg} " if explicit_cfg else ""
    cd = f"cd {resume_cwd} && " if resume_cwd else ""
    print(f"  {cd}{prefix}claude --resume {session_id}")
    if not args.cwd:
        print("  (file placed under the original project key; any directory works, "
              "resume falls back to a scan)")
    return 0


# ------------------------------------------------------------------- main ---

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="package a session from a Claude config dir")
    e.add_argument("--session-id",
                   help="defaults to $CLAUDE_CODE_SESSION_ID (set inside a running session)")
    e.add_argument("--config-dir", help="defaults to $CLAUDE_CONFIG_DIR or ~/.claude")
    e.add_argument("--cwd", help="disambiguate: the working directory the session was run from")
    e.add_argument("-o", "--output", required=True)
    e.add_argument("--compact", action="store_true", help="single-line JSON")
    e.set_defaults(fn=cmd_export)

    i = sub.add_parser("inspect", help="print capsule metadata and verify hashes")
    i.add_argument("capsule")
    i.set_defaults(fn=cmd_inspect)

    m = sub.add_parser("import", help="install a capsule into a Claude config dir")
    m.add_argument("capsule")
    m.add_argument("--config-dir", help="defaults to $CLAUDE_CONFIG_DIR or ~/.claude")
    m.add_argument("--cwd", help="destination working directory; file goes under its project key")
    m.add_argument("--force", action="store_true",
                   help="import even if the id exists under other project keys")
    m.set_defaults(fn=cmd_import)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
