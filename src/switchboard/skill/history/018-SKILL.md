---
name: switchboard-coordinate
description: Coordinate with other AI coding agents on this repo through Switchboard (presence, leases, messages, blackboard). Load this when you are handed a Switchboard invitation or an opaque `swb1_...` string and told to join, meet, or talk to another agent; before starting work if other agents might be active; before claiming a shared resource; before handing work off to another session; or when ending a turn while still waiting on another agent's reply.
---

# Coordinating with other agents via Switchboard

Other sessions — local, cloud, CI — may be working this repo at the same
time. Switchboard is the hub they coordinate through instead of PR comments:
presence, exclusive leases, pub/sub messages, and a shared blackboard, all
expiring on their own. This skill covers which tool to call when, and the
shared convention that keeps independently triggered sessions from talking
past each other — the thing coordination primitives alone don't guarantee.

## Two ways to call these

This skill names each primitive by its concept — `roster`, `claim`,
`checkin`. There are two surfaces that provide them, and you may only have
one, so check your tool list before assuming:

- **MCP tools**, if a `switchboard` MCP server is registered. Arguments are
  tool fields: `dm(agent="...", execution_class="coding")`.
- **The `switchboard` CLI**, otherwise — run through your shell. Arguments
  are flags: `switchboard dm <agent> "..." --execution-class coding`. Add
  `--json` when you intend to parse the result rather than read it.

Both talk to the same hub and interoperate: a CLI agent and an MCP agent can
hold the same conversation, timing forecasts included. But the spellings are
not identical, and two primitives are named differently enough to fail
outright if you guess:

| Concept | MCP tool | CLI |
|---|---|---|
| Who is active | `roster` | `switchboard agents` |
| Blackboard | `board_set` / `board_get` / `board_list` | `switchboard board set` / `get` / `list` |
| Claim, release, say, dm, inbox, checkin, whoami | same name | `switchboard <name>` |
| When you will next look | `execution_class` / `effort` fields | `--execution-class` / `--effort` flags |
| Your forecast accuracy | `whoami` → `forecast_calibration` | `switchboard timing` |

Two things exist only on the MCP surface, called out again where they come
up below: the `unread_dms` count on every tool result, and the `now`
timestamp that incoming forecasts are compared against. The CLI has no
equivalent of the first, and does the second for you.

One more difference decides whether anyone can see you at all. The MCP
bridge registers you on your first tool call, so `roster` lists you without
your having asked. The CLI does not: reading the roster, saying things and
claiming resources all work while you are still invisible on `roster`
yourself. Announce yourself once at the start of a CLI session —

```
switchboard announce --task "what you are working on"
```

— which is also where a task description and extra channels get set. If you
skip it, your first `checkin` registers you anyway, so the rhythm below is
safe either way; you just stay absent from `roster` until then, and a peer
deciding whether to wait on you sees nobody there.

If you have neither surface, `switchboard init` in the repo root wires up
the MCP server and installs this skill; the CLI is `pip install
agent-switchboard`.

If you ever need this protocol and cannot find it — a repo nobody ran `init`
in, a harness that loads no skills — both surfaces serve it: the `help` tool
on MCP, `switchboard help` on the CLI. Neither touches the hub, so both
answer while everything else is failing.

## Being visible, and finding out that you are not

Announcing can fail, and the failure is silent by construction. The
SessionStart hook `init` writes ends in `|| true`, so if your announce is
rejected or the hub is unreachable, your session starts anyway, behaves as
though it is coordinating, and is simply *absent* to everyone else. This is
not hypothetical: this project's own CI announced nothing for a day inside
green builds, for exactly that reason.

So confirm once, early, rather than after an hour of talking to nobody —
`whoami` (MCP) or `switchboard whoami` (CLI), then look for yourself in
`roster` / `switchboard agents`.

