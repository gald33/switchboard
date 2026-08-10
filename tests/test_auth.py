"""Authentication, and workspace as a security boundary.

The load-bearing test here is
:func:`test_every_guarded_route_enforces_workspace_scope`. It enumerates the
app's own route table rather than a hand-written list, so a new endpoint added
later is covered the day it is added — which is the only way this stays true.
A cross-tenant read is exactly the kind of bug that never announces itself.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from switchboard.auth import (
    Principal,
    SharedTokenResolver,
    StaticKeyResolver,
    load_static_keys,
)
from switchboard.config import ServerConfig
from switchboard.server import create_app
from switchboard.store import Store

MINE = "acme/app"
THEIRS = "globex/app"


@pytest.fixture
def shared(tmp_path):
    """The self-hosted default: one token, unrestricted."""
    store = Store(str(tmp_path / "s.db"))
    app = create_app(ServerConfig(db_path=str(tmp_path / "s.db"), token="tok"), store=store)
    with TestClient(app) as c:
        yield c
    store.close()


@pytest.fixture
def multi(tmp_path):
    """A shared hub: two scoped keys plus one operator key."""
    store = Store(str(tmp_path / "m.db"))
    resolver = StaticKeyResolver({
        "key-acme": Principal(key_id="acme", workspaces=frozenset({MINE}), tier="standard"),
        "key-globex": Principal(key_id="globex", workspaces=frozenset({THEIRS})),
        "key-both": Principal(key_id="both", workspaces=frozenset({MINE, THEIRS})),
        "key-admin": Principal(key_id="admin", workspaces=None, label="operator"),
    })
    app = create_app(ServerConfig(db_path=str(tmp_path / "m.db")), store=store,
                     resolver=resolver)
    with TestClient(app) as c:
        yield c
    store.close()


def h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# --- the resolvers ----------------------------------------------------------


def test_shared_token_resolver_accepts_only_its_token():
    r = SharedTokenResolver("tok")
    assert r.resolve("tok") is not None
    assert r.resolve("nope") is None
    assert r.resolve(None) is None


def test_shared_token_resolver_with_no_token_is_open():
    r = SharedTokenResolver(None)
    assert r.open is True
    principal = r.resolve(None)
    assert principal is not None and principal.unrestricted


def test_unrestricted_principal_may_access_anything():
    assert Principal(key_id="x").may_access("literally-anything") is True


def test_scoped_principal_is_limited_to_its_workspaces():
    p = Principal(key_id="x", workspaces=frozenset({"a"}))
    assert p.may_access("a") is True
    assert p.may_access("b") is False
    assert p.unrestricted is False


def test_static_resolver_rejects_unknown_and_absent_keys():
    r = StaticKeyResolver({"k": Principal(key_id="one")})
    assert r.resolve("k") is not None
    assert r.resolve("other") is None
    assert r.resolve(None) is None


def test_self_hosted_behaviour_is_unchanged(shared):
    """The shipped default must behave exactly as it did before scoping existed."""
    assert shared.get("/health").status_code == 200  # open, no auth
    assert shared.get("/agents", params={"workspace": "anything"}).status_code == 401
    ok = shared.get("/agents", params={"workspace": "anything"}, headers=h("tok"))
    assert ok.status_code == 200
    # An unrestricted key may still use any workspace it invents, and may still
    # ask hub-wide questions.
    assert shared.get("/stats", headers=h("tok")).status_code == 200


# --- the boundary -----------------------------------------------------------


def test_scoped_key_reaches_its_own_workspace(multi):
    r = multi.post("/agents/register", json={
        "workspace": MINE, "agent_id": "a1", "name": "a1"}, headers=h("key-acme"))
    assert r.status_code == 200


def test_scoped_key_cannot_read_another_tenant(multi):
    multi.post("/agents/register", json={
        "workspace": THEIRS, "agent_id": "secret", "name": "secret"}, headers=h("key-globex"))
    denied = multi.get("/agents", params={"workspace": THEIRS}, headers=h("key-acme"))
    assert denied.status_code == 403
    assert "no access" in denied.json()["detail"]


def test_scoped_key_cannot_write_into_another_tenant(multi):
    denied = multi.post("/messages", json={
        "workspace": THEIRS, "channel": "c", "agent_id": "a", "body": "leak"},
        headers=h("key-acme"))
    assert denied.status_code == 403


def test_scoped_key_cannot_take_a_lease_in_another_tenant(multi):
    denied = multi.post("/leases/acquire", json={
        "workspace": THEIRS, "resource": "r", "agent_id": "a"}, headers=h("key-acme"))
    assert denied.status_code == 403


def test_scoped_key_cannot_read_another_tenants_blackboard(multi):
    multi.put("/board", json={
        "workspace": THEIRS, "key": "k", "agent_id": "a", "value": "secret"},
        headers=h("key-globex"))
    denied = multi.get("/board/k", params={"workspace": THEIRS}, headers=h("key-acme"))
    assert denied.status_code == 403


def test_a_key_may_span_several_workspaces(multi):
    for ws in (MINE, THEIRS):
        r = multi.post("/agents/register", json={
            "workspace": ws, "agent_id": "shared", "name": "shared"}, headers=h("key-both"))
        assert r.status_code == 200


def test_scoped_key_is_denied_hub_wide_questions(multi):
    """No named workspace means "all of them", which a tenant key may not ask."""
    denied = multi.get("/stats", headers=h("key-acme"))
    assert denied.status_code == 403
    assert "scoped" in denied.json()["detail"]
    assert multi.get("/stats", headers=h("key-admin")).status_code == 200


def test_unknown_key_is_401_not_403(multi):
    """Authentication failure must not leak whether a workspace exists."""
    assert multi.get("/agents", params={"workspace": MINE},
                     headers=h("no-such-key")).status_code == 401


def test_default_workspace_is_still_authorized(multi):
    """Omitting `workspace` falls back to "default" — which is somebody's tenant."""
    denied = multi.post("/agents/register", json={"agent_id": "a", "name": "a"},
                        headers=h("key-acme"))
    assert denied.status_code == 403


