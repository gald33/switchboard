# Switchboard

**An ephemeral orchestration hub for AI coding agents.**

When several coding agents work the same repo — one on your laptop, one in a
cloud session, one in CI — they need to coordinate. Today they mostly do it
through pull request bodies and review comments. That works, but it is the
wrong medium for most of what they have to say:

- **It's permanent.** "I'm taking the migration file, don't touch it for the
  next 20 minutes" is true for 20 minutes and then it is litter in your
  repository history, forever.
- **It's slow.** A PR comment is a message with a latency floor measured in
  whole review cycles.
- **It can't say "now".** There is no way to ask *who else is awake right now*,
  and no way to hold a claim that releases itself when you crash.

Switchboard is a small hub the agents talk to instead. Everything in it
**expires on its own**, because coordination state is not a record — it should
live exactly as long as the work does.

```
   local agent ─┐
   cloud agent ─┼─►  switchboard hub  ─►  SQLite (everything TTL'd)
   CI  agent   ─┘         ▲
                          │  MCP tools / CLI / REST
```

---

## Four primitives

| | What it's for | Default TTL |
|---|---|---|
| **Presence** | Who is working right now, on what branch, on what task | 2 min |
| **Leases** | Exclusive claim on a resource key — *expires instead of leaking* | 15 min |
| **Messages** | Channel pub/sub with per-agent read cursors | 1 hour |
| **Blackboard** | Shared key/value scratch space for handoffs too big for a message | 24 hours |

That's the whole model. Direct messages aren't a fifth concept — a DM to agent
`bob` is just a message on channel `@bob`.

### Why leases expire

This is the part worth dwelling on. A conventional "claim" — a row in a table,
a lock file, a label on an issue — is acquired explicitly and released
explicitly. The release is the half that gets dropped, because nothing ever
*asks* an agent whether it is still working. A session crashes, or merges and
moves on, and its claim sits there holding a piece of work hostage until a
human notices.

A Switchboard lease is acquired explicitly and released **by running out**.
Agents renew what they hold as a side effect of their heartbeat, so a live
agent keeps its claims and a dead one gives them up within a minute or two,
with nobody having to remember anything.

---

## Install

```bash
# Agent side: client + CLI only (one dependency, httpx)
pip install agent-switchboard

# Hub side: also the server
pip install "agent-switchboard[server]"

# With the MCP bridge for Claude Code / any MCP client
pip install "agent-switchboard[all]"
```

## Run a hub

```bash
export SWITCHBOARD_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
switchboard serve --host 0.0.0.0 --port 8787 --db ./switchboard.db
```

Or with Docker:

```bash
docker run -p 8787:8787 -e SWITCHBOARD_TOKEN=secret -v swb:/data ghcr.io/gald33/switchboard
```

The hub is one process and one SQLite file. It holds no source code and no
credentials — only who is awake and what they are saying to each other — so it
is cheap to run and cheap to lose.

## Point agents at it

```bash
export SWITCHBOARD_URL=https://hub.example.com
export SWITCHBOARD_TOKEN=secret
export SWITCHBOARD_WORKSPACE=my-org/my-repo
```

```bash
switchboard whoami                       # identity inferred from git + host
switchboard agents                       # who else is awake
switchboard claim db/migrations -m "adding 0142"
switchboard say build "migrations are mine for ~15m"
switchboard inbox --wait 25              # long-poll for messages
switchboard release db/migrations
```

## Use it from Claude Code

Add the MCP server (`.mcp.json` in your repo, or `claude mcp add`):

```json
{
  "mcpServers": {
    "switchboard": {
      "command": "switchboard-mcp",
      "env": {
        "SWITCHBOARD_URL": "https://hub.example.com",
        "SWITCHBOARD_TOKEN": "secret",
        "SWITCHBOARD_WORKSPACE": "my-org/my-repo"
      }
    }
  }
}
```

Agents then get these as native tools:

| Tool | Does |
|---|---|
| `whoami` / `roster` | this agent's identity; who else is awake |
| `claim` / `release` / `claims` | take, drop and inspect leases |
| `say` / `dm` / `inbox` / `history` | channel and direct messaging |
| `board_set` / `board_get` / `board_list` | shared scratch space |
| `checkin` | heartbeat + renew leases + drain inbox, in one call |

`checkin` is the one that matters most in practice: a single tool call that
keeps the agent alive, renews everything it holds, and hands back anything
other agents said since last time.

See [`docs/claude-code.md`](docs/claude-code.md) for the full setup including a
`SessionStart` hook that registers the agent automatically and a `Stop` hook
that releases its leases.

## Use it from Python

```python
from switchboard import Client, LeaseHeld, detect_identity

me = detect_identity()
with Client(agent_id=me.agent_id) as hub:
    hub.register(name=me.name, kind=me.kind, branch=me.branch, channels=["build"])

    try:
        hub.acquire("db/migrations", note="adding 0142", ttl=900)
    except LeaseHeld as exc:
        print(f"{exc.holder} has it for another {exc.expires_in}s — doing something else")
    else:
        hub.post("build", "migrations are mine for ~15m")
        ...
        hub.release("db/migrations")
```

---

## Documentation

- [Quickstart](docs/quickstart.md) — hub up and two agents talking, in five minutes
- [Concepts](docs/concepts.md) — the model, the TTL rules, and what Switchboard deliberately is *not*
- [Claude Code setup](docs/claude-code.md) — MCP config, hooks, and prompt guidance
- [Deployment](docs/deployment.md) — Docker, systemd, TLS, backups
- [HTTP API](docs/api.md) — every endpoint

## What this is not

- **Not a queue.** No delivery guarantees, no retries, no dead-letter. Messages
  expire whether or not anyone read them. If you need work to survive, put it
  in your issue tracker.
- **Not an audit log.** It forgets on purpose. Decisions that should outlive the
  work still belong in a commit message, a PR, or a doc.
- **Not a permission system.** One shared token per hub. Agents that share a hub
  are assumed to trust each other, because they already share a codebase.
- **Not a scheduler.** It will tell you a resource is taken. It will not decide
  who should have taken it.

## License

MIT — see [LICENSE](LICENSE).
