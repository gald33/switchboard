"""Configuration for the Switchboard server and clients.

Everything is environment-driven so a hub can be stood up with no config file:

    SWITCHBOARD_DB           path to the SQLite file (server)
    SWITCHBOARD_TOKEN        shared bearer token (server + client)
    SWITCHBOARD_URL          hub base URL (client)
    SWITCHBOARD_WORKSPACE    default workspace (client)
    SWITCHBOARD_AGENT_ID     stable identity for this agent (client)
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

# Long-poll ceiling for `GET /inbox?wait=`. Kept under the 30s that most
# proxies use as an idle-read timeout.
MAX_WAIT_SECONDS = 25.0
POLL_INTERVAL_SECONDS = 0.25

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
    sweep_interval: float = SWEEP_INTERVAL_SECONDS

    @classmethod
    def from_env(cls) -> ServerConfig:
        return cls(
            db_path=os.environ.get("SWITCHBOARD_DB", "switchboard.db"),
            token=os.environ.get("SWITCHBOARD_TOKEN") or None,
            sweep_interval=_env_float("SWITCHBOARD_SWEEP_INTERVAL", SWEEP_INTERVAL_SECONDS),
        )


@dataclass
class ClientConfig:
    """Client-side settings, read from the environment."""

    url: str = "http://127.0.0.1:8787"
    token: str | None = None
    workspace: str = "default"
    agent_id: str | None = None

    @classmethod
    def from_env(cls) -> ClientConfig:
        return cls(
            url=os.environ.get("SWITCHBOARD_URL", "http://127.0.0.1:8787").rstrip("/"),
            token=os.environ.get("SWITCHBOARD_TOKEN") or None,
            workspace=os.environ.get("SWITCHBOARD_WORKSPACE", "default"),
            agent_id=os.environ.get("SWITCHBOARD_AGENT_ID") or None,
        )


def clamp_ttl(ttl: float | None, default: float, maximum: float) -> float:
    """Resolve a caller-supplied ttl against its default and ceiling.

    A ttl of None means "use the default". Anything <= 0 is rejected by the
    API layer before reaching here, so this only guards the upper bound.
    """
    if ttl is None:
        return float(default)
    return float(min(ttl, maximum))
