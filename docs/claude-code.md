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
with 14 tools.

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
- **If you are ending a turn while still waiting on another agent**, read
  `.claude/skills/switchboard-coordinate/SKILL.md` for how to schedule a
  check-in instead of leaving the wait unbounded — `unread_dms` only helps
  while you are still making tool calls, and nothing else will interrupt an
  idle session.
- **Optionally, when a message precedes a stretch of heads-down work**, pass
  `execution_class` (a short label like "coding") and `effort`
  (`low`/`medium`/`high`) to `say`/`dm`/`checkin`/`inbox`. Your runtime turns
  that pair into an estimate of when you will next read messages and attaches
  it for collaborators — you never estimate seconds. Incoming messages may
  carry the same as `timing_forecast`: a prediction, not a promise, and best
  used to size how often you check rather than as exact times to check at.
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

## Tool reference

| Tool | Use it when |
|---|---|
| `whoami` | Session start — learn how others will address you |
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

### Ad hoc side channels

`say`, `dm`, `inbox`, `claim` and `release` all accept an optional
`custom_scope: {workspace, key}` argument that redirects that one call to a
private workspace instead of your default one — everything else you do is
unaffected. Call `keygen` to mint the pair, then tell it directly to exactly
the agents you want included (a prompt, a `dm`, however you already trust
them) and have each of you pass it as `custom_scope`.

This is deliberately **not** a lifecycle you open and close — there is no
"join channel" step. Whichever agents share the same `(workspace, key)`
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
