# The seam with a plan: leases, liveness, and staleness

A system that decomposes work — a roadmap, a backlog, an issue tracker with
dependency edges — and Switchboard are **oppositely designed**, and that is
what makes them fit together rather than merge.

| | A plan | Switchboard |
|---|---|---|
| Core promise | a committed artifact readable with no hub, no token, no network | state decays, and the decay *is* the correctness property |
| Unit of truth | durable decisions that outlive every session | deliberately not durable |
| Blast radius | losing it is expensive | "cheap to run and cheap to lose" |
| Readability | must read, query and index its own content | the hub **cannot** read, by construction |

Fuse them and one has to betray its own model. So: two things, one narrow
seam, and a dependency that only ever points one way — **the plan may depend
on Switchboard, softly; Switchboard never depends on the plan.** That is not
politeness. Under end-to-end encryption the hub cannot read a payload, so a
dependency on plan semantics is unimplementable rather than merely unwise.

Degradation is the non-negotiable half: with no hub, no token, and no CLI on
`PATH`, the plan must still read, validate and render. Coordination degrades;
the backlog never becomes unreadable.

## Lease the write, not the work

This is the mistake worth naming, because it is easy to make and it looks
correct while you are making it.

A work item can legitimately be owned for **days**. A Switchboard lease is
built to decay in minutes. So it is tempting either to hold a very long lease,
or to ask for a lease class that outlives a session.

**Don't.** A lease does not mean "this is owned". It means *a live process
holds this* — renewal is a side effect of the heartbeat, which is the entire
mechanism described in [the README](../README.md#why-leases-expire). A lease
engineered to outlive its holder stops being a lease and becomes a lock that
survives its holder's death, which is precisely the leaked claim the project
exists to eliminate. And it fails silently: a three-day lease whose holder
died on day one blocks the work for two days, and nothing ever asks.

The tension dissolves once you notice that **one primitive was being asked
three different questions**:

| Question | Where it belongs | Duration |
|---|---|---|
| "This item is mine for the next three days" | the plan's own committed files | never expires |
| "Nobody else read-modify-write this item while I am doing it" | a Switchboard lease | **seconds** |
| "Am I actually breathing on the item I own?" | heartbeat-renewed presence | expiry is *correct* |

The race is on **writing the claim**, not on doing the work. Once the lease
covers only the read-modify-write of the item file, no long-TTL lease class is
needed and Switchboard grows nothing.

The third row is worth dwelling on: there, expiry is not a bug to design
around but the useful signal. If an agent stops heartbeating it is genuinely
not working right now — the durable claim in the file still stands, and peers
simply learn that nobody is currently at the keyboard.

## The seam is three elements, not one

It is tempting to stop at "claims". That leaves out the one that protects the
plan from a failure Switchboard already solved.

**1. Write exclusion.** A short lease on the item key while the claim is
written. Seconds. This is the only thing the plan cannot do with files alone.

**2. Liveness attestation.** Heartbeat-renewed presence, so peers can see
whether the holder of a durable claim is currently working.

**3. Staleness evidence.** Durable claims re-create exactly the leak
Switchboard exists to prevent: an agent dies and its item stays claimed
forever. The plan therefore needs a reaper, and the reaper needs evidence —
"this item has been claimed for three days and its holder has not appeared on
the roster for two of them". Switchboard answers that question; it does not
hold the claim and does not decide. Read-only, and the plan pulls it.

## Static collisions compile to names

A plan often knows something Switchboard cannot: that two differently-named
items will collide because both bump one shared counter — a migration number,
a version manifest. The hub knows who holds what; it cannot know that two
distinct resources are the same underlying contention.

**Switchboard should not grow vocabulary for this**, and does not need to.
Compile the static edge into a resource *name* — `artifact:migrations-counter`
— and the hub keeps knowing nothing about arcs while excluding correctly.

One wrinkle that is easy to get wrong: an advisory "these will collide, warn
me" edge is not the same as an exclusive lease. Compiling it to `claim` would
silently upgrade a warning into a hard block. Use the **read** side instead —
list the claims, see who holds `artifact:X`, warn. Acquire nothing.

## Degrade loudly

A soft dependency is right: shell out, and no-op when Switchboard is absent.
The dangerous half is the silence. This project has been bitten by exactly
that — its own CI announced nothing for a full day inside green builds,
because the hook ends in `|| true` and nobody could tell coordinating from
no-op-ing.

So no-op, but **say so once, visibly**. A silent no-op is indistinguishable
from working.

## Pin the identity you claim under

An integration that shells out to the CLI inherits whatever identity the CLI
derives from its working directory — and an unpinned id is built from repo +
branch + session. A helper invoked from a different directory records its
claim under a *different* agent id than the one the agent announces as, so the
claim is held by a ghost that the staleness reaper cannot match to any live
agent.

Set `SWITCHBOARD_AGENT_ID` (or pass `--agent-id`) inside the integration.
`switchboard whoami` states whether the id is pinned or derived — worth
checking, because under a key the id is blinded before you see it,
so a pin that worked and a pin that was ignored both render as opaque tokens.

Pin it to something **session- or process-distinct**, and specifically not to
the branch, however tempting that is. A claim record naming a branch and a
lease holder naming an agent id look like two strings that ought to be one,
and collapsing them makes a later "is this holder still alive?" join trivial.
It also makes every session on that branch the same agent: a lease is
re-entrant for its own holder, so two of them acquire the same lock and
neither excludes the other — which is exactly the race the lock existed for,
now failing silently and looking like flakiness. The join does not need it.
The roster publishes `branch` as its own field, so a reaper can match on that
while ids stay distinct, and a branch survives a session restart where an
agent id does not.

## Reviewing across the seam

Two systems means two agents, and their review verdicts cross the same
channels the work does. **A verdict that can invalidate work in flight belongs
on the blackboard, not in a message.** A message expires in an hour; a merge
does not wait for it.

That failed here concretely. A rejected assumption was sent as a DM before the
other side had pushed anything, expired unread, and reached them via the
blackboard only after their branch had been squash-merged — leaving the
rejected design live on `main` for thirteen minutes until a follow-up landed.
Where the branch under review auto-deploys, that ordering is the difference
between a correction and a rollback. Put the reasoning on the board, and make
the pointer carry the verdict itself.

Two more things that exchange proved worth doing, both cheap:

- **Verify on `main`, not on a green merge.** The follow-up was found because
  someone checked that the fix was actually in the merged tree rather than
  inferring it from a passing merge. Squash takes the commits that exist at
  merge time, which is not necessarily the ones you pushed.
- **Mark any claim about a repo the other side cannot read.** In one session
  two unverified claims — one about a dependency version, one about a flag not
  working — reached a written deliverable before anyone checked them. Both were
  false. Whoever can see the code verifies; everyone else attributes.

## What the repo declares

None of the above needs Switchboard to know what a roadmap is. The concrete
shapes — what an item key looks like, which roles exist, where the plan lives
— are recorded by the repo itself with `switchboard refresh` and served back
by `help --role`. See [layers](layers.md) for why that boundary is where it
is.
