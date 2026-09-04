"""Claude Code sessions as portable capsules: locate, package, install, resume.

A Claude Code conversation is one file. The CLI writes every turn, tool call
and tool result of a session to ``<config-dir>/projects/<project-key>/<id>.jsonl``
and, when the session spawned subagents, a sidecar directory ``<id>/`` beside
it. Copy that file under *any* project key on another machine and
``claude --resume <id>`` continues the conversation there — from a different
working directory, with no path rewriting, and with no other state. That was
established empirically (Claude Code 2.1.260; see
``extras/session-capsule/README.md`` for the experiment log) and it is the
whole basis of this module: the transcript is carried byte for byte and never
interpreted, beyond reading two metadata fields off native records.

This module is the *harness-specific* half of a handoff. It knows where Claude
Code keeps things and how to start it again; it knows nothing about a hub. The
hub-neutral half — publishing a capsule to another agent through the
blackboard and a pointer message — is ``switchboard.handoff``, which calls in
here through a small adapter surface (``HARNESS``, ``package``, ``install``,
``resume_command``) so a second harness can be added beside this one without
touching the transport.

Stdlib only, on purpose: it is imported by both the CLI and the MCP bridge, and
those may depend on nothing beyond ``httpx``.

Facts this relies on, all observed rather than assumed:

* ``<config-dir>`` is ``$CLAUDE_CONFIG_DIR`` when set, else ``~/.claude``.
* ``<project-key>`` is the working directory with every character outside
  ``[A-Za-z0-9]`` replaced by ``-`` (Claude Code additionally truncates keys
  over 200 characters and appends a hash; that branch is not reproduced, so a
  path that long is refused rather than guessed at).
* ``claude --resume <id>`` looks under the current directory's key first, then
  under every key. The scan refuses to choose when the id sits under more than
  one key, so an import places the file under exactly one.
* Inside a running session Claude Code exports ``CLAUDE_CODE_SESSION_ID`` and
  ``CLAUDE_PROJECT_DIR``, and stdio MCP servers inherit both. A child
  ``claude`` process inherits the session id too and would then write into its
  parent's transcript, so anything that starts one must unset it first.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any, Iterator

#: The free-form harness label a capsule carries. Switchboard enumerates no
#: harnesses (docs/layers.md); this string is what ``switchboard.handoff``
#: dispatches on, and a Codex adapter would declare its own.
HARNESS = "claude-code"

CAPSULE_VERSION = 1

#: How file bytes travel inside a capsule. Transcripts are JSON lines and
#: compress about 3.7x with zlib; base64 alone would grow them by a third. The
#: prototype's plain ``base64`` is still accepted on the way in.
ENCODING = "zlib+base64"
_ENCODINGS = ("zlib+base64", "base64")

#: The most one capsule file may decode to. A transcript is a few MB after a
#: long day; this is far above that and far below what a small sealed value
#: could be made to expand into on the receiver's machine.
MAX_FILE_BYTES = 256 * 1024 * 1024

SESSION_ID_VAR = "CLAUDE_CODE_SESSION_ID"
CONFIG_DIR_VAR = "CLAUDE_CONFIG_DIR"
PROJECT_DIR_VAR = "CLAUDE_PROJECT_DIR"

#: Variables a child ``claude`` must not inherit from a running session. The
#: first would make it append to the parent's transcript; the others make it
#: believe it is that session's remote runner.
CHILD_UNSET = ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_REMOTE_SESSION_ID")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MAX_KEY = 200


class CapsuleError(Exception):
    """A capsule could not be built, verified or installed.

    Distinct from ``ValueError`` because the MCP bridge labels that
    ``unknown_tool``; the bridge maps this one to its own error code.
    """


# ------------------------------------------------------------ discovery ---

def config_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Where this machine's Claude Code keeps its state."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(CONFIG_DIR_VAR)
    return Path(env).expanduser() if env else Path.home() / ".claude"


#: Functions below take a ``config_dir`` argument, which shadows the function.
_resolve_config_dir = config_dir


def current_session_id() -> str | None:
    """The session this process runs inside, if Claude Code told us."""
    value = os.environ.get(SESSION_ID_VAR, "").strip().lower()
    return value if _UUID.match(value) else None


def current_project_dir() -> str | None:
    """The working directory of the session this process runs inside."""
    value = os.environ.get(PROJECT_DIR_VAR, "").strip()
    return value or None


def valid_session_id(value: str) -> str:
    """Normalise a session id, refusing anything that is not a UUID.

    The id names a file and a directory on the receiving side, so this is a
    path-safety check as much as a format one.
    """
    candidate = (value or "").strip().lower()
    if not _UUID.match(candidate):
        raise CapsuleError(f"not a Claude Code session id: {value!r}")
    return candidate


def project_key(cwd: str | os.PathLike[str]) -> str:
    """Claude Code's ``projects/`` directory name for a working directory."""
    text = os.path.abspath(os.fspath(cwd))
    key = re.sub(r"[^a-zA-Z0-9]", "-", text)
    if len(key) > _MAX_KEY:
        raise CapsuleError(
            f"working directory too long to key a Claude project ({len(key)} > {_MAX_KEY}): {text}"
        )
    return key


