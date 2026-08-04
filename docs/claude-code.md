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
"Coordinating with other agents" section to `CLAUDE.md`, and — if you have
not already pointed it at a hub — generates a dev token into a gitignored
`.env`. It merges into whatever is already in those files, so it is safe to
run again (e.g. after a teammate adds their own MCP server to `.mcp.json`).
Pass `--url`/`--token`/`--workspace` to point it at an existing hub instead
of the local default, or `--skip-mcp`/`--skip-hooks`/`--skip-claude-md` to
opt out of a step. Run `switchboard init --help` for the full list.

The rest of this page is what `init` does for you, spelled out — read it if
you want to understand or hand-customize any of the pieces.

## 1. Install

On every machine that runs an agent:

```bash
pip install agent-switchboard        # client + CLI + MCP bridge, one dependency
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

If the workspace uses [end-to-end encryption](encryption.md), `SWITCHBOARD_KEY`
needs the same treatment, plus a Claude-Code-specific catch: the MCP bridge is
a subprocess Claude Code spawns once, at session start, inheriting whatever is
already in your shell at that moment. The `SessionStart` hook in step 5 runs
*after* that, so it cannot hand the bridge a key the bridge already needed.
**Export `SWITCHBOARD_KEY` before you launch Claude Code** — not from inside a
hook, and not by putting it in a prompt (it's a long-lived secret with no safe
in-place rotation, so treat it like any other credential, not like something
disposable). See [Getting the key to the
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
with 13 tools.

## 4. Tell the agent to use it

Tools alone are not enough; the agent needs to know *when*. Add this to your
`CLAUDE.md`:

```markdown
## Coordinating with other agents

Other Claude sessions may be working this repo at the same time — locally, in
cloud sessions, and in CI. Switchboard is how you coordinate with them.

- **Before starting work**, call `roster` to see who else is active and what
  they hold, and `claim` the resource you are about to touch (a path, a
  directory, a subsystem). If `claim` reports someone else holds it, pick
  different work rather than waiting.
- **While working**, call `checkin` every few minutes. It keeps your claims
  alive and hands you anything other agents have said. If you stop calling it,
  your claims expire and free themselves — which is correct if you have
  crashed and wrong if you are still working.
- **When something you learn changes what another agent should do**, `say` it
  on a channel, or `dm` the specific agent. Examples worth sending: an
  interface you just changed, a test you discovered is flaky, a migration
  number you took, a plan you abandoned.
- **When you finish or abandon a piece of work**, `release` the claim.
- **For handoffs**, put the detail on the blackboard with `board_set` and
  mention the key in a message. Messages are for signals; the blackboard is for
  payloads.

Switchboard is ephemeral by design. Anything that should outlive the work still
belongs in a commit message, a PR body, or a doc — not in a channel.
```

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
            "command": "switchboard register --quiet -c build || true"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "switchboard claims --holder \"$(switchboard whoami --json | python -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" --json | python -c 'import sys,json,subprocess;[subprocess.run([\"switchboard\",\"release\",l[\"resource\"],\"--quiet\"]) for l in json.load(sys.stdin)]' || true"
          }
        ]
      }
    ]
  }
}
```

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
