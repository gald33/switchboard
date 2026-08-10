# Switchboard

**An ephemeral orchestration hub for AI coding agents.**

*Early release — pre-1.0, the shape is still settling. Two minutes in:
[watch two agents coordinate](demo/README.md) before installing anything.*

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
agent keeps its claims and a dead one gives them up within the TTL of its last
heartbeat — 15 minutes by default — with nobody having to remember anything.

---

## Install

```bash
# Agent side: client + CLI only (one dependency, httpx)
pip install agent-switchboard

# Hub side: also the server
pip install "agent-switchboard[server]"

# With end-to-end encryption
pip install "agent-switchboard[crypto]"

# With the MCP bridge for Claude Code / any MCP client
pip install "agent-switchboard[all]"
```

To track `main` instead of a release — for unreleased fixes, or to
contribute — install from GitHub instead:
`pip install "agent-switchboard[all] @ git+https://github.com/gald33/switchboard.git"`,
or pin a commit or tag by appending it to the URL:
`git+https://github.com/gald33/switchboard.git@v0.4.6`.

## The fast path

From the root of a repo:

```bash
pip install "agent-switchboard[all]"
switchboard init
```

That's one command instead of hand-editing four files: it writes `.mcp.json`,
adds the `SessionStart`/`Stop` lifecycle hooks to `.claude/settings.json`,
appends a coordination section to `CLAUDE.md`, installs the coordination
protocol as a skill agents load on demand
(`.claude/skills/switchboard-coordinate/SKILL.md`), and — if you didn't
already point it at a hub — generates a dev token into a gitignored `.env` so
`docker compose up -d` just works. It merges into whatever is already in
those files and is safe to run again — untouched output from a past run gets
upgraded automatically (e.g. after a package update fixes a hook), and
anything that looks hand-edited is left alone unless you pass `--force`. See
[Use it from Claude Code](#use-it-from-claude-code) below for what it wires
up and why, or skip straight to `switchboard init --help`.

### Getting local, cloud, and CI agents onto the same hub

`switchboard init` with no `--url` defaults to a hub on `127.0.0.1:8787` — a
local dev instance reachable only from the machine it runs on. That's fine
for two terminals on your laptop, but a cloud Claude Code session or a CI
runner pointed at that same default would each spin up their *own* local
hub and never see each other, even though the workspace name (inferred from
your git remote) matches perfectly. Matching workspace names only matter
once everyone is actually talking to the same hub.

To get local + cloud + CI coordinating with each other:

1. Deploy one hub somewhere all three can reach it — see
   [Deployment](docs/deployment.md) for Docker, systemd, and TLS.
2. Run `switchboard init --url https://your-hub` in the repo and commit the
   `.mcp.json` it writes. Every clone of the repo — laptop, cloud session,
   CI checkout — now points at the same URL and workspace with no further
   config, because that file is part of the repo.
3. Set `SWITCHBOARD_TOKEN` in each environment separately: your shell
   profile locally, your cloud environment's secrets, your CI provider's
   secrets store. `init` deliberately never writes the token into a
   committed file, so this one step doesn't get automated away — it's the
   one thing each environment has to be told on its own.

For several repos sharing one cloud environment, for setups with no repo at
all, and for what a workspace defaults to when nobody names one, see
[Setting up an environment](docs/environments.md).

## Upgrading

Three different things live at three different scopes, which is easy to get
wrong:

| What | Scope | When |
|---|---|---|
| `pip install -U agent-switchboard` | the Python environment — per venv, or once per machine if global | on every machine running an agent |
| `switchboard init --force` | **one repo** (`--dir`, default cwd) | once per repo you want coordinating |
| restart Claude Code / the MCP server | one session | after upgrading, per project you have open |

The restart is not optional and not cosmetic: the MCP tool schema is read
once when the `switchboard-mcp` subprocess starts, so a session that was
already running will not see tools or parameters added by the upgrade no
matter what you tell the agent.

`init` is safe to re-run. Untouched output from a past run is recognized and
upgraded in place; anything that looks hand-edited is left alone unless you
pass `--force`.

Learned timing history is the exception to all of the above — see
[docs/adaptive-timing.md](docs/adaptive-timing.md). It lives in your home
directory rather than the repo, so there is nothing to install or upgrade,
and nothing to commit.

## Run a hub

```bash
export SWITCHBOARD_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
switchboard serve --host 0.0.0.0 --port 8787 --db ./switchboard.db
```

Or with Docker — no published image yet, so build it from a clone
(`docker-compose.yml` does the same thing):

```bash
git clone https://github.com/gald33/switchboard.git && cd switchboard
docker build -t agent-switchboard .
docker run -p 8787:8787 -e SWITCHBOARD_TOKEN=secret -v swb:/data agent-switchboard
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

The same MCP server works from any MCP-speaking coding agent — see
[`docs/codex-cli.md`](docs/codex-cli.md) for Codex CLI, which has an
equivalent `config.toml`-based hook system.

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

- [Demo](demo/README.md) — two agents, two sessions, one terminal, ~40 seconds, reproducible
- [Why the coordination protocol exists](docs/why-this-exists.md) — the failure that made "messages vs. blackboard" a written rule instead of a guess
- [Quickstart](docs/quickstart.md) — hub up and two agents talking, in five minutes
- [Concepts](docs/concepts.md) — the model, the TTL rules, and what Switchboard deliberately is *not*
- [Coordination skill](src/switchboard/skill/switchboard-coordinate/SKILL.md) — the shared convention `switchboard init` installs so turn-based agents stop talking past each other
- [Claude Code setup](docs/claude-code.md) — MCP config, hooks, and prompt guidance
- [Codex CLI setup](docs/codex-cli.md) — same idea, `config.toml`-based hooks
- [Deployment](docs/deployment.md) — Docker, systemd, TLS, backups
- [HTTP API](docs/api.md) — every endpoint
- [End-to-end encryption](docs/encryption.md) — run a hub that cannot read its own traffic
- [Managed hubs](docs/managed-hub.md) — running one *for other people*: multi-tenancy, what actually runs out first, and how congestion should degrade

## Encrypt it, and the hub can't read it either

A hub only ever needs to *route* and *compare*, never to read. So it doesn't
have to:

```bash
switchboard init --new-key                    # once, wherever you set it up
switchboard init --key <key> -w <workspace>   # on every other machine
```

`init` keeps the key out of git (`.claude/settings.local.json`, gitignored)
and puts only the workspace name in the committed `.mcp.json`. The key never
reaches the hub. The workspace name *does* — it is the routing key and cannot
be encrypted — which is why `--new-key` pairs it with an opaque one rather
than letting `acme/billing` become the most descriptive string the hub holds.

`switchboard keygen` still prints a bare key and workspace name if you would
rather distribute `SWITCHBOARD_KEY` through your own secret store.

Message bodies, blackboard values, lease notes, branch names and task
descriptions are sealed with AES-256-GCM before they leave the agent. Channel
names, lease resources and agent ids become opaque tokens the hub can still
compare for equality — which is all it needs to deliver a message or exclude a
second lease holder.

Costs 1.1µs to encrypt and 0.7µs to decrypt a message, against a ~1000µs
network round trip. Everything else — `claim`, `inbox`, `checkin`, the MCP
tools — behaves exactly as before.

What the hub stores once you do this:

```
channel = mYkpn3DkU7rhr_Qjk_objQ
body    = {"$swb":1,"n":"ARyT0f4DEQnXsJ2C","c":"4yTX0Vd1QuD_5Y38U7gkkZ5A…"}
```

The hub needs **no changes and no configuration** to support this — it cannot
tell an encrypted workspace from a plaintext one, so it cannot be
misconfigured into weakening one. Plaintext is padded to size buckets before sealing, so message *length* does
not leak either. What remains is timing, volume, and which opaque tokens are
equal — [what that reveals, and what hiding more would cost](docs/encryption.md).

## Sharing a hub between teams that don't trust each other

By default a hub has one token and every caller may use every workspace —
workspaces are a *namespace*, for keeping one team's coordination out of
another's way. That is the right shape for a hub your own agents share, and it
is what you get if you change nothing.

If a hub is shared by parties that shouldn't see each other's traffic,
workspaces become a *boundary* instead. Give `create_app` a resolver that maps
each key to the workspaces it may touch:

```python
from switchboard import Principal, StaticKeyResolver
from switchboard.server import create_app   # server extra; not at the package root

app = create_app(resolver=StaticKeyResolver({
    "key-acme":   Principal(key_id="acme",   workspaces=frozenset({"acme/app"})),
    "key-globex": Principal(key_id="globex", workspaces=frozenset({"globex/api"})),
    "key-ops":    Principal(key_id="ops",    workspaces=None),   # unrestricted
}))
```

Every workspace-bearing endpoint then returns 403 outside a key's scope,
enforced in one shared dependency rather than per-handler — see
[docs/managed-hub.md](docs/managed-hub.md). Clients need no changes.

## What this is not

- **Not a queue.** No delivery guarantees, no retries, no dead-letter. Messages
  expire whether or not anyone read them. If you need work to survive, put it
  in your issue tracker.
- **Not an audit log.** It forgets on purpose. Decisions that should outlive the
  work still belong in a commit message, a PR, or a doc.
- **Not confidential from your own agents.** One key per workspace: everyone in
  it reads everything in it. The encryption keeps out the hub and other
  tenants, not your own colleagues.
- **Not an identity system.** Keys scope *which workspaces* a caller may touch;
  within a workspace, agents are assumed to trust each other, because they
  already share a codebase. Agent ids tell agents apart, they don't keep them
  apart.
- **Not a scheduler.** It will tell you a resource is taken. It will not decide
  who should have taken it.

## License

MIT — see [LICENSE](LICENSE).
