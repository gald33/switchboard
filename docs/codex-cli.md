# Using Switchboard with Codex CLI

Codex CLI speaks MCP and has its own hook system, so the setup mirrors
[Claude Code's](claude-code.md) almost exactly — same MCP server, same
lifecycle automation, different config file.

## 1. Install

On every machine that runs an agent (not yet on PyPI, so install from GitHub):

```bash
pip install "agent-switchboard @ git+https://github.com/gald33/switchboard.git"
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
guidance there:

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
- **If you are ending a turn while still waiting on another agent**, and your
  environment can schedule a future wake-up, use it to check back rather than
  letting the wait go unbounded — a short interval if you are waiting on one
  specific reply, longer for a general "check in later." `unread_dms` only
  helps while you are still making tool calls; it does nothing once you have
  gone idle, and nothing else will interrupt you. When the wake-up fires,
  `checkin` tells you whether anything changed.
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

Codex hooks live in `config.toml` too, under a `[[hooks.<Event>]]` /
`[[hooks.<Event>.hooks]]` pair rather than Claude Code's nested JSON. The
same two events do the job — `SessionStart` to register, `Stop` for session
end to release:

```toml
[[hooks.SessionStart]]
[[hooks.SessionStart.hooks]]
type = "command"
command = "switchboard -q register -c build || true"

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "switchboard --json claims --holder \"$(switchboard --json whoami | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"agent_id\"])')\" | python3 -c 'import sys,json,subprocess;[subprocess.run([\"switchboard\",\"-q\",\"release\",l[\"resource\"]]) for l in json.load(sys.stdin)]' || true"
```

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
