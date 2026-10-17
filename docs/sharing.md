# Shared rooms

A pair is two machines. Work that needs three — a laptop, a cloud session and
the desktop under the desk — needs one room all three read, and neither shape
that already exists gives it one. An invite would, but an invite *is* the
room, so the chat or ticket that carried it holds the room for as long as the
room lives, which is the paper trail pairing exists to avoid. And three pairs
are three rooms: what is said in one reaches one other machine.

A shared room is the third shape. It is an ordinary write-protected room that
one machine mints and offers to machines it is already paired with, sealed
inside the pair rooms that connect them. No human carries it, and no
conversation, ticket or transcript ever holds its key.

```bash
switchboard share new api --with laptop-cloud laptop-desk   # on the laptop: mints shared:api, offers it to two pairs
switchboard share accept api                                # on each machine offered it
switchboard --room shared:api say build "taking 0142-0145"  # any of them, from then on
```

MCP has the same three steps on the local bridge: `share_new`,
`share_accept`, then `room="shared:<name>"` on any tool. The rest of this page
is the model those commands keep to, what each one says, and what none of
them can do.

## The model

It is deliberately small, and fixed:

- **A room is a room.** Hub URL, workspace, key, write key, and nothing else.
  The hub knows nothing about sharing: a shared room has no members, nothing
  on the hub says who is in it, and access is knowing it.
- **Each machine keeps, per room, its own list** of the pairs it shares that
  room with. The laptop's list for `api` is `laptop-cloud` and `laptop-desk`;
  the cloud session's is `laptop-cloud` alone, the pair it was offered the
  room over. Lists are never merged, synchronised or passed on, and the cloud
  session and the desktop need not be paired with each other at all.
- **One action: `new`.** It mints a fresh room and offers it to exactly the
  list given. There is no add and no remove: to change who is in, make a new
  room with a new list. `--from` starts from an old room's list as it stands,
  and the new list belongs to the new room — the old room's list and records
  are never touched by it.
- **Rooms run in parallel, and nothing is revoked.** A new room takes nobody
  out of the old one; whoever holds the old room holds it for as long as it
  lives.
- **Leave** drops the room here and asks the machines on its list to take
  this machine off their lists for it, so that a later `share new --from`
  there does not include it. It takes the room from nobody but this machine.
- **Nothing moves on its own.** Each agent reads what its peers did, as notes,
  and decides for itself whether to accept a room, keep one, or leave one.
  No room is joined, no list changed and no offer passed on without a command
  on that machine: a machine offers a room only to the list its own `share
  new` named, plus the machine it accepted the room from.

## A shared room, a pairing code, or an invite

| | An invite (`swb1_…`) | A pairing code (`swbp1_…`) | A shared room (`shared:<name>`) |
|---|---|---|---|
| What it is | a room, as a string a human carries | the start of a key exchange between two machines | a room one machine passes to the machines it is paired with |
| Who carries it | a human | a human, once, for minutes — 15 by default | nobody: it travels sealed inside pair rooms |
| Right for | throwaway rooms, viewers, hosted models | introducing two machines | rooms several paired machines work in |
| What the other side needs | anything that reads invites | switchboard on a machine of its own | a confirmed pair with the machine sharing it, and switchboard on its own machine — never a hosted bridge |
| When a key leaks | mint a new room and hand it out again | `pair heal` for a work room's key; a fresh pairing for the pair secret | a new room without whoever holds it (`share new`), and everyone moving to it; nothing is revoked |

`shared:<name>` is a handle, the way `pair:<name>` is: it names the room,
never a key, and it is looked up in this machine's share store on every use,
so a room left in one terminal is gone from the next command in another. A
shared room can still be handed to somebody as an ordinary invite —
`switchboard --room shared:<name> invite` — and that is the one way its keys
leave the share store, deliberately; nothing takes them back once out, and the
command says so. `whoami --show-key` and `--env` refuse under `--room
shared:<name>`, as they do for a pair's work room. No other result, on any
surface, carries a shared room's key, write key or token.

## Three machines on the CLI

The laptop is paired with a cloud session (`laptop-cloud`) and with the
desktop (`laptop-desk`), both confirmed: `pair status` shows them `paired`.
On the laptop:

