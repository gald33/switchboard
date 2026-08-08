# Using Switchboard with Codex CLI

Codex CLI speaks MCP and has its own hook system, so the setup mirrors
[Claude Code's](claude-code.md) almost exactly — same MCP server, same
lifecycle automation, different config file.

## 1. Install

On every machine that runs an agent:

```bash
pip install agent-switchboard
```

## 2. Point it at a hub

See [deployment.md](deployment.md) for standing one up. You need a URL and a
token.

## 3. Register the MCP server

Codex reads MCP servers from `config.toml` — `~/.codex/config.toml` for every
project, or `<repo>/.codex/config.toml` to scope it to one repo (only for
projects you trust, since project-level config is trusted automatically):

```toml
[mcp_servers.switchboard]
command = "switchboard-mcp"

[mcp_servers.switchboard.env]
SWITCHBOARD_URL = "https://hub.example.com"
SWITCHBOARD_WORKSPACE = "my-org/my-repo"
```

`SWITCHBOARD_TOKEN` is inherited from the shell, so keep it in your own
environment rather than in the committed file.

## 4. Tell the agent to use it

Codex reads `AGENTS.md` instead of `CLAUDE.md`. Add the same coordination
guidance there. It points at a skill file installed under `.claude/skills/`
— that's shared infrastructure `switchboard init` writes for any agent to
read, not Claude-Code-specific tooling; Codex just opens it as a plain file:

```markdown
## Coordinating with other agents

Other agents may be working this repo at the same time — locally, in cloud
sessions, and in CI. Switchboard is how you coordinate with them.

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

Codex hooks live in `config.toml` too, under a `[[hooks.<Event>]]` /
`[[hooks.<Event>.hooks]]` pair rather than Claude Code's nested JSON. The
same two events do the job — `SessionStart` to register, `Stop` for session
end to release:

```toml
[[hooks.SessionStart]]
[[hooks.SessionStart.hooks]]
type = "command"
command = "export SWITCHBOARD_URL=https://hub.example.com; export SWITCHBOARD_WORKSPACE=my-org/my-repo; switchboard -q register -c build || true"

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "export SWITCHBOARD_URL=https://hub.example.com; export SWITCHBOARD_WORKSPACE=my-org/my-repo; switchboard --json claims --holder \"$(switchboard --json whoami | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" | python3 -c 'import sys,json,subprocess;[subprocess.run([\"switchboard\",\"-q\",\"release\",l[\"resource\"]]) for l in json.load(sys.stdin)]' || true"
```

Fill in your actual URL and workspace — `switchboard` runs as a plain shell
command in a hook, not inside the `switchboard-mcp` subprocess `.mcp.json`'s
`env` block reaches, so it can't assume those two are ambient the way
`SWITCHBOARD_TOKEN` usually is. Neither is secret, so they get exported
explicitly instead.

The `|| true` matters here for the same reason it does in Claude Code: a hub
being down should never block a session. Every Switchboard CLI command exits
non-zero on failure and prints to stderr, so a guarded hook degrades to a
no-op.

Codex hook commands time out at 1s for `Stop` by default (600s for other
events) — if the release pipeline above ever runs slow against a remote hub,
raise it explicitly:

```toml
[[hooks.Stop.hooks]]
type = "command"
command = "..."
timeout = 30
```

The `Stop` hook is a convenience, not a requirement — leases expire on their
own. Releasing eagerly just frees the resource sooner.

## Tool reference

Same 14 MCP tools as Claude Code — see the [tool reference in
`claude-code.md`](claude-code.md#tool-reference).

## Troubleshooting

**MCP tools missing.** Run `switchboard-mcp` by hand — the bridge logs its
identity and target hub to stderr on startup. Then check `switchboard health`.

**Tools return `hub_unreachable`.** The bridge starts even when the hub is
down, on purpose, so a hub outage does not stop your session from loading. Fix
the hub and retry; no restart needed.

**An agent shows as stale in the roster.** It has not heartbeated in over a
minute. It is probably mid-task on something long — or gone, in which case it
drops off entirely once its TTL runs out.

**Claims from a session that already ended.** They expire on their own. If you
need one back immediately, `switchboard release <resource> --force`.
