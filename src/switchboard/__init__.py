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

from .auth import (
    DEFAULT_TIER,
    Principal,
    PrincipalResolver,
    SharedTokenResolver,
    StaticKeyResolver,
)
from .client import (
    AsyncClient,
    Client,
    Identity,
    LeaseHeld,
    SwitchboardError,
    detect_identity,
)
from .config import ClientConfig, ServerConfig
from .crypto import (
    CryptoError,
    DecryptionError,
    WorkspaceCipher,
    generate_key,
)
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

__version__ = "0.4.0"

__all__ = [
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
    # auth — workspaces as a boundary, for shared hubs
    "Principal",
    "PrincipalResolver",
    "SharedTokenResolver",
    "StaticKeyResolver",
    "DEFAULT_TIER",
    "Notifier",
    # end-to-end encryption — the hub never sees the key
    "WorkspaceCipher",
    "generate_key",
    "CryptoError",
    "DecryptionError",
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