def find_transcripts(cfg: Path, session_id: str) -> list[Path]:
    """Every ``projects/*/<id>.jsonl`` under a config dir, sorted."""
    projects = cfg / "projects"
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.glob(f"*/{session_id}.jsonl") if p.is_file())


def live_session_ids(cfg: Path) -> set[str]:
    """Sessions a running Claude Code process on this machine has open.

    Claude Code registers each live process at ``<config-dir>/sessions/<pid>.json``
    with its ``sessionId``. Best effort: an unreadable or stale file is
    ignored, and a process that died without cleaning up is reported as live
    — the cost of that is a refusal that ``force`` overrides.
    """
    live: set[str] = set()
    sessions = cfg / "sessions"
    if not sessions.is_dir():
        return live
    for path in sessions.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        sid = record.get("sessionId") if isinstance(record, dict) else None
        if isinstance(sid, str) and _UUID.match(sid.lower()):
            live.add(sid.lower())
    return live


def count_records(data: bytes) -> int:
    """How many lines a transcript has — its length, as Claude Code appends it.

    A final line without its newline still counts: a session captured
    mid-append ends that way, and the same bytes must count the same on both
    sides of a comparison.
    """
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def record_count(path: Path) -> int:
    return count_records(path.read_bytes())


def transcript_metadata(path: Path) -> dict[str, Any]:
    """Read cwd, CLI version and branch off native records without interpreting them.

    Every record carries the working directory it was written from; the last
    one wins because that is where the session was most recently driven.
    Unparseable lines are skipped, not failed on: the file is Claude Code's
    to define and this reader must not be stricter than the CLI itself.
    """
    meta: dict[str, Any] = {
        "cwd": None,
        "version": None,
        "git_branch": None,
        "records": 0,
        "user_messages": 0,
        "assistant_messages": 0,
    }
    with path.open("rb") as fh:
        for raw in fh:
            meta["records"] += 1
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            for field, source in (("cwd", "cwd"), ("version", "version"),
                                  ("git_branch", "gitBranch")):
                value = record.get(source)
                if isinstance(value, str) and value:
                    meta[field] = value
            kind = record.get("type")
            if kind == "user":
                meta["user_messages"] += 1
            elif kind == "assistant":
                meta["assistant_messages"] += 1
    return meta


# -------------------------------------------------------------- package ---

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_bytes(data: bytes) -> str:
    return base64.b64encode(zlib.compress(data, 9)).decode("ascii")


