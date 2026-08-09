# Setting up an environment

[Deployment](deployment.md) is about the hub. This is about everything that
connects to one: a laptop, a cloud coding session, a CI runner. The hub side
can be someone else's problem — this page assumes a hub already exists and you
have a token for it.

An agent needs four values. Where each one comes from is the whole subject:

| Value | Comes from | Secret |
|---|---|---|
| `SWITCHBOARD_URL` | `.mcp.json`, committed | no |
| `SWITCHBOARD_WORKSPACE` | `.mcp.json`, committed | no |
| `SWITCHBOARD_TOKEN` | the environment's own secret store, or `init` on a hub that self-issues | yes |
| `SWITCHBOARD_KEY` | the environment's own secret store | yes |

The client reads all four from the environment and nowhere else — there is no
config file, no `~/.switchboard/config`, no user-level scope. What looks
per-repo is your agent runner injecting environment from files in the repo:
`.mcp.json`'s `env` block carries the two non-secrets, and (for Claude Code)
`.claude/settings.local.json` carries the key on a developer machine, where it
is gitignored and never leaves.

That asymmetry is the thing to hold onto. **The two non-secrets are per-repo
and travel with the clone. The two secrets are per-environment and are set
once for all repos in it.** Every flow below follows from that.

Whatever runs the agent also needs the package: `pip install agent-switchboard`
in the image, or `switchboard-mcp` is not on `PATH` and the MCP server simply
fails to start.

## One repo

The baseline. On your machine, in the repo:

```bash
switchboard init --new-key
```

Commit what it writes — `.mcp.json`, `.switchboard/hooks/`,
`.claude/settings.json`, the `switchboard-coordinate` skill, and the
`CLAUDE.md` section. Do **not** commit `.claude/settings.local.json`; `init`
adds it to `.gitignore` for you and checks that on every run.

`.switchboard/hooks/` holds the lifecycle hook bodies as plain `/bin/sh`
scripts. The agent's own config gets only a one-line shim pointing at them, so
the same scripts serve any runner — see [Codex CLI](codex-cli.md) — and a
clone that does not commit them ends up with hooks referring to files that are
not there.

Before it finishes, `init` checks that the workspace it just wired up is
actually reachable with the token you have. A workspace it minted is new by
construction, so on a hub that scopes tokens to workspaces — the managed hub
does — nothing is bound to it yet, and `init` registers a token and writes it
next to the key. Without that step every call 403s while the setup looks fine,
which is the failure this check exists to prevent. `--skip-token` opts out; it
is the only step that uses the network, and a hub it cannot reach is reported
rather than fatal, since the files it writes are correct either way.

Then set `SWITCHBOARD_TOKEN` and `SWITCHBOARD_KEY` in the cloud environment's
secret settings, and in your CI provider's secret store if CI agents should
join too. You do not need to set `SWITCHBOARD_WORKSPACE` there: the committed
`.mcp.json` supplies it, and the `SessionStart`/`Stop` hooks have the URL and
workspace baked into the command itself, so a session with only an ambient
token still registers against the right hub.

## Several repos in one environment

A cloud environment's variables are global to the environment, so it holds
exactly **one** `SWITCHBOARD_KEY`. Per-repo keys are not possible there. What
still varies per repo is the workspace, because each repo commits its own
`.mcp.json`.

So: mint once, adopt everywhere. In the first repo only —

```bash
switchboard init --new-key
```

— and in every repo after that, adopt the key it printed:

```bash
switchboard init --key <the key from the first repo>
```

Note the omitted `-w`. Each repo should derive its own workspace from its own
git remote; that is what keeps them separate rooms under a single key. Running
`--new-key` again in the second repo is the mistake to avoid — it mints a
second key that cannot coexist with the first in one environment.

`init` confirms which of those two you got: it names the workspace it paired
the key with, and says whether that name is one other clones will derive too.
A repo with a git remote gets a name every clone agrees on, which is what you
want here. A repo without one gets a name derived from this machine that
nothing else will arrive at on its own — fine for a single agent, but anything
that should join it needs `-w` with that exact name.

The limit is real, not a preference: if two repos need genuinely different
audiences — different teams, different blast radius — they need different
keys, and therefore different environments. One environment, one key.

## No repo at all

Supported, with two things that will bite.

There is no `.mcp.json` to be found, so register the MCP server in the
platform's own configuration, with the command `switchboard-mcp`, and supply
all four values as environment variables rather than two.

**Set `SWITCHBOARD_WORKSPACE` explicitly.** This is the one people skip. With
nothing set the client falls back to `default-<tag>` — as it does everywhere,
repo or not, since the fallback never reads a git remote. That keeps you from
landing in one shared room with every other unconfigured user on the hub, but
it is a name you did not choose and it will not match your other agents. Two
agents that should coordinate must be given the same workspace name on
purpose.

There is also no `.claude/settings.json`, so the `SessionStart` and `Stop`
hooks do not exist: nothing registers the agent at session start or releases
its leases at the end. The MCP tools all work, but the lifecycle that `init`
normally makes automatic becomes something the model has to remember, via
`checkin`. Say so in whatever instructions the platform does support.

## What the default workspace name means

Two different questions live here, and conflating them is easy. `init` picks a
name to *write down*. The client picks one when it finds nothing written.

**What `init` writes into `.mcp.json`:**

| Situation | Name |
|---|---|
| repo with a git remote | `org/repo`, derived from the remote |
| repo with no remote | `<directory>-<tag>` |
| `--new-key` | `w_<opaque>`, replacing whichever of the above applied |

The remote case is the good one, and the reason a laptop, a cloud session and
a CI runner agree for free: every clone derives the same name from the same
remote. A readable name is fine here because you chose it — you saw it printed,
and it goes into a committed file that every clone reads.

**What the client falls back to when nothing is set** — no `.mcp.json`, no
`SWITCHBOARD_WORKSPACE`, `init` never run — is always `default-<tag>`, and
never the git remote. A name nobody chose has to be unguessable: on a shared
hub, `acme/payments` is a room a stranger can walk into just by knowing where
you work. `init` is how you opt into a readable name deliberately.

The `<tag>` is eight hex characters, hashed from the machine plus the checkout
root — so two unrelated directories both called `api`, or simply two different
projects on one laptop, do not land on each other. It is a hash rather than a
hostname on purpose: the workspace is the one value the hub always sees in the
clear, so a readable machine name would make it the most identifying string you
hand over.

The root is the enclosing checkout, not the working directory, so two terminals
in different subdirectories of one project still meet with no configuration.
A git worktree counts as its own checkout and gets its own room. Two agents on
*different* machines deliberately do not match — anything coordinating across a
network should be naming its workspace on purpose, and a name nobody chose
silently matching is not a property worth relying on.

To hide the name from the hub entirely rather than merely disambiguate it, use
`switchboard init --new-key`, which replaces it with an opaque one. See
[Encryption](encryption.md).

## Checking it worked

From the new environment, once it is up:

```bash
switchboard agents
```

This is the check that matters, because the failure mode is otherwise
invisible. An agent holding the wrong key has an empty inbox that looks
exactly like "nothing new", its messages go where nobody reads them, and its
leases stop excluding anyone else's. `agents` tries to open each peer's name
and warns explicitly when it cannot, exiting non-zero so a hook notices too.

An agent pointed at the wrong *workspace* does not even show up in the same
listing — if two agents you expect to see each other are each alone in
`switchboard agents`, compare their `SWITCHBOARD_WORKSPACE` before anything
else.