# --- the guard that cannot be forgotten -------------------------------------

# Routes that legitimately take no workspace. Everything else must enforce it.
_NO_WORKSPACE = {"/health", "/sweep", "/openapi.json", "/docs", "/redoc",
                 "/docs/oauth2-redirect"}


def _sample_body(path: str, workspace: str) -> dict:
    """A minimally-valid body so a 403 cannot be confused with a 422."""
    base = {"workspace": workspace, "agent_id": "probe"}
    if "lease" in path:
        base["resource"] = "r"
    if path == "/messages":
        base.update({"channel": "c", "body": "x"})
    if path == "/board":
        base.update({"key": "k", "value": "v"})
    if "register" in path:
        base["name"] = "probe"
    return base


def test_every_guarded_route_enforces_workspace_scope(multi):
    """Walk the real route table; every workspace-bearing route must 403.

    Enumerating the app rather than a fixed list is the point: an endpoint
    added next year is covered without anyone remembering to add it here.
    """
    app = multi.app
    checked = 0
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path in _NO_WORKSPACE or not methods:
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            # Probe the other tenant's workspace with acme's key.
            url = path.replace("{agent_id}", "probe").replace("{resource:path}", "r")
            url = url.replace("{key:path}", "k").replace("{channel:path}", "c")
            if "{" in url:
                pytest.fail(f"unhandled path parameter in {url!r}; extend this probe")
            kwargs: dict = {"headers": h("key-acme")}
            if method in ("POST", "PUT", "PATCH"):
                kwargs["json"] = _sample_body(path, THEIRS)
            else:
                kwargs["params"] = {"workspace": THEIRS}
            response = multi.request(method, url, **kwargs)
            assert response.status_code == 403, (
                f"{method} {url} returned {response.status_code}, not 403 — "
                f"this route does not enforce workspace scope"
            )
            checked += 1
    # Guard against the walk silently matching nothing.
    assert checked >= 15, f"only probed {checked} routes; the walk is not finding them"


# --- load_static_keys: the file -> resolver loader used by `switchboard serve --keys-file` ---


def test_load_static_keys_builds_a_working_resolver(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({
        "tok-acme": {"workspaces": ["acme/app"], "label": "acme"},
        "tok-both": {"workspaces": ["acme/app", "globex/app"]},
    }))

    resolver = load_static_keys(str(path))
    assert len(resolver) == 2

    acme = resolver.resolve("tok-acme")
    assert acme.key_id == "acme"
    assert acme.workspaces == frozenset({"acme/app"})
    assert not acme.unrestricted

    # No label -> key_id falls back to a hash prefix, never the raw token.
    both = resolver.resolve("tok-both")
    assert both.key_id != "tok-both"
    assert both.workspaces == frozenset({"acme/app", "globex/app"})

    assert resolver.resolve("not-a-real-token") is None


def test_load_static_keys_rejects_missing_workspaces(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"tok": {"label": "oops"}}))
    with pytest.raises(ValueError, match="workspaces"):
        load_static_keys(str(path))


def test_load_static_keys_rejects_empty_workspaces(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"tok": {"workspaces": []}}))
    with pytest.raises(ValueError, match="workspaces"):
        load_static_keys(str(path))


def test_load_static_keys_rejects_non_object_top_level(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(ValueError, match="expected a JSON object"):
        load_static_keys(str(path))


def test_load_static_keys_rejects_non_object_entry(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"tok": "not-a-dict"}))
    with pytest.raises(ValueError, match="must be an object"):
        load_static_keys(str(path))


# --- self-issued keys: the /keys/register endpoint --------------------------



def test_register_key_route_does_not_exist_on_a_shared_token_hub(shared):
    """Not mounted at all for a hub not running this mode — see server.py's
    conditional route registration."""
    r = shared.post("/keys/register", json={"workspace": MINE}, headers=h("tok"))
    assert r.status_code == 404


def test_register_key_route_does_not_exist_on_a_static_keys_hub(multi):
    r = multi.post("/keys/register", json={"workspace": MINE}, headers=h("key-acme"))
    assert r.status_code == 404


# --- /health reports auth status correctly across every resolver ------------


def test_health_reports_auth_true_for_shared_token(shared):
    assert shared.get("/health").json()["auth"] is True


def test_health_reports_auth_false_for_an_open_hub(tmp_path):
    store = Store(str(tmp_path / "open.db"))
    app = create_app(ServerConfig(db_path=str(tmp_path / "open.db")), store=store)
    with TestClient(app) as c:
        assert c.get("/health").json()["auth"] is False
    store.close()


def test_health_reports_auth_true_for_static_keys(multi):
    # Regression: config.token is unset in this mode, so `bool(config.token)`
    # alone (the pre-fix behaviour) would have wrongly reported False here.
    assert multi.get("/health").json()["auth"] is True