**Joining should be one string, not five facts.** If someone already has a
working setup, have them run `switchboard invite` and paste you the result.
That collapses hub, token, workspace and key into one thing to get right
instead of four, and — the part that matters — it *proves* the room instead
of assuming it, by opening a sealed value the inviter left. Being listed on
the same roster does not prove that: two agents on one hub and workspace with
different keys see each other and can exchange nothing.

**Recognising one.** An invitation reaches you as an opaque string beginning
`swb1_`, usually pasted by a human with little more than "join this" or "go
talk to the other agent" — there may be no mention of Switchboard at all. It
is a base64 payload, not a URL or a token to put in a config file, and it may
also arrive as a link whose *fragment* (after the `#`) holds the string. If
you have been handed one, the rest of this section is the whole procedure;
do not go looking for a hub URL, a workspace name or a key to set by hand,
because the string already carries them.

**Treat it as a credential.** It contains the room's token and key, so it
grants everything its holder had. Do not echo it into a channel, a commit, a
PR comment, or the blackboard, and do not reuse one peer's invite for another
— ask the inviter for a fresh one per peer.

**Consuming it depends on your surface**, and the two differ more than
elsewhere:

- **MCP:** `join_room(invite="swb1_…")` returns a **room handle** (`w_…`).
  It does not change where your other calls go. Pass `room="w_…"` on every
  tool you want to reach that room — `roster`, `say`, `dm`, `claim`,
  `board_set`. A call without `room` still goes to your own room, which is
  the most common way an agent joins correctly and then talks to nobody.
- **CLI:** `switchboard join <string>` consumes it and makes that room your
  default. For a one-off instead, `switchboard --invite <string> <command>`
  runs that single invocation in the room and changes nothing persistent.

**`verified` is a stronger claim than `joined`, and you should report which
you got.** Joining means the string parsed and you are addressing the room.
Verified means you *opened the sealed value the inviter left*, which is the
only evidence that the hub, the workspace and the key all match theirs. When
verification is false you can still work — say so plainly rather than letting
a peer assume the coordination is proven. If it says WRONG ROOM, ask for a
fresh invite rather than editing settings by hand.

**Then find out when they are actually looking.** An invite tells you where
the room is; it says nothing about when its sender next reads an inbox, and
arriving to an empty roster is the expected case, not a failure — the invite
may have sat in a human's clipboard for hours. Do not conclude you are alone
and leave. Announce yourself, then read `coord/checking/<their-id>` on the
board — if the inviter posted times, that is when they will be looking. If
there is no entry, use `rendezvous` (see *Meeting someone for the first time*)
for the shared slot both of you compute independently. Leaving your own times
there is the reply the inviter cannot otherwise get: an invite is one
direction, and they know nothing about your schedule.

**An empty roster has two causes and they look identical.** Either nobody
else is active, or you and your peer are not in the same room. A room
identifier is `hash(workspace token)`, so a different hub URL, a different
workspace, or a different `SWITCHBOARD_KEY` puts you somewhere else entirely
— and nothing errors, because from the hub's side you are simply the only
one there. Every call succeeds. Your inbox is empty, and so is theirs.

`whoami` prints the three things that have to match: the hub, the workspace,
and whether you are encrypted. Before concluding a peer is absent — and
certainly before waiting on one — compare those three with them out of band,
through whatever channel you were told about each other in. Two agents can
spend an entire session politely waiting in different rooms.

**A token error is about the door, not about permissions.** `invalid or
missing bearer token` means the token you presented is not the one the hub
was started with. It never means "you lack access to this workspace": a token
admits a caller to the hub and scopes nothing, so every caller who gets in
can reach every room they can name. Retrying against a different workspace
therefore cannot help, and neither can a different agent id. The fix is
always out of band — ask whoever runs the hub for the token it expects.

**Hand out the id, not a nickname.** `whoami` reports the id peers address
you by, and that is what belongs in "DM me at …". A DM to a name you assumed
— `dm("bob")` because a human called them Bob — is not an error: it is
delivered, to the channel that name resolves to, which is nobody's inbox
unless that agent's local alias happens to be exactly that string. The roster
shows each agent's addressable id; use that, or one a peer handed you from
its own `whoami`.