def decode_entry(entry: dict[str, Any]) -> bytes:
    """The bytes of one capsule file, verified against its recorded hash."""
    name = entry.get("relative_destination")
    encoding = entry.get("encoding", "base64")
    if encoding not in _ENCODINGS:
        raise CapsuleError(f"unknown capsule file encoding: {encoding!r}")
    declared = entry.get("bytes")
    if not isinstance(declared, int) or declared < 0 or declared > MAX_FILE_BYTES:
        raise CapsuleError(f"capsule file {name!r} declares an unusable size: {declared!r}")
    try:
        raw = base64.b64decode(entry["data"], validate=True)
        if encoding == "zlib+base64":
            # Bounded by what the entry declares, so a small value cannot be
            # made to expand into more than the receiver agreed to hold.
            inflater = zlib.decompressobj()
            data = inflater.decompress(raw, declared + 1)
            if inflater.unconsumed_tail or not inflater.eof:
                raise CapsuleError(f"capsule file {name!r} inflates past its declared size")
        else:
            data = raw
    except (KeyError, ValueError, zlib.error) as exc:
        raise CapsuleError(f"capsule file {name!r} is corrupt: {exc}") from exc
    if _sha256(data) != entry.get("sha256"):
        raise CapsuleError(f"sha256 mismatch for {name!r}; capsule corrupt")
    if declared != len(data):
        raise CapsuleError(f"size mismatch for {name!r}; capsule corrupt")
    return data