```console
$ switchboard share new api --with laptop-cloud laptop-desk --note "the api migration, 0142-0145"
shared:api offered to laptop-cloud, laptop-desk
```

The room is minted on this environment's hub, write-protected (`ws_…`), and
saved here before any offer is written. Stdout names exactly the pairs that
were offered it, because that is the claim the command makes. A pair whose
room could not be reached is not on that line: stderr names it, with why
(`share: could not reach laptop-desk: …`), and the offer is written by the
next touch that reaches it (below), with nothing to run again.

`--with` takes one pair or several and can be repeated. Each must be a
confirmed pair. Nothing is shared over a pair whose handshake has not been
collected, and a pair in any state but `paired` refuses the whole command with
that state, with nothing shared. The note travels in every record of the room,
so it is at most 200 characters and should say nothing you would not tell
every machine on the list.

**An offer is the room.** It carries the keys, so every machine offered a room
holds it from the moment its pair room is read, whether or not its agent ever
accepts. Accepting decides whether that machine keeps the room and works in
it, not whether it could. Offer a room only to machines that may hold it.

On the cloud session the offer arrives as a note: on a parked listener's wake,
as a `share_notes` row on the MCP bridge, or from `share status`, which reads
every pair first:

```console
$ switchboard share status
OFFERED TO THIS MACHINE
  laptop-cloud/api: the api migration, 0142-0145  (share accept laptop-cloud/api keeps it)
NOTES, LAST 7 DAYS
  2026-10-17T09:14:02+00:00 laptop-cloud offers room api: the api migration, 0142-0145 (`share accept laptop-cloud/api` keeps it)
$ switchboard share accept api
shared:api
```

Stdout is the handle and nothing else. Offers are kept here without their
keys, so `accept` reads the live offer again from the pair room, and refuses
with `withdrawn` if it is gone. An offer goes by the sharer's name for the
room. When two pairs offer rooms with the same name, `accept` refuses to
guess, listing both, and `share accept laptop-cloud/api` names one. Names are
local: `--name` keeps the room under another one here, which is needed when
the sharer's is taken. Stderr says what accepting does not do: claims, board
entries and messages in other rooms do not carry into this one.

Accepting puts the sharer on this room's list here, so this machine's record
to it now carries the room back. That is how the laptop learns of it:

```console
$ switchboard share status
ROOM                           ORIGIN                 LIST
shared:api                     created here           laptop-cloud (has it), laptop-desk (offered)
  note the api migration, 0142-0145
NOTES, LAST 7 DAYS
  2026-10-17T09:15:40+00:00 laptop-cloud accepted api
```

Each entry on a list says how far the room has got with that pair, read from
the records in the pair room rather than from what this side hopes: *has it*
— the pair's own record carries the room back; *offered* — this side's record
to it is in the pair room; *not offered yet* — it is not, and the next touch
writes it. `share status <name>` shows one room, and exits 1 when that name is
neither a room held here nor an offer waiting. Under `--json` it is one
document — `rooms`, `offers`, `left`, `notes`, and `unreached` for the pairs
this read could not reach, with a `state` of `unreachable`, `gone` or
`unconfirmed` — and no key, write key or token anywhere in it.

From then on it is a room like any other, on every machine that kept it:

```console
$ switchboard --room shared:api say build "taking 0142-0145"
$ switchboard --room shared:api claim backend/alembic
$ switchboard --room shared:api inbox
```

Any channel will do: unlike a pair's work room, whose channel is `pair`, a
shared room has no fixed one. A listener parked there wakes the way it does
in any room — on direct messages, and on the channels this agent subscribed
to *in that room* — so subscribe there to the channels the machines on its
list talk on (`--room shared:api announce -c build`; MCP:
`subscribe(room="shared:api", channels=["build"])`), or send each other DMs.
`--room shared:<name>` resolves with a lookup in the share store rather than
a hub call, except once a day: when the room's records were last refreshed
more than a day ago it touches the room's pairs first, so a machine that only
ever works in the room still keeps its records alive and reads its peers'. A
refresh that could not reach every pair is tried again an hour later, not on
every command, since each try waits out the unreachable pair's timeout.
Anything that touch says is on stderr as `shared:` lines.

