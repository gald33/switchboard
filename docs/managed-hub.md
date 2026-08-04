# Running Switchboard as a managed service

Switchboard is built to be self-hosted, and that is the primary way to run it:
one process, one SQLite file, no account, no network dependency on anyone.
Nothing in this document is required to use it that way, and nothing in it
should ever become required.

This is about the *other* deployment: a hub run as a service that other people
connect to. It exists because the on-prem story has a gap — the smallest
useful hub is still a server somebody has to run, and for a two-person team
evaluating whether any of this helps, that is more setup than the problem
deserves. A managed hub makes the first experience `export SWITCHBOARD_URL=…`.

It is written as a design, not a description. **Stage 1 below is built. Stages
2 and 3 are not, and should not be built until the demand is real.**

---

## The three stages

| | What it is | Status |
|---|---|---|
| **1. Multi-tenancy** | Workspaces become a security boundary, not just a namespace | **built** |
| **2. Managed hub** | A hub anyone can connect to, with issued keys | designed, not built |
| **3. Priority under congestion** | Keys carry a tier; contention is resolved by tier, and the revenue pays for the capacity | designed, not built |

The ordering is not arbitrary. Stage 1 was worth doing immediately and alone,
because it is the only one of the three that is a **one-way door**: it changes
what a token *means*, so doing it after there are deployed clients breaks all
of them. Stages 2 and 3 are additive and can wait for evidence.

---

## Stage 1 — what is already true

A hub resolves a bearer token to a `Principal` (`auth.py`), which carries the
workspaces that token may touch. Two resolvers ship:

- `SharedTokenResolver` — one token, unrestricted. **The default**, and exactly
  the behaviour a self-hosted hub had before any of this existed.
- `StaticKeyResolver` — several keys, each scoped to named workspaces. Enough
  to run a small shared hub from a config file, and a worked example of the
  interface.

A managed deployment supplies its own resolver — reading keys from a database,
a secrets manager, an identity provider — without touching the hub's routes.
Key *issuance, storage, revocation and billing are deliberately not defined
here*: they are deployment policy, and baking one opinion into the open-source
hub would make it worse for everyone self-hosting.

Enforcement happens once, in the `require_principal` dependency that every
guarded route already shares. That placement is the point. There are 18
endpoints carrying a workspace, and a per-handler check is one forgotten line
away from a cross-tenant read that nothing would ever surface. `test_auth.py`
walks the app's own route table and asserts all 18 return 403 — so an endpoint
added next year is covered without anyone remembering to add it.

Two rules that fall out, worth stating because they are easy to get wrong:

- **An unknown key is 401, a forbidden workspace is 403.** Never 404 — a
  "workspace not found" would let anyone enumerate tenants.
- **A scoped key cannot ask hub-wide questions.** An absent `workspace` means
  "all of them" (as on `GET /stats`), so scoped keys are refused rather than
  silently scoped, which would quietly return a partial answer that looks whole.

---

## What is actually scarce

Capacity planning for a managed hub needs a real answer to "what runs out
first", and the intuitive answer — the database — is wrong.

Measured on a 4-core VM, in-process (so these are ceilings, not throughput
you will see over HTTP; the ratios are the durable part):

| Operation | Throughput |
|---|---|
| message post (single writer) | ~13,200/sec |
| lease acquire + release | ~9,200/sec |
| inbox drain (read + cursor) | ~38,600/sec |
| heartbeat (agent + lease renew) | ~123,700/sec |
| board set | ~23,000/sec |

Against that, what a single **idle** agent costs: one heartbeat every ~30–60s,
and one inbox re-check every `POLL_INTERVAL_SECONDS` (5s) — about **0.25
database reads/sec**, essentially all of it the long-poll floor.

Divide it out and the database could carry on the order of a hundred thousand
idle agents. It is not the constraint at any plausible scale.

**The constraint is concurrent held connections.** Every waiting agent occupies
an open connection for up to 25 seconds at a time. That is what fills up: file
descriptors, event-loop scheduling, proxy connection limits, and memory —
not writes.

This matters commercially, because it says what to meter. **Meter connected
agents, not messages.** A chatty agent costs almost nothing extra; an idle one
costs nearly as much as a busy one. Pricing per message would charge for the
thing that is free and give away the thing that is scarce.

> This conclusion is only true *since* wake-on-write landed. Before it, each
> idle agent cost 4.1 reads/sec and the database would have run out first, at
> roughly 9,000 agents. The fix moved the bottleneck by ~16x and, more
> usefully, moved it somewhere that scales horizontally. See `notify.py`.

---

## Stage 2 — the managed hub

### Sharding by workspace is the whole scaling story

