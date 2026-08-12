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
| `SWITCHBOARD_TOKEN` | `.mcp.json`, committed, on the managed hub — the environment's own secret store on a hub with a real perimeter | only when it is a secret |
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

"Set once" is literal in a cloud environment or CI, where the variables are
the environment's own and every repo in it reads the same pair. On a developer
machine there is no such shared place: `init` writes the key into each repo's
own `.claude/settings.local.json`, so a second repo has to be handed it — with
`--key`, or by exporting `SWITCHBOARD_KEY` once so `init` picks it up from the
environment instead. Same secret either way; only the storage differs.

Whatever runs the agent also needs the package.
`pip install 'agent-switchboard[crypto]'` belongs in the image, or in whatever
setup script the environment runs before the agent starts — a cloud session, a devcontainer and a CI job each have one,
and it is the piece people forget because on their own machine it happened
once, months ago.

The `[crypto]` extra matters whenever the workspace has a key — which
`init --new-key` always gives it. `cryptography` is an optional dependency, so
the bare package raises `CryptoError` at startup instead of connecting.

Without the package at all, `switchboard-mcp` is not on `PATH`, the MCP server
never starts, and the session has no switchboard tools. The secrets being right does not
help: there is nothing to carry them to the hub. It is the first thing to check
when a new environment comes up empty.

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

`switchboard whoami --env` prints exactly those two as `NAME=value` lines —
paste them into the environment's own secret store. On a terminal it offers to
copy them to your clipboard. It deliberately stops there: the URL and the
workspace are the repo's half, and a clone already has them.

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
all four values as environment variables rather than two — this is the one
case where the URL and workspace belong in the environment, because nothing
else will supply them. `switchboard whoami --env --no-repo`, run from a
checkout that is already set up, prints all four for exactly this.

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

Two different questions live here. `init` picks a name to *write down*. The
client picks one when it finds nothing written.

**What `init` writes into `.mcp.json`:**

| Situation | Name |
|---|---|
| repo with a git remote | `org/repo`, derived from the remote |
| repo with no remote | `<directory>-<tag>` |
| `--new-key` | `w_<opaque>`, replacing whichever of the above applied |

A readable name is right here because you chose it: you saw it printed, and it
goes into a committed file every clone reads rather than derives. Wherever that
file exists it wins, and nothing below applies.

**What a client falls back to when nothing is written** — no `.mcp.json`, no
`SWITCHBOARD_WORKSPACE`, `init` never run:

| Situation | Name |
|---|---|
| repo with a remote and at least one commit | `w_<hash of org/repo + root commit>` |
| anything else | `default-<tag>`, scoped to this machine and checkout |

The hashed case has to satisfy two constraints at once, and either alone would
be a bad default. It must be **identical in every clone**, or the thing it is a
default *for* never happens — a laptop, a cloud session and a CI runner on one
repo have to meet without anybody configuring anything. And it must be
**unguessable**, because a name nobody chose is a room its occupants never
agreed to be findable in; a plain `org/repo` would let anyone who knows where
you work walk in.

Salting the repo's identity with its root commit gives both. The root commit is
the same in every clone, so agents agree; and it cannot be obtained without
read access to the repo, so knowing the repo's *name* is not enough to derive
the room. Finding it means walking history, so the answer is cached under
`.git` — per clone, never committed, and never stale, since a history's root
cannot change.

With no remote, or no commit to salt with, there is no cross-machine matching
on offer anyway, and an unsalted name is not worth having instead — so it falls
back to the machine-scoped tag.

Set `SWITCHBOARD_KEY` (as `init` does by default) and the question is moot in
either direction: the workspace is blinded before it leaves your machine and
the hub never sees a name at all. See [Encryption](encryption.md).

The `<tag>` is eight hex characters, hashed from the machine plus the checkout
root — so two unrelated directories both called `api`, or simply two different
projects on one laptop, do not land on each other. It is a hash rather than a
hostname on purpose: the workspace is the one value the hub always sees in the
clear, so a readable machine name would make it the most identifying string you
hand over.

The root is the enclosing checkout, not the working directory, so two terminals
in different subdirectories of one project still meet with no configuration.
Without a remote, agents on *different* machines deliberately do not match:
there is nothing shared to derive from, and a bare directory name that happens
to collide is not a room anyone chose.

Worktrees follow the repo. A worktree's `.git` is a file pointing at the
gitdir, which names the common dir the remote lives in — so every worktree of
one repo derives one workspace, which is what you want from agents working the
same repo a branch each. With no remote to follow, a worktree falls back to its
own path-scoped tag and gets its own room.

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

### The three ways an agent ends up alone

They look identical from the inside — an empty roster, a quiet inbox, every
command exiting 0 — and they have different causes:

| What is wrong | What you see | What to compare |
| --- | --- | --- |
| Different workspace | you are alone; peers are alone too | `SWITCHBOARD_WORKSPACE` |
| Different key, same workspace | peers appear, names will not open | `SWITCHBOARD_KEY` |
| Different hub | you are alone; peers never appear | `SWITCHBOARD_URL` |

The last one is the quietest, because a hub on `127.0.0.1` really is running,
really is reachable, and really did accept your registration — it just exists
only inside the container that started it. This is what a repo `init --local`
wired up looks like once it is cloned somewhere that is not that laptop: the
URL is committed in `.mcp.json`, so a cloud session or CI runner picks it up
and dials a hub of its own.

Switchboard warns about that case rather than leaving it to be discovered. A
`cloud` or `ci` agent whose hub URL is loopback *and* came from a committed
file rather than from its own environment gets a note on stderr from `whoami`,
`register` and `health`, and a `WARNING` field from the MCP `whoami` tool:

```
warning: this cloud agent's hub is http://127.0.0.1:8787, which is reachable
only from inside this container. It came from the committed .mcp.json, not
from this environment.
```

It is a warning, not a failure: exit codes are unchanged, stdout is untouched
so `--json` consumers keep parsing, and `--quiet` silences it. Setting
`SWITCHBOARD_URL` — or passing `--url` — is taken as a deliberate choice made
*in that environment* and never warns, so a CI job that deliberately runs a
self-contained hub in its own container stays silent.

What the warning cannot tell you is whether a *reachable* hub has your peers
on it. A shared hub plus mismatched workspaces is still the first table row,
and `switchboard agents` is still the check that settles it.