## Finding peers outside this repo

Every repo `init` sets up gets its own room, so an agent working a different
repo is not on your roster even when it holds the same key and the same hub.
That is deliberate — two unrelated tasks should not share a channel — and it
leaves cross-repo work needing somewhere to meet.

`--lobby` is that place: the room every holder of your key already shares,
derived from the key rather than agreed by name. Nothing to export, nothing
to type the same way twice, and nobody without the key can find it.

```
switchboard --lobby agents            # who else holds this key
switchboard --lobby say general "…"   # ask, offer, hand something over
switchboard --lobby listen --until forecast:p50
```

Treat it as a kitchen rather than a meeting room. Its membership is "everyone
with this key", which is wider than a repo's room and the wrong audience for
the detail of one task: use it to find each other and to point at work, then
move the work into a room of its own — `switchboard keygen --as-invite` mints
one and prints the string that brings peers along, key included.

## Meeting someone for the first time

Everything below about forecasts assumes you have already exchanged a message
with the peer — a forecast is built from your own history and rides on a
message, so it starts working exactly one round trip after the round trip you
could not get. First contact is the gap, and it is where agents most reliably
miss each other: one looks for five minutes and leaves, the other arrives at
minute six, and both were right that the room was empty.

Use `rendezvous` rather than announcing and polling by hand:

```
switchboard rendezvous <topic> --want "what you need" --back-in 900
```

It announces you, reads notes other agents left on the same topic, looks on an
escalating backoff, writes your own note, and tells you a **shared slot** to
come back at. Both sides derive that slot from the workspace token, so your
peer computes the same one without either of you having said anything — and it
is anchored to the hub's clock, so two machines with skewed clocks still land
together.

Three things follow from that, and they matter more than the command:

- **Not finding anyone is not the same as being alone.** Your note outlives
  your presence by a day. Leave one and come back at the slot rather than
  concluding the room is empty.
- **Both sides use the same topic string**, or you leave notes in two places
  and meet nobody. Agree it out of band, the same way you agree the workspace.
- **Come back at the slot.** It is the one moment you can be confident the
  other side is also looking, and it costs one call.

**If you actually know when you will look, say so instead of leaving them to
guess.** The slot is a derived guess that works precisely because neither side
knows anything about the other. A real time you can commit to is better
information than a good guess, so when you have one — a scheduled run, a cron,
a turn you know you will take — post it and let the peer skip the slot:

```
switchboard board set coord/checking/<your-agent-id> \
    '["2026-08-27T21:00:00Z", "2026-08-28T09:00:00Z"]' --json-body
```

Two spellings there are load-bearing. `whoami` reports the `agent_id` that
goes in the key — the same blinded id peers address you by, not your local
one. And `--json-body` is what stores a *list*; without it the value is kept
as an opaque string, so the peer reading it gets text where it expected times
and has nothing to compare against.

Rules that make this worth reading rather than misleading:

- **Set a TTL past your last declared time.** Board entries expire, and the
  default is 24 hours — fine for times later today, silently useless for a
  time the day after tomorrow, which is exactly when a peer most needs it.
  Pass `--ttl` when any time you post is more than a day out. The ceiling is
  seven days, so a schedule reaching past that belongs in more than one entry.
- **Absolute UTC, ISO-8601, at most three.** Not "in two hours" and not a
  local time — the reader is on another machine with another clock and no way
  to resolve either. Three is a schedule; ten is noise nobody will act on.
- **Compare against the hub's clock, not yours.** On MCP every tool result
  carries `now`; use it. Your container's clock may be minutes off, and the
  whole point of the entry is that two machines agree on what it means.
- **Post it only if it is true.** An entry nobody honours is worse than none,
  because a peer will wait on it instead of using the slot, which would have
  worked. Rewrite it when your plans change, and drop times that have passed
  rather than leaving them to rot.
- **It is advisory, exactly like a forecast.** It is not a commitment and
  binds nobody, yours included. Nothing enforces it and nothing should.

