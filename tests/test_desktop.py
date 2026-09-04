"""Registering a handed-off session with the Claude Code desktop app.

Every test runs against a fake store under `tmp_path`, never the real one: this
module writes into another program's directory, and a suite that practised on
the developer's own app would be a suite nobody could run twice.

What is pinned here is mostly what fails *silently*. A `cliSessionId` carrying
the app's `local_` prefix produces a row that opens empty; a filename whose stem
disagrees with the `sessionId` field is orphaned the moment the app next saves
that session. Neither raises anything at the time, and both were found by reading
the app's loader rather than by anything going wrong — so they are asserted here,
where a future change to this module has to keep passing them.
"""

from __future__ import annotations

import json

import pytest

from switchboard import desktop

CLI_ID = "9c91c9ee-23cf-5840-9f5d-8595e20fa78f"
ACCOUNT = "3aa45cbd-39c1-4da0-b5c2-983ab5d7a876"
PROJECT = "57064d6d-3004-419c-a56d-af92d9d01176"
REPO = "/Users/somebody/Projects/switchboard"
MS = 1788502753107


def _store(tmp_path, *, sessions=((REPO, "local_1111"),)):
    """A store shaped like the app's own: <store>/<account>/<project>/local_*.json."""
    root = tmp_path / "claude-code-sessions"
    project = root / ACCOUNT / PROJECT
    project.mkdir(parents=True)
    for cwd, sid in sessions:
        (project / f"{sid}.json").write_text(json.dumps({
            "sessionId": sid, "cliSessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "cwd": cwd, "originCwd": cwd, "createdAt": MS, "lastActivityAt": MS,
            "model": "claude-opus-5", "isArchived": False, "title": "existing",
            "permissionMode": "default", "enabledMcpTools": {}, "remoteMcpServersConfig": [],
        }))
    return root


# --- knowing when not to act -------------------------------------------------

def test_no_app_is_not_an_error(tmp_path, monkeypatch):
    """A cloud runner has no desktop app, and a handoff there must not report a
    failure for a sidebar that does not exist."""
    monkeypatch.setenv(desktop.STORE_VAR, str(tmp_path / "absent"))
    report = desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS,
                              last_activity_at=MS)
    assert report == {"registered": False,
                      "reason": "no Claude Code desktop app on this machine"}


def test_a_repo_the_app_never_opened_has_nowhere_to_file_the_row(tmp_path, monkeypatch):
    """The project directory is a uuid the app assigns; it cannot be derived from
    a path. Inventing one would make a folder the app has no reason to read, so
    the honest answer is to say so."""
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    report = desktop.register(CLI_ID, cwd="/Users/somebody/Projects/elsewhere",
                              title="t", created_at=MS, last_activity_at=MS)
    assert report["registered"] is False
    assert "no project for" in report["reason"]


def test_registration_is_opt_in(monkeypatch):
    monkeypatch.delenv(desktop.ENABLE_VAR, raising=False)
    assert desktop.enabled() is False
    for yes in ("1", "true", "YES", "on"):
        assert desktop.enabled({desktop.ENABLE_VAR: yes}) is True
    assert desktop.enabled({desktop.ENABLE_VAR: "0"}) is False


# --- the two silent failures -------------------------------------------------

def test_a_prefixed_session_id_is_refused(tmp_path, monkeypatch):
    """The app's record ids carry `local_` and its transcript ids do not. Passing
    the prefixed form writes a row that opens empty — no error, just nothing."""
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    with pytest.raises(desktop.DesktopError, match="bare session uuid"):
        desktop.register(f"local_{CLI_ID}", cwd=REPO, title="t", created_at=MS,
                         last_activity_at=MS)


def test_the_filename_always_matches_the_session_id_field(tmp_path, monkeypatch):
    """The app keys its in-memory map by the field and writes every later save to
    the path derived from it, so a disagreement orphans the file."""
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    report = desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS,
                              last_activity_at=MS)
    assert report["registered"] is True
    written = json.loads(open(report["record"]).read())
    assert report["record"].endswith(f"{written['sessionId']}.json")


def test_seconds_instead_of_milliseconds_are_refused(tmp_path, monkeypatch):
    """Every timestamp in the app's own records is epoch ms; seconds sort the row
    to 1970 rather than failing."""
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    with pytest.raises(desktop.DesktopError, match="MILLISECONDS"):
        desktop.register(CLI_ID, cwd=REPO, title="t", created_at=1788502753,
                         last_activity_at=1788502753)


# --- the record itself -------------------------------------------------------

