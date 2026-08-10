"""Configuration for the Switchboard server and clients.

Everything is environment-driven so a hub can be stood up with no config file:

    SWITCHBOARD_DB           path to the SQLite file (server)
    SWITCHBOARD_TOKEN        shared bearer token (server + client)
    SWITCHBOARD_KEYS_FILE    path to a JSON file of scoped keys (server) — see
                             below; mutually exclusive with SWITCHBOARD_TOKEN
    SWITCHBOARD_SELF_ISSUED_KEYS  "1" to run multi-tenant with client-generated
                             keys instead of an operator-curated file (server)
                             — mutually exclusive with the two above
    SWITCHBOARD_URL          hub base URL (client)
    SWITCHBOARD_WORKSPACE    workspace to join (client). Unset, the client
                             derives one — see ``default_workspace`` and
                             docs/environments.md
    SWITCHBOARD_AGENT_ID     stable identity for this agent (client)
    SWITCHBOARD_KEY          workspace key for end-to-end encryption (client only —
                             a hub must never be given one, and has no use for it)

A hub with SWITCHBOARD_KEYS_FILE set runs multi-tenant: each key is scoped to
specific workspaces rather than granting access to all of them (see auth.py).
The file is a JSON object, token -> {"workspaces": [...], "label": "..."}:

    {
      "the-bearer-token-for-acme": {"workspaces": ["acme/app"], "label": "acme"},
      "the-bearer-token-for-globex": {"workspaces": ["globex/app"], "label": "globex"}
    }

Nothing issues, stores, or rotates these tokens for you — each party
generates their own (e.g. ``python -c 'import secrets;print(secrets.token_urlsafe(32))'``)
and gives it to the operator to add to the file. Changes need a restart to
take effect, same as rotating SWITCHBOARD_TOKEN does today.

A hub with SWITCHBOARD_SELF_ISSUED_KEYS=1 is also multi-tenant, but without
an operator-curated file: a client picks its own token (``switchboard
register-key --workspace <name>``, the same shape as ``keygen``) and the hub
binds it to that workspace itself, first-claim-wins, no restart needed. See
auth.SelfIssuedKeyResolver and docs/managed-hub.md for why a self-issued key
has to scope down to be worth anything at all.
"""

from __future__ import annotations

import functools
import getpass
import hashlib
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

from . import rooms

# --- TTL defaults (seconds) -------------------------------------------------
# Every record in Switchboard expires. These are the defaults applied when a
# caller does not pass an explicit ttl; each can be overridden per call, and
# the ceilings below bound what a caller is allowed to ask for.

DEFAULT_AGENT_TTL = 120
DEFAULT_LEASE_TTL = 900  # 15 minutes; renew via heartbeat
DEFAULT_MESSAGE_TTL = 3600  # 1 hour
DEFAULT_BOARD_TTL = 86400  # 24 hours

MAX_AGENT_TTL = 3600
MAX_LEASE_TTL = 86400
MAX_MESSAGE_TTL = 86400
MAX_BOARD_TTL = 7 * 86400

# How long an unused read cursor survives. Deliberately much longer than
# DEFAULT_AGENT_TTL: an agent's presence lapsing (a long turn, a gap between
# sessions) must not cost it its place in a conversation — only an agent that
# never reads at all for this long forfeits it. No message ever outlives this
# either, since MAX_MESSAGE_TTL is bounded well under it, so a cursor this old
# has nothing left to protect anyway.
DEFAULT_CURSOR_TTL = 7 * 86400  # 7 days

# Long-poll ceiling for `GET /inbox?wait=`. Kept under the 30s that most
# proxies use as an idle-read timeout.
MAX_WAIT_SECONDS = 25.0

# A waiting reader is woken directly by any write to a channel it cares about
# (see notify.py), so it does not poll in order to find messages. It still
# re-checks on this slow floor, because the notifier is in-process and cannot
# see writes made by another worker or another hub instance sharing the same
# database. Delivery correctness rests on this interval; the notifier only
# makes the common case fast. Lower it if you run multiple workers and care
# more about worst-case latency than about idle query load.
POLL_INTERVAL_SECONDS = 5.0

# How often the background sweeper hard-deletes expired rows. Reads already
# filter on expiry, so this is about reclaiming space, not correctness.
SWEEP_INTERVAL_SECONDS = 60.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class ServerConfig:
    """Server-side settings, read from the environment."""

    db_path: str = "switchboard.db"
    token: str | None = None
    #: Path to a JSON keys file (see module docstring). When set, the hub is
    #: multi-tenant: each key is scoped to specific workspaces rather than
    #: granting access to all of them. Mutually exclusive with ``token`` —
    #: ``cmd_serve`` rejects both being set rather than silently picking one.
    keys_file: str | None = None
    #: Multi-tenant without a curated file: clients register their own keys.
    #: Mutually exclusive with ``token`` and ``keys_file``, same reasoning.
    self_issued_keys: bool = False
    sweep_interval: float = SWEEP_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> ServerConfig:
        return cls(
            db_path=os.environ.get("SWITCHBOARD_DB", "switchboard.db"),
            token=os.environ.get("SWITCHBOARD_TOKEN") or None,
            keys_file=os.environ.get("SWITCHBOARD_KEYS_FILE") or None,
            self_issued_keys=os.environ.get("SWITCHBOARD_SELF_ISSUED_KEYS", "").lower()
            in {"1", "true", "yes"},
            sweep_interval=_env_float("SWITCHBOARD_SWEEP_INTERVAL", SWEEP_INTERVAL_SECONDS),
        )


