"""Registering a handed-off session with the Claude Code desktop app.

`session receive` installs a transcript where `claude --resume` finds it, and
for a CLI user that is the whole job. For someone working in the desktop app it
is half of one: the app keeps its own list of sessions beside the transcripts,
and a conversation whose transcript exists but whose record does not is invisible
there. On 2026-09-04 a session handed from a cloud agent to a laptop resumed
perfectly on the command line and could not be found in the app the recipient
actually uses, which is what this module exists to close.

**Everything that knows about the app's private format lives here**, and nothing
else in the package imports its details. That containment is the point: this is
an undocumented store belonging to another program, and it can change under us
without notice. So every function here answers "not here" rather than raising
when the shape is not what it expects, and `VERIFIED_AGAINST` records the app
version the layout was actually read on.

What was established by reading the store, not by guessing:

- The app loads sessions by listing `<store>/<account>/<project>/`, keeping every
  `local_*.json` it can parse, keyed by the record's own `sessionId`. There is no
  index, manifest or checksum, so a well-formed file in that directory is enough.
- It reads that directory **only at launch**. A record written while the app runs
  appears at the next restart, and callers are expected to say so.
- Account and project are opaque uuids the app assigns. The project cannot be
  derived from a path, so a repo the app has never opened has nowhere for its
  record to go — `discover` returns None and the caller skips rather than
  inventing a directory.
- The app never writes to a record it does not already hold in memory, which is
  why adding a file while it is running is safe.

Two mistakes here fail *silently* rather than loudly, and both are guarded below:
a `cliSessionId` carrying the `local_` prefix gives a row that opens empty, and a
filename whose stem disagrees with the `sessionId` field is orphaned the moment
the app next saves that session.
"""

from __future__ import annotations

import json
import os
import platform
import re
import uuid
from pathlib import Path
from typing import Any

#: The app version whose store layout this was read against. Kept so a future
#: failure can be dated, and so `switchboard session register` can say what it
#: is assuming rather than pretending the format is documented.
VERIFIED_AGAINST = "2.1.259"

#: Set in a repo's gitignored settings by `switchboard init`, which asks once.
#: Registration is opt-in because it writes into another application's store,
#: and that is not a thing to do to somebody's machine unasked.
ENABLE_VAR = "SWITCHBOARD_DESKTOP_REGISTER"

#: Overrides the store location, for tests and for anyone whose app is not where
#: it usually is.
STORE_VAR = "SWITCHBOARD_DESKTOP_STORE"

_STORE = "Library/Application Support/Claude/claude-code-sessions"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: The app's own prefix for a record file and for the id inside it.
_PREFIX = "local_"
#: A record larger than this is skipped by the app's loader, so writing one
#: would be writing a file that is never read.
_MAX_RECORD_BYTES = 10 * 1024 * 1024


class DesktopError(Exception):
    """A registration that cannot be done — never raised for "no app here"."""