## Touches

Nothing about a shared room is pushed. Each machine reads its pairs' rooms and
writes what its own store says should be there, on what this page calls a
*touch*:

- every `share` command: `new`, `accept` and `leave` on the pairs they
  concern, and `status` on every pair this machine has, or with a name on
  that room's;
- a parked `switchboard listen`, on every pair as it arms, and on the woken
  pair at each share wake;
- the MCP bridge, at most once a minute, on whatever tool call comes next;
- `--room shared:<name>` and `room="shared:<name>"`, on that room's pairs, once
  a day.

A touch of a pair resolves it first, as any use of a pair does, so a pair that
is over is noticed there, and a heal or a lapse move that resolution follows
is said as a note (`pair_moved`). It does not count as using the pair: a
listener parks in a pair because it carries shared rooms (*Listening*), not
because the bridge's sweep read it a minute ago. Then it reads the share records in the pair room,
applies whatever is newer than what it applied before, and writes what is
owed: for each held room with this pair on its list, the offer; for each room
left, a `left`, until the pair acknowledges it. A write happens only where the
record read back is missing, different, or a day old, so that one rule is also
the retry for a write that failed, a reply that was lost, and a `share new`
that reached only some of its pairs. After a write it posts a wake in the
pair's work room (*Listening*, below). A pair whose room cannot be read costs
that pair alone, and says so once per outage — a room that reads but refuses
every write included, and so do records this release cannot apply at all.

A record is applied only if it is newer, by a counter each writer keeps, than
the last one applied for that pair and room — so neither a hub replaying an
older record nor two processes here applying their reads out of order can put
back a pair that left. And this side's own record in the pair room, which the
other side could also write, is never taken as content: the one thing acted
on in it is a `left`, which means another copy of this machine left the room
(`left_elsewhere`, below).

## Working without someone

Nothing is revoked, so working without a machine means working in a room it
does not hold:

- **A room leaves a machine only when that machine leaves it**, and leaving
  takes it from nobody else. A machine that will not leave cannot be put out:
  the room is its keys, and it has them.
- **A new room is the way.** Offer it to everyone who should stay, and not to
  whoever should not, and have everyone move.
- **Claims, board entries and unread messages do not carry across rooms.** A
  lease in the old room excludes nobody in the new one.

On the laptop, to carry on with the cloud session and without the desktop:

```console
$ switchboard share new api-2 --with laptop-cloud --from api
shared:api-2 offered to laptop-cloud
share: api runs on in parallel: claims, board entries and unread messages there do not carry over to api-2, and nothing in api was revoked.
share: 1 lease(s) this agent holds in api stay there and exclude nobody in api-2: claim again with shared:api-2 what you still need (`--room shared:api claims` lists them).
```

The list is exactly `--with` when it is given. Without it, `share new
api-2 --from api` takes `api`'s list as it stands after reading those pairs,
so a machine that has already left `api` is not offered `api-2`, and stderr
names whoever was on the old list and is not on the new one. If a pair on
that list cannot be read, nothing is shared (`unreachable`): its `left` may
be waiting unread in its pair room, and there is no add to bring it back
later or remove to take it off. Try again, or name the list with `--with`. Neither form
changes `api`, its list or its records. What `--from` adds is the cost, said
rather than discovered — the things a heal strands, for the reason [the
model](model.md) gives: leases are compare-and-set per room, so the leases
this agent holds in `api` are counted here (listed, on MCP, as
`left_behind`) to be claimed again in `api-2`. Counted, not named: the room is
encrypted, so the hub knows each lease by a blinded token, and the name
never left the agent that claimed it — `--room shared:api claims` lists them
with their notes. Board entries stay where they were written
until they expire, and so do unread messages.

Then each machine that moves accepts the new room and leaves the old one:

```console
$ switchboard share accept api-2                       # on the cloud session
$ switchboard share leave api --note "moved to api-2"  # on every machine that moved, the laptop too
```

Until they all have, the machines are in two rooms and do not exclude each
other, and whatever is said in `api` is read by everyone who still holds it —
including the machine the new room is meant to work without. That is the
price of a room nobody can revoke. The desktop keeps `api`, and is told that
the others left it.

## Leaving

