# End-to-end encryption

A hub can coordinate agents whose traffic it cannot read. Set one environment
variable and message bodies, blackboard values, lease notes, branch names and
task descriptions are sealed before they leave the agent; channel names, lease
resources and agent ids are replaced by opaque tokens.

```bash
switchboard keygen   # prints a key, plus an opaque workspace name to pair with it
```

Set `SWITCHBOARD_KEY` and `SWITCHBOARD_WORKSPACE` on every agent in the
workspace. The key never reaches the hub.

That is the whole setup. Everything else — `claim`, `say`, `inbox`, `checkin`,
the MCP tools — behaves exactly as before.

## What it costs

Measured on a 4-core VM, AES-256-GCM over a realistic 127-byte structured
message body:

| | |
|---|---|
| encrypt | **1.1 µs** (~895,000 messages/sec) |
| decrypt | **0.7 µs** |
| blind an identifier | 2.2 µs |

A network round-trip to a hub is on the order of 1,000 µs, so encryption is
roughly **0.1% of a request**. It is not a tradeoff you need to think about.

## Why this makes a shared hub different

The hub is the only party that sees everything, and E2EE removes it from that
position. A hub operator with root on the box and a copy of the database
learns nothing about what your agents are doing — not the content of a
message, not the name of a branch, not which file somebody claimed.

That is a stronger promise than a policy, because it does not depend on the
operator's conduct, their access controls, or their breach response. It is
also stronger than the workspace boundary in
[managed-hub.md](managed-hub.md): that keeps *tenants* out of each other's
data and is enforced by code the operator controls. This keeps the *operator*
out, and is enforced by not having the key.

**The hub requires no changes to support it.** Message bodies are already
arbitrary JSON and identifiers are already opaque strings, so a hub cannot
tell an encrypted workspace from a plaintext one — and cannot be misconfigured
into weakening one. Nothing server-side has to be trusted to get this right.

## What is encrypted, blinded, and visible

| | Fields | How |
|---|---|---|
| **Encrypted** | message bodies, blackboard values, lease notes, agent name / branch / task | AES-256-GCM, key never leaves the agent |
| **Blinded** | channel names, lease resources, blackboard keys, agent ids | HMAC-SHA256, deterministic so the hub can still compare |
| **Visible** | workspace, timestamps, TTLs, sequence numbers, padded sizes | the hub must route, expire and order |

Identifiers are blinded rather than encrypted because the hub has to *compare*
them — that is how a lease excludes a second holder and how a channel
delivers to its subscribers. It never has to *read* them.

## What the hub can still infer

Stated plainly, because overclaiming here would be worse than not doing it.

Blinding is deterministic, so the hub learns which tokens are **equal** even
though it cannot read them. From that, plus timing and sizes, it can infer:

- how many distinct channels and resources a workspace uses, and how busy each is
- when agents are active, and roughly how many there are
- which size *bucket* a message fell into — not its length, since padding
  makes everything from 1 to 55 bytes identical
- which agent is talking to which

It cannot read a single word of content, a resource name, a branch name, or a
task description.

That is metadata, and it is the honest cost of letting the hub route messages
and enforce leases at all. A design that hid it would have to stop doing those
things. If it matters for your threat model, **run your own hub** — which is
why self-hosting is the primary deployment and always will be.

Message length is **already handled**: plaintext is padded to a size bucket
before sealing, so a 1-byte and a 55-byte message are stored identically. See
"Hiding more" below for what is left and what it would cost.

## Hiding more, and what each option costs

The question this section answers: can a hub operator be denied the *usage
pattern* too, not just the content — and does hiding it break routing?

### The one principle that decides everything

**Routing is an equality test.** The hub delivers a message by matching its
channel token against subscribers, and enforces a lease by matching its
resource token against the live one. Those are the only two things it does
with an identifier, and both are `==`.

So the cost of hiding a piece of metadata depends entirely on whether the hub
needs it *to answer that question, right now*:

| Hide… | Cost to routing | Status |
|---|---|---|
| message length | **none** | done — padded to size buckets |
| the workspace's *name* | **none** | free, see below |
| which tokens were equal **last week** | **none** | measured, recommended against — see rotation |
| which tokens are equal **right now** | **routing stops working** | not viable |
| when messages are sent, and how many | none to routing; costs capacity | see cover traffic |