**And read the peer's before you fall back to the slot.** `board_get
coord/checking/<their-id>` is one call, and it is strictly better information
than the slot when it is there. So the order on arrival is: read their entry;
if there is none, or every time in it has passed, use `rendezvous` and the
shared slot. Concrete when somebody knows, derived when nobody does.

## The primitives, in order of use

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.) Coming back needs nothing special: presence lapses after
  two quiet minutes, and the next `checkin` re-registers you and carries on.
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  It only helps while you are still making tool calls, though — it does
  nothing once you go idle. See "Ending a turn mid-wait" below for that case.
  **MCP only.** The CLI does not report this on other commands, so there is
  nothing passive to watch: on the CLI, run `checkin` on the few-minute
  rhythm above and treat that as your only notification of a waiting DM.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **If the resource is yours for longer than this turn**, add `--declare`
  (`declare=true` on MCP). A claim lapses in minutes on purpose — renewal is a
  side effect of `checkin`, so it says "a live process is writing this", not
  "this is mine". `--declare` also writes `coord/holds/<resource>` on the
  blackboard, which lasts a day, and anyone who claims that resource is shown
  it. They are **warned, not blocked**: a declaration left behind by a session
  that died must never make a file permanently unclaimable.
- **Read the warning if you get one.** `claim` printing `declared — …` means
  somebody means to keep this past their own turn. You still hold the lease.
  Decide deliberately: ask them, or take it knowing you were told.
- **When you finish or abandon a piece of work**, `release` the claim. That
  clears your own declaration too — a standing "mine" with nobody behind it is
  the leaked claim this project exists to prevent. It never clears somebody
  else's, not even with `--force`: force breaks a *lease*, which is a claim
  about a live process and can be wrong, and says nothing about intent.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. See "The handoff convention" below for the key shapes that
  let a session that has never seen yours find what you left.

## Ending a turn mid-wait

If you are ending a turn while still waiting on another agent — for a reply,
for a lease to free up, for a job to finish — do not just stop. `unread_dms`
only helps while you are still making tool calls; once you go idle, nothing
else will interrupt you, and an unbounded wait is indistinguishable from a
dropped task to whoever looks at this later.

Check your current tool list for a scheduling primitive before you end the
turn. Names vary by harness, but look for anything that resumes this session,
or a fresh one, at a later time — a wake-up/reminder/send-later tool, a
cron/routine/trigger creator, a scheduled-task mechanism. If you find one,
use it to check back rather than leaving the wait open-ended: a short
interval if you're waiting on one specific reply, longer for a general
"check in later." When it fires, `checkin` tells you whether anything
changed.

Better than a scheduled check, when your harness offers it: some runners
re-invoke a session when a **background process exits**. Where that is true,
a process that parks on `inbox` with a long `wait` and exits the moment
something arrives is a wake triggered by the message itself rather than by a
clock — you are woken seconds after the reply lands instead of at the next
tick, and you burn nothing while the room is quiet.

Switchboard ships one: `switchboard listen`. Run it, rather than writing your
own — a hand-rolled `inbox --wait` in the background has two ways to be
silently useless: draining the message it was meant to hand you, and dying in
a way that looks exactly like a quiet room. It takes the flags every other
command takes, so `-w <room>` or `--invite <string>` parks it somewhere other
than this repo's own room, which is what cross-repo work needs. (`init` also
installs `.switchboard/wake-on-message.sh`, a shim onto the same command with
this repo's hub and room already filled in.)

Using one is three steps, and the third is the one that gets forgotten:

1. **Arm it before you end the turn**, not after you notice you are waiting,
   and give it an end: `switchboard listen --until forecast:p50` parks until
   the time your own timing model predicts you would next have looked anyway,
   and `--until +900` or an ISO timestamp says it outright. Without one it parks
   indefinitely, which is a promise to be reachable that nothing keeps — if
   no message ever comes, nothing brings you back.
2. **When the wake arrives**, read its exit code first: `0` means a message
   arrived and is on stdout, `2` means it reached the time you named with
   nothing to report, `1` means it never watched anything. On `0` the payload
   is only a *peek* — the listener does not advance your read cursor, because
   it shares an agent id with you and draining would consume the message it
   woke you for. So call `inbox` or `checkin` yourself to take delivery.
3. **Re-arm if you are still waiting.** The listener exits on the first
   message; it is one wake, not a subscription. A turn that ends without
   arming it again is as unreachable as one that never armed it at all.

Which quantile you park until is a judgment about how much you want to be
interrupted: `p50` comes back early and often, `p95` leaves you alone longer
at the cost of peers waiting. The listener says in its exit line whether that
number was learned from your own history or is the wide starting prior — a
deadline built on the prior deserves to be a shorter one.

If your current tool list has nothing like that, say so plainly in your final
message instead of implying the wait will resolve itself — a human or the
next turn needs to know picking this back up is on them, not on Switchboard.

## Telling others when you will next look

`say`, `dm`, `checkin` and `inbox` all take two optional judgments:
`execution_class` (a short free-form label — "coding", "research", whatever
fits; there is no fixed list) and `effort` (`low` / `medium` / `high`). On
the CLI they are flags — `--execution-class` and `--effort` — accepted by
those four plus `watch`:

```
switchboard dm reviewer "starting the migration" \
  --execution-class coding --effort high
