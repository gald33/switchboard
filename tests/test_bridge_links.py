"""Signing in to the hosted bridge, and the keys it holds for you.

A throwaway room works from an invite in the conversation. A permanent one
should not: its key is the team's, and a transcript is no place for it. So a
person links their keys once, over OAuth, and gives the conversation a
key-less invite (`switchboard invite --no-key`) that the bridge completes.

These tests hold the three promises that make this safe: the store keeps nothing that
opens a keyring without the token that sealed it, a stolen refresh token
burns the whole link rather than quietly working, and a room joined with
linked keys answers only to that sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from test_mcp import call, make_bridge
from test_mcp_hosted import OPERATOR, WS, hub, invite_for  # noqa: F401 - fixture
from test_mcp_http import rpc, send

from switchboard import bridge_links
from switchboard.bridge_links import Keyring, LinkError, LinkStore, OAuthServer
from switchboard.crypto import generate_key
from switchboard.invite import Invite, InviteError
from switchboard.mcp_server import HostedBridges, hosted_relay, make_hosted_server

ISSUER = "https://bridge.example"
SEAL = secrets.token_bytes(32)
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _ring(key: str = "k" * 43) -> Keyring:
    return Keyring(keys={"default": {"key": key, "write_key": "WRITEKEY"}},
                   tokens={"https://hub.example": "HUBTOKEN"})


# --- the store --------------------------------------------------------------


def test_a_link_opens_with_its_access_token(tmp_path):
    store = LinkStore(tmp_path / "links.db", SEAL)
    link_id, access, _ = store.issue("client", _ring())
    link = store.open_access(access)
    assert link.link_id == link_id and link.keyring.keys == _ring().keys
    assert store.open_access("not-a-token") is None


def test_the_database_holds_no_key_and_no_token(tmp_path):
    path = tmp_path / "links.db"
    store = LinkStore(path, SEAL)
    _, access, refresh = store.issue("client", _ring("SECRETKEY" * 5))
    store.close()
    raw = path.read_bytes()
    for secret in (b"SECRETKEY", b"WRITEKEY", b"HUBTOKEN", access.encode(), refresh.encode()):
        assert secret not in raw


def test_the_seal_key_alone_opens_nothing(tmp_path):
    # Somebody with the database and the server's seal key, but no token,
    # has ciphertext keyed on a token they never saw.
    path = tmp_path / "links.db"
    store = LinkStore(path, SEAL)
    _, access, _ = store.issue("client", _ring())
    other = LinkStore(path, secrets.token_bytes(32))
    assert other.open_access(access) is None          # the token alone isn't enough
    rows = sqlite3.connect(path).execute("SELECT lookup, sealed FROM tokens").fetchall()
    for lookup, sealed in rows:
        assert store._open(lookup, "access", sealed) is None   # nor is the lookup


def test_an_access_token_expires(tmp_path):
    clock = _Clock()
    store = LinkStore(tmp_path / "links.db", SEAL, clock)
    _, access, _ = store.issue("client", _ring())
    clock.now += bridge_links.ACCESS_SECONDS + 1
    assert store.open_access(access) is None


def test_a_refresh_rotates_both_tokens(tmp_path):
    store = LinkStore(tmp_path / "links.db", SEAL)
    link_id, access, refresh = store.issue("client", _ring())
    same, access2, refresh2 = store.refresh(refresh, "client")
    assert same == link_id and {access2, refresh2}.isdisjoint({access, refresh})
    assert store.open_access(access) is None           # the old access token is gone
    assert store.open_access(access2).keyring.keys == _ring().keys


def test_a_refresh_token_reused_after_its_grace_revokes_the_link(tmp_path):
    clock = _Clock()
    store = LinkStore(tmp_path / "links.db", SEAL, clock)
    _, _, refresh = store.issue("client", _ring())
    _, access2, refresh2 = store.refresh(refresh, "client")
    clock.now += bridge_links.REFRESH_GRACE_SECONDS + 1
    with pytest.raises(LinkError, match="reused"):
        store.refresh(refresh, "client")
    # Both parties are out: whoever held the copy, and the rightful app.
    assert store.open_access(access2) is None
    with pytest.raises(LinkError):
        store.refresh(refresh2, "client")


def test_a_retried_refresh_inside_the_grace_still_works(tmp_path):
    clock = _Clock()
    store = LinkStore(tmp_path / "links.db", SEAL, clock)
    _, _, refresh = store.issue("client", _ring())
    store.refresh(refresh, "client")
    clock.now += 5
    _, access3, _ = store.refresh(refresh, "client")
    assert store.open_access(access3) is not None


def test_a_refresh_token_is_bound_to_its_client(tmp_path):
    store = LinkStore(tmp_path / "links.db", SEAL)
    _, _, refresh = store.issue("client", _ring())
    with pytest.raises(LinkError):
        store.refresh(refresh, "another-client")


def test_an_unused_link_is_forgotten(tmp_path):
    clock = _Clock()
    store = LinkStore(tmp_path / "links.db", SEAL, clock)
    _, _, refresh = store.issue("client", _ring())
    clock.now += bridge_links.REFRESH_SECONDS + 1
    with pytest.raises(LinkError):
        store.refresh(refresh, "client")


def test_revoking_any_token_deletes_the_link(tmp_path):
    store = LinkStore(tmp_path / "links.db", SEAL)
    _, access, refresh = store.issue("client", _ring())
    store.revoke(refresh)
    assert store.open_access(access) is None


# --- the keyring ------------------------------------------------------------


def _blob(**fields) -> str:
    base = dict(url="https://hub.example", workspace="ws", token="tok", key="K1")
    if "key" not in fields:
        fields["key"] = K1
    return Invite(**{**base, **fields}).encode()


HUB = "https://hub.example"
K1, K2 = generate_key(), generate_key()


def test_the_keyring_keeps_keys_rooms_and_where_they_are_used():
    ops = _blob(key=K2, key_id="ops", workspace="other", note="ops room")
    ring = Keyring.from_text(f"{_blob(key=K1, write_key='W1', note='repo')}\n"
                             f"Join this one too: {ops}")
    assert ring.keys == {"default": {"key": K1, "write_key": "W1", "hub": HUB},
                         "ops": {"key": K2, "hub": HUB}}
    assert ring.tokens == {HUB: "tok"}
    assert ring.room_names() == ["repo", "ops room", "lobby", "lobby:ops"]


def test_a_bare_key_is_linked_as_the_team_key():
    ring = Keyring.from_text(f"  {K1}  ", frozenset({HUB}))
    assert ring.keys == {"default": {"key": K1, "hub": HUB}}
    assert ring.room_names() == ["lobby"]


def test_environment_lines_are_linked_as_the_environment_files_them():
    ring = Keyring.from_text(
        f"export SWITCHBOARD_URL={HUB}\nSWITCHBOARD_TOKEN='tok'\n"
        f'SWITCHBOARD_KEY="{K1}"\nSWITCHBOARD_WRITE_KEY=W1\nSWITCHBOARD_KEY_TEAM_OPS={K2}\n'
        "SWITCHBOARD_KEY_EPOCH_PERIOD=3600\nSWITCHBOARD_WORKSPACE=my-repo")
    assert ring.keys["default"] == {"key": K1, "write_key": "W1", "hub": HUB}
    assert ring.keys["team_ops"]["key"] == K2
    assert ring.tokens == {HUB: "tok"} and ring.room_names()[0] == "my-repo"
    # `team/ops` in an invite is the variable SWITCHBOARD_KEY_TEAM_OPS: the same key.
    done = ring.complete(Invite.decode(_blob(key=None, key_id="team/ops")))
    assert done.key == K2


def test_invites_and_keys_mix_in_one_paste():
    ring = Keyring.from_text(f"{_blob(key=None, key_id='ops', note='ops')}\n"
                             f"SWITCHBOARD_KEY_OPS={K2}\nSWITCHBOARD_URL={HUB}")
    assert ring.room("ops").key == K2


def test_a_room_whose_key_was_not_pasted_is_refused():
    with pytest.raises(InviteError, match="needs key 'ops'"):
        Keyring.from_text(f"{_blob(key=K1)}\n{_blob(key=None, key_id='ops', workspace='o')}")


def test_nothing_linkable_is_refused():
    with pytest.raises(InviteError, match="nothing to link"):
        Keyring.from_text(_blob(key=None))
    with pytest.raises(InviteError, match="not an invite"):
        Keyring.from_text("hello there")


def test_the_keyring_refuses_hubs_the_bridge_does_not_serve():
    with pytest.raises(InviteError, match="serves"):
        Keyring.from_text(_blob(key=K1), frozenset({"https://other.example"}))


def test_the_lobby_is_derived_from_the_key():
    from switchboard import rooms
    lobby = Keyring.from_text(_blob(key=K1)).room("lobby")
    assert lobby.key == K1 and lobby.url == HUB
    assert lobby.workspace == rooms.lobby(K1).workspace


def test_a_key_less_invite_is_completed_by_the_key_it_names():
    ring = Keyring.from_text(_blob(key=K1) + "\n" + _blob(key=K2, key_id="ops"))
    done = ring.complete(Invite.decode(_blob(key=None, key_id="ops", token=None)))
    assert (done.key, done.token) == (K2, "tok")
    assert ring.complete(Invite.decode(_blob(key=None))).key == K1


def test_a_key_the_keyring_lacks_is_refused_not_guessed():
    ring = Keyring.from_text(_blob(key=K1))
    with pytest.raises(InviteError, match="'ops'"):
        ring.complete(Invite.decode(_blob(key=None, key_id="ops")))
    with pytest.raises(InviteError, match="Linked rooms"):
        ring.room("nope")


# --- the OAuth dance, over a socket -----------------------------------------


@pytest.fixture
def signed(hub, tmp_path):  # noqa: F811 - the fixture
    bridges = HostedBridges(hosted_relay(OPERATOR, ISSUER), hubs=[hub.url])
    oauth = OAuthServer(LinkStore(tmp_path / "links.db", SEAL), ISSUER, SEAL,
                        operator=OPERATOR, hubs=bridges.hubs)
    server = make_hosted_server(bridges, "127.0.0.1", 0, oauth=oauth)
    yield server, oauth, bridges
    server.server_close()
    bridges.close()


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _register(server, redirect: str = REDIRECT):
    return send(server, path="/oauth/register",
                body={"client_name": "ChatGPT", "redirect_uris": [redirect]})


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _authorize_query(client_id: str, challenge: str, **extra) -> str:
    return urlencode({"response_type": "code", "client_id": client_id,
                      "redirect_uri": REDIRECT, "state": "st8",
                      "code_challenge": challenge, "code_challenge_method": "S256",
                      "scope": "keys", "resource": ISSUER, **extra})


def sign_in(server, invites: str) -> dict:
    """The whole dance, as ChatGPT would do it. Returns the token response."""
    client_id = _register(server).json()["client_id"]
    verifier, challenge = _pkce()
    page = send(server, "GET", "/oauth/authorize?" + _authorize_query(client_id, challenge))
    assert page.status_code == 200, page.text
    request = page.text.split('name="request" value="')[1].split('"')[0]
    submitted = send(server, path="/oauth/authorize", headers=FORM, body=urlencode(
        {"request": request.replace("&amp;", "&"), "invites": invites, "action": "link"}))
    assert submitted.status_code == 302, submitted.text
    back = urlsplit(submitted.headers["Location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    query = {k: v[0] for k, v in parse_qs(back.query).items()}
    assert query["state"] == "st8" and query["iss"] == ISSUER
    token = send(server, path="/oauth/token", headers=FORM, body=urlencode({
        "grant_type": "authorization_code", "code": query["code"], "client_id": client_id,
        "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": ISSUER}))
    assert token.status_code == 200, token.text
    return {**token.json(), "client_id": client_id}


def front(server, tool_name: str, token: str | None = None, **arguments):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = send(server, path="/mcp", headers=headers,
                    body=rpc("tools/call", name=tool_name, arguments=arguments))
    return response


def payload(response) -> tuple[dict, dict]:
    result = response.json()["result"]
    return json.loads(result["content"][0]["text"]), result


def test_the_metadata_points_at_this_server(signed):
    server, _, _ = signed
    resource = send(server, "GET", "/.well-known/oauth-protected-resource").json()
    assert resource["resource"] == ISSUER and resource["authorization_servers"] == [ISSUER]
    meta = send(server, "GET", "/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint"] == ISSUER + "/oauth/token"


def test_registration_refuses_a_redirect_elsewhere(signed):
    # The sign-in page is where people paste keys. An app that could have the
    # code sent anywhere could collect them.
    server, _, _ = signed
    refused = _register(server, "https://evil.example/cb")
    assert refused.status_code == 400 and refused.json()["error"] == "invalid_redirect_uri"


def test_a_forged_client_id_gets_no_page(signed):
    server, _, _ = signed
    _, challenge = _pkce()
    page = send(server, "GET", "/oauth/authorize?" + _authorize_query("swbc_forged.x", challenge))
    assert page.status_code == 400 and "swb1_" not in page.text


def test_the_page_names_the_app_and_where_it_returns(signed):
    server, _, _ = signed
    client_id = _register(server).json()["client_id"]
    page = send(server, "GET", "/oauth/authorize?" + _authorize_query(client_id, _pkce()[1]))
    assert "ChatGPT" in page.text and "chatgpt.com" in page.text
    assert "can't read your rooms" in page.text
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]


def test_pkce_is_checked(signed, hub):  # noqa: F811
    server, _, _ = signed
    client_id = _register(server).json()["client_id"]
    _, challenge = _pkce()
    page = send(server, "GET", "/oauth/authorize?" + _authorize_query(client_id, challenge))
    request = page.text.split('name="request" value="')[1].split('"')[0]
    submitted = send(server, path="/oauth/authorize", headers=FORM, body=urlencode(
        {"request": request, "invites": invite_for(hub), "action": "link"}))
    code = parse_qs(urlsplit(submitted.headers["Location"]).query)["code"][0]
    token = send(server, path="/oauth/token", headers=FORM, body=urlencode({
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": REDIRECT, "code_verifier": "wrong"}))
    assert token.status_code == 400 and token.json()["error"] == "invalid_grant"


def test_cancel_sends_the_user_back_with_no_code(signed):
    server, _, _ = signed
    client_id = _register(server).json()["client_id"]
    page = send(server, "GET", "/oauth/authorize?" + _authorize_query(client_id, _pkce()[1]))
    request = page.text.split('name="request" value="')[1].split('"')[0]
    back = send(server, path="/oauth/authorize", headers=FORM,
                body=urlencode({"request": request, "action": "deny"}))
    query = parse_qs(urlsplit(back.headers["Location"]).query)
    assert query["error"] == ["access_denied"] and "code" not in query


def test_a_bad_invite_keeps_the_user_on_the_page(signed, hub):  # noqa: F811
    server, _, _ = signed
    client_id = _register(server).json()["client_id"]
    page = send(server, "GET", "/oauth/authorize?" + _authorize_query(client_id, _pkce()[1]))
    request = page.text.split('name="request" value="')[1].split('"')[0]
    again = send(server, path="/oauth/authorize", headers=FORM, body=urlencode(
        {"request": request, "invites": invite_for(hub, key=None), "action": "link"}))
    assert again.status_code == 400 and "nothing to link" in again.text


def test_a_key_less_invite_without_a_sign_in_asks_for_one(signed, hub):  # noqa: F811
    server, _, bridges = signed
    data, result = payload(front(server, "join_room", invite=invite_for(hub, key=None)))
    assert result["isError"] and data["error"] == "sign_in_required"
    (challenge,) = result["_meta"]["mcp/www_authenticate"]
    assert f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource"' in challenge
    assert len(bridges) == 0


def test_signed_in_a_key_less_invite_is_a_working_agent(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, invite_for(hub))
    joined = front(server, "join_room", tokens["access_token"],
                   invite=invite_for(hub, key=None, token=None))
    data, result = payload(joined)
    assert not result["isError"], data
    assert data["encrypted"] is True and data["key_from"] == "your linked keys"
    assert hub.key not in joined.text
    claimed, _ = payload(front(server, "claim", tokens["access_token"],
                               resource="README.md", room=data["room"]))
    assert claimed["acquired"] is True
    # Held in the real room: someone holding the same key on their laptop sees it.
    theirs, _ = call(make_bridge(hub, "laptop"), "claim", resource="README.md")
    assert theirs["acquired"] is False


def test_a_named_key_is_found_by_its_id(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, invite_for(hub, key_id="team"))
    data, result = payload(front(server, "join_room", tokens["access_token"],
                                 invite=invite_for(hub, key=None, key_id="team")))
    assert not result["isError"], data
    listed, _ = payload(front(server, "linked_keys", tokens["access_token"]))
    assert listed["key_ids"] == ["team"] and hub.key not in json.dumps(listed)


def test_the_same_person_is_the_same_agent_across_conversations(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, invite_for(hub))
    one, _ = payload(front(server, "join_room", tokens["access_token"],
                           invite=invite_for(hub, key=None, note="a")))
    two, _ = payload(front(server, "join_room", tokens["access_token"],
                           invite=invite_for(hub, key=None, note="b")))
    me1, _ = payload(front(server, "whoami", tokens["access_token"], room=one["room"]))
    me2, _ = payload(front(server, "whoami", tokens["access_token"], room=two["room"]))
    assert me1["agent_id"] == me2["agent_id"]


def test_a_linked_room_answers_only_to_its_sign_in(signed, hub):  # noqa: F811
    server, _, _ = signed
    mine = sign_in(server, invite_for(hub))
    data, _ = payload(front(server, "join_room", mine["access_token"],
                            invite=invite_for(hub, key=None)))
    # The handle alone, without the sign-in: no.
    anon, result = payload(front(server, "roster", room=data["room"]))
    assert result["isError"] and anon["error"] == "sign_in_required"
    # Someone else's sign-in: no.
    theirs = sign_in(server, invite_for(hub))
    other, result = payload(front(server, "roster", theirs["access_token"], room=data["room"]))
    assert result["isError"] and other["error"] == "sign_in_required"


def test_an_unknown_token_is_a_401_that_points_at_the_sign_in(signed):
    server, _, _ = signed
    response = front(server, "roster", "not-a-token")
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["WWW-Authenticate"]


def test_a_full_invite_still_needs_no_sign_in(signed, hub):  # noqa: F811
    server, _, _ = signed
    data, result = payload(front(server, "join_room", invite=invite_for(hub)))
    assert not result["isError"] and data["key_from"] == "invite"


def test_tools_declare_both_ways_in(signed):
    server, _, _ = signed
    tools = send(server, path="/mcp", body=rpc("tools/list")).json()["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["join_room"]["securitySchemes"] == [
        {"type": "noauth"}, {"type": "oauth2", "scopes": ["keys"]}]
    for name in ("linked_keys", "unlink_keys"):
        assert by_name[name]["securitySchemes"] == [{"type": "oauth2", "scopes": ["keys"]}]
    assert "--no-key" in by_name["join_room"]["description"]


def test_linked_keys_without_a_sign_in_asks_for_one(signed):
    server, _, _ = signed
    data, result = payload(front(server, "linked_keys"))
    assert data["error"] == "sign_in_required" and "mcp/www_authenticate" in result["_meta"]


def test_a_refresh_over_http_rotates(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, invite_for(hub))
    refreshed = send(server, path="/oauth/token", headers=FORM, body=urlencode({
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": tokens["client_id"]}))
    assert refreshed.status_code == 200
    assert front(server, "linked_keys", tokens["access_token"]).status_code == 401
    assert front(server, "linked_keys", refreshed.json()["access_token"]).status_code == 200


def test_without_a_store_there_is_no_sign_in(hub):  # noqa: F811
    bridges = HostedBridges(hosted_relay(OPERATOR, ISSUER), hubs=[hub.url])
    server = make_hosted_server(bridges, "127.0.0.1", 0)
    try:
        assert send(server, "GET", "/.well-known/oauth-protected-resource").status_code == 404
        tools = send(server, path="/mcp", body=rpc("tools/list")).json()["result"]["tools"]
        assert all("securitySchemes" not in t for t in tools)
    finally:
        server.server_close()
        bridges.close()


def test_the_seal_key_must_be_long_enough():
    assert bridge_links.seal_key_from_env(secrets.token_urlsafe(32)) is not None
    assert bridge_links.seal_key_from_env(secrets.token_hex(32)) is not None
    with pytest.raises(ValueError):
        bridge_links.seal_key_from_env("short")


# --- turning it on ----------------------------------------------------------


def _args(monkeypatch, seal: str | None, *extra: str):
    from switchboard.mcp_server import _parse_args
    if seal is None:
        monkeypatch.delenv("SWITCHBOARD_BRIDGE_SEAL_KEY", raising=False)
    else:
        monkeypatch.setenv("SWITCHBOARD_BRIDGE_SEAL_KEY", seal)
    return _parse_args(["--http", "--hosted", "--operator", "ops", *extra])


def test_a_store_needs_a_public_url_and_a_seal_key(monkeypatch, tmp_path):
    store = str(tmp_path / "links.db")
    with pytest.raises(SystemExit):
        _args(monkeypatch, secrets.token_urlsafe(32), "--store", store)
    with pytest.raises(SystemExit):
        _args(monkeypatch, None, "--store", store, "--public-url", ISSUER)
    args = _args(monkeypatch, secrets.token_urlsafe(32), "--store", store,
                 "--public-url", ISSUER)
    assert len(args.seal_key) == 32


def test_unlinking_deletes_the_keys_at_once(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, invite_for(hub))
    data, _ = payload(front(server, "join_room", tokens["access_token"],
                            invite=invite_for(hub, key=None)))
    gone, _ = payload(front(server, "unlink_keys", tokens["access_token"]))
    assert gone["unlinked"] is True
    assert front(server, "roster", tokens["access_token"], room=data["room"]).status_code == 401
    refreshed = send(server, path="/oauth/token", headers=FORM, body=urlencode({
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": tokens["client_id"]}))
    assert refreshed.status_code == 400


def test_signed_in_the_lobby_needs_no_invite(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, invite_for(hub, note="repo"))
    listed, _ = payload(front(server, "linked_keys", tokens["access_token"]))
    assert listed["rooms"] == ["repo", "lobby"]
    data, result = payload(front(server, "join_room", tokens["access_token"], name="lobby"))
    assert not result["isError"], data
    # The lobby every holder of the key shares: a laptop with the key meets us there.
    me, _ = payload(front(server, "whoami", tokens["access_token"], room=data["room"]))
    met, _ = call(make_bridge(hub, "laptop"), "roster", room="lobby")
    assert me["agent_id"] in [a["agent_id"] for a in met["agents"]]


def test_signed_in_a_linked_room_is_joined_by_name(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, invite_for(hub, note="repo"))
    data, result = payload(front(server, "join_room", tokens["access_token"], name="repo"))
    assert not result["isError"] and data["workspace"] == hub.workspace, data
    missing, result = payload(front(server, "join_room", tokens["access_token"], name="x"))
    assert result["isError"] and "'repo'" in missing["error"]


def test_a_bare_key_on_the_sign_in_page_links(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, f"SWITCHBOARD_KEY={hub.key}\nSWITCHBOARD_TOKEN={hub.token}")
    data, result = payload(front(server, "join_room", tokens["access_token"],
                                 invite=invite_for(hub, key=None, token=None)))
    assert not result["isError"], data


def test_a_full_invite_for_a_linked_key_gets_a_tip(signed, hub):  # noqa: F811
    server, _, _ = signed
    tokens = sign_in(server, invite_for(hub))
    data, _ = payload(front(server, "join_room", tokens["access_token"], invite=invite_for(hub)))
    assert "--no-key" in data["tip"]
