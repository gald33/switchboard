# Pairing two machines

An invite is the room itself — hub, token and key in one string — so it is
exactly as secret as the room, for as long as the room lives. That is right
for a room that lives an afternoon. It is the wrong shape for two machines
that will work together for weeks, a laptop and a cloud session say, because
the string has to be carried somewhere first: a chat, an email, a ticket, a
pasted prompt. Whatever carried it keeps it. Anyone who reads that paper trail
next month holds the room, and nothing about the room tells you it is being
read.

Pairing carries one short-lived code instead, and both machines then derive
the rooms they share from a secret that neither of them ever sends. Once the
code has been redeemed, finding it in a paper trail is worth nothing.

```bash
switchboard pair start                          # on the machine that stays: prints a swbp1_ code
switchboard pair accept swbp1_...               # on the other one: prints pair:<name>
switchboard --room pair:<name> say pair "hi"    # either side, from then on
```

MCP has the same three steps: `pair_start`, `pair_accept`, then
`room="pair:<name>"` on any tool. The rest of this page is what those
commands do, what they cost, and what they cannot promise.

## Pairing or an invite

| | An invite (`swb1_…`) | A pairing code (`swbp1_…`) |
|---|---|---|
| What the string is | the room: hub, token, key, write key | half of a key exchange, and the address of a room to finish it in |
| What it is worth once carried | the room, for as long as the room lives | nothing, once it is redeemed or expires — 15 minutes by default |
| Right for | an afternoon; a viewer in a browser; a hosted model (`invite --no-key`) | two machines that will keep working together |
| What the other side needs | anything that reads invites: the SDK, the CLI, either bridge, a browser | switchboard on a machine of its own, CLI or local MCP bridge — never a hosted one |
| When a key leaks | mint a new room and hand it out again | `switchboard pair heal <name>`; both sides follow on their own |

The two are not exclusive. A paired work room can still be handed to a third
party as an ordinary invite (`switchboard --room pair:<name> invite`), and
`pair heal` is then how you take it back.

## The three rooms