def store_dir(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Where the desktop app keeps its session records, or None.

    None is the ordinary answer on a cloud runner, in CI, and on any machine
    without the app: this feature is additive, and its absence is not an error.
    """
    if explicit is not None:
        return Path(explicit)
    from_env = os.environ.get(STORE_VAR)
    if from_env:
        return Path(from_env)
    if platform.system() != "Darwin":
        return None
    path = Path.home() / _STORE
    return path if path.is_dir() else None


def enabled(env: dict[str, str] | None = None) -> bool:
    """Whether this checkout opted in at `init` time."""
    source = os.environ if env is None else env
    return (source.get(ENABLE_VAR) or "").strip().lower() in ("1", "true", "yes", "on")


def _records(store: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every record the app would load, with the unreadable ones dropped.

    Mirrors the app's own tolerance: a file it cannot parse is skipped rather
    than fatal, so a corrupt record elsewhere in the store cannot stop us.
    """
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(store.glob(f"*/*/{_PREFIX}*.json")):
        try:
            if path.stat().st_size > _MAX_RECORD_BYTES:
                continue
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("sessionId"):
            out.append((path, value))
    return out


def discover(cwd: str | os.PathLike[str], *,
             store: str | os.PathLike[str] | None = None) -> dict[str, str] | None:
    """The account and project directory the app files this repo's sessions under.

    Both are uuids the app assigns; neither is derivable from the path, so the
    only way to learn them is to find a session the app already keeps for this
    directory. A repo the app has never opened therefore has no home here, and
    the honest answer is None — inventing a project directory would create a
    folder the app has no reason to associate with anything.

    Matched on ``originCwd`` before ``cwd`` so that a repo whose sessions all
    ran in worktrees still resolves to the repo itself.
    """
    root = store_dir(store)
    if root is None or not root.is_dir():
        return None
    want = os.path.abspath(os.fspath(cwd)).rstrip("/")
    for path, record in _records(root):
        for key in ("originCwd", "cwd"):
            value = record.get(key)
            if isinstance(value, str) and value.rstrip("/") == want:
                return {
                    "account": path.parent.parent.name,
                    "project": path.parent.name,
                    "project_dir": str(path.parent),
                }
    return None


def find(cli_session_id: str, *,
         store: str | os.PathLike[str] | None = None) -> Path | None:
    """The record pointing at this transcript, if the app already has one."""
    root = store_dir(store)
    if root is None or not root.is_dir():
        return None
    for path, record in _records(root):
        if record.get("cliSessionId") == cli_session_id:
            return path
    return None


def _template(*, session_id: str, cli_session_id: str, cwd: str, title: str,
              model: str, created_at: int, last_activity_at: int) -> dict[str, Any]:
    """The record, in the shape the app's own smallest records take.

    Twelve fields, every one of which appears in every record the app has
    written for a session with no worktree. Deliberately no more than that: an
    unknown field is dropped the first time the app re-saves the session, so
    inventing one buys nothing and misleads the next reader.
    """
    return {
        "sessionId": session_id,
        "cliSessionId": cli_session_id,
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": created_at,
        "lastActivityAt": last_activity_at,
        "model": model,
        "isArchived": False,
        "title": title,
        "permissionMode": "default",
        "enabledMcpTools": {},
        "remoteMcpServersConfig": [],
    }


def register(
    cli_session_id: str,
    *,
    cwd: str | os.PathLike[str],
    title: str,
    model: str = "claude-opus-5",
    created_at: int,
    last_activity_at: int,
    store: str | os.PathLike[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Give the desktop app a row for an already-installed transcript.

    Returns a report rather than raising for the ordinary "cannot" cases — no
    app, no project directory, already registered — because a handoff that
    installed correctly must not be reported as failed when only the sidebar
    row is missing. `DesktopError` is reserved for a caller passing something
    that cannot be right.

    The two guards that matter are enforced here rather than left to callers:
    `cliSessionId` must be a bare uuid, since the `local_` prefix yields a row
    that opens empty, and the file's stem must equal the `sessionId` field, or
    the app keys it in memory by the field and writes every later save to a
    different path.
    """
    if not _UUID.match(cli_session_id):
        raise DesktopError(
            f"{cli_session_id!r} is not a bare session uuid. The app's own record ids "
            f"carry a {_PREFIX!r} prefix and its transcript ids do not; using the "
            f"prefixed form here produces a row that opens empty."
        )
    for name, stamp in (("created_at", created_at), ("last_activity_at", last_activity_at)):
        if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 10 ** 12:
            raise DesktopError(
                f"{name} must be epoch MILLISECONDS as an int (got {stamp!r}); seconds "
                f"sort the row to 1970"
            )

    root = store_dir(store)
    if root is None or not root.is_dir():
        return {"registered": False, "reason": "no Claude Code desktop app on this machine"}

    existing = find(cli_session_id, store=root)
    if existing is not None:
        return {"registered": False, "reason": "already registered", "record": str(existing)}

    where = discover(cwd, store=root)
    if where is None:
        return {
            "registered": False,
            "reason": (f"the desktop app has no project for {os.fspath(cwd)} — open that "
                       f"directory in it once, then register again"),
        }

    project = Path(where["project_dir"])
    taken = {path.stem for path, _ in _records(root)}
    chosen = session_id or ""
    if chosen and not chosen.startswith(_PREFIX):
        chosen = _PREFIX + chosen
    while not chosen or chosen in taken or (project / f"{chosen}.json").exists():
        chosen = _PREFIX + str(uuid.uuid4())

    record = _template(
        session_id=chosen, cli_session_id=cli_session_id,
        cwd=os.path.abspath(os.fspath(cwd)), title=title, model=model,
        created_at=created_at, last_activity_at=max(last_activity_at, created_at),
    )
    target = project / f"{chosen}.json"
    if target.stem != record["sessionId"]:  # pragma: no cover - defensive
        raise DesktopError("filename and sessionId disagree; refusing to orphan the record")

    text = json.dumps(record, indent=1)
    try:
        target.write_text(text)
        target.chmod(0o644)
        json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        try:
            target.unlink()
        except OSError:
            pass
        return {"registered": False, "reason": f"could not write the record ({exc})"}

    return {
        "registered": True,
        "record": str(target),
        "session_id": chosen,
        "verified_against": VERIFIED_AGAINST,
        "note": ("the app reads this directory only at launch, so the session appears "
                 "after it is next restarted"),
    }


def unregister(cli_session_id: str, *,
               store: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Remove the row, leaving the transcript alone.

    The safe undo, and the reason it exists as a command: deleting or archiving
    the session from inside the app writes a `.desktop-released.json` marker
    next to the transcript which the CLI acts on, and a user tidying up a row
    they no longer want has no reason to expect that.
    """
    path = find(cli_session_id, store=store)
    if path is None:
        return {"unregistered": False, "reason": "no desktop record points at that session"}
    try:
        path.unlink()
    except OSError as exc:
        return {"unregistered": False, "reason": f"could not remove the record ({exc})"}
    return {"unregistered": True, "record": str(path)}
