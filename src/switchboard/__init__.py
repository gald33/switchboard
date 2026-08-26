"""Switchboard — an ephemeral orchestration hub for AI coding agents.

Four primitives, all of which expire on their own:

*Presence*    who is working right now, and on what
*Leases*      exclusive claims on a resource that time out instead of leaking
*Messages*    channel pub/sub with per-agent read cursors
*Blackboard*  shared key/value scratch space for handoffs

The point of the TTLs is that coordination state is *not* a record. It lives
as long as the work does and then it is gone — unlike a PR comment, which is
forever whether or not it was ever meant to be.
"""

from __future__ import annotations

from .auth import Perimeter
from .client import (
    AsyncClient,
    Client,
    Identity,
    LeaseHeld,
    RoomCheck,
    SwitchboardError,
    detect_identity,
)
from .config import ClientConfig, RepoRoom, ServerConfig, rooms_in
from .crypto import (
    CryptoError,
    DecryptionError,
    WorkspaceCipher,
    generate_key,
)
from .invite import PROBE_SENTINEL, Invite, InviteError
from .notify import Notifier
from .store import (
    Agent,
    BoardEntry,
    Lease,
    LeaseConflict,
    Message,
    NotLeaseHolder,
    Store,
    StoreError,
)
from .timing import unwrap_forecast, wrap_forecast

__version__ = "1.0.0"

__all__ = [
    "Invite",
    "InviteError",
    "PROBE_SENTINEL",
    "Perimeter",
    "RoomCheck",
    "__version__",
    # client
    "Client",
    "AsyncClient",
    "Identity",
    "detect_identity",
    "SwitchboardError",
    "LeaseHeld",
    # config
    "ClientConfig",
    "ServerConfig",
    # what a checkout says it takes part in, and under what local name
    "RepoRoom",
    "rooms_in",
    # auth — workspaces as a boundary, for shared hubs
    "Notifier",
    # end-to-end encryption — the hub never sees the key
    "WorkspaceCipher",
    "generate_key",
    "CryptoError",
    "DecryptionError",
    # adaptive timing — the envelope a forecast rides in, both directions.
    # Exporting only the reading half is what made it undiscoverable before.
    "wrap_forecast",
    "unwrap_forecast",
    # store
    "Store",
    "StoreError",
    "LeaseConflict",
    "NotLeaseHolder",
    "Agent",
    "Lease",
    "Message",
    "BoardEntry",
]