def machine_suffix(extra: str = "") -> str:
    """A short, stable, opaque tag for this machine, for disambiguating a
    workspace name nobody chose.

    `extra` mixes in anything else that should make the tag differ — a
    checkout path, when there is one — so two unrelated directories both
    called `api` do not land on each other.

    An unconfigured client used to land in the workspace literally named
    ``default``. On a private hub that is harmless and even useful — two
    terminals on one laptop meet with no setup. On a shared hub it means every
    unconfigured user in the world is in one room, colliding over lease names
    and wondering who the strangers are. A key seals what they say, so this was
    never a disclosure, but it is a bad default.

    Hashing means the tag is unique without being *descriptive*: the workspace
    is the one thing the hub always sees in the clear, so a hostname would make
    it the most identifying string we hand over. Same inputs on the same
    machine give the same tag, so same-machine agents still find each other
    with no configuration — which is the property that made ``default`` useful
    in the first place.

    It is deliberately *not* stable across machines: agents that must
    coordinate across a network are exactly the ones that should be naming
    their workspace on purpose, and a name they never chose silently matching
    is not a property worth engineering for.
    """
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry, no USER, no LOGNAME
        user = ""
    seed = f"{socket.gethostname()}\0{user}\0{extra}"
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:8]


@functools.lru_cache(maxsize=128)
def _checkout_root(cwd: str) -> str:
    """The checkout containing `cwd`, or `cwd` itself when there is none.

    The project root rather than the working directory, so two terminals in
    different subdirectories of one project still agree — meeting with no
    configuration is the whole point of having a default at all.

    Found by walking up for a `.git` entry rather than by running
    `git rev-parse`. This is on the path of every client construction,
    including the lifecycle hooks that run at session start and stop, and
    spawning a process there costs more than the answer is worth. A `.git`
    that is a file rather than a directory — a worktree or submodule — counts
    as a root too, which is what we want: a worktree is a separate checkout
    and deserves its own room.
    """
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return cwd


def default_workspace() -> str:
    """The workspace a client uses when nobody named one.

    Deliberately opaque, and deliberately *not* the `org/repo` that `init`
    derives from a git remote. `init` writing a readable name is a considered
    act: you see it, it is printed, and it goes into a committed file so every
    clone agrees. A fallback is none of those things, and on a shared hub a
    guessable room that nobody chose is one an unconfigured agent can be
    joined in by a stranger. Hashing keeps the name unguessable while still
    being stable for the same project on the same machine.

    The checkout path is mixed into the tag so that two unrelated directories
    both called `api` — or simply two different projects on one laptop — do
    not land in the same room. Two agents on *different* machines still do not
    match, on purpose: anything coordinating across a network should be naming
    its workspace, and `init` is how you do that.
    """
    return f"default-{machine_suffix(_checkout_root(os.getcwd()))}"


def _selected_room(directory: Path):
    """The room this repo and this environment agree on, or None.

    Deliberately swallows a resolution failure rather than raising here.
    `from_env` is on the path of every command, including ones that have
    nothing to do with a hub, so a malformed rooms file must not make
    `switchboard --help` fail. The failure surfaces where it is actionable —
    see `switchboard rooms`, which reports it in full.
    """
    try:
        declared = rooms.load(directory)
        if not declared:
            return None
        return rooms.select(declared)
    except rooms.RoomsError:
        return None


@dataclass
class ClientConfig:
    """Client-side settings, read from the environment."""

    url: str = "http://127.0.0.1:8787"
    token: str | None = None
    workspace: str = field(default_factory=default_workspace)
    agent_id: str | None = None
    #: Workspace key for end-to-end encryption. When set, payloads are sealed
    #: and identifiers blinded before anything leaves this process. It is never
    #: transmitted; the hub cannot read the workspace with or without it.
    key: str | None = None
    #: Path to this agent's local timing-observations database (see timing.py).
    #: Purely local — never sent to the hub, never shared with other agents.
    timing_db: str = "~/.switchboard/timing.db"

    @classmethod
    def from_env(cls, directory: Path | None = None) -> ClientConfig:
        """Resolve a client's settings.

        A repo that declares rooms (see rooms.py) supplies the workspace, the
        key and the hub together, as one record — which is what stops a key and
        a workspace disagreeing, since they are no longer two values that have
        to be kept in step.

        The environment still wins over the record, for the ordinary reason:
        it is how a cloud environment or a one-off command overrides what a
        checkout says. A repo with no rooms file behaves exactly as before.
        """
        url = os.environ.get("SWITCHBOARD_URL", "").rstrip("/") or None
        workspace = os.environ.get("SWITCHBOARD_WORKSPACE") or None
        key = os.environ.get("SWITCHBOARD_KEY") or None

        room = _selected_room(Path.cwd() if directory is None else directory)
        if room is not None:
            url = url or room.hub_url
            workspace = workspace or room.workspace_token
            key = key or rooms.key_for(room.key_id)

        return cls(
            url=url or "http://127.0.0.1:8787",
            token=os.environ.get("SWITCHBOARD_TOKEN") or None,
            workspace=workspace or default_workspace(),
            agent_id=os.environ.get("SWITCHBOARD_AGENT_ID") or None,
            key=key,
            timing_db=os.environ.get("SWITCHBOARD_TIMING_DB", "~/.switchboard/timing.db"),
        )


def clamp_ttl(ttl: float | None, default: float, maximum: float) -> float:
    """Resolve a caller-supplied ttl against its default and ceiling.

    A ttl of None means "use the default". Anything <= 0 is rejected by the
    API layer before reaching here, so this only guards the upper bound.
    """
    if ttl is None:
        return float(default)
    return float(min(ttl, maximum))
