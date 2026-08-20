# The model

The authoritative statement of how identity, access and encryption work, and
why. Everything else in `docs/` describes a flow; this describes the shape all
the flows have to fit.

If something here and something elsewhere disagree, this is right and the other
is stale.

## The whole thing in six sentences

A **room** is identified by `hash(workspace_token)`. Two parties holding the
same token compute the same identifier without being told, so nothing has to be
registered, claimed or arbitrated. The **repo declares** which rooms it takes
part in; the **environment holds keys**; an agent joins the intersection. What
protects a room is that its identifier is unguessable and its contents are
sealed with a key the hub never receives. The hub is a router with a front
door. **Nothing in this system is ever registered** — agents announce, peers
witness, and everything expires.

## What each value is

| | What it is | Who has it | Where it lives |
|---|---|---|---|
| **workspace token** | names a room; `hash()` of it is the wire identifier | anyone in the room | committed `.switchboard/rooms.json`, or the gitignored overlay for a private room |
| **key** | seals content; never transmitted. Scoped to whoever shares it — usually a person or a team, not one repo: one key opens every room whose `key_id` names it | anyone who may *read* the room | `SWITCHBOARD_KEY_<ID>` in the environment |
| **hub token** | gets you through the front door | everyone, or one operator's users | committed `.mcp.json` when public; the environment when it is a secret |
| **signing key** | proves *which agent* wrote something | one agent, one process | memory only, never written anywhere |

An **invite** is those first three bundled into one string, so the values that
have to match a peer's cannot be assembled wrongly one at a time — every one
of them fails silently, and the result is a room you are alone in that looks
like a quiet one. `Client.from_invite(blob)` is the whole of joining; it does
no I/O, because verification is a round trip and belongs to the caller.

`verify()` then reads the invite's **proof-of-room**: a board key whose value
the inviter sealed. Opening it proves the hub, the workspace *and* the key all
match, which a roster listing you both does not. Three verdicts, because two
of the failures need opposite responses:

| | Means |
|---|---|
| `verified` | opened what the inviter sealed |
| `wrong_room` | the board holds entries this key cannot open — your key differs from theirs |
| `probe_gone` | the board reads cleanly and simply lacks the probe. It expired; nothing suggests the key is wrong |

The same string works from all three surfaces, and none of them touches your
environment or your checkout:

| | |
|---|---|
| SDK | `Client.from_invite(blob)`, then `verify()` |
| CLI | `--invite <blob>` on any command — one invocation runs in that room |
| MCP | `join_room` returns a handle; `room=` on any tool routes there |

A field the invite omits means "you already hold this" — that is what
`invite --no-key` and `--no-token` produce — so it falls back to the
environment. A field it carries **outranks** the environment, which is a
safety property rather than a convenience: nearly every agent has a key
exported, and an invite that lost to it would land the agent in the right
workspace on the wrong key. Registered, on a roster, reading nothing. A flag
that contradicts an invite is refused rather than merged; a half-invite
describes a room nobody is in.

## The five rules

**1. Identifiers are derived, never assigned.**
`hash(workspace_token)`, computed identically by everyone holding the token.
There is no name to claim, nothing to bind, and no authority to arbitrate. This
is why `key_bindings`, `/keys/register` and first-claim-wins are all gone.

*Consequence:* a leaked workspace token exposes that room's metadata forever,
because the identifier never rotates. Recovery is minting a new token and
updating the repo — a commit, not a protocol operation.

**2. The key and the room are chosen together.**
One record carries both, so they cannot disagree. Every bug in #52, #53 and #55
was a variant of "the key names one room and the routing names another", and
they are unrepresentable now rather than merely fixed.

*Consequence:* a room you hold no key for is a room you do not join, decided
offline before any hub call — a loud local error rather than an empty inbox.

