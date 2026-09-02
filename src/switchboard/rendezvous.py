"""Finding a peer you have never met.

Every timing signal Switchboard has is built *from* contact: a forecast comes
from your own history in a workspace and rides on a message, so it starts
working exactly one round trip after the round trip you could not get. First
contact is the gap, and it is where two agents most reliably miss each other —
one polls for five minutes and leaves, the other arrives at minute six, and
both were right to conclude the room was empty.

This is the rendezvous problem, and the three classical answers map onto what
a hub can offer:

**Leave a note at the meeting place.** An intent entry on the blackboard
outlives presence by a day rather than two minutes, and says what a roster
never could: somebody wants to meet, here is who, here is when they will next
look. An agent arriving forty minutes late can still act on it.

**Agree a time without being able to talk.** Two agents in one workspace
already share a secret nobody else has — the workspace token. Hashing it gives
both of them the same phase within a repeating cadence, so they converge on
the same minutes without ever having negotiated. No hub involvement, no
message, nothing to get out of sync.

**Do not both play the same strategy.** Uniform polling by both sides is the
worst case; the first arrival writes intent and becomes the anchor, and the
second finds a note rather than a blank.

One detail decides whether the shared cadence works at all: it is anchored to
the **hub's** clock, not to local time. Two machines minutes apart would
otherwise compute the same phase against different nows and never overlap,
which is the failure this exists to prevent, reintroduced one layer down. The
hub's time comes back on the announce that precedes any rendezvous, so this
costs no extra call.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Any

#: Where intent lives. Under `coord/` with everything else a session leaves
#: for a session it will never overlap with.
PREFIX = "coord/rendezvous/"

#: The topic for agents who have no topic. A shared string has to come from
#: somewhere, and two agents who have never met cannot agree one — so the pair
#: that needs it most is exactly the pair that cannot have it. Reserving a
#: constant closes that: a helper with no task to name, and a requester whose
#: task the helper has never heard of, still land on the same key.
#:
#: It is a *last* resort, not a default worth reaching for. Every agent holding
#: the key shares this one topic, so its notes are the widest audience there is
#: — right for "who is around", wrong for the detail of any one task. Meet
#: here, then move to a named topic or a room of your own.
OPEN_TOPIC = "open"

#: The two halves of an unequal meeting. Symmetric rendezvous assumes both
#: sides want the same thing; the common real case does not. One agent is
#: parked with capacity and no task, the other arrives with a task and needs
#: capacity — and neither is served by matching its own kind. A room full of
#: helpers finding each other is not a meeting.
OFFERING = "offer"
SEEKING = "seek"

#: How often the shared meeting slot comes round. Long enough that hitting it
#: is cheap even for an agent that only wakes occasionally, short enough that
#: a first meeting is minutes away rather than an hour.
SLOT_SECONDS = 300.0

#: What one `rendezvous` invocation is willing to spend looking before it
#: writes its note and hands the caller a time to come back at. A turn-based
#: agent cannot hold a socket for an hour, and pretending otherwise is what
#: burns a turn for nothing.
DEFAULT_LOOK_SECONDS = 60.0

#: Escalating rather than uniform, which is the whole trick: five uniform
#: 25-second polls cover two minutes, and these five cover twenty for the same
#: number of calls. The early ones catch a peer who is already here; the late
#: ones catch one who is arriving.
BACKOFF = (0.0, 5.0, 15.0, 40.0, 90.0, 180.0)


def complement(role: str) -> str:
    """Who a note of this role is actually looking for.

    The matching rule in one place, because getting it wrong is silent: an
    agent that matched its own role would report a successful meeting and stop
    looking for the peer that could have helped it.
    """
    return SEEKING if role == OFFERING else OFFERING



def slot_phase(workspace_token: str, topic: str) -> float:
    """This workspace-and-topic's offset within the slot cadence.

    Derived from a secret both parties hold and nobody else does, so two agents
    agree without exchanging anything — and two *different* topics do not all
    pile onto the same minute, which would turn a rendezvous into a thundering
    herd on a busy hub.
    """
    digest = hashlib.sha256(f"{workspace_token}\x00{topic}".encode()).digest()
    return (int.from_bytes(digest[:4], "big") % int(SLOT_SECONDS)) * 1.0


def next_slot(workspace_token: str, topic: str, now: float) -> float:
    """The next moment both sides will independently decide to look.

    ``now`` must be the hub's clock. Local time would put two machines with a
    few minutes of skew on different slots forever, which is precisely the
    miss this is meant to remove.
    """
    phase = slot_phase(workspace_token, topic)
    elapsed = now - phase
    return phase + (math.floor(elapsed / SLOT_SECONDS) + 1) * SLOT_SECONDS


def key_for(topic: str) -> str:
    return f"{PREFIX}{topic}"


@dataclass
class Intent:
    """One agent's standing statement that it is looking for someone.

    Deliberately not a message. A message expires in an hour and is read once;
    intent has to survive being unread by a peer who has not started yet, which
    is the ordinary case for a first meeting.
    """

    agent_id: str
    topic: str
    want: str
    since: float
    looking_until: float
    next_slot: float
    #: Whether this agent has capacity or a task. Defaults to seeking, which is
    #: what every note written before this field existed was: someone with a
    #: topic in mind, looking for anyone else on it.
    role: str = SEEKING

    def as_json(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "topic": self.topic,
            "want": self.want,
            "since": self.since,
            "looking_until": self.looking_until,
            "next_slot": self.next_slot,
            "role": self.role,
        }

    @classmethod
    def from_json(cls, raw: Any) -> Intent | None:
        if not isinstance(raw, dict) or not raw.get("agent_id"):
            return None
        try:
            return cls(
                agent_id=str(raw["agent_id"]),
                topic=str(raw.get("topic") or ""),
                want=str(raw.get("want") or ""),
                since=float(raw.get("since") or 0.0),
                looking_until=float(raw.get("looking_until") or 0.0),
                next_slot=float(raw.get("next_slot") or 0.0),
                # An older note has no role and was, by definition, seeking.
                # Reading it as anything else would hide it from the helpers
                # who are the only ones who could answer it.
                role=str(raw.get("role") or SEEKING),
            )
        except (TypeError, ValueError):
            return None

    def still_looking(self, now: float) -> bool:
        """Whether this note is worth answering, or is just litter.

        A note whose author gave up hours ago should not send a newcomer into a
        wait nobody is on the other end of — the same reason presence expires.
        """
        return now < self.looking_until


def schedule(look_seconds: float) -> list[float]:
    """Sleep lengths for one invocation's worth of looking.

    Truncated to fit the budget rather than scaled, so the early checks keep
    their timing: catching a peer who is already present is worth more than
    spreading evenly across whatever time was allowed.
    """
    out: list[float] = []
    spent = 0.0
    for gap in BACKOFF:
        if spent + gap > look_seconds:
            break
        out.append(gap)
        spent += gap
    return out or [0.0]


def hub_now(agent: dict[str, Any]) -> float:
    """The hub's clock, from a register response.

    Deliberately not `time.time()`. The whole point of a derived slot is that
    two machines compute the same one, and two machines are exactly what has
    skewed clocks — anchoring on local time would rebuild the miss this is
    meant to remove, one layer down and much harder to see.

    Lives here rather than in one caller because both surfaces need it, and a
    second copy of this rule is a second answer that can drift from the first:
    a CLI agent and an MCP agent computing slots off different clocks is the
    same miss again, wearing the name of the thing that prevents it.
    """
    stamp = agent.get("last_seen_at")
    if isinstance(stamp, str):
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()