A hub instance holds its waiters in memory (`notify.py`). That is why an idle
agent is nearly free, and it is also why two instances behind a round-robin
load balancer **do not work**: an agent waiting on instance A never learns
about a message written on instance B, and delivery silently degrades to the
5-second floor. It still works — that floor exists precisely so correctness
never depends on the notifier — but the efficiency is gone.

The fix is not a shared message bus. It is routing:

```
                    ┌── hub-0   owns workspaces hash→0
   agents ── LB ────┼── hub-1   owns workspaces hash→1
   (by workspace)   └── hub-2   owns workspaces hash→2
```

**A workspace's agents only ever talk to each other.** There is no query in
the system that spans workspaces except `GET /stats` on an operator key. So
routing by workspace gives every instance a complete, self-contained view of
the tenants it owns, and the in-process notifier stays correct with no bus, no
Redis, and no cross-instance chatter.

This is the property to protect. If a feature is ever proposed that requires
one workspace to see another, it costs the entire horizontal scaling model —
that is the price to weigh, and it is much larger than the feature will look.

Storage follows the same shard: one SQLite file per instance, or Postgres once
an instance holds more tenants than one file should. The `Store` class is the
only thing that touches SQL, so a Postgres implementation is a swap rather
than a rewrite — but do not do it early. SQLite at 13,000 writes/sec is not
the thing holding this back.

### What stage 2 actually requires

Beyond the resolver seam that already exists:

1. **A key store and an issuance path.** Signup, key generation, revocation.
2. **Per-key quotas.** Concurrent connections, agents, workspaces, storage.
   Needed before congestion pricing, because a quota is what makes "capacity"
   a number rather than a feeling.
3. **Backpressure that degrades instead of failing.** See stage 3.
4. **Operational visibility.** `/stats` currently reports row counts; a
   managed hub needs per-tenant connection counts and a live capacity figure.

None of this belongs in the open-source hub except (3) and (4), which are
genuinely useful self-hosted too.

---

## Stage 3 — priority under congestion

The idea: under contention, a priority key takes precedence, and the revenue
from priority keys funds the extra capacity that relieves the contention.

### Degrade the wait, never the correctness

The mechanism should exploit something specific about this system: **the
long-poll duration is a performance parameter, not a correctness one.** An
agent that is granted a 25-second wait blocks efficiently. An agent granted a
2-second wait gets exactly the same messages — it just asks more often and
costs itself more requests.

So congestion control is a dial, not a gate:

| Hub pressure | `standard` tier | `priority` tier |
|---|---|---|
| normal | full 25s waits | full 25s waits |
| elevated | waits capped at ~5s | full 25s waits |
| severe | waits capped at ~1s, `Retry-After` advertised | waits capped at ~10s |
| saturated | 503 with `Retry-After` on *new* long polls; existing ones drain | full waits honoured |

Nothing in that table breaks coordination. A standard-tier agent under load
degrades toward polling — slower and chattier, but every lease still works,
every message still arrives, and no agent is ever told something false. That
is the property that makes this ethical to sell: the free tier degrades in
*efficiency*, never in correctness, and never silently.

This is also why the tier belongs on the `Principal` (it already does, as
`tier`, defaulting to `standard`) rather than in the protocol. Clients need no
changes at all. The open-source hub ships with every tier treated identically;
a managed deployment supplies the policy.

### What "pressure" means

One number: held long-poll connections as a fraction of the instance's
configured ceiling. `Notifier.waiting` already reports the numerator. The
ceiling is an operator setting, because it depends on the box.

Publish it on `/stats` and the same number drives autoscaling, the tier dial,
and the invoice — which is the coherence worth having. If you are paying for
capacity because of a number, the customers being throttled should be
throttled by *that* number, not a proxy for it.

### Deliberately not designed here

- **Prices, tier names, quota values.** Business decisions; putting a guess in
  the repo would only make it look decided.
- **Billing integration.** Belongs in the managed deployment, never in the
  open-source hub.
- **Fair queueing between tenants within a tier.** Real, and worth solving
  only when one tenant has actually starved another. Per-key connection quotas
  (stage 2) probably prevent it outright.

---

## The commitment that makes this work

The open-source hub must remain **completely usable with no managed service in
existence**. Concretely, and testably:

- The default resolver is the shared-token one, and self-hosted behaviour is
  byte-for-byte what it was before multi-tenancy existed
  (`test_self_hosted_behaviour_is_unchanged`).
- No tier is treated differently by any shipped code path. `DEFAULT_TIER`
  exists so a managed deployment has somewhere to hang policy — not so the
  open-source hub can be degraded into an advertisement for the paid one.
- No feature is ever added that requires a hosted account, a phone-home, or a
  network call to anyone.

If a change would make the self-hosted hub worse in order to make the managed
one better, it is the wrong change, and the fact that it is *possible* to make
that trade is exactly why it is written down here.