**3. The hub has a front door, not authorization.**
One optional bearer token. Every admitted caller reaches every room whose
identifier it knows. On the managed hub the token is a **published constant**
that ships in `.mcp.json` beside the URL: nothing issues it, nobody types it,
and it exists to make untargeted scanning cost a string compare instead of a
database query. A hub wanting a real perimeter sets a secret instead, and that
one never goes in a committed file.

*This is not a boundary.* Anyone who reads the repository has the public token.
Do not build anything on top of it.

**4. Encryption is the boundary, and it rotates.**
The payload key is `KDF(key, workspace_token, epoch)` where
`epoch = floor(now / period)`, 15 minutes by default. Readers take the epoch
**from the message**, never from their own clock — which is why there is no
schedule to agree, no clock-skew window, and no unreadable history.

Blinded identifiers deliberately do *not* rotate: the hub compares them to
route, so a rotating blind key would stop a channel matching itself.

*Scope:* this bounds a leaked *derived* key to one period. It is **not**
forward secrecy — the key derives every epoch, past and future.

**5. Attribution is witnessed, not certified.**
Each agent process generates an Ed25519 keypair in memory and never writes it
anywhere, because the peers it distinguishes are usually sibling processes
sharing a filesystem. Messages are signed *inside* the sealed body, so the hub
cannot read, alter or strip the signature.

Peers accumulate the keys they have seen. A restart is ordinary and says
nothing; a key that changes while the same id is still heartbeating is another
agent announcing over one in use, and is reported.

An agent is not one process. The MCP server holds the key and signs on behalf
of the lifecycle hooks and every CLI command, over a unix socket both sides
locate without a handoff — `agent_id` is already derived deterministically. The
key stays in one process's memory; only signatures cross. Where a socket is
unavailable, each process signs as itself, which is what happened before.

*Consequence:* identity ends with the session, not the process. This buys
unforgeability within a live conversation, not reputation, and a rogue agent
can shed its identity by restarting.

*This grants any process running as the same user the ability to sign as this
agent* — not a new exposure, since such a process could already read the
server's memory. The OS user was always the boundary.

## Things that are true and easy to forget

- **`POST /agents/register` does not register anything.** It is an
  announcement: self-asserted, unvalidated, expiring in 120s unless
  heartbeated. The command is `announce`; `register` is a kept alias.
- **`/stats` deliberately does not list room identifiers.** Since the hub does
  no authorization, publishing them would let anyone enumerate every room and
  then read and post in all of them. It reports a count.
- **`meta` on an agent record is not sealed.** `host` and `repo` reach the hub
  in the clear even with encryption on.
- **The `uvx` bootstrap caches.** An environment can sit on an old build until
  `uvx --refresh`.

## Decisions that were made and rejected

Recorded because each was argued for at length, and the arguments recur.

**Rotating the room identifier.** Rejected: leases and the blackboard are
compare-and-set on `(workspace, …)`, so rotation silently breaks mutual
exclusion — two agents in adjacent epochs each acquire the same resource,
correctly, having asked different rooms. Any room rotation period is bounded
below by the longest-lived state keyed to the room.

**Operator-curated tokens scoped to workspaces** (`StaticKeyResolver`).
Rejected: it treats a workspace as ownable, which is the authority rule 1
removes.

**First-writer-wins on a published signing key.** Rejected: durable state where
the first claimant owns a name is a registry in disguise. The client notices
instead.

**A separate billing service for unlinkable payment.** Rejected as written: run
by the same entity, it is a promise rather than a property. Only blind
signatures would make it structural, and that is not the direction.

## Abuse control, once authorization is gone

See #72. The shape: **prioritize, never price.** The only quantity anyone has
to choose is a load target — queueing delay, not CPU — and effort orders the
queue above it. Three levels, in this order: class (so admission is never
starved), room (so a flood is contained), effort (so a flood inside a room
sorts itself). Rooms are free to create, so per-room fairness must sit
underneath something scarce or it amplifies.