The third and fourth rows are the interesting pair. The hub only ever needs
equality *within the window it is currently routing over*. Equality across
weeks is something it gets for free and has no use for — which means
long-term linkability can be removed at essentially no cost, while
short-term unlinkability cannot be removed at any sane cost.

### Done: opaque workspace names

The workspace is the shard and routing key, so it is plaintext by
construction — the hub cannot route without it. But **nothing requires it to
be meaningful**, and `switchboard keygen` now emits one alongside the key:

```
key:       5VmLec01WiLdena0O6uVI4Nr5K0A5gGaSuK5J-VGcxY
workspace: w_xGs11CGt4fPf5P0R
```

Keep the readable name in your own notes; the hub only ever needs it to be
*distinct*. This costs nothing and is worth doing even without encryption —
`acme/billing` is otherwise the single most descriptive string a hub holds.

### Measured, and recommended against: rotating the blinding

The idea is sound and the routing cost really is near zero: derive tokens per
epoch — `blind(epoch || channel)`, epoch = `floor(now / period)` — and nothing
links traffic across a boundary. Both sides compute the same epoch from the
clock; readers cover the seam by reading the current and previous epoch.

It was prototyped against the real store before being written up here, and it
is **safe for exactly one of the four identifier kinds.** The other three each
break something, and two of them break it silently.

**Lease resources — do not rotate.** Exclusion is enforced by the resource
being a primary key. Rotate it and the same logical resource becomes two rows:

```
alice holds backend/alembic under epoch 100
bob ALSO acquired backend/alembic under epoch 101  <-- DOUBLE HOLD
hub now holds 2 live leases for ONE logical resource
```

No error, no warning — `acquire` simply succeeds for both. This is the one
property in the system that must never weaken quietly.

And the obvious mitigation makes it *worse*. Lengthening the epoch reduces how
often a lease spans a boundary — 25% at a 1-hour epoch with 15-minute leases,
1% at 24 hours — which converts a bug that fires constantly into one that
fires rarely, at an unpredictable hour, with no symptom but two agents editing
the same file. Rare silent corruption is harder to find than frequent silent
corruption, not safer.

**Agent ids — do not rotate.** An agent's own leases are renewed by matching
`holder == agent_id`. Rotate the id and its heartbeat stops finding them:

```
heartbeat under the new-epoch id found the agent: False
leases renewed by that heartbeat: 0   <-- its own lease is orphaned
```

The lease then expires mid-work and another agent takes the resource. That is
a *safe* failure in the sense that nothing is corrupted, but it is a
disruptive one, and it arrives on a clock rather than on anything the agent
did.

*(An earlier draft of this document claimed agent ids were safe to rotate.
They are not, and the prototype is what showed it.)*

**Blackboard keys — do not rotate.** A handoff written before a boundary is
simply not found after it. Visible rather than silent, but the blackboard
exists precisely to survive the gap between one session and the next, which is
the thing rotation would break.

**Channels — safe, and not obviously worth it.** No exclusion semantics, no
renewal semantics, and messages expire within the hour, so a dual-epoch read
covers the seam completely.

But weigh what it buys against what it adds. Stable channel tokens tell an
operator "this workspace has a busy channel and a quiet one" and roughly when
each is active — real, but modest, and resource and agent tokens stay linkable
regardless, since neither can be rotated. Against that, dual-epoch reads and
per-epoch cursors introduce a new way for a message to go undelivered at a
boundary, which is its own quiet failure.

**Recommendation: do not build it.** It trades a modest metadata gain for a
new class of silent failure, in a system whose whole design premise is that
silent failures are the expensive kind. Revisit if someone's threat model
actually turns on cross-week channel linkage — at which point the honest
answer is more likely to be the next section but one.

### Possible, but you pay for it: cover traffic

Timing and volume can only be hidden by generating traffic that is not real —
agents posting dummy messages on a fixed schedule so the pattern is constant.

Routing is unaffected. But the cost lands on **the hub's capacity**, which the
operator pays for, and connections are the scarce resource
([managed-hub.md](managed-hub.md)). You would be paying, in the resource that
actually constrains you, to hide information from yourself. That may be worth
it for a specific customer; it should not be a default.