Pairing uses three rooms, and none of them is the team key's. Every one is
*derived* — computed by each machine from a secret it holds rather than
minted by one side and sent to the other — and every one is write-protected
(`ws_…`, [encryption.md](encryption.md#read-only-rooms-enforced-by-the-hub)),
so knowing a room's identifier, which the hub does, is not enough to write
into it.

1. **The handshake room**, named by the code. The code is the one thing a
   human carries, so it is the thing most likely to be disclosed later. The
   room is short-lived and is built to hold nothing that would help whoever
   finds it: two public keys, two proofs, and a marker saying the code was
   used.
2. **The pair room**, derived from the pair secret. Long-lived in the sense
   that the secret is; on the hub it holds two sealed records, one per side,
   each rewritten while the pair is in use. It is used for nothing but keeping
   the pair in step: which work room is current, and whether either side has
   left.
3. **The work room**, generation *n*, derived from the pair secret and *n*.
   This is where the two sides actually coordinate — say, claim, board,
   listen — exactly as in any other room. When its key is exposed, a heal
   moves both sides to generation *n + 1*.

`pair:<name>` is the handle for the third, on every surface: `--room
pair:<name>` on the CLI, `room="pair:<name>"` on MCP. It names the pair, never
a key. Each use reads the pair room and resolves to whatever generation is
current, so the handle follows a heal on both sides without anybody retyping
anything.

## The handshake, step by step

1. **`pair start`, on the machine that stays (the initiator).** It mints a
   32-byte handshake secret `S`, an ephemeral X25519 keypair, and a random
   nonce it commits to. It leaves a sealed *hello* in the handshake room —
   one write, which anchors the code's expiry on the hub's clock instead of
   this machine's and lets the other side notice it is on the wrong hub. It
   saves the pending handshake *before* printing the code, so no code is ever
   in anybody's hands that this machine could not collect. The code carries
   the hub's URL, `S`, the ephemeral public key, the expiry and the
   commitment, the pair's name and note when given, and the hub token only
   with `--with-token`.
2. **`pair accept`, on the other machine (the responder).** It refuses a
   code that is clearly expired by its own clock before touching the hub,
   then reads the handshake room's `pair` channel and refuses — sending
   nothing — if the code was already answered or used, or if the hello the
   code promised is not there. Otherwise it makes its own ephemeral keypair,
   computes the pair secret `P` from the Diffie-Hellman result with `S` mixed
   in as a pre-shared key, and drops its private key. It saves the pair (the
   commit point: a machine that cannot keep the pair stops here, with the
   code unspent), then posts its *answer* — its public key and a MAC proving
   it derived `P` — and reads the channel again for a rival. It is now in
   work room 0, **unconfirmed**: usable at once, read by nobody until the
   initiator collects.
3. **Collection, on the initiator's next touch.** Exactly one in-time answer
   pairs. The initiator derives the same `P`, checks the answer's MAC, saves
   the pair, and announces it: its own record in the pair room (with its MAC
   and the nonce it committed to), a *consumed* marker in the handshake room,
   and a hello in work room 0 that wakes a responder parked there. Then it
   deletes the pending handshake — the only private key this package ever
   writes to disk.
4. **Confirmation, on the responder's next use.** It checks the initiator's
   MAC and that the nonce matches the commitment the code carried, marks the
   pair **paired**, and drops its copy of `S`.

Both sides then show the same **short authentication string**, `NNNN-NNNN`
(about 26.6 bits). Comparing the two is optional, and it is the initiator's
only defence against a raced code — see the threat model below. The nonce it
is computed from was committed to in the code and disclosed only after the
answer was fixed, so somebody racing for the code cannot grind their key until
their string matches the real one.

**Collection is not a step anybody has to remember.** The initiator is
usually turn-based: it printed a code and ended its turn, and nothing promises
it runs again within minutes. So every touch collects — a parked `switchboard
listen` (which parks in the handshake room and collects in-process when the
answer lands), `pair status`, `pair wait`, any `--room pair:<name>` command,
and on the MCP bridge any later tool call, whose result then carries
`pairings`. It may collect for 12 hours after the code expires; the answer
only had to arrive in time.

## Two machines on the CLI

On the laptop — the machine that stays:

```console
$ switchboard pair start --name laptop-cloud --note "the laptop, ~/src/api"
swbp1_eyJ2IjoxLCJ1IjoiaHR0cHM6Ly9odWIuZXhhbXBs…
```

Only the code is on stdout, so it can be piped. Stderr says what it is worth:
a credential until it is redeemed or expires (fifteen minutes here; `--ttl`
takes 60 seconds to 12 hours), who runs `pair accept`, and that this machine
collects the answer on its own. Hand the code to one place, once. The note
rides in the code and the other side's `--note` in its answer, both readable
by anyone who holds the code, so say which machine it is and nothing you
would not put in the ticket. Then park a listener as a background process, so
the answer is collected seconds after it lands rather than whenever somebody
next types:

```console
$ switchboard listen --until +900
```

`switchboard pair wait laptop-cloud --until 900` is the blocking version, for
a script: exit 0 paired, 2 still pending at the deadline, 1 it can never pair
(expired, or voided because the code leaked).

On the cloud session:

```console
$ switchboard pair accept swbp1_eyJ2IjoxLCJ1IjoiaHR0cHM6Ly9odWIuZXhhbXBs…
pair:laptop-cloud
```

Stdout is the handle and nothing else. Stderr says it is unconfirmed until
the laptop collects. A name chosen with `--name` travels in the code, so both
sides call the pair the same thing; without one, both derive the same
`pair-xxxxxx` from the code without agreeing it. Exit 2, not 1, means the
code was already answered by somebody else: it leaked on its way here, nothing
was sent, and the person who carried it needs to hear that rather than see a
retry.

Either side, once the laptop has collected:

```console
$ switchboard pair status
NAME                   ROOM                        ROLE       STATE            GEN  SAS
laptop-cloud           pair:laptop-cloud           initiator  paired             0  4821-0937
```

The cloud side prints the same row as `responder`, with the same SAS. If
somebody asks "is it the right machine?", that string, read off both screens,
is the answer.

From then on the pair is a room like any other:

```console
$ switchboard --room pair:laptop-cloud say pair "taking migrations 0142-0145"
$ switchboard --room pair:laptop-cloud claim backend/alembic
$ switchboard --room pair:laptop-cloud inbox -c pair
```

Talk on the `pair` channel. Neither side knows the other's agent id — a cloud
session's changes every session — so the channel is the address that does not
move, and it is what a listener parked in a pair's work room watches. A plain
`switchboard listen` parks in every pair used in the last day, alongside the
rooms it always parks in; `--only pair:laptop-cloud` parks there alone,
`--in pair:laptop-cloud` adds one that has been quiet, and `--no-pairs` leaves
pairs out. `--room` may come after the subcommand as well as before it.

## Two agents over MCP

This is the bridge on the agent's own machine. The hosted bridge withholds
every `pair_*` tool and refuses `pair:` handles (see
[chatgpt.md](chatgpt.md#what-it-withholds)): a hosted operator holding a code
could answer it in place of the intended peer, and a pair store is a directory
of secrets on whoever's disk the bridge runs on.

```
pair_start   name="laptop-cloud" note="the laptop, ~/src/api"
             → {code: "swbp1_…", name: "laptop-cloud", room: "pair:laptop-cloud",
                expires_at: "2026-10-16T16:05:00+00:00", state: "pending",
                verifiable: true, next: "…"}
```

`verifiable` is false when the hub could not be reached to leave the hello;
the code still pairs, but its expiry is this machine's clock, and a responder
on the wrong hub cannot be told so before it answers. The model hands `code`
to the human, who carries it to the other agent:

```
pair_accept  code="swbp1_…"
             → {paired: true, name: "laptop-cloud", room: "pair:laptop-cloud",
                state: "unconfirmed", generation: 0, warnings: [], next: "…"}
say          room="pair:laptop-cloud" channel="pair" message="here, taking the tests"
             → {…, pair: {name: "laptop-cloud", generation: 0, state: "unconfirmed",
                          warnings: ["unconfirmed: the other side has not collected …"]}}
```

A refusal is data, not an error: `{paired: false, reason, state, next}`, and
`state: "already_redeemed"` is the leaked-code case. Back on the laptop, a
later tool call collects and says so once:

```
roster       → {…, pairings: [{name: "laptop-cloud", state: "paired",
                               room: "pair:laptop-cloud", sas: "4821-0937"}]}
pair_status  → {pairs: [{name: "laptop-cloud", room: "pair:laptop-cloud",
                         role: "initiator", state: "paired", generation: 0,
                         peer_generation: 0, sas: "4821-0937", warnings: [], …}]}
```

Every result reached through `room="pair:<name>"` carries `pair: {name,
generation, state, warnings}`. The bridge re-reads the pair room at most every
30 seconds, and immediately after any `pair_*` tool, so a heal by the other
side is followed within half a minute. Warnings there are things to pass on —
unconfirmed, the other side a generation behind, the pair room unreadable —
not noise. No result ever contains the pair secret or a work-room key; only
`pair_start` returns the code. An agent with only MCP still parks its
listener through its shell (`switchboard listen`), and that listener collects
for it.

## What lives where, and for how long

Everything pairing leaves on the hub expires, like everything else there.
The one long-lived value it creates is the pair secret, and that is on the
two machines only.

| What | Where | Lives |
|---|---|---|
| the code | whatever carried it — a chat, a ticket | forever, but redeemable for 15 minutes by default (60 s to 12 h, `--ttl`), and worth nothing once redeemed or expired |
| the hello | handshake room, `pair` channel | the code's lifetime plus 17 minutes (the responder's clock slack and two minutes of skew) |
| the answer — the responder's public key, a MAC, its note | handshake room, `pair` channel | until the collect deadline — the code's expiry plus 12 hours, so at most 24 hours |
| the *consumed* or *aborted* marker | handshake room, `pair` channel | until the collect deadline |
| each side's record | pair room board, `coord/pair/side/<role>` | 7 days, the board ceiling; rewritten at most once a day while the pair is used |
| the *paired*, *moved* and *ended* notes | a work room, `pair` channel | an hour, like any message |
| what agents put in a work room | work room *n* | what it would anywhere: messages an hour, board entries a day (seven at most), leases their TTL |
| the pending handshake — `S`, the initiator's ephemeral private key, the nonce | the initiator only, `~/.switchboard/pairs/pending/<name>.json` | until the answer is collected and announced; one never collected is deleted at its deadline (the code's expiry plus 12 hours) by the first thing on this machine to pass — a parked listener, `pair start`, `pair status`, the MCP bridge |
| the pair record — `P`, the transcript, the generation, the SAS | both machines, `~/.switchboard/pairs/<name>.json`, or `SWITCHBOARD_PAIR_<NAME>` | until `pair forget` |
| the responder's copy of `S` | inside its pair record | until it is confirmed — it reads the handshake room for the initiator's verdict meanwhile |

The store is a directory, owner-only (0700, files 0600), and every write is a
temporary file renamed over the old one. Unlike every other store under
`~/.switchboard` it holds the *only* copy of a secret, so it does not fail
soft on write: a pairing command that cannot save says so and stops, before
anything is sent. `SWITCHBOARD_PAIRS` moves it; set to the empty string it
disables pairing on that machine, and the commands refuse rather than pair
into nowhere. It is per machine and per OS user — every agent running as this
user can use the pair, which is the boundary `signing.py` states for
everything else: the pair is between machines, not between processes. Losing
it costs a fresh pairing, not data; there is nothing else the secret opens.

## Healing a work room

A work room's key can get out — not the pair secret, just one room's key. The
usual way is the ordinary one: somebody ran `switchboard --room
pair:<name> invite` to bring a third party in, and the invite went somewhere
it should not have. The fix is one command, on either side:

```console
$ switchboard pair heal laptop-cloud --reason "work-room invite pasted into #general"
pair:laptop-cloud → work room 1
```

MCP: `pair_heal name="laptop-cloud" reason="…"` → `{name, room, from: 0, to:
1, left_behind, warnings}`.

The healing side writes a higher generation into its own record in the pair
room. The other side adopts it the next time it resolves the handle — any
`--room pair:<name>` command, a listener's wake, an MCP call — because the
current generation is simply the higher of the two records, and its record
moving up *is* the acknowledgement. Its stderr (or its `pair` field) then
says:

```
pair: moved from work room 0 to 1: the other side healed the pair (work-room invite pasted into #general)
```

Work room 1's key is derived one-way from the pair secret, so holding room 0's
key says nothing about room 1's: whoever had the invite is left in a room
nobody will use again. Two heals from both sides at once converge on the
higher generation; nothing is compare-and-set, so there is nothing to race.

**A heal is a room rotation**, and [the model](model.md) records rotating a
room's identifier as a rejected decision, because leases and the board are
compare-and-set per room. That reasoning holds here; it is why a heal is
never periodic and never automatic. It happens on an event — a key known to
have leaked — and both sides move only on the sealed signal in the pair room,
never on a clock. What it strands is said rather than discovered:

- **Leases.** Every lease in the old room stays there until its TTL and
  excludes nobody in the new one. `left_behind` lists the ones the healing
  agent held, by the hub's ids — claim again what you still need. The other
  side gets no list: what it held is stranded the same way.
- **Board entries** stay in the old room until they expire. Write again what
  still matters.
- **Unread messages** stay in the old room, which no handle names any more,
  and expire within the hour.
- **Until the other side resolves, the two sides are in different rooms**
  and do not exclude each other. The healing side's resolution keeps saying
  so ("the other side has not moved to work room 1 yet") until the other
  record catches up, and a *moved* note in the old room wakes a listener
  parked there, whose next resolution follows.

A heal refuses to run when the pair room cannot be read: a generation written
blind could land behind the other side's.

**What a heal cannot do** is rescue a leaked pair secret. Every generation
derives from it, so whoever holds it holds every room the pair will ever
have. That is a copied store file, a printed `pair export`, a compromised
machine — and the answer is `pair forget` on both sides and a fresh code.

### When the hub forgets both sides

Side records live seven days and are rewritten daily while the pair is in
use, so they outlive any gap shorter than a week. After a longer silence on
both sides the pair room is empty, and an absent record is ambiguous: the
other side may simply have been idle — or it healed past this one, then went
quiet long enough for its record to lapse, and resuming the last known
generation would walk back into the room the heal abandoned. So a side that
has not seen the other for six days, and finds its record gone, jumps
2²⁰ generations ahead — further than any run of heals in a forgotten week
could reach, so certainly a room nobody retired — and says so. The other side
follows on its next read, because the higher generation always wins. Nothing
is lost: everything in a work room has expired after a week of silence.

## Cloud containers, and `pair export --peer`

**Run `pair start` on the machine that stays, and `pair accept` on the one
that may not.** Until the answer is collected, the pending handshake on the
initiator is the only copy of the pairing. A container thrown away first takes
it along, and the other side is left unconfirmed until its deadline, when it
is told the pair was never confirmed. `pair start` warns when it is run in an
environment that looks like a cloud session, and so does `pair_start`.

The responder's pair record lives in `~/.switchboard/pairs/`, which a cloud
container does not keep either. If the pair should outlive the session, put
it in the cloud environment's secrets. Run this **on the laptop**:

```console
$ switchboard pair export laptop-cloud --peer
SWITCHBOARD_PAIR_LAPTOP_CLOUD=swbsecret1_eyJ…
```

`--peer` exports the *other* side's half — the same pair secret, the
responder's role — so the secret goes from the machine that stays into the
cloud environment's secret settings without ever passing through the cloud
session's own transcript. Every container that environment starts then comes
up already paired, under the same name, and resolves the current generation
from the pair room like any other run.

Stderr says what the value is, every time, `-q` or not: the pair's long-lived
secret. Every work room the pair will ever have derives from it, a heal does
not take it back, and only `pair forget` on both sides ends it. It goes in a
secret store, never in a conversation, a ticket or a commit — and a
`swbsecret1_` pasted where an invite or a code was expected is refused by
name, with the advice to treat it as disclosed. After a heal, a fresh
container still starts on the exported generation and catches up on its
first read; the resolution warns that the variable is behind, and `pair
export` again refreshes it.

`pair export <name>` without `--peer`, run inside the cloud session, prints
that side's own half — the same secret, printed into a transcript. Prefer
`--peer`.

## What it costs

Stated as plainly as the rest of the docs state theirs:

- **The code is a live credential for its window.** Fifteen minutes by
  default, and whoever holds it in that time can answer first. A shorter
  `--ttl` shrinks the window; nothing removes it.
- **The responder waits on a turn-based initiator.** It is in work room 0 at
  once, but nothing it says there is read until the initiator collects, and
  a message is gone after an hour: a pairing collected the next morning has
  lost whatever was said the night before. The board, which keeps a day, is
  the place for anything that has to wait. Parking a listener on the
  initiator is what makes "collected" mean seconds.
- **One private key on disk**, on the initiator, between printing the code
  and collecting the answer. One never collected is deleted once its
  deadline passes (the code's expiry plus 12 hours) by the first thing on
  this machine to pass by — a parked listener, `pair start`, `pair status`,
  the MCP bridge. Not by a timer: a machine that runs none of those keeps the
  file until it does.
- **The pair secret on two disks**, owner-only and in the clear, for as long
  as the pair lives — or in an environment's secret store, after `pair
  export`.
- **A round trip per use.** Every `--room pair:<name>` command reads the pair
  room before it does anything else, which is how it follows a heal; the MCP
  bridge does it at most every 30 seconds.
- **One hub.** The code names the hub, both records name it, and the rooms
  are on it. A pair does not move to another hub; pairing again on the new
  one is the move.
- **No agent ids across the pair.** A cloud session's id changes every
  session, so the two sides find each other on the `pair` channel, not by
  `dm`. A DM needs an id read off a message's `from`, as in a side channel.
- **No hosted bridge.** Pairing writes secrets to the disk it runs on, so it
  runs on your machines only.

## What the hub sees

The hub sees what it sees for any write-protected room, three or more times
over:

- **Room identifiers** — the handshake room's, the pair room's, and each work
  room's — and the public halves of their write keys, which the identifiers
  already commit to. They are unrelated by name: nothing in one says which
  pair or which code it belongs to.
- **Sealed, padded traffic**: in the handshake room a hello, one answer (or
  more, if the code leaked) and a marker, on one blinded channel; in the pair
  room two board entries under blinded keys, with their seven-day TTLs; in a
  work room, whatever the agents do there.
- **Timing and addresses.** The rooms are unlinkable by name, not by traffic:
  the same machine touches the handshake room, the pair room and work room 0
  within the same second, and a heal is visible as one room going quiet while
  another starts. A room that holds exactly two week-long board entries and no
  messages is a recognisable shape.

Handshake-room and pair-room writes go out under a throwaway agent id, fresh
per call, and register no presence: the handshake room is the one room whose
coordinates are expected to end up in a paper trail, and a roster row there
would hand a code holder the machine's name, branch and host. The hub never
sees the code, `S`, `P`, a room key, the SAS, a pair's name or note, which
side is which, or the generation number.

## What it protects against, and what it does not

**A paper trail read later.** The code and the hub's entire history together
— every message the handshake room ever held, kept past its TTL by a hub that
chose to — yield two public keys, a MAC, a transcript hash and two markers.
Both ephemeral private keys are gone by then: the responder never writes its
own down, and the initiator deletes its own once the answer is collected. This is the property
the whole design is for, and `tests/test_pairing.py` asserts it against what a
code holder can actually read.

**The hub.** It can withhold, delay, replay and refuse. It cannot read
anything, and it cannot forge anything a holder of the pair secret would
accept: an answer has to carry a MAC under a key salted with `S`, which the
hub never sees, so it cannot put its own key in the middle. It *can* serve a
stale record from the pair room, which is why a heal is acknowledged rather
than assumed, and a record served under the other side's slot is refused as
tampering rather than read as that side speaking.

**A code that leaked before it was redeemed.** Detected, not prevented. The
code is a **live bearer credential** until the initiator collects or it
expires, and anyone holding it in that window can answer in place of the
intended peer — they hold the handshake room's write key, so nothing the room
does can exclude them. What the room can do is keep a record nobody can take
back: the answer channel is append-only, so

- **two different answers abort the pairing on both sides** — "the code
  leaked", with nothing paired;
- **a redeemer after collection is refused** — "already used" — and told to
  treat the code as leaked;
- **an answer the hub stamped after the expiry is ignored by both sides**, and
  the initiator is warned that somebody used the code late.

What detection cannot cover is the intended peer never trying — the code was
intercepted rather than copied — or a hub that also holds the code choosing
which answer the initiator sees. In both, the initiator is paired with
somebody else and nothing in the protocol says so. **Comparing the SAS is the
initiator's only defence there**, and the one check that does not run through
the hub. Compare it when it matters which machine answered.

**A leaked work-room key.** `pair heal`, above. It moves both sides on and
leaves the holder of the old key in a dead room.

It does **not** protect against:

- **A leaked pair secret.** Every generation derives from it, past and
  future, so the handshake has forward secrecy and the generations do not. A
  machine compromised, a store file copied or a `pair export` printed into a
  transcript is a pair to forget and make again.
- **Other processes running as the same OS user.** They can read the pair
  store, as they can read `.claude/settings.local.json` and a signer's
  memory. The OS user was always the boundary.
- **A hub that will not serve you.** It can make pairing fail; it cannot make
  it succeed wrongly.

It also does not tell you *who* is on the other side — only that it is the
machine that redeemed this code, and, if you compare the SAS, that it is the
machine the human carried it to.

## When it goes wrong

Every refusal is a sentence that says what to do, and carries a
machine-readable `state` (the CLI's `--json`, the MCP results):

| `state` | Means | Do |
|---|---|---|
| `already_redeemed` | somebody else answered this code, or the initiator collected an answer that was not this machine's. CLI exit 2 | the code leaked: tell the initiator's side to `pair forget` what it paired, and pair again with a fresh code, shared more narrowly |
| `aborted` | more than one machine answered, so the code leaked and nothing was paired | pair again with a fresh code |
| `superseded` | the initiator collected an answer that was not this machine's; this side's record is gone | tell the other side — it is paired with whoever that was — and pair again |
| `expired` | the code expired, or the hub received the answer too late | ask for a fresh code; do not reuse the old one, which may already be in a paper trail |
| `wrong_hub` | the code says a hello was left, and this machine cannot see it: an expired code, or a different hub at that address — a container's own `127.0.0.1` | nothing was sent; check `SWITCHBOARD_URL`, or ask for a fresh code |
| `never_confirmed` | the initiator could have collected until its deadline and did not | `pair forget` here and pair again |
| `pending` | the initiator's side has no answer yet | wait, or park a listener |
| `ended_by_peer` | the other side ran `pair forget` | `pair forget` here, and pair again if it is still needed |
| `name_taken` | this machine already has a pair, or a pending pairing, by that name | `--name` something else, or `pair forget` the old one |
| `unreachable` | a heal could not read the pair room | try again when the hub is reachable |
| `tampered` | the pair room holds a record that is not what either side wrote | nothing was changed; suspect the hub |
| `upgrade` | the other side moved the pair on with a healing strategy this release does not know | upgrade agent-switchboard here |
| `disabled` | `SWITCHBOARD_PAIRS` is the empty string on this machine | unset it, or point it at a directory |
| `withheld` | a hosted bridge was asked to pair, or to reach a `pair:` room | pair from a bridge on your own machine, or with `switchboard pair` |
| `unknown` | no pair by that name on this machine | `pair status` lists them |

`pair status` reports a pair that fails as a row with its `error`, never as an
exception, so one dead pair does not hide the others; it exits non-zero only
when it was asked about one pair by name and that pair is the failure.

## The extension point

Each side record names the *healing strategy* the pair is on, in a field
called `s`. Protocol 1 knows one, `derive`: a heal is the next generation of
the same derivation, which is everything above, including its limit. A later
release can add a strategy whose heal mixes a fresh X25519 exchange from both
sides into the pair room — a ratchet — which would lock a *passive* holder of
a leaked pair secret out of the next generation. (An active one can write to
the pair room and could interpose on the exchange, so even that is recovery
from a copy, not from a compromised machine.) A record naming a strategy this
release does not know is refused with `upgrade` rather than guessed at, which
is what lets that release ship without an older peer silently downgrading a
pair the newer one moved on.

## Versions

The derivation labels are frozen for protocol 1 (`switchboard/pair/v1/…`, in
`pairing.py`). Two machines that paired on one release must keep landing in
the same rooms after either upgrades, so a change is a new protocol with its
own prefix, never an edit.

That looks inconsistent with the lobby, whose derivation carries no version
on purpose — but the two cases are opposites. The lobby is for agents
that do not ship together and have agreed nothing, so a version in its label
would sort them into different rooms by library version, silently. A pair
*has* agreed something, the handshake, and the protocol rides with it: in the
code, in each machine's record and in an export. An older client refuses a
newer code or record by name and says to upgrade, instead of deriving rooms
nobody else is in.

A release before pairing, handed a code, refuses it at the parse — `swbp1_`
is deliberately not an invite prefix, so it reads as "not a switchboard
invite" rather than being joined as an ordinary room — and has no `pair`
command and no `pair:` handle to resolve. Both machines need the release that
has it. The hub needs nothing: pairing is board values, messages and
client-side cryptography, on the endpoints every hub already serves. See
[upgrading.md](upgrading.md).

## How this differs from the original three-room sketch, and why

The design started as a sketch with the same three rooms: a handshake room
named by a one-off code, a long-lived pair room, and a work room. What
shipped keeps all three and changes how each is reached. Each change is
recorded here because each was the obvious first version, and each had a
quiet failure in it.

**D1. The pair secret is agreed, never sent.** In the sketch the initiator
minted the pair room's key and sealed it into the handshake room under the
handshake room's key, and the responder read it out. That makes the code's
paper trail exactly as dangerous as before — the code opens the handshake
room, and the handshake room holds the key — for as long as the hub keeps the
message. "The handshake message is short-lived" would then be a promise made
by the hub, and the hub is untrusted by construction
([the model](model.md): encryption is the boundary). So the responder answers
with an ephemeral X25519 public key instead, and both sides compute `P` from
the Diffie-Hellman result with `S` as a pre-shared key — the shape of Noise
`NNpsk0`, built from the primitives `crypto.py` already uses. A leak of the
code plus the hub's whole history now yields public keys. A property, not a
promise.

**D2. The initiator's public key rides in the code.** With an exchange in
place of a sealed key, the next obvious step is for the initiator to write
its public key into the handshake room, where the sketch wrote the key.
Carrying it in the code instead saves a round trip — the responder can
finish in one step without waiting on an initiator that is probably between
turns — and removes the one thing the initiator would otherwise have had to
write. It still writes one thing, a sealed hello, because that is cheap and
turns two failures loud: the code's expiry is set on the hub's clock rather
than a laptop's, and a responder on a different hub at the same address
learns so before answering into a room the initiator will never read.

**D3. The answer is a message on an append-only channel, and a race is
detected rather than excluded.** The natural first version is a board entry
written only-if-absent, first writer wins. But every code holder holds the
handshake room's write key, so a first-writer rule would be enforced against
nobody who matters: a rival could delete the entry and write its own, and the
loser would see a board that looked like it had always said that. Messages on
a channel cannot be taken back by a client. So the rule is about what both
sides read: exactly one in-time answer pairs; two distinct answers abort both
sides; a redeemer after collection is refused as "already used"; an answer
stamped after expiry is ignored by both. Detection, not exclusion — stated as
that, with the SAS for the case detection cannot reach.

**D4. Work rooms are derived, and each side only ever writes its own
record.** The sketch's pair room carried the work room's key, sealed, the way
its handshake room carried the pair room's. So a heal would mint a new key
and send it through the hub — the paper-trail problem again, one level down
— and update one shared "current room" record that two sides could race to
write. Instead work room *n* is `HKDF(P, n)`: nothing is
minted or sent, both sides are in work room 0 the moment they hold `P` (the
responder optimistically, unconfirmed until the initiator collects), and a
heal is one integer. Each side keeps one sealed record in the pair room
(`coord/pair/side/<role>`: its generation, its strategy, whether it ended the
pair, and for the initiator the confirmation MAC and SAS reveal). The current
generation is the higher of the two, so there is no compare-and-set and
nothing to race, and the other side's record catching up is the
acknowledgement.

Beyond the four: every room is write-protected rather than only sealed, so a
room's identifier — which the hub holds — admits no writes; the SAS, with its
commitment in the code, gives the initiator a check that does not run through
the hub; and a side that finds the pair room has forgotten both sides jumps
to a generation nobody could have retired rather than guessing which one is
safe.
