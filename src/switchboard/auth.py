"""Who is calling, and which workspaces they may touch.

A self-hosted hub has one shared token and every caller may use every
workspace — workspaces there are a *namespace*, for keeping one team's
coordination out of another's way, and that is all they need to be.

A hub shared between parties that do not trust each other needs the same
workspaces to be a *boundary*. That is a different requirement, and it cannot
be bolted on later without breaking every deployed client, because it changes
what a token means rather than how it is sent.

So the seam lives here from the start: a resolver turns a bearer token into a
:class:`Principal`, and routes authorize the requested workspace against it.
The shipped resolver reproduces the shared-token behaviour exactly, so
self-hosted hubs are unaffected. A managed deployment supplies a resolver that
looks keys up wherever it keeps them.

Deliberately NOT decided here: how keys are issued, stored, revoked or billed.
Those are deployment policy. This module only defines what the rest of the hub
needs to know about a caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

#: Tier of an anonymous/self-hosted caller. Tiers exist so that a hub under
#: contention can order work by them; nothing in the open-source hub treats
#: one tier differently from another, and `standard` is what everyone gets.
DEFAULT_TIER = "standard"


@dataclass(frozen=True)
class Principal:
    """An authenticated caller."""

    key_id: str
    #: Workspaces this caller may touch. ``None`` means *all of them* — which
    #: is right for a self-hosted hub and wrong for a shared one.
    workspaces: frozenset[str] | None = None
    tier: str = DEFAULT_TIER
    label: str | None = None
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def unrestricted(self) -> bool:
        return self.workspaces is None

    def may_access(self, workspace: str) -> bool:
        return self.unrestricted or workspace in self.workspaces


class PrincipalResolver(Protocol):
    """Turns a bearer token into a :class:`Principal`, or ``None`` to reject."""

    def resolve(self, token: str | None) -> Principal | None:
        ...


class SharedTokenResolver:
    """One token, full access — the self-hosted default.

    With ``token=None`` the hub is open: every caller is accepted and may use
    every workspace. That is a reasonable default for something bound to
    localhost and an unreasonable one for anything else, which is why
    ``switchboard serve`` warns loudly about it at startup.
    """

    def __init__(self, token: str | None) -> None:
        self._token = token

    @property
    def open(self) -> bool:
        return self._token is None

    def resolve(self, token: str | None) -> Principal | None:
        if self._token is None:
            return Principal(key_id="anonymous", label="open hub")
        if token is None or not _constant_time_eq(token, self._token):
            return None
        return Principal(key_id="shared", label="shared token")


class StaticKeyResolver:
    """Several keys, each scoped to named workspaces.

    Enough to run a small shared hub from a config file, and a worked example
    of the interface for anything larger. Build the mapping however you like —
    a database, a secrets manager, an identity provider.
    """

    def __init__(self, keys: dict[str, Principal]) -> None:
        self._keys = dict(keys)

    def __len__(self) -> int:
        return len(self._keys)

    def resolve(self, token: str | None) -> Principal | None:
        if token is None:
            return None
        # Linear scan with a constant-time compare: correct regardless of key
        # count, and key counts here are small. A dict lookup on the raw token
        # would leak length/prefix information through timing.
        for candidate, principal in self._keys.items():
            if _constant_time_eq(token, candidate):
                return principal
        return None


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


def load_static_keys(path: str) -> StaticKeyResolver:
    """Build a :class:`StaticKeyResolver` from a JSON keys file.

    File shape — token -> {"workspaces": [...], "label": "...", "tier": "..."}::

        {
          "the-bearer-token-for-acme": {"workspaces": ["acme/app"], "label": "acme"}
        }

    ``workspaces`` is required and must be non-empty: an entry with no
    workspaces would be indistinguishable from a typo that dropped the field,
    and silently granting ``unrestricted`` access on a *missing* key is
    exactly the failure mode this file exists to prevent. ``label`` and
    ``tier`` are optional; ``key_id`` is derived from ``label`` if given,
    else a truncated hash of the token (never the token itself — this ends
    up in logs).
    """
    import hashlib
    import json

    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a JSON object mapping token -> key config")

    keys: dict[str, Principal] = {}
    for token, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: entry for a key must be an object, got {type(entry)!r}")
        workspaces = entry.get("workspaces")
        if not isinstance(workspaces, list) or not workspaces:
            raise ValueError(
                f"{path}: key {entry.get('label', '<unlabeled>')!r} needs a non-empty "
                "\"workspaces\" list — omitting it is not the same as \"all workspaces\""
            )
        label = entry.get("label")
        key_id = label or hashlib.sha256(token.encode()).hexdigest()[:12]
        keys[token] = Principal(
            key_id=key_id,
            workspaces=frozenset(workspaces),
            tier=entry.get("tier", DEFAULT_TIER),
            label=label,
        )
    return StaticKeyResolver(keys)
