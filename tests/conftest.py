"""Shared fixtures.

The suite drives the CLI in-process, and the CLI reads its connection
settings from the environment. That makes the developer's own environment an
input to every test — and `switchboard init` exists precisely to *set* those
variables, so anyone who has run it against a real repo has them exported.
Tests then read the developer's hub instead of the tmp_path repo they just
built, and fail only on their machine. Clear them once, globally, rather than
per test.
"""

from __future__ import annotations

import os
import tempfile

import pytest

_SWITCHBOARD_ENV = (
    "SWITCHBOARD_URL",
    "SWITCHBOARD_TOKEN",
    "SWITCHBOARD_WORKSPACE",
    "SWITCHBOARD_KEY",
    "SWITCHBOARD_DB",
    "SWITCHBOARD_KEYS_FILE",
    "SWITCHBOARD_AGENT_ID",
    "SWITCHBOARD_SESSION_ID",
    "SWITCHBOARD_KEY_EPOCH_PERIOD",
)

#: Session identifiers the agent id is derived from. Cleared for the same
#: reason as the rest: a developer running the suite inside an editor session
#: would otherwise have that session's id folded into every derived agent id,
#: so a test asserting on identity passes or fails depending on where it ran.
_SESSION_ENV = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_HOST_SESSION_ID",
    "TERM_SESSION_ID",
)


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch):
    """Fail module-level httpx calls rather than letting a test reach a hub.

    `switchboard init` contacts the hub to check that its workspace is
    actually reachable, and its default URL is the real managed hub — so
    without this the suite would talk to production. Tests that exercise that
    path patch these back with fakes of their own.

    Only the module-level helpers are blocked. Starlette's TestClient is an
    ``httpx.Client`` subclass and goes through instance methods, so the API
    tests are unaffected.
    """
    import httpx

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("outbound HTTP is disabled in tests")

    monkeypatch.setattr(httpx, "get", refuse)
    monkeypatch.setattr(httpx, "post", refuse)


@pytest.fixture(autouse=True)
def clean_switchboard_env(monkeypatch, tmp_path):
    for name in _SWITCHBOARD_ENV + _SESSION_ENV:
        monkeypatch.delenv(name, raising=False)
    # The book of known rooms is per machine and written by ordinary commands
    # (`init`, `join`, `--invite`), so without this every CLI test would leave
    # its throwaway rooms in the developer's own ~/.switchboard. One file per
    # test; subprocesses inherit it through the environment.
    monkeypatch.setenv("SWITCHBOARD_KNOWN_ROOMS", str(tmp_path / "known-rooms.json"))
    # Session capsules are read from and written to Claude Code's own config
    # dir, which defaults to the developer's real ~/.claude. Point every test at
    # a throwaway one so an import test can never land a transcript there.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    # `init` only prompts when it detects a terminal, and never under CI. Tests
    # inherit neither, but be explicit: a test that blocks on input that never
    # arrives hangs the suite rather than failing it.
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture(autouse=True, scope="session")
def isolated_signing_socket_dir():
    """Give this pytest process its own directory for signing sockets.

    Same failure as the environment variables above, one layer down: ambient
    machine state leaking into every test. `Client.__init__` calls
    `signing.attach(agent_id)`, which adopts **whatever socket already exists**
    at `signing.socket_path(agent_id)` — a path derived from the agent id
    alone, under one per-user temp dir. Nothing in it distinguishes one process
    from another, and that is deliberate for real agents: the socket is how the
    hooks, the CLI and the model sign as one agent rather than three strangers.

    It is wrong for tests. Every suite invents agents called `alice` and `bob`,
    so two pytest processes on one machine compute the *same* socket path, and
    a client in one silently attaches to the signer in the other — inheriting a
    foreign identity. What that looks like is a whisper that cannot be opened:
    `unreadable`, in whichever whisper test happens to race, which is why it
    presented as two different tests failing rather than one.

    It cost a day to find precisely because the conditions hide it: never in
    CI, where a runner has one suite; never in a sequential rerun, which is the
    first thing anyone tries; only on a developer's machine running two suites
    at once — or one running an MCP server for an agent a test also names.

    Session-scoped and `os.environ` rather than `monkeypatch`, which is
    function-scoped and cannot hold an environment variable for a whole
    session. Kept short (the system temp dir, not `tmp_path_factory`) because a
    unix socket path is capped near 104 bytes and a long prefix would make
    `SigningServer.start()` fail with `OSError` — degrading tests to "no socket
    available", which is a different code path and would quietly stop testing
    this one.
    """
    base = tempfile.mkdtemp(prefix="swb-")  # short: the socket path is capped
    previous = os.environ.get("XDG_RUNTIME_DIR")
    os.environ["XDG_RUNTIME_DIR"] = base
    try:
        yield base
    finally:
        if previous is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = previous