def test_the_record_matches_the_shape_the_app_writes(tmp_path, monkeypatch):
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    report = desktop.register(CLI_ID, cwd=REPO, title="handed off",
                              created_at=MS, last_activity_at=MS + 5)
    record = json.loads(open(report["record"]).read())
    assert set(record) == {
        "sessionId", "cliSessionId", "cwd", "originCwd", "createdAt", "lastActivityAt",
        "model", "isArchived", "title", "permissionMode", "enabledMcpTools",
        "remoteMcpServersConfig",
    }
    assert record["cliSessionId"] == CLI_ID and not record["cliSessionId"].startswith("local_")
    assert record["sessionId"].startswith("local_")
    assert record["cwd"] == record["originCwd"] == REPO
    assert record["isArchived"] is False and record["title"] == "handed off"
    # The row is filed with the sessions the app already keeps for this repo.
    assert report["record"].startswith(str(tmp_path / "claude-code-sessions" / ACCOUNT / PROJECT))
    # And the caller is told the thing it cannot see: nothing appears until a restart.
    assert "launch" in report["note"]


def test_last_activity_never_precedes_creation(tmp_path, monkeypatch):
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    report = desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS,
                              last_activity_at=MS - 9999)
    record = json.loads(open(report["record"]).read())
    assert record["lastActivityAt"] >= record["createdAt"]


def test_a_session_is_registered_once(tmp_path, monkeypatch):
    """Two rows for one transcript would be two sidebar entries opening the same
    conversation, and the second would append to it under a different id."""
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    first = desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS, last_activity_at=MS)
    again = desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS, last_activity_at=MS)
    assert first["registered"] is True
    assert again == {"registered": False, "reason": "already registered",
                     "record": first["record"]}


def test_an_invented_id_never_collides(tmp_path, monkeypatch):
    """The filename is the identity: colliding with an existing record would
    clobber a real session of the user's."""
    root = _store(tmp_path)
    monkeypatch.setenv(desktop.STORE_VAR, str(root))
    taken = {p.stem for p in root.rglob("local_*.json")}
    report = desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS, last_activity_at=MS)
    assert report["session_id"] not in taken


# --- finding and undoing -----------------------------------------------------

def test_find_and_unregister_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    report = desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS, last_activity_at=MS)
    assert str(desktop.find(CLI_ID)) == report["record"]
    assert desktop.unregister(CLI_ID) == {"unregistered": True, "record": report["record"]}
    assert desktop.find(CLI_ID) is None
    assert desktop.unregister(CLI_ID)["unregistered"] is False


def test_unregister_leaves_the_transcript_alone(tmp_path, monkeypatch):
    """The whole reason the command exists: removing the row from inside the app
    writes a release marker the CLI acts on, and this must not."""
    monkeypatch.setenv(desktop.STORE_VAR, str(_store(tmp_path)))
    transcript = tmp_path / "projects" / f"{CLI_ID}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS, last_activity_at=MS)
    desktop.unregister(CLI_ID)
    assert transcript.read_text() == "{}\n"
    assert not list(transcript.parent.glob("*.desktop-released.json"))


# --- reading a store that is not perfect -------------------------------------

def test_an_unreadable_record_elsewhere_does_not_stop_us(tmp_path, monkeypatch):
    """The app skips a record it cannot parse rather than failing; so must we, or
    one corrupt file anywhere in the store would block every registration."""
    root = _store(tmp_path)
    (root / ACCOUNT / PROJECT / "local_broken.json").write_text("{not json")
    monkeypatch.setenv(desktop.STORE_VAR, str(root))
    assert desktop.discover(REPO) is not None
    assert desktop.register(CLI_ID, cwd=REPO, title="t", created_at=MS,
                            last_activity_at=MS)["registered"] is True


def test_discover_matches_the_repo_even_when_its_sessions_ran_in_worktrees(tmp_path,
                                                                          monkeypatch):
    """Most real sessions have cwd inside a worktree and originCwd at the repo
    root; matching only on cwd would find nothing for the repo itself."""
    root = tmp_path / "claude-code-sessions"
    project = root / ACCOUNT / PROJECT
    project.mkdir(parents=True)
    (project / "local_wt.json").write_text(json.dumps({
        "sessionId": "local_wt", "cliSessionId": "11111111-2222-3333-4444-555555555555",
        "cwd": f"{REPO}/.claude/worktrees/some-branch", "originCwd": REPO,
        "createdAt": MS, "lastActivityAt": MS, "model": "claude-opus-5",
        "isArchived": False, "title": "in a worktree", "permissionMode": "default",
        "enabledMcpTools": {}, "remoteMcpServersConfig": [],
    }))
    monkeypatch.setenv(desktop.STORE_VAR, str(root))
    assert desktop.discover(REPO) == {
        "account": ACCOUNT, "project": PROJECT, "project_dir": str(project)}
