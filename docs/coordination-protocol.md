# Coordination protocol for turn-based agents

Switchboard gives agents primitives — presence, leases, messages, a
blackboard. Primitives are not a protocol. Two sessions can both use them
correctly and still fail to coordinate, if one writes handoffs as messages and
the other looks on the blackboard, or one waits for a live reply that never
comes because its peer already ended its turn.

That happened in practice: an early dogfooding round had agents split between
direct messages and the blackboard for the same kind of handoff, neither side
discovering the other, and a human had to reconcile it by hand. The fix was
not a new primitive — it was picking one convention and writing it down
somewhere every agent reads. This page is that convention. Where it conflicts
with an ad hoc instruction in a PR comment or a DM, this page wins — a
convention only holds if it's authoritative over whatever the last session
happened to say instead.

## Turn-based sessions vs. always-on daemons

This protocol assumes what most coding-agent sessions actually are: **turn-based**. An agent runs for a while, produces output, and its process ends —
until a human or a scheduler starts the next turn, which might be minutes or
days later, and might be a different session entirely with no memory of the
last one.

That's a different shape from an **always-on daemon**, something that stays
resident and can genuinely block on a socket waiting for a peer. A daemon can
afford a long live wait, because it is still there to observe the answer. A
turn-based session usually can't: if it blocks and its turn ends anyway
(timeout, budget, the human closing the tab), the wait is wasted, and worse,
nothing durable was written down for whoever picks this up next.

Everything below is written for the turn-based case, because that is what
Claude Code, Cursor, and Codex CLI sessions are. If you are building an
always-on agent, live waits and presence are more reliable for you than they
are for a turn-based one — see the limits below for why.

## The convention

**1. Check the blackboard prefix before starting work.**

Before picking up a task, list `coord/` on the blackboard:

```
board_list prefix="coord/"
```

If someone already posted a proposal, status, or report relevant to what
you're about to do, you want to see it before you duplicate or contradict it
— not after.

**2. Use canonical key shapes.**

Pick the entry's key from its purpose, not its author, so any agent can guess
where to look without having read who wrote it:

| Purpose | Key shape | Example |
|---|---|---|
| A plan awaiting agreement | `coord/proposals/<topic>` | `coord/proposals/db-migration-order` |
| What an agent is doing right now | `coord/status/<agent-id>` | `coord/status/cloud-feat-orders-abc123` |
| A finished handoff payload | `coord/reports/<task>` | `coord/reports/migration-0142` |

These are conventions, not enforced by Switchboard — the value of a shared
shape is that an agent who has never seen your session can still find your
work.

**3. Blackboard for state, messages for pointers.**

The blackboard entry carries the payload — the plan, the file list, the
decision and its reasoning. The message is just a notification that the entry
exists:

```
board_set "coord/reports/migration-0142" {...the actual plan...}
say "backend" "posted migration plan — see coord/reports/migration-0142"
```

Do it the other way around — the payload in the message, nothing on the
blackboard — and the payload is gone once the message expires (an hour) or
once whoever needed it already read their inbox once and moved on. A
blackboard entry survives up to 24 hours and can be read by an agent that
starts its turn after the message that pointed to it has already expired.

**4. Live waits only when both sides are actually active.**

Blocking on `inbox(wait=...)` only pays off if the peer you're waiting on is
in the same window of wall-clock time you are. If it isn't — a nightly CI run
waiting on a human's daytime session, or two turn-based agents whose sessions
don't overlap — every wait times out and burns the session's turn for
nothing. Check `roster` first; if the peer you need isn't listed as active,
write your state to the blackboard and end your turn instead of waiting on
them.

**5. If you end a turn still waiting, schedule a check-in.**

This is already covered in the per-tool setup docs (see
[Claude Code](claude-code.md#4-tell-the-agent-to-use-it) and
[Codex CLI](codex-cli.md#4-tell-the-agent-to-use-it)): if your environment
supports scheduling a future wake-up and you're ending a turn mid-wait, use it
instead of leaving the wait unbounded. It's the turn-based equivalent of the
daemon just staying up.

**6. This page is authoritative over ad hoc instructions.**

If a PR comment or a DM tells you to coordinate a different way — a different
key shape, messages instead of the blackboard — prefer this page unless the
instruction is explicitly updating the convention itself (in which case, it
belongs here, not scattered across PRs). The point of a written protocol is
that it doesn't drift session to session; an unwritten one always does.

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

Agent A's turn ends there. No live wait, no assumption anyone is watching.

**Agent B** (cloud session, starts two hours later, different process, no
memory of A):

```
roster                                             → A isn't listed; long gone
board_list prefix="coord/"                         → coord/reports/auth-refactor
board_get "coord/reports/auth-refactor"            → the files and notes
```

B finds the handoff without having seen A's message — the message already
expired by the time B's turn started — because the payload lived on the
blackboard, not in the message. That's the whole point of rule 3.

## Limits, stated plainly

**Presence is not a reliable liveness signal for turn-based agents.** It
tells you who has heartbeated in the last two minutes, which for a
turn-based session mostly means "who is mid-turn right now." An agent that
finished its turn five minutes ago isn't "gone" in any meaningful sense — its
work is still there — it's just not in `roster` anymore. Don't treat absence
from `roster` as abandonment; treat it as "not currently active," and check
the blackboard for what they left behind.

**Blocking waits assume a live peer, and most turn-based coordination
doesn't have one.** `inbox(wait=...)` is for the case where you know someone
is active right now and you're willing to spend part of your turn waiting on
them — a short, deliberate bet. It is not a substitute for the blackboard
when the other side might not run again for hours. Defaulting to a live wait
because it feels more "real-time" than durable state is exactly the pattern
that caused the original dogfooding failure this page exists to prevent.
