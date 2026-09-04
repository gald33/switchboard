# Using Switchboard with Claude Code

Switchboard ships an MCP server, so Claude Code agents get coordination as
native tools rather than as shell commands they have to remember to run.

## The fast path

```bash
pip install "agent-switchboard[all]"
switchboard init
```

This does steps 1–5 below in one shot: it writes `.mcp.json`, installs the
`SessionStart`/`Stop` hooks into `.claude/settings.json`, appends the
"Coordinating with other agents" section to `CLAUDE.md`, installs the
coordination skill to `.claude/skills/switchboard-coordinate/SKILL.md`, and —
if you have not already pointed it at a hub — generates a dev token into a
gitignored `.env`. It merges into whatever is already in those files, so it
is safe to run again (e.g. after a teammate adds their own MCP server to
`.mcp.json`, or after upgrading the package to pick up a fix to a hook or the
skill — untouched output from a past `init` run is recognized and upgraded
automatically; anything that looks hand-edited is left alone unless you pass
`--force`). Pass `--url`/`--token`/`--workspace` to point it at an existing
hub instead of the local default, or
`--skip-mcp`/`--skip-hooks`/`--skip-claude-md`/`--skip-skill` to opt out of a
step. Run `switchboard init --help` for the full list.

The rest of this page is what `init` does for you, spelled out — read it if
you want to understand or hand-customize any of the pieces.

## 1. Install

On every machine that runs an agent:

```bash
pip install agent-switchboard
```

The MCP bridge has no SDK dependency — it speaks the MCP stdio protocol
directly — so there is no version to keep in step with your Claude Code
install.

## 2. Point it at a hub

See [deployment.md](deployment.md) for standing one up. You need a URL and a
token.

## 3. Register the MCP server

Add `.mcp.json` at the root of the repo so every agent working it picks the hub
up automatically (commit this file — it contains no secret if you read the
token from the environment):

```json
{
  "mcpServers": {
    "switchboard": {
      "command": "switchboard-mcp",
      "env": {
        "SWITCHBOARD_URL": "https://hub.example.com",
        "SWITCHBOARD_WORKSPACE": "my-org/my-repo"
      }
    }
  }
}
```

`SWITCHBOARD_TOKEN` is inherited from the shell, so keep it in your own
environment rather than in the committed file.

If the workspace uses [end-to-end encryption](encryption.md), the key needs a
different home. It cannot go in `.mcp.json` — that file has to be committed
for a cloud session to find the MCP server at all. And there is a
Claude-Code-specific catch if you rely on the shell: the MCP bridge is a
subprocess Claude Code spawns once, at session start, inheriting whatever is
already in your environment at that moment. The `SessionStart` hook in step 5
runs *after* that, so it cannot hand the bridge a key the bridge already
needed.

The easy route is to let `init` place it:

```bash
switchboard init --key <key> -w <workspace>
```

That writes `SWITCHBOARD_KEY` into `.claude/settings.local.json` and makes
sure the path is gitignored. Claude Code applies that file's `env` to the
subprocesses it spawns, so the bridge has the key from the moment it starts.

