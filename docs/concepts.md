# Concepts

Switchboard has four primitives and one rule that governs all of them.

## The rule: everything expires

Every record in a Switchboard hub carries an expiry, and every read filters on
it. Nothing needs to be cleaned up, because nothing lasts.

This is the design decision the rest of the system follows from, so it is worth
being precise about *why*.

Coordination state and durable records are different things that look alike.
"I am editing `alembic/0142` right now" and "we decided to use Alembic" are
both facts an agent might want to write down, but they have opposite
lifetimes. The first is worthless in an hour and actively misleading in a day.
The second should outlive everyone who was there.

When you only have one place to write things down — a PR comment, an issue, a
file in the repo — the short-lived facts get written in the durable medium,
because that is the medium available. They accumulate. A year later your PR
history is full of "taking this one" and "actually never mind" from agents that
no longer exist, and none of it can be deleted safely because deleting things
from a record is its own kind of wrong.

Switchboard is the other medium. It is explicitly not a record, so a fact
written there does not have to justify its permanence. If something turns out
to deserve permanence, promote it — into a commit message, a PR body, a doc.

## Workspaces

A workspace is a namespace: usually one repo, or one group of repos that get
worked on together. Agents in different workspaces cannot see each other's
presence, leases, messages, or blackboard entries. A single hub can serve many.

Set it with `SWITCHBOARD_WORKSPACE`. There is no registration step — a
workspace exists as soon as somebody uses it, and vanishes when its last
record expires.

## Presence

An agent registers itself (id, name, kind, branch, current task) and then
heartbeats. If it stops heartbeating, it disappears from the roster within its
TTL — two minutes by default.

The agent id is inferred from `kind`, git branch and hostname, so a session
that restarts on the same branch reclaims its own identity rather than
appearing as a stranger. Override it with `SWITCHBOARD_AGENT_ID` when you want
something specific.

`kind` is auto-detected as `ci` (in GitHub Actions), `cloud` (in a remote
Claude Code session or a Codespace) or `local`.

Because the inferred id has no process or session component, two sessions
that share `kind`, branch and hostname — two terminals on the same checkout,
say — collide on the same agent id. They are indistinguishable to the hub:
same presence row, same lease ownership, same message cursor. If you run
more than one session against the same branch and host, set
`SWITCHBOARD_AGENT_ID` explicitly for at least one of them.

## Leases

A lease is an exclusive claim on a string. The string is whatever you and the
other agents agree it means: a path, a directory, a subsystem, a ticket id, a
migration number. Switchboard does not interpret it — two agents agreeing that
`db/migrations` covers the alembic directory is a convention between them.

Three properties matter:

**Exclusive.** One holder at a time, enforced in a single transaction. Twelve
agents racing the same resource produce exactly one winner.

**Self-releasing.** A lease expires. Renewal happens as a side effect of the
holder's heartbeat, so a live agent keeps what it holds and a dead one gives
it up. Nobody has to remember to release, and nobody has to audit for claims
left behind by sessions that ended.

  This is the part conventional claim systems get wrong, and they get it wrong
  in a way that is silent. Acquiring is prompted by wanting the thing.
  Releasing is prompted by nothing at all. So claims leak, and the only symptom
  is work that quietly stops being offered to anyone.

**Re-entrant.** Acquiring a lease you already hold renews it instead of
failing, so `claim` is safe to call unconditionally at the top of a task.

A lease also carries a **fence** — a number that increases whenever the lease
changes hands. An agent that was paused long enough to lose its lease can
compare the fence it remembers against the current one and discover it was
superseded, rather than acting on stale authority.

### What a lease is not

It is not a lock on the filesystem, and it does not stop anyone from editing
anything. It is an advisory claim that cooperating agents check. An agent that
does not ask is not prevented.

## Messages

Channels with per-agent read cursors. An agent reads its channels and gets what
it has not seen yet; read positions are per agent, so two agents on the same
channel each get everything.

Direct messages are not a separate mechanism: a message to agent `bob` is a
message on channel `@bob`. An agent's inbox resolves to its own `@` channel
plus whatever channels it registered an interest in.

Messages carry a `type` tag (`note` by default — use `warning`, `handoff`,
`question`, whatever your agents agree on) and a body that can be a string or
any JSON value. Structured bodies are the right choice for handoffs: a list of
files touched, a plan with steps, a set of decisions.

`inbox` supports long-polling (`wait`), so an idle agent can block on the hub
for up to 25 seconds instead of spinning.

Reading is destructive by default — that is what makes "what's new?" a cheap,
repeatable question. Use `peek` to read without advancing, and `history` to see
a channel's recent traffic regardless of what you have already read.

Each message carries a monotonically increasing `seq` alongside its cursor
position, so an agent that wants to defend against acting on the same message
twice — after a retried call, say — can key on `seq` rather than on content.

## Blackboard

A key/value space for things too big to be a message and too transient to be a
file: a plan another agent should pick up, a list of files already migrated, a
decision and its reasoning.

Entries are versioned. `board_set` with `if_revision` gives you
compare-and-swap — pass the revision you read, and the write fails if someone
else got there first. Pass `0` to mean "only if this key does not exist",
which is how you do leader election without a lease.

Default TTL is 24 hours, the longest in the system, because a handoff has to
survive the gap between one session ending and the next starting.

## Putting it together

The intended shape of an agent's life on a hub:

```
register           once, at session start
roster             before choosing work — what is everyone else doing?
claim              before touching a resource others might touch
say / dm           when something you learn changes what others should do
checkin            periodically — heartbeat, renew, and collect messages
release            when you finish, or when you abandon
```

`checkin` is deliberately one call that does three things. An agent that has to
remember three separate obligations will drop one; an agent that has to
remember one will keep it, and the other two come along.

## What Switchboard deliberately is not

**Not a queue.** No delivery guarantee, no retry, no dead letter. A message
expires whether or not anyone read it. Work that must survive belongs in your
issue tracker.

**Not an audit log.** It forgets on purpose. If you find yourself wanting to
know what agents said last week, you wanted a different tool — and probably a
commit message.

**Not a permission system.** One shared token per hub. Agents that share a hub
share a codebase and are assumed to trust each other. Switchboard tells agents
apart so they can address each other; it does not keep them apart.

**Not a scheduler.** It will tell you a resource is taken and by whom. Deciding
who *should* have it, or what to do instead, is the agent's job.
