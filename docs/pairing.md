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
machine that made the code has collected the answer — or the code has expired
with nobody answering it — finding it in a paper trail is worth nothing. Until
then it is a live credential: whoever holds it can answer in place of the
machine it was meant for, or void the pairing.

```bash
switchboard pair start                          # on the machine that stays: prints a swbp1_ code
switchboard pair accept swbp1_...               # on the other one: prints pair:<name>
switchboard --room pair:<name> say pair "hi"    # either side, from then on
```

MCP has the same three steps: `pair_start`, `pair_accept`, then
`room="pair:<name>"` on any tool. The rest of this page is what those
commands do, what they cost, and what they cannot promise.

## Pairing or an invite

| | An invite (`swb1_…`) | A pairing code (`swbp1_…`) | A shared room (`shared:<name>`) |
|---|---|---|---|
| What the string is | the room: hub, token, key, write key | half of a key exchange, and the address of a room to finish it in | there is none: a room one machine passes to the machines it is paired with, sealed inside their pair rooms |
| What it is worth once carried | the room, for as long as the room lives | the pairing, until the answer is collected; nothing after that, or once it expires unanswered — 15 minutes by default | nobody carries it |
| Right for | an afternoon; a viewer in a browser; a hosted model (`invite --no-key`) | two machines that will keep working together | a room several paired machines work in |
| What the other side needs | anything that reads invites: the SDK, the CLI, either bridge, a browser | switchboard on a machine of its own, CLI or local MCP bridge — never a hosted one | a confirmed pair with the machine sharing it, on a machine of its own |
| When a key leaks | mint a new room and hand it out again | `switchboard pair heal <name>`; both sides follow on their own | a new room without whoever holds it (`share new`), and everyone moving; nothing is revoked |

The three are not exclusive. A paired work room can still be handed to a third
party as an ordinary invite (`switchboard --room pair:<name> invite`), and
`pair heal` is then how you take it back. A room that more than two machines
work in is a shared room: one machine mints it and offers it over its pairs,
and each machine it is offered to decides whether to keep it — see
[sharing.md](sharing.md).

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
   finds it: the responder's public key and proof, and a marker saying which
   answer was collected, which carries the initiator's proof and — disclosed
   once it can no longer be used to cheat — the nonce behind the SAS. The
   initiator's public key is never in the room; it travels only in the code.
2. **The pair room**, derived from the pair secret. Long-lived in the sense
   that the secret is; on the hub it holds two sealed records, one per side,
   each rewritten while the pair is in use, which keep the pair in step:
   which work room is current, and whether either side has left. It also
   carries, on purpose, the shared rooms each side offers the other
   ([sharing.md](sharing.md)): one sealed record per room per side, under
   `coord/share/`. Those rooms are minted, not derived, so their keys are in
   the records — and the pair secret opens every room currently offered over
   the pair as well as the pair's own.
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
   nothing — if the code was already answered or used, if the room is too
   full of messages to read whole, or if the hello the code promised is not
   there. Otherwise it makes its own ephemeral keypair, computes the pair
   secret `P` from the Diffie-Hellman result with `S` mixed in as a
   pre-shared key, and drops its private key. It saves the pair (the commit
   point: a machine that cannot keep the pair stops here, with the code
   unspent), then posts its *answer* — its public key and a MAC proving it
   derived `P` — and reads the channel again. If the initiator has already
   collected this answer, nothing it finds there changes that. Otherwise a
   rival answer, a marker, or a flood voids this side: its record is deleted
   and marked ended in the pair room, so an initiator that collected it a
   moment earlier hears that the pair ended instead of sitting in a quiet
   room. It is now in work room 0, **unconfirmed**: usable at once, read by
   nobody until the initiator collects.

   If the hub fails after the pair is saved, the answer may never have
   arrived. The pair is kept as *unsent*, `accept` says so (`state:
   "unsent"`), and re-running it with the same code — or any use of the pair
   at all, `pair status` included — sends the saved answer again. It counts
   only if the hub receives it within two minutes of the code's expiry; the
   retries go on a little longer than that only so a responder whose clock
   runs fast is not refused early. Every use first checks whether the answer
   did arrive after all — a post whose reply was lost, a proof from the
   initiator in the pair room — and only an empty handshake room past the
   code's life deletes the pair, reported `unsent`. If somebody else's answer
   is there instead, it is said as the leak it is (`already_redeemed`).
3. **Collection, on the initiator's next touch.** Exactly one in-time answer
   pairs, and anything else in the handshake room is read as what it is —
   proof that somebody else holds the code — and aborts on both sides: a
   second answer; a *consumed* or *aborted* marker this machine did not
   write; an answer that is not a usable key or whose MAC does not verify;
   or a room too full to read whole. That last one is a ceiling, not a
   guess: the hub returns at most 500 messages, newest first, and the oldest
   are the ones that say who answered first, so a full read means somebody
   holding the code pushed them out of sight. On a single good answer the
   initiator derives the same `P`, saves the pair, and announces it: its own
   record in the pair room (with its MAC and the nonce it committed to), a
   *consumed* marker in the handshake room carrying the same MAC and nonce,
   and a hello in work room 0 that wakes a responder parked there. Then it
   deletes the pending handshake — the only private key this package ever
   writes to disk.