```console
$ switchboard share leave api --note "moved to api-2"
left shared:api; told laptop-cloud, laptop-desk
share: nothing was revoked: everyone who holds api still does, and this machine keeps no copy of it.
```

A leave is done here before anything is sent: the room's keys are deleted from
the share store, and a *tombstone* takes their place — the room's name, the
note, and the pairs to tell, with no key. The pairs told are those on the
room's list here, plus any other pair that offered this machine the same room.
Each is written a `left`, which on the other side takes this machine off that
room's list and withdraws any offer of it, and which is final for that room
over that pair. Stdout names the pairs the leave
reached; stderr names any it could not, and every later touch writes the
`left` again until each pair acknowledges it. `share leave api` again retries
now. It exits 0 either way.

Passed an offer instead — `share leave laptop-cloud/api` — it refuses the
offer the same way, to the pair that made it. A peer can offer a room under
any name, including one held here, so a bare name that is both a room held
here and an offer of another room is refused (`ambiguous`), naming both
forms: `share leave shared:api` is the room alone, and `share leave
laptop-cloud/api` the offer alone.

A pair has acknowledged a leave once this side's `left` is in the pair room,
the pair no longer offers the room back, and the pair has used its pair room
since the `left` landed there. A pair that has read the `left` stops
refreshing its old offer rather than deleting it, so that can take a week;
`share status` lists the room under `LEFT, NOT YET ACKNOWLEDGED` until then,
which means "not yet known to be read", not "not read". A tombstone is kept a
week past its last acknowledgement, so a room just left does not come back as
a new offer from a pair it told.

The last condition is for **a pair away for longer than a record lives**. An
offer that expired because its machine has been away for over a week looks
exactly like one its machine stopped refreshing, so the offer being gone is
not enough: the pair's own side record — which every use of the pair there
rewrites, a share touch included — must be newer than the `left`. A machine
that stays away keeps being written the `left` every day, for as long as the
pair lasts, and reads it when it comes back.

A copy of this machine started fresh — a container from `pair export` with
no share store — holds no tombstone, but its own slot in the pair room still
says `left`: it does not take the sharer's offer of that room as new
(`left_elsewhere`).

## Notes

Every change a peer makes is a note, and so is everything this machine could
not do. Notes are kept seven days, and `share status` lists all of them every
time, whatever an earlier command or another session already said: the CLI,
the MCP bridge, a listener and other sessions all share one store, and a
"seen" flag would let the first of them to look hide a note from the rest.
Each surface also says the notes it produced as it produced them — `share:`
lines on a command's stderr, `shared:` lines from `--room shared:<name>`,
`shared` on a listener's wake, `share_notes` on the MCP bridge's next result —
and every note has an `id` that only grows.

Text a peer supplied is checked on arrival: a room name that is not a usable
name is shown by its record's rid and never used, as a path or anything else,
and a note is cut to 200 characters on one line, with control characters and
anything a UTF-8 terminal cannot print taken out. Nothing else a peer writes
reaches a note: a record from another share protocol is said to be one, not
quoted.

