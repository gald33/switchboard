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
def clean_switchboard_env(monkeypatch):
    for name in _SWITCHBOARD_ENV:
        monkeypatch.delenv(name, raising=False)
    # `init` only prompts when it detects a terminal, and never under CI. Tests
    # inherit neither, but be explicit: a test that blocks on input that never
    # arrives hangs the suite rather than failing it.
    monkeypatch.delenv("CI", raising=False)
