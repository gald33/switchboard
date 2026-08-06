# Why the coordination protocol exists

Switchboard shipped with four primitives — presence, leases, messages, a blackboard — before it
had a convention for using them. That gap is what this page is about, and it's worth being
specific about the failure that exposed it, because the fix is not a new primitive. It's a rule
about which of the existing ones carries the payload.

## What happened

An early dogfooding round had two agent sessions working the same repo. One was mid-refactor and
needed to hand off what it had changed and why. It did the natural thing: posted the details as a
direct message to the other agent.

The other agent started its turn later — a different process, no memory of the first session —
and drained its inbox. The message had already expired. Messages default to a one-hour TTL, exactly
because they're meant to be signals, not storage. But nothing in Switchboard's primitives said
that out loud, so the sending agent had used the tool that felt right in the moment: a message
*feels* like the thing you send when you have something to tell someone.

The second agent, finding nothing, made its own decision about how to proceed — one that
conflicted with what the first agent had already done. Two agents, both using Switchboard
correctly by the letter of the API, produced an outcome neither would have chosen if either had
seen the other's state. A human had to notice the conflict and reconcile it by hand.

## Why more primitives wouldn't have fixed it

The instinct after a failure like this is to add something: a fifth primitive, a stronger
guarantee, a required acknowledgment. None of that was the actual gap. Presence, leases, messages,
and the blackboard were all doing exactly what they're built to do. The problem was that two
correct implementations of the same tool made *different assumptions* about where durable state
lives — one wrote it to something ephemeral, the other looked in the one place durability actually
lives. Coordination doesn't fail because a tool is missing. It fails because two sides of it
guessed differently about shape.

That's a convention problem, not a feature problem. The fix was to pick one shape and write it
down somewhere every agent reads — not a smarter message queue.

## The convention

**The payload goes on the blackboard. The message is a pointer to it, not a copy of it.**

```
board_set "coord/reports/migration-0142" {...the actual plan...}
say "build" "posted migration plan — see coord/reports/migration-0142"
```

The blackboard entry survives up to 24 hours by default; a message survives one. An agent that
starts its turn after the pointer message has already expired can still find the entry, because
the two live on different clocks and only one of them was ever meant to hold the state. This
matters specifically because most coding-agent sessions are **turn-based** — they run for a while
and then their process ends, possibly for hours, before anything picks the work back up. A
turn-based agent can't count on a live peer to ask; it can only count on what got written down.

That rule, plus a few conventions about key shape (so an agent that's never seen your session can
still guess where to look) is the whole content of
[`docs/coordination-protocol.md`](coordination-protocol.md).

## Seeing it, not just reading it

The scenario above — one agent's session ending before the other's begins, the durable state
outliving both — is exactly what [`demo/run.sh`](../demo/run.sh) plays out against a real,
throwaway hub: `bash demo/run.sh`. It's the shortest path from "I read the claim" to "I watched it
happen."

## The limit worth stating plainly

This convention only helps agents that actually consult it. Switchboard cannot enforce that a
session writes to the blackboard instead of a message — that's a discipline, not a lock. What
changed after the dogfooding failure wasn't the software; it was that there was finally one
written answer to "where does this go," instead of each session inventing its own.
