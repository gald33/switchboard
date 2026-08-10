"""The hub's front door, which is all that is left of authentication.

There used to be three credential models here: a shared bearer token, an
operator-curated file mapping tokens to the workspaces they may touch, and
tokens clients issued themselves and bound to a workspace on first use. Two of
them treated a workspace as something *ownable* — a name an authority assigns
and defends on your behalf.

That went away with #61. A room identifier is ``hash(workspace_token)``:
derived rather than claimed, computed identically by anyone holding the token,
with nothing to register and no third party to arbitrate a name nobody typed.
Which leaves exactly one credential in the system, and nobody issues it.

So there is no resolver, no principal, and no per-workspace authorization. Two
things protect a room, and neither is the hub:

- **knowing its identifier**, which is unguessable unless someone tells you
- **holding its key**, without which the contents are ciphertext

What remains here is a **perimeter**, not authorization. An optional shared
token keeps a hub off the open internet, which matters the moment one has a
public address. It does not scope anything, does not protect one room from
another, and must never be described as though it does — every caller who gets
through the door can reach every room they can name.
"""

from __future__ import annotations

import hashlib
import hmac


def hash_token(token: str) -> str:
    """One-way digest of a bearer token.

    Used where a token would otherwise be compared or logged in the clear.
    Kept as a function rather than inlined so there is one definition of what
    "the same token" means.
    """
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


class Perimeter:
    """Whether a caller may talk to this hub at all.

    With no token, the hub is open: every caller is accepted, which is right
    for something bound to localhost and wrong for anything with a public
    address — ``switchboard serve`` warns about it at startup.

    With a token, callers must present it. That is the entire check. It says
    nothing about *which* rooms they may reach, because rooms are not owned.
    """

    def __init__(self, token: str | None) -> None:
        self._token = token

    @property
    def open(self) -> bool:
        return self._token is None

    def admits(self, presented: str | None) -> bool:
        if self._token is None:
            return True
        return presented is not None and _constant_time_eq(presented, self._token)
