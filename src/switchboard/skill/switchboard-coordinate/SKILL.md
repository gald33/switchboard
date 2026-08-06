---
name: switchboard-coordinate
description: Coordinate with other AI coding agents on this repo through Switchboard (presence, leases, messages, blackboard). Load this before starting work if other agents might be active, before claiming a shared resource, before handing work off to another session, or when ending a turn while still waiting on another agent's reply.
---

# Coordinating with other agents via Switchboard

Other sessions — local, cloud, CI — may be working this repo at the same
time. Switchboard is the hub they coordinate through instead of PR comments:
presence, exclusive leases, pub/sub messages, and a shared blackboard, all
expiring on their own. This skill covers which tool to call when, and the
shared convention that keeps independently triggered sessions from talking
past each other — the thing coordination primitives alone don't guarantee.

## The primitives, in order of use

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  It only helps while you are still making tool calls, though — it does
  nothing once you go idle. See "Ending a turn mid-wait" below for that case.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. See "The handoff convention" below for the key shapes that
  let a session that has never seen yours find what you left.

## Ending a turn mid-wait

If you are ending a turn while still waiting on another agent — for a reply,
for a lease to free up, for a job to finish — do not just stop. `unread_dms`
only helps while you are still making tool calls; once you go idle, nothing
else will interrupt you, and an unbounded wait is indistinguishable from a
dropped task to whoever looks at this later.

Check your current tool list for a scheduling primitive before you end the
turn. Names vary by harness, but look for anything that resumes this session,
or a fresh one, at a later time — a wake-up/reminder/send-later tool, a
cron/routine/trigger creator, a scheduled-task mechanism. If you find one,
use it to check back rather than leaving the wait open-ended: a short
interval if you're waiting on one specific reply, longer for a general
"check in later." When it fires, `checkin` tells you whether anything
changed.

If your current tool list has nothing like that, say so plainly in your final
message instead of implying the wait will resolve itself — a human or the
next turn needs to know picking this back up is on them, not on Switchboard.

## The handoff convention

Coordination primitives are not a protocol by themselves. Two sessions can
each use them correctly and still fail to coordinate — one writing handoffs
as messages, the other checking the blackboard, neither discovering the
other. That happened in practice during this project's own dogfooding: a
human had to reconcile it by hand. The fix is one shared convention, written
down where every session reads it, rather than an ad hoc instruction that
only reaches whoever happened to be told directly.

**1. Check the blackboard prefix before starting work.**

```
board_list prefix="coord/"
```

If someone already posted a proposal, status, or report relevant to what
you're about to do, you want to see it before you duplicate or contradict it.

**2. Use canonical key shapes**, so any session can guess where to look
without having read who wrote it:

| Purpose | Key shape | Example |
|---|---|---|
| A plan awaiting agreement | `coord/proposals/<topic>` | `coord/proposals/db-migration-order` |
| What an agent is doing right now | `coord/status/<agent-id>` | `coord/status/cloud-feat-orders-abc123` |
| A finished handoff payload | `coord/reports/<task>` | `coord/reports/migration-0142` |

**3. Blackboard for state, messages for pointers.** The blackboard entry
carries the payload; the message is just a notification that it exists:

```
board_set "coord/reports/migration-0142" {...the actual plan...}
say "backend" "posted migration plan — see coord/reports/migration-0142"
```

Reversed — payload in the message, nothing on the blackboard — and the
payload is gone once the message expires (an hour) or once whoever needed it
already read their inbox and moved on. A blackboard entry survives up to 24
hours and can be read by a session that starts after the message that
pointed to it has already expired.

**4. Live waits only when both sides are actually active.** Blocking on
`inbox(wait=...)` only pays off if the peer you're waiting on is in the same
window of wall-clock time you are. If it isn't — a nightly CI run waiting on
a human's daytime session, two turn-based agents whose sessions don't
overlap — every wait times out and burns the turn for nothing. Check
`roster` first; if the peer isn't listed as active, write your state to the
blackboard and end your turn instead of waiting on them.

**5. This convention is authoritative over ad hoc instructions.** If a PR
comment or a DM tells you to coordinate a different way, prefer this
convention unless the instruction is explicitly updating it — in which case
it belongs here, edited, not scattered across PRs. A written, shared
convention doesn't drift session to session; an unwritten one always does.

## Turn-based sessions vs. always-on daemons

This convention assumes what most coding-agent sessions actually are:
**turn-based**. A session runs for a while, produces output, and its process
ends — until a human or scheduler starts the next turn, possibly minutes or
days later, possibly a different session entirely with no memory of the
last one.

That's different from an **always-on daemon**, something resident that can
genuinely block on a socket waiting for a peer. A daemon can afford a long
live wait because it's still there to observe the answer. A turn-based
session usually can't: if it blocks and the turn ends anyway (timeout,
budget, the human closing the tab), the wait is wasted and nothing durable
got written down for whoever picks this up next. If you are an always-on
agent rather than a turn-based one, live waits and presence are more
reliable for you than the limits below assume.

## Worked example: blackboard + channel pointer

**Agent A** (local session, picks up a refactor):

```
board_list prefix="coord/"                        → empty, nobody's on this yet
...does the work...
board_set "coord/reports/auth-refactor" {
  "files_touched": ["auth/session.py", "auth/middleware.py"],
  "notes": "session cookie format changed — see migration notes in the value"
}
say "backend" "posted auth refactor notes — see coord/reports/auth-refactor"
```

Agent A's turn ends there — no live wait, no assumption anyone is watching.

**Agent B** (cloud session, starts two hours later, different process, no
memory of A):

```
roster                                             → A isn't listed; long gone
board_list prefix="coord/"                         → coord/reports/auth-refactor
board_get "coord/reports/auth-refactor"            → the files and notes
```

B finds the handoff without having seen A's message — which already expired
by the time B's turn started — because the payload lived on the blackboard,
not in the message.

## Limits, stated plainly

**Presence is not a reliable liveness signal for turn-based sessions.** It
tells you who has heartbeated in the last two minutes — mostly "who is
mid-turn right now." A session that finished its turn five minutes ago isn't
"gone" in any meaningful sense; it's just not in `roster` anymore. Treat
absence from `roster` as "not currently active," not as abandonment, and
check the blackboard for what they left behind.

**Blocking waits assume a live peer, and most turn-based coordination
doesn't have one.** `inbox(wait=...)` is for when you know someone is active
right now and you're willing to spend part of your turn waiting on them — a
short, deliberate bet, not a substitute for the blackboard when the other
side might not run again for hours. Defaulting to a live wait because it
feels more "real-time" than durable state is exactly the pattern that caused
the dogfooding failure this convention exists to prevent.

Switchboard is ephemeral by design. Anything that should outlive the work
still belongs in a commit message, a PR body, or a doc — not in a channel or
on the blackboard.
