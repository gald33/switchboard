# Upgrading

What changes for you between releases that are not backwards compatible, in
the order you will hit them. Everything not listed here kept its behaviour.

## 2.1.0 → 2.2.0

Additive. A whole Claude Code session can be moved between environments
through the hub: `switchboard session handoff <agent>` publishes the
conversation as a capsule sealed to the room, and `switchboard session
receive` collects it and installs it for `claude --resume`. `export` and
`import` do the two halves against a file instead. The capsule is a
blackboard entry that expires on its own and is deleted the moment it is
collected; the hub never opens it. Nothing that worked in 2.1.0 changed.

The commands are new, so a peer running 2.1.0 or earlier cannot collect a
capsule you publish and will not recognise the pointer. Both ends need 2.2.0.

## 2.0.1 → 2.1.0

**The whisper envelope says `whisper` on the wire.** From 0.11.0 to 2.0.1 it
said `ask` in three places a reader never sees — the envelope marker
`m`, the HKDF label the pair key is derived under, and the AEAD context the
body is sealed under (`ask.body`) — while the tool, CLI and method had said
`whisper` since 1.0.0. The split was kept on purpose, to spare a break for a
name only humans read; it cost more than it saved when a page written to
the human name opened whispers under `message.body` and every one came back
unreadable, with no error to say why. Now the wire says what the tool says:
`m: "whisper"`, `switchboard/v1/whisper`, `whisper.body`.

### Upgrade readers before senders

A 2.1.0 client **opens** everything an earlier one sealed — the marker says
which names an envelope was sealed under, and the old names are still
accepted on the way in. The other direction does not hold: **a 2.0.1 client
cannot open a 2.1.0 whisper**, and reports it `unreadable` exactly as it
would a sender whose exchange key it has not seen. So upgrade the agents
that *receive* whispers first, and the ones that send them last. In a room
where one process does both, upgrade it with everybody it whispers to. The
hub is not involved — it never opens an envelope — and needs no change.

### `Client.ask` is removed

The deprecated alias for `whisper`, kept since 1.0.0, is gone from the sync
and async clients. Nothing else answered to the old name any longer. The
wire `type` `"ask"` a 0.11.0 sender puts on a message still opens.

### Also new, none of it breaking

- **A book of known rooms**, per machine, at `~/.switchboard/known-rooms.json`.
  Written by `init`, `join`, `keygen --as-invite`, any `--invite` command and
  any command run inside an `init`-ed checkout. It holds *references* — the
  variable, the checkout, or the invite a key arrived as — never a key's
  value. `SWITCHBOARD_KNOWN_ROOMS` moves it; set it empty to disable it.
  `switchboard rooms --known`, `--forget`, `--label` manage it.
- **`rendezvous` sweeps every known room** by default, read-only, and reports
  per room who is there and who has a listener parked; `--here` keeps it to
  this room. New `switchboard find <name or branch>` is the same sweep with a
  peer in mind. `--room <label>` runs any command in a known room.
- **`listen` parks in more than one room**, in one process: this room, the
  lobby every holder of your key shares, and every room this machine joined,
  was invited into or minted in the last hour. `--no-lobby`, `--in <label>`
  and `--only <label>` adjust that. The wake payload keeps `messages` first
  and adds `room`, `role` and `agent_id`.
- **Minted keys never start with a hyphen**, so `--key <key>` cannot be read
  as a flag. Existing keys are unaffected.
- **The coordination skill is a third the length**, with one table naming
  every primitive on both surfaces; `init` upgrades an installed copy in
  place, as it does for every recorded revision.

## 2.0.0 → 2.0.1

Server only. The hub's CORS allow-list now includes the two write-key
headers, so a browser page can present a room's write key across origins.
On 2.0.0 such a page signed correctly and was refused at the preflight,
before the hub saw the request; the read-only viewer never hit it. Clients
are unchanged; self-hosters upgrade the hub. The managed hub already runs it.

## 1.x → 2.0