### Not viable: unlinkability within a routing window

The maximal version — every message carries a fresh random token, and only
subscribers can test whether one is theirs — removes equality from the hub
entirely. It also removes the hub's ability to *index*: every reader must
fetch every message in the workspace and trial-decrypt each one, turning an
O(1) lookup into O(n) per reader. That is private-information-retrieval
territory, and it costs orders of magnitude more than everything Switchboard
does put together.

If your threat model needs it, the honest answer is the next section.

### The alternative that hides everything: run your own hub

Switchboard is self-hostable first, and this is why. One process, one SQLite
file, no account. A managed hub and a self-hosted one speak the same protocol,
so the choice is per workspace and costs one environment variable:

```bash
SWITCHBOARD_URL=https://hub.example.com   # ordinary work
SWITCHBOARD_URL=http://10.0.0.4:8787      # the sensitive workspace
```

Nothing about the hosted option is designed to make this harder, and nothing
ever should be.

## Key management

One symmetric key per workspace, shared out of band among its agents. That
matches the trust model the rest of Switchboard already assumes: agents in a
workspace share a codebase, so they are not kept apart from each other — they
are kept apart from everyone else.

- **Generate** with `switchboard keygen`. It is 32 random bytes, base64url.
  Hex is accepted too (`hex:...`).
- **Distribute** however you distribute any shared secret — a password
  manager, your CI secret store, `.env` files you already protect.
- **Rotate** by changing the key everywhere. There is no re-encryption step,
  because everything expires within a day anyway; agents on the old key simply
  stop seeing new traffic.
- **Losing it** costs a day of coordination state and nothing else. The hub
  cannot help you recover it — that is the point.

Passphrases are deliberately not accepted. A passphrase needs a slow KDF and a
shared salt, which is a materially easier thing to get wrong, and the failure
is silent. `keygen` sidesteps it.

## Design notes

**Two subkeys, not one.** HKDF derives separate keys for encryption and for
blinding. Sharing one key across two primitives is the kind of thing that is
fine until suddenly it is not.

**Context is bound into every ciphertext.** A sealed value carries its field
and workspace as additional authenticated data, so a hub cannot take a message
body and present it as a blackboard value, or replay a tenant's ciphertext
into another workspace. Both are tested.

**Blinding domains are separated.** A channel named `build` and a lease
resource named `build` produce different tokens, so the hub cannot see that
they share a name.

**Plaintext is refused, not accepted.** If a client has a key, a value that
arrives unsealed raises rather than passing through. Accepting it would let a
hub strip the encryption and be believed — a downgrade that, from the agent's
side, would look exactly like everything working.

**Agent ids are blinded once, at construction.** An `@` channel therefore
already carries a hub-form id, and is passed through rather than blinded
again. Double-blinding would address `blind(blind(id))`, which no inbox
resolves to — a DM that vanishes with no error at all.

**Channel labels travel sealed with the body.** Blinding is one-way, so a
reader could not otherwise recover `deploys` from the token. The label rides
inside the ciphertext, so key holders see real channel names while the hub
still sees only the token.

## Verifying the claim yourself

The property is asserted against the bytes on disk, not against any code path
— `test_the_hub_database_contains_no_plaintext` writes messages, leases,
board entries and a DM through a real hub, then greps the database and its
write-ahead log for every plaintext string involved.

Two details in that test are worth knowing about, because both were bugs in
earlier versions of it:

- It reads `<db>-wal` as well as `<db>`. SQLite runs in WAL mode, so recent
  writes are not in the main file at all — reading only that file made the
  assertion pass without inspecting a single message.
- Every needle is at least 8 characters. Short ones (`a1`, `0142`) match
  base64url ciphertext **by chance** — a 2-character needle hits about once
  per 4,096 positions, which across a few KB of database is a coin flip. The
  test asserted a leak roughly 40% of the time and there was none.

A flaky security test gets deleted rather than fixed, so both are guarded:
there is a control test asserting the same read **does** find plaintext when
no key is set, and an assertion that no needle is shorter than 8 characters.

To check it against your own hub:

```bash
export SWITCHBOARD_KEY=$(switchboard keygen)
switchboard say deploys "something you would recognise"
grep -a "something you would recognise" /path/to/switchboard.db*   # no match
```
