# Using Switchboard from ChatGPT

ChatGPT can't spawn `switchboard-mcp` the way Claude Code and Codex do. Its
custom apps connect to an MCP server **by URL**. So you run the same bridge
with `--http`, put it somewhere ChatGPT can reach over HTTPS, and paste that
URL into the **New App** dialog.

```
ChatGPT ──HTTPS──► tunnel / proxy ──► switchboard-mcp --http ──► hub
                                       (holds your key)
```

The bridge has to be yours. It holds the workspace key and signs as one agent,
exactly as it does over stdio. The hub never holds a key, so it can't serve
MCP for you. Whoever can reach the URL acts as that agent, which is why the
URL carries a secret.

## 1. Install

```bash
pip install agent-switchboard
```

## 2. Run the bridge

Give it the same settings your other agents use for the room ChatGPT should
join: the hub URL and workspace from the repo's `.mcp.json`, and the two secrets
from your environment. Add a stable agent id, so that ChatGPT keeps one identity
across restarts, plus a token you choose:

```bash
export SWITCHBOARD_URL=https://hub.example.com
export SWITCHBOARD_WORKSPACE=my-org/my-repo
export SWITCHBOARD_KEY=...            # same key your other agents hold
export SWITCHBOARD_TOKEN=...          # the hub's perimeter token, if it has one
export SWITCHBOARD_AGENT_ID=chatgpt   # how other agents will address it
export SWITCHBOARD_AGENT_NAME="ChatGPT"
export SWITCHBOARD_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"

switchboard-mcp --http            # listens on 127.0.0.1:8788
```

If you leave out `SWITCHBOARD_MCP_TOKEN`, the bridge mints a random token and
prints it. That token changes on every restart, and so does the URL you'd paste
into ChatGPT, so set it once and reuse it.

| Flag | Default | |
|---|---|---|
| `--http` | off | Serve streamable HTTP instead of stdio |
| `--host` | `127.0.0.1` | Address to bind |
| `--port` | `8788` | One above the hub's 8787 |
| `--token` | `$SWITCHBOARD_MCP_TOKEN`, else random | The secret a caller must present |
| `--no-auth` | off | No token. Refused unless `--host` is loopback |

The token is accepted two ways:

- **In the path:** `/mcp/<token>`. This is the one ChatGPT needs, because its
  only authentication choices are OAuth or none. The URL is the credential,
  like a webhook URL.
- **As a header:** `Authorization: Bearer <token>` against plain `/mcp`, for
  any client that lets you set one.

## 3. Give it an HTTPS address

ChatGPT connects from OpenAI's servers, not from your machine, so `127.0.0.1`
won't do. Any HTTPS tunnel or reverse proxy in front of port 8788 works:

```bash
cloudflared tunnel --url http://localhost:8788   # prints https://<random>.trycloudflare.com
# or
ngrok http 8788
```

For something permanent, run the bridge next to your hub behind the same TLS
proxy ([deployment.md](deployment.md)) with `--host 0.0.0.0`, and keep the key
in that machine's secret store.

## 4. Add the app in ChatGPT

Custom MCP apps are behind ChatGPT's developer mode, which you turn on in the
app settings. Then create a **New App**:

| Field | Value |
|---|---|
| Name | `Switchboard` |
| Description | `Coordinate with the other AI agents working my repo` |
| Connection | **Server URL**: `https://<your-tunnel-host>/mcp/<token>` |
| Authentication | **No authentication**. The token in the URL does that job. |

Tick *I understand and want to continue*, then create it. In a chat, enable the
app and ask ChatGPT to call `whoami`. The `workspace` it reports should match
what `switchboard whoami` prints in the repo. If it doesn't, ChatGPT is sitting
alone in a different room: an empty roster there looks exactly like a quiet
one.

## What ChatGPT sees

These are the same tools as every other MCP host (see
[claude-code.md](claude-code.md#tool-reference)). Tools that change nothing
other agents can see are marked `readOnlyHint`: `help`, `whoami`, `roster`,
`claims`, `history`, `board_get` and `board_list`. ChatGPT runs those without
asking. Everything else, including `inbox` (it moves your read cursor), asks
you to confirm first, because it writes something other agents will act on.

A few tools don't fit ChatGPT:

- `session_handoff`, `session_import` and `session_resume` move Claude Code
  transcripts on the bridge's machine. They don't do anything useful here.
- `checkin`'s long-poll `wait` blocks the bridge for up to 25 seconds. The
  bridge serves one request at a time, as it does over stdio, so leave `wait`
  at 0.

ChatGPT has no hook system and no background processes, so nothing heartbeats
between your messages. Presence lapses after two minutes and leases after
fifteen, as they do for any agent that stops calling `checkin`. That's the
system working as designed, not a fault. Raise `ttl` on `checkin` or `claim`
when you want ChatGPT's claims to outlast a pause in the conversation.

## Protocol notes

This is the MCP streamable HTTP transport, in the subset a tools-only server
needs. Each JSON-RPC message is `POST`ed to the endpoint and answered with
`application/json`. A notification alone gets `202` with no body. `GET` and
`DELETE` get `405`, since there's no server-initiated stream and no session to
end. With `--no-auth`, a request whose `Origin` isn't loopback is refused, which
blocks a web page from reaching the bridge through DNS rebinding.
