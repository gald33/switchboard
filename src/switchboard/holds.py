"""Standing declarations: "this resource is mine past this turn".

A lease and a board note answer two different questions on two different
clocks. A lease means *a live process holds this write* — renewal is a side
effect of the heartbeat, so it lapses the moment an agent stops, which is
correct. A declaration means *somebody means to keep this*, which no lease can
say without becoming a lock that survives its holder's death.

Keeping them separate is deliberate (see `docs/seam.md`). What was missing was
that the enforced surface never mentioned the advisory one: a subagent read
`do not touch it, I am mid-rewrite` off the board and then took the lease on
that exact file, and nothing it did was wrong. Checking leases *instead of* the
board is the cheaper, more obvious check, and it walked straight past a
standing claim.

So this is a convention, not a protocol. The hub knows nothing about it — a
declaration is an ordinary board entry, encrypted like any other, expiring on
the board's own day-long clock so abandoned intent collects itself. It lives
here rather than in the CLI because the CLI is not the only agent surface, and
a warning that reaches only one of them leaves the other exactly as blind as
before.
"""

from __future__ import annotations

from typing import Any

from .client import Client, SwitchboardError

#: Where a declaration lives. Documented in the skill's key-shape table, so a
#: reaper, the viewer or a human can find one without running `claim`.
HOLDS_PREFIX = "coord/holds/"


def declared_hold(hub: Client, resource: str) -> dict[str, Any] | None:
    """Somebody *else's* standing declaration on ``resource``, if there is one.

    Your own is not returned: a warning you always get is a warning you stop
    reading, and reclaiming across turns is the normal path for whoever
    declared.

    Never raises. The lease is the enforced surface; making it depend on this
    advisory one would trade a missed warning for an outage.
    """
    try:
        held = hub.board_get(HOLDS_PREFIX + resource)
    except SwitchboardError:
        return None
    if not isinstance(held, dict) or held.get("agent_id") == hub.agent_id:
        return None
    return held


def declare(hub: Client, resource: str, *, intent: str = "",
            since: Any = None) -> dict[str, Any]:
    """Record that ``resource`` is yours past this turn.

    ``since`` should come off the hub's clock — the lease it accompanies
    carries `acquired_at` — because the two agents comparing these notes are
    exactly the two whose local clocks disagree.
    """
    value = {
        "agent_id": hub.agent_id, "who": hub.local_agent_id,
        "resource": resource, "intent": intent, "since": since,
    }
    hub.board_set(HOLDS_PREFIX + resource, value)
    return value


def clear_own_declaration(hub: Client, resource: str) -> bool:
    """Drop your declaration on ``resource``. Yours only.

    ``--force`` breaks somebody else's *lease*, because liveness is a claim
    about a live process and that claim can be wrong. Intent is not force's to
    revoke, so nothing here takes a force flag.
    """
    try:
        mine = hub.board_get(HOLDS_PREFIX + resource)
        if isinstance(mine, dict) and mine.get("agent_id") == hub.agent_id:
            return hub.board_delete(HOLDS_PREFIX + resource)
    except SwitchboardError:
        pass  # never turn a successful release into a failure
    return False


def holder(held: dict[str, Any]) -> str:
    """Who to name when reporting a declaration.

    ``who`` is the agent's own string; ``agent_id`` is its blinded form, which
    is what the hub sees and is unreadable to anyone else. Board values are
    encrypted end to end, so carrying the legible name inside one costs no
    exposure the entry did not already have — and an opaque token in a warning
    is a warning nobody acts on.
    """
    for field in ("who", "agent_id"):
        name = held.get(field)
        if isinstance(name, str) and name:
            return name[:40]
    return "someone"
