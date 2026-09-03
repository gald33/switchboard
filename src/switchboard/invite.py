"""One string that carries everything needed to join a room.

Joining takes five facts — hub URL, token, workspace, key, and an agent id —
and every one of them must match a peer's independently. What makes that
expensive is not the number: it is that **getting any of them wrong fails
silently**. A different key, a different workspace, a different hub, and every
call still succeeds; you are simply somewhere else, in a room you are the only
occupant of, with an empty inbox that looks exactly like a quiet one.

That is not hypothetical. Two agents in this project's own dogfooding spent
forty minutes politely waiting for each other on the same hub, in the same
workspace, on two different keys, both watching a roster that listed them both.
The hub cannot help: it routes on the workspace and compares blinded tokens,
so from its side nothing is wrong.

So the fix is to stop transmitting five things. An invite is one opaque string
carrying all of them, produced by whoever already has a working setup and
consumed whole. Five chances to differ become one, and a mistyped invite fails
at the parse rather than an hour later in an empty room.

**This string is a credential.** It contains the token and the workspace key,
so it grants everything its holder had. Hand it over the way you would a
password, and prefer a fresh one per peer over reusing a durable one.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from . import rooms

#: The sentinel an inviter seals under the probe key. Its content does not
#: matter; being able to read it at all is the proof.
PROBE_SENTINEL = "switchboard-room-proof"

#: Bumped if the payload shape ever changes, so an old client says "I do not
#: understand this invite" rather than half-reading it and joining somewhere
#: unexpected — which would be the silent failure this exists to remove,
#: reintroduced by the fix itself.
VERSION = 1

PREFIX = "swb1_"


class InviteError(ValueError):
    """An invite that cannot be read. Never a silent partial join."""


@dataclass(frozen=True)
class Invite:
    url: str
    #: What goes on the wire. Optional at construction *only* because it can be
    #: derived from `workspace_token` — see `__post_init__`, which refuses an
    #: invite that ends up with neither.
    workspace: str = ""
    token: str | None = None
    key: str | None = None
    #: Free-form, purely for the human pasting it: which room this is, why.
    note: str = ""
    #: Which key opens this room, by id — the same `key_id` a rooms file uses
    #: and `SWITCHBOARD_KEY_<ID>` holds. Non-secret: it names a key, it is not
    #: one.
    #:
    #: What it is *for* is the invite that deliberately carries no key.
    #: `invite --no-key` means "you already hold this", and without an id that
    #: is only true for whoever keeps their key in the unnamed
    #: `SWITCHBOARD_KEY`. Anyone holding several — which is the whole point of
    #: named keys — has no way to know which of them was meant, and picks the
    #: default one: same hub, same workspace, wrong key, quiet room. With the
    #: id, the right key is *found*, and its absence is a sentence naming the
    #: variable to set rather than an afternoon.
    key_id: str = ""
    #: The room's *token*, of which `workspace` is the hash.
    #:
    #: `workspace` is what goes on the wire and is all a joiner needs in order
    #: to talk. The token is what a joiner needs in order to **write the room
    #: down**: `.switchboard/rooms.json` records rooms by token, precisely
    #: because the identifier is derived rather than assigned, and a hash
    #: cannot be un-hashed. Carrying only the derived form makes every invited
    #: room a visit — usable now, unrecordable afterwards — and leaves a
    #: joiner who wants to stay retyping values out of an export block.
    #:
    #: Also an integrity check on the way in: when both are present they must
    #: agree, so a `workspace` altered in transit is caught at the parse rather
    #: than becoming a room the sender never named.
    workspace_token: str = ""
    #: Board key of a value the INVITER sealed, for the joiner to open.
    #:
    #: A joiner writing and reading its own probe proves nothing: sealing and
    #: unsealing with one key is self-consistent in *any* room, including the
    #: wrong one, which is precisely the failure being prevented. Only opening
    #: something the other side wrote distinguishes "same room" from "same
    #: hub". The key name is plaintext here so the joiner can ask for it; the
    #: value under it is sealed, and unreadable to anyone on another key.
    probe: str = ""
    #: The room's write key, for a peer who will *work* in it. Absent from an
    #: invite meant for a viewer, which is what makes that viewer read-only
    #: in fact: the hub refuses every write from anyone without it, whatever
    #: the page or the person tries. A secret, like the key, and carried the
    #: same way. Meaningless for a room whose identifier is not `ws_…`.
    write_key: str | None = None

    def __post_init__(self) -> None:
        """Keep `workspace` and `workspace_token` from ever disagreeing.

        One is the hash of the other, so two fields carrying it is two chances
        to be wrong. Deriving the identifier when only the token is given, and
        refusing when they contradict, means the pair can only ever be
        consistent — the same reason the identifier is derived rather than
        assigned in the first place.
        """
        if not self.workspace and not self.workspace_token:
            raise InviteError(
                "an invite needs a workspace, or a workspace token to derive "
                "one from"
            )
        if self.workspace_token:
            derived = rooms.workspace_for(self.workspace_token)
            if not self.workspace:
                object.__setattr__(self, "workspace", derived)
            elif self.workspace != derived:
                raise InviteError(
                    f"invite names workspace {self.workspace!r} but carries a "
                    f"token for {derived!r}. One is the hash of the other, so "
                    "they cannot both be right — this string has been altered "
                    "or assembled by hand."
                )

    def encode(self) -> str:
        payload = {
            "v": VERSION,
            "u": self.url,
            "w": self.workspace,
            "t": self.token or "",
            "k": self.key or "",
            "n": self.note,
            "p": self.probe,
            "ki": self.key_id,
            "wt": self.workspace_token,
        }
        if self.write_key:
            # Only when carried. An invite without one must encode to exactly
            # the bytes the browser build produces for the same room, and that
            # build has no write key to add — a viewer never holds one.
            payload["wk"] = self.write_key
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, blob: str) -> Invite:
        text = blob.strip()
        if not text.startswith(PREFIX):
            raise InviteError(
                f"not a switchboard invite (expected it to start with {PREFIX!r}). "
                "Invites are produced by `switchboard invite` and pasted whole."
            )
        body = text[len(PREFIX):]
        # base64url without padding, restored here rather than emitted, so the
        # string survives being pasted into places that treat `=` specially.
        body += "=" * (-len(body) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(body.encode()))
        except (ValueError, TypeError) as exc:
            raise InviteError(f"invite is corrupt or truncated ({exc})") from exc
        if not isinstance(payload, dict):
            raise InviteError("invite did not contain an object")
        version = payload.get("v")
        if version != VERSION:
            raise InviteError(
                f"invite is version {version!r}, this client understands {VERSION}. "
                "Upgrade agent-switchboard rather than editing the string."
            )
        url, workspace = payload.get("u"), payload.get("w")
        if not isinstance(url, str) or not url:
            raise InviteError("invite has no hub URL")
        if not isinstance(workspace, str) or not workspace:
            raise InviteError("invite has no workspace")
        return cls(
            url=url,
            workspace=workspace,
            token=str(payload.get("t") or "") or None,
            key=str(payload.get("k") or "") or None,
            note=str(payload.get("n") or ""),
            probe=str(payload.get("p") or ""),
            key_id=str(payload.get("ki") or ""),
            # `__post_init__` checks this against `workspace` — a mismatch is
            # an altered string, not a preference to resolve.
            workspace_token=str(payload.get("wt") or ""),
            write_key=str(payload.get("wk") or "") or None,
        )

    def resolve_key(self, fallback: str | None = None,
                    env: dict[str, str] | None = None) -> str | None:
        """The key to actually seal with, given what this environment holds.

        Three cases, and the third is the one this method exists for:

        - The invite carries a key. Use it; it outranks everything, because an
          invite that lost to an exported key would put its holder in the right
          workspace on the wrong one.
        - It names no key at all. `fallback` — whatever the caller resolved
          from its own tiers — which is what makes `--no-key` mean "you already
          hold this" rather than "send in the clear".
        - It *names* a key it does not carry. Then there is a right answer and
          a wrong one, and guessing between them is the failure: look the id up
          in the environment, and refuse rather than quietly using some other
          key that happens to be lying around under `SWITCHBOARD_KEY`.

        Refusing is the point. Every alternative to it is a room you appear to
        be in.
        """
        if self.key:
            return self.key
        if not self.key_id or self.key_id == rooms.DEFAULT_KEY_ID:
            return fallback
        found = rooms.key_for(self.key_id, env)
        if found is None:
            raise InviteError(
                f"this room needs key {self.key_id!r}, which this environment "
                f"does not hold — set {rooms.env_var_for(self.key_id)}. The "
                "invite names the key instead of carrying it, so joining "
                "without it would put you in the right workspace on the wrong "
                "key: registered, on a roster, reading nothing."
            )
        return found

    def resolve_write_key(self, fallback: str | None = None,
                          env: dict[str, str] | None = None) -> str | None:
        """The write key to sign with, on the same rule as `resolve_key`.

        Carried, it wins. Absent, the environment's — looked up by `key_id`
        when the invite names one, since a write key is filed under the same
        id as the key it accompanies. Unlike `resolve_key` this never
        refuses: an invite without a write key is the *read-only* invite,
        and a reader with nothing to sign with is exactly what it describes.
        """
        if self.write_key:
            return self.write_key
        if not self.key_id or self.key_id == rooms.DEFAULT_KEY_ID:
            return fallback
        return rooms.write_key_for(self.key_id, env) or fallback

    def env_block(self) -> str:
        """Exactly what to export, for an environment with no checkout."""
        lines = [f"export SWITCHBOARD_URL={self.url}",
                 f"export SWITCHBOARD_WORKSPACE={self.workspace}"]
        if self.token:
            lines.append(f"export SWITCHBOARD_TOKEN={self.token}")
        if self.key:
            lines.append(f"export SWITCHBOARD_KEY={self.key}")
        if self.write_key:
            lines.append(f"export SWITCHBOARD_WRITE_KEY={self.write_key}")
        return "\n".join(lines)

    def link(self, page: str) -> str:
        """This invite as a URL onto a viewer page, for a human to click.

        The invite rides in the **fragment**, which is the only reason this is
        offerable: a fragment is never sent to the server, so the page's host
        never receives the key — not in its access log, not in a Referer, not
        in anything a proxy in between records. A query string would put the
        key in all three.

        What it does *not* protect against is the page itself, and no URL
        shape can: whoever serves that file could serve one that reads the
        fragment and posts it somewhere. Which page to trust is the caller's
        decision, so `page` is required rather than defaulted — a convenient
        default here would be this library choosing, on somebody's behalf, who
        gets to see their key.
        """
        return page.split("#", 1)[0] + "#" + self.encode()

    def room_record(self, name: str) -> dict[str, str]:
        """This room as a `.switchboard/rooms.json` entry.

        The reason the token is carried at all. Everything a record needs is
        non-secret and already here — token, key id, hub — so a joiner who
        wants to keep the room does not have to be told any of it a second
        time, and cannot mistype it. `name` is a local label and belongs to
        whoever is keeping the room, not to whoever sent it.

        The key is deliberately absent: records name a key, environments hold
        one. That split is what keeps a rooms file committable.
        """
        if not self.workspace_token:
            raise InviteError(
                "this invite carries no workspace token, so the room cannot be "
                "written down — a rooms file records rooms by token, and the "
                "identifier it carries is a hash. Ask for one minted by a "
                "newer client, or from a repo that declares this room."
            )
        record = {"name": name, "workspace_token": self.workspace_token}
        if self.key_id:
            record["key_id"] = self.key_id
        if self.url:
            record["hub_url"] = self.url
        return record

    def redacted(self) -> str:
        """A description safe to print in a log or a PR body."""
        carried = "set" if self.key else "none"
        if self.key_id:
            carried += f" (id {self.key_id})"
        text = (
            f"hub={self.url} workspace={self.workspace} "
            f"token={'set' if self.token else 'none'} "
            f"key={carried}"
        )
        if rooms.is_write_protected(self.workspace):
            text += f" write_key={'set' if self.write_key else 'none (read-only)'}"
        return text