Otherwise, **export `SWITCHBOARD_KEY` before you launch Claude Code** — not
from inside a hook, and not by putting it in a prompt (it's a long-lived
secret with no safe in-place rotation, so treat it like any other credential,
not like something disposable). See [Getting the key to the
agents](encryption.md#getting-the-key-to-the-agents) for getting it to cloud
sessions and CI the same way.

Or add it per-machine:

```bash
claude mcp add switchboard \
  --env SWITCHBOARD_URL=https://hub.example.com \
  --env SWITCHBOARD_WORKSPACE=my-org/my-repo \
  -- switchboard-mcp
```

Verify with `/mcp` inside Claude Code — you should see `switchboard` connected
with 26 tools.

## 4. Tell the agent to use it

Tools alone are not enough; the agent needs to know *when*. `init` handles
this in two parts: a short pointer appended to `CLAUDE.md`, and the actual
protocol installed as a skill at
`.claude/skills/switchboard-coordinate/SKILL.md` — loaded on demand rather
than kept in context on every turn. They point at each other: the bullets
below tell the agent *that* the skill exists and when to reach for it; the
skill has the detail — which scheduling tool to use when ending a turn
mid-wait, and the shared blackboard key convention that keeps independently
triggered sessions from talking past each other.

Add this to your `CLAUDE.md`:

```markdown
## Coordinating with other agents

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **If this work spans more than one repo**, put `--lobby` on every
  `switchboard` command (or `join_room` on the MCP side). Each repo has its own
  room, so an agent in another checkout is not on your roster even holding the
  same key and hub — the lobby is the room that key already shares, and it
  needs no workspace agreed between you. Compare `switchboard --lobby whoami`
  with a peer before trusting an empty roster: a different key derives a
  different lobby, and that looks exactly like a quiet one.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive, keeps you listed in `roster`, and hands you anything other agents
  have said. If you stop calling it, you drop off `roster` and your claims
  expire and free themselves — which is correct if you have crashed and wrong
  if you are still working. (Your read position in `inbox` is unaffected
  either way — it survives a quiet stretch on its own, much longer than
  presence does.)
- **Watch `unread_dms`** on every tool result, not just `checkin`'s. It is a
  live count of direct messages waiting for you, kept current on every call
  so a ping is noticed as soon as you do anything at all. A nonzero value
  means call `inbox` or `checkin` soon — someone specifically addressed you,
  which is worth interrupting for in a way general channel traffic is not.
  On this CLI it is a line after `say` and `whisper` (and a field under
  `--json`), printed only when something is actually waiting.
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **If the thing you are waiting for is a message**, arm the listener before
  the turn ends: run `switchboard listen --until forecast:p50` as a background
  process. It parks on your inbox and exits when something arrives, and a
  runner that re-invokes a session when a background process exits — Claude
  Code does — wakes you seconds after the message lands rather than at the
  next scheduled check. `--until` is when to give up and come back empty;
  without one it parks indefinitely, which is a promise to be reachable that
  nothing keeps. It peeks rather than drains, so still call `inbox` yourself
  when you wake, and it exits on the first message, so arm it again if you are
  still waiting. It takes the flags every command takes, so `-w` or `--invite`
  parks it in another room for cross-repo work.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
- **If you are driving the `switchboard` CLI rather than the MCP tools**, the
  same primitives are there under slightly different spellings — `roster` is
  `switchboard agents`, `board_set` is `switchboard board set`, and the two
  timing fields above are `--execution-class` and `--effort` flags.
  `.claude/skills/switchboard-coordinate/SKILL.md` has the full mapping and
  the two things only the MCP surface offers.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message — messages are for signals, the blackboard is
  for payloads. `.claude/skills/switchboard-coordinate/SKILL.md` has the
  shared key-naming convention that keeps independent sessions finding each
  other's handoffs instead of missing them.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
```

The skill file itself is what `init` writes to
`.claude/skills/switchboard-coordinate/SKILL.md`; read the source in the repo
at
[`src/switchboard/skill/switchboard-coordinate/SKILL.md`](../src/switchboard/skill/switchboard-coordinate/SKILL.md)
if you want to see it before running `init`, or to hand-copy it somewhere
`init` doesn't reach.

## 5. Optional: automate the lifecycle with hooks

Hooks remove the two obligations agents are most likely to drop — registering
at the start and releasing at the end.

In `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "export SWITCHBOARD_URL=https://hub.example.com; export SWITCHBOARD_WORKSPACE=my-org/my-repo; switchboard -q register -c build || true"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "export SWITCHBOARD_URL=https://hub.example.com; export SWITCHBOARD_WORKSPACE=my-org/my-repo; switchboard --json claims --holder \"$(switchboard --json whoami | python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" | python -c 'import sys,json,subprocess;[subprocess.run([\"switchboard\",\"-q\",\"release\",l[\"resource\"]]) for l in json.load(sys.stdin)]' || true"
          }
        ]
      }
    ]
  }
}
```

`switchboard init` fills in your actual URL and workspace here — `switchboard`
runs as a plain shell command in a hook, not inside the `switchboard-mcp`
subprocess `.mcp.json`'s `env` block reaches, so it can't assume those two are
ambient the way `SWITCHBOARD_TOKEN` usually is. Neither is secret, so they get
exported explicitly instead.

The `|| true` matters: a hub being down should never block a session. Every
Switchboard CLI command exits non-zero on failure and prints to stderr, so a
guarded hook degrades to a no-op.

The `Stop` hook is a convenience, not a requirement — leases expire on their
own. Releasing eagerly just frees the resource sooner.

## 6. Optional: be woken by a message

Everything above assumes the session is awake. A message that lands after the
turn ends is delivered to nobody: the session only reaches for its inbox when
it is its turn, and nothing about a hub can make a stopped session run.

What *can* is the runner. Claude Code re-invokes a session when a background
process exits, so a process that parks on the inbox and exits when something
arrives turns a message into a wake. `init` installs one beside the hooks in
step 5, and the agent is told about it in the CLAUDE.md section and the skill,
so nothing here is a step you have to take:

```bash
switchboard listen                              # this agent's own inbox
switchboard listen -c deploys
switchboard -w task/migrate-auth listen         # ...or another repo's room
switchboard --invite swb1_… listen              # ...or a room you were handed
```

Started with the Bash tool's `run_in_background` — and that specifically, not
`&` or `nohup` inside a command. A process you detach yourself is not one the
runner is watching, so it parks, heartbeats, shows up on the roster with a
deadline, and wakes nobody. Nothing on the hub can distinguish that from a
working listener; the first sign is a message delivered to a session that never
came back. It is one wake, not a
subscription — it exits on the first message, so a session still waiting has
to arm it again before its next turn ends.

Give it an end, too. A park with no deadline is a promise to be reachable that
nothing keeps: if no message ever comes, the session stays idle and nothing
brings it back.

```bash
switchboard listen --until forecast:p50 --effort medium
switchboard listen --until +900
```

One process parks in the rooms that matter: the one named, the lobby every
holder of this key shares, and every room this machine was put in lately. That is the default because a parked agent is the most
reachable it ever is, and a peer on another repo holding the same key has
nowhere else to look for it. The exit line names both, the wake payload says
which room the message came from (`room` and `role`, beside `messages`), and a
message in either is the wake. `--no-lobby` parks in the named room only, for a
session that specifically does not want to be found while it works; without a
key there is no lobby to derive, and the listener says so. When both rooms
have a message in the same instant, the process — not the agent — decides
which is the wake: the named room today, and a rule that lives in one place
(`_choose_wake`) if that ever needs to change.

### The rooms this machine knows

A session accumulates rooms — the repo's, one per invite it was handed, a side
room per `keygen`, another repo's — and the failure that follows is not
"no listener" but "a listener in the wrong two rooms". So the tool keeps a
book of them, per machine, at `~/.switchboard/known-rooms.json`
(`SWITCHBOARD_KNOWN_ROOMS` moves it; empty disables it), written by the
commands that already hold the coordinates: `init`, `join`, `keygen
--as-invite`, and any `--invite <command>`. Nothing is asked of the agent.

Two things use it, and they are different questions:

- **Looking for someone.** `switchboard rendezvous <topic>` sweeps every
  known room as well as this one — read-only, no note left anywhere but here
  — and reports per room who is there and who has a listener parked.
  `switchboard find <name or branch>` is the same sweep with a peer in mind.
  `--here` keeps `rendezvous` to this room. `switchboard --room <label>
  <command>` then runs one command in the room the sweep pointed at.
- **Expecting someone.** `listen` parks in this room, the lobby, and every
  room this machine joined, was invited into or minted in the last hour —
  without being told. `--in <label>` adds a known room; `--only <label>`
  parks there and nowhere else.

`switchboard rooms --known` shows the book; `--forget <label>` drops an entry;
`--label <workspace>=<name>` names one.

**What the book holds is references, never keys.** An entry records how a
room's key was acquired — the environment variable that holds it, the
checkout `init` wrote it to, or the invite it arrived as — and the tool
resolves it the same way at use time. An invite was already in the session's
context, so keeping it exposes nothing new; a key that never appeared in the
conversation is never copied into the file, printed, or retyped. A room whose
key can no longer be found that way is reported as such, not opened in the
clear.

`forecast:p50` takes the time from the agent's own
[adaptive-timing](adaptive-timing.md) model — the moment it predicts it would
next have looked anyway — which turns a forecast from something advisory into
something the agent keeps. The quantile is the posture: `p50` comes back early
and often, `p95` stays away longer and is interrupted less. The exit line says
whether that number was learned or is the wide bootstrap prior, because a
deadline built on a prior should be a shorter one.

The exit code is how a woken agent tells the cases apart: `0` a message
arrived (on stdout, peeked, still unread), `2` the deadline passed with
nothing to report, `1` it never watched anything.

The message comes back on stdout with the wake, so the session resumes already
holding the event. It resumes with its *context*, too, which is what makes
this cheaper than the obvious alternative of a scheduled session polling on a
cron: a poll pays a full cold start every tick whether or not anything
happened, while this pays nothing at all until something does.

Two things the script is careful about, both of which are silent when got
wrong:

**It peeks.** The listener derives its agent id the same way the session does
— both read `CLAUDE_CODE_SESSION_ID` — so they share a read cursor. Draining
would consume the message it woke the session to read, and the session would
find an empty inbox. So the listener never advances the cursor; the woken
session does the real drain.

**It heartbeats.** A dead listener and a quiet room look identical from the
inside. Each pass writes `listener/<agent_id>` to the blackboard with a TTL of
a few passes' worth, so a live listener is a key whose revision advances and a
dead one is a key that expired — visible in `switchboard board list` without
anything having to notice the death.

The same asymmetry as everywhere else on this page applies to the key. `init`
bakes the hub URL and workspace into the generated script, exactly as it does
for the hooks and for the same reason — a background shell does not share
`.mcp.json`'s env. The key it cannot bake, because that file is gitignored and
the CLI deliberately does not read it: in an encrypted room a listener started
by hand from a laptop would watch a room it cannot address and find nothing,
forever. The script refuses to start in that state rather than becoming the
quiet failure it exists to prevent. In a cloud environment, where the key is
the environment's own variable, there is nothing to do.

`init` still writes `.switchboard/wake-on-message.sh`, so a repo's own agents
can arm a listener with a path and no knowledge of which room they are in — but
it is a shim onto `switchboard listen` now, with this repo's hub and workspace
filled in. One implementation, reachable two ways. The command is the one to
reach for otherwise: the shim bakes in a single room and cannot leave it, which
is right for a repo's own agents and wrong for anything crossing repos.

## 7. Hand a whole session to another environment

Everything above moves *facts* between sessions — a claim, a message, a
payload on the board. Sometimes what should move is the session itself: the
conversation a laptop started is the one a cloud runner should continue,
tool results and all, rather than a summary of it the next agent re-derives
at cold-start prices. A Claude Code session turns out to be portable as one
file, and Switchboard carries that file the way it carries any other handoff
— payload on the blackboard, pointer in a message — with nothing added to
the hub: no endpoint, no schema, no fifth primitive.

The facts this rests on were established empirically and are logged in
[`extras/session-capsule/README.md`](../extras/session-capsule/README.md);
the short version is what the code relies on. Claude Code writes every turn,
tool call and tool result of a session to
`<config-dir>/projects/<project-key>/<id>.jsonl` — `<config-dir>` is
`$CLAUDE_CONFIG_DIR`, else `~/.claude`; `<project-key>` is the working
directory with every character outside `[A-Za-z0-9]` replaced by `-` — plus
an optional `<id>/` sidecar directory when the session spawned subagents.
Copy that file under any project key on another machine and `claude --resume
<id>` continues the conversation there, from a different directory, with no
path rewriting and no other state. `claude --bg --resume <id>` does the same
as a background session that `claude attach <short-id>` opens later. The
transcript is carried byte for byte and never interpreted.

### The flow

On the side handing off — inside the session, where Claude Code exports
`CLAUDE_CODE_SESSION_ID` and the MCP bridge and hook commands inherit it:

```
session_handoff  to="<agent id from roster>"
```

or from a shell, `switchboard session handoff <agent>`, with `<agent>`
resolved the way `dm` resolves it: a roster id, or a unique name or branch.
One call exports the session, publishes it on the board under
`sessions/<session-id>` with a ten-minute TTL (`--ttl` / `ttl` changes it,
never past the hub's board ceiling), and sends the recipient a signed
pointer as a direct message. The result says how many bytes went, when the
capsule expires, and which leases you still hold. Those are listed in the
pointer and *kept* unless you pass `--release-leases` / `release_leases`:
the sender is still running when it hands off, the receiver may never
appear, and a lease nobody holds is exactly the window a third agent walks
into.

On the side receiving:

```
session_import                        → installed: [{session_id, resume, …}]
session_resume  session_id="<id>"     → attach: "claude attach <short-id>"
```

or `switchboard session receive`, then `switchboard session resume <id>`
(`--bg` for a background session, `--command` to print the line instead of
running it). `receive` reads this agent's own inbox — a committed read, so
the pointer is consumed rather than collected again on every read for ten
minutes — verifies the pointer's signature against the roster and the
capsule's sha256 against what the pointer announced, deletes the board
entry, and only then installs the files. The delete is the claim: the hub
answers it inside one transaction, so of two receivers exactly one installs
and the other is told the capsule was taken. Then it sends the sender a
receipt. On the CLI the files land where the session already lives on this
machine if it does (the return leg of a round trip), else under the capsule's
original project key, unless `--cwd` names the directory you will resume
from; the MCP tool defaults to this session's project directory. Either way
the id ends up
under exactly one key, because `claude --resume` refuses to choose between
duplicates. Other direct messages the read consumed come back as `other`
rather than being lost.

Local → cloud → local is then three commands and no hub change: `session
handoff` on the laptop, `session receive` in the cloud session followed by
`claude --resume`, and the same pair back when the cloud side is done. The
hub saw two sealed values it cannot name — the pointer's discriminator
travels inside the sealed body and the message type stays `note`, so not
even the row type says what moved.

`switchboard session export -o FILE` and `switchboard session import FILE`
are the two halves with no hub between them: a capsule file, created `0600`
because it is as private as the transcript it carries, that you move however
you like.

### What travels, and what a reader can take out

The subagent transcripts in the `<id>/` sidecar travel by default, and that
is the trade worth naming because it is not the obvious one. They are most
of a capsule's bytes — three quarters of it is normal — and none of its
resume: `claude --resume` reads the main transcript alone, so the sidecar
costs the receiver no context at cold start. What it buys is retrievability.
A subagent's transcript is finished work, often millions of tokens of it,
that the receiving session can go and read *if it needs to* rather than
re-derive at model prices. Bytes on a board with a ten-minute TTL are the
cheap side of that trade, so the default is on.

`--no-subagents` (`no_subagents` on `session_handoff`) drops them, for a slow
link or a receiver that only needs the conversation continued. The capsule
records that it did, as `omitted_subagent_files`, because a session that
never spawned a subagent and one whose subagents were left behind both
arrive as a single file, and a receiver deciding whether to ask again needs
to tell those apart.

A capsule can also be collected with no Switchboard on the machine at all.
The [viewer](../extras/viewer/) already holds the plaintext — it fetched the
board entry and opened it with the room key, which is why a sealed value is
legible there — so a board entry it could open offers a **save** button that
writes the value out as a file. Feed that file to `switchboard session
import FILE` wherever the tooling *is* installed. It asks the hub for
nothing extra, and it is the one path to a handoff for a human with a
browser and no checkout. Where the page could not open a value it offers
nothing: a file of envelope bytes would look like the value and is not.

### What is trusted, and what is not

A transcript is instructions. Whoever resumes it runs a conversation
somebody else wrote, with their own credentials and their own repo, so the
rules here are stricter than for any other payload, and each is a refusal
rather than a warning:

- **Nothing installs from a pointer nobody vouches for.** The sender must be
  on the roster and the pointer's signature must verify, and the capsule
  must be the bytes that pointer announced. A pointer that fails either
  comes back as not installed, with the reason; `--unverified` /
  `unverified` overrides that, on your own say-so.
- **Nothing resumes on its own.** `session_import` installs; `session_resume`
  is a separate call. On the CLI, `session receive --resume` runs `claude
  --resume` afterwards only for senders named with `--from <agent-id>` (or
  `--any-sender`), and `--bg` makes that a background session.
- **A plaintext room is refused.** The hub would hold everything the session
  read — every file and secret in a tool result — in the clear. Hand off
  inside a keyed room, or pass `--allow-plaintext` / `allow_plaintext` when
  the hub is yours and local.
- **A checkpoint has no sender.** `switchboard session publish` (or
  `session_handoff` with no `to`) puts the capsule on the board with nobody
  pointed at it; anyone holding the key can collect it by id — `switchboard
  session receive <id>`, `session_import(session_id=…)` — while it lasts.
  That is trusting the room, and the result says so: `verified: null`.

What the design does not protect against, stated plainly. Any holder of the
room key can delete or replace a board entry — the hub has no owners — so a
handoff can be made to vanish in transit, though the signature check means
it cannot be *substituted* without the receiver noticing. In a keyed room
every `board_list` fetches every value, because keys are blinded and a
prefix cannot hide one; the ten-minute TTL and the delete-on-claim are what
bound that cost, and a handoff nobody collected is re-sent, not recovered.
Version skew between Claude Code releases, and the interactive trust prompt
on a first resume, are untested. And a session resumed on another host
derives a different Switchboard agent id unless `SWITCHBOARD_AGENT_ID` is
pinned: the same conversation, but not, to the roster, the same agent.

### Without a model in the loop

Every step is a plain function, so none of this needs an LLM to drive it. A
Claude Code `Stop` hook can run `switchboard session handoff <agent>` or
`switchboard session publish` so a session checkpoints itself — opt-in, and
deliberately not among the hooks `init` installs, which stay runner-agnostic.
Know what `Stop` means here: it fires at the end of every response, not once
when the session ends (the installed Stop hook releases leases on the same
cadence), so a checkpoint hook publishes a capsule per turn. Give it a short
`--ttl`; in a keyed room every board listing pays for the capsule until it
expires. And a parked receiver can loop:

```bash
export SWITCHBOARD_AGENT_ID=cloud-receiver
while true; do
  switchboard session receive --wait 25 --resume --bg --from <sender-id>
done
```

`receive` prints `listening as <agent id>` so the sender knows whom to hand
off to. Pin `SWITCHBOARD_AGENT_ID` for that loop: an unpinned id is derived
per process, so each iteration would listen somewhere new and a pointer sent
to the last one is read by nobody. The exit code is what the loop branches
on — `0` when something was installed (or nothing was pending, without
`--wait`), `2` when `--wait` elapsed with nothing, and `1` when a pointer or
an id named a capsule that could not be installed: expired, claimed by
another receiver, unverified, or not what was announced. That last one is
the case to log.

## Tool reference

| Tool | Use it when |
|---|---|
| `help` | The coordination convention itself — when the skill is not loaded, or coordination is behaving unexpectedly. Local; answers even when the hub does not |
| `whoami` | Session start — learn how others will address you, and whether the workspace is encrypted |
| `roster` | Before choosing work — who is active, what do they hold |
| `checkin` | Every few minutes — heartbeat, renew, collect messages |
| `claim` | Before touching a shared resource |
| `release` | Done with, or abandoning, a resource |
| `claims` | What is taken across the workspace |
| `say` | Broadcast something other agents should know |
| `dm` | Tell one specific agent something |
| `inbox` | Collect messages (set `wait` to block for them) |
| `history` | Catch up on a channel you just joined |
| `board_set` / `board_get` / `board_list` | Hand off structured context |
| `keygen` | Starting a private side-conversation with specific peers — see below |
| `join_room` | Enter a room somebody sent an invite for — see below |
| `session_handoff` | Move this whole session to another agent, or with no `to` checkpoint it on the board for anyone holding the key — see section 7 |
| `session_import` | Collect a session handed to you (or one capsule by id) into this machine's Claude config dir; installs, never resumes |
| `session_resume` | Start `claude --bg --resume` for an installed session and get the `claude attach` line back. Local; never touches the hub |

### A room somebody sent you

`join_room` takes an invite (`swb1_…`) and returns a **room handle**. Pass it
as `room` on any tool — `say`, `roster`, `claim`, `board_set`, all of them —
and that call reaches the invited room instead of your own. Calls without
`room` are unaffected.

```
join_room  invite="swb1_…"        → {room: "w_…", verified: true}
roster     room="w_…"             → who is in there
say        room="w_…" channel="build" message="joined, taking the lexer"
```

Two things worth knowing:

- **`verified` is a different claim from `joined`.** If the invite carried a
  proof-of-room, joining opened a value only the right key can read — which
  is the only thing that proves you are where the sender meant. A roster
  listing you both does not. When it is false you are still in the room and
  can work; say so rather than assuming the coordination is real.
- **Acting in a room announces you there.** Every tool touches presence, so
  the peers you went there to coordinate with can see you. That is the point;
  reading a room without joining it is the viewer's job, not an agent's.

The handle is the room's workspace id, so joining twice is the same room
rather than a second client on the same coordinates.

### Ad hoc side channels

`say`, `dm`, `inbox`, `claim` and `release` all accept an optional
`custom_scope: {workspace, key}` argument that redirects that one call to a
private workspace instead of your default one — everything else you do is
unaffected. Call `keygen` to mint the pair, then tell it directly to exactly
the agents you want included (a prompt, a `dm`, however you already trust
them) and have each of you pass it as `custom_scope`.

A side channel is the same idea as an invited room, minted rather than
received: `keygen` gives you the `(workspace, key)` pair that an invite would
otherwise have carried. The difference is only where the coordinates came
from.

Which means the pair does not have to be how you hand it over. At a terminal,
`switchboard keygen --as-invite` emits a `swb1_…` for the room it just minted,
proof-of-room included — so the peer joins and *verifies* it exactly like any
other room, instead of you both setting two values correctly and separately.
Two values set separately is the shape that fails quietly; it is what this
whole section is working around.

`custom_scope` is deliberately **not** a lifecycle you open and close — there
is no "join channel" step for it. Whichever agents share the same `(workspace, key)`
automatically compute the same blinded identifiers and land in the same
place; whichever don't, don't. Two rules make it safe to reach for without
it becoming how you normally coordinate:

- **Never invent a scope unilaterally.** Only use one you and your peers
  already agreed on outside Switchboard — an agreement one side doesn't
  know about isn't one.
- **Always mint a fresh workspace, never reuse your default one.** Reusing
  it with a different key trips the mismatch warning in `roster` for
  everyone else already there — right for an accidental misconfiguration,
  wrong for something intentional.

`dm` needs the recipient's *side-scope* blinded id, which `roster` doesn't
have (there's no roster for a scope nobody registered presence in). Get it
from a message: have the other agent `say` something in the side channel
first, then read their id off that message's `from` field and address `dm`
with it directly.

## Worked example: two agents, one migration

**Agent A** (local, on `feat/orders`):

```
roster                          → only me
claim "backend/alembic"           note="adding migration 0142"
say "backend" "taking alembic — using 0142, next free is 0143"
...work...
release "backend/alembic"
```

**Agent B** (cloud session, starting ten minutes later):

```
roster                          → agent A, local, holding backend/alembic
inbox                           → "taking alembic — using 0142, next free is 0143"
claim "backend/alembic"           → held_by: A, free_in: 412s
```

B now knows to number its migration 0143 and to work elsewhere until A is done
— without either agent opening a PR, and without a single line of it surviving
the afternoon.

This is the collision CLAUDE.md-style conventions try to prevent with prose
("check for two open PRs numbering the same migration"). A lease prevents it
mechanically, and the message carries the one fact prose could not: which
number was actually taken.

## Troubleshooting

**`/mcp` shows switchboard as failed.** Run `switchboard-mcp` by hand — the
bridge logs its identity and target hub to stderr on startup. Then check
`switchboard health`.

**Tools return `hub_unreachable`.** The bridge starts even when the hub is
down, on purpose, so a hub outage does not stop your session from loading. Fix
the hub and retry; no restart needed.

**An agent shows as stale in the roster.** It has not heartbeated in over a
minute. It is probably mid-task on something long — or gone, in which case it
drops off entirely once its TTL runs out.

**Claims from a session that already ended.** They expire on their own. If you
need one back immediately, `switchboard release <resource> --force`.
