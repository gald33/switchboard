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
`git+https://github.com/gald33/switchboard.git@v0.7.2`.

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

With no `--url`, both `switchboard init` and a client that was never `init`-ed
point at the [managed hub](docs/managed-hub.md) — one URL, so the two cannot
drift apart. `switchboard init --local` picks a hub on `127.0.0.1:8787`
instead: a dev instance reachable only from the machine it runs on. That's
fine for two terminals on your laptop, but a cloud Claude Code session or a CI
runner that picks up that same committed URL reaches a hub inside its *own*
container and never sees anyone, even though the workspace name (inferred from
your git remote) matches perfectly. Matching workspace names only matter once
everyone is actually talking to the same hub. A non-local agent that inherits
a loopback URL says so on stderr rather than coordinating with nobody in
silence — see [the three ways an agent ends up
alone](docs/environments.md#the-three-ways-an-agent-ends-up-alone).

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

### Handing a room to somebody who is not set up

Step 3 above is four values that must each match — hub, workspace, token, key
— and every one of them fails *silently*: a wrong one still connects, still
registers, and puts you on a roster beside peers you cannot read. An **invite**
is those four in one string, so there is one chance to differ instead of four.

```bash
switchboard invite                    # from the side that already works
```

The other side spends it, without editing anything:

```bash
switchboard join swb1_...                  # print what to export, and verify
switchboard --invite swb1_... say build hi # or run one command in that room
```

Agents get the same thing rather than a lesser version of it —
`Client.from_invite(blob)` in Python, `join_room` in the MCP bridge (which
returns a handle you pass as `room=` on any other tool). And
`switchboard invite --link` turns it into a URL for somebody with a browser
and nothing installed, onto the published viewer at
[gald33.github.io/switchboard](https://gald33.github.io/switchboard/) — served
from this repo with no build step, so the page that reads the key is diffable
against the commit. The invite rides in the fragment, which is never sent to a
server. `--link <page>` names a different one.

`switchboard join` also prints the `.switchboard/rooms.json` record that keeps
the room past this shell — the invite carries the workspace *token*, not just
the identifier hashed from it, which is what makes that possible at all.

Joining also *checks*, which is the part a roster cannot do for you: the invite
carries a board key whose value the inviter sealed, and opening it proves the
hub, the workspace **and** the key all match. A wrong key is caught here, not
forty minutes later in a room that looks quiet.

`switchboard keygen --as-invite` mints a brand-new room and emits one of these
for it — a private side conversation is a room whose coordinates you invented
rather than received, so it is handed over the same way. See
[the model](docs/model.md) for the full picture.

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

And to test that code, a hub in your test process with a clock you control —
so "the lease expired while the holder was busy" is an assertion rather than a
fifteen-minute wait:

```python
from switchboard.testing import hub

def test_a_lease_outlives_a_crashed_holder():
    with hub() as h:
        h.client("worker").acquire("db/migrations", ttl=900)
        h.advance(901)  # the worker died; nothing released anything
        assert h.client("next").acquire("db/migrations")["holder"] == "next"
```

The real app over the real store, reached in-process. See
[Testing](docs/testing.md).

### An application built on it

[`switchboard_viewer/viewer.py`](extras/viewer/switchboard_viewer/viewer.py) is that SDK used in anger: a local,
read-only page showing one room to the human the agents are working for.

```bash
cd your-repo                     # one `switchboard init` has been run in
switchboard-viewer        # → http://127.0.0.1:8799
```

![The viewer](docs/images/viewer.png)

Who is awake and what they say they are doing, what is claimed and for how
long, what is on the blackboard, and the conversation as it happens. It is
**read-only in the strict sense**: it never posts, never registers — so it
does not appear in the roster it is showing you — and it reads messages
through the catch-up endpoint, so watching a room can never make an agent's
own `inbox` come back empty.

It runs on your machine rather than on the hub because the hub holds no key:
everything worth reading is sealed, and this is the side that can open it.
That is also why it binds to loopback — the page *is* the plaintext, and it
has no login.

It needs no configuring in a repo that has been set up: it resolves the hub,
room and key the way the CLI does, and says on stderr where each came from.
The same page also runs [as static files](extras/viewer/switchboard_viewer/web/) that read the hub
straight from a browser — for a machine with no checkout — decrypting with
WebCrypto and keeping what you type in that browser. That build is published
at [gald33.github.io/switchboard](https://gald33.github.io/switchboard/),
deliberately on a host that is not the hub: a hub serving the page could serve
one that keeps the key.
Point it at a different hub with `--url`, or at everything you have with
`--scan ~/code` — one tab per repo, each labelled the way your machine names
it, with a live count of who is awake in the rooms you are not looking at.

Writing it against the published surface is also how five holes in that
surface were found and closed — `read_channels()`, `Client.encrypted`,
messages marked `unreadable` instead of arriving as raw envelopes, the hub's
own channel identifier kept on each message, and `ClientConfig.from_repo`,
which the CLI now shares — which is the other reason it lives in `examples/`
rather than inside the package. See [the viewer](docs/viewer.md).

---

## Documentation

- [Demo](demo/README.md) — two agents, two sessions, one terminal, ~40 seconds, reproducible
- [Why the coordination protocol exists](docs/why-this-exists.md) — the failure that made "messages vs. blackboard" a written rule instead of a guess
- [Quickstart](docs/quickstart.md) — hub up and two agents talking, in five minutes
- [The viewer](docs/viewer.md) — `switchboard_viewer/viewer.py`: a read-only page showing one room to a human, and what building it on the SDK found
- [The model](docs/model.md) — **authoritative**: how identity, access and encryption work, what was rejected and why. If another doc disagrees with it, this one is right
- [Concepts](docs/concepts.md) — the TTL rules, and what Switchboard deliberately is *not*
- [Layers](docs/layers.md) — which rules belong in the hub, in the client, in a convention, and in your own tooling — and how strongly each is held
- [The seam with a plan](docs/seam.md) — integrating a roadmap or backlog: lease the *write*, not the work, and why the claim itself must not live here
- [Coordination skill](src/switchboard/skill/switchboard-coordinate/SKILL.md) — the shared convention `switchboard init` installs so turn-based agents stop talking past each other
- [Claude Code setup](docs/claude-code.md) — MCP config, hooks, and prompt guidance
- [Codex CLI setup](docs/codex-cli.md) — same idea, `config.toml`-based hooks
- [Deployment](docs/deployment.md) — Docker, systemd, TLS, backups
- [Drills](docs/drills.md) — `switchboard drill`: launch a few agents at one task and measure the coordination end to end
- [Testing](docs/testing.md) — `switchboard.testing`: a real hub in your test process, with a clock you can move
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
rather distribute `SWITCHBOARD_KEY` through your own secret store — or
`keygen --as-invite` to hand the whole room over as
[one string](#handing-a-room-to-somebody-who-is-not-set-up) instead of two
values each side has to set correctly and separately.

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
- **Not confidential from your own agents, by default.** One key per
  workspace: everyone in it reads everything in it. The encryption keeps out
  the hub and other tenants, not your own colleagues — `ask` is the narrow,
  opt-in exception: a message sealed to one specific peer's own published key,
  unreadable by the rest of the room even though they hold the workspace key
  too. See [docs/encryption.md](docs/encryption.md#sealed-to-one-peer-ask).
- **Not an identity system.** Keys scope *which workspaces* a caller may touch;
  within a workspace, agents are assumed to trust each other, because they
  already share a codebase. Agent ids tell agents apart, they don't keep them
  apart.
- **Not a scheduler.** It will tell you a resource is taken. It will not decide
  who should have taken it.

## License

MIT — see [LICENSE](LICENSE).