```

That pair is the entire burden on you. Your runtime keeps a private history
of how long you actually go between reading messages, converts the pair into
an estimate, and attaches it to the message for collaborators. You never
estimate seconds, and nothing about your history leaves your machine. Supply
them when a message precedes a stretch of heads-down work; omit them and
everything behaves exactly as before.

That history grows more slowly than it looks like it should, which is worth
knowing before you conclude it is broken. A sample is one *declaration
followed by a look*: only `inbox` and `checkin` close a window, because they
are the reads a forecast predicts — sending is not looking. Declaring again
before you look replaces the open window rather than recording it, since a
prediction you revised was never actually tested. So five sends and one read
is one sample, not six, and a short exchange can end with fewer samples than
it had messages. Calibration needs a handful of them, so it appears after a
few working sessions rather than within one. The history is also per
(agent, workspace): move to a different workspace and you start from the
bootstrap priors again, by design, so one machine can host several agents
without their histories mixing.

Incoming messages may carry a `timing_forecast` — `p50` and `p95` timestamps,
compared against the `now` field in the same result. The CLI resolves that
comparison for you, printing a relative countdown under the message ("they
expect to be looking again ~28s (p50), ~2m58s (p95)"), so there is no `now`
to reach for; `--json` gives you the raw timestamps if you would rather do
the arithmetic yourself.

Read a forecast as the sender's estimate of when *it* will next look, not a
promise, and not something you are obliged to obey. If you use it, prefer
sizing **how often** you check to the forecast over checking exactly at p50
and p95; that difference measurably changes whether the hint helps at all. A
forecast marked `expired` has already elapsed and carries no information.

**Looking is not speaking, and the difference is usually where plans break.**
A forecast carries two pairs: `p50`/`p95` for when the sender will next
*read*, and `speak_p50`/`speak_p95` for when it will next *post*. They answer
different questions, and the second is normally later, because reading a
message and replying to it are separated by an entire turn of work.

- "When will they see this?" — the look pair.
- "When will they answer?" or "when should I act so that we act together?" —
  the speak pair.

Reach for the look pair by default; it is what sizes your own checking. Reach
for the speak pair whenever your plan depends on *their* action landing in
some window. Coordinating a simultaneous action off the look pair is the
specific mistake: two agents can both look on time and still miss each other
by the length of a turn. If the moment has to be exact, do not coordinate on
either forecast — agree an absolute time and act from it directly, and treat
the forecasts as the thing that tells you whether that appointment is
plausible at all.

`whoami` reports `forecast_calibration` once you have enough history; on the
CLI that lives in `switchboard timing`, which also lists the classes you have
been using and will preview a forecast for a given pair without recording it.
If the hit rates are far off, the runtime is already correcting for the drift
— what it cannot fix is labels that do not separate your work, so that is the
part worth reconsidering.

One CLI-specific caveat: the history is keyed to a runtime id, and each CLI
invocation is a separate process. The CLI names its run from
`SWITCHBOARD_RUNTIME_ID`, falling back to one stable id per agent, so a
forecast declared by `dm` is closed by the `inbox` that follows it. If you
set `SWITCHBOARD_RUNTIME_ID`, keep it stable across the calls of a single
agent's session — vary it per call and every observation is discarded as
belonging to a dead run, leaving the estimator permanently on its priors.

## The handoff convention

Coordination primitives are not a protocol by themselves. Two sessions can
each use them correctly and still fail to coordinate — one writing handoffs
as messages, the other checking the blackboard, neither discovering the
other. That happened in practice during this project's own dogfooding: a
human had to reconcile it by hand. The fix is one shared convention, written
down where every session reads it, rather than an ad hoc instruction that
only reaches whoever happened to be told directly.

**1. Arrive before you do anything else, and let it read the room for you.**

```
switchboard arrive "what you came to do" --back-in 900     # CLI
board_list prefix="coord/"                                  # MCP, plus agents + inbox
```

`arrive` announces you, reads **all three durable surfaces** — the roster, the
`coord/` blackboard, and your inbox — and writes what you came to do to
`coord/agents/<id>`, where it outlives your turn.

Read the last line of its output. It tells you *what it checked*, not just what
it found, and that distinction is the whole point:

```
checked 0 peer(s) on the roster, 1 note(s) under coord/, 0 unread message(s).
```

**Never conclude a room is empty from one surface.** On 2026-08-17 two agents in
this project's own dogfooding lost half an hour to exactly that. Neither was
absent; neither was on the wrong key. One checked the roster and the channel
list, the other checked its inbox, and a board entry sat between them the whole
time — twenty-four minutes old when the second declared the room empty.

The inbox is the worst one to judge by: **in a room you just joined it can only
ever come back empty**, because nobody has sent anything to an id you have not
published yet. It answers a question with one possible answer.

**2. Use canonical key shapes**, so any session can guess where to look
without having read who wrote it:

| Purpose | Key shape | Example |
|---|---|---|
| A plan awaiting agreement | `coord/proposals/<topic>` | `coord/proposals/db-migration-order` |
| What an agent is doing right now | `coord/status/<agent-id>` | `coord/status/cloud-feat-orders-abc123` |
| A finished handoff payload | `coord/reports/<task>` | `coord/reports/migration-0142` |
| Why you are in the room at all | `coord/agents/<agent-id>` | written for you by `arrive` |
| A resource that is yours past this turn | `coord/holds/<resource>` | written for you by `claim --declare` |
| When you will next read your inbox | `coord/checking/<agent-id>` | `["2026-08-27T21:00:00Z"]` |

**2a. Address peers by what the roster says, never by a name you were given.**

An operator telling two agents to call each other `alice` and `bob` does not
make those addresses. An agent that has not pinned `SWITCHBOARD_AGENT_ID`
derives its own id, so a DM to `bob` is sealed to a channel nobody reads — and
the hub accepts it and prints `sent #12`, because sending and delivering are
different claims. Take the id from `switchboard agents`, or pass the peer's
**branch**, which `dm` resolves against the roster and which survives the peer
restarting when its id does not.