4. **Confirmation, on the responder's next use.** It checks the initiator's
   MAC and that the nonce matches the commitment the code carried — read
   from the initiator's record in the pair room or from the consumed marker,
   whichever it finds first — marks the pair **paired**, and drops its copy
   of `S`. A consumed marker without that MAC confirms nothing: anyone with
   the code can write one, and only a holder of `P` can compute the MAC.
   From then on the responder's own record carries the initiator's proof
   too, so a fresh container started from an export can still confirm after
   the initiator's record has expired.

Both sides then show the same **short authentication string**, `NNNN-NNNN`
(about 26.6 bits). Comparing the two is optional, and it is the initiator's
only defence against a raced code — see the threat model below. Compare the
SAS before sharing over a pair: a pair with whoever raced the code hands them
every room offered over it ([sharing.md](sharing.md#what-a-leak-opens)). The nonce it
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
only had to arrive in time. A code nobody answered in time is settled the
other way just as promptly: the first collection after its expiry plus four
minutes — twice the clock skew allowed — reports it `expired` and deletes the
pending handshake, rather than calling it pending, and redeemable until a
time already past, for another half day.

## Two machines on the CLI

On the laptop — the machine that stays:

```console
$ switchboard pair start --name laptop-cloud --note "the laptop, ~/src/api"
swbp1_eyJ2IjoxLCJ1IjoiaHR0cHM6Ly9odWIuZXhhbXBs…
```

Only the code is on stdout, so it can be piped. Stderr says what it is worth —
a credential, and until when (fifteen minutes here; `--ttl` takes 60 seconds
to 12 hours) — who runs `pair accept`, and that this machine collects the
answer on its own. Until it has, whoever holds the code can answer in place
of the other machine, or void the pairing. Hand the code to one place, once.
The note rides in the code and the other side's `--note` in its answer, both
readable by anyone who holds the code, so say which machine it is and nothing
you would not put in the ticket. Then park a listener as a background
process, so the answer is collected seconds after it lands rather than
whenever somebody next types:

```console
$ switchboard listen --until +900
```

`switchboard pair wait laptop-cloud --until 900` is the blocking version, for
a script: exit 0 paired, 2 still pending at the deadline, 1 it can never pair
(expired, voided because the code leaked, or an answer that never reached the
hub in time).

On the cloud session:

```console
$ switchboard pair accept swbp1_eyJ2IjoxLCJ1IjoiaHR0cHM6Ly9odWIuZXhhbXBs…
pair:laptop-cloud
```

Stdout is the handle and nothing else. Stderr says it is unconfirmed until
the laptop collects. A name chosen with `--name` travels in the code, so both
sides call the pair the same thing; without one, both derive the same
`pair-xxxxxx` from the code without agreeing it.

Exit 2, not 1, means the code was answered by a machine other than this one,
and this machine is not paired. Usually that is the leak it sounds like —
somebody else holds the code, and the person who carried it needs to hear
that rather than see a retry. One way to get here is not a leak, and the
refusal says so: a cloud session redeeming the same code again from its
transcript after its container was replaced, within the code's life (the
first answer was the old container's, and that pair went with it). And one
leak looks different: a rival answering at the same moment as this machine
voids this side even though its answer was sent — the pair room is told, so
an initiator that had collected it hears the pair ended. In every case the
machine that made the code should `pair forget`
it and make a fresh one.

Exit 1 with `unsent` means the pair is saved here but the hub never
acknowledged the answer. Run the same `pair accept` again before the code
expires — or any `pair` command that touches the pair — and the saved answer
is sent again; nothing new is derived.

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
`switchboard listen` parks in every pair used in the last day, and in every
pair carried in `SWITCHBOARD_PAIR_<NAME>` — a fresh container's export says
when the *exporting* machine last used it, which is no reason to skip it —
alongside the rooms it always parks in. Used means worked over: the share
reads that look at every pair on a schedule (`share status`, a listener
arming, the MCP bridge's sweep) do not count, or every pair would read as
used within the minute; a pair that carries shared rooms is parked in for
that reason instead ([sharing.md](sharing.md#listening)). A pair it cannot read or resolve costs
only its own room, with a line on stderr saying why — even under `-q`, since
that is a failure, not chatter; only the hint about a pair skipped for being
idle, naming the `--in` flag that adds it, is dropped by `-q`. `--only
pair:laptop-cloud` parks there alone, `--in pair:laptop-cloud` adds one that
has been quiet, and `--no-pairs` leaves pairs out. `--room` may come after
the subcommand as well as before it.

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

A refusal is data, not an error: `{paired: false, reason, state, next}`.
`state: "already_redeemed"` is the answered-elsewhere case above — usually a
leak, and the `reason` says what else it can be — and `"unsent"` is the saved
pair whose answer has not reached the hub yet. Back on the laptop, a later
tool call collects and says so once:

```
roster       → {…, pairings: [{name: "laptop-cloud", state: "paired",
                               room: "pair:laptop-cloud", sas: "4821-0937"}]}
pair_status  → {pairs: [{name: "laptop-cloud", room: "pair:laptop-cloud",
                         role: "initiator", state: "paired", generation: 0,
                         peer_generation: 0, peer_present: true,
                         peer_active_at: "2026-10-16T16:04:12+00:00",
                         sas: "4821-0937", warnings: [], …}]}
```

`peer_present` says whether the other side's record is in the pair room, and
`peer_active_at` when the other side last wrote it — when it last used the
pair, to within a day.

Every result reached through `room="pair:<name>"` carries `pair: {name,
generation, state, warnings}`. The bridge re-reads the pair room at most every
30 seconds, and immediately after any `pair_*` tool, so a heal by the other
side is followed within half a minute. Warnings there are things to pass on —
unconfirmed, the other side a generation behind, the other side quiet for
days or gone from the pair room, an exported variable out of date, the pair
room unreadable — not noise. No result ever contains the pair secret or a
work-room key; only `pair_start` returns the code. An agent with only MCP
still parks its listener through its shell (`switchboard listen`), and that
listener collects for it.

## What lives where, and for how long

Everything pairing leaves on the hub expires, like everything else there.
The one long-lived value it creates is the pair secret, and that is on the
two machines only.

| What | Where | Lives |
|---|---|---|
| the code | whatever carried it — a chat, a ticket | forever, but redeemable for 15 minutes by default (60 s to 12 h, `--ttl`); a live credential until the answer is collected, and worth nothing after that or once it expires unanswered |
| the hello | handshake room, `pair` channel | the code's lifetime plus 17 minutes (the responder's clock slack and two minutes of skew) |
| the answer — the responder's public key, a MAC, its note | handshake room, `pair` channel | until the collect deadline — the code's expiry plus 12 hours, so at most 24 hours |
| the *consumed* marker — the collected transcript, the initiator's MAC, the SAS nonce — or an *aborted* one | handshake room, `pair` channel | until the collect deadline |
| each side's record — its generation, strategy, last write, whether it ended the pair, and the initiator's proof once this side has it | pair room board, `coord/pair/side/<role>` | 7 days, the board ceiling; rewritten at most once a day while the pair is used |
| each side's share records — per shared room, the room offered (its keys included) or a `left` ([sharing.md](sharing.md)) | pair room board, `coord/share/<role>/<rid>` | 7 days; rewritten at most once a day while the room is held, or until a `left` is acknowledged |
| the *paired*, *moved* and *ended* notes | a work room, `pair` channel | an hour, like any message |
| what agents put in a work room | work room *n* | what it would anywhere: messages an hour, board entries a day (seven at most), leases their TTL |
| the pending handshake — `S`, the initiator's ephemeral private key, the nonce | the initiator only, `~/.switchboard/pairs/pending/<name>.json` | until the answer is collected and announced, or the pairing aborts. A code nobody answered in time is deleted by the first collection after its expiry plus four minutes — `pair status`, `pair wait`, a `--room pair:<name>` command, the MCP bridge's next call. Any pending handshake is deleted at its deadline (the code's expiry plus 12 hours) by the first thing on this machine to pass — a parked listener, `pair start`, `pair status`, `pair wait`, the MCP bridge |
| the pair record — `P`, the transcript, the generation, the SAS | both machines, `~/.switchboard/pairs/<name>.json`, or `SWITCHBOARD_PAIR_<NAME>` | until `pair forget`, or until a resolution finds this side's own record marked ended; a responder's also until its handshake voids it (`aborted`, `superseded`, `already_redeemed`, a late `expired`, `unsent`) |
| the responder's copy of `S` | inside its pair record | until it is confirmed — it reads the handshake room for the initiator's verdict meanwhile |

The store is a directory, owner-only (0700, files 0600), and every write is a
temporary file renamed over the old one. Unlike every other store under
`~/.switchboard` it holds the *only* copy of a secret, so it does not fail
soft on write: a pairing command that cannot save says so and stops, before
anything is sent. `SWITCHBOARD_PAIRS` moves it; set to the empty string it
disables pairing on that machine. `pair start`, `accept`, `heal`, `forget`,
`export` and `--room pair:<name>` then refuse with `disabled`, rather than
pair into nowhere or answer "no pair called …" about a pair that is sitting
on disk unread while the other side keeps talking into a room nobody here
will open. `pair status` lists only the pairs carried in
`SWITCHBOARD_PAIR_<NAME>`, which still work, and refuses with `disabled` when
there are none. It is per machine and per OS user — every agent running as
this user can use the pair, which is the boundary `signing.py` states for
everything else: the pair is between machines, not between processes. Losing
it costs a fresh pairing, not data. Leaking it costs more than the pair's own
rooms: every shared room currently offered over the pair travels sealed under
the secret, so whoever copies a pair record holds those rooms too, and a heal
does not take them back ([sharing.md](sharing.md#what-a-leak-opens)).

Several processes on one machine — the CLI, the MCP bridge, a parked listener
— can touch one pair at once, and they do not take turns across a hub call.
So nothing one of them writes can undo what another just did. A refresh or a
heal rewrites this side's record in the pair room only if it is still the
revision that was read: a resolution that loses reads again, and a heal that
loses refuses with `conflict`. A resolution saves its record merged with
whatever is on disk now — never a lower generation than a heal saved, never
back after a `pair forget` deleted it. And `pair forget` deletes the local
record before it tells the other side, so a resolution racing it finds
nothing to save.

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
higher generation: each machine writes only its own record, so between the two
machines nothing is compare-and-set and there is nothing to race. (Between
processes on one machine there is, and it is handled as described above.)

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
  and do not exclude each other. The heal says so, and the healing side's
  resolution keeps saying so ("the other side has not moved to work room 1
  yet") until the other record catches up. A *moved* note goes into the room
  this agent was working in before the heal — and into the room the heal's
  own read resolved to, if that was a different one — and wakes a listener
  parked there, whose next resolution follows. It goes out under a throwaway
  agent id, so the listener on the healing machine wakes too: a listener
  never wakes on its own agent's messages.

A heal refuses to run when the pair room cannot be read (`unreachable`): a
generation written blind could land behind the other side's. It refuses with
`conflict` when this side's record changed between the heal's read and its
write — another process on this machine refreshing or healing it — rather
than write over a view newer than its own; run it again.

**What a heal cannot do** is rescue a leaked pair secret. Every generation
derives from it, so whoever holds it holds every room the pair will ever
have — and a heal moves only the work room, never the pair room, so they
also keep every shared room offered over the pair. That is a copied store
file, a printed `pair export`, a compromised machine — and the answer is
`pair forget` on both sides and a fresh code, then a new room for anything
that was shared over the old pair ([sharing.md](sharing.md#what-a-leak-opens)).

### When the other side goes quiet

A work room the other side no longer reads looks exactly like a quiet one, so
every resolution says how long it has been. Each side's record carries the
time it was written, sealed — the hub can serve an older record but cannot
forge a newer time — and a record is rewritten at most once a day while the
pair is in use, so it says when the other side last used the pair to within
a day. `pair status --json` and MCP's `pair_status` carry it as
`peer_active_at`.

- After **no write from the other side for three days**, with its record
  still in the pair room: "no sign of the other side since …: it has not used
  this pair for at least N days, so anything said in the work room waits for
  it".
- Once its record has **gone from the pair room** — seven days without a
  write: "the other side's record is not in the pair room: it has not used
  this pair for over a week, or it lost it (a container restarted without
  `pair export --peer`). Nothing said in the work room is read until it comes
  back".

Neither warning moves anything. A side that keeps working while its peer is
away stays in its room for as long as the peer is gone, and its own record
stays in the pair room, rewritten daily, for the peer to find when it returns.

### When this side has been away

Side records live seven days, and every read of the pair room sees what is
there. So the one way a heal by the other side can go unseen is for this side
to stay away longer than a record lives: the other side heals, goes quiet,
and its record — the only place the new generation was written — expires
before this side next looks. An absent record after a gap like that is
ambiguous. The other side may simply have been idle, or it may have healed
past this one, and resuming the last known generation would walk back into a
room abandoned because its key leaked.

So a confirmed side that **has not read the pair room for six days** —
counted from its last read, or from its own record's last write if that is
later — and finds the other side's record gone does not guess. It jumps 2²⁰
generations past the highest generation it knows, plus a random amount below
2³², and says so ("this side had not read the pair room for N days … moved to
a fresh work room"). The random part is what makes the room fresh: two
lapses from the same starting point — two containers started a week apart
from one stale `pair export` — pick different rooms, where a fixed jump would
send both into the same one, even after a heal had abandoned it because its
key leaked. A repeat is a one-in-2³² chance, not an impossibility.

A lapse happens once per gap: the read that decided it is itself a read, so
the next resolution sees no gap and stays where it is. The other side follows
on its next read, because the higher generation always wins, and is told "the
other side came back to a pair room that had forgotten this side and moved to
a fresh work room" — not that it healed, which would read as a leaked key.
Two sides that come back at the same moment, both reading before either
writes, lapse into different rooms for one read; the next converges them on
the higher, and the side that moved is told. Whatever is still alive in the
old room stays there. After six days in which neither side touched the pair,
that is at most a board entry written to last the week, or a long lease.

## Cloud containers, and `pair export --peer`

**Run `pair start` on the machine that stays, and `pair accept` on the one
that may not.** Until the answer is collected, the pending handshake on the
initiator is the only copy of the pairing. A container thrown away first takes
it along, and the other side is left unconfirmed until its deadline, when it
is told the pair was never confirmed. `pair start` warns when it is run in an
environment that looks like a cloud session, and so does `pair_start`.

The responder's pair record lives in `~/.switchboard/pairs/`, which a cloud
container does not keep either. A replacement container has no pair at all —
`--room pair:<name>` says so — and the code still in the session's
transcript cannot bring it back: redeeming it again is refused — almost
always as `expired`, since a container is rarely replaced within fifteen
minutes of the code's expiry, and as `already_redeemed` if it is — because the
answer the laptop holds was the old container's and its key went with it.
Both refusals say so. On the laptop that shows as the other
side going quiet, then gone (above). If the pair should outlive the session,
put it in the cloud environment's secrets. Run this **on the laptop**, once
`pair status` there shows the pair as paired:

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
not take it back, and only `pair forget` on both sides ends it. It opens more
than the pair's own rooms, too: every shared room currently offered over the
pair travels sealed under it ([sharing.md](sharing.md#what-a-leak-opens)), and
nothing revokes those. It goes in a secret store, never in a conversation, a
ticket or a commit — and a `swbsecret1_` pasted where an invite or a code was
expected is refused by name, with the advice to treat it as disclosed.

An export does not have to be kept current to keep working, but a stale one
is said. After a heal, a fresh container still starts on the exported
generation and catches up on its first read of the pair room; its resolution
warns that the variable is behind and says what fixes it — on the machine
that stays, run `switchboard pair export <name> --peer` and replace the
variable with what it prints. The warning is worded for the side that sees
it, which is the one running from the variable: it never suggests a bare
`pair export` there, because that would print the secret into its own
session. The same warning comes up if the variable was exported before the
pair was confirmed. An export carries no record of when the exporting machine
last used the pair, so a listener in a fresh container parks in it whatever
the export says.

`pair export <name>` without `--peer`, run inside the cloud session, prints
that side's own half — the same secret, printed into a transcript. Prefer
`--peer`. And it refuses (`unconfirmed`) on a responder the initiator has
not collected yet: frozen unconfirmed, that copy would outlive the
initiator's proof in the pair room, and a container started from it a week
later would be told the pair was never confirmed and advised to forget a
pair that works. Wait with `pair wait <name>`, or export with `--peer` from
the other machine once it has collected.

## Keeping pairs somewhere else

Every pairing call takes the store it reads and writes as `store=`: `start`,
`accept`, `collect`, `resolve`, `heal`, `forget` and `status`, and the
listener's helpers `active`, `pending_rooms` and `sweep_expired`. The CLI and
the local MCP bridge pass `PairStore`, the directory above. A program that
keeps secrets its own way passes anything implementing `pairing.Store` — a
server that pairs on its users' behalf, say, whose containers are replaced
on every deploy and whose users' secrets live in its own encrypted table,
passes one store per owner over that table:

```python
from switchboard import pairing

store = OwnerPairs(db, owner)                       # implements pairing.Store
started = pairing.start(base, store=store)          # hand started.code to the user, once
done = pairing.collect(started.name, base, store=store)        # from the reader loop
room = pairing.resolve(started.name, base, store=store).config  # the current work room
```

What the core relies on, and so what such a store has to do:

- **`save` and `save_pending` are atomic and durable before they return.** A
  pair record is the only copy of its pair secret, and the core saves before
  it sends anything: a save that raises leaves the code unspent, and one that
  returns and is later lost costs the pair.
- **A pending handshake holds a private key** — the initiator's ephemeral
  key, and the code's secret. Encrypt it at rest, and delete it when
  `delete_pending` is called: the core calls it once the handshake is over —
  collected and announced, aborted, or past its deadline.
- **`lock()` is exclusive per owner**, across every process and server that
  shares the table; an advisory lock is enough. The core never nests it and
  never makes a hub call while holding it — only the store reads and writes
  of one read-modify-write happen inside — so a slow hub never holds an
  owner's lock.
- **`get`, `pending` and `verdict` return `None` when nothing is saved under
  the name**, and `verdict` also once past the `until` it was saved with. A
  verdict holds no secret: a state, a sentence, and until when to say it.
- **`require()` raises `PairingError(state="disabled")`** when the store
  cannot keep pairs at all, and `enabled` is false then.
- **A storage failure raises `OSError` or `PairingError`.** `active`,
  `pending_rooms` and `sweep_expired` skip such an item and go on; `status`
  reports a pair it could not read as that pair's row; every other call
  stops.

Records go in and out as `PairRecord` and `PendingHandshake`, whose
`to_json()` and `from_json()` are the form to keep: store the JSON whole
rather than picking fields out of it, since a later release may add one.
`SWITCHBOARD_PAIR_<NAME>` is a feature of `PairStore`, not of the protocol.
The calls are synchronous, with a synchronous `connect`; from an event loop,
run each in a worker thread. The full contract is the docstring of
`pairing.Store`, and the tests hold the core to it with a store that has
those members and nothing else (`tests/test_pairing_store.py`). Shared rooms
are kept beside the pairs, through a protocol of their own, `sharing.Store`
([sharing.md](sharing.md#the-store)).

## What it costs

Stated as plainly as the rest of the docs state theirs:

- **The code is a live credential until the answer is collected.** Whoever
  holds it before then can answer first, or — by posting a second answer, a
  marker or a flood into the handshake room — void the pairing on both
  sides. Fifteen minutes by default for the answering; a shorter `--ttl`
  shrinks that, and a parked listener on the initiator shrinks the time to
  collection to seconds. Nothing removes the window.
- **The responder waits on a turn-based initiator.** It is in work room 0 at
  once, but nothing it says there is read until the initiator collects, and
  a message is gone after an hour: a pairing collected the next morning has
  lost whatever was said the night before. Its resolution says so, every
  time. The board, which keeps a day, is the place for anything that has to
  wait. Parking a listener on the initiator is what makes "collected" mean
  seconds.
- **One private key on disk**, on the initiator, between printing the code
  and collecting the answer. A code nobody answered in time is settled, and
  its key deleted, by the first collection after its expiry plus four
  minutes; any pending handshake is deleted once its deadline passes (the
  code's expiry plus 12 hours) by the first thing on this machine to pass by
  — a parked listener, `pair start`, `pair status`, `pair wait`, the MCP
  bridge. Not by a timer: a machine that runs none of those keeps the file
  until it does.
- **The pair secret on two disks**, owner-only and in the clear, for as long
  as the pair lives — or in an environment's secret store, after `pair
  export`.
- **A round trip per use.** Every `--room pair:<name>` command reads the pair
  room before it does anything else, which is how it follows a heal; the MCP
  bridge does it at most every 30 seconds.
- **A long absence moves the work room.** A side that comes back after six
  days without reading the pair room, to find the other side's record gone,
  moves both sides to a fresh room (above) — no key leaked, but it cannot
  tell. Whatever was still alive in the old room stays there.
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
  room two board entries under blinded keys, with their seven-day TTLs, and
  one more per shared room each side offers or has left
  ([sharing.md](sharing.md#what-the-hub-sees)); in a work room, whatever the
  agents do there. Every pairing message goes out as
  an ordinary `note`, with which step it is — hello, answer, marker,
  *paired*, *moved*, *ended* — sealed inside the body. A message's type
  travels in the clear, so a `pair.answer` type would have told the hub which
  rooms are handshakes and when each pairing completed; a `note` says nothing
  a chat message would not.
- **Timing, order and addresses.** The rooms are unlinkable by name, not by
  traffic, and the hub can work out from the traffic much of what it cannot
  read. The first post in a handshake room is the initiator's hello, and the
  post after it from a different address is the answer. The initiator's
  machine touches the pair room and work room 0 within seconds of posting the
  marker. A heal or a forget is one post in a work room followed by quiet,
  with another room starting up for a heal. A room that holds exactly two
  week-long board entries and no messages is a recognisable shape.

Handshake-room and pair-room writes, and the *moved* and *ended* notes, go out
under a throwaway agent id, fresh per call, and register no presence: the
handshake room is the one room whose coordinates are expected to end up in a
paper trail, and a roster row there would hand a code holder the machine's
name, branch and host. The hub never sees the code, `S`, `P`, a room key, the
SAS, or a pair's name or note. It cannot read which side is which, or which
generation a work room is — though the order and timing above can tell it
which machine started the pairing, and which of a pair's work rooms came
first.

## What it protects against, and what it does not

**A paper trail read later.** The code and the hub's entire history together
— every message the handshake room ever held, kept past its TTL by a hub that
chose to — yield two public keys, two MACs, a transcript hash, the nonce the
SAS was computed from, and the notes each side attached. None of it opens
anything without `P`, and `P` needs a private key that no longer exists: the
responder never writes its own down, and the initiator deletes its own once
the answer is collected. This is the property the whole design is for, and
`tests/test_pairing.py` asserts it against what a code holder can actually
read.

**The hub.** It can withhold, delay, replay and refuse. It cannot read
anything, and it cannot forge anything a holder of the pair secret would
accept: an answer has to carry a MAC under a key salted with `S`, which the
hub never sees, so it cannot put its own key in the middle. It *can* serve a
stale record from the pair room, which is why a heal is acknowledged rather
than assumed, and a record served under the other side's slot is refused as
tampering rather than read as that side speaking.

**A code that leaked before it was redeemed.** Detected, not prevented. The
code is a **live bearer credential** until the initiator has collected an
answer, or until it expires with none, and anyone holding it in that window
can answer in place of the intended peer — they hold the handshake room's
write key, so nothing the room does can exclude them. What the room can do is
keep a record nobody can take back: the answer channel is append-only, and
both sides read it the same way, so

- **two different answers abort the pairing on both sides** — "the code
  leaked", with nothing paired. So does anything else a code holder can put
  there to confuse the verdict: a *consumed* or *aborted* marker the
  initiator did not write (it writes them only after deciding, so finding
  one while undecided is proof of a forger), an answer that is not a usable
  key, or enough messages to push the first answer out of the 500 the hub
  returns per read. The initiator posts *aborted* and deletes its pending
  handshake; the responder, reading the same room, deletes its pair and marks
  its record ended in the pair room, so that if the initiator had collected
  it a moment before the trouble started, its next use of the pair says the
  other side ended (`ended_by_peer`) instead of leaving it alone in a quiet
  room;
- **a marker can end a pairing, never confirm one.** The responder takes a
  *consumed* marker as confirmation only when it carries the initiator's MAC
  for this transcript, which needs `P`; a forged one confirms nothing, and
  one naming another transcript voids this side as `superseded`. The
  initiator, finding any marker it did not write, aborts — so the two sides
  still agree;
- **a redeemer after collection is refused** — "already answered and
  collected" — and told to treat the code as leaked, and the pair the
  initiator collected is untouched by it: a rival answer that lands after
  the collection does not void the responder whose answer was collected;
- **an answer the hub stamped after the expiry is ignored by both sides**, and
  the initiator is warned that somebody used the code late.

So a code holder can always make a pairing fail, loudly and on both sides.
Answering first instead, while the intended peer arrives second, gets the
peer refused as `already_redeemed`, which tells the person who carried the
code that it leaked.

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
  transcript is a pair to forget and make again. A leaked pair secret, or a
  code somebody else answered, opens every room currently offered over that
  pair as well; the remedy is a fresh pairing, then a new room whose list
  leaves the old pair out ([sharing.md](sharing.md#what-a-leak-opens)), and a
  heal does not help.
- **Other processes running as the same OS user.** They can read the pair
  store, as they can read `.claude/settings.local.json` and a signer's
  memory. The OS user was always the boundary.
- **A hub that will not serve you.** It can make pairing fail; without the
  code it cannot make it succeed wrongly.

It also does not tell you *who* is on the other side — only that it is the
machine that redeemed this code, and, if you compare the SAS, that it is the
machine the human carried it to.

## When it goes wrong

Every refusal is a sentence that says what to do, and carries a
machine-readable `state` (the CLI's `--json`, the MCP results):

A pairing that ended badly — `aborted`, `superseded`, `expired`, `unsent`,
`already_redeemed` after a race — leaves a short verdict behind on this
machine (`~/.switchboard/pairs/voided/`, no key in it) for three days. So the
verdict is said again by `pair status`, by `--room pair:<name>` and by the MCP
bridge even if the process that found out died before it could say so;
`pair forget <name>` clears it, and a new pairing under the name replaces it.

| `state` | Means | Do |
|---|---|---|
| `already_redeemed` | this code was answered by a machine other than this one, or the handshake room holds what no honest handshake writes (a flood, a message the code cannot open), and this machine is not paired. A leak — including a rival answering at the same moment, which voids this side even though its answer was sent (the pair room is told) — except when a cloud session redeems the code again within its life after its container was replaced. CLI exit 2 (1 from `pair wait`) | the machine that made the code runs `pair forget` on what it paired, and you pair again with a fresh code, shared more narrowly |
| `aborted` | the handshake was interfered with — two answers, a marker the initiator did not write, an answer that is not a key or does not verify, a flooded room — so the code leaked and nothing was paired. A responder's record is gone and marked ended | pair again with a fresh code |
| `superseded` | the handshake room says the initiator collected an answer that was not this machine's, naming an answer that is really there; this side's record is gone. (A marker naming no answer in the room is a forgery, reported `aborted`, which is what the initiator concludes too) | check `pair status` on the other side — it may be paired with whoever that was — have it `pair forget` that, and pair again |
| `expired` | the code expired — unanswered, or the hub received the answer too late. Also what a replaced cloud container usually sees redeeming the code again from its transcript | ask for a fresh code; do not reuse the old one, which may already be in a paper trail |
| `unsent` | this side saved the pair but its answer has not reached the hub. From `pair accept`: re-run it, or use the pair, before the code expires. From any later use: the handshake room showed no answer from this side past the code's life, so the pair is gone | retry while the code is live; after that, pair again |
| `wrong_hub` | the code says a hello was left, and this machine cannot see it: an expired code, or a different hub at that address — a container's own `127.0.0.1` | nothing was sent; check `SWITCHBOARD_URL`, or ask for a fresh code |
| `unconfirmed` | the initiator has not collected this side's answer yet. As a refusal, from `pair export` without `--peer`, which will not freeze the pair unconfirmed | wait (`pair wait <name>`), or export with `--peer` from the other machine once it has collected |
| `never_confirmed` | the initiator could have collected until its deadline and did not. For a pair loaded from `SWITCHBOARD_PAIR_<NAME>`, the variable predates the confirmation and the proof has since expired, so this side cannot tell | `pair forget` here and pair again — for a variable, first check `pair status` on the other machine, and re-export with `--peer` there if it shows paired |
| `pending` | the initiator's side has no answer yet | wait, or park a listener |
| `ended_by_peer` | the other side ran `pair forget`, or voided its side because the handshake went wrong after this side collected it | `pair forget` here, and pair again if it is still needed |
| `ended` | this side's own record in the pair room says the pair was ended — a `pair forget` here that was interrupted, another copy of this side, or a forget racing this command; the record is gone from this machine now too | pair again if it is still needed |
| `name_taken` | this machine already has a pair, or a pending pairing, by that name | `--name` something else, or `pair forget` the old one |
| `unreachable` | a heal could not read the pair room | try again when the hub is reachable |
| `conflict` | a heal lost a race with another process on this machine writing the same record; nothing was healed | run `pair heal` again |
| `tampered` | the pair room holds a record that is not what either side wrote | nothing was changed; suspect the hub |
| `upgrade` | the other side moved the pair on with a healing strategy this release does not know | upgrade agent-switchboard here |
| `disabled` | `SWITCHBOARD_PAIRS` is the empty string on this machine | unset it, or point it at a directory |
| `store` | the pair store could not be read or written (a full disk, a read-only directory) — from the MCP bridge, the CLI's `pair` commands and `--room pair:<name>`. It holds the only copy of each pair secret, so nothing was overwritten | make the directory in `SWITCHBOARD_PAIRS` (default `~/.switchboard/pairs`) writable by this user, with space left |
| `withheld` | a hosted bridge was asked to pair (any `pair_*` tool), or to reach a `pair:` room | pair from a bridge on your own machine, or with `switchboard pair` |
| `unknown` | no pair by that name on this machine | `pair status` lists them |
| `error` | a `pair status` row that failed for a reason with no state of its own — an unreadable file, say | its `error` says what; the other rows are unaffected |

`pair status` reports a pair that fails as a row with its `error`, never as an
exception, so one dead pair — or one file this release cannot read — does not
hide the others; it exits non-zero only when it was asked about one pair by
name and that pair is the failure.

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
stamped after expiry is ignored by both. A review of the first build found
that "what both sides read" has to mean all of it, and only what is
authentic: a code holder can also write markers, and can post enough to push
the first answer out of the newest-first window one read returns. So a
marker the initiator did not write, or a read that comes back full, aborts
both sides as surely as a second answer, and the responder believes a
*consumed* marker only when it carries the MAC that only `P` produces.
Detection, not exclusion — stated as that, with the SAS for the case
detection cannot reach.

**D4. Work rooms are derived, and each side only ever writes its own
record.** The sketch's pair room carried the work room's key, sealed, the way
its handshake room carried the pair room's. So a heal would mint a new key
and send it through the hub — the paper-trail problem again, one level down
— and update one shared "current room" record that two sides could race to
write. Instead work room *n* is `HKDF(P, n)`: nothing is
minted or sent, both sides are in work room 0 the moment they hold `P` (the
responder optimistically, unconfirmed until the initiator collects), and a
heal is one integer. Each side keeps one sealed record in the pair room
(`coord/pair/side/<role>`: its generation, its strategy, when it was written,
whether it ended the pair, and — the initiator's from the start, the
responder's once it has confirmed — the initiator's confirmation MAC and SAS
reveal). The current generation is the higher of the two, so between the two
machines there is no compare-and-set and nothing to race, and the other
side's record catching up is the acknowledgement. Processes on one machine
do share a record, so there the write is conditional on the revision read.

The pair room has one other tenant, on purpose: shared rooms
([sharing.md](sharing.md)) travel through it, one record per room per side
under `coord/share/<role>/`, by the same rule — each side writes only its own.
The pair room already reaches exactly the two machines that hold `P`, sealed,
write-protected and kept fresh by every use of the pair, so a room passed
between them needs no channel of its own. What that costs is the exception to
"nothing is minted or sent": a shared room is minted, its keys are in those
records, and so `P` opens every room currently offered over the pair. A heal
moves the work room, not the pair room, and does not change that.

Beyond the four: every room is write-protected rather than only sealed, so a
room's identifier — which the hub holds — admits no writes; the SAS, with its
commitment in the code, gives the initiator a check that does not run through
the hub; and a side that comes back after a long absence to find the other
side's record gone jumps to a random generation past everything it knows,
rather than guessing which room is safe — once per absence, decided by this
side's own gap in reading, so that unlike the first build's jump it does not
repeat on every read and strand each thing said in a different room, and
never fires on a side that kept working while its peer was away.
