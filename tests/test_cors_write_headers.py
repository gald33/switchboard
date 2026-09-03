"""A browser can present a room's write key across origins.

A page that signs a write the way `writekey.RoomWriteKey.sign_request` does
puts two headers on the request, and a browser will not send a cross-origin
request carrying a header the hub's preflight did not allow. 2.0.0 allowed
`Authorization` and `Content-Type` only, so every page in a write-protected
room signed correctly and was refused before the hub ever saw the request --
the viewer is read-only and never noticed. This is the preflight, as a
browser sends it, against the real middleware.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from switchboard import writekey
from switchboard.config import ServerConfig
from switchboard.server import create_app
from switchboard.store import Store

ORIGIN = "http://127.0.0.1:4173"


def _app(tmp_path):
    store = Store(str(tmp_path / "hub.db"))
    return create_app(ServerConfig(db_path=store.path, cors_origins=(ORIGIN,)),
                      store=store)


def test_the_preflight_allows_the_write_key_headers(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        answer = client.options(
            "/messages",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers":
                    f"content-type, {writekey.KEY_HEADER}, {writekey.SIG_HEADER}",
            },
        )
    assert answer.status_code == 200, answer.text
    allowed = {h.strip().lower()
               for h in answer.headers["access-control-allow-headers"].split(",")}
    assert writekey.KEY_HEADER.lower() in allowed
    assert writekey.SIG_HEADER.lower() in allowed
    assert answer.headers["access-control-allow-origin"] == ORIGIN


def test_the_preflight_still_refuses_an_origin_it_does_not_know(tmp_path):
    """Adding headers must not widen who may ask: the allowlist of origins is
    the whole access control on a hub with a published token."""
    with TestClient(_app(tmp_path)) as client:
        answer = client.options(
            "/messages",
            headers={
                "Origin": "https://somebody-else.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": writekey.KEY_HEADER,
            },
        )
    assert answer.status_code == 400