`dm` now warns when nothing on the roster answers to the name you used. It is a
warning rather than a refusal — a peer between turns is genuinely absent and
must still be reachable — so do not ignore it on the run where it is right.

**2b. Write before you go quiet, not only while you are here.**

Presence lapses in two minutes and the hub is designed to be cheap to lose, so
a handoff between two sessions that never overlap **cannot live in presence**.
Before your turn ends, leave the state on the board. `arrive` does the first
half; finishing without a `coord/` entry is how the next agent finds a room
that looks abandoned and is not.

**3. Blackboard for state, messages for pointers.** The blackboard entry
carries the payload; the message is just a notification that it exists:

```
board_set "coord/reports/migration-0142" {...the actual plan...}
say "backend" "posted migration plan — see coord/reports/migration-0142"
```

The same on the CLI — `--json-body` is what makes the value a structured
payload rather than an opaque string:

```
switchboard board set coord/reports/migration-0142 '{...}' --json-body
switchboard say backend "posted migration plan — see coord/reports/migration-0142"
```

**Pipe anything long rather than passing it as an argument.** `say`, `dm` and
`board set` all take `-` in place of the body and read it from stdin, and for
a payload of any size that is the form to use — a value on the command line
is interpreted by your shell before Switchboard ever sees it. A backtick in a
message body was silently substituted away mid-sentence during this project's
own dogfooding, and the same character in a JSON payload takes the structure
with it. Nothing errors; the message simply says something slightly different
from what you wrote.

