"""Signing in to a hosted bridge: a person's keys, held for them, sealed.

The hosted bridge (`switchboard-mcp --hosted`, see mcp_server.py) is an
encryption service for apps that cannot encrypt on their own. Without an
account it can only act on a key the conversation hands it, which is fine for
a throwaway room and wrong for a permanent one: a team key pasted into a chat
lives in the transcript, and the model has no business holding it.

So a person can *link* their keys instead. ChatGPT signs in over OAuth, the
sign-in page takes the keys (pasted as invites, which already carry them),
and from then on an invite made with `switchboard invite --no-key` — the room,
without its key — is enough: the bridge fills the key in from the link. One
team key opens every room made under it, which is the design, so the link is
a keyring (key id -> key and write key), not a list of rooms.

**How the keyring is kept.** Nothing that opens it is stored:

- The bridge issues two random 256-bit tokens per link, an access token
  (an hour) and a refresh token (90 days, renewed on every use). It stores
  only their SHA-256, to find the row.
- Each row's keyring is sealed with AES-GCM under a key derived by HKDF from
  that row's token, salted with the bridge's own seal key. Opening it takes
  both: the token, which only the signed-in app holds and presents on each
  call, and the seal key, which only the running bridge holds. A copy of the
  database opens nothing, with or without the seal key.
- A refresh rotates both tokens and re-seals under the new pair. A spent
  refresh token presented again, after a short grace for a retried request,
  means someone else has a copy: the whole link is deleted.

What this does not change is the trust model of any hosted integration:
while a request is being served, the bridge has the token and the seal key,
so it can open the keyring and act with it. That is the service.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .invite import Invite, InviteError
from .rooms import DEFAULT_KEY_ID

#: The one scope: use the keys you linked.
SCOPE = "keys"
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 90 * 86400
#: How long a spent refresh token may be presented again without it counting
#: as theft: a client that lost the response to its refresh and retried.
REFRESH_GRACE_SECONDS = 60
CODE_SECONDS = 600
#: Where an app may ask for the code to be sent. A sign-in page is a place
#: people paste keys; a client that could name any redirect could collect
#: them. Loopback is allowed too, for testing on your own machine.
DEFAULT_REDIRECT_HOSTS = ("chatgpt.com",)
MAX_KEYS = 32
MAX_REDIRECTS = 5
MAX_FORM = 64 * 1024

INVITE_PATTERN = re.compile(r"swb1_[A-Za-z0-9_-]+")

PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
AUTH_SERVER_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REGISTER_PATH = "/oauth/register"
REVOKE_PATH = "/oauth/revoke"

_SEAL_INFO = b"switchboard/bridge-link/v1/"


class LinkError(Exception):
    """An OAuth error, as the spec names them (`invalid_grant`, ...)."""

    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(description or error)
        self.error, self.description = error, description


def hub_key(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _lookup(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# -- the keyring -------------------------------------------------------------


@dataclass
class Keyring:
    """Key id -> {"key", "write_key"}, and a hub token per hub.

    The same shape an environment has (`SWITCHBOARD_KEY_<ID>`,
    `SWITCHBOARD_WRITE_KEY_<ID>`, `SWITCHBOARD_TOKEN`), so a key-less invite
    resolves here exactly as it would on the machine it was made on.
    """

    keys: dict[str, dict[str, str]] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"keys": self.keys, "tokens": self.tokens}

    @classmethod
    def from_json(cls, data: Any) -> Keyring:
        data = data if isinstance(data, dict) else {}
        return cls(keys=dict(data.get("keys") or {}), tokens=dict(data.get("tokens") or {}))

    @classmethod
    def from_invites(cls, text: str, hubs: frozenset[str] | None = None) -> Keyring:
        """The keys carried by every invite pasted into `text`.

        Only the keys and the hub token are kept. Which room each invite was
        for is dropped on purpose: the key opens every room made under it,
        and the room itself arrives later, key-less, in the conversation.
        """
        found = INVITE_PATTERN.findall(text or "")
        if not found:
            raise InviteError("paste at least one invite (a string starting 'swb1_'), "
                              "as `switchboard invite` prints it")
        ring = cls()
        for blob in found:
            invite = Invite.decode(blob)
            if hubs is not None and hub_key(invite.url) not in hubs:
                raise InviteError(f"an invite is for {invite.url}, and this bridge serves "
                                  f"{', '.join(sorted(hubs))} only")
            if not invite.key:
                raise InviteError(
                    "an invite leaves its key out (it was made with --no-key), so there "
                    "is nothing in it to link. Paste one made with plain "
                    "`switchboard invite`.")
            key_id = invite.key_id or DEFAULT_KEY_ID
            held = ring.keys.get(key_id)
            if held and held["key"] != invite.key:
                raise InviteError(f"two invites carry different keys under the same key id "
                                  f"{key_id!r}; link them one at a time")
            entry = held or {"key": invite.key}
            if invite.write_key:
                entry["write_key"] = invite.write_key
            ring.keys[key_id] = entry
            if invite.token:
                ring.tokens[hub_key(invite.url)] = invite.token
        if len(ring.keys) > MAX_KEYS:
            raise InviteError(f"at most {MAX_KEYS} keys per sign-in")
        return ring

    def key_ids(self) -> list[str]:
        return sorted(self.keys)

    def complete(self, invite: Invite) -> Invite:
        """`invite` with its key, write key and token filled in from here.

        A key the invite carries outranks the keyring, as it does in
        `Invite.resolve_key`. A key it names and this keyring lacks is
        refused rather than guessed: the right workspace on the wrong key is
        a room that looks exactly like a quiet one.
        """
        if invite.key:
            return invite
        key_id = invite.key_id or DEFAULT_KEY_ID
        entry = self.keys.get(key_id)
        if entry is None:
            linked = ", ".join(repr(k) for k in self.key_ids()) or "none"
            raise InviteError(
                f"this invite leaves its key out and needs key {key_id!r}, which is not "
                f"among the keys linked to this sign-in ({linked}). Sign in again and "
                "paste an invite that carries it.")
        return replace(
            invite, key=entry["key"],
            write_key=invite.write_key or entry.get("write_key"),
            token=invite.token or self.tokens.get(hub_key(invite.url)),
        )


@dataclass(frozen=True)
class Link:
    """A signed-in request's link: who it is, and the keyring it opened."""

    link_id: str
    client_id: str
    keyring: Keyring
    created: float


