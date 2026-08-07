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
    SWITCHBOARD_WORKSPACE    default workspace (client)
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

import os
from dataclasses import dataclass

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


@dataclass
class ClientConfig:
    """Client-side settings, read from the environment."""

    url: str = "http://127.0.0.1:8787"
    token: str | None = None
    workspace: str = "default"
    agent_id: str | None = None
    #: Workspace key for end-to-end encryption. When set, payloads are sealed
    #: and identifiers blinded before anything leaves this process. It is never
    #: transmitted; the hub cannot read the workspace with or without it.
    key: str | None = None
    #: Path to this agent's local timing-observations database (see timing.py).
    #: Purely local — never sent to the hub, never shared with other agents.
    timing_db: str = "~/.switchboard/timing.db"

    @classmethod
    def from_env(cls) -> ClientConfig:
        return cls(
            url=os.environ.get("SWITCHBOARD_URL", "http://127.0.0.1:8787").rstrip("/"),
            token=os.environ.get("SWITCHBOARD_TOKEN") or None,
            workspace=os.environ.get("SWITCHBOARD_WORKSPACE", "default"),
            agent_id=os.environ.get("SWITCHBOARD_AGENT_ID") or None,
            key=os.environ.get("SWITCHBOARD_KEY") or None,
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
