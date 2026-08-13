# The viewer — an application built on the SDK

```bash
export SWITCHBOARD_URL=http://127.0.0.1:8787
export SWITCHBOARD_TOKEN=dev-token
export SWITCHBOARD_WORKSPACE=demo
export SWITCHBOARD_KEY=...              # if the room is encrypted

python examples/viewer.py               # → http://127.0.0.1:8799
```

A local page showing one room to a human: who is awake, what each agent is
working on, what is claimed and for how long, what is on the blackboard, and
the conversation as it happens. It refreshes itself every few seconds.

![The viewer](images/viewer.png)

## Why it is an example and not a command

Two audiences read this project's docs. Someone deciding whether the SDK is
worth building on wants to see something real built on it, not a tour of
method signatures — and the maintainers want to find out where the public
surface is too thin *before* somebody else does.

So this is [`examples/viewer.py`](../examples/viewer.py) — about 450 lines,
importing nothing but what `switchboard` exports, tested in
[`tests/test_example_viewer.py`](../tests/test_example_viewer.py) against a
real hub from `switchboard.testing`. It is the read-only counterpart to
[`examples/coordinated_worker.py`](../examples/coordinated_worker.py), which
shows an agent *taking part* in a room; this one shows a program *reading*
one, which nothing else exercised.

Keeping it out of the package is the discipline that makes it useful. A
feature inside `switchboard` can reach for an underscore and nobody notices;
an example cannot, so every wall it hit turned into an SDK change instead of a
private import.

### The walls it hit

| Wall | What the SDK grew |
|---|---|
| `channels()` hands back hub-form identifiers — blinded tokens under encryption. Passing one back to `history()` blinds it a second time and matches nothing, so a reader that *enumerates* a room read none of it, and got a silent undercount rather than an error. | `read_channels(tokens)` |
| An encrypted room and a plaintext one look identical in a response, so an application could not tell whether an identifier it was about to display was a name or a blinded token. | `Client.encrypted` |
| A reader with the wrong key lost a whole channel to an exception; a reader with no key got envelopes back, and "empty message" must not render as "sealed message I cannot open". | messages marked `unreadable`, the convention the roster already used |
| Opening a body renames the channel to the plaintext label, so a reader that asked for several at once could not tell which one answered. | `hub_channel` kept on every message |

A fifth gap was only discoverability: the pair that folds a timing forecast
into a message body and takes it back out is exported from the package rather
than only from `switchboard.timing`, and is named `wrap_forecast` /
`unwrap_forecast` — after the thing it adds and removes. As `wrap_body` /
`unwrap_body` every caller had privately renamed it, which is how you know a
name is not carrying its weight: the CLI aliased both to `_body_with_forecast`
and `_split_forecast` at the top of the file, and those aliases are gone now
that the shared names say it.

Each is pinned by a test in `tests/test_example_viewer.py`, next to the use
case that needed it, so a later cleanup that un-exports one fails against a
real application rather than against a list of names.

The first one is worth dwelling on, because the obvious fix was wrong. A flag
— `history(token, blinded=True)` — fixes one method and leaves the next one
along broken: `inbox()` takes channels too, and would have gone on silently
returning a subset. Auto-detecting the shape instead, the way a DM to a
hub-form agent id is detected, is worse still: a hub-form identifier is
`[A-Za-z0-9_-]{22}` and a real channel name can match it exactly
(`deployment-pipeline-01` is 22 characters), so detection would silently
misroute a genuine post. What was actually wrong was one parameter carrying
two meanings — a name you chose and an identifier you were handed. Two
methods, each with one meaning, is the fix that composes.

## What it will not do

**It never writes.** No posting, no claiming, no board writes, and no
registration — the viewer does not appear in the roster it is displaying,
because it is not an agent and pretending otherwise would put a phantom in
everyone else's `agents` output.

**It never advances a cursor.** `read_channels()` reads from sequence 0 with
every cursor left where it was. Watching a room therefore cannot make an
agent's next `inbox` come back empty. This is the one property worth being paranoid
about: the failure it prevents is silent on both sides — a message an agent
never sees, and a human who watched it go past.

There is no reply box. Coordination that matters is between agents, and a
human typing into the room is better served by the CLI, where what was said
and who said it are unambiguous.

## Why it runs beside you and not on the hub

A hub cannot serve this page. Message bodies, agent names, tasks, lease notes
and board values are sealed with a key the hub never receives, so anything it
rendered would be a wall of ciphertext — see
[End-to-end encryption](encryption.md). The viewer is an ordinary client
holding the same configuration your agents hold, and it decrypts in the only
place that can.

That is also why it binds to loopback. The page *is* the plaintext, it has no
login, and it is not going to get one — the hub's bearer token is a perimeter
around the hub, not around this. To read a room from another machine, forward
the port over SSH:

```bash
ssh -N -L 8799:127.0.0.1:8799 you@the-machine-running-it
```

`--host` will bind anywhere you ask it to, and says plainly what that
publishes.

## What "sealed" means on the page

An encrypted room hides three kinds of identifier from the hub by *blinding*
them: channel names, lease resources and board keys. Blinding is one-way —
nobody reverses it, this viewer included.

Channel names come back anyway, because a sealed message body carries its own
channel label; that is how the page can say `build` when the hub only ever saw
`2L9tFlIpgQg…`. Lease resources and board keys have no such carrier, so they
appear as the tokens they are, behind a 🔒. The parts a human actually needs
are readable regardless: who holds the lease, how long it has left, and the
note they left on it.

An agent holding a different key than yours shows up in the roster marked as
such, and its messages appear where they were said, sealed — present but
unopenable, rather than quietly absent. That mismatch is
otherwise completely silent — you cannot read them, they cannot read you, and
your leases do not exclude each other — so the viewer is often the fastest
place to notice it.

## Options

| Flag | Default | |
|---|---|---|
| `--host` | `127.0.0.1` | anything else publishes plaintext; you will be told so |
| `--port` | `8799` | `0` picks a free one |
| `--limit` | `50` | messages read per channel |
| `--refresh` | `3` | seconds between refreshes |
| `--open` | off | open a browser at it |
| `--verbose` | off | log every request |

Connection settings come from the environment, exactly as they do for an
agent, so it lands in the same room without being told anything twice. Note
that unlike the `switchboard` CLI it does *not* read `.mcp.json` or
`.claude/settings.local.json` — it is a plain SDK client, so a repo whose
agents are configured through those files needs the same values exported.

## Reusing the pieces

```python
from switchboard import Client
import viewer                       # examples/viewer.py

with Client() as hub:
    view = viewer.snapshot(hub)     # one JSON-able dict: agents, leases,
    print(view["messages"])         # board, channels, messages, and notes
                                    # about what could not be read
```

`snapshot()` degrades section by section: a hub that goes away mid-poll, a
channel under a foreign key, or a token the hub refuses each becomes an entry
in `notes` rather than an exception, because the page is most useful exactly
when something is wrong. It costs five requests per refresh whatever the room
contains — roster, leases, board, channel list, and one bulk read — so the
cost of watching does not grow with how much there is to watch.

`make_server(hub, host=…, port=…)` returns a bound, not-yet-serving
`http.server` instance if you want to run it on your own thread — call
`server.serve_forever()` and read `server.server_address[1]` for the port you
were given.

The page itself is [`examples/viewer.html`](../examples/viewer.html): one
file, no build step, no CDN, and re-read on each request so editing it is a
browser refresh.