# -- the store ---------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    lookup TEXT PRIMARY KEY,
    link_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'access', 'refresh', or 'spent'
    client_id TEXT NOT NULL,
    sealed BLOB,                 -- NULL once a spent token's grace is over
    expires REAL NOT NULL,
    spent_at REAL
);
CREATE INDEX IF NOT EXISTS tokens_link ON tokens(link_id);
"""


class LinkStore:
    """Tokens and the keyrings sealed under them. See the module docstring."""

    def __init__(self, path: str | Path, seal_key: bytes,
                 clock: Callable[[], float] = time.time) -> None:
        if len(seal_key) < 32:
            raise ValueError("the seal key must be at least 32 bytes")
        self._seal_key = seal_key
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- sealing --

    def _key(self, token: str, kind: str) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=self._seal_key,
                    info=_SEAL_INFO + kind.encode()).derive(token.encode())

    def _seal(self, token: str, kind: str, payload: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._key(token, kind)).encrypt(
            nonce, json.dumps(payload).encode(), _lookup(token).encode())

    def _open(self, token: str, kind: str, sealed: bytes | None) -> dict[str, Any] | None:
        if not sealed or len(sealed) < 13:
            return None
        try:
            raw = AESGCM(self._key(token, kind)).decrypt(
                sealed[:12], sealed[12:], _lookup(token).encode())
        except InvalidTag:
            return None
        return json.loads(raw)

    # -- rows --

    def _purge(self, now: float) -> None:
        self._db.execute("DELETE FROM tokens WHERE expires < ?", (now,))
        self._db.execute("UPDATE tokens SET sealed = NULL WHERE kind = 'spent' AND "
                         "spent_at < ?", (now - REFRESH_GRACE_SECONDS,))

    def _insert_pair(self, payload: dict[str, Any], now: float) -> tuple[str, str]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        for token, kind, ttl in ((access, "access", ACCESS_SECONDS),
                                 (refresh, "refresh", REFRESH_SECONDS)):
            self._db.execute(
                "INSERT INTO tokens (lookup, link_id, kind, client_id, sealed, expires) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_lookup(token), payload["link_id"], kind, payload["client_id"],
                 self._seal(token, kind, payload), now + ttl))
        return access, refresh

    def issue(self, client_id: str, keyring: Keyring) -> tuple[str, str, str]:
        """A new link holding `keyring`. Returns (link_id, access, refresh)."""
        now = self._clock()
        payload = {"link_id": secrets.token_urlsafe(16), "client_id": client_id,
                   "created": now, "keyring": keyring.to_json()}
        with self._lock:
            self._purge(now)
            self._db.execute("BEGIN")
            try:
                access, refresh = self._insert_pair(payload, now)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return payload["link_id"], access, refresh

    def open_access(self, token: str) -> Link | None:
        """The link behind an access token, or None for anything else."""
        now = self._clock()
        with self._lock:
            row = self._db.execute(
                "SELECT kind, sealed, expires FROM tokens WHERE lookup = ?",
                (_lookup(token),)).fetchone()
        if row is None or row[0] != "access" or row[2] < now:
            return None
        payload = self._open(token, "access", row[1])
        return _link(payload) if payload else None

    def refresh(self, token: str, client_id: str) -> tuple[str, str, str]:
        """Rotate a refresh token. Returns (link_id, access, refresh).

        Raises `LinkError("invalid_grant")` for anything but a live refresh
        token of this client — and deletes the whole link when a spent one
        comes back after its grace, since then two parties hold it.
        """
        now = self._clock()
        lookup = _lookup(token)
        with self._lock:
            self._purge(now)
            row = self._db.execute(
                "SELECT link_id, kind, client_id, sealed, expires, spent_at FROM tokens "
                "WHERE lookup = ?", (lookup,)).fetchone()
            if row is None or row[4] < now:
                raise LinkError("invalid_grant", "unknown or expired refresh token")
            link_id, kind, owner, sealed, _, spent_at = row
            if kind == "spent" and (sealed is None
                                    or now - (spent_at or 0) > REFRESH_GRACE_SECONDS):
                self._db.execute("DELETE FROM tokens WHERE link_id = ?", (link_id,))
                raise LinkError("invalid_grant", "refresh token reused; the link is revoked")
            if kind not in ("refresh", "spent") or not hmac.compare_digest(owner, client_id):
                raise LinkError("invalid_grant", "not a refresh token of this client")
            payload = self._open(token, "refresh", sealed)
            if payload is None:
                raise LinkError("invalid_grant", "refresh token does not open its link")
            self._db.execute("BEGIN")
            try:
                if kind == "refresh":
                    # Spent, not deleted: seeing it again is how theft shows.
                    self._db.execute(
                        "UPDATE tokens SET kind = 'spent', spent_at = ? WHERE lookup = ?",
                        (now, lookup))
                    self._db.execute(
                        "DELETE FROM tokens WHERE link_id = ? AND kind = 'access'",
                        (link_id,))
                access, refresh = self._insert_pair(payload, now)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return link_id, access, refresh

    def revoke(self, token: str) -> None:
        """Delete the link behind any of its tokens. Unknown tokens are ignored."""
        with self._lock:
            row = self._db.execute("SELECT link_id FROM tokens WHERE lookup = ?",
                                   (_lookup(token),)).fetchone()
            if row is not None:
                self._db.execute("DELETE FROM tokens WHERE link_id = ?", (row[0],))

    def revoke_link(self, link_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM tokens WHERE link_id = ?", (link_id,))


def _link(payload: dict[str, Any]) -> Link:
    return Link(link_id=payload["link_id"], client_id=payload["client_id"],
                keyring=Keyring.from_json(payload.get("keyring")),
                created=float(payload.get("created") or 0))


# -- the authorization server ------------------------------------------------


@dataclass
class Reply:
    status: int
    body: bytes = b""
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _json(status: int, body: Any, headers: dict[str, str] | None = None) -> Reply:
    return Reply(status, json.dumps(body).encode(), "application/json", headers or {})


def _oauth_error(error: str, description: str = "", status: int = 400) -> Reply:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return _json(status, body)


def _redirect_ok(uri: str, hosts: frozenset[str]) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.fragment or not host:
        return False
    if parts.scheme == "https" and host in hosts:
        return True
    return parts.scheme == "http" and (host in ("localhost", "::1") or host.startswith("127."))


class OAuthServer:
    """The bridge's own authorization server, for linking keys.

    Clients register without a database: a `client_id` is the client's
    redirect URIs and name, signed with a key derived from the seal key, so
    registering costs nothing to keep and cannot be forged. Codes live in
    memory for ten minutes.
    """

    def __init__(self, store: LinkStore, issuer: str, seal_key: bytes, *,
                 operator: str = "", hubs: frozenset[str] | None = None,
                 redirect_hosts: tuple[str, ...] = DEFAULT_REDIRECT_HOSTS,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.issuer = issuer.rstrip("/")
        self.operator = operator or "an unnamed operator"
        self.hubs = hubs
        self.redirect_hosts = frozenset(h.lower() for h in redirect_hosts)
        self._clock = clock
        self._sign_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=seal_key,
                              info=b"switchboard/bridge-oauth/v1").derive(b"client")
        self._lock = threading.Lock()
        self._codes: dict[str, dict[str, Any]] = {}

    @property
    def resource_metadata_url(self) -> str:
        return self.issuer + PROTECTED_RESOURCE_PATH

    def challenge(self, error: str, description: str) -> str:
        """A `WWW-Authenticate` value pointing a client at the sign-in."""
        safe = description.replace("\\", "").replace('"', "'")
        return (f'Bearer resource_metadata="{self.resource_metadata_url}", '
                f'error="{error}", error_description="{safe}"')

    # -- signing --

    def _sign(self, data: dict[str, Any]) -> str:
        raw = _b64e(json.dumps(data, separators=(",", ":")).encode())
        mac = hmac.new(self._sign_key, raw.encode(), hashlib.sha256).digest()[:16]
        return f"{raw}.{_b64e(mac)}"

    def _unsign(self, blob: str) -> dict[str, Any] | None:
        raw, _, mac = (blob or "").partition(".")
        want = hmac.new(self._sign_key, raw.encode(), hashlib.sha256).digest()[:16]
        try:
            if not hmac.compare_digest(_b64d(mac), want):
                return None
            data = json.loads(_b64d(raw))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def _client(self, client_id: str) -> dict[str, Any] | None:
        if not client_id.startswith("swbc_"):
            return None
        data = self._unsign(client_id[5:])
        if data is None or data.get("t") != "client":
            return None
        return data

    # -- routing --

    def handle(self, method: str, path: str, query: str, headers: Any,
               body: bytes) -> Reply | None:
        """Answer an OAuth request, or None when `path` is not one."""
        if method == "GET" and path in (PROTECTED_RESOURCE_PATH,
                                        PROTECTED_RESOURCE_PATH + "/mcp"):
            return _json(200, self.protected_resource())
        if method == "GET" and path == AUTH_SERVER_PATH:
            return _json(200, self.metadata())
        if path == REGISTER_PATH and method == "POST":
            return self._register(body)
        if path == AUTHORIZE_PATH and method in ("GET", "POST"):
            if method == "GET":
                return self._authorize_page(_form(query.encode()))
            return self._authorize_submit(_form(body))
        if path == TOKEN_PATH and method == "POST":
            return self._token(_form(body))
        if path == REVOKE_PATH and method == "POST":
            token = _form(body).get("token")
            if token:
                self.store.revoke(token)
            return Reply(200)
        if path in (REGISTER_PATH, AUTHORIZE_PATH, TOKEN_PATH, REVOKE_PATH):
            return _json(405, {"error": "method not allowed"})
        return None

    def protected_resource(self) -> dict[str, Any]:
        return {
            "resource": self.issuer,
            "authorization_servers": [self.issuer],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_documentation":
                "https://github.com/gald33/switchboard/blob/main/docs/chatgpt.md",
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + AUTHORIZE_PATH,
            "token_endpoint": self.issuer + TOKEN_PATH,
            "registration_endpoint": self.issuer + REGISTER_PATH,
            "revocation_endpoint": self.issuer + REVOKE_PATH,
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        }

    # -- registration --

    def _register(self, body: bytes) -> Reply:
        try:
            request = json.loads(body or b"{}")
        except ValueError:
            return _oauth_error("invalid_client_metadata", "expected JSON")
        if not isinstance(request, dict):
            return _oauth_error("invalid_client_metadata", "expected a JSON object")
        uris = request.get("redirect_uris")
        if (not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECTS
                or not all(isinstance(u, str) and len(u) <= 512 for u in uris)):
            return _oauth_error("invalid_redirect_uri", "give 1 to 5 redirect_uris")
        refused = [u for u in uris if not _redirect_ok(u, self.redirect_hosts)]
        if refused:
            return _oauth_error(
                "invalid_redirect_uri",
                f"this bridge sends sign-ins to {', '.join(sorted(self.redirect_hosts))} "
                f"(or loopback) only, not {refused[0]}")
        name = _clean(request.get("client_name")) or urlsplit(uris[0]).hostname or "an app"
        client_id = "swbc_" + self._sign({"t": "client", "r": uris, "n": name})
        return _json(201, {
            "client_id": client_id,
            "client_id_issued_at": int(self._clock()),
            "client_name": name,
            "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })

    # -- authorization --

    def _check_request(self, params: dict[str, str]) -> tuple[dict[str, Any] | None, Reply | None]:
        """Validate an authorization request. Errors that can be sent back
        to the client are redirected there; the rest are shown here."""
        client = self._client(params.get("client_id", ""))
        if client is None:
            return None, self._page(400, "Unknown app",
                                    "<p>This sign-in link names an app this bridge does "
                                    "not know. Start again from the app.</p>")
        redirect = params.get("redirect_uri") or (
            client["r"][0] if len(client["r"]) == 1 else "")
        if redirect not in client["r"]:
            return None, self._page(400, "Wrong return address",
                                    "<p>This sign-in link would send you somewhere the "
                                    "app did not register. Start again from the app.</p>")
        state = params.get("state", "")

        def back(error: str, description: str) -> Reply:
            return self._send_back(redirect, state, {"error": error,
                                                     "error_description": description})

        if params.get("response_type") != "code":
            return None, back("unsupported_response_type", "only 'code' is supported")
        if not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
            return None, back("invalid_request", "PKCE with S256 is required")
        resource = params.get("resource", "")
        if resource and resource.rstrip("/") not in (self.issuer, self.issuer + "/mcp"):
            return None, back("invalid_target", "this server issues tokens for itself only")
        scope = params.get("scope", SCOPE) or SCOPE
        if set(scope.split()) - {SCOPE}:
            return None, back("invalid_scope", f"the only scope is {SCOPE!r}")
        return {"client_id": params["client_id"], "name": client["n"],
                "redirect_uri": redirect, "state": state,
                "code_challenge": params["code_challenge"], "resource": resource,
                "exp": self._clock() + CODE_SECONDS}, None

    def _authorize_page(self, params: dict[str, str], error: str = "",
                        status: int = 200) -> Reply:
        request, refusal = self._check_request(params)
        if refusal is not None:
            return refusal
        return self._form_page(request, error, status)

    def _form_page(self, request: dict[str, Any], error: str = "", status: int = 200) -> Reply:
        name = html.escape(request["name"])
        host = html.escape(urlsplit(request["redirect_uri"]).hostname or "")
        operator = html.escape(self.operator)
        signed = html.escape(self._sign({"t": "authorize", **request}))
        problem = f'<p class="error">{html.escape(error)}</p>' if error else ""
        body = f"""
