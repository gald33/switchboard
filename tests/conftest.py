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

import pytest

_SWITCHBOARD_ENV = (
    "SWITCHBOARD_URL",
    "SWITCHBOARD_TOKEN",
    "SWITCHBOARD_WORKSPACE",
    "SWITCHBOARD_KEY",
    "SWITCHBOARD_DB",
    "SWITCHBOARD_KEYS_FILE",
    "SWITCHBOARD_AGENT_ID",
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
def clean_switchboard_env(monkeypatch):
    for name in _SWITCHBOARD_ENV:
        monkeypatch.delenv(name, raising=False)
    # `init` only prompts when it detects a terminal, and never under CI. Tests
    # inherit neither, but be explicit: a test that blocks on input that never
    # arrives hangs the suite rather than failing it.
    monkeypatch.delenv("CI", raising=False)