def package(
    session_id: str,
    *,
    config_dir: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Build a capsule for a session found under this machine's config dir.

    ``cwd`` disambiguates when the same id sits under several project keys
    (rare: it means the session was already imported here from elsewhere).
    Nothing is modified; the transcript is read as it is, which inside a live
    session means up to the tool call that asked for the export.
    """
    session_id = valid_session_id(session_id)
    cfg = _resolve_config_dir(config_dir)
    candidates = find_transcripts(cfg, session_id)
    if cwd is not None:
        preferred = cfg / "projects" / project_key(cwd) / f"{session_id}.jsonl"
        candidates = [p for p in candidates if p == preferred] or candidates
    if not candidates:
        raise CapsuleError(f"session {session_id} not found under {cfg / 'projects'}")
    if len(candidates) > 1:
        listing = "\n  ".join(str(p) for p in candidates)
        raise CapsuleError(
            f"session {session_id} exists under several project keys; pass the working "
            f"directory it belongs to:\n  {listing}"
        )
    transcript = candidates[0]
    project_dir = transcript.parent
    sidecar = project_dir / session_id

    files: list[dict[str, Any]] = []

    def add(path: Path, relative: str) -> None:
        data = path.read_bytes()
        files.append({
            "relative_destination": relative,
            "bytes": len(data),
            "sha256": _sha256(data),
            "encoding": ENCODING,
            "data": encode_bytes(data),
        })

    add(transcript, f"{session_id}.jsonl")
    if sidecar.is_dir():
        for path in sorted(sidecar.rglob("*")):
            if path.is_file() and not path.is_symlink():
                add(path, f"{session_id}/{path.relative_to(sidecar).as_posix()}")

    meta = transcript_metadata(transcript)
    return {
        "capsule_version": CAPSULE_VERSION,
        "source_harness": {"name": HARNESS, "version": meta["version"]},
        "session_id": session_id,
        "project_key": project_dir.name,
        "original_working_directory": meta["cwd"],
        "git_branch": meta["git_branch"],
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": {k: meta[k] for k in ("records", "user_messages", "assistant_messages")},
        "files": files,
    }


def capsule_size(capsule: dict[str, Any]) -> int:
    """Uncompressed bytes a capsule carries — what lands on disk on import."""
    return sum(int(f.get("bytes") or 0) for f in capsule.get("files", []))


def _write_private(path: Path, data: bytes) -> None:
    """Create or replace ``path`` owner-read/write from the first byte.

    ``write_bytes`` honours the umask, and 0o022 is the common one, so a
    transcript would be world-readable for the moment before a chmod. Claude
    Code writes its own files 0o600; the export must not be the step that
    loosens that.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # the file may have existed with a wider mode
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _record_count_of(entry: dict[str, Any]) -> int:
    return count_records(decode_entry(entry))


def write_capsule(capsule: dict[str, Any], path: str | os.PathLike[str]) -> Path:
    """Save a capsule to a file as private as the transcript it carries."""
    target = Path(path)
    _write_private(target, json.dumps(capsule, separators=(",", ":")).encode())
    return target


def read_capsule(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            capsule = json.load(fh)
    except ValueError as exc:
        raise CapsuleError(f"{path} is not a capsule: {exc}") from exc
    return validate(capsule)


# ------------------------------------------------------------- validate ---

def validate(capsule: Any) -> dict[str, Any]:
    """Check a capsule's shape and path safety before trusting any of it.

    A capsule arrives from another agent, so ``relative_destination`` is an
    attacker-controlled string that is about to become a filesystem path under
    the receiver's config dir. Every entry must live at ``<id>.jsonl`` or under
    ``<id>/``; anything else is refused, whatever it claims to be.
    """
    if not isinstance(capsule, dict):
        raise CapsuleError("capsule is not a JSON object")
    if capsule.get("capsule_version") != CAPSULE_VERSION:
        raise CapsuleError(f"unsupported capsule_version: {capsule.get('capsule_version')!r}")
    harness = capsule.get("source_harness")
    if not isinstance(harness, dict) or harness.get("name") != HARNESS:
        raise CapsuleError(f"not a {HARNESS} capsule: source_harness={harness!r}")
    session_id = valid_session_id(str(capsule.get("session_id", "")))
    for field in ("project_key", "original_working_directory", "git_branch"):
        value = capsule.get(field)
        if value is not None and not isinstance(value, str):
            raise CapsuleError(f"capsule {field} is not a string: {value!r}")
    files = capsule.get("files")
    if not isinstance(files, list) or not files:
        raise CapsuleError("capsule carries no files")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise CapsuleError("capsule file entry is not an object")
        rel = entry.get("relative_destination")
        if not isinstance(rel, str) or not rel:
            raise CapsuleError("capsule file entry has no relative_destination")
        parts = rel.split("/")
        if (
            rel.startswith("/")
            or "\\" in rel
            or any(part in ("", ".", "..") for part in parts)
            or not (rel == f"{session_id}.jsonl" or parts[0] == session_id)
        ):
            raise CapsuleError(f"unsafe relative_destination in capsule: {rel!r}")
        if rel in seen:
            raise CapsuleError(f"duplicate file in capsule: {rel!r}")
        seen.add(rel)
        if entry.get("encoding", "base64") not in _ENCODINGS:
            raise CapsuleError(f"unknown capsule file encoding: {entry.get('encoding')!r}")
        if not isinstance(entry.get("sha256"), str) or not isinstance(entry.get("data"), str):
            raise CapsuleError(f"capsule file {rel!r} lacks sha256 or data")
        size = entry.get("bytes")
        if not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
            raise CapsuleError(f"capsule file {rel!r} declares an unusable size: {size!r}")
    if f"{session_id}.jsonl" not in seen:
        raise CapsuleError("capsule has no main transcript")
    return capsule


def iter_files(capsule: dict[str, Any]) -> Iterator[tuple[str, bytes]]:
    """``(relative_destination, bytes)`` for every file, each hash-verified."""
    for entry in validate(capsule)["files"]:
        yield entry["relative_destination"], decode_entry(entry)


def summary(capsule: dict[str, Any]) -> dict[str, Any]:
    """The metadata half of a capsule, for logs, results and pointers."""
    return {
        "session_id": capsule.get("session_id"),
        "harness": (capsule.get("source_harness") or {}).get("name"),
        "harness_version": (capsule.get("source_harness") or {}).get("version"),
        "project_key": capsule.get("project_key"),
        "original_working_directory": capsule.get("original_working_directory"),
        "git_branch": capsule.get("git_branch"),
        "exported_at": capsule.get("exported_at"),
        "files": len(capsule.get("files") or []),
        "bytes": capsule_size(capsule),
        "stats": capsule.get("stats"),
    }


# -------------------------------------------------------------- install ---

def install(
    capsule: dict[str, Any],
    *,
    config_dir: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Write a capsule's files where ``claude --resume`` will find them.

    With ``cwd`` the files go under that directory's project key, which resume
    checks first and which is immune to copies elsewhere; without it they go
    under the capsule's original key, where resume's fallback scan finds them
    from any directory. Either way the id ends up under exactly one key: the
    scan refuses to pick between duplicates, so an existing copy under a
    *different* key is an error unless ``force`` says otherwise.

    Files that would be overwritten with different content are renamed to
    ``<name>.bak-<utc stamp>`` first; identical content is left alone. Modes
    are ``0o700``/``0o600``, matching what Claude Code itself uses.
    """
    validate(capsule)
    session_id = capsule["session_id"]
    cfg = _resolve_config_dir(config_dir)
    local = find_transcripts(cfg, session_id)
    if cwd is not None:
        dest_key = project_key(cwd)
        resume_cwd: str | None = os.path.abspath(os.fspath(cwd))
    elif len(local) == 1:
        # The session has been here before — the return leg of a round trip,
        # typically — so home is where it already is, and resume from where
        # it was last driven here, not from the sender's directory.
        dest_key = local[0].parent.name
        resume_cwd = transcript_metadata(local[0])["cwd"]
    else:
        dest_key = str(capsule.get("project_key") or "")
        if not dest_key or "/" in dest_key or dest_key in (".", ".."):
            raise CapsuleError(f"capsule project_key cannot name a directory: {dest_key!r}")
        resume_cwd = capsule.get("original_working_directory")
    dest_dir = cfg / "projects" / dest_key

    if not force and (session_id == current_session_id() or session_id in live_session_ids(cfg)):
        raise CapsuleError(
            f"session {session_id} is running on this machine right now; installing over a "
            f"live transcript would corrupt it. Stop that session first, or import with force"
        )
    elsewhere = [p for p in local if p.parent != dest_dir]
    if elsewhere and not force:
        listing = "\n  ".join(str(p) for p in elsewhere)
        raise CapsuleError(
            f"session {session_id} already exists under other project keys, so resume by id "
            f"would be ambiguous:\n  {listing}\nimport with force to add another copy anyway"
        )
    existing = dest_dir / f"{session_id}.jsonl"
    if existing.is_file() and not force:
        incoming = next(e for e in capsule["files"]
                        if e["relative_destination"] == f"{session_id}.jsonl")
        current = existing.read_bytes()
        local, remote = count_records(current), _record_count_of(incoming)
        if local > remote and current != decode_entry(incoming):
            raise CapsuleError(
                f"the transcript already here has {local} records and the capsule only "
                f"{remote}: this side is ahead, and installing would roll it back. Hand the "
                f"session the other way, or import with force to keep the capsule's copy"
            )

    written: list[str] = []
    unchanged: list[str] = []
    backed_up: list[str] = []
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative, data in iter_files(capsule):
        target = dest_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if target.read_bytes() == data:
                unchanged.append(str(target))
                continue
            backup = target.with_name(f"{target.name}.bak-{stamp}")
            target.replace(backup)
            backed_up.append(str(backup))
        _write_private(target, data)
        written.append(str(target))

    return {
        "session_id": session_id,
        "config_dir": str(cfg),
        "project_dir": str(dest_dir),
        "transcript": str(dest_dir / f"{session_id}.jsonl"),
        "written": written,
        "unchanged": unchanged,
        "backed_up": backed_up,
        "resume_cwd": resume_cwd,
        "resume": shell_resume_command(session_id, cwd=resume_cwd, config_dir=cfg),
    }


# --------------------------------------------------------------- resume ---

def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """An environment a child ``claude`` may safely start from."""
    env = dict(os.environ if base is None else base)
    for name in CHILD_UNSET:
        env.pop(name, None)
    return env


def resume_argv(session_id: str, *, background: bool = False) -> list[str]:
    argv = ["claude"]
    if background:
        argv.append("--bg")
    argv += ["--resume", valid_session_id(session_id)]
    return argv


def shell_resume_command(
    session_id: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    background: bool = False,
) -> str:
    """The one line a human runs to pick the session up, for messages and docs."""
    # Quoted: the directory came from a capsule somebody else built, and the
    # line is meant to be pasted into a shell.
    parts: list[str] = []
    if cwd:
        parts.append(f"cd {shlex.quote(os.fspath(cwd))} &&")
    explicit = os.fspath(config_dir) if config_dir else os.environ.get(CONFIG_DIR_VAR)
    if explicit and Path(explicit).expanduser() != Path.home() / ".claude":
        parts.append(f"{CONFIG_DIR_VAR}={shlex.quote(explicit)}")
    parts.append(" ".join(resume_argv(session_id, background=background)))
    return " ".join(parts)


def claude_available() -> bool:
    return shutil.which("claude") is not None


def spawn_resume(
    session_id: str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    background: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Start ``claude --resume`` as a separate process and report what happened.

    ``background=True`` uses Claude Code's own ``--bg`` mode, which returns at
    once and leaves a session that ``claude attach <id>`` opens later; that is
    the only form a tool can offer, since a tool has no terminal to hand over.
    ``background=False`` is for the CLI, which waits for the interactive
    session to end. Returns data rather than raising when ``claude`` is
    missing: the capsule is installed either way and the human can run the
    command themselves.
    """
    session_id = valid_session_id(session_id)
    argv = resume_argv(session_id, background=background)
    cfg = _resolve_config_dir(config_dir)
    env = child_env()
    env[CONFIG_DIR_VAR] = str(cfg)
    command = shell_resume_command(session_id, cwd=cwd, config_dir=cfg, background=background)
    out: dict[str, Any] = {"session_id": session_id, "started": False, "command": command}
    # `claude --bg --resume` of an id it cannot find exits 0 and starts an
    # empty session that evaporates, so check what resume will find first.
    copies = find_transcripts(cfg, session_id)
    if not copies:
        return {**out, "reason": f"no transcript for {session_id} under {cfg / 'projects'}"}
    if len(copies) > 1 and not (cwd and (cfg / "projects" / project_key(cwd)) in
                                {c.parent for c in copies}):
        return {**out, "reason": "the id exists under several project keys and the working "
                                 "directory matches none of them; resume would refuse to choose"}
    if not claude_available():
        return {**out, "reason": "claude is not on PATH"}
    run_cwd = os.fspath(cwd) if cwd else None
    if run_cwd and not os.path.isdir(run_cwd):
        return {**out, "reason": f"no such directory: {run_cwd}"}
    try:
        if not background:
            done = subprocess.run(argv, cwd=run_cwd, env=env)
            return {**out, "started": True, "returncode": done.returncode}
        done = subprocess.run(
            argv, cwd=run_cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**out, "reason": str(exc)}
    output = (done.stdout + done.stderr).strip()
    out.update({"returncode": done.returncode, "output": output})
    # What --bg prints: "backgrounded · <short id>" and, when nothing was
    # loaded, "(idle — send a prompt to start)". The short id is what
    # `claude attach` takes.
    short = _short_id(output)
    if done.returncode != 0 or short is None:
        return {**out, "reason": "claude did not report a backgrounded session"}
    if "idle" in output.lower():
        return {**out, "reason": "claude started an empty session instead of resuming; "
                                 "the transcript was not where it looked"}
    return {**out, "started": True, "short_id": short, "attach": f"claude attach {short}"}


def _short_id(output: str) -> str | None:
    for line in output.splitlines():
        if line.startswith("backgrounded"):
            parts = line.replace("·", " ").split()
            if len(parts) >= 2:
                return parts[1]
    return None