<p><strong>{name}</strong> (returning to <strong>{host}</strong>) wants to use your
Switchboard keys through this bridge.</p>
<p>The Switchboard hub can't read your rooms: they are end-to-end encrypted, and keys
never reach it. {name} can't encrypt on its own, so this bridge, an optional encryption
service run by {operator}, does it for {name}. As with any hosted integration, the
service works with your rooms' contents while it acts for you.</p>
<p>Your keys are stored sealed under this sign-in's tokens, which only {name} holds. A copy
of the bridge's database opens nothing, and a sign-in unused for 90 days is deleted.</p>
{problem}
<form method="post" action="{AUTHORIZE_PATH}">
<input type="hidden" name="request" value="{signed}">
<label for="invites">Paste an invite for each key to link. Run
<code>switchboard invite</code> in a project that holds the key. Only the keys and the hub
token are kept.</label>
<textarea id="invites" name="invites" rows="5" required
 autocomplete="off" spellcheck="false" placeholder="swb1_..."></textarea>
<p>Afterwards, give {name} rooms with <code>switchboard invite --no-key</code>: the invite
names the room, and the bridge supplies the key, so the conversation never holds it.</p>
<button type="submit" name="action" value="link">Link keys</button>
<button type="submit" name="action" value="deny" formnovalidate class="quiet">Cancel</button>
</form>"""
        origin = urlsplit(request["redirect_uri"])
        return self._page(status, "Link your Switchboard keys", body,
                          form_action=f"'self' {origin.scheme}://{origin.netloc}")

    def _authorize_submit(self, form: dict[str, str]) -> Reply:
        request = self._unsign(form.get("request", ""))
        if request is None or request.get("t") != "authorize" or \
                request.get("exp", 0) < self._clock():
            return self._page(400, "Sign-in expired",
                              "<p>This sign-in page has expired. Start again from the "
                              "app.</p>")
        if form.get("action") == "deny":
            return self._send_back(request["redirect_uri"], request["state"],
                                   {"error": "access_denied"})
        try:
            keyring = Keyring.from_invites(form.get("invites", ""), self.hubs)
        except InviteError as exc:
            return self._form_page({k: v for k, v in request.items() if k != "t"},
                                   str(exc), 400)
        code = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            for stale in [c for c, v in self._codes.items() if v["exp"] < now]:
                del self._codes[stale]
            self._codes[code] = {**request, "keyring": keyring, "exp": now + CODE_SECONDS}
        return self._send_back(request["redirect_uri"], request["state"], {"code": code})

    def _send_back(self, redirect: str, state: str, params: dict[str, str]) -> Reply:
        query = {**params, "iss": self.issuer}
        if state:
            query["state"] = state
        joiner = "&" if urlsplit(redirect).query else "?"
        return Reply(302, headers={"Location": redirect + joiner + urlencode(query)})

    # -- tokens --

    def _token(self, form: dict[str, str]) -> Reply:
        grant = form.get("grant_type")
        client_id = form.get("client_id", "")
        if self._client(client_id) is None:
            return _oauth_error("invalid_client", "unknown client_id", 401)
        if grant == "authorization_code":
            with self._lock:
                code = self._codes.pop(form.get("code", ""), None)
            if code is None or code["exp"] < self._clock():
                return _oauth_error("invalid_grant", "unknown or expired code")
            if code["client_id"] != client_id or \
                    form.get("redirect_uri", code["redirect_uri"]) != code["redirect_uri"]:
                return _oauth_error("invalid_grant", "code was issued to another client")
            verifier = form.get("code_verifier", "")
            expected = _b64e(hashlib.sha256(verifier.encode()).digest())
            if not verifier or not hmac.compare_digest(expected, code["code_challenge"]):
                return _oauth_error("invalid_grant", "PKCE verification failed")
            resource = form.get("resource", "")
            if resource and resource.rstrip("/") not in (self.issuer, self.issuer + "/mcp"):
                return _oauth_error("invalid_target", "tokens are for this server only")
            _, access, refresh = self.store.issue(client_id, code["keyring"])
        elif grant == "refresh_token":
            try:
                _, access, refresh = self.store.refresh(form.get("refresh_token", ""),
                                                        client_id)
            except LinkError as exc:
                return _oauth_error(exc.error, exc.description)
        else:
            return _oauth_error("unsupported_grant_type")
        return _json(200, {"access_token": access, "token_type": "Bearer",
                           "expires_in": ACCESS_SECONDS, "refresh_token": refresh,
                           "scope": SCOPE}, {"Pragma": "no-cache"})

    # -- the page --

    def _page(self, status: int, title: str, body: str,
              form_action: str = "'self'") -> Reply:
        page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Switchboard</title>
<style>
:root {{ color-scheme: light dark; --fg:#1b1d21; --bg:#fbfbfa; --muted:#5d6470;
  --line:#d9dce1; --accent:#2f5bd3; --error:#b3261e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg:#e8e9ec; --bg:#141518;
  --muted:#a2a8b3; --line:#34373d; --accent:#8aa8ff; --error:#ff8a80; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width:36rem; margin:0 auto; padding:2.5rem 16px; }}
h1 {{ font-size:1.4rem; margin:0 0 1rem; }}
p, label {{ color:var(--muted); }} strong {{ color:var(--fg); }}
code {{ font-size:.9em; }}
label {{ display:block; margin-top:1.25rem; }}
textarea {{ box-sizing:border-box; width:100%; margin:.5rem 0; padding:.6rem;
  font:14px ui-monospace, monospace; color:var(--fg); background:transparent;
  border:1px solid var(--line); border-radius:6px; }}
button {{ font:inherit; padding:.55rem 1.1rem; border-radius:6px; border:0;
  background:var(--accent); color:#fff; cursor:pointer; margin-right:.5rem; }}
button.quiet {{ background:transparent; color:var(--muted); border:1px solid var(--line); }}
.error {{ color:var(--error); }}
</style></head>
<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
        return Reply(status, page.encode(), "text/html; charset=utf-8", {
            "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                        f"form-action {form_action}; frame-ancestors 'none'; "
                                        "base-uri 'none'"),
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        })


def _clean(value: Any) -> str:
    """A client-supplied name, trimmed to one short printable line."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())[:60]


def _form(body: bytes) -> dict[str, str]:
    if len(body) > MAX_FORM:
        return {}
    try:
        parsed = parse_qs(body.decode(), keep_blank_values=True)
    except UnicodeDecodeError:
        return {}
    return {k: v[0] for k, v in parsed.items() if v}


def seal_key_from_env(value: str | None) -> bytes | None:
    """`SWITCHBOARD_BRIDGE_SEAL_KEY`: at least 32 bytes, as base64url or hex."""
    if not value:
        return None
    text = value.strip()
    for decode in (bytes.fromhex, _b64d):
        try:
            key = decode(text)
        except ValueError:
            continue
        if len(key) >= 32:
            return key
    raise ValueError("SWITCHBOARD_BRIDGE_SEAL_KEY must be at least 32 random bytes, as "
                     "hex or base64url (make one with: python3 -c 'import secrets; "
                     "print(secrets.token_urlsafe(32))')")
