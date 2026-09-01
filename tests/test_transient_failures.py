"""A listener holds a connection open, so a timeout is ordinary weather.

Both of these were met in production within an hour of each other, by the
session that wrote them.

A `ReadTimeout` on a 25-second long-poll escaped the listener's retry handler —
which caught `SwitchboardError` and `OSError`, and httpx's exceptions are
neither — reached the top-level handler, and exited 1: "it never watched
anything". `--max-fails` never got a say, and a session that believed itself
reachable was not.

And when something between the client and the hub answered instead of it, the
HTML error page went to the terminal verbatim. `</div></body></html>` as
command output reads as the tool malfunctioning rather than as a proxy in the
way.
"""

from __future__ import annotations

import httpx
import pytest

from switchboard.client import SwitchboardError, _raise_for


def _response(status, body, content_type):
    return httpx.Response(status, text=body, headers={"content-type": content_type},
                          request=httpx.Request("GET", "https://hub.example.com/agents"))


def test_an_html_error_page_is_described_not_repeated():
    with pytest.raises(SwitchboardError) as raised:
        _raise_for(_response(502, "<html><body><h1>502 Bad Gateway</h1></body></html>",
                             "text/html; charset=utf-8"))
    message = str(raised.value)
    assert "HTML error page" in message
    assert "502" in message
    assert "</body>" not in message, "the page itself should not reach a terminal"


def test_the_hubs_own_error_still_speaks_for_itself():
    with pytest.raises(SwitchboardError) as raised:
        _raise_for(_response(401, '{"detail": "invalid or missing bearer token"}',
                             "application/json"))
    assert "invalid or missing bearer token" in str(raised.value)


def test_an_unparseable_body_says_what_it_was():
    """Not HTML, not JSON — say the status and the type rather than nothing."""
    with pytest.raises(SwitchboardError) as raised:
        _raise_for(_response(503, "upstream connect error", "text/plain"))
    assert "503" in str(raised.value)


def test_the_listener_counts_a_timeout_as_a_retry_not_a_death(monkeypatch, capsys):
    """One timeout must not end the wake path. Every check on the hub would
    still say this agent is listening; only the session would know it is not."""
    import argparse

    from switchboard import cli

    attempts = []

    class FlakyHub:
        agent_id = "me"
        # Sealed, so the "this repo has a key and you do not" guard stays out of
        # the way — it is not what this test is about.
        encrypted = True
        config = type("C", (), {"url": "https://hub.example.com", "workspace": "w"})()

        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def health(self): return {"ok": True}
        def register(self, **kw): return {}
        def board_set(self, *a, **kw): return {}
        def board_delete(self, *a, **kw): return True

        def inbox(self, **kw):
            attempts.append(1)
            raise httpx.ReadTimeout("the read operation timed out")

    monkeypatch.setattr(cli, "_make_client", lambda args: FlakyHub())
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    args = argparse.Namespace(
        until=None, channel=None, ttl=1.0, max_fails=3, quiet=True,
        agent_id="me", execution_class=None, effort=None, json=False)

    assert cli.cmd_listen(args) == cli.EXIT_ERROR
    assert len(attempts) == 3, "it should have spent its retries, not died on the first"
    assert "3/3" in capsys.readouterr().err
