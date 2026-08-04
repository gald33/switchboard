"""Tests for `switchboard register-key`, which calls out to a hub over HTTP.

`cmd_register_key` imports httpx locally and calls `httpx.post` directly, so
these tests patch `httpx.post` itself rather than going through a real
server — the endpoint's own behavior is already covered by test_auth.py.
"""

from __future__ import annotations

import httpx

from switchboard.cli import MANAGED_HUB_URL, main


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def test_successful_registration_prints_token(capsys, monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse(200, {"workspace": json["workspace"]})

    monkeypatch.setattr("httpx.post", fake_post)
    code = main(["--workspace", "acme/app", "register-key"])
    assert code == 0
    out = capsys.readouterr().out.strip()
    # stdout is just the bare token, one line, so scripts can capture it directly.
    assert out and "\n" not in out
    assert captured["url"] == f"{MANAGED_HUB_URL}/keys/register"
    assert captured["json"] == {"workspace": "acme/app"}
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def test_json_output_includes_token_workspace_and_url(capsys, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return FakeResponse(200, {"workspace": json["workspace"]})

    monkeypatch.setattr("httpx.post", fake_post)
    code = main([
        "--json", "--workspace", "acme/app", "--url", "https://hub.example", "register-key",
    ])
    assert code == 0
    import json as jsonlib

    payload = jsonlib.loads(capsys.readouterr().out)
    assert payload["workspace"] == "acme/app"
    assert payload["url"] == "https://hub.example"
    assert isinstance(payload["token"], str) and len(payload["token"]) > 16


def test_network_error_is_a_clean_failure(capsys, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.post", fake_post)
    code = main(["--workspace", "acme/app", "register-key"])
    assert code != 0
    err = capsys.readouterr().err
    assert "error" in err.lower()
    assert "connection refused" in err


def test_hub_rejects_conflicting_workspace(capsys, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return FakeResponse(
            409, {"detail": "workspace 'acme/app' is already claimed by a different key"}
        )

    monkeypatch.setattr("httpx.post", fake_post)
    code = main(["--workspace", "acme/app", "register-key"])
    assert code != 0
    err = capsys.readouterr().err
    assert "already claimed" in err


def test_hub_error_without_json_body_falls_back_to_text(capsys, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return FakeResponse(500, payload=None, text="internal server error")

    monkeypatch.setattr("httpx.post", fake_post)
    code = main(["--workspace", "acme/app", "register-key"])
    assert code != 0
    assert "internal server error" in capsys.readouterr().err


def test_url_precedence_flag_over_env_over_default(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        return FakeResponse(200, {"workspace": json["workspace"]})

    monkeypatch.setattr("httpx.post", fake_post)

    monkeypatch.setenv("SWITCHBOARD_URL", "https://from-env.example")
    main(["--workspace", "w", "register-key"])
    assert captured["url"] == "https://from-env.example/keys/register"

    main(["--workspace", "w", "--url", "https://from-flag.example", "register-key"])
    assert captured["url"] == "https://from-flag.example/keys/register"

    monkeypatch.delenv("SWITCHBOARD_URL")
    main(["--workspace", "w", "register-key"])
    assert captured["url"] == f"{MANAGED_HUB_URL}/keys/register"


def test_workspace_precedence_flag_over_env_over_inferred(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["workspace"] = json["workspace"]
        return FakeResponse(200, {"workspace": json["workspace"]})

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr("switchboard.cli._default_workspace", lambda directory: "inferred/ws")

    monkeypatch.delenv("SWITCHBOARD_WORKSPACE", raising=False)
    main(["register-key"])
    assert captured["workspace"] == "inferred/ws"

    monkeypatch.setenv("SWITCHBOARD_WORKSPACE", "env/ws")
    main(["register-key"])
    assert captured["workspace"] == "env/ws"

    main(["--workspace", "flag/ws", "register-key"])
    assert captured["workspace"] == "flag/ws"
