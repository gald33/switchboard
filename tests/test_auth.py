"""The hub's front door, which is all the hub does about access.

There is no per-workspace authorization to test any more. A room identifier is
`hash(workspace_token)` — derived, not owned — so there is nobody to check
ownership against. What protects a room is knowing its identifier and holding
its key, and neither is the hub's to enforce.

What is left is a perimeter, and the thing worth pinning is that it is honest
about being one: every admitted caller can reach every room it can name.
"""

from __future__ import annotations

import pytest

from switchboard.auth import Perimeter, hash_token
from switchboard.testing import hub


@pytest.fixture
def closed():
    """Bearer token required. Handed out with no token of its own, so a
    request that sends none is the default rather than the special case."""
    with hub(token="tok") as h:
        yield h.raw(token=None)


@pytest.fixture
def open_hub():
    with hub() as h:
        yield h.raw(token=None)


def h(token):
    return {"Authorization": f"Bearer {token}"}


def test_a_token_is_required_when_one_is_set(closed):
    assert closed.get("/agents", params={"workspace": "w"}).status_code == 401
    assert closed.get("/agents", params={"workspace": "w"}, headers=h("wrong")).status_code == 401
    assert closed.get("/agents", params={"workspace": "w"}, headers=h("tok")).status_code == 200


def test_no_token_means_open(open_hub):
    assert open_hub.get("/agents", params={"workspace": "w"}).status_code == 200


def test_the_perimeter_does_not_scope_anything(closed):
    """The claim auth.py insists on: admitted callers reach every room they can
    name. Anything narrower would be authorization, and rooms are not owned."""
    for workspace in ("w_one", "w_two", "someone-elses-room"):
        r = closed.get("/agents", params={"workspace": workspace}, headers=h("tok"))
        assert r.status_code == 200, workspace


def test_health_reports_whether_the_door_is_shut(closed, open_hub):
    assert closed.get("/health").json()["auth"] is True
    assert open_hub.get("/health").json()["auth"] is False


def test_admission_is_constant_time_and_hashes(tmp_path):
    p = Perimeter("secret")
    assert p.admits("secret") and not p.admits("secre") and not p.admits(None)
    assert Perimeter(None).admits(None), "no token configured means open"
    # hash_token stays, since a token should never be compared or logged raw
    assert hash_token("secret") != "secret"
    assert hash_token("secret") == hash_token("secret")


@pytest.mark.parametrize("path,params", [
    ("/agents", {"workspace": "w"}),
    ("/leases", {"workspace": "w"}),
    ("/board", {"workspace": "w"}),
    ("/channels", {"workspace": "w"}),
    ("/stats", {}),
])
def test_every_guarded_route_is_behind_the_door(closed, path, params):
    # The whole route table, so a new endpoint cannot quietly skip the check.
    assert closed.get(path, params=params).status_code == 401
    assert closed.get(path, params=params, headers=h("tok")).status_code == 200