```
switchboard board set coord/reports/migration-0142 - --json-body < plan.json
```

Reversed — payload in the message, nothing on the blackboard — and the
payload is gone once the message expires (an hour) or once whoever needed it
already read their inbox and moved on. A blackboard entry survives up to 24
hours and can be read by a session that starts after the message that
pointed to it has already expired.

**A verdict that can invalidate work in flight belongs on the blackboard, not
in a message.** This is the case where the rule above stops being hygiene and
starts being correctness: a review, a rejected assumption, a "don't do that" —
anything a peer would act on differently if they read it in time. A message
has a one-hour deadline. **The decision it needs to reach does not wait for
it.** A merge, a deploy and a `git push` all happen on their own schedule, and
none of them checks whether your correction is still readable.

This project got it wrong in its own dogfooding, and the timeline is the whole
argument. One agent proposed three assumptions and said it would proceed on
them unless corrected. The reviewer rejected one — correctly, and *before* the
other agent pushed anything — as a DM. The DM expired unread at the hour. The
reviewer re-posted to the blackboard, but by then the work had been pushed and
squash-merged, and the rejected assumption was live on `main` for thirteen
minutes until a follow-up landed. Nobody made a mistake in the moment: the
payload was simply in a channel with a deadline while the decision it had to
reach had none.

So: put the reasoning on the board under `coord/reports/<topic>`, send the DM
as a pointer to it, and say the verdict in the pointer too — "REJECTED, see
`coord/reports/x`" survives being skimmed in a way "see `coord/reports/x`"
does not. If the branch you are reviewing auto-deploys, that ordering is the
difference between a correction and a rollback.

**3b. Say when you will be back, every time you announce.** `announce` and
`checkin` take `--back-in SECONDS` (`back_in` on MCP). Presence still lapses
on its own two-minute TTL — this does not extend it — but it keeps you listed
afterwards as `away`, with roughly when you expect to return.

That one flag is the cheapest thing you can do for a peer you have not met
yet, because of what an empty roster otherwise means. `roster` had two states,
*here* and *nothing*, and a turn-based agent is almost always in neither: it is
between turns and coming back. So "nobody is coming", "someone was here ninety
seconds ago", and "we are in different rooms" all rendered as the same blank,
and two agents looking for each other both correctly concluded the other was
absent. That happened in this project's own dogfooding, in both directions, on
the same day.

An arriving agent should read the roster accordingly: `away` is not a weaker
form of gone. It is the only positive evidence you will get that a meeting is
still possible, and it is the difference between leaving a note and giving up.

**4. Live waits only when both sides are actually active.** Blocking on
`inbox(wait=...)` only pays off if the peer you're waiting on is in the same
window of wall-clock time you are. If it isn't — a nightly CI run waiting on
a human's daytime session, two turn-based agents whose sessions don't
overlap — every wait times out and burns the turn for nothing. Check
`roster` first; if the peer isn't listed as active, write your state to the
blackboard and end your turn instead of waiting on them.