| `kind` | Says | Means | Do |
|---|---|---|---|
| `offered` | "`<pair>` offers room `<name>`: `<note>`" | a room not held here is waiting, kept without its keys | `share accept <pair>/<name>` if the work needs it, `share leave <pair>/<name>` to refuse it, or nothing: it waits for as long as the sharer keeps offering it |
| `accepted` | "`<pair>` accepted `<name>`" | the pair's own record now carries back a room this side offered it. Said once | nothing: its entry on the list reads *has it* |
| `offered_held` | "`<pair>` offers `<name>`, which you already hold as `<local>`" | the same room — hub, workspace, key and write key all match — offered by a pair that is not on its list here | `share accept <pair>/<name>` puts the pair on this room's list: this side's record carries the room back to it, and `share new --from` includes it. Otherwise nothing changes |
| `different_keys` | "`<pair>` offers `<name>` with different keys; ignored" | an offer naming the hub and workspace of a room held or offered here, with another key or write key. An offer is the same room only if all four match, and nothing here follows a key a peer substitutes | nothing was changed. A machine keeping to this protocol never sends one, and only the two sides of the pair — or whoever holds its secret — can write its room: ask the other side what it is running |
| `unusable` | "`<pair>` offers `<name>`, but …; ignored" | an offer without a key or a write key, whose write key does not open its own workspace, or whose key, hub URL or token no client could open the room with — accepted, such a room would fail wherever it was opened, the listener included. Nothing missing is ever filled in from this environment | nothing was changed; the sentence says what was wrong |
| `left` | "`<pair>` left `<name>`: `<note>`" | the pair dropped the room and asked to come off this machine's list for it: it is off, and an offer of it waiting here is withdrawn | nothing; a `share new --from` here leaves it out. It is not revoked from it — it said it dropped its copy |
| `withdrawn` | "`<pair>`'s offer of `<name>` lapsed or was withdrawn" | an offer waiting here is gone from the pair room: the sharer's record lapsed after a week without a refresh — it has been away that long, or lost its share store — or no longer carries the room | nothing to accept any more |
| `record_gone` | "`<pair>`'s record for `<name>` is gone — not refreshed for 7 days, or it lost its copy; still on your list" | a pair that had the room has written nothing about it for a week: away that long, or a container that lost its share store. The list is unchanged | if it lost its copy, this side's next touch offers it the room again and its agent can accept it; if it is gone for good, a new room without it |
| `left_elsewhere` | "another copy of this machine left `<name>` (its record to `<pair>` says so) …" | two copies of this side of a pair are running — a laptop and a container started from its `pair export`, say — and the other one left the room, or refused its offer. A `left` is final: a copy holding the room keeps it but stops offering it to that pair, and a copy that does not hold it does not take that pair's offer of it as new | `share leave <name>` here too if the leave was meant for both copies; or keep working in it, since nothing was revoked. To be in the room after all, a new room is the way |
| `pair_moved` | "pair `<pair>`: moved from work room …", or "pair `<pair>`: this side had not read the pair room for …" | the touch's resolution of the pair followed a move — the other side healed it, or one side came back to a pair room that had forgotten it — and is the only resolution that will see it, so the move is said here | what a heal or a lapse move always asks: claims, board entries and unread messages in the old work room stay there; claim again in `pair:<pair>` what you still need |
| `pair_gone` | "pair `<pair>` … : it is off the list of …; its offer of … was dropped … Nothing more is written to it." | the pair was forgotten, ended by either side, never confirmed, voided, or paired again under the same name — which is another machine. It leaves every list and loses its offers, and a `left` it was still owed is never sent | to share with that machine again, pair again and offer it a new room (`share new --with`) |
| `cannot_read` | "cannot reach pair `<pair>`'s room (…); its share records are read and written again on the next touch" | that pair room could not be read or written, or its records could not be applied at all (named by the error's class alone, since its text can quote them). Said once per outage, which ends only when a touch has read the room and written everything owed | nothing: the next touch that reaches it catches up; for records that cannot be applied, suspect whoever can write that pair room. A listener says it without waking for it |
| `unreadable` | "N record(s) in pair `<pair>`'s room could not be read and were ignored (…)" | records this release cannot use: one it cannot open, a newer share protocol, a name that is not a name, an offer whose room is not the one its key names. Said again only when the set changes | upgrade if it says to; otherwise suspect whoever can write that pair room |
| `store` | (MCP bridge only) the share store could not be read | the bridge could not look for anything this time. Said once, until it clears | make the directory in `SWITCHBOARD_PAIRS` readable |

## Over MCP

On the bridge on the agent's own machine:

```
share_new     name="api" pairs=["laptop-cloud", "laptop-desk"] note="the api migration, 0142-0145"
              → {name: "api", room: "shared:api", workspace: "ws_…",
                 offered: ["laptop-cloud", "laptop-desk"], unreached: [], dropped: [],
                 from: null, left_behind: [], warnings: [], next: "…"}
share_accept  offer="laptop-cloud/api"
              → {name: "api", room: "shared:api", pair: "laptop-cloud", joined: false,
                 offered_back: true, warnings: ["claims, board entries and messages …"],
                 next: "…"}
say           room="shared:api" channel="build" message="taking 0142-0145"
              → {…, shared: {name: "api", workspace: "ws_…", pairs: ["laptop-cloud"],
                             warnings: []}}
share_leave   name="api" note="moved to api-2"
              → {name: "api", kind: "room", told: ["laptop-cloud"], untold: [],
                 warnings: ["nothing was revoked: …"]}
share_status  → {rooms: [{name, room, workspace, url, origin, from, note, token_carried,
                          created_at, last_used_at, refreshed_at,
                          list: [{pair, has, offered}]}],
                 offers: [{offer, pair, name, note, at, held}],
                 left: [{name, waiting, at, note}], notes: […], unreached: […], now}
```

`unreached` on `share_new` and `untold` on `share_leave` are pairs whose room
could not be reached, with why; the bridge writes what they are owed by itself
on later calls. Every result reached through `room="shared:<name>"` carries
`shared`, and the name is looked up in the share store on every call, so a
room left or replaced in a terminal is followed at once — a name that now
reaches a different room than the bridge last used it for is said, in that
result's warnings.

Any result can carry `share_notes`: the notes this bridge has not yet
returned, from whichever process on this machine produced them. The bridge
remembers, in memory, the highest note id it has returned, and keeps no "seen"
flag in the store. A `share_status` returns every note of the last week, and
later results do not repeat those. On its own the bridge reads every pair's
share records at most once a minute, and nothing it meets there can fail the
unrelated tool it rides on.

`share_new`, `share_accept` and `share_leave` are marked destructive, so a
client that asks before such a call asks before these: none has an undo,
because nothing in sharing is taken back. The hosted bridge withholds all four
`share_*` tools and refuses `shared:` handles (`withheld`), as it does pairing:
the rooms travel over pairs it does not keep, and their keys would sit on its
operator's disk.

## Listening

`switchboard listen` parks in every shared room used on this machine in the
last day — a `share` command or a `--room shared:` use counts as use, and
parking does not — alongside the rooms it always parks in. It names each
shared room it leaves out, with the `--in shared:<name>` that adds it, and
`--only shared:<name>` parks there alone. It also counts as active every pair
that is on a held room's list or has an offer waiting here, and parks in that
pair's work room even when nobody has used the pair itself for days, because
that is where a peer's share wake lands. A shared room this machine cannot
open — a key or URL this release refuses — costs that room, with a line
saying so, never the listener.

A share wake is a note on the work room's `pair` channel carrying the tag
`swbp1/shared` and nothing else, sealed with the work room's key, from an
agent id used once. It means "look", never what changed: the listener reads
that pair's share records and wakes only if the read turns up a note it has
not seen. So this machine's own writes, which post the same note, wake
nobody — and neither does one forged by whoever holds a leaked work-room key,
the key a heal exists for. The wake's JSON carries what the read found, as
`shared: {notes, next}`. A listener also does one share read as it arms, of
every pair, so an offer that arrived while nothing was parked is said at
once — over a pair it parks in or not. `--no-pairs`
leaves pairs out, and with them the share wakes; shared rooms used lately are
still parked in.

## Tokens

An offer carries no hub token unless it was made with `share new
--with-token`. A machine keeping a room without one uses its own
environment's token only when the room is on the hub its environment points
at, and none otherwise — never "whichever token it has", which would hand
this environment's token to a hub another machine chose. `--with-token` puts
this environment's token into the offer for machines that do not hold it,
and so into every record of the room; `share status --json` shows
`token_carried`.

## Cloud containers

Shared rooms are kept in the share store, beside the pair store in
`~/.switchboard/pairs/`, and a cloud container keeps neither. A container
started from `SWITCHBOARD_PAIR_<NAME>` comes up paired, with no shared rooms.
Nothing restores them on its own — the record that would trigger it is one
the other side can write — but every room the other side still holds with
this pair on its list is still offered over the pair, so the container's
first touch finds each one as an offer, and its agent can accept them again.
That includes rooms the lost copy created, once the other side had accepted
them: its record carries them back. A room the lost copy created and nobody
accepted went with it.

## The store

`~/.switchboard/pairs/shared_rooms.json`, owner-only and written as the pair
store is — a temporary file renamed over the old one — with a lock of its own,
`shared.lock`, never the pair store's: sharing calls pairing, and pairing
takes that one. It is not `shared.json`, because that is where a pair called
`shared` is kept. `SWITCHBOARD_PAIRS` moves it with the pair store, and the
empty string turns sharing off with pairing (`disabled`).

It holds, for each room held here, its keys, where it came from, its list and
when it was created, last used and last refreshed; the offers waiting, without
their keys; the tombstones; what was last applied from each pair for each
room; and the notes. For a room this machine created and nobody has accepted
it is the only copy of the room's keys, so a store that cannot be read is
refused with `store` and never overwritten.

A program that keeps pairs its own way (`pairing.Store`,
[pairing.md](pairing.md#keeping-pairs-somewhere-else)) keeps shared rooms
beside them through `sharing.Store`: `enabled`, `require()`, `lock()`,
`load()` and `save(doc)`. Every call in `switchboard.sharing` that reaches a
hub takes `store=`, `pair_store=`, `connect=` and `now=`. The core relies on
`save` being atomic and durable, and on `lock()` being exclusive per owner and
separate from the pair store's; it never holds that lock across a hub call or
a pairing call. The document holds room keys, so keep it encrypted at rest, as
the pair secrets are. The full contract is the docstring of `sharing.Store`.

## What the hub sees

The hub sees what it sees for any write-protected room, and for any pair:

- **Each shared room is an ordinary room**: its identifier, sealed and padded
  traffic, and the agents that announce in it, by id, and when.
- **One more board entry in a pair room per shared room per side** that
  offers or has left it, under a blinded key, sealed and padded, with the
  board's seven-day TTL. So it can count how many rooms ride on a pair, see an
  entry change size when an offer, which carries keys, becomes a `left`, which
  does not, and see when each is written: daily while the room is held, and in
  a burst on `share new`, `accept` and `leave`.
- **A wake**: one note in a pair's work room seconds after a write to its pair
  room, from a fresh agent id — which ties the two rooms together by timing,
  as a heal's *moved* note does.
- **Timing across rooms.** A `share new` is writes to several pair rooms in a
  row and a new room starting up; an accept is a write to one pair room and a
  new agent in that room. The rooms are unlinkable by name, not by traffic.

It cannot read a room's name or note, its rid, its keys or which side wrote a
record, and nothing names the pair rooms a shared room travels in — though
the timing and addresses above can tell it much of that.

## What a leak opens

**A pair secret.** The records travel sealed under the pair room's key, and
they carry minted rooms, not derived ones: the keys themselves. So **a leaked
pair secret, or a code somebody else answered, opens every room currently
offered over that pair.** That is in both directions — the rooms this machine
offers the pair, and those the pair offers it — and, to a hub that kept old
records past their TTL, every room ever offered over it. A heal does not help:
it moves the work room, not the pair room. Leaving does not either, since
nothing is revoked. The remedy is a fresh pairing, then a new room whose list
leaves the old pair out, and everyone moving to it:

1. `pair forget <name>` here, and on the other machine if it is yours. On
   this one the pair then leaves every list, with a note.
2. Pair again with a fresh code, and compare the SAS.
3. `share new <room> --with` the new pair and everyone else who should stay.
   `--from <old room>` would give that list without the forgotten pair, but
   cannot add the new one — a list is exactly what one command names — so name
   them all.
4. Every machine that stays accepts the new room and leaves the old one.
   Whoever held the leaked secret keeps every room it opened, for as long as
   anybody talks there.

**Compare the SAS before sharing over a pair.** A code that leaked before it
was redeemed can be answered by whoever holds it, and the initiator then holds
a pair with them that looks like any other; pairing's one defence there is the
short authentication string read off both screens
([pairing.md](pairing.md#what-it-protects-against-and-what-it-does-not)).
Without sharing, that mistake costs the pair's own rooms. With it, every room
offered over the pair goes to whoever answered. `share new` cannot check the
SAS for you; only the people reading the two screens can.

**A work-room key** opens nothing shared: the work room carries only wakes,
which say "look" and nothing else. A heal is still the remedy for it, and
changes nothing about shared rooms, whose records live in the pair room.

**A shared room's own key** — its invite pasted somewhere, say — opens that
room and nothing else. Make a new room without whoever has it, and move.

**The share store** holds every held room's keys in the clear, owner-only, as
the pair store holds pair secrets, and other processes running as the same OS
user can read it. A copy of it opens every room in it. The OS user was always
the boundary.

## What it costs

- **No revocation.** A room is as private as the least careful machine that
  ever held it, for as long as it lives — and every machine it was offered to
  held it, accepted or not.
- **No read-only offers.** Every machine offered a room can write it: an
  offer carries the write key.
- **A leave can take a week to be acknowledged**, and longer for a machine
  that is away: it is written every day until that machine comes back and
  reads it (*Leaving*, above).
- **A touch reads a pair room per pair.** `share status` and the MCP bridge's
  sweep read every pair; a command in the room itself reads none, except once
  a day.
- **One hub per room**: the one the machine that made it was pointed at. A
  room never moves; a new room on another hub is the move.
- **Two rooms at once while machines move.** Between a `share new --from` and
  the last accept, the machines are in two rooms, and nothing said in one
  reaches the other.

## When it goes wrong

Every refusal is a sentence that says what to do, and carries a
machine-readable `state` (the CLI's `--json`, the MCP results). A `share`
command exits 1 on any of them. A refused command has shared, accepted or
left nothing, though the read it did first may have brought notes, which
`share status` shows.

| `state` | Means | Do |
|---|---|---|
| `invalid` | a name that is not a usable name (lowercase letters, digits and dashes, at most 32), a note over 200 characters, or `share new` with neither `--with` nor `--from` | fix the argument |
| `name_taken` | a shared room by that name is already held here, or another process here took the name a moment ago | choose another; for `accept`, `--name` |
| `unknown` | no pair, room or offer by that name on this machine; from `accept`, also a name that is already a room held here | `share status` and `pair status` list them |
| `ambiguous` | more than one pair offers a room by that name; for `leave`, the name is both a room held here and an offer of another room | `share accept <pair>/<name>`; `share leave shared:<name>` for the room, `share leave <pair>/<name>` for the offer |
| `withdrawn` | the offer is no longer in the pair room — withdrawn, lapsed, or the room left — or the pair now names a different pairing than the one that made it | nothing to accept |
| `unconfirmed` | the pair named has not been confirmed, and nothing is shared over a pair until it is | `pair wait <name>`, and compare the SAS |
| `empty` | `--from`'s list is empty once its pairs were read: every pair on it left or is over | name pairs with `--with` |
| `unreachable` | `accept` could not read the pair room to fetch the live offer, or `share new --from` could not read a pair on the old list to learn whether it left; nothing was accepted or shared | try again when the hub is reachable; for `--from`, or name the list with `--with` |
| `left` | `--room shared:<name>`, after this machine left that room | the room is no longer held here |
| `offered` | `--room shared:<name>` for a room offered here and not accepted | `share accept <name>` |
| `store` | the share store or the pair store could not be read or written. For a room this machine created, the share store may hold the only copy of its keys, so nothing was overwritten | make the directory in `SWITCHBOARD_PAIRS` (default `~/.switchboard/pairs`) writable, with space left; move aside a file that is not valid JSON |
| `upgrade` | the share store was written by a newer release | upgrade agent-switchboard here |
| `disabled` | `SWITCHBOARD_PAIRS` is the empty string on this machine | unset it, or point it at a directory |
| `withheld` | a hosted bridge was asked for a `share_*` tool or a `shared:` room | share from a bridge on your own machine, or with `switchboard share` |
| a pair's state | the pair named is over or unusable — `ended_by_peer`, `ended`, `never_confirmed` and the rest — refused with the state the pairing is in | as [pairing.md](pairing.md#when-it-goes-wrong) says for that state |

## Versions

Records are share protocol 1, and the label their rids are derived from is
frozen (`switchboard/share/v1/rid`, in `sharing.py`): every machine must file
a room's records under the same rid. A record of a newer protocol is skipped
and said (`unreadable`, "upgrade to read it"), never half-read, and a store
written by a newer release is refused with `upgrade`. Every machine on a
room's list needs a release with shared rooms. The hub needs nothing: a
shared room is an ordinary room, and its records are board values in a pair
room. See [upgrading.md](upgrading.md).

Not in this release: offering a room as part of `pair start`, read-only
offers, accepting by policy, and `rendezvous` and `find` across shared rooms.
