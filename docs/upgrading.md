# Upgrading

What changes for you between releases that are not backwards compatible, in
the order you will hit them. Everything not listed here kept its behaviour.

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
