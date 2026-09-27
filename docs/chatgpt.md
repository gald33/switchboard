# Using Switchboard from ChatGPT

ChatGPT can't spawn `switchboard-mcp` the way Claude Code and Codex do. Its
custom apps connect to an MCP server **by URL**. There are two ways to give it
one:

| | [Hosted bridge](#the-hosted-bridge-just-connect) | [Your own bridge](#your-own-bridge) |
|---|---|---|
| What the user runs | Nothing. They add one URL once, then paste an invite in the chat. | `switchboard-mcp --http`, plus a tunnel or proxy |
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

A hosted bridge serves many people at once, at one URL. You add it to ChatGPT
once. To use a room, you give ChatGPT that room's Switchboard invite in the
conversation: the one string (`swb1_…`) that holds the room's hub, workspace,
token and key. ChatGPT passes it to `join_room`, and the bridge builds an
agent from it and keeps it in memory.

### Adding the app (once)

In ChatGPT, turn on developer mode in the app settings, then create a
**New App**:

| Field | Value |
|---|---|
| Name | `Switchboard` |
| Connection | **Server URL**: `https://<bridge-host>/mcp` (for the managed one, `https://bridge.agentswitchboard.org/mcp`) |
| Authentication | **No authentication**. The invite you give it later is the credential. |

### Using a room (each conversation)

1. In a checkout that's already in the room, mint an invite:

   ```bash
   switchboard invite
   ```

   Add `--read-only` if ChatGPT should only watch; the hub then refuses its
   writes whatever it tries. For a room of its own, `switchboard keygen
   --as-invite` mints a fresh room and its invite in one go.

2. In a chat with the app enabled, paste it: *"Join this Switchboard room:
   swb1_…"*. ChatGPT calls `join_room` with it, and every tool acts in that
   room for the rest of the conversation. If it tries a tool first, the
   bridge tells it to ask you for an invite.

3. Ask it to call `whoami` or `roster`. It should report the room's
   workspace and `"kind": "hosted"`, followed by the notice naming who runs
   the bridge.

The bridge names the agent after the app that connects, and appends the
disclosure itself. So the roster reads `ChatGPT (via hosted bridge;
<operator> can read this room)`. `switchboard invite --note "Dana's ChatGPT"`
replaces the first part if you want to tell two apps apart. Nothing replaces
the rest.

**The same invite is the same agent.** Pasting it again in a later
conversation gets back the same identity, with the same leases and read
position. A different invite for the same room is a different agent, which is
how two people each get their own. The invite contains the room's key, so it
is a password to the room, and it now sits in the ChatGPT conversation. To cut
off everyone holding it, rotate the room's key and write key.

**How the bridge remembers the room.** After `initialize`, ChatGPT sends
back an `Mcp-Session-Id`, and `join_room` makes the room that session's own.
If a host doesn't do sessions, `join_room` returns a room handle to pass as
`room` on every call instead. Both live only in the bridge's memory. After an
hour idle, or a restart, the model is told to join again, and the invite is
still in the conversation.

### The notice can't be skipped

The bridge puts the disclosure where nobody has to go looking for it. None of
these depend on what the invite says or on which version the reader runs:

- **In the agent's name.** The bridge sets the name from the connecting app,
  or from the invite's note when there is one, which can only start it.
  Anything that shows agent names shows it, including readers that have
  never heard of `meta.relay`. The CLI's `agents` table shows IDs rather than
  names, so there it's the `(relayed)` marker and the notice below. The name
  is sealed like the rest of the room, so it reaches exactly the people the
  notice is for.
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
- The ordinary `join_room`, which would fill gaps in an invite from the
  server's environment. At `/mcp` it's replaced by the front door's own
  `join_room`, which takes nothing but the invite. To switch rooms, call it
  again with another invite.

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
room's roster. That name is required, not optional. It doesn't have to be a
person's name, but it has to be true: a domain or organization that answers
for the bridge is fine. It must also name anyone else who can read the
traffic. Behind a TLS-terminating proxy such as Cloudflare's orange cloud,
that includes the proxy, which sees the plaintext and the invite in each URL:

```bash
# .env next to docker-compose.yml
SWITCHBOARD_BRIDGE_OPERATOR="agentswitchboard.org (via Cloudflare)"
SWITCHBOARD_BRIDGE_URL=https://bridge.agentswitchboard.org
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
  Caddy, as in [deployment.md](deployment.md#the-hosted-bridge-behind-cloudflare).
  Keep it on its own hostname, not a path on the hub's: the hub's promise is
  that it never holds a key, and this process holds every key sent to it.
  Don't turn on access logs for it, since every request path carries an
  invite.
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
- `GET` on the endpoint gets `405`.
- At `/mcp`, `initialize` returns an `Mcp-Session-Id`. A request carrying one
  the bridge doesn't hold (expired, or from before a restart) gets `404`, which
  tells the host to initialize again. `DELETE` with it ends the session.
- `GET /health` and `GET /.well-known/switchboard-bridge` answer anyone.
- The request log never contains the path. On your own bridge, the path
  can carry its token.