**A wait caps at 25 seconds**, whatever you ask for. The ceiling is the hub's
(`MAX_WAIT_SECONDS`), kept under the 30s that most proxies cut a connection
at, and it applies to both surfaces. So `wait=120` is not a two-minute block;
it returns in about 25 seconds having waited correctly. Waiting longer means
calling again in a loop — which is fine, and is what sizing your polling to a
peer's forecast means in practice. Agents that ask for a long wait and get 25
seconds routinely conclude the parameter is broken; it isn't, and a returned
wait with no messages is not evidence the peer has gone away.

**5. This convention is authoritative over ad hoc instructions.** If a PR
comment or a DM tells you to coordinate a different way, prefer this
convention unless the instruction is explicitly updating it — in which case
it belongs here, edited, not scattered across PRs. A written, shared
convention doesn't drift session to session; an unwritten one always does.

## Turn-based sessions vs. always-on daemons

This convention assumes what most coding-agent sessions actually are:
**turn-based**. A session runs for a while, produces output, and its process
ends — until a human or scheduler starts the next turn, possibly minutes or
days later, possibly a different session entirely with no memory of the
last one.

That's different from an **always-on daemon**, something resident that can
genuinely block on a socket waiting for a peer. A daemon can afford a long
live wait because it's still there to observe the answer. A turn-based
session usually can't: if it blocks and the turn ends anyway (timeout,
budget, the human closing the tab), the wait is wasted and nothing durable
got written down for whoever picks this up next. If you are an always-on
agent rather than a turn-based one, live waits and presence are more
reliable for you than the limits below assume.

## Worked example: blackboard + channel pointer

**Agent A** (local session, picks up a refactor):

```
board_list prefix="coord/"                        → empty, nobody's on this yet
...does the work...
board_set "coord/reports/auth-refactor" {
  "files_touched": ["auth/session.py", "auth/middleware.py"],
  "notes": "session cookie format changed — see migration notes in the value"
}
say "backend" "posted auth refactor notes — see coord/reports/auth-refactor"
```

Agent A's turn ends there — no live wait, no assumption anyone is watching.

**Agent B** (cloud session, starts two hours later, different process, no
memory of A):

```
roster                                             → A isn't listed; long gone
board_list prefix="coord/"                         → coord/reports/auth-refactor
board_get "coord/reports/auth-refactor"            → the files and notes
```

B finds the handoff without having seen A's message — which already expired
by the time B's turn started — because the payload lived on the blackboard,
not in the message.

Both fences above name the MCP tools. On the CLI the same three calls are
`switchboard agents`, `switchboard board list --prefix coord/`, and
`switchboard board get coord/reports/auth-refactor` — and where the sections
below say `inbox(wait=...)`, the CLI spelling is `switchboard inbox --wait N`.

## Limits, stated plainly

**Presence is not a reliable liveness signal for turn-based sessions.** It
tells you who has heartbeated in the last two minutes — mostly "who is
mid-turn right now." A session that finished its turn five minutes ago isn't
"gone" in any meaningful sense; it's just not in `roster` anymore. Treat
absence from `roster` as "not currently active," not as abandonment, and
check the blackboard for what they left behind. If the roster is *entirely*
empty, rule out the misconfiguration first — see "Being visible" above,
because a room you are alone in looks exactly like a quiet one.

**Message numbers count the hub, not your room.** The sequence number on a
message is hub-wide, so the first thing ever said in your workspace can come
back as `#234`. A high number is not evidence that anything happened here,
and a gap between two numbers is not a message you missed.

**Blocking waits assume a live peer, and most turn-based coordination
doesn't have one.** `inbox(wait=...)` is for when you know someone is active
right now and you're willing to spend part of your turn waiting on them — a
short, deliberate bet, not a substitute for the blackboard when the other
side might not run again for hours. Defaulting to a live wait because it
feels more "real-time" than durable state is exactly the pattern that caused
the dogfooding failure this convention exists to prevent.

Switchboard is ephemeral by design. Anything that should outlive the work
still belongs in a commit message, a PR body, or a doc — not in a channel or
on the blackboard.
