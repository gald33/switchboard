# Layers, and how strongly each thing is held

Switchboard is used by more than one kind of caller, and most of what people
want from it is not true at the same level. A rule that belongs in the hub and
a rule that belongs in one company's roadmap tool look identical when they are
written down side by side — and once they are mixed, the hub grows opinions it
cannot enforce and the roadmap tool re-derives conventions it should have
inherited.

This is the map. It answers two questions about any proposed rule: **which
layer does it belong to**, and **how strongly is it held**.

## The layer test

Two questions, in order.

**1. Can the hub check it without understanding it?**

Under end-to-end encryption the hub sees opaque tokens and sealed bodies. It
routes and compares equality; it cannot read. That is not a policy that could
be relaxed — it is what [`crypto.py`](../src/switchboard/crypto.py) does. So a
rule that requires understanding a payload cannot live in the hub, whatever
anyone would prefer. It can only live in the client, which is the last place
that can see meaning, or in a convention.

**2. Does it presume an artifact not every caller has?**

A git checkout, a roadmap with arcs, a specific key naming scheme — each of
those pushes a rule one layer out.

## The four layers

### Layer 1 — Switchboard core

Any caller, coding agent or not.

| | |
|---|---|
| Lease exclusivity, TTL expiry, the 25s wait cap | Enforced |
| Presence, and what its lapsing means | Enforced |
| The `coord/` namespace and its three key shapes | Guidance |
| Payload on the blackboard, pointer in the message | Guidance |
| "What must outlive the work lives outside the hub" | Guidance |
| Non-blocking by default; check the roster before any live wait | Guidance |
| State your assumption in the question you ask | Guidance |
| One round trip per question — a second means the brief was wrong | Guidance |
| Timing forecasts, and the look/speak distinction | Optional affordance |
| `kind` as a **free-form** role label | Optional affordance |

The restraint that matters most here: **core enumerates nothing.**
`execution_class` is free-form and deliberately has no fixed list, and roles
follow the same rule. The moment this package defines what an "orchestrator"
is, it has taken an opinion the hub could never check — and it has become a
scheduler, which [the README](../README.md) says it is not.

### Layer 2 — coding agents

Presumes a repo.

| | |
|---|---|
| "Outside the hub" resolves to the repo: a commit, a PR body, a doc | Guidance |
| Promote findings out of `coord/reports/` before the 24h TTL takes them | Guidance |
| A branch carries durable context **as of dispatch**; the hub carries the invalidation | Guidance |
| Block only when the decision is expensive to revise | Guidance |
| Every agent can actually read the durable store | **Assumption** |

### Layer 3 — repos with a plan

Presumes something that decomposes work: a roadmap, arcs, dependency edges.

Switchboard ships **no content** at this layer. What it ships is the mechanism
for a repo to declare its own: [`spec.py`](../src/switchboard/spec.py) records
what a repo says, `switchboard refresh` is how an agent writes it down, and
`help --role` serves it back. Roles, claim naming and seam shapes all live in
`.switchboard/spec.json`, which the repo owns.

An overlay should describe **what a role owes the other roles** — what they
will ask it, what it must answer, when it should block — and not how to do the
role's own job. That belongs to whatever system decomposes the work.

### Layer 4 — one organisation's tooling

The concrete key shapes, where the plan physically lives, dispatch policy, the
mapping from a work item to an agent. None of it is Switchboard's, and all of
it is what `refresh` records. See [the seam](seam.md) for the specific case of
a roadmap product.

## The strength ladder

Five levels. Choosing the wrong one is how a rule becomes either unenforceable
or tyrannical.

| Level | Lives in | Example already in this repo |
|---|---|---|
| **Enforced** | the hub | lease exclusivity, TTL, workspace scoping, the wait cap |
| **Linted** | the client — warns, never blocks | the loopback-URL warning; the key-mismatch warning; the identity-drift warning |
| **Guidance** | `SKILL.md`, declared authoritative over ad hoc instruction | the `coord/` handoff convention |
| **Optional affordance** | offered, degrades silently when omitted | `execution_class` / `effort` |
| **Assumption** | unchecked, silent when violated | "agents in a workspace trust each other" |

**Linted is the level to reach for more often than feels natural.** The client
is the only place that can still see meaning, so it is the only place a
semantic rule can have teeth without the hub growing an opinion it cannot
justify. Every warning listed above exists because the failure it catches is
otherwise completely silent — which is the property that decides whether a
rule needs teeth at all.

**Assumptions must be written down, not merely held.** They are the class that
fails silently by construction, so an unlisted assumption is indistinguishable
from a bug. The ones this project makes are in
[the model](model.md) and in ["what this is not"](../README.md).

## Why the boundary is load-bearing

Switchboard owns three things and should own no more:

1. **Mechanism** — presence, leases, messages, blackboard, forecasts.
2. **A vocabulary** — key shapes and the payload/pointer split, so two
   independently written clients interoperate without having met.
3. **A refusal** — it will not interpret roles, decide assignment, or persist.

The third is not a limitation being worked around. Because Switchboard refuses
to schedule, whatever *does* own assignment has to be the component that knows
which work is parallel — which is the plan, not the hub. The refusal puts the
decision where the knowledge already is.
