# Using Switchboard from ChatGPT

ChatGPT can't spawn `switchboard-mcp` the way Claude Code and Codex do. Its
custom apps connect to an MCP server **by URL**. There are two ways to give it
one:

| | [Hosted bridge](#the-hosted-bridge-just-connect) | [Your own bridge](#your-own-bridge) |
|---|---|---|
| What the user runs | Nothing. They paste a URL. | `switchboard-mcp --http`, plus a tunnel or proxy |
| Who holds the room's key | The bridge's operator, in memory | The user, on a machine they control |
| Who can read the room | OpenAI **and the operator** | OpenAI |
| Other agents are told | Yes, on every roster | Nothing new to tell |

**Neither is end-to-end encrypted to the user's own devices, and nothing
could make ChatGPT so.** The model has to read messages in order to reason
about them. It also writes the tool calls (`say "…"`, `claim …`) itself, on
OpenAI's servers, and a model can't encrypt. So whatever server receives
those calls receives plaintext. The only choice is whether that server is the
user's own or somebody else's.

## The hosted bridge: just connect

A hosted bridge serves many people at once. Each ChatGPT app's URL carries a
Switchboard invite: the one string (`swb1_…`) that already holds a room's
hub, workspace, token and key. The bridge builds an agent from that invite on
the first call and keeps it in memory.

### Connecting (for anyone in a room)

1. In a checkout that's already in the room, mint an invite for ChatGPT alone:

   ```bash
   switchboard invite
   ```

   Nothing else is needed to name it. The bridge names the agent after the
   app that connects, reported in its `initialize` request, and appends the
   disclosure itself. So the roster reads `ChatGPT (via hosted bridge;
   <operator> can read this room)`. `--note "Dana's ChatGPT"` replaces the
   first part if you want to tell two apps apart. Nothing replaces the rest.

   Add `--read-only` if ChatGPT should only watch. The hub then refuses its
   writes whatever it tries.

2. In ChatGPT, turn on developer mode in the app settings, then create a
   **New App**:

   | Field | Value |
   |---|---|
   | Name | `Switchboard` |
   | Connection | **Server URL**: `https://<bridge-host>/mcp/<the invite>` |
   | Authentication | **No authentication**. The invite in the URL is the credential. |

3. Enable the app in a chat and ask it to call `whoami`. It should report the
   room's workspace and `"kind": "hosted"`, followed by the notice naming who
   runs the bridge.

The URL is a password to the room. Anyone holding it can act as that agent,
and it contains the key. To cut ChatGPT off, rotate the room's key and write
key. To give each person their own identity, mint one invite per person;
reusing one invite means sharing one agent.

### The notice can't be skipped

The bridge puts the disclosure where nobody has to go looking for it. None of
these depend on what the invite says or on which version the reader runs:

- **In the agent's name.** The bridge sets the name from the connecting app,
  or from the invite's note when there is one, which can only start it. Every roster reader shows names, including older CLIs,
  the web viewer, and anything else that has never heard of `meta.relay`.
  The name is sealed like the rest of the room, so it reaches exactly the
  people the notice is for.
- **On every result ChatGPT gets.** Every tool result, errors included,
  carries the notice as a text block of its own. So does the `initialize`
  instructions ChatGPT receives on connecting. The model can't use the room
  through the bridge without being handed it, and can correct a user who
  assumes the conversation is private to their devices.
- **On current rosters, as data.** `roster` (MCP) marks the agent with
  `relay: {operator, source, commit, image, about}` and adds a `RELAY_NOTICE`
  telling the model to tell its user. `switchboard agents` marks it
  `(relayed)` and prints the same notice.

The notice is deliberately blunt. The bridge holds the room's key, so its
operator can read **the whole room**: every message, board entry and lease
note, not only what the ChatGPT agent is shown. Whispers between other agents
stay sealed to their recipients. The hub still sees only ciphertext.

### What it withholds

These tools reach for the machine the bridge runs on, which here is somebody
else's server shared with every other room. So the bridge doesn't serve them:

- `session_handoff`, `session_import` and `session_resume`, which move Claude
  Code transcripts.
- `join_room`, which would fill gaps in an invite from the server's
  environment. To use a second room, add a second app with that room's invite.

For the same reason, an invite that leaves its key out (`invite --no-key`) is
refused rather than completed from the operator's environment. So is an invite
for a hub the bridge doesn't serve: by default only the managed hub, set with
`--hub` or `SWITCHBOARD_BRIDGE_HUBS`. Otherwise an invite could make the
bridge's server send requests to any address it can reach.

## What you can check, and what you cannot

The viewer answers "why trust the page?" by being hosted where its operator
can't change it: GitHub Pages, uploaded from the repo with no build step, so
what runs in your browser can be diffed against a commit
([viewer.md](viewer.md)). A bridge can't be hosted that way. It is a server,
and it has to run somewhere its operator controls. So the chain is shorter,
and here is where it ends.

**What you can check:**

1. **What the bridge claims to be.** `GET https://<bridge-host>/.well-known/switchboard-bridge`
   returns the commit it was built from, the image digest it was started
   from, and the operator. The same fields are on the roster under `relay`.
2. **That the image is that commit.** `.github/workflows/bridge.yml` builds
   every bridge image on GitHub's runners and signs a build-provenance
   attestation. This is a statement made by GitHub, not by the operator, that
   the digest came from that commit and that workflow:

   ```bash
   gh attestation verify oci://ghcr.io/gald33/switchboard-bridge@sha256:<digest> \
     --repo gald33/switchboard
   ```
3. **That the code keeps nothing and says who can read.** The image is built
   from `src/switchboard/mcp_server.py` at that commit, and
   `tests/test_mcp_hosted.py` holds it to what this page says. It writes
   nothing to disk, takes nothing from the operator's environment, withholds
   the tools above, and declares itself on every roster.

**What you cannot check:** that the server behind the URL is running the
image it names. The digest it reports is its own word, and an operator who
wanted to read rooms could run something else and report the same digest.
That's the same limit as any web service that says it doesn't look. Build
provenance narrows "trust the operator" to "trust the operator to run the
image they name", but it doesn't close it.

What would close it is remote attestation from confidential-computing
hardware, such as AMD SEV-SNP or Intel TDX behind Google Confidential Space,
or AWS Nitro Enclaves. There the CPU vendor, not the operator, signs a
statement of which image digest is running, and the operator can't read the
enclave's memory. The bridge is ready for that: it is one stateless image
with its commit baked in. What's missing is hosting it on such hardware and
publishing the attestation next to `/.well-known/switchboard-bridge`. Until
then, the honest summary is the one on the roster: *the operator can read
this room.*

If that's not good enough for a room, use your own bridge.

### Running a hosted bridge (operators)

Whoever runs this can read every room sent to it, and is named on each
room's roster. That name is required, not optional:

```bash
# .env next to docker-compose.yml
SWITCHBOARD_BRIDGE_OPERATOR="Your Name <you@example.com>"
SWITCHBOARD_BRIDGE_URL=https://bridge.example.com
BRIDGE_IMAGE=ghcr.io/gald33/switchboard-bridge@sha256:<digest from the workflow run>

docker compose --profile bridge up -d bridge
```

- **Pin the image by digest.** Use the digest the `bridge.yml` run printed.
  The bridge publishes `BRIDGE_IMAGE` as the image it runs, and a tag like
  `:latest` can't be verified.
- **Pull it, don't build it.** An image built on the host carries no
  attestation, and its digest would prove nothing. That's why compose has no
  `build:` for it.
- **Put it behind a TLS proxy** at `SWITCHBOARD_BRIDGE_URL`, for example with
  Caddy, as in [deployment.md](deployment.md). Keep it on its own hostname,
  not a path on the hub's: the hub's promise is that it never holds a key,
  and this process holds every key sent to it.
- **Nothing to back up.** It keeps agents in memory, drops one after an hour
  idle, holds at most 256 at a time, and forgets everything on restart. A
  forgotten agent is rebuilt on its next call, with the same identity.

## Your own bridge

The bridge runs on your machine, so only you and OpenAI can read what it
reads. Give it the settings your other agents use, plus a stable id and a
token of its own:

```bash
export SWITCHBOARD_URL=https://hub.example.com
export SWITCHBOARD_WORKSPACE=my-org/my-repo
export SWITCHBOARD_KEY=...            # same key your other agents hold
export SWITCHBOARD_TOKEN=...          # the hub's perimeter token, if it has one
export SWITCHBOARD_AGENT_ID=chatgpt   # how other agents will address it
export SWITCHBOARD_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"

switchboard-mcp --http                # listens on 127.0.0.1:8788

cloudflared tunnel --url http://localhost:8788   # or: ngrok http 8788
```

Then add a New App with **Server URL** `https://<tunnel-host>/mcp/<SWITCHBOARD_MCP_TOKEN>`
and **No authentication**. ChatGPT connects from OpenAI's servers, so it needs
the tunnel's HTTPS address, not `127.0.0.1`. The app works only while your
machine and the tunnel are up.

| Flag | Default | |
|---|---|---|
| `--http` | off | Serve streamable HTTP instead of stdio |
| `--host` / `--port` | `127.0.0.1` / `8788` | Where to listen |
| `--token` | `$SWITCHBOARD_MCP_TOKEN`, else random | Accepted as `/mcp/<token>` or `Authorization: Bearer` |
| `--no-auth` | off | No token. Refused unless `--host` is loopback |
| `--hosted` | off | The hosted bridge above. Needs `--operator` |
| `--operator` | `$SWITCHBOARD_BRIDGE_OPERATOR` | Who runs a hosted bridge, as shown on rosters |
| `--hub` | `$SWITCHBOARD_BRIDGE_HUBS`, else the managed hub | Hubs a hosted bridge serves; repeatable |
| `--public-url` | `$SWITCHBOARD_BRIDGE_URL` | The HTTPS address, for printed links and the roster |

## Either way

- **Read-only tools run without asking.** `help`, `whoami`, `roster`,
  `claims`, `history`, `board_get` and `board_list` are marked
  `readOnlyHint`, so ChatGPT runs them without a prompt. Everything else asks
  first, including `inbox`, which moves your read cursor.
- **Presence and leases lapse between messages.** ChatGPT has no hooks and no
  background processes, so presence lapses after two minutes and leases after
  fifteen. Raise `ttl` on `checkin` or `claim` when that's too short.
- **Leave `checkin`'s `wait` at 0.** On your own bridge it holds the only
  request slot for up to 25 seconds.

## Protocol notes

This is the MCP streamable HTTP transport, in the subset a tools-only server
needs:

- Each JSON-RPC message is `POST`ed and answered with `application/json`.
- A notification alone gets `202` with no body.
- `GET` and `DELETE` on the endpoint get `405`.
- `GET /health` and `GET /.well-known/switchboard-bridge` answer anyone.
- The request log never contains the path, since the path is the credential.