2.0 adds **write-protected rooms**: a room whose identifier is derived from a
write key, where the hub refuses every write that key did not sign. Holding
the workspace key alone now means *reading* such a room. The design and the
mechanism are in [encryption.md](encryption.md#read-only-rooms-enforced-by-the-hub);
this is the list of things that stop working the way they did.

### Upgrade the hub before the clients

A 1.x hub does not know a write signature from any other header. A `ws_…`
room on a 1.x hub is therefore protected by nothing: every 2.0 client signs
correctly, the hub ignores it, and anyone holding the key can still write.
Nothing errors. Deploy the hub first, then the clients — or, on the managed
hub, which is upgraded with the release, simply upgrade clients.

Check what is *running*, not what is checked out: `switchboard health`
reports the hub's version.

### 1.x agents in a 2.0 room are readers

A 1.x client cannot sign, so in a write-protected room every one of its
writes is refused with a 403 it reports as a plain error. It can still read
everything. Rooms that already exist — `w_…` identifiers, `org/repo` names,
every room minted before 2.0 — are unchanged for every version of client.

### `switchboard keygen` prints an env block, not a bare key

```
SWITCHBOARD_WORKSPACE=ws_…
SWITCHBOARD_KEY=…
SWITCHBOARD_WRITE_KEY=…
```

Three values that only work together, in the shape `whoami --env` already
printed. Anything doing `export SWITCHBOARD_KEY=$(switchboard keygen)` now
sets a three-line value and must change:

```bash
switchboard keygen > room.env
set -a; . ./room.env; set +a
```

or `switchboard keygen --json`, which carries `write_key` and
`workspace_token` as well.

### `init --new-key` mints a write-protected room

The opaque workspace `--new-key` always chose is now derived from a freshly
minted write key, and the write key is written to
`.claude/settings.local.json` beside the key. Two consequences:

- **Teammates need the write key.** The handover line is now
  `switchboard init --key <key> --write-key <write key>`, with no `-w`: the
  write key names the room. The old line, `init --key <key> -w ws_…`, still
  works and leaves that teammate able to read the room and nothing else.
- **Other environments need a third secret.** `whoami --env` prints
  `SWITCHBOARD_WRITE_KEY` with the other two; add it to the cloud or CI
  secret store. `SWITCHBOARD_WORKSPACE` becomes optional for anyone holding
  it, since the key names the room, and if both are set they must agree or
  the client refuses to start.

`init` without `--new-key` is unchanged: it mints a key and keeps the derived
`org/repo` workspace, which cannot be protected because a chosen name is not
the hash of anything.

### Invites carry the write key unless told not to

`switchboard invite` hands over membership — hub, token, key, and now the
write key. `invite --read-only` hands over reading, and every `invite --link`
is read-only, because a browser is a viewer. `Invite` gained a `write_key`
field (`wk` on the wire); 1.x clients ignore it, and an invite without one
encodes to exactly the bytes it did before.

### Side rooms need `write_key` in the scope

The MCP `keygen` tool returns `write_key` and `workspace_token` alongside
`key` and `workspace`, and `custom_scope` on `say`, `dm`, `inbox`, `claim`
and `release` accepts `write_key`. A scope carrying only the workspace and
key reads the side room and cannot post to it — the same is true of the SDK's
`custom_scope=` argument.

### New error, and one change to error bodies

A refused write is `403 {"error": "read_only", "detail": "…"}`. The SDK
raises `ReadOnlyRoom` (a `SwitchboardError`, status 403); the CLI exits 1 and
says what to set. `Client.can_write` answers the question before the hub
does.

HTTP errors raised with a structured detail now arrive flat —
`{"error": …, "detail": …}` — rather than nested under `detail`. No 1.x
endpoint produced one, so no existing error body changed shape.

### What is new in the SDK

`ClientConfig.write_key`, `Client.writer` and `Client.can_write`,
`ReadOnlyRoom`, `RoomWriteKey` and `generate_write_key`,
`Invite.write_key` and `Invite.resolve_write_key`, and in
`switchboard.testing`: `hub(write_key=…)`, whose workspace is then the room
that key names, and `hub.client(write_key="")` for the reader whose writes
should be refused.

### What did not change

The sealed-message format, key epochs, blinding, `whisper`, the lobby
derivation and per-process signing are all as they were. A 2.0 client in a
1.x room on a 1.x hub behaves exactly like 1.7.
