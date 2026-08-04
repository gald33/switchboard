# End-to-end encryption

A hub can coordinate agents whose traffic it cannot read. Set one environment
variable and message bodies, blackboard values, lease notes, branch names and
task descriptions are sealed before they leave the agent; channel names, lease
resources and agent ids are replaced by opaque tokens.

```bash
switchboard keygen              # prints a key; the hub never receives it
export SWITCHBOARD_KEY=<key>    # same key on every agent in the workspace
```

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
| which tokens were equal **last week** | **none** | designed, not built (see rotation) |
| which tokens are equal **right now** | **routing stops working** | not viable |
| when messages are sent, and how many | none to routing; costs capacity | see cover traffic |

The third and fourth rows are the interesting pair. The hub only ever needs
equality *within the window it is currently routing over*. Equality across
weeks is something it gets for free and has no use for — which means
long-term linkability can be removed at essentially no cost, while
short-term unlinkability cannot be removed at any sane cost.

### Free today: use an opaque workspace name

The workspace is the shard and routing key, so it is plaintext by
construction. But **nothing requires it to be meaningful**:

```bash
export SWITCHBOARD_WORKSPACE=w_7f3ca91b4e2d8065      # not "acme/billing"
```

Zero code, zero cost, and it removes the single most descriptive plaintext
string a hub holds. Keep the readable name in your own notes; the hub only
ever needs it to be *distinct*.

This is worth doing even without encryption.

### Designed, not built: rotating the blinding

Today `blind(channel)` is stable forever, so an operator can watch one channel
across months. Deriving it per epoch instead —
`blind(epoch || channel)`, epoch = `floor(now / period)` — means tokens change
on a schedule and nothing links traffic across a boundary.

Routing cost is genuinely near zero: both sides compute the same epoch from
the clock, and readers subscribe to the current and previous epoch to cover
the seam. Cursors reset per epoch, which is fine because messages expire in an
hour anyway.

**It is not built because leases make it dangerous.** A lease's identifier
must stay stable for the lease's whole life, and exclusion is the one property
in this system that must never quietly weaken. If a token rotates mid-lease,
two agents can hold what they both believe is the same resource under
different tokens, and **nothing errors** — the second acquire simply succeeds.
That is the worst failure this system can have, and it would appear only under
a clock boundary, which is exactly when nobody is watching.

Three ways to make it safe, none free:

1. **Epoch period well above the maximum lease TTL** (currently 24h), so a
   lease cannot span a boundary. Simple, but a 48-hour epoch buys much less
   unlinkability than an hourly one.
2. **Acquire under both epochs near a boundary.** Correct, and doubles the
   lease bookkeeping at exactly the moment it is hardest to reason about.
3. **Rotate channels and agent ids only, never resources.** Cheap and safe,
   but leaves resource names linkable — and a resource name like
   `backend/alembic` is as descriptive as anything else here.

(3) is probably where this should land, with (1) as a bound. It is a real
feature with a real hazard, so it needs a decision rather than a default.

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
